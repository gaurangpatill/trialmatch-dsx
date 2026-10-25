from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from backend.app.clinical.database import ClinicalRepository
from backend.app.clinical.models import IngestionReport, IngestionStatus
from backend.app.clinical.normalize import normalize_study, source_structured_criteria
from backend.app.clinical.postgres_database import _postgres_identity
from backend.app.clinical.repository import create_repository


def _ingestion_report(
    *, index_retrieved_at: datetime, published_at: datetime | None
) -> IngestionReport:
    publication_completed = published_at is not None
    return IngestionReport(
        source_url="https://clinicaltrials.gov/api/v2/studies",
        effective_page_size=20,
        pagination_completed=publication_completed,
        started_at=datetime(2026, 10, 24, 15, 0, tzinfo=UTC),
        index_retrieved_at=index_retrieved_at,
        published_at=published_at,
        completed_at=datetime(2026, 10, 24, 23, 0, tzinfo=UTC),
        status=(IngestionStatus.COMPLETE if publication_completed else IngestionStatus.PARTIAL),
        page_count=1,
        raw_record_count=1,
        accepted_record_count=1,
        duplicate_identifier_count=0,
        missing_value_counts={},
        excluded_records=[],
        errors=[] if publication_completed else ["synthetic detail failure"],
        detail_requested_count=0,
        detail_retrieved_count=0,
        publication_attempted=True,
        publication_completed=publication_completed,
        published_record_count=1 if publication_completed else 0,
        previous_snapshot_preserved=not publication_completed,
    )


def _publish_derived_outputs(repository, study) -> tuple[str, str]:
    extraction_key = f"extraction-{study.source_hash}"
    summary_key = f"summary-{study.source_hash}"
    repository.replace_criteria(
        study.nct_id,
        "source_structured",
        source_structured_criteria(study),
    )
    repository.publish_extraction(
        cache_key=extraction_key,
        nct_id=study.nct_id,
        source_hash=study.source_hash,
        provider="test-provider",
        model="test-model",
        schema_version="test-v1",
        prompt_version="test-v1",
        payload={"coverage": {"status": "partial"}},
        criteria=[],
        created_at=datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
    )
    repository.publish_summary(
        cache_key=summary_key,
        nct_id=study.nct_id,
        source_hash=study.source_hash,
        provider="test-provider",
        model="test-model",
        schema_version="test-v1",
        prompt_version="test-v1",
        payload={"plain_language_summary": "Synthetic summary."},
        created_at=datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
    )
    return extraction_key, summary_key


def test_repeat_snapshot_with_repeated_deterministic_criteria_is_idempotent(
    study_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = study_factory()
    criterion = source_structured_criteria(study)[0]
    repeated = {"first": [criterion], "second": [criterion]}

    assert repository.publish_complete_snapshot([study], repeated) == 1
    assert repository.publish_complete_snapshot([study], repeated) == 1

    assert repository.get_criteria(study.nct_id) == [criterion]
    assert repository.record_counts()["studies"] == 1


def test_source_hash_controls_transactional_cache_invalidation(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    original = study_factory(title="Original")
    repository.upsert_studies([original])
    extraction_key, summary_key = _publish_derived_outputs(repository, original)

    repository.upsert_studies([original])
    assert repository.get_cached_extraction(extraction_key) is not None
    assert repository.get_cached_summary(summary_key) is not None
    assert repository.get_criteria(original.nct_id)

    changed = study_factory(title="Changed")
    repository.upsert_studies([changed])

    assert repository.get_study(changed.nct_id).source_hash == changed.source_hash
    assert repository.get_cached_extraction(extraction_key) is None
    assert repository.get_cached_summary(summary_key) is None
    assert repository.get_criteria(changed.nct_id) == []


def test_failed_snapshot_publication_rolls_back_source_and_cache_invalidation(
    study_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    original = study_factory("NCT00000001", title="Published")
    repository.upsert_studies([original])
    _, summary_key = _publish_derived_outputs(repository, original)

    changed = study_factory("NCT00000001", title="Changed")
    second = study_factory("NCT00000002")
    first_criterion = source_structured_criteria(changed)[0]
    collision = source_structured_criteria(second)[0].model_copy(
        update={"criterion_id": first_criterion.criterion_id}
    )

    with pytest.raises(sqlite3.IntegrityError):
        repository.publish_complete_snapshot(
            [changed, second],
            {changed.nct_id: [first_criterion], second.nct_id: [collision]},
        )

    persisted = repository.get_study(original.nct_id)
    assert persisted is not None
    assert persisted.source_hash == original.source_hash
    assert repository.get_cached_summary(summary_key) is not None


def test_storage_readiness_statistics_are_non_sensitive(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    assert repository.schema_ready() is True
    assert repository.data_ready() is False

    repository.upsert_studies([study_factory()])
    stats = repository.storage_stats()

    assert repository.data_ready() is True
    assert stats["backend"] == "sqlite"
    assert stats["schema_ready"] is True
    assert stats["data_ready"] is True
    assert stats["counts"]["studies"] == 1
    assert stats["storage_bytes"] > 0
    assert "sqlite:///" not in repr(stats)


def test_repository_factory_allowlists_schemes_without_creating_empty_sqlite(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "existing.sqlite3"
    ClinicalRepository(database_path)
    repository = create_repository(f"sqlite:///{database_path}")
    assert repository.identity.startswith("sqlite:")

    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(RuntimeError, match="initialize it explicitly"):
        create_repository(f"sqlite:///{missing}")
    assert not missing.exists()

    for unsafe in (
        "http://database.example/clinical",
        "file:///tmp/clinical.sqlite3",
        "sqlite://remote/clinical.sqlite3",
        "sqlite:///clinical.sqlite3?mode=memory",
    ):
        with pytest.raises(RuntimeError):
            create_repository(unsafe)

    captured: list[str] = []

    class FakePostgresRepository:
        def __init__(self, value: str):
            captured.append(value)

    monkeypatch.setattr(
        "backend.app.clinical.repository.PostgresClinicalRepository",
        FakePostgresRepository,
    )
    create_repository("postgresql+psycopg://user:secret@db.example/clinical?sslmode=require")
    assert captured == ["postgresql://user:secret@db.example/clinical?sslmode=require"]


def test_postgres_repository_identity_excludes_credentials_and_url():
    identity = _postgres_identity(
        "postgresql://sensitive-user:sensitive-password@db.example:5432/clinical"
    )
    assert identity.startswith("postgresql:")
    assert "sensitive" not in identity
    assert "db.example" not in identity


def test_republishing_same_extraction_preserves_independent_verification(
    study_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = study_factory()
    repository.upsert_studies([study])
    arguments = {
        "cache_key": "stable-generation",
        "nct_id": study.nct_id,
        "source_hash": study.source_hash,
        "provider": "test-provider",
        "model": "test-model",
        "schema_version": "test-v1",
        "prompt_version": "test-v1",
        "payload": {"coverage": {"status": "complete"}},
        "criteria": [],
        "created_at": datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
    }
    repository.publish_extraction(**arguments)
    repository.approve_extraction_coverage(
        "stable-generation",
        verification_method="deterministic_test_fixture",
        verified_at=datetime(2026, 10, 24, 18, 5, tzinfo=UTC),
    )

    repository.publish_extraction(**arguments)

    state, _, verified, method = repository.get_extraction_state(
        study.nct_id, study.source_hash
    )
    assert state == "accepted"
    assert verified is True
    assert method == "deterministic_test_fixture"


def test_repeat_list_only_snapshot_with_source_criteria_is_idempotent(
    source_record_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    record = source_record_factory()
    record["protocolSection"].pop("contactsLocationsModule")
    study = normalize_study(record)
    criteria = source_structured_criteria(study)

    assert study.detail_status.value == "not_retrieved"
    assert repository.publish_complete_snapshot(
        [study], {study.nct_id: criteria}
    ) == 1
    assert repository.publish_complete_snapshot(
        [study], {study.nct_id: criteria}
    ) == 1
    assert {item.criterion_id for item in repository.get_criteria(study.nct_id)} == {
        item.criterion_id for item in criteria
    }


def test_snapshot_freshness_uses_latest_successful_index_not_later_detail_write(
    study_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = study_factory()
    repository.upsert_studies([study])

    # Legacy databases without successful report rows still derive freshness from studies.
    assert repository.latest_retrieved_at() == study.retrieved_at

    index_retrieved_at = datetime(2026, 10, 24, 16, 0, tzinfo=UTC)
    published_at = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
    repository.save_ingestion_report(
        _ingestion_report(
            index_retrieved_at=index_retrieved_at,
            published_at=published_at,
        )
    )

    # Detail retrieval and a later unsuccessful run must not advance dataset freshness.
    repository.upsert_studies(
        [
            study.model_copy(
                update={"retrieved_at": datetime(2026, 10, 24, 20, 0, tzinfo=UTC)}
            )
        ]
    )
    repository.save_ingestion_report(
        _ingestion_report(
            index_retrieved_at=datetime(2026, 10, 24, 21, 0, tzinfo=UTC),
            published_at=None,
        )
    )

    assert repository.latest_retrieved_at() == index_retrieved_at
    stats = repository.storage_stats()
    assert stats["latest_ingestion"] == {
        "status": "complete",
        "pagination_completed": 1,
        "completed_at": datetime(2026, 10, 24, 23, 0, tzinfo=UTC).isoformat(),
        "index_retrieved_at": index_retrieved_at.isoformat(),
        "published_at": published_at.isoformat(),
    }
    assert "sqlite:///" not in repr(stats)


def test_sqlite_snapshot_freshness_migration_is_additive_and_idempotent(tmp_path):
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE ingestion_runs (id INTEGER PRIMARY KEY)")

    repository = ClinicalRepository(database_path)
    repository.initialize()

    with sqlite3.connect(database_path) as connection:
        columns = connection.execute("PRAGMA table_info(ingestion_runs)").fetchall()
    matching = {
        row[1]: (row[2], row[3])
        for row in columns
        if row[1] in {"index_retrieved_at", "published_at"}
    }
    assert matching == {
        "index_retrieved_at": ("TEXT", 0),
        "published_at": ("TEXT", 0),
    }
    assert sum(row[1] == "index_retrieved_at" for row in columns) == 1
    assert sum(row[1] == "published_at" for row in columns) == 1
