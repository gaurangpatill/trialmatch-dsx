from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from backend.app.clinical.client import ClinicalTrialsClient, DetailResult, FetchResult, RequestMetrics
from backend.app.clinical.cli import parser
from backend.app.clinical.database import ClinicalRepository
from backend.app.clinical.ingestion import ClinicalIngestionService, update_manifest
from backend.app.clinical.models import IngestionReport, IngestionStatus
from backend.app.clinical.normalize import normalize_study


def _official_record(source_record_factory, nct_id: str, *, revision: int = 1):
    record = source_record_factory(nct_id, title=f"Synthetic revision {revision}")
    protocol = record["protocolSection"]
    protocol["statusModule"].update(
        {
            "statusVerifiedDate": f"2026-0{revision}-15",
            "lastUpdateSubmitDate": f"2026-0{revision}-16",
            "lastUpdatePostDateStruct": {
                "date": f"2026-0{revision}-17",
                "type": "ACTUAL",
            },
        }
    )
    protocol["designModule"]["studyType"] = "INTERVENTIONAL"
    protocol["armsInterventionsModule"] = {
        "interventions": [
            {
                "type": "DRUG",
                "name": f"Synthetic treatment {revision}",
                "description": "Official synthetic intervention text.",
                "otherNames": ["Fixture alias"],
            }
        ]
    }
    return record


def test_normalization_preserves_official_refresh_metadata(source_record_factory):
    study = normalize_study(_official_record(source_record_factory, "NCT00000001"))

    assert study.source_last_updated == "2026-01-17"
    assert study.status_verified_date == "2026-01-15"
    assert study.study_type == "INTERVENTIONAL"
    assert study.interventions[0].intervention_type == "DRUG"
    assert study.interventions[0].name == "Synthetic treatment 1"


@pytest.mark.asyncio
async def test_identical_imports_succeed_and_changed_index_record_refetches(
    source_record_factory, tmp_path
):
    revisions = {"NCT00000001": 1, "NCT00000002": 1}
    detail_calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/studies"):
            return httpx.Response(
                200,
                json={
                    "studies": [
                        _official_record(source_record_factory, nct_id, revision=revisions[nct_id])
                        for nct_id in sorted(revisions)
                    ]
                },
                request=request,
            )
        nct_id = request.url.path.rsplit("/", 1)[-1]
        detail_calls[nct_id] += 1
        return httpx.Response(
            200,
            json=_official_record(source_record_factory, nct_id, revision=revisions[nct_id]),
            request=request,
        )

    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    checkpoint = tmp_path / "checkpoint.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(
            http_client=http_client,
            cache_dir=tmp_path / "cache",
            max_retries=0,
            min_request_interval_seconds=0,
        ) as client:
            service = ClinicalIngestionService(client, repository)
            first = await service.ingest(checkpoint_path=checkpoint)
            second = await service.ingest(checkpoint_path=checkpoint)
            revisions["NCT00000001"] = 2
            third = await service.ingest(checkpoint_path=checkpoint)

    assert first.status == second.status == third.status == IngestionStatus.COMPLETE
    for report in (first, second, third):
        assert report.index_retrieved_at is not None
        assert report.published_at is not None
        assert report.started_at <= report.index_retrieved_at <= report.published_at
        assert report.published_at <= report.completed_at
    assert first.published_record_count == second.published_record_count == 2
    assert second.detail_cache_hit_count == 2
    assert second.detail_resumed_count == 2
    assert detail_calls == Counter({"NCT00000001": 2, "NCT00000002": 1})
    updated = repository.get_study("NCT00000001")
    assert updated is not None
    assert updated.title == "Synthetic revision 2"
    assert updated.source_last_updated == "2026-02-17"


@pytest.mark.asyncio
async def test_interrupted_details_resume_without_refetch_and_preserve_snapshot(
    source_record_factory, study_factory, tmp_path
):
    ids = ["NCT00000001", "NCT00000002", "NCT00000003"]
    detail_calls: Counter[str] = Counter()
    failing = {"NCT00000003"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/studies"):
            return httpx.Response(
                200,
                json={"studies": [_official_record(source_record_factory, nct_id) for nct_id in ids]},
                request=request,
            )
        nct_id = request.url.path.rsplit("/", 1)[-1]
        detail_calls[nct_id] += 1
        if nct_id in failing:
            return httpx.Response(503, json={}, request=request)
        return httpx.Response(
            200,
            json=_official_record(source_record_factory, nct_id),
            request=request,
        )

    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies([study_factory("NCT00000009", title="Previous snapshot")])
    checkpoint = tmp_path / "checkpoint.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(
            http_client=http_client,
            cache_dir=tmp_path / "cache",
            max_retries=0,
            min_request_interval_seconds=0,
        ) as client:
            service = ClinicalIngestionService(client, repository)
            interrupted = await service.ingest(checkpoint_path=checkpoint)
            assert repository.get_study("NCT00000009") is not None
            failing.clear()
            resumed = await service.ingest(checkpoint_path=checkpoint)

    assert interrupted.status == IngestionStatus.PARTIAL
    assert interrupted.index_retrieved_at is not None
    assert interrupted.started_at <= interrupted.index_retrieved_at <= interrupted.completed_at
    assert interrupted.published_at is None
    assert interrupted.publication_completed is False
    assert interrupted.previous_snapshot_preserved is True
    assert interrupted.incomplete_detail_ids == ["NCT00000003"]
    assert resumed.status == IngestionStatus.COMPLETE
    assert resumed.detail_resumed_count == 2
    assert resumed.detail_cache_hit_count == 2
    assert detail_calls == Counter(
        {"NCT00000003": 2, "NCT00000001": 1, "NCT00000002": 1}
    )
    assert repository.get_study("NCT00000009") is None


class _ConcurrencyClient:
    def __init__(self, records: list[dict], cache_dir: Path):
        self.records = records
        self.cache_dir = cache_dir
        self.metrics = RequestMetrics()
        self.active = 0
        self.peak = 0

    async def fetch_all_recruiting_massachusetts(self, page_size: int):
        return FetchResult(self.records, 1, 0)

    async def fetch_detail(self, nct_id: str, **_kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            record = next(
                item
                for item in self.records
                if item["protocolSection"]["identificationModule"]["nctId"] == nct_id
            )
            study = normalize_study(record)
            return DetailResult(record, datetime.now(UTC), False, study.source_hash)
        finally:
            self.active -= 1

    def list_query_url(self, page_size: int):
        return f"https://clinicaltrials.gov/api/v2/studies?pageSize={page_size}"


@pytest.mark.asyncio
async def test_detail_concurrency_is_bounded(source_record_factory, tmp_path):
    records = [
        _official_record(source_record_factory, f"NCT{number:08d}")
        for number in range(1, 7)
    ]
    client = _ConcurrencyClient(records, tmp_path / "cache")
    report = await ClinicalIngestionService(
        client, ClinicalRepository(tmp_path / "clinical.sqlite3")
    ).ingest(max_concurrency=2, checkpoint_path=tmp_path / "checkpoint.json")

    assert report.status == IngestionStatus.COMPLETE
    assert client.peak == 2
    assert report.detail_network_count == 6


@pytest.mark.asyncio
async def test_publication_timestamps_validate_and_update_manifest(
    source_record_factory, tmp_path
):
    record = _official_record(source_record_factory, "NCT00000001")
    report = await ClinicalIngestionService(
        _ConcurrencyClient([record], tmp_path / "cache"),
        ClinicalRepository(tmp_path / "clinical.sqlite3"),
    ).ingest(checkpoint_path=tmp_path / "checkpoint.json")

    assert report.publication_completed is True
    assert report.index_retrieved_at is not None
    assert report.published_at is not None
    assert report.published_at >= report.index_retrieved_at

    payload = report.model_dump()
    invalid_updates = [
        {"index_retrieved_at": None},
        {"published_at": None},
        {"published_at": report.index_retrieved_at - timedelta(microseconds=1)},
        {
            "publication_completed": False,
            "published_record_count": 0,
        },
    ]
    for update in invalid_updates:
        with pytest.raises(ValidationError):
            IngestionReport.model_validate(payload | update)

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "last_updated_utc": None,
                "sources": [{"id": "clinicaltrials-gov-ma-recruiting"}],
            }
        )
    )
    update_manifest(report, manifest_path)
    manifest = json.loads(manifest_path.read_text())
    acquisition = manifest["sources"][0]["acquisition"]
    assert acquisition["index_retrieved_at"] == report.index_retrieved_at.isoformat()
    assert acquisition["published_at"] == report.published_at.isoformat()
    assert acquisition["acquired_at_utc"] == report.index_retrieved_at.isoformat()
    assert manifest["last_updated_utc"] == report.published_at.isoformat()

    partial = IngestionReport.model_validate(
        payload
        | {
            "status": IngestionStatus.PARTIAL,
            "publication_completed": False,
            "published_at": None,
            "published_record_count": 0,
            "previous_snapshot_preserved": True,
        }
    )
    before = manifest_path.read_text()
    update_manifest(partial, manifest_path)
    assert manifest_path.read_text() == before


@pytest.mark.asyncio
async def test_nonpublished_runs_record_index_time_without_publication_time(
    source_record_factory, tmp_path
):
    empty_report = await ClinicalIngestionService(
        _ConcurrencyClient([], tmp_path / "empty-cache"),
        ClinicalRepository(tmp_path / "empty.sqlite3"),
    ).ingest(checkpoint_path=tmp_path / "empty-checkpoint.json")

    assert empty_report.status == IngestionStatus.FAILED
    assert empty_report.index_retrieved_at is not None
    assert empty_report.published_at is None
    assert empty_report.publication_completed is False

    repository = ClinicalRepository(tmp_path / "failed-publication.sqlite3")

    def fail_publication(*_args, **_kwargs):
        raise RuntimeError("synthetic publication failure")

    repository.publish_complete_snapshot = fail_publication
    record = _official_record(source_record_factory, "NCT00000001")
    failed_publication = await ClinicalIngestionService(
        _ConcurrencyClient([record], tmp_path / "failed-publication-cache"),
        repository,
    ).ingest(checkpoint_path=tmp_path / "failed-publication-checkpoint.json")

    assert failed_publication.status == IngestionStatus.PARTIAL
    assert failed_publication.index_retrieved_at is not None
    assert failed_publication.publication_attempted is True
    assert failed_publication.publication_completed is False
    assert failed_publication.published_at is None
    assert any("synthetic publication failure" in error for error in failed_publication.errors)


@pytest.mark.asyncio
async def test_ma_verification_blocks_unverified_record_and_preserves_location_statuses(
    source_record_factory, study_factory, tmp_path
):
    out_of_state = _official_record(source_record_factory, "NCT00000001")
    locations = out_of_state["protocolSection"]["contactsLocationsModule"]["locations"]
    locations[0]["state"] = "New York"
    client = _ConcurrencyClient([out_of_state], tmp_path / "cache")
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies([study_factory("NCT00000009", title="Previous snapshot")])

    failed = await ClinicalIngestionService(client, repository).ingest(
        checkpoint_path=tmp_path / "checkpoint.json"
    )

    assert failed.status == IngestionStatus.PARTIAL
    assert failed.ma_unverified_count == 1
    assert failed.ma_unverified_records[0]["nct_id"] == "NCT00000001"
    assert repository.get_study("NCT00000009") is not None

    verified = _official_record(source_record_factory, "NCT00000001", revision=2)
    verified_locations = verified["protocolSection"]["contactsLocationsModule"]["locations"]
    verified_locations[0]["status"] = "NOT_YET_RECRUITING"
    verified_locations.append(
        {
            "facility": "Synthetic New York Site",
            "status": "RECRUITING",
            "city": "Albany",
            "state": "New York",
            "country": "United States",
        }
    )
    success_client = _ConcurrencyClient([verified], tmp_path / "cache-two")
    success = await ClinicalIngestionService(success_client, repository).ingest(
        checkpoint_path=tmp_path / "checkpoint-two.json"
    )

    assert success.status == IngestionStatus.COMPLETE
    stored = repository.get_study("NCT00000001")
    assert stored is not None
    assert [(item.state, item.recruiting_status) for item in stored.locations] == [
        ("Massachusetts", "NOT_YET_RECRUITING"),
        ("New York", "RECRUITING"),
    ]


@pytest.mark.asyncio
async def test_retry_after_and_shared_pacing_are_honored(source_record_factory, tmp_path):
    calls = 0
    sleeps: list[float] = []
    clock = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, request=request)
        return httpx.Response(200, json={"studies": [source_record_factory()]}, request=request)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(
            http_client=http_client,
            cache_dir=tmp_path,
            max_retries=1,
            min_request_interval_seconds=0.5,
            sleep=sleep,
            monotonic=lambda: clock[0],
        ) as client:
            result = await client.fetch_all_recruiting_massachusetts()

    assert len(result.records) == 1
    assert sleeps == [3]
    assert client.metrics.retry_count == 1
    assert client.metrics.rate_limited_count == 1


def test_manual_refresh_cli_has_safe_full_cohort_options():
    args = parser().parse_args(
        [
            "ingest",
            "--refresh",
            "--max-concurrency",
            "3",
            "--min-request-interval-seconds",
            "0.4",
            "--timeout-seconds",
            "20",
            "--max-retries",
            "2",
        ]
    )

    assert args.refresh_details is True
    assert args.max_concurrency == 3
    assert args.min_request_interval_seconds == 0.4
    assert not hasattr(args, "detail_limit")
