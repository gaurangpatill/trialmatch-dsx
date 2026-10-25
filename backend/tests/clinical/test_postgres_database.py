from __future__ import annotations

import os
from contextlib import closing
from datetime import UTC, datetime

import psycopg
import pytest

from backend.app.clinical.migrations import (
    LATEST_POSTGRES_SCHEMA_VERSION,
    migrate_postgres,
)
from backend.app.clinical.models import IngestionReport, IngestionStatus
from backend.app.clinical.normalize import source_structured_criteria
from backend.app.clinical.postgres_database import PostgresClinicalRepository


def _successful_ingestion_report() -> IngestionReport:
    return IngestionReport(
        source_url="https://clinicaltrials.gov/api/v2/studies",
        effective_page_size=20,
        pagination_completed=True,
        started_at=datetime(2026, 10, 24, 15, 0, tzinfo=UTC),
        index_retrieved_at=datetime(2026, 10, 24, 16, 0, tzinfo=UTC),
        published_at=datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
        completed_at=datetime(2026, 10, 24, 19, 0, tzinfo=UTC),
        status=IngestionStatus.COMPLETE,
        page_count=1,
        raw_record_count=1,
        accepted_record_count=1,
        duplicate_identifier_count=0,
        missing_value_counts={},
        excluded_records=[],
        errors=[],
        detail_requested_count=0,
        detail_retrieved_count=0,
        publication_attempted=True,
        publication_completed=True,
        published_record_count=1,
    )


@pytest.fixture
def postgres_repository():
    database_url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    migrate_postgres(database_url)
    repository = PostgresClinicalRepository(database_url)
    with closing(repository._connect()) as connection:
        connection.execute(
            """
            TRUNCATE TABLE ingestion_runs, criteria, extraction_cache,
                           extraction_attempts, summary_cache, studies
            RESTART IDENTITY CASCADE
            """
        )
        connection.commit()
    yield repository
    with closing(repository._connect()) as connection:
        connection.execute(
            """
            TRUNCATE TABLE ingestion_runs, criteria, extraction_cache,
                           extraction_attempts, summary_cache, studies
            RESTART IDENTITY CASCADE
            """
        )
        connection.commit()


def test_postgres_migrations_are_explicit_and_idempotent():
    database_url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    migrate_postgres(database_url)
    assert migrate_postgres(database_url) == ()
    with psycopg.connect(database_url) as connection:
        marker_count = connection.execute(
            "SELECT COUNT(*) FROM clinical_schema_migrations WHERE version = %s",
            (LATEST_POSTGRES_SCHEMA_VERSION,),
        ).fetchone()[0]
        columns = dict(
            connection.execute(
                """
                SELECT column_name, data_type || ':' || is_nullable
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'ingestion_runs'
                  AND column_name IN ('index_retrieved_at', 'published_at')
                """
            ).fetchall()
        )
    assert marker_count == 1
    assert columns == {
        "index_retrieved_at": "timestamp with time zone:YES",
        "published_at": "timestamp with time zone:YES",
    }
    assert PostgresClinicalRepository(database_url).schema_ready() is True


def test_postgres_repository_snapshot_and_source_version_contract(
    postgres_repository, study_factory
):
    repository = postgres_repository
    original = study_factory(title="Original")
    criteria = source_structured_criteria(original)

    assert repository.publish_complete_snapshot(
        [original], {original.nct_id: criteria}
    ) == 1
    assert repository.publish_complete_snapshot(
        [original], {original.nct_id: criteria}
    ) == 1
    repository.publish_summary(
        cache_key="summary-original",
        nct_id=original.nct_id,
        source_hash=original.source_hash,
        provider="test-provider",
        model="test-model",
        schema_version="test-v1",
        prompt_version="test-v1",
        payload={"plain_language_summary": "Synthetic summary."},
        created_at=original.retrieved_at,
    )

    repository.upsert_studies([original])
    assert repository.get_cached_summary("summary-original") is not None

    changed = study_factory(title="Changed")
    repository.upsert_studies([changed])
    assert repository.get_cached_summary("summary-original") is None
    assert repository.get_criteria(changed.nct_id) == []
    assert repository.storage_stats()["counts"]["studies"] == 1


def test_postgres_failed_snapshot_publication_rolls_back(postgres_repository, study_factory):
    repository = postgres_repository
    previous = study_factory("NCT00000003", title="Previous")
    repository.upsert_studies([previous])
    first = study_factory("NCT00000001")
    second = study_factory("NCT00000002")
    first_criterion = source_structured_criteria(first)[0]
    collision = source_structured_criteria(second)[0].model_copy(
        update={"criterion_id": first_criterion.criterion_id}
    )

    with pytest.raises(psycopg.IntegrityError):
        repository.publish_complete_snapshot(
            [first, second],
            {first.nct_id: [first_criterion], second.nct_id: [collision]},
        )

    studies, total = repository.search_studies()
    assert total == 1
    assert studies[0].nct_id == previous.nct_id


def test_postgres_inherited_report_persistence_preserves_snapshot_freshness(
    postgres_repository, study_factory
):
    repository = postgres_repository
    report = _successful_ingestion_report()
    repository.save_ingestion_report(report)

    with closing(repository._connect()) as connection:
        row = connection.execute(
            """
            SELECT index_retrieved_at, published_at
            FROM ingestion_runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert row["index_retrieved_at"] == report.index_retrieved_at
    assert row["published_at"] == report.published_at
    assert isinstance(row["index_retrieved_at"], datetime)
    assert isinstance(row["published_at"], datetime)

    # A later detail write changes studies.retrieved_at, not published snapshot freshness.
    repository.upsert_studies([study_factory()])
    assert repository.latest_retrieved_at() == report.index_retrieved_at
