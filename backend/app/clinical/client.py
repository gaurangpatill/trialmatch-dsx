"""Resilient asynchronous ClinicalTrials.gov API v2 client."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from .models import DetailCacheEnvelope, SourcePage, SourceStudy


DEFAULT_BASE_URL = "https://clinicaltrials.gov/api/v2"
LIST_FIELDS = (
    "NCTId,BriefTitle,OverallStatus,StatusVerifiedDate,LastUpdateSubmitDate,"
    "LastUpdatePostDate,Condition,Phase,StudyType,InterventionName,InterventionType,"
    "MinimumAge,MaximumAge,Sex,BriefSummary,DetailedDescription"
)
_RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class ClinicalTrialsSourceError(RuntimeError):
    """A typed source failure safe to surface in provenance logs."""


class RepeatedPageTokenError(ClinicalTrialsSourceError):
    pass


class InvalidSourcePayloadError(ClinicalTrialsSourceError):
    pass


@dataclass(frozen=True)
class FetchResult:
    records: list[dict[str, Any]]
    page_count: int
    duplicate_identifier_count: int


@dataclass(frozen=True)
class PartialFetchError(ClinicalTrialsSourceError):
    message: str
    records: list[dict[str, Any]]
    page_count: int
    duplicate_identifier_count: int

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class DetailResult:
    record: dict[str, Any]
    retrieved_at: datetime
    from_cache: bool
    detail_hash: str = ""


@dataclass
class RequestMetrics:
    request_count: int = 0
    retry_count: int = 0
    rate_limited_count: int = 0


class ClinicalTrialsClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        max_retry_delay_seconds: float | None = None,
        min_request_interval_seconds: float | None = None,
        cache_dir: str | Path | None = None,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.base_url = (base_url or os.getenv("CLINICALTRIALS_API_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.getenv("CLINICALTRIALS_REQUEST_TIMEOUT_SECONDS", "30"))
        )
        self.max_retries = (
            max_retries
            if max_retries is not None
            else int(os.getenv("CLINICALTRIALS_MAX_RETRIES", "3"))
        )
        self.max_retry_delay_seconds = (
            max_retry_delay_seconds
            if max_retry_delay_seconds is not None
            else float(os.getenv("CLINICALTRIALS_MAX_RETRY_DELAY_SECONDS", "60"))
        )
        self.min_request_interval_seconds = (
            min_request_interval_seconds
            if min_request_interval_seconds is not None
            else float(os.getenv("CLINICALTRIALS_MIN_REQUEST_INTERVAL_SECONDS", "0.2"))
        )
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        if self.max_retry_delay_seconds <= 0:
            raise ValueError("max_retry_delay_seconds must be positive")
        if self.min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds must be nonnegative")
        self.cache_dir = Path(
            cache_dir or os.getenv("CLINICALTRIALS_CACHE_DIR", "backend/data/cache/clinicaltrials")
        )
        self._client = http_client
        self._owns_client = http_client is None
        self._sleep = sleep
        self._monotonic = monotonic
        self._pace_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self.metrics = RequestMetrics()

    async def __aenter__(self) -> ClinicalTrialsClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _pace(self) -> None:
        if self.min_request_interval_seconds == 0:
            return
        async with self._pace_lock:
            now = self._monotonic()
            scheduled = max(now, self._next_request_at)
            delay = scheduled - now
            self._next_request_at = scheduled + self.min_request_interval_seconds
            if delay > 0:
                await self._sleep(delay)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return max(0.0, (parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("ClinicalTrialsClient must be used as an async context manager")
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._pace()
            self.metrics.request_count += 1
            response: httpx.Response | None = None
            try:
                response = await self._client.get(url, params=params, timeout=self.timeout_seconds)
                if response.status_code == 429:
                    self.metrics.rate_limited_count += 1
                if response.status_code in _RETRYABLE_STATUSES:
                    raise httpx.HTTPStatusError(
                        f"transient source status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable = (
                    not isinstance(exc, httpx.HTTPStatusError)
                    or exc.response.status_code in _RETRYABLE_STATUSES
                )
                if not retryable or attempt >= self.max_retries:
                    break
                self.metrics.retry_count += 1
                retry_after = self._retry_after_seconds(response) if response is not None else None
                retry_delay = max(min(2**attempt, 8), retry_after or 0)
                await self._sleep(min(retry_delay, self.max_retry_delay_seconds))
        raise ClinicalTrialsSourceError(f"ClinicalTrials.gov request failed: {last_error}") from last_error

    async def fetch_all_recruiting_massachusetts(self, page_size: int = 1000) -> FetchResult:
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_hashes: dict[str, str] = {}
        seen_tokens: set[str] = set()
        duplicate_count = 0
        page_count = 0
        token: str | None = None
        while True:
            params = self.list_parameters(page_size)
            if token:
                params["pageToken"] = token
            try:
                response = await self._get(f"{self.base_url}/studies", params=params)
                envelope = SourcePage.model_validate(response.json())
            except (ClinicalTrialsSourceError, ValueError, ValidationError) as exc:
                raise PartialFetchError(
                    message=str(exc),
                    records=records,
                    page_count=page_count,
                    duplicate_identifier_count=duplicate_count,
                ) from exc
            page_count += 1
            for record in envelope.studies:
                nct_id = (
                    record.get("protocolSection", {})
                    .get("identificationModule", {})
                    .get("nctId")
                )
                if nct_id:
                    canonical_hash = _canonical_hash(record)
                    if nct_id in seen_ids:
                        duplicate_count += 1
                        if seen_hashes[nct_id] != canonical_hash:
                            raise PartialFetchError(
                                message=f"conflicting duplicate source record for {nct_id}",
                                records=records,
                                page_count=page_count,
                                duplicate_identifier_count=duplicate_count,
                            )
                        continue
                    seen_ids.add(nct_id)
                    seen_hashes[nct_id] = canonical_hash
                records.append(record)
            next_token = envelope.next_page_token
            if not next_token:
                return FetchResult(records, page_count, duplicate_count)
            if next_token in seen_tokens:
                raise PartialFetchError(
                    message=f"ClinicalTrials.gov repeated page token after page {page_count}",
                    records=records,
                    page_count=page_count,
                    duplicate_identifier_count=duplicate_count,
                )
            seen_tokens.add(next_token)
            token = next_token

    def list_parameters(self, page_size: int) -> dict[str, Any]:
        return {
            "query.locn": "Massachusetts",
            "filter.overallStatus": "RECRUITING",
            "pageSize": page_size,
            "fields": LIST_FIELDS,
            "format": "json",
        }

    def list_query_url(self, page_size: int) -> str:
        return str(httpx.URL(f"{self.base_url}/studies", params=self.list_parameters(page_size)))

    @staticmethod
    def _source_last_update(source: SourceStudy) -> str | None:
        posted = source.protocol.status.last_update_post_date_struct
        return posted.date if posted and posted.date else source.protocol.status.last_update_submit_date

    @classmethod
    def _validate_detail(
        cls,
        record: dict[str, Any],
        requested_nct_id: str,
        *,
        expected_overall_status: str | None = None,
        expected_last_update: str | None = None,
    ) -> SourceStudy:
        try:
            parsed = SourceStudy.model_validate(record)
        except ValidationError as exc:
            raise InvalidSourcePayloadError(
                f"invalid detail payload for {requested_nct_id}: {exc}"
            ) from exc
        received_nct_id = parsed.protocol.identification.nct_id
        if received_nct_id != requested_nct_id:
            raise InvalidSourcePayloadError(
                f"detail identifier mismatch: requested {requested_nct_id}, received {received_nct_id}"
            )
        if not parsed.protocol.identification.brief_title or not parsed.protocol.identification.brief_title.strip():
            raise InvalidSourcePayloadError(f"detail {requested_nct_id} is missing required BriefTitle")
        status = parsed.protocol.status.overall_status
        if not status or not status.strip():
            raise InvalidSourcePayloadError(f"detail {requested_nct_id} is missing required OverallStatus")
        if expected_overall_status and status.casefold() != expected_overall_status.casefold():
            raise InvalidSourcePayloadError(
                f"detail {requested_nct_id} status changed from {expected_overall_status} to {status}"
            )
        actual_update = cls._source_last_update(parsed)
        if expected_last_update is not None and actual_update != expected_last_update:
            raise InvalidSourcePayloadError(
                f"detail {requested_nct_id} source update changed from {expected_last_update} to {actual_update}"
            )
        return parsed

    async def fetch_detail(
        self,
        nct_id: str,
        *,
        use_cache: bool = True,
        expected_overall_status: str | None = None,
        expected_last_update: str | None = None,
        expected_index_hash: str | None = None,
    ) -> DetailResult:
        if not nct_id.startswith("NCT") or len(nct_id) != 11 or not nct_id[3:].isdigit():
            raise ValueError(f"invalid NCT identifier: {nct_id}")
        cache_path = self.cache_dir / f"{nct_id}.json"
        if use_cache and cache_path.exists():
            try:
                envelope = DetailCacheEnvelope.model_validate_json(cache_path.read_text())
                if envelope.nct_id != nct_id:
                    raise InvalidSourcePayloadError("cached detail identifier does not match filename")
                if _canonical_hash(envelope.study) != envelope.detail_hash:
                    raise InvalidSourcePayloadError("cached detail content hash does not match")
                if expected_index_hash is not None and envelope.index_record_hash != expected_index_hash:
                    raise InvalidSourcePayloadError("cached detail belongs to a different index record")
                if (
                    expected_overall_status is not None
                    and envelope.expected_overall_status != expected_overall_status
                ):
                    raise InvalidSourcePayloadError("cached detail status identity is stale")
                if (
                    expected_last_update is not None
                    and envelope.expected_last_update != expected_last_update
                ):
                    raise InvalidSourcePayloadError("cached detail update identity is stale")
                self._validate_detail(
                    envelope.study,
                    nct_id,
                    expected_overall_status=expected_overall_status,
                    expected_last_update=expected_last_update,
                )
                return DetailResult(
                    record=envelope.study,
                    retrieved_at=envelope.retrieved_at,
                    from_cache=True,
                    detail_hash=envelope.detail_hash,
                )
            except (ValueError, ValidationError, InvalidSourcePayloadError):
                cache_path.unlink(missing_ok=True)

        response = await self._get(f"{self.base_url}/studies/{nct_id}")
        try:
            record = response.json()
            if not isinstance(record, dict):
                raise TypeError("detail response must be an object")
            parsed = self._validate_detail(
                record,
                nct_id,
                expected_overall_status=expected_overall_status,
                expected_last_update=expected_last_update,
            )
        except (ValueError, TypeError, InvalidSourcePayloadError) as exc:
            raise InvalidSourcePayloadError(f"invalid detail payload for {nct_id}: {exc}") from exc
        retrieved_at = datetime.now(UTC)
        detail_hash = _canonical_hash(record)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        envelope = DetailCacheEnvelope(
            retrieved_at=retrieved_at,
            source_url=f"{self.base_url}/studies/{nct_id}",
            nct_id=nct_id,
            expected_overall_status=expected_overall_status or parsed.protocol.status.overall_status,
            expected_last_update=(
                expected_last_update
                if expected_last_update is not None
                else self._source_last_update(parsed)
            ),
            index_record_hash=expected_index_hash,
            detail_hash=detail_hash,
            study=record,
        )
        _atomic_write(cache_path, envelope.model_dump_json())
        return DetailResult(
            record=record,
            retrieved_at=retrieved_at,
            from_cache=False,
            detail_hash=detail_hash,
        )


def _canonical_hash(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
