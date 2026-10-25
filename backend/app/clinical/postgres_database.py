"""PostgreSQL 17 implementation of the clinical repository contract."""

from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

from .database import ClinicalRepository, _age_to_years
from .migrations import LATEST_POSTGRES_SCHEMA_VERSION, migrate_postgres
from .models import Study
from .normalize import normalize_phase


class _RepositoryIdentity(str):
    """Path-compatible opaque identity for existing process-local lock callers."""

    def resolve(self) -> _RepositoryIdentity:
        return self


class _PostgresConnection:
    """Translate qmark SQL and serialize repository write transactions."""

    _WRITE_LOCK_ID = 2_024_102_405

    def __init__(self, database_url: str):
        self._connection = psycopg.connect(database_url, row_factory=dict_row)
        self._write_locked = False

    @staticmethod
    def _sql(statement: str) -> str:
        return statement.replace("?", "%s")

    def _lock_writes(self) -> None:
        if not self._write_locked:
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(%s)", (self._WRITE_LOCK_ID,)
            )
            self._write_locked = True

    def execute(self, statement: str, parameters: Any = None):
        normalized = statement.strip().upper()
        if normalized == "BEGIN IMMEDIATE":
            cursor = self._connection.execute("BEGIN")
            self._lock_writes()
            return cursor
        if normalized.startswith(("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ")) or normalized.startswith(
            "SELECT NCT_ID, SOURCE_HASH, DETAIL_STATUS FROM STUDIES"
        ):
            self._lock_writes()
        return self._connection.execute(self._sql(statement), parameters)

    def executemany(self, statement: str, parameters: Any) -> None:
        self._lock_writes()
        with self._connection.cursor() as cursor:
            cursor.executemany(self._sql(statement), parameters)

    def commit(self) -> None:
        self._connection.commit()
        self._write_locked = False

    def close(self) -> None:
        self._connection.close()


class PostgresClinicalRepository(ClinicalRepository):
    """Connection-per-operation PostgreSQL repository with explicit migrations."""

    def __init__(self, database_url: str):
        self._database_url = database_url
        self.identity = _postgres_identity(database_url)
        # Preserve compatibility with the existing router's process-local identity use
        # without exposing a PostgreSQL URL or credentials.
        self.path = _RepositoryIdentity(self.identity)
        self._require_current_schema()

    def _connect(self) -> _PostgresConnection:
        return _PostgresConnection(self._database_url)

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
    ) -> tuple[list[Study], int]:
        """Filter lightweight columns before loading full source JSON for one page."""

        columns = ["nct_id", "detail_status"]
        if query:
            columns.extend(["title", "conditions_json"])
        elif condition:
            columns.append("conditions_json")
        if phase:
            columns.append("phases_json")
        if age_years is not None:
            columns.extend(["minimum_age", "maximum_age"])
        if location:
            columns.append("locations_json")
        selected_columns = ", ".join(dict.fromkeys(columns))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {selected_columns} FROM studies "
                "ORDER BY CASE WHEN detail_status = 'retrieved' THEN 0 ELSE 1 END, nct_id"
            ).fetchall()

        filtered_ids: list[str] = []
        query_needle = query.casefold() if query else None
        condition_needle = condition.strip().casefold() if condition else None
        phase_needle = normalize_phase(phase).casefold() if phase else None
        location_needle = location.strip().casefold() if location else None
        for row in rows:
            conditions = json.loads(row["conditions_json"]) if query or condition else []
            if query_needle and not (
                query_needle in row["title"].casefold()
                or any(query_needle in item.casefold() for item in conditions)
            ):
                continue
            if condition_needle and not any(
                item.casefold() == condition_needle for item in conditions
            ):
                continue
            if phase_needle and not any(
                normalize_phase(item).casefold() == phase_needle
                for item in json.loads(row["phases_json"])
            ):
                continue
            if age_years is not None:
                minimum = _age_to_years(row["minimum_age"])
                maximum = _age_to_years(row["maximum_age"])
                if minimum is None and maximum is None:
                    continue
                if minimum is not None and age_years < minimum:
                    continue
                if maximum is not None and age_years > maximum:
                    continue
            if location_needle and not any(
                location_needle in (value or "").casefold()
                for item in json.loads(row["locations_json"])
                for value in (
                    item.get("facility"),
                    item.get("city"),
                    item.get("state"),
                    item.get("country"),
                )
            ):
                continue
            filtered_ids.append(row["nct_id"])

        total = len(filtered_ids)
        start = (page - 1) * page_size
        page_ids = filtered_ids[start : start + page_size]
        studies_by_id = {
            study.nct_id: study for study in self.get_studies(page_ids, limit=page_size)
        }
        return [studies_by_id[nct_id] for nct_id in page_ids], total

    def initialize(self) -> None:
        """Explicitly migrate this database; construction never invokes this method."""

        migrate_postgres(self._database_url)
        self._require_current_schema()

    def _require_current_schema(self) -> None:
        try:
            with psycopg.connect(self._database_url) as connection:
                row = connection.execute(
                    """
                    SELECT 1 FROM clinical_schema_migrations
                    WHERE version = %s
                    """,
                    (LATEST_POSTGRES_SCHEMA_VERSION,),
                ).fetchone()
        except psycopg.Error as exc:
            raise RuntimeError(
                "PostgreSQL clinical schema is unavailable; run explicit migrations first"
            ) from exc
        if row is None:
            raise RuntimeError(
                "PostgreSQL clinical schema is out of date; run explicit migrations first"
            )

    def schema_ready(self) -> bool:
        try:
            self._require_current_schema()
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS value
                    FROM information_schema.tables
                    WHERE table_schema = current_schema()
                      AND table_name IN (
                        'studies', 'ingestion_runs', 'criteria',
                        'extraction_cache', 'extraction_attempts', 'summary_cache'
                      )
                    """
                ).fetchone()
            return bool(row and row["value"] == 6)
        except (RuntimeError, psycopg.Error):
            return False

    def data_ready(self) -> bool:
        try:
            return self.record_counts()["studies"] > 0
        except psycopg.Error:
            return False

    def storage_stats(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            storage = connection.execute(
                "SELECT pg_database_size(current_database()) AS value"
            ).fetchone()
            latest = connection.execute(
                """
                SELECT status, pagination_completed, completed_at
                FROM ingestion_runs ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        counts = self.record_counts()
        return {
            "backend": "postgresql",
            "schema_ready": self.schema_ready(),
            "data_ready": counts["studies"] > 0,
            "counts": counts,
            "storage_bytes": int(storage["value"]),
            "latest_ingestion": dict(latest) if latest else None,
        }


def _postgres_identity(database_url: str) -> _RepositoryIdentity:
    """Build a stable, non-secret identity without retaining credentials in output."""

    parsed = urlsplit(database_url)
    try:
        endpoint = f"{parsed.hostname or 'local'}:{parsed.port or 5432}{parsed.path or '/'}"
    except ValueError as exc:
        raise RuntimeError("Invalid PostgreSQL database URL") from exc
    digest = sha256(endpoint.encode("utf-8")).hexdigest()[:20]
    return _RepositoryIdentity(f"postgresql:{digest}")
