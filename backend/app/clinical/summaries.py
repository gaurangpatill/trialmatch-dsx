"""Direct plain-language study explanations with best-effort current-source caching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .client import ClinicalTrialsClient
from .models import DetailRetrievalStatus, PlainLanguageSummaryPayload, Study
from .normalize import normalize_study

SCHEMA_VERSION = "4.0"
PROMPT_VERSION = "4.0"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
logger = logging.getLogger(__name__)
_RETRYABLE_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
_SECTION_ORDER = (
    ("investigating", "What is this study investigating?"),
    ("participation", "What might participation involve?"),
    ("intended_for", "Who is the study intended for?"),
    ("important", "Important things to know."),
)
_MISSING_SECTION_TEXT = {
    "investigating": "The official record does not provide enough information to explain the study question.",
    "participation": "The official record does not clearly describe what participation may involve.",
    "intended_for": "The official record does not provide enough information to describe the intended participants.",
    "important": "The official record does not provide additional details for this section.",
}
_SOURCE_MODULES = (
    "identificationModule",
    "statusModule",
    "descriptionModule",
    "conditionsModule",
    "designModule",
    "armsInterventionsModule",
    "outcomesModule",
    "eligibilityModule",
)


class SummaryError(RuntimeError):
    """Safe summary-generation failure without provider secrets or response bodies."""


def _development_log(nct_id: str, stage: str, started: float, status: str, category: str) -> None:
    """Emit concise local pipeline diagnostics without source content or credentials."""

    if os.getenv("TRIALMATCH_ENV", "development").casefold() not in {"development", "dev", "local"}:
        return
    logger.warning(
        "clinical_summary nct_id=%s stage=%s elapsed_ms=%s http_status=%s category=%s",
        nct_id,
        stage,
        round((time.perf_counter() - started) * 1000),
        status,
        category,
    )


class GeneratedExplanation(BaseModel):
    """Minimal structured model response; no model-authored evidence metadata."""

    model_config = ConfigDict(extra="forbid", strict=True)

    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    investigating: str = Field(min_length=1)
    participation: str = Field(min_length=1)
    intended_for: str = Field(min_length=1)
    important: str = Field(min_length=1)


class SummaryRepository(Protocol):
    def get_study(self, nct_id: str) -> Study | None: ...
    def upsert_studies(self, studies: list[Study]) -> int: ...
    def get_cached_summary(self, cache_key: str) -> dict[str, Any] | None: ...
    def publish_summary(self, **kwargs: Any) -> None: ...


class SummaryProvider(Protocol):
    name: str
    model: str

    async def summarize(self, study: Study) -> str: ...


class DisabledSummaryProvider:
    name = "disabled"
    model = "disabled"

    async def summarize(self, study: Study) -> str:
        raise SummaryError(
            f"LLM provider is disabled; no cached summary is available for {study.nct_id}"
        )


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def _official_study_information(study: Study) -> dict[str, Any]:
    """Select actual official modules needed for a participant explanation."""

    protocol = study.raw_source.get("protocolSection")
    if not isinstance(protocol, dict):
        return {}
    return {
        module: protocol[module]
        for module in _SOURCE_MODULES
        if module in protocol and isinstance(protocol[module], dict)
    }


def _explanation_schema() -> dict[str, Any]:
    section = {"type": "string", "minLength": 1}
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


def _render_sections(sections: dict[str, str]) -> str:
    return "\n\n".join(
        f"{heading} {_normalized_text(sections.get(name, '')) or _MISSING_SECTION_TEXT[name]}"
        for name, heading in _SECTION_ORDER
    )


def _parse_generated_content(study: Study, content: str) -> str:
    """Normalize strict JSON when available and retain readable plain text otherwise."""

    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    if not cleaned:
        raise SummaryError("OpenAI returned an empty explanation")

    try:
        raw = json.loads(cleaned)
    except json.JSONDecodeError:
        return cleaned
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if not isinstance(raw, dict):
        raise SummaryError("OpenAI returned an unusable explanation")

    returned_nct_id = raw.get("nct_id")
    returned_source_hash = raw.get("source_hash")
    if returned_nct_id is not None and returned_nct_id != study.nct_id:
        raise SummaryError("OpenAI explanation NCT identifier mismatch")
    if returned_source_hash is not None and returned_source_hash != study.source_hash:
        raise SummaryError("OpenAI explanation source hash mismatch")

    try:
        structured = GeneratedExplanation.model_validate(raw, strict=True)
    except ValidationError:
        direct = raw.get("plain_language_summary") or raw.get("explanation") or raw.get("summary")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        aliases = {
            "investigating": ("investigating", "what_is_this_study_investigating"),
            "participation": ("participation", "what_might_participation_involve"),
            "intended_for": ("intended_for", "who_is_the_study_intended_for"),
            "important": ("important", "important_things_to_know"),
        }
        sections: dict[str, str] = {}
        for name, keys in aliases.items():
            value = next((raw.get(key) for key in keys if isinstance(raw.get(key), str)), "")
            sections[name] = value.strip() if isinstance(value, str) else ""
        if any(sections.values()):
            return _render_sections(sections)
        raise SummaryError("OpenAI returned an unusable explanation")

    if structured.nct_id != study.nct_id:
        raise SummaryError("OpenAI explanation NCT identifier mismatch")
    if structured.source_hash != study.source_hash:
        raise SummaryError("OpenAI explanation source hash mismatch")
    return _render_sections(structured.model_dump())


class OpenAISummaryProvider:
    """One-request OpenAI adapter with simple usable-output normalization."""

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
        if timeout_seconds <= 0 or max_retries < 0 or max_retry_delay_seconds <= 0 or max_http_requests < 1:
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

    async def summarize(self, study: Study) -> str:
        official_information = _official_study_information(study)
        if not official_information:
            raise SummaryError(f"{study.nct_id} has no official information suitable for explanation")
        body = {
            "model": self.model,
            "temperature": 0,
            "max_completion_tokens": 1200,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "trialmatch_study_explanation",
                    "strict": True,
                    "schema": _explanation_schema(),
                },
            },
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Explain this ClinicalTrials.gov study for a general audience using only the supplied "
                        "official study information. Return the NCT ID and source hash unchanged plus four concise "
                        "sections: what the study investigates, what participation may involve, who it is intended "
                        "for, and important things to know. Do not decide whether a reader is eligible, give medical "
                        "advice, promise benefit, or add facts absent from the record. If information for a section "
                        "is missing, say that clearly."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "nct_id": study.nct_id,
                            "source_hash": study.source_hash,
                            "official_study_information": official_information,
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
                        raise SummaryError(f"OpenAI explanation request failed with status {response.status_code}")
                except httpx.HTTPError as exc:
                    if attempt >= self.max_retries:
                        raise SummaryError(
                            f"OpenAI explanation request failed after {attempt + 1} attempt(s): {type(exc).__name__}"
                        ) from exc
                await self._sleep(min(2**attempt, self.max_retry_delay_seconds))
            if response is None or response.status_code >= 400:
                raise SummaryError("OpenAI explanation request failed")
            envelope = response.json()
            usage = envelope.get("usage", {})
            if isinstance(usage, dict):
                self.last_usage = {
                    key: value for key, value in usage.items()
                    if isinstance(key, str) and isinstance(value, int)
                }
            choice = envelope["choices"][0]
            if choice.get("finish_reason") not in {None, "stop"}:
                raise SummaryError("OpenAI explanation response was incomplete")
            message = choice["message"]
            if message.get("refusal"):
                raise SummaryError("OpenAI refused the explanation request")
            content = message["content"]
            if not isinstance(content, str):
                raise TypeError("provider content must be text")
            return _parse_generated_content(study, content)
        except SummaryError:
            raise
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise SummaryError(f"OpenAI explanation response was unusable: {type(exc).__name__}") from exc
        finally:
            if owns_client:
                await client.aclose()


def summary_provider_from_environment() -> SummaryProvider:
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
            base_url=os.getenv("LLM_BASE_URL", DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            max_http_requests=max_http_requests,
        )
    raise SummaryError(f"unsupported LLM_PROVIDER: {provider}")


class _GenerationBudget:
    def __init__(self, remaining: int):
        self.remaining = remaining
        self.lock = asyncio.Lock()

    async def claim(self) -> None:
        async with self.lock:
            if self.remaining <= 0:
                raise SummaryError("summary generation request limit reached")
            self.remaining -= 1


class StudySummaryService:
    """Cache, coalesce, and bound direct explanation generation for one study record."""

    def __init__(
        self,
        repository: SummaryRepository,
        provider: SummaryProvider,
        *,
        max_concurrency: int = 2,
        max_generation_requests: int = 25,
        generation_budget: _GenerationBudget | None = None,
    ):
        if max_concurrency < 1 or max_generation_requests < 1:
            raise ValueError("summary generation limits must be positive")
        self.repository = repository
        self.provider = provider
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._generation_budget = generation_budget or _GenerationBudget(max_generation_requests)
        self._in_flight: dict[str, asyncio.Task[tuple[PlainLanguageSummaryPayload, bool]]] = {}

    def cache_key(self, study: Study) -> str:
        material = ":".join(
            (study.nct_id, study.source_hash, SCHEMA_VERSION, PROMPT_VERSION, self.provider.name, self.provider.model)
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def cached(self, study: Study) -> PlainLanguageSummaryPayload | None:
        try:
            raw = self.repository.get_cached_summary(self.cache_key(study))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        try:
            payload = PlainLanguageSummaryPayload.model_validate(raw, strict=True)
        except ValidationError:
            return None
        if payload.nct_id != study.nct_id or payload.source_hash != study.source_hash:
            return None
        return payload

    async def summarize(self, study: Study) -> tuple[PlainLanguageSummaryPayload, bool]:
        key = self.cache_key(study)
        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.create_task(self._summarize_once(study, key))
            self._in_flight[key] = task
            task.add_done_callback(lambda completed, request_key=key: self._discard(request_key, completed))
        return await asyncio.shield(task)

    def _discard(self, key: str, task: asyncio.Task[tuple[PlainLanguageSummaryPayload, bool]]) -> None:
        if self._in_flight.get(key) is task:
            self._in_flight.pop(key, None)

    async def _summarize_once(self, study: Study, key: str) -> tuple[PlainLanguageSummaryPayload, bool]:
        started = time.perf_counter()
        cached = self.cached(study)
        if cached is not None:
            _development_log(study.nct_id, "summary_cache_read", started, "200", "cache_hit")
            return cached, True
        _development_log(study.nct_id, "summary_cache_read", started, "miss", "cache_miss")
        await self._generation_budget.claim()
        provider_started = time.perf_counter()
        async with self._semaphore:
            explanation = await self.provider.summarize(study)
        _development_log(study.nct_id, "openai_complete", provider_started, "200", "usable_output")
        if not isinstance(explanation, str) or not explanation.strip():
            raise SummaryError("OpenAI returned an empty explanation")
        payload = PlainLanguageSummaryPayload(
            nct_id=study.nct_id,
            source_hash=study.source_hash,
            plain_language_summary=explanation.strip(),
        )
        try:
            self.repository.publish_summary(
                cache_key=key,
                nct_id=study.nct_id,
                source_hash=study.source_hash,
                provider=self.provider.name,
                model=self.provider.model,
                schema_version=SCHEMA_VERSION,
                prompt_version=PROMPT_VERSION,
                payload=payload.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
            _development_log(study.nct_id, "summary_cache_write", started, "200", "stored")
        except Exception as exc:
            _development_log(
                study.nct_id,
                "summary_cache_write",
                started,
                "ignored",
                f"{type(exc).__name__}_nonblocking",
            )
            # Cache availability is secondary; a usable generated explanation must be returned.
        return payload, False


class _MemorySummaryRepository:
    """Isolated cache for official studies outside the imported discovery snapshot."""

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        self.studies: dict[str, Study] = {}
        self.summaries: dict[str, dict[str, Any]] = {}
        self.cache_dir = Path(cache_dir) if cache_dir else None

    def get_study(self, nct_id: str) -> Study | None:
        return self.studies.get(nct_id)

    def upsert_studies(self, studies: list[Study]) -> int:
        for study in studies:
            self.studies[study.nct_id] = study
        return len(studies)

    def get_cached_summary(self, cache_key: str) -> dict[str, Any] | None:
        cached = self.summaries.get(cache_key)
        if cached is not None or self.cache_dir is None:
            return cached
        path = self.cache_dir / f"{cache_key}.json"
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        self.summaries[cache_key] = payload
        return payload

    def publish_summary(self, **kwargs: Any) -> None:
        cache_key = str(kwargs["cache_key"])
        payload = dict(kwargs["payload"])
        self.summaries[cache_key] = payload
        if self.cache_dir is None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_dir / f".{cache_key}.{os.getpid()}.tmp"
        try:
            temporary.write_text(json.dumps(payload, sort_keys=True))
            temporary.replace(self.cache_dir / f"{cache_key}.json")
        finally:
            temporary.unlink(missing_ok=True)


async def load_official_study_for_summary(nct_id: str) -> Study:
    """Fetch the selected official ClinicalTrials.gov API v2 record directly."""

    cache_dir = os.getenv("SUMMARY_SOURCE_CACHE_DIR", "backend/data/cache/summary-source")
    async with ClinicalTrialsClient(cache_dir=cache_dir) as client:
        detail = await client.fetch_detail(nct_id, use_cache=False)
    return normalize_study(
        detail.record,
        detail.retrieved_at,
        detail_status=DetailRetrievalStatus.RETRIEVED,
    )


class OnDemandStudySummaryService:
    """Fetch one selected official study, generate once, and cache when possible."""

    def __init__(
        self,
        repository: SummaryRepository,
        provider: SummaryProvider,
        official_study_loader: Callable[[str], Awaitable[Study]] | None = None,
        *,
        max_concurrency: int = 2,
        max_generation_requests: int = 25,
        external_cache_dir: str | Path | None = None,
    ):
        self.repository = repository
        self.provider = provider
        self.official_study_loader = official_study_loader or load_official_study_for_summary
        budget = _GenerationBudget(max_generation_requests)
        self.summary_service = StudySummaryService(
            repository,
            provider,
            max_concurrency=max_concurrency,
            max_generation_requests=max_generation_requests,
            generation_budget=budget,
        )
        self.external_repository = _MemorySummaryRepository(
            external_cache_dir
            if external_cache_dir is not None
            else os.getenv("SUMMARY_EXTERNAL_CACHE_DIR", "backend/data/cache/summaries")
        )
        self.external_summary_service = StudySummaryService(
            self.external_repository,
            provider,
            max_concurrency=max_concurrency,
            max_generation_requests=max_generation_requests,
            generation_budget=budget,
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._in_flight: dict[str, asyncio.Task[tuple[PlainLanguageSummaryPayload, bool]]] = {}

    async def summarize_nct_id(self, nct_id: str) -> tuple[PlainLanguageSummaryPayload, bool]:
        if re.fullmatch(r"NCT\d{8}", nct_id) is None:
            raise SummaryError("invalid NCT identifier")

        started = time.perf_counter()
        try:
            current = self.repository.get_study(nct_id)
        except Exception as exc:
            current = None
            _development_log(nct_id, "summary_cache_read", started, "ignored", f"{type(exc).__name__}_nonblocking")
        if current is not None:
            cached = self.summary_service.cached(current)
            if cached is not None:
                _development_log(nct_id, "summary_cache_read", started, "200", "cache_hit")
                return cached, True
        if isinstance(self.provider, DisabledSummaryProvider):
            raise SummaryError(f"LLM provider is disabled; on-demand generation is unavailable for {nct_id}")

        task = self._in_flight.get(nct_id)
        if task is None:
            task = asyncio.create_task(self._bounded_load_and_summarize(nct_id))
            self._in_flight[nct_id] = task
            task.add_done_callback(lambda completed, request_id=nct_id: self._discard(request_id, completed))
        return await asyncio.shield(task)

    def _discard(self, nct_id: str, task: asyncio.Task[tuple[PlainLanguageSummaryPayload, bool]]) -> None:
        if self._in_flight.get(nct_id) is task:
            self._in_flight.pop(nct_id, None)

    async def _bounded_load_and_summarize(self, nct_id: str) -> tuple[PlainLanguageSummaryPayload, bool]:
        async with self._semaphore:
            source_started = time.perf_counter()
            try:
                study = await self.official_study_loader(nct_id)
            except SummaryError:
                raise
            except Exception as exc:
                _development_log(
                    nct_id,
                    "official_study_fetch",
                    source_started,
                    "failed",
                    type(exc).__name__,
                )
                raise SummaryError(f"official study retrieval failed: {type(exc).__name__}") from exc
            if study.nct_id != nct_id:
                raise SummaryError("official source returned a different NCT identifier")
            _development_log(nct_id, "official_study_fetch", source_started, "200", "retrieved")

            try:
                current = self.repository.get_study(nct_id)
            except Exception as exc:
                current = None
                _development_log(
                    nct_id,
                    "summary_cache_read",
                    source_started,
                    "ignored",
                    f"{type(exc).__name__}_nonblocking",
                )
            if current is not None and current.source_hash == study.source_hash:
                return await self.summary_service.summarize(study)

            # Keep arbitrary official records outside the imported discovery snapshot while
            # persisting their summaries in an isolated cache when local storage is writable.
            self.external_repository.upsert_studies([study])
            return await self.external_summary_service.summarize(study)


async def generate_trial_explanation(
    nct_id: str,
    repository: SummaryRepository,
    provider: SummaryProvider,
) -> tuple[PlainLanguageSummaryPayload, bool]:
    """Simple public service function used by direct integrations and tests."""

    return await OnDemandStudySummaryService(repository, provider).summarize_nct_id(nct_id)
