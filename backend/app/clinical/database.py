"""SQLite persistence and backend-neutral clinical repository contracts."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import DetailRetrievalStatus, IngestionReport, StructuredCriterion, Study
from .normalize import normalize_phase


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS studies (
    nct_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    overall_status TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    phases_json TEXT NOT NULL,
    source_description TEXT,
    brief_summary TEXT,
    detailed_description TEXT,
    eligibility_criteria TEXT,
    minimum_age TEXT,
    maximum_age TEXT,
    sex TEXT,
    locations_json TEXT NOT NULL,
    contacts_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    detail_status TEXT NOT NULL DEFAULT 'not_retrieved',
    detail_attempted_at TEXT,
    detail_error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_studies_status_nct ON studies(overall_status, nct_id);
CREATE INDEX IF NOT EXISTS idx_studies_title ON studies(title);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    effective_page_size INTEGER NOT NULL DEFAULT 20,
    pagination_completed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    index_retrieved_at TEXT,
    published_at TEXT,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    raw_record_count INTEGER NOT NULL,
    accepted_record_count INTEGER NOT NULL,
    duplicate_identifier_count INTEGER NOT NULL,
    missing_value_counts_json TEXT NOT NULL,
    excluded_records_json TEXT NOT NULL,
    errors_json TEXT NOT NULL,
    detail_requested_count INTEGER NOT NULL,
    detail_retrieved_count INTEGER NOT NULL,
    index_retrieved_count INTEGER NOT NULL DEFAULT 0,
    detail_cache_hit_count INTEGER NOT NULL DEFAULT 0,
    detail_network_count INTEGER NOT NULL DEFAULT 0,
    detail_resumed_count INTEGER NOT NULL DEFAULT 0,
    detail_failed_count INTEGER NOT NULL DEFAULT 0,
    incomplete_detail_ids_json TEXT NOT NULL DEFAULT '[]',
    ma_verified_count INTEGER NOT NULL DEFAULT 0,
    ma_unverified_count INTEGER NOT NULL DEFAULT 0,
    ma_unverified_records_json TEXT NOT NULL DEFAULT '[]',
    request_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    rate_limited_count INTEGER NOT NULL DEFAULT 0,
    cohort_fingerprint TEXT,
    checkpoint_resumed INTEGER NOT NULL DEFAULT 0,
    publication_attempted INTEGER NOT NULL DEFAULT 0,
    publication_completed INTEGER NOT NULL DEFAULT 0,
    published_record_count INTEGER NOT NULL DEFAULT 0,
    previous_snapshot_preserved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS criteria (
    criterion_id TEXT PRIMARY KEY,
    nct_id TEXT NOT NULL REFERENCES studies(nct_id) ON DELETE CASCADE,
    extraction_method TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_criteria_nct ON criteria(nct_id, extraction_method);

CREATE TABLE IF NOT EXISTS extraction_cache (
    cache_key TEXT PRIMARY KEY,
    nct_id TEXT NOT NULL REFERENCES studies(nct_id) ON DELETE CASCADE,
    source_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    coverage_verified INTEGER NOT NULL DEFAULT 0,
    coverage_verification_method TEXT,
    coverage_verified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_extraction_cache_nct ON extraction_cache(nct_id);

CREATE TABLE IF NOT EXISTS extraction_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nct_id TEXT NOT NULL REFERENCES studies(nct_id) ON DELETE CASCADE,
    source_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extraction_attempts_current
ON extraction_attempts(nct_id, source_hash, created_at);

CREATE TABLE IF NOT EXISTS summary_cache (
    cache_key TEXT PRIMARY KEY,
    nct_id TEXT NOT NULL REFERENCES studies(nct_id) ON DELETE CASCADE,
    source_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_cache_current
ON summary_cache(nct_id, source_hash, created_at);
"""


class ClinicalRepository:
    """Small connection-per-operation repository suitable for one-process SQLite use."""

    def __init__(self, database_path: str | Path, *, initialize_schema: bool = True):
        self.path = Path(database_path)
        self.identity = f"sqlite:{self.path.resolve()}"
        if initialize_schema:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.initialize()
        elif not self.path.is_file():
            raise RuntimeError(
                "SQLite clinical database is unavailable; initialize it explicitly first"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            study_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(studies)")
            }
            for column in ("source_description", "brief_summary", "detailed_description"):
                if column not in study_columns:
                    connection.execute(f"ALTER TABLE studies ADD COLUMN {column} TEXT")
            # Derived description columns can be repaired losslessly from the preserved
            # official source JSON. Run this on every startup so databases created by
            # older releases do not remain permanently blank merely because the column
            # itself already existed.
            for row in connection.execute(
                """
                SELECT nct_id, raw_json, source_description,
                       brief_summary, detailed_description
                FROM studies
                WHERE source_description IS NULL
                   OR brief_summary IS NULL
                   OR detailed_description IS NULL
                """
            ):
                try:
                    description = json.loads(row["raw_json"]).get("protocolSection", {}).get(
                        "descriptionModule", {}
                    )
                    brief = _clean_source_text(description.get("briefSummary"))
                    detailed = _clean_source_text(description.get("detailedDescription"))
                except (json.JSONDecodeError, AttributeError):
                    brief = None
                    detailed = None
                source_description = _clean_source_text(row["source_description"]) or brief or detailed
                connection.execute(
                    """
                    UPDATE studies
                    SET source_description = ?, brief_summary = ?, detailed_description = ?
                    WHERE nct_id = ?
                    """,
                    (source_description, brief, detailed, row["nct_id"]),
                )
            if "detail_status" not in study_columns:
                connection.execute(
                    "ALTER TABLE studies ADD COLUMN detail_status TEXT NOT NULL DEFAULT 'not_retrieved'"
                )
            if "detail_attempted_at" not in study_columns:
                connection.execute("ALTER TABLE studies ADD COLUMN detail_attempted_at TEXT")
            if "detail_error_code" not in study_columns:
                connection.execute("ALTER TABLE studies ADD COLUMN detail_error_code TEXT")
            # Preserve previously expanded rows when migrating an existing database. The
            # indexed list payload contains none of these detail-only source modules.
            connection.execute(
                """
                UPDATE studies
                SET detail_status = 'retrieved', detail_attempted_at = retrieved_at
                WHERE detail_status = 'not_retrieved'
                  AND raw_json LIKE '%\"contactsLocationsModule\"%'
                  AND (raw_json LIKE '%\"eligibilityModule\"%'
                       OR raw_json LIKE '%\"descriptionModule\"%')
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(ingestion_runs)")
            }
            ingestion_columns = {
                "effective_page_size": "INTEGER NOT NULL DEFAULT 20",
                "pagination_completed": "INTEGER NOT NULL DEFAULT 0",
                "index_retrieved_at": "TEXT",
                "published_at": "TEXT",
                "index_retrieved_count": "INTEGER NOT NULL DEFAULT 0",
                "detail_cache_hit_count": "INTEGER NOT NULL DEFAULT 0",
                "detail_network_count": "INTEGER NOT NULL DEFAULT 0",
                "detail_resumed_count": "INTEGER NOT NULL DEFAULT 0",
                "detail_failed_count": "INTEGER NOT NULL DEFAULT 0",
                "incomplete_detail_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "ma_verified_count": "INTEGER NOT NULL DEFAULT 0",
                "ma_unverified_count": "INTEGER NOT NULL DEFAULT 0",
                "ma_unverified_records_json": "TEXT NOT NULL DEFAULT '[]'",
                "request_count": "INTEGER NOT NULL DEFAULT 0",
                "retry_count": "INTEGER NOT NULL DEFAULT 0",
                "rate_limited_count": "INTEGER NOT NULL DEFAULT 0",
                "cohort_fingerprint": "TEXT",
                "checkpoint_resumed": "INTEGER NOT NULL DEFAULT 0",
                "publication_attempted": "INTEGER NOT NULL DEFAULT 0",
                "publication_completed": "INTEGER NOT NULL DEFAULT 0",
                "published_record_count": "INTEGER NOT NULL DEFAULT 0",
                "previous_snapshot_preserved": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, definition in ingestion_columns.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE ingestion_runs ADD COLUMN {column} {definition}"
                    )
            cache_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(extraction_cache)")
            }
            if "coverage_verified" not in cache_columns:
                connection.execute(
                    "ALTER TABLE extraction_cache ADD COLUMN coverage_verified INTEGER NOT NULL DEFAULT 0"
                )
            if "coverage_verification_method" not in cache_columns:
                connection.execute(
                    "ALTER TABLE extraction_cache ADD COLUMN coverage_verification_method TEXT"
                )
            if "coverage_verified_at" not in cache_columns:
                connection.execute(
                    "ALTER TABLE extraction_cache ADD COLUMN coverage_verified_at TEXT"
                )
            connection.commit()

    @staticmethod
    def _study_payloads(studies: Iterable[Study]) -> list[tuple[Any, ...]]:
        return [
            (
                study.nct_id,
                study.title,
                study.overall_status,
                json.dumps(study.conditions),
                json.dumps(study.phases),
                study.source_description,
                study.brief_summary,
                study.detailed_description,
                study.eligibility_criteria,
                study.minimum_age,
                study.maximum_age,
                study.sex,
                json.dumps([item.model_dump(mode="json") for item in study.locations]),
                json.dumps([item.model_dump(mode="json") for item in study.contacts]),
                study.source_url,
                study.retrieved_at.isoformat(),
                study.source_hash,
                json.dumps(study.raw_source, sort_keys=True),
                study.detail_status.value,
                study.detail_attempted_at.isoformat() if study.detail_attempted_at else None,
                study.detail_error_code,
            )
            for study in studies
        ]

    @staticmethod
    def _invalidate_changed_sources(
        connection: sqlite3.Connection, payloads: list[tuple[Any, ...]]
    ) -> None:
        """Delete derived outputs only when an accepted source version changes."""

        if not payloads:
            return
        identifiers = [str(payload[0]) for payload in payloads]
        placeholders = ",".join("?" for _ in identifiers)
        existing = {
            row["nct_id"]: row
            for row in connection.execute(
                f"SELECT nct_id, source_hash, detail_status FROM studies "
                f"WHERE nct_id IN ({placeholders})",
                identifiers,
            ).fetchall()
        }
        changed_ids = [
            str(payload[0])
            for payload in payloads
            if (row := existing.get(str(payload[0]))) is not None
            and row["source_hash"] != payload[16]
            and not (
                row["detail_status"] == DetailRetrievalStatus.RETRIEVED.value
                and payload[18] != DetailRetrievalStatus.RETRIEVED.value
            )
        ]
        if not changed_ids:
            return
        changed_placeholders = ",".join("?" for _ in changed_ids)
        for table in ("criteria", "extraction_cache", "summary_cache"):
            connection.execute(
                f"DELETE FROM {table} WHERE nct_id IN ({changed_placeholders})",
                changed_ids,
            )

    @staticmethod
    def _upsert_on_connection(
        connection: sqlite3.Connection, payloads: list[tuple[Any, ...]]
    ) -> None:
        ClinicalRepository._invalidate_changed_sources(connection, payloads)
        connection.executemany(
            """
            INSERT INTO studies (
                nct_id, title, overall_status, conditions_json, phases_json,
                source_description, brief_summary, detailed_description,
                eligibility_criteria, minimum_age, maximum_age, sex,
                locations_json, contacts_json, source_url, retrieved_at,
                source_hash, raw_json, detail_status, detail_attempted_at,
                detail_error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(nct_id) DO UPDATE SET
                title=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.title ELSE excluded.title END,
                overall_status=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.overall_status ELSE excluded.overall_status END,
                conditions_json=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.conditions_json ELSE excluded.conditions_json END,
                phases_json=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.phases_json ELSE excluded.phases_json END,
                source_description=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.source_description ELSE excluded.source_description END,
                brief_summary=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.brief_summary ELSE excluded.brief_summary END,
                detailed_description=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.detailed_description ELSE excluded.detailed_description END,
                eligibility_criteria=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.eligibility_criteria ELSE excluded.eligibility_criteria END,
                minimum_age=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.minimum_age ELSE excluded.minimum_age END,
                maximum_age=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.maximum_age ELSE excluded.maximum_age END,
                sex=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.sex ELSE excluded.sex END,
                locations_json=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.locations_json ELSE excluded.locations_json END,
                contacts_json=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.contacts_json ELSE excluded.contacts_json END,
                source_url=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.source_url ELSE excluded.source_url END,
                retrieved_at=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.retrieved_at ELSE excluded.retrieved_at END,
                source_hash=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.source_hash ELSE excluded.source_hash END,
                raw_json=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.raw_json ELSE excluded.raw_json END,
                detail_status=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.detail_status ELSE excluded.detail_status END,
                detail_attempted_at=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.detail_attempted_at ELSE excluded.detail_attempted_at END,
                detail_error_code=CASE WHEN studies.detail_status = 'retrieved'
                               AND excluded.detail_status != 'retrieved'
                           THEN studies.detail_error_code ELSE excluded.detail_error_code END
            """,
            payloads,
        )

    def upsert_studies(self, studies: Iterable[Study]) -> int:
        payloads = self._study_payloads(studies)
        if not payloads:
            return 0
        with closing(self._connect()) as connection:
            self._upsert_on_connection(connection, payloads)
            connection.commit()
        return len(payloads)

    def replace_complete_snapshot(self, studies: Iterable[Study]) -> int:
        """Atomically upsert a complete recruiting snapshot and remove stale studies."""

        payloads = self._study_payloads(studies)
        if not payloads:
            raise ValueError("refusing to replace a complete snapshot with no accepted studies")
        identifiers = [(payload[0],) for payload in payloads]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TEMP TABLE current_snapshot_ids (nct_id TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO current_snapshot_ids (nct_id) VALUES (?)", identifiers
            )
            self._upsert_on_connection(connection, payloads)
            connection.execute(
                "DELETE FROM studies WHERE nct_id NOT IN (SELECT nct_id FROM current_snapshot_ids)"
            )
            connection.commit()
        return len(payloads)

    def publish_complete_snapshot(
        self,
        studies: Iterable[Study],
        source_criteria: dict[str, list[StructuredCriterion]],
    ) -> int:
        """Atomically publish a complete study snapshot and all source-derived rules."""

        studies_list = list(studies)
        payloads = self._study_payloads(studies_list)
        if not payloads:
            raise ValueError("refusing to publish an empty complete snapshot")
        study_by_id = {study.nct_id: study for study in studies_list}
        if len(study_by_id) != len(studies_list):
            raise ValueError("complete snapshot contains duplicate NCT identifiers")
        flattened = [item for values in source_criteria.values() for item in values]
        criteria: list[StructuredCriterion] = []
        seen_criteria: dict[str, StructuredCriterion] = {}
        for criterion in flattened:
            previous = seen_criteria.get(criterion.criterion_id)
            if previous is not None and previous == criterion:
                # Deterministically generated criteria can be repeated by callers;
                # publishing the same identity and payload remains idempotent.
                continue
            seen_criteria[criterion.criterion_id] = criterion
            criteria.append(criterion)
        for criterion in criteria:
            study = study_by_id.get(criterion.nct_id)
            if study is None:
                raise ValueError("source criterion references a study outside the snapshot")
            if criterion.extraction_method != "source_structured":
                raise ValueError("snapshot publication accepts only source_structured criteria")
            if criterion.source_hash != study.source_hash:
                raise ValueError("source criterion hash must match its study")
        identifiers = [(study.nct_id,) for study in studies_list]
        incoming_expanded = {
            study.nct_id
            for study in studies_list
            if study.detail_status == DetailRetrievalStatus.RETRIEVED
        }
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TEMP TABLE published_snapshot_ids (nct_id TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO published_snapshot_ids (nct_id) VALUES (?)", identifiers
            )
            existing_expanded = {
                row["nct_id"]
                for row in connection.execute(
                    """
                    SELECT studies.nct_id
                    FROM studies
                    JOIN published_snapshot_ids USING (nct_id)
                    WHERE studies.detail_status = 'retrieved'
                    """
                ).fetchall()
            }
            preserved_expanded = existing_expanded - incoming_expanded
            replace_identifiers = [
                (study.nct_id,)
                for study in studies_list
                if study.nct_id not in preserved_expanded
            ]
            connection.execute(
                "CREATE TEMP TABLE replace_source_criteria_ids (nct_id TEXT PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO replace_source_criteria_ids (nct_id) VALUES (?)",
                replace_identifiers,
            )
            self._upsert_on_connection(connection, payloads)
            connection.execute(
                "DELETE FROM studies WHERE nct_id NOT IN (SELECT nct_id FROM published_snapshot_ids)"
            )
            # Replace every accepted generation idempotently. A narrow list-only
            # refresh still preserves criteria for an older expanded row whose source
            # payload is deliberately retained by the study upsert.
            connection.execute(
                """
                DELETE FROM criteria
                WHERE extraction_method = 'source_structured'
                  AND nct_id IN (SELECT nct_id FROM replace_source_criteria_ids)
                """
            )
            connection.executemany(
                "INSERT INTO criteria (criterion_id, nct_id, extraction_method, payload_json) VALUES (?, ?, ?, ?)",
                [
                    (
                        criterion.criterion_id,
                        criterion.nct_id,
                        criterion.extraction_method,
                        criterion.model_dump_json(),
                    )
                    for criterion in criteria
                    if criterion.nct_id not in preserved_expanded
                ],
            )
            connection.commit()
        return len(payloads)

    def _database_datetime(self, value: datetime | None) -> datetime | str | None:
        """Encode datetimes for SQLite TEXT while preserving PostgreSQL codecs."""

        if value is None or self.identity.startswith("postgresql:"):
            return value
        return value.isoformat()

    def save_ingestion_report(self, report: IngestionReport) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO ingestion_runs (
                    source_url, effective_page_size, pagination_completed,
                    started_at, index_retrieved_at, published_at, completed_at,
                    status, page_count,
                    raw_record_count, accepted_record_count, duplicate_identifier_count,
                    missing_value_counts_json, excluded_records_json, errors_json,
                    detail_requested_count, detail_retrieved_count,
                    index_retrieved_count, detail_cache_hit_count, detail_network_count,
                    detail_resumed_count, detail_failed_count, incomplete_detail_ids_json,
                    ma_verified_count, ma_unverified_count, ma_unverified_records_json,
                    request_count, retry_count, rate_limited_count, cohort_fingerprint,
                    checkpoint_resumed, publication_attempted, publication_completed,
                    published_record_count, previous_snapshot_preserved
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    report.source_url,
                    report.effective_page_size,
                    int(report.pagination_completed),
                    report.started_at.isoformat(),
                    self._database_datetime(report.index_retrieved_at),
                    self._database_datetime(report.published_at),
                    report.completed_at.isoformat(),
                    report.status.value,
                    report.page_count,
                    report.raw_record_count,
                    report.accepted_record_count,
                    report.duplicate_identifier_count,
                    json.dumps(report.missing_value_counts, sort_keys=True),
                    json.dumps(report.excluded_records, sort_keys=True),
                    json.dumps(report.errors),
                    report.detail_requested_count,
                    report.detail_retrieved_count,
                    report.index_retrieved_count,
                    report.detail_cache_hit_count,
                    report.detail_network_count,
                    report.detail_resumed_count,
                    report.detail_failed_count,
                    json.dumps(report.incomplete_detail_ids),
                    report.ma_verified_count,
                    report.ma_unverified_count,
                    json.dumps(report.ma_unverified_records, sort_keys=True),
                    report.request_count,
                    report.retry_count,
                    report.rate_limited_count,
                    report.cohort_fingerprint,
                    int(report.checkpoint_resumed),
                    int(report.publication_attempted),
                    int(report.publication_completed),
                    report.published_record_count,
                    int(report.previous_snapshot_preserved),
                ),
            )
            connection.commit()

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
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM studies ORDER BY nct_id").fetchall()
        studies = [self._row_to_study(row) for row in rows]
        if query:
            needle = query.casefold()
            studies = [
                study
                for study in studies
                if needle in study.title.casefold()
                or any(needle in item.casefold() for item in study.conditions)
            ]
        if condition:
            needle = condition.strip().casefold()
            studies = [
                study for study in studies if any(item.casefold() == needle for item in study.conditions)
            ]
        if phase:
            needle = normalize_phase(phase).casefold()
            studies = [
                study
                for study in studies
                if any(normalize_phase(item).casefold() == needle for item in study.phases)
            ]
        if age_years is not None:
            studies = [study for study in studies if _age_matches(study, age_years)]
        if location:
            needle = location.strip().casefold()
            studies = [
                study
                for study in studies
                if any(
                    needle in (value or "").casefold()
                    for item in study.locations
                    for value in (item.facility, item.city, item.state, item.country)
                )
            ]
        studies.sort(
            key=lambda study: (
                study.detail_status != DetailRetrievalStatus.RETRIEVED,
                study.nct_id,
            )
        )
        total = len(studies)
        start = (page - 1) * page_size
        return studies[start : start + page_size], total

    def get_studies(self, nct_ids: list[str] | None = None, limit: int = 20) -> list[Study]:
        with closing(self._connect()) as connection:
            if nct_ids is None:
                rows = connection.execute(
                    "SELECT * FROM studies ORDER BY nct_id LIMIT ?", (limit,)
                ).fetchall()
            elif not nct_ids:
                return []
            else:
                placeholders = ",".join("?" for _ in nct_ids)
                rows = connection.execute(
                    f"SELECT * FROM studies WHERE nct_id IN ({placeholders}) ORDER BY nct_id",
                    nct_ids,
                ).fetchall()
        return [self._row_to_study(row) for row in rows]

    def get_study(self, nct_id: str) -> Study | None:
        studies = self.get_studies([nct_id], limit=1)
        return studies[0] if studies else None

    def record_detail_failure(self, nct_id: str, attempted_at: datetime, error_code: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE studies
                SET detail_status = CASE WHEN detail_status = 'retrieved'
                                         THEN detail_status ELSE 'retrieval_failed' END,
                    detail_attempted_at = ?,
                    detail_error_code = ?
                WHERE nct_id = ?
                """,
                (attempted_at.isoformat(), error_code, nct_id),
            )
            connection.commit()

    def latest_retrieved_at(self) -> datetime | None:
        with closing(self._connect()) as connection:
            published = connection.execute(
                """
                SELECT index_retrieved_at AS value
                FROM ingestion_runs
                WHERE publication_completed = 1
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if published is None:
                published = connection.execute(
                    "SELECT MAX(retrieved_at) AS value FROM studies"
                ).fetchone()
        value = published["value"] if published else None
        if value is None or isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value)

    def replace_criteria(self, nct_id: str, method: str, criteria: list[StructuredCriterion]) -> None:
        if any(criterion.extraction_method != method for criterion in criteria):
            raise ValueError("criterion extraction_method must match replacement method")
        if any(criterion.nct_id != nct_id for criterion in criteria):
            raise ValueError("criterion nct_id must match replacement study")
        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM criteria WHERE nct_id = ? AND extraction_method = ?", (nct_id, method)
            )
            connection.executemany(
                "INSERT INTO criteria (criterion_id, nct_id, extraction_method, payload_json) VALUES (?, ?, ?, ?)",
                [
                    (
                        criterion.criterion_id,
                        criterion.nct_id,
                        criterion.extraction_method,
                        criterion.model_dump_json(),
                    )
                    for criterion in criteria
                ],
            )
            connection.commit()

    def get_criteria(
        self, nct_id: str, *, source_hash: str | None = None
    ) -> list[StructuredCriterion]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM criteria WHERE nct_id = ? ORDER BY criterion_id", (nct_id,)
            ).fetchall()
        criteria = [StructuredCriterion.model_validate_json(row["payload_json"]) for row in rows]
        return [item for item in criteria if source_hash is None or item.source_hash == source_hash]

    def existing_study_ids(self, nct_ids: list[str]) -> set[str]:
        if not nct_ids:
            return set()
        placeholders = ",".join("?" for _ in nct_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT nct_id FROM studies WHERE nct_id IN ({placeholders})", nct_ids
            ).fetchall()
        return {row["nct_id"] for row in rows}

    def get_cached_extraction(self, cache_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM extraction_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def publish_extraction(
        self,
        *,
        cache_key: str,
        nct_id: str,
        source_hash: str,
        provider: str,
        model: str,
        schema_version: str,
        prompt_version: str,
        payload: dict[str, Any],
        criteria: list[StructuredCriterion],
        created_at: datetime,
    ) -> None:
        """Atomically publish validated extraction cache, criteria, and accepted attempt."""

        if any(
            item.nct_id != nct_id
            or item.source_hash != source_hash
            or item.extraction_method != "llm"
            for item in criteria
        ):
            raise ValueError("published criteria must match the extraction generation")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT source_hash FROM studies WHERE nct_id = ?", (nct_id,)
            ).fetchone()
            if current is None or current["source_hash"] != source_hash:
                raise ValueError("cannot publish extraction for a stale or missing study")
            existing_payloads = connection.execute(
                """
                SELECT payload_json, coverage_verified FROM extraction_cache
                WHERE nct_id = ? AND source_hash = ?
                """,
                (nct_id, source_hash),
            ).fetchall()
            existing_complete = any(
                bool(row["coverage_verified"])
                and json.loads(row["payload_json"]).get("coverage", {}).get("status") == "complete"
                for row in existing_payloads
            )
            incoming_status = payload.get("coverage", {}).get("status")
            if existing_complete and incoming_status != "complete":
                raise ValueError(
                    "refusing to replace complete current-source coverage with incomplete coverage"
                )
            connection.execute(
                """
                INSERT INTO extraction_cache (
                    cache_key, nct_id, source_hash, provider, model, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    nct_id=excluded.nct_id,
                    source_hash=excluded.source_hash,
                    provider=excluded.provider,
                    model=excluded.model,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (
                    cache_key,
                    nct_id,
                    source_hash,
                    provider,
                    model,
                    json.dumps(payload, sort_keys=True),
                    created_at.isoformat(),
                ),
            )
            connection.execute(
                "DELETE FROM criteria WHERE nct_id = ? AND extraction_method = 'llm'", (nct_id,)
            )
            connection.executemany(
                "INSERT INTO criteria (criterion_id, nct_id, extraction_method, payload_json) VALUES (?, ?, ?, ?)",
                [
                    (item.criterion_id, item.nct_id, item.extraction_method, item.model_dump_json())
                    for item in criteria
                ],
            )
            connection.execute(
                "DELETE FROM extraction_cache WHERE nct_id = ? AND cache_key != ?",
                (nct_id, cache_key),
            )
            connection.execute(
                """
                INSERT INTO extraction_attempts (
                    nct_id, source_hash, provider, model, schema_version,
                    prompt_version, status, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'accepted', NULL, ?)
                """,
                (
                    nct_id,
                    source_hash,
                    provider,
                    model,
                    schema_version,
                    prompt_version,
                    created_at.isoformat(),
                ),
            )
            connection.commit()

    def delete_extraction_cache(self, cache_key: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM extraction_cache WHERE cache_key = ?", (cache_key,))
            connection.commit()

    def record_extraction_attempt(
        self,
        *,
        nct_id: str,
        source_hash: str,
        provider: str,
        model: str,
        schema_version: str,
        prompt_version: str,
        status: str,
        error: str,
        created_at: datetime,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO extraction_attempts (
                    nct_id, source_hash, provider, model, schema_version,
                    prompt_version, status, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    nct_id,
                    source_hash,
                    provider,
                    model,
                    schema_version,
                    prompt_version,
                    status,
                    error,
                    created_at.isoformat(),
                ),
            )
            connection.commit()

    def approve_extraction_coverage(
        self,
        cache_key: str,
        *,
        verification_method: str,
        verified_at: datetime,
    ) -> None:
        allowed = {"manual_protocol_review", "deterministic_test_fixture"}
        if verification_method not in allowed:
            raise ValueError("unsupported independent coverage verification method")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM extraction_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is None:
                raise ValueError("extraction generation does not exist")
            payload = json.loads(row["payload_json"])
            if payload.get("coverage", {}).get("status") != "complete":
                raise ValueError("only provider-declared complete coverage can be independently verified")
            connection.execute(
                """
                UPDATE extraction_cache
                SET coverage_verified = 1,
                    coverage_verification_method = ?,
                    coverage_verified_at = ?
                WHERE cache_key = ?
                """,
                (verification_method, verified_at.isoformat(), cache_key),
            )
            connection.commit()

    def get_extraction_state(
        self, nct_id: str, source_hash: str
    ) -> tuple[str, dict[str, Any] | None, bool, str | None]:
        with closing(self._connect()) as connection:
            accepted = connection.execute(
                """
                SELECT payload_json, coverage_verified, coverage_verification_method
                FROM extraction_cache
                WHERE nct_id = ? AND source_hash = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (nct_id, source_hash),
            ).fetchone()
            if accepted:
                try:
                    return (
                        "accepted",
                        json.loads(accepted["payload_json"]),
                        bool(accepted["coverage_verified"]),
                        accepted["coverage_verification_method"],
                    )
                except json.JSONDecodeError:
                    return "failed", None, False, None
            attempt = connection.execute(
                """
                SELECT status FROM extraction_attempts
                WHERE nct_id = ? AND source_hash = ?
                ORDER BY id DESC LIMIT 1
                """,
                (nct_id, source_hash),
            ).fetchone()
            if attempt and attempt["status"] != "accepted":
                return "failed", None, False, None
            stale = connection.execute(
                """
                SELECT 1 FROM extraction_cache
                WHERE nct_id = ? AND source_hash != ? LIMIT 1
                """,
                (nct_id, source_hash),
            ).fetchone()
        return ("stale", None, False, None) if stale else ("not_attempted", None, False, None)

    def get_cached_summary(self, cache_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM summary_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_current_summary(self, nct_id: str, source_hash: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM summary_cache
                WHERE nct_id = ? AND source_hash = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (nct_id, source_hash),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        value = payload.get("plain_language_summary")
        return value if isinstance(value, str) and value.strip() else None

    def get_search_enrichment(
        self, studies: list[Study]
    ) -> tuple[
        dict[str, str | None],
        dict[str, tuple[str, dict[str, Any] | None, bool, str | None]],
    ]:
        """Load summary and extraction metadata for one search page in one connection."""

        if not studies:
            return {}, {}
        source_hashes = {study.nct_id: study.source_hash for study in studies}
        pair_predicate = " OR ".join("(nct_id = ? AND source_hash = ?)" for _ in studies)
        pair_parameters = tuple(
            value for study in studies for value in (study.nct_id, study.source_hash)
        )
        ids = tuple(source_hashes)
        id_placeholders = ",".join("?" for _ in ids)
        summaries: dict[str, str | None] = {nct_id: None for nct_id in ids}
        states: dict[str, tuple[str, dict[str, Any] | None, bool, str | None]] = {}

        with closing(self._connect()) as connection:
            summary_rows = connection.execute(
                f"SELECT nct_id, payload_json FROM summary_cache WHERE {pair_predicate} "
                "ORDER BY created_at DESC",
                pair_parameters,
            ).fetchall()
            extraction_rows = connection.execute(
                f"SELECT nct_id, payload_json, coverage_verified, coverage_verification_method "
                f"FROM extraction_cache WHERE {pair_predicate} ORDER BY created_at DESC",
                pair_parameters,
            ).fetchall()
            attempt_rows = connection.execute(
                f"SELECT nct_id, status FROM extraction_attempts WHERE {pair_predicate} "
                "ORDER BY id DESC",
                pair_parameters,
            ).fetchall()
            stale_rows = connection.execute(
                f"SELECT DISTINCT nct_id, source_hash FROM extraction_cache "
                f"WHERE nct_id IN ({id_placeholders})",
                ids,
            ).fetchall()

        seen_summaries: set[str] = set()
        for row in summary_rows:
            nct_id = row["nct_id"]
            if nct_id in seen_summaries:
                continue
            seen_summaries.add(nct_id)
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                continue
            value = payload.get("plain_language_summary")
            if isinstance(value, str) and value.strip():
                summaries[nct_id] = value

        for row in extraction_rows:
            nct_id = row["nct_id"]
            if nct_id in states:
                continue
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                states[nct_id] = ("failed", None, False, None)
            else:
                states[nct_id] = (
                    "accepted",
                    payload,
                    bool(row["coverage_verified"]),
                    row["coverage_verification_method"],
                )

        latest_attempts: dict[str, str] = {}
        for row in attempt_rows:
            latest_attempts.setdefault(row["nct_id"], row["status"])
        stale_ids = {
            row["nct_id"]
            for row in stale_rows
            if row["source_hash"] != source_hashes[row["nct_id"]]
        }
        for nct_id in ids:
            if nct_id in states:
                continue
            if latest_attempts.get(nct_id) not in (None, "accepted"):
                states[nct_id] = ("failed", None, False, None)
            elif nct_id in stale_ids:
                states[nct_id] = ("stale", None, False, None)
            else:
                states[nct_id] = ("not_attempted", None, False, None)
        return summaries, states

    def publish_summary(
        self,
        *,
        cache_key: str,
        nct_id: str,
        source_hash: str,
        provider: str,
        model: str,
        schema_version: str,
        prompt_version: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT source_hash FROM studies WHERE nct_id = ?", (nct_id,)
            ).fetchone()
            if current is None or current["source_hash"] != source_hash:
                raise ValueError("cannot publish summary for a stale or missing study")
            connection.execute(
                """
                INSERT INTO summary_cache (
                    cache_key, nct_id, source_hash, provider, model,
                    schema_version, prompt_version, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    nct_id=excluded.nct_id,
                    source_hash=excluded.source_hash,
                    provider=excluded.provider,
                    model=excluded.model,
                    schema_version=excluded.schema_version,
                    prompt_version=excluded.prompt_version,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (
                    cache_key,
                    nct_id,
                    source_hash,
                    provider,
                    model,
                    schema_version,
                    prompt_version,
                    json.dumps(payload, sort_keys=True),
                    created_at.isoformat(),
                ),
            )
            connection.execute(
                "DELETE FROM summary_cache WHERE nct_id = ? AND cache_key != ?",
                (nct_id, cache_key),
            )
            connection.commit()

    def schema_ready(self) -> bool:
        """Return whether the expected schema is present and queryable."""

        required = {
            "studies",
            "ingestion_runs",
            "criteria",
            "extraction_cache",
            "extraction_attempts",
            "summary_cache",
        }
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
        except sqlite3.Error:
            return False
        return required.issubset({row["name"] for row in rows})

    def record_counts(self) -> dict[str, int]:
        """Return non-sensitive row counts used by readiness diagnostics."""

        counts: dict[str, int] = {}
        with closing(self._connect()) as connection:
            for table in (
                "studies",
                "ingestion_runs",
                "criteria",
                "extraction_cache",
                "extraction_attempts",
                "summary_cache",
            ):
                counts[table] = int(
                    connection.execute(f"SELECT COUNT(*) AS value FROM {table}").fetchone()[
                        "value"
                    ]
                )
        return counts

    def data_ready(self) -> bool:
        """Return whether a non-empty serving snapshot exists."""

        try:
            return self.record_counts()["studies"] > 0
        except sqlite3.Error:
            return False

    def storage_stats(self) -> dict[str, Any]:
        """Return backend, schema, data, counts, and allocated storage statistics."""

        with closing(self._connect()) as connection:
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            latest = connection.execute(
                """
                SELECT status, pagination_completed, completed_at,
                       index_retrieved_at, published_at
                FROM ingestion_runs
                WHERE publication_completed = 1
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        counts = self.record_counts()
        return {
            "backend": "sqlite",
            "schema_ready": self.schema_ready(),
            "data_ready": counts["studies"] > 0,
            "counts": counts,
            "storage_bytes": page_count * page_size,
            "latest_ingestion": dict(latest) if latest else None,
        }

    @staticmethod
    def _row_to_study(row: sqlite3.Row) -> Study:
        return Study.model_validate(
            {
                "nct_id": row["nct_id"],
                "title": row["title"],
                "overall_status": row["overall_status"],
                "conditions": json.loads(row["conditions_json"]),
                "phases": json.loads(row["phases_json"]),
                "source_description": row["source_description"],
                "brief_summary": row["brief_summary"],
                "detailed_description": row["detailed_description"],
                "eligibility_criteria": row["eligibility_criteria"],
                "minimum_age": row["minimum_age"],
                "maximum_age": row["maximum_age"],
                "sex": row["sex"],
                "locations": json.loads(row["locations_json"]),
                "contacts": json.loads(row["contacts_json"]),
                "source_url": row["source_url"],
                "retrieved_at": row["retrieved_at"],
                "source_hash": row["source_hash"],
                "raw_source": json.loads(row["raw_json"]),
                "detail_status": row["detail_status"],
                "detail_attempted_at": row["detail_attempted_at"],
                "detail_error_code": row["detail_error_code"],
            }
        )


def _age_matches(study: Study, age_years: float) -> bool:
    minimum = _age_to_years(study.minimum_age)
    maximum = _age_to_years(study.maximum_age)
    if minimum is None and maximum is None:
        return False
    return (minimum is None or age_years >= minimum) and (
        maximum is None or age_years <= maximum
    )


def _age_to_years(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s+(Year|Years|Month|Months|Week|Weeks|Day|Days)\s*",
        value,
    )
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).casefold()
    if unit.startswith("year"):
        return number
    if unit.startswith("month"):
        return number / 12
    if unit.startswith("week"):
        return number / 52.1429
    return number / 365.2425


def _clean_source_text(value: object) -> str | None:
    """Return nonblank official text without inventing a replacement."""

    return value.strip() if isinstance(value, str) and value.strip() else None
