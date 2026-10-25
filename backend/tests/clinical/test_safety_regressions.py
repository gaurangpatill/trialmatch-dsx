from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.clinical.client import (
    ClinicalTrialsClient,
    DetailResult,
    FetchResult,
    InvalidSourcePayloadError,
    PartialFetchError,
)
from backend.app.clinical.database import ClinicalRepository
from backend.app.clinical.dependencies import get_repository
from backend.app.clinical.evaluator import evaluate_criterion
from backend.app.clinical.extraction import OpenAICompatibleProvider, ProviderUnavailable
from backend.app.clinical.ingestion import ClinicalIngestionService
from backend.app.clinical.normalize import normalize_study, source_structured_criteria
from backend.app.clinical.matching import MatchingService, find_contradictions
from backend.app.clinical.models import (
    CriterionAttribute,
    CriterionOperator,
    CriterionStatus,
    Predicate,
    ScreeningStatus,
    SyntheticPatient,
)
from backend.app.main import app


def test_complete_snapshot_removes_stale_studies_atomically(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    first = study_factory("NCT00000001")
    stale = study_factory("NCT00000002")
    repository.upsert_studies([first, stale])

    repository.replace_complete_snapshot([first])
    studies, total = repository.search_studies()

    assert total == 1
    assert [study.nct_id for study in studies] == [first.nct_id]

    with pytest.raises(sqlite3.IntegrityError):
        repository.replace_complete_snapshot([first, first])
    _, total_after_failed_transaction = repository.search_studies()
    assert total_after_failed_transaction == 1


def test_stale_criteria_are_not_evaluated_after_source_change(
    study_factory, criterion_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    original = study_factory(title="Original source version")
    repository.upsert_studies([original])
    repository.replace_criteria(original.nct_id, "test_fixture", [criterion_factory()])
    updated = study_factory(title="Updated source version")
    repository.upsert_studies([updated])

    result = MatchingService(repository).match(
        SyntheticPatient(profile_id="synthetic", synthetic=True, age_years=40)
    ).results[0]

    assert result.screening_status == ScreeningStatus.UNRESOLVED
    assert result.criteria == []
    assert any("No current validated" in item for item in result.unresolved_requirements)


@pytest.mark.asyncio
async def test_mismatched_cached_detail_is_removed_and_refetched(source_record_factory, tmp_path):
    cache_path = tmp_path / "NCT00000001.json"
    cache_path.write_text(
        json.dumps(
            {
                "retrieved_at": datetime(2026, 10, 24, 17, 0, tzinfo=UTC).isoformat(),
                "study": source_record_factory("NCT00000002"),
            }
        )
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=source_record_factory("NCT00000001"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(http_client=http_client, cache_dir=tmp_path) as client:
            result = await client.fetch_detail("NCT00000001")

    assert result.from_cache is False
    assert calls == 1
    cached = json.loads(cache_path.read_text())
    assert cached["study"]["protocolSection"]["identificationModule"]["nctId"] == "NCT00000001"


@pytest.mark.asyncio
async def test_network_detail_identity_is_validated_before_cache(source_record_factory, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=source_record_factory("NCT00000002"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(http_client=http_client, cache_dir=tmp_path) as client:
            with pytest.raises(InvalidSourcePayloadError, match="identifier mismatch"):
                await client.fetch_detail("NCT00000001")
    assert not (tmp_path / "NCT00000001.json").exists()


@pytest.mark.asyncio
async def test_conflicting_duplicate_source_record_marks_fetch_partial(
    source_record_factory, tmp_path
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("pageToken") is None:
            return httpx.Response(
                200,
                json={"studies": [source_record_factory()], "nextPageToken": "next"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"studies": [source_record_factory(title="Conflicting title")]},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(http_client=http_client, cache_dir=tmp_path) as client:
            with pytest.raises(PartialFetchError, match="conflicting duplicate") as raised:
                await client.fetch_all_recruiting_massachusetts()
    assert raised.value.duplicate_identifier_count == 1
    assert len(raised.value.records) == 1


class PartialClient:
    def __init__(self, record):
        self.record = record

    async def fetch_all_recruiting_massachusetts(self, page_size):
        raise PartialFetchError("source interrupted", [self.record], 1, 0)

    def list_query_url(self, page_size):
        return f"https://clinicaltrials.gov/api/v2/studies?pageSize={page_size}"


class CompleteClient:
    def __init__(self, record):
        self.record = record

    async def fetch_all_recruiting_massachusetts(self, page_size):
        return FetchResult([self.record], 1, 0)

    async def fetch_detail(self, nct_id, **_kwargs):
        study = normalize_study(self.record)
        assert nct_id == study.nct_id
        return DetailResult(
            record=self.record,
            retrieved_at=study.retrieved_at,
            from_cache=False,
            detail_hash=study.source_hash,
        )

    def list_query_url(self, page_size):
        return f"https://clinicaltrials.gov/api/v2/studies?pageSize={page_size}"


@pytest.mark.asyncio
async def test_partial_refresh_preserves_previous_snapshot_and_records_runtime_query(
    source_record_factory, study_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies([study_factory("NCT00000002")])
    report = await ClinicalIngestionService(
        PartialClient(source_record_factory("NCT00000001")), repository
    ).ingest(page_size=500)

    studies, total = repository.search_studies()
    assert total == 1
    assert studies[0].nct_id == "NCT00000002"
    assert report.pagination_completed is False
    assert report.index_retrieved_at is not None
    assert report.started_at <= report.index_retrieved_at <= report.completed_at
    assert report.published_at is None
    assert report.publication_completed is False
    assert report.effective_page_size == 500
    assert report.source_url.endswith("pageSize=500")


@pytest.mark.asyncio
async def test_complete_refresh_clears_removed_source_structured_criteria(
    source_record_factory, study_factory, criterion_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    old = study_factory()
    repository.upsert_studies([old])
    repository.replace_criteria(
        old.nct_id, "source_structured", source_structured_criteria(old)
    )
    record_without_criteria = source_record_factory(
        eligibility=None, minimum_age=None, maximum_age=None, sex=None
    )

    report = await ClinicalIngestionService(
        CompleteClient(record_without_criteria), repository
    ).ingest(checkpoint_path=tmp_path / "checkpoint.json")

    assert report.pagination_completed is True
    assert repository.get_criteria(old.nct_id) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"attribute": "age_years", "operator": "present"},
        {
            "attribute": "condition",
            "operator": "greater_than",
            "target": "asthma",
            "value": 3,
        },
        {
            "attribute": "laboratory_value",
            "operator": "less_than",
            "target": "ALT",
            "value": 10,
        },
        {
            "attribute": "unsupported",
            "operator": "all",
            "value": 1,
            "children": [
                {"attribute": "age_years", "operator": "greater_than", "value": 18},
                {"attribute": "age_years", "operator": "less_than", "value": 65},
            ],
        },
    ],
)
def test_unsupported_predicate_shapes_are_rejected(payload):
    with pytest.raises(ValidationError):
        Predicate.model_validate(payload)


def test_unit_sensitive_laboratory_criterion_remains_unknown(criterion_factory):
    criterion = criterion_factory(
        attribute=CriterionAttribute.LABORATORY_VALUE,
        operator=CriterionOperator.LESS_THAN,
        target="ALT",
        value=10,
        unit="U/L",
    )
    patient = SyntheticPatient(
        profile_id="synthetic", synthetic=True, laboratory_values={"ALT": 5}
    )
    result = evaluate_criterion(criterion, patient)
    assert result.status == CriterionStatus.UNKNOWN
    assert "unit" in result.explanation


def test_contradictory_ranges_and_fact_polarity_are_surfaced(criterion_factory):
    criteria = [
        criterion_factory(criterion_id="older", value=65, text="Age at least 65."),
        criterion_factory(
            criterion_id="younger",
            operator=CriterionOperator.LESS_THAN_OR_EQUAL,
            value=40,
            text="Age at most 40.",
        ),
        criterion_factory(
            criterion_id="present",
            attribute=CriterionAttribute.CONDITION,
            operator=CriterionOperator.PRESENT,
            target="asthma",
            value=None,
            text="Asthma required.",
        ),
        criterion_factory(
            criterion_id="absent",
            attribute=CriterionAttribute.CONDITION,
            operator=CriterionOperator.ABSENT,
            target="asthma",
            value=None,
            text="Asthma must be absent.",
        ),
    ]
    warnings = find_contradictions(criteria)
    assert len(warnings) == 2


def test_patient_present_and_absent_fact_conflict_is_rejected():
    with pytest.raises(ValidationError, match="both present and absent"):
        SyntheticPatient(
            profile_id="synthetic",
            synthetic=True,
            conditions=["asthma"],
            absent_conditions=["Asthma"],
        )


def test_remote_llm_provider_requires_https():
    with pytest.raises(ProviderUnavailable, match="must use HTTPS"):
        OpenAICompatibleProvider(
            model="model", api_key="secret", base_url="http://models.example.com/v1"
        )
    provider = OpenAICompatibleProvider(
        model="model", api_key="secret", base_url="http://127.0.0.1:1234/v1"
    )
    assert provider.base_url.startswith("http://127.0.0.1")


def test_synthetic_declaration_is_not_coerced(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies([study_factory()])
    app.dependency_overrides[get_repository] = lambda: repository
    with TestClient(app) as client:
        response = client.post(
            "/api/match",
            json={"patient": {"profile_id": "synthetic", "synthetic": "yes"}},
        )
    app.dependency_overrides.clear()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_requested_trial_returns_404_contract_error(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies([study_factory("NCT00000001")])
    app.dependency_overrides[get_repository] = lambda: repository
    with TestClient(app) as client:
        response = client.post(
            "/api/match",
            json={
                "patient": {"profile_id": "synthetic", "synthetic": True},
                "trial_ids": ["NCT99999999"],
            },
        )
    app.dependency_overrides.clear()
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRIALS_NOT_FOUND"


@pytest.mark.asyncio
async def test_unusable_identity_correct_detail_is_not_cached(source_record_factory, tmp_path):
    invalid = source_record_factory()
    del invalid["protocolSection"]["identificationModule"]["briefTitle"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=invalid, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(http_client=http_client, cache_dir=tmp_path) as client:
            with pytest.raises(InvalidSourcePayloadError, match="BriefTitle"):
                await client.fetch_detail("NCT00000001")
    assert not (tmp_path / "NCT00000001.json").exists()


@pytest.mark.asyncio
async def test_exclusion_bearing_refresh_is_partial_and_does_not_publish(
    source_record_factory, study_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    previous = study_factory("NCT00000002", title="Previously published")
    repository.upsert_studies([previous])
    invalid = source_record_factory("NCT00000003")
    del invalid["protocolSection"]["identificationModule"]["briefTitle"]
    client = CompleteClient(source_record_factory("NCT00000001"))

    async def fetch_with_invalid(page_size):
        return FetchResult([client.record, invalid], 1, 0)

    client.fetch_all_recruiting_massachusetts = fetch_with_invalid
    report = await ClinicalIngestionService(client, repository).ingest(
        checkpoint_path=tmp_path / "checkpoint.json"
    )

    studies, total = repository.search_studies()
    assert report.status.value == "partial"
    assert len(report.excluded_records) == 1
    assert total == 1
    assert studies[0].title == "Previously published"


@pytest.mark.parametrize(
    "days",
    [float("nan"), float("inf"), float("-inf"), -1],
)
def test_dated_facts_require_finite_non_negative_values(days):
    with pytest.raises(ValidationError, match="finite and non-negative"):
        SyntheticPatient(
            profile_id="synthetic",
            synthetic=True,
            procedures=["surgery"],
            procedure_days_ago={"surgery": days},
        )


def test_dated_fact_keys_are_case_insensitively_unique_and_explicitly_present():
    with pytest.raises(ValidationError, match="unique ignoring case"):
        SyntheticPatient(
            profile_id="synthetic",
            synthetic=True,
            procedures=["surgery"],
            procedure_days_ago={"Surgery": 10, "surgery": 40},
        )
    with pytest.raises(ValidationError, match="also be explicitly present"):
        SyntheticPatient(
            profile_id="synthetic",
            synthetic=True,
            procedure_days_ago={"surgery": 10},
        )


def test_predicate_rejects_explicit_irrelevant_inclusivity_field():
    with pytest.raises(ValidationError, match="allowed only for between"):
        Predicate.model_validate(
            {
                "attribute": "age_years",
                "operator": "greater_than",
                "value": 18,
                "include_upper": False,
            }
        )


def test_equality_and_inequality_contradictions_are_surfaced(criterion_factory):
    age_equal = criterion_factory(
        criterion_id="age-equal",
        operator=CriterionOperator.EQUALS,
        value=18,
        text="Age equals 18.",
    )
    age_not_equal = criterion_factory(
        criterion_id="age-not-equal",
        operator=CriterionOperator.NOT_EQUALS,
        value=18,
        text="Age does not equal 18.",
    )
    assert len(find_contradictions([age_equal, age_not_equal])) == 1


@pytest.mark.asyncio
async def test_whitespace_status_is_rejected_before_detail_cache(source_record_factory, tmp_path):
    invalid = source_record_factory(status="   ")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=invalid, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(http_client=http_client, cache_dir=tmp_path) as client:
            with pytest.raises(InvalidSourcePayloadError, match="OverallStatus"):
                await client.fetch_detail("NCT00000001")
    assert not (tmp_path / "NCT00000001.json").exists()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_extracted_predicate_numbers_are_rejected(value):
    with pytest.raises(ValidationError, match="must be finite"):
        Predicate(
            attribute=CriterionAttribute.AGE_YEARS,
            operator=CriterionOperator.GREATER_THAN,
            value=value,
        )
    with pytest.raises(ValidationError, match="must be finite"):
        Predicate(
            attribute=CriterionAttribute.AGE_YEARS,
            operator=CriterionOperator.BETWEEN,
            lower=18,
            upper=value,
        )


def test_complete_snapshot_and_source_criteria_publish_atomically(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    previous = study_factory("NCT00000003", title="Previous serving snapshot")
    repository.upsert_studies([previous])
    first = study_factory("NCT00000001")
    second = study_factory("NCT00000002")
    first_criterion = source_structured_criteria(first)[0]
    conflicting_second = source_structured_criteria(second)[0].model_copy(
        update={"criterion_id": first_criterion.criterion_id}
    )

    with pytest.raises(sqlite3.IntegrityError):
        repository.publish_complete_snapshot(
            [first, second],
            {
                first.nct_id: [first_criterion],
                second.nct_id: [conflicting_second],
            },
        )

    studies, total = repository.search_studies()
    assert total == 1
    assert studies[0].nct_id == previous.nct_id
