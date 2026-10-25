"""Backend-neutral clinical repository protocol and safe URL factory."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from .database import ClinicalRepository
from .models import IngestionReport, StructuredCriterion, Study
from .postgres_database import PostgresClinicalRepository


@runtime_checkable
class ClinicalRepositoryProtocol(Protocol):
    identity: str

    def upsert_studies(self, studies: Iterable[Study]) -> int: ...
    def replace_complete_snapshot(self, studies: Iterable[Study]) -> int: ...
    def publish_complete_snapshot(
        self, studies: Iterable[Study], source_criteria: dict[str, list[StructuredCriterion]]
    ) -> int: ...
    def save_ingestion_report(self, report: IngestionReport) -> None: ...
    def search_studies(
        self,
        *,
        query: str | None = None,
        condition: str | None = None,
        phase: str | None = None,
        age_years: float | None = None,
        location: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Study], int]: ...
    def get_studies(self, nct_ids: list[str] | None = None, limit: int = 20) -> list[Study]: ...
    def get_study(self, nct_id: str) -> Study | None: ...
    def record_detail_failure(self, nct_id: str, attempted_at: datetime, error_code: str) -> None: ...
    def latest_retrieved_at(self) -> datetime | None: ...
    def replace_criteria(
        self, nct_id: str, method: str, criteria: list[StructuredCriterion]
    ) -> None: ...
    def get_criteria(
        self, nct_id: str, *, source_hash: str | None = None
    ) -> list[StructuredCriterion]: ...
    def existing_study_ids(self, nct_ids: list[str]) -> set[str]: ...
    def get_cached_extraction(self, cache_key: str) -> dict[str, Any] | None: ...
    def publish_extraction(self, **kwargs: Any) -> None: ...
    def delete_extraction_cache(self, cache_key: str) -> None: ...
    def record_extraction_attempt(self, **kwargs: Any) -> None: ...
    def approve_extraction_coverage(
        self, cache_key: str, *, verification_method: str, verified_at: datetime
    ) -> None: ...
    def get_extraction_state(
        self, nct_id: str, source_hash: str
    ) -> tuple[str, dict[str, Any] | None, bool, str | None]: ...
    def get_cached_summary(self, cache_key: str) -> dict[str, Any] | None: ...
    def get_current_summary(self, nct_id: str, source_hash: str) -> str | None: ...
    def get_search_enrichment(
        self, studies: list[Study]
    ) -> tuple[
        dict[str, str | None],
        dict[str, tuple[str, dict[str, Any] | None, bool, str | None]],
    ]: ...
    def publish_summary(self, **kwargs: Any) -> None: ...
    def schema_ready(self) -> bool: ...
    def data_ready(self) -> bool: ...
    def record_counts(self) -> dict[str, int]: ...
    def storage_stats(self) -> dict[str, Any]: ...


def create_repository(database_url: str) -> ClinicalRepositoryProtocol:
    """Create a repository from an allowlisted URL without exposing the URL."""

    if not isinstance(database_url, str) or not database_url:
        raise RuntimeError("Clinical database URL cannot be empty")
    parsed = urlsplit(database_url)
    scheme = parsed.scheme.casefold()
    if scheme == "sqlite":
        if parsed.netloc or parsed.query or parsed.fragment or not database_url.startswith("sqlite:///"):
            raise RuntimeError("Invalid SQLite database URL")
        value = database_url.removeprefix("sqlite:///")
        if not value:
            raise RuntimeError("SQLite database path cannot be empty")
        repository = ClinicalRepository(Path(value), initialize_schema=False)
        if not repository.schema_ready():
            raise RuntimeError(
                "SQLite clinical schema is unavailable; initialize it explicitly first"
            )
        return repository
    if scheme in {"postgresql", "postgres", "postgresql+psycopg"}:
        if parsed.fragment or not parsed.path or parsed.path == "/":
            raise RuntimeError("Invalid PostgreSQL database URL")
        if scheme == "postgresql+psycopg":
            parsed = parsed._replace(scheme="postgresql")
            database_url = urlunsplit(parsed)
        return PostgresClinicalRepository(database_url)
    raise RuntimeError("Unsupported clinical database URL scheme")
