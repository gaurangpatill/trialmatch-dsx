"""Source-grounded plain-language study summaries with validated current-source caching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .client import ClinicalTrialsClient
from .models import DetailRetrievalStatus, PlainLanguageSummaryPayload, Study
from .normalize import normalize_study

SCHEMA_VERSION = "2.0"
PROMPT_VERSION = "2.0"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_SECTION_ORDER = (
    ("investigating", "What is this study investigating?"),
    ("participation", "What might participation involve?"),
    ("intended_for", "Who is the study intended for?"),
    ("important", "Important things to know."),
)
_MISSING_SECTION_TEXT = {
    "investigating": "The official record does not provide enough detail to explain the study question.",
    "participation": "The official record does not specify what participation would involve.",
    "intended_for": "The official record does not provide enough detail to describe who the study is intended for.",
    "important": "The official record does not provide additional important details for a plain-language summary.",
}
_ALLOWED_ROOTS = {
    "identificationModule",
    "statusModule",
    "descriptionModule",
    "conditionsModule",
    "designModule",
    "armsInterventionsModule",
    "outcomesModule",
    "eligibilityModule",
}
_RETRYABLE_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
_UNSUPPORTED_PATTERNS = (
    r"\byou (?:are|would be|will be) eligible\b",
    r"\byou should (?:join|enroll|participate)\b",
    r"\b(?:will|is guaranteed to) (?:benefit|improve|cure)\b",
    r"\b(?:safe|risk[- ]free|no risks?)\b",
    r"\bmedical advice\b",
)
_SECTION_PATH_MARKERS = {
    "investigating": (
        ".descriptionModule.", ".conditionsModule.", ".armsInterventionsModule.",
        ".outcomesModule.",
    ),
    "participation": (
        ".descriptionModule.", ".armsInterventionsModule.", ".outcomesModule.",
    ),
    "intended_for": (".eligibilityModule.",),
    "important": (".statusModule.", ".designModule.", ".descriptionModule.", ".outcomesModule."),
}
# These words add grammar or plain-language framing but no new clinical fact. All
# other material words must occur in the statement's exact cited evidence.
_SAFE_FRAMING_WORDS = {
    "a", "about", "also", "an", "and", "applies", "apply", "are", "as", "at",
    "be", "been", "being", "by", "called", "clinicaltrials", "clinicaltrials.gov",
    "does", "for", "from", "given", "has", "have", "in", "intended", "is", "it",
    "its", "lists", "looking", "many", "may", "might", "not", "of", "on", "or", "other",
    "participants", "people", "population", "record", "researchers", "says", "study", "than", "that",
    "the", "their", "them", "they", "this", "those", "to", "up", "was", "were",
    "what", "which", "while", "who", "with", "without",
}
_EQUIVALENT_WORDS = {
    "iv": {"intravenous"},
    "questions": {"questionnaires"},
    "questionnaires": {"questions"},
    "check": {"measured", "measurement"},
    "checks": {"measured", "measurements"},
    "answer": {"complete"},
    "answers": {"completes"},
    "connected": {"relate", "related", "relationship"},
    "enrollment": {"enrolling"},
    "visited": {"seen"},
    "treatment": {"therapy"},
    "treatments": {"therapies"},
}


class SummaryError(RuntimeError):
    """Safe summary-generation failure without provider secrets or response bodies."""


class SourceEvidence(BaseModel):
    """An exact excerpt from one allowlisted official ClinicalTrials.gov field."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_path: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=500)


class GroundedStatement(BaseModel):
    """One plain-language statement and the official text supporting it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=180)
    evidence: list[SourceEvidence] = Field(min_length=1, max_length=3)


class SummarySection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    statements: list[GroundedStatement] = Field(default_factory=list, max_length=1)


class StructuredSummary(BaseModel):
    """Provider output retained in the cache alongside the rendered API string."""

    model_config = ConfigDict(extra="forbid", strict=True)

    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    investigating: SummarySection
    participation: SummarySection
    intended_for: SummarySection
    important: SummarySection


class SummaryRepository(Protocol):
    """Storage operations required by summary generation across SQLite/PostgreSQL."""

    def get_study(self, nct_id: str) -> Study | None: ...

    def upsert_studies(self, studies: list[Study]) -> int: ...

    def get_cached_summary(self, cache_key: str) -> dict[str, Any] | None: ...

    def publish_summary(self, **kwargs: Any) -> None: ...


class SummaryProvider(Protocol):
    name: str
    model: str

    async def summarize(self, study: Study) -> dict[str, Any]: ...


class DisabledSummaryProvider:
    """Explicit offline provider used by default and throughout automated tests."""

    name = "disabled"
    model = "disabled"

    async def summarize(self, study: Study) -> dict[str, Any]:
        raise SummaryError(
            f"LLM provider is disabled; no cached summary is available for {study.nct_id}"
        )


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def _official_source_fields(study: Study) -> dict[str, str]:
    """Flatten only summary-relevant official protocol fields into prompt evidence."""

    protocol = study.raw_source.get("protocolSection", {})
    if not isinstance(protocol, dict):
        return {}
    fields: dict[str, str] = {}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, str):
            cleaned = _normalized_text(value)
            if cleaned:
                fields[path] = cleaned
        elif isinstance(value, (int, float, bool)):
            fields[path] = str(value)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(item, f"{path}.{key}" if path else key)

    for root in sorted(_ALLOWED_ROOTS):
        if root in protocol:
            visit(protocol[root], f"protocolSection.{root}")
    return fields


def _structured_schema() -> dict[str, Any]:
    evidence = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source_path": {"type": "string"},
            "quote": {"type": "string"},
        },
        "required": ["source_path", "quote"],
    }
    statement = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "evidence": {"type": "array", "items": evidence, "minItems": 1, "maxItems": 3},
        },
        "required": ["text", "evidence"],
    }
    section = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "statements": {"type": "array", "items": statement, "maxItems": 1},
        },
        "required": ["statements"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "nct_id": {"type": "string"},
            "source_hash": {"type": "string"},
            **{name: section for name, _ in _SECTION_ORDER},
        },
        "required": ["nct_id", "source_hash", *[name for name, _ in _SECTION_ORDER]],
    }


class OpenAISummaryProvider:
    """OpenAI structured-output adapter with bounded retries and sanitized failures."""

    name = "openai"

    def __init__(
        self,
        *,
        model: str = DEFAULT_OPENAI_MODEL,
        api_key: str,
        base_url: str = DEFAULT_OPENAI_BASE_URL,
        timeout_seconds: float = 45,
        max_retries: int = 2,
        max_retry_delay_seconds: float = 8,
        max_http_requests: int = 75,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if not model or not api_key or not base_url:
            raise SummaryError("LLM model, API key, and base URL are required")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise SummaryError("LLM_BASE_URL must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise SummaryError("remote LLM_BASE_URL must use HTTPS to protect credentials")
        if (
            timeout_seconds <= 0
            or max_retries < 0
            or max_retry_delay_seconds <= 0
            or max_http_requests < 1
        ):
            raise SummaryError("LLM timeout and retry settings are invalid")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self._remaining_http_requests = max_http_requests
        self._client = http_client
        self._sleep = sleep
        self.last_usage: dict[str, int] = {}

    async def summarize(self, study: Study) -> dict[str, Any]:
        source_fields = _official_source_fields(study)
        if not source_fields:
            raise SummaryError(f"{study.nct_id} has no official fields suitable for summarization")
        body = {
            "model": self.model,
            "temperature": 0,
            "max_completion_tokens": 1200,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "trialmatch_grounded_study_summary",
                    "strict": True,
                    "schema": _structured_schema(),
                },
            },
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Explain the supplied ClinicalTrials.gov record in concise, everyday language. "
                        "Use no knowledge beyond supplied_source_fields. Every factual statement must cite "
                        "one to three exact supporting excerpts and their exact field paths. Use an empty "
                        "statements list when a section is unspecified. Do not infer eligibility, treatment "
                        "effects, procedures, duration, payment, risks, or medical advice. Do not copy long "
                        "technical descriptions; accurately paraphrase them. Return the identifiers unchanged."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "nct_id": study.nct_id,
                            "source_hash": study.source_hash,
                            "supplied_source_fields": source_fields,
                        },
                        sort_keys=True,
                    ),
                },
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response: httpx.Response | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    if self._remaining_http_requests <= 0:
                        raise SummaryError("OpenAI HTTP request limit reached")
                    self._remaining_http_requests -= 1
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=body,
                        headers=headers,
                        timeout=self.timeout_seconds,
                    )
                    if response.status_code < 400:
                        break
                    if response.status_code not in _RETRYABLE_STATUSES or attempt >= self.max_retries:
                        raise SummaryError(
                            f"OpenAI summary request failed with status {response.status_code}"
                        )
                except httpx.HTTPError as exc:
                    if attempt >= self.max_retries:
                        raise SummaryError(
                            f"OpenAI summary request failed after {attempt + 1} attempt(s): "
                            f"{type(exc).__name__}"
                        ) from exc
                await self._sleep(min(2**attempt, self.max_retry_delay_seconds))
            if response is None or response.status_code >= 400:
                raise SummaryError("OpenAI summary request failed")
            envelope = response.json()
            usage = envelope.get("usage", {})
            if isinstance(usage, dict):
                self.last_usage = {
                    key: value for key, value in usage.items() if isinstance(key, str) and isinstance(value, int)
                }
            choice = envelope["choices"][0]
            if choice.get("finish_reason") not in {None, "stop"}:
                raise SummaryError("OpenAI summary response was incomplete")
            message = choice["message"]
            if message.get("refusal"):
                raise SummaryError("OpenAI refused the summary request")
            content = message["content"]
            if not isinstance(content, str):
                raise TypeError("provider content must be a JSON string")
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise TypeError("provider JSON must be an object")
            return payload
        except SummaryError:
            raise
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise SummaryError(
                f"OpenAI summary response failed validation: {type(exc).__name__}"
            ) from exc
        finally:
            if owns_client:
                await client.aclose()


def summary_provider_from_environment() -> SummaryProvider:
    """Build a backend-only provider; disabled mode is the secure default."""

    provider = os.getenv("LLM_PROVIDER", "disabled").strip().casefold()
    if provider in {"", "disabled", "offline"}:
        return DisabledSummaryProvider()
    if provider in {"openai", "openai-compatible"}:
        try:
            timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", "45"))
            max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))
            max_http_requests = int(os.getenv("LLM_MAX_HTTP_REQUESTS", "75"))
        except ValueError as exc:
            raise SummaryError("LLM timeout and retry settings must be numeric") from exc
        return OpenAISummaryProvider(
            model=os.getenv("LLM_MODEL", DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL,
            api_key=os.getenv("LLM_API_KEY", ""),
            base_url=os.getenv("LLM_BASE_URL", DEFAULT_OPENAI_BASE_URL).strip()
            or DEFAULT_OPENAI_BASE_URL,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            max_http_requests=max_http_requests,
        )
    raise SummaryError(f"unsupported LLM_PROVIDER: {provider}")


def _word_stem(word: str) -> str:
    for suffix in ("ingly", "edly", "ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 3:
            return word[: -len(suffix)]
    return word


def _unsupported_material_words(statement: str, evidence: str) -> set[str]:
    statement_words = set(
        re.findall(r"[a-z][a-z0-9]*(?:\.[a-z0-9]+)*", statement.casefold())
    )
    evidence_words = set(
        re.findall(r"[a-z][a-z0-9]*(?:\.[a-z0-9]+)*", evidence.casefold())
    )
    evidence_stems = {_word_stem(word) for word in evidence_words}
    unsupported: set[str] = set()
    for word in statement_words:
        if word in _SAFE_FRAMING_WORDS or word in evidence_words:
            continue
        equivalents = _EQUIVALENT_WORDS.get(word, set())
        if equivalents.intersection(evidence_words):
            continue
        if _word_stem(word) in evidence_stems:
            continue
        unsupported.add(word)
    return unsupported


def _validate_structured_summary(study: Study, raw: dict[str, Any]) -> StructuredSummary:
    try:
        result = StructuredSummary.model_validate(raw, strict=True)
    except ValidationError as exc:
        raise SummaryError(
            f"summary output failed schema validation: {exc.error_count()} error(s)"
        ) from exc
    if result.nct_id != study.nct_id or result.source_hash != study.source_hash:
        raise SummaryError("summary output identifiers do not match the current source record")

    source_fields = _official_source_fields(study)
    for section_name, _ in _SECTION_ORDER:
        section = getattr(result, section_name)
        allowed_markers = _SECTION_PATH_MARKERS[section_name]
        relevant_fields = [
            path for path in source_fields if any(marker in path for marker in allowed_markers)
        ]
        if not section.statements and relevant_fields:
            raise SummaryError(
                f"summary output omitted available official information for {section_name}"
            )
        for statement in section.statements:
            normalized_statement = _normalized_text(statement.text)
            if any(
                re.search(pattern, normalized_statement, re.IGNORECASE)
                for pattern in _UNSUPPORTED_PATTERNS
            ):
                raise SummaryError(
                    "summary output contains an unsupported eligibility or medical claim"
                )
            evidence_text: list[str] = []
            for evidence in statement.evidence:
                if not any(marker in evidence.source_path for marker in allowed_markers):
                    raise SummaryError("summary output cites evidence from the wrong section")
                official_value = source_fields.get(evidence.source_path)
                normalized_quote = _normalized_text(evidence.quote)
                if (
                    official_value is None
                    or normalized_quote.casefold() not in official_value.casefold()
                ):
                    raise SummaryError(
                        "summary output contains evidence absent from the official source"
                    )
                evidence_text.append(normalized_quote)
            joined_evidence = " ".join(evidence_text)
            supported_numbers = set(re.findall(r"\d+(?:\.\d+)?", joined_evidence))
            statement_numbers = set(re.findall(r"\d+(?:\.\d+)?", normalized_statement))
            if not statement_numbers.issubset(supported_numbers):
                raise SummaryError(
                    "summary output contains a number absent from its cited evidence"
                )
            if unsupported := _unsupported_material_words(
                normalized_statement, joined_evidence
            ):
                raise SummaryError(
                    "summary output contains material words absent from its cited evidence: "
                    + ", ".join(sorted(unsupported)[:5])
                )
            copied = any(
                normalized_statement.casefold() == item.casefold() and len(item) > 100
                for item in evidence_text
            )
            if copied:
                raise SummaryError("summary output copied a long technical source passage")
    return result


def _render_summary(result: StructuredSummary) -> str:
    parts: list[str] = []
    for name, heading in _SECTION_ORDER:
        statements = getattr(result, name).statements
        text = " ".join(_normalized_text(statement.text) for statement in statements)
        parts.append(f"{heading} {text or _MISSING_SECTION_TEXT[name]}")
    return "\n\n".join(parts)


class StudySummaryService:
    """Validate, cache, coalesce, and bound paid generation for known studies."""

    def __init__(
        self,
        repository: SummaryRepository,
        provider: SummaryProvider,
        *,
        max_concurrency: int = 2,
        max_generation_requests: int = 25,
    ):
        if max_concurrency < 1 or max_generation_requests < 1:
            raise ValueError("summary generation limits must be positive")
        self.repository = repository
        self.provider = provider
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._remaining_requests = max_generation_requests
        self._request_lock = asyncio.Lock()
        self._in_flight: dict[str, asyncio.Task[tuple[PlainLanguageSummaryPayload, bool]]] = {}

    def cache_key(self, study: Study) -> str:
        material = ":".join(
            (
                study.nct_id,
                study.source_hash,
                SCHEMA_VERSION,
                PROMPT_VERSION,
                self.provider.name,
                self.provider.model,
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()

    async def summarize(self, study: Study) -> tuple[PlainLanguageSummaryPayload, bool]:
        key = self.cache_key(study)
        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.create_task(self._summarize_once(study, key))
            self._in_flight[key] = task
            task.add_done_callback(
                lambda completed, request_key=key: self._discard_completed(
                    request_key, completed
                )
            )
        return await asyncio.shield(task)

    def _discard_completed(
        self,
        key: str,
        task: asyncio.Task[tuple[PlainLanguageSummaryPayload, bool]],
    ) -> None:
        if self._in_flight.get(key) is task:
            self._in_flight.pop(key, None)

    async def _summarize_once(
        self, study: Study, key: str
    ) -> tuple[PlainLanguageSummaryPayload, bool]:
        cached = self.repository.get_cached_summary(key)
        if cached is not None:
            try:
                return self._validate_cached(study, cached), True
            except SummaryError:
                pass
        async with self._request_lock:
            if self._remaining_requests <= 0:
                raise SummaryError("summary generation request limit reached")
            self._remaining_requests -= 1
        async with self._semaphore:
            raw = await self.provider.summarize(study)
        structured = _validate_structured_summary(study, raw)
        payload = PlainLanguageSummaryPayload(
            nct_id=study.nct_id,
            source_hash=study.source_hash,
            plain_language_summary=_render_summary(structured),
        )
        cached_payload = {
            **payload.model_dump(mode="json"),
            "grounded_sections": structured.model_dump(mode="json"),
        }
        self.repository.publish_summary(
            cache_key=key,
            nct_id=study.nct_id,
            source_hash=study.source_hash,
            provider=self.provider.name,
            model=self.provider.model,
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            payload=cached_payload,
            created_at=datetime.now(UTC),
        )
        return payload, False

    @staticmethod
    def _validate_cached(study: Study, raw: dict[str, Any]) -> PlainLanguageSummaryPayload:
        sections = raw.get("grounded_sections")
        if not isinstance(sections, dict):
            raise SummaryError("cached summary lacks grounding evidence")
        structured = _validate_structured_summary(study, sections)
        expected_text = PlainLanguageSummaryPayload(
            nct_id=study.nct_id,
            source_hash=study.source_hash,
            plain_language_summary=_render_summary(structured),
        ).plain_language_summary
        try:
            payload = PlainLanguageSummaryPayload.model_validate(
                {
                    "nct_id": raw.get("nct_id"),
                    "source_hash": raw.get("source_hash"),
                    "plain_language_summary": raw.get("plain_language_summary"),
                },
                strict=True,
            )
        except ValidationError as exc:
            raise SummaryError("cached summary failed schema validation") from exc
        if payload.nct_id != study.nct_id or payload.source_hash != study.source_hash:
            raise SummaryError("cached summary identifiers do not match the current source record")
        if payload.plain_language_summary != expected_text:
            raise SummaryError("cached summary text does not match its grounded sections")
        return payload

    @staticmethod
    def _validate(study: Study, raw: dict[str, Any]) -> PlainLanguageSummaryPayload:
        """Compatibility validation entry point used by callers and focused tests."""

        structured = _validate_structured_summary(study, raw)
        return PlainLanguageSummaryPayload(
            nct_id=study.nct_id,
            source_hash=study.source_hash,
            plain_language_summary=_render_summary(structured),
        )


class _MemorySummaryRepository:
    """Process-local cache for records that must not enter the ingestion catalog."""

    def __init__(self) -> None:
        self.studies: dict[str, Study] = {}
        self.summaries: dict[str, dict[str, Any]] = {}

    def get_study(self, nct_id: str) -> Study | None:
        return self.studies.get(nct_id)

    def upsert_studies(self, studies: list[Study]) -> int:
        for study in studies:
            self.studies[study.nct_id] = study
        return len(studies)

    def get_cached_summary(self, cache_key: str) -> dict[str, Any] | None:
        return self.summaries.get(cache_key)

    def publish_summary(self, **kwargs: Any) -> None:
        study = self.studies.get(str(kwargs["nct_id"]))
        if study is None or study.source_hash != kwargs["source_hash"]:
            raise SummaryError("cannot cache a summary for a stale official source")
        self.summaries[str(kwargs["cache_key"])] = dict(kwargs["payload"])


async def load_official_study_for_summary(nct_id: str) -> Study:
    """Revalidate one official record using a cache isolated from bulk ingestion."""

    cache_dir = os.getenv(
        "SUMMARY_SOURCE_CACHE_DIR", "backend/data/cache/summary-source"
    )
    async with ClinicalTrialsClient(cache_dir=cache_dir) as client:
        detail = await client.fetch_detail(nct_id, use_cache=False)
    return normalize_study(
        detail.record,
        detail.retrieved_at,
        detail_status=DetailRetrievalStatus.RETRIEVED,
    )


class OnDemandStudySummaryService:
    """Generate for any valid NCT ID without mutating the ingestion-owned catalog.

    Every request revalidates the official record. If its hash matches a catalog row,
    the durable repository cache is used. Unknown or newly changed records use an
    isolated process-local cache until post-ingestion Supabase integration is safe.
    """

    def __init__(
        self,
        repository: SummaryRepository,
        provider: SummaryProvider,
        official_study_loader: Callable[[str], Awaitable[Study]] | None = None,
        *,
        max_concurrency: int = 2,
        max_generation_requests: int = 25,
    ):
        self.repository = repository
        self.provider = provider
        self.official_study_loader = official_study_loader or load_official_study_for_summary
        self.summary_service = StudySummaryService(
            repository,
            provider,
            max_concurrency=max_concurrency,
            max_generation_requests=max_generation_requests,
        )
        self.external_repository = _MemorySummaryRepository()
        self.external_summary_service = StudySummaryService(
            self.external_repository,
            provider,
            max_concurrency=max_concurrency,
            max_generation_requests=max_generation_requests,
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._in_flight: dict[str, asyncio.Task[tuple[PlainLanguageSummaryPayload, bool]]] = {}

    async def summarize_nct_id(
        self, nct_id: str
    ) -> tuple[PlainLanguageSummaryPayload, bool]:
        if re.fullmatch(r"NCT\d{8}", nct_id) is None:
            raise SummaryError("invalid NCT identifier")
        if isinstance(self.provider, DisabledSummaryProvider):
            raise SummaryError(
                f"LLM provider is disabled; on-demand generation is unavailable for {nct_id}"
            )
        task = self._in_flight.get(nct_id)
        if task is None:
            task = asyncio.create_task(self._bounded_load_and_summarize(nct_id))
            self._in_flight[nct_id] = task
            task.add_done_callback(
                lambda completed, request_id=nct_id: self._discard_completed(
                    request_id, completed
                )
            )
        return await asyncio.shield(task)

    def _discard_completed(
        self,
        nct_id: str,
        task: asyncio.Task[tuple[PlainLanguageSummaryPayload, bool]],
    ) -> None:
        if self._in_flight.get(nct_id) is task:
            self._in_flight.pop(nct_id, None)

    async def _bounded_load_and_summarize(
        self, nct_id: str
    ) -> tuple[PlainLanguageSummaryPayload, bool]:
        async with self._semaphore:
            return await self._load_and_summarize(nct_id)

    async def _load_and_summarize(
        self, nct_id: str
    ) -> tuple[PlainLanguageSummaryPayload, bool]:
        try:
            study = await self.official_study_loader(nct_id)
        except SummaryError:
            raise
        except Exception as exc:
            raise SummaryError(
                f"official study retrieval failed: {type(exc).__name__}"
            ) from exc
        if study.nct_id != nct_id:
            raise SummaryError("official source returned a different NCT identifier")

        current = self.repository.get_study(nct_id)
        if current is not None and current.source_hash == study.source_hash:
            return await self.summary_service.summarize(study)

        self.external_repository.upsert_studies([study])
        return await self.external_summary_service.summarize(study)
