from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from backend.app.clinical.client import ClinicalTrialsClient, PartialFetchError
from backend.app.clinical.database import ClinicalRepository
from backend.app.clinical.normalize import (
    StudyNormalizationError,
    normalize_study,
    source_structured_criteria,
)


@pytest.mark.asyncio
async def test_paginates_and_deduplicates_nct_ids(source_record_factory, tmp_path):
    pages = {
        None: {
            "studies": [source_record_factory("NCT00000001"), source_record_factory("NCT00000002")],
            "nextPageToken": "second",
        },
        "second": {
            "studies": [source_record_factory("NCT00000002"), source_record_factory("NCT00000003")]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert {"MinimumAge", "MaximumAge", "Sex", "BriefSummary", "DetailedDescription"} <= set(
            request.url.params["fields"].split(",")
        )
        return httpx.Response(200, json=pages.get(request.url.params.get("pageToken")), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(
            http_client=http_client, cache_dir=tmp_path, max_retries=0
        ) as client:
            result = await client.fetch_all_recruiting_massachusetts(page_size=2)

    assert result.page_count == 2
    assert result.duplicate_identifier_count == 1
    assert [
        item["protocolSection"]["identificationModule"]["nctId"] for item in result.records
    ] == ["NCT00000001", "NCT00000002", "NCT00000003"]


@pytest.mark.asyncio
async def test_repeated_page_token_is_rejected(source_record_factory, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"studies": [source_record_factory()], "nextPageToken": "same"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(
            http_client=http_client, cache_dir=tmp_path, max_retries=0
        ) as client:
            with pytest.raises(PartialFetchError, match="repeated page token"):
                await client.fetch_all_recruiting_massachusetts(page_size=20)


@pytest.mark.asyncio
async def test_transient_source_error_retries(source_record_factory, tmp_path):
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={}, request=request)
        return httpx.Response(200, json={"studies": [source_record_factory()]}, request=request)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(
            http_client=http_client,
            cache_dir=tmp_path,
            max_retries=1,
            min_request_interval_seconds=0,
            sleep=sleep,
        ) as client:
            result = await client.fetch_all_recruiting_massachusetts()

    assert len(result.records) == 1
    assert calls == 2
    assert sleeps == [1]


@pytest.mark.asyncio
async def test_detail_is_cached_atomically(source_record_factory, tmp_path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=source_record_factory(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with ClinicalTrialsClient(http_client=http_client, cache_dir=tmp_path) as client:
            first = await client.fetch_detail("NCT00000001")
            second = await client.fetch_detail("NCT00000001")

    assert first.from_cache is False
    assert second.from_cache is True
    assert calls == 1
    payload = json.loads((tmp_path / "NCT00000001.json").read_text())
    assert payload["study"]["protocolSection"]["identificationModule"]["nctId"] == "NCT00000001"
    assert not list(tmp_path.glob("*.tmp"))


def test_normalization_preserves_source_fields_and_evidence(source_record_factory):
    record = source_record_factory(detailed_description="Full official technical description.")
    record["protocolSection"]["identificationModule"]["officialTitle"] = (
        "Full Official Synthetic Study Title"
    )
    study = normalize_study(record)
    assert study.nct_id == "NCT00000001"
    assert study.title == "Full Official Synthetic Study Title"
    assert study.source_description == "This official source text describes the synthetic study purpose."
    assert study.brief_summary == "This official source text describes the synthetic study purpose."
    assert study.detailed_description == "Full official technical description."
    assert study.eligibility_criteria == "Participants must be 18 years or older."
    assert study.locations[0].facility == "Synthetic Facility"
    assert study.locations[0].recruiting_status == "RECRUITING"
    assert study.contacts[0].email == "test@example.invalid"
    assert study.source_url.endswith(study.nct_id)
    assert study.raw_source["protocolSection"]["eligibilityModule"]["minimumAge"] == "18 Years"


def test_not_applicable_phase_is_canonicalized_without_changing_raw_source(source_record_factory):
    record = source_record_factory(phases=["NA"])
    study = normalize_study(record)
    assert study.phases == ["NOT_APPLICABLE"]
    assert study.raw_source["protocolSection"]["designModule"]["phases"] == ["NA"]


def test_missing_required_identity_field_is_rejected(source_record_factory):
    record = source_record_factory()
    del record["protocolSection"]["identificationModule"]["briefTitle"]
    with pytest.raises(StudyNormalizationError, match="BriefTitle is missing"):
        normalize_study(record)


def test_missing_optional_api_fields_are_preserved(source_record_factory):
    record = source_record_factory(eligibility=None, minimum_age=None, maximum_age=None, sex=None)
    del record["protocolSection"]["conditionsModule"]
    del record["protocolSection"]["designModule"]
    del record["protocolSection"]["contactsLocationsModule"]
    study = normalize_study(record)
    assert study.conditions == []
    assert study.phases == []
    assert study.eligibility_criteria is None
    assert study.locations == []
    assert study.contacts == []


def test_repository_search_is_deterministic(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies(
        [
            study_factory("NCT00000002", title="Beta Cancer Study", conditions=["Cancer"]),
            study_factory("NCT00000001", title="Alpha Study", conditions=["Cardiology"]),
        ]
    )
    studies, total = repository.search_studies(query="study", page=1, page_size=1)
    assert total == 2
    assert [study.nct_id for study in studies] == ["NCT00000001"]
    studies, total = repository.search_studies(condition="cancer")
    assert total == 1
    assert studies[0].nct_id == "NCT00000002"


def test_repository_age_and_phase_filters_handle_open_bounds(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies(
        [
            study_factory("NCT00000001", minimum_age="18 Years", maximum_age=None, phases=["NA"]),
            study_factory("NCT00000002", minimum_age=None, maximum_age="12 Years"),
            study_factory("NCT00000003", minimum_age=None, maximum_age=None),
        ]
    )
    adult, adult_total = repository.search_studies(age_years=40)
    child, child_total = repository.search_studies(age_years=10)
    not_applicable, phase_total = repository.search_studies(phase="NA")
    assert adult_total == 1 and [study.nct_id for study in adult] == ["NCT00000001"]
    assert child_total == 1 and [study.nct_id for study in child] == ["NCT00000002"]
    assert phase_total == 1 and not_applicable[0].phases == ["NOT_APPLICABLE"]


def test_list_snapshot_does_not_overwrite_expanded_detail_or_criteria(
    source_record_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    detailed = normalize_study(source_record_factory())
    repository.upsert_studies([detailed])
    repository.replace_criteria(
        detailed.nct_id, "source_structured", source_structured_criteria(detailed)
    )

    indexed_record = source_record_factory(title="Updated list-only title")
    indexed_record["protocolSection"].pop("eligibilityModule")
    indexed_record["protocolSection"].pop("contactsLocationsModule")
    indexed_record["protocolSection"].pop("descriptionModule")
    indexed = normalize_study(indexed_record)
    repository.publish_complete_snapshot([indexed], {indexed.nct_id: []})

    preserved = repository.get_study(detailed.nct_id)
    assert preserved is not None
    assert preserved.title == detailed.title
    assert preserved.minimum_age == "18 Years"
    assert preserved.locations[0].facility == "Synthetic Facility"
    assert repository.get_criteria(
        detailed.nct_id, source_hash=preserved.source_hash
    )


def test_repository_repairs_missing_description_columns_from_preserved_source(
    source_record_factory, tmp_path
):
    path = tmp_path / "clinical.sqlite3"
    repository = ClinicalRepository(path)
    study = normalize_study(
        source_record_factory(detailed_description="Full official technical description.")
    )
    repository.upsert_studies([study])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE studies SET source_description = NULL, brief_summary = NULL, detailed_description = NULL"
        )
        connection.commit()

    repository.initialize()
    repaired = repository.get_study(study.nct_id)

    assert repaired is not None
    assert repaired.source_description == study.brief_summary
    assert repaired.brief_summary == study.brief_summary
    assert repaired.detailed_description == "Full official technical description."
