from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from backend.app.clinical.cli import run_summarize
from backend.app.clinical.database import ClinicalRepository
from backend.app.clinical.models import DetailRetrievalStatus
from backend.app.clinical.normalize import normalize_study
from backend.app.clinical.summaries import (
    DEFAULT_OPENAI_MODEL,
    DisabledSummaryProvider,
    OnDemandStudySummaryService,
    OpenAISummaryProvider,
    StudySummaryService,
    SummaryError,
    _unsupported_material_words,
    summary_provider_from_environment,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "summary_source_records.json"
_DESCRIPTION_PATH = "protocolSection.descriptionModule.briefSummary"
_DETAILED_PATH = "protocolSection.descriptionModule.detailedDescription"
_ELIGIBILITY_PATH = "protocolSection.eligibilityModule.eligibilityCriteria"
_POPULATION_PATH = "protocolSection.eligibilityModule.studyPopulation"
_STATUS_PATH = "protocolSection.statusModule.overallStatus"
_INTERVENTION_PATH = "protocolSection.armsInterventionsModule.interventions[0].description"


def _studies():
    records = json.loads(_FIXTURES.read_text())
    return [
        normalize_study(record, datetime(2026, 10, 25, 12, 0, tzinfo=UTC))
        for record in records
    ]


def _statement(text: str, path: str, quote: str) -> dict:
    return {"text": text, "evidence": [{"source_path": path, "quote": quote}]}


def _grounded_payload(study) -> dict:
    empty = {"statements": []}
    if study.nct_id == "NCT90000001":
        return {
            "nct_id": study.nct_id,
            "source_hash": study.source_hash,
            "investigating": {
                "statements": [
                    _statement(
                        "Researchers are comparing Therapy A with standard chemotherapy for advanced non-small cell lung cancer.",
                        _DESCRIPTION_PATH,
                        "compares investigational Therapy A with standard chemotherapy in adults with advanced non-small cell lung cancer",
                    )
                ]
            },
            "participation": {
                "statements": [
                    _statement(
                        "The record says Therapy A is given by IV every 3 weeks for as many as 6 cycles.",
                        _INTERVENTION_PATH,
                        "Therapy A is given by intravenous infusion every 3 weeks for up to 6 cycles.",
                    )
                ]
            },
            "intended_for": {
                "statements": [
                    _statement(
                        "The study is intended for adults with confirmed advanced lung cancer and measurable disease; other criteria also apply.",
                        _ELIGIBILITY_PATH,
                        "Adults with confirmed advanced non-small cell lung cancer and measurable disease may be considered. Additional criteria apply.",
                    )
                ]
            },
            "important": {
                "statements": [
                    _statement(
                        "ClinicalTrials.gov lists the study as recruiting.",
                        _STATUS_PATH,
                        "RECRUITING",
                    )
                ]
            },
        }
    if study.nct_id == "NCT90000002":
        return {
            "nct_id": study.nct_id,
            "source_hash": study.source_hash,
            "investigating": {
                "statements": [
                    _statement(
                        "Researchers are looking at how activity and blood pressure are connected with heart health over time.",
                        _DESCRIPTION_PATH,
                        "examines how daily activity and blood pressure relate to heart health over time",
                    )
                ]
            },
            "participation": {
                "statements": [
                    _statement(
                        "Participants answer health questions, wear an activity monitor for 7 days, and have a blood-pressure check; the study does not assign treatment.",
                        _DETAILED_PATH,
                        "Participants complete health questionnaires, wear an activity monitor for 7 days, and have blood pressure measured at a study visit. No treatment is assigned by the study.",
                    )
                ]
            },
            "intended_for": {
                "statements": [
                    _statement(
                        "The study population is adults who previously visited participating primary-care clinics.",
                        _POPULATION_PATH,
                        "Adults previously seen in participating primary care clinics.",
                    )
                ]
            },
            "important": {
                "statements": [
                    _statement(
                        "ClinicalTrials.gov says enrollment is by invitation.",
                        _STATUS_PATH,
                        "ENROLLING_BY_INVITATION",
                    )
                ]
            },
        }
    return {
        "nct_id": study.nct_id,
        "source_hash": study.source_hash,
        "investigating": empty,
        "participation": empty,
        "intended_for": empty,
        "important": {
            "statements": [
                _statement(
                    "ClinicalTrials.gov lists the study as not yet recruiting.",
                    _STATUS_PATH,
                    "NOT_YET_RECRUITING",
                )
            ]
        },
    }


class FixtureProvider:
    name = "test-provider"
    model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, study):
        self.calls += 1
        return _grounded_payload(study)


@pytest.mark.asyncio
@pytest.mark.parametrize("study_index", [0, 1, 2])
async def test_representative_summaries_are_grounded_structured_and_cached(
    study_index, tmp_path
):
    repository = ClinicalRepository(tmp_path / f"clinical-{study_index}.sqlite3")
    study = _studies()[study_index]
    repository.upsert_studies([study])
    provider = FixtureProvider()
    service = StudySummaryService(repository, provider)

    first, first_cached = await service.summarize(study)
    second, second_cached = await service.summarize(study)

    assert first == second
    assert first_cached is False
    assert second_cached is True
    assert provider.calls == 1
    assert first.nct_id == study.nct_id
    assert first.source_hash == study.source_hash
    for heading in (
        "What is this study investigating?",
        "What might participation involve?",
        "Who is the study intended for?",
        "Important things to know.",
    ):
        assert heading in first.plain_language_summary
    assert repository.get_current_summary(study.nct_id, study.source_hash) == (
        first.plain_language_summary
    )
    if study_index == 2:
        assert first.plain_language_summary.count("does not") == 3
        assert "not yet recruiting" in first.plain_language_summary


@pytest.mark.asyncio
async def test_summary_rejects_false_evidence_and_unsupported_claims(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[0]
    repository.upsert_studies([study])

    class BadProvider(FixtureProvider):
        async def summarize(self, current):
            payload = _grounded_payload(current)
            payload["investigating"]["statements"][0]["evidence"][0]["quote"] = (
                "The treatment cures everyone"
            )
            return payload

    with pytest.raises(SummaryError, match="evidence absent"):
        await StudySummaryService(repository, BadProvider()).summarize(study)

    class AdviceProvider(FixtureProvider):
        async def summarize(self, current):
            payload = _grounded_payload(current)
            payload["investigating"]["statements"][0]["text"] = (
                "You are eligible and should enroll."
            )
            return payload

    with pytest.raises(SummaryError, match="unsupported eligibility"):
        await StudySummaryService(repository, AdviceProvider()).summarize(study)
    assert repository.get_current_summary(study.nct_id, study.source_hash) is None


@pytest.mark.asyncio
async def test_summary_rejects_numbers_not_present_in_cited_evidence(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[1]
    repository.upsert_studies([study])

    class InventedDurationProvider(FixtureProvider):
        async def summarize(self, current):
            payload = _grounded_payload(current)
            statement = payload["participation"]["statements"][0]
            statement["text"] = "Participants stay in the study for 30 days."
            return payload

    with pytest.raises(SummaryError, match="number absent"):
        await StudySummaryService(repository, InventedDurationProvider()).summarize(study)


@pytest.mark.asyncio
async def test_openai_adapter_parses_structured_output_and_retries_transient_failure():
    study = _studies()[0]
    calls = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer test-secret"
        body = json.loads(request.content)
        assert body["model"] == DEFAULT_OPENAI_MODEL
        assert body["response_format"]["type"] == "json_schema"
        schema = body["response_format"]["json_schema"]["schema"]
        statement = schema["properties"]["investigating"]["properties"]["statements"]["items"]
        assert statement["properties"]["text"]["maxLength"] == 180
        assert statement["properties"]["evidence"]["items"]["properties"]["quote"]["maxLength"] == 500
        assert statement["properties"]["text"]["description"].endswith("1-180 characters.")
        assert "1-500 characters" in statement["properties"]["evidence"]["items"]["properties"]["quote"]["description"]
        assert "supplied_source_fields" in json.loads(body["messages"][1]["content"])
        if calls == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(_grounded_payload(study))},
                    }
                ],
                "usage": {"prompt_tokens": 300, "completion_tokens": 180, "total_tokens": 480},
            },
        )

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAISummaryProvider(
            api_key="test-secret", http_client=client, sleep=fake_sleep
        )
        result = await provider.summarize(study)

    assert result == _grounded_payload(study)
    assert calls == 2
    assert delays == [1]
    assert provider.last_usage["total_tokens"] == 480


@pytest.mark.asyncio
async def test_openai_adapter_has_bounded_sanitized_failure():
    study = _studies()[0]
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request, text="test-secret sensitive body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAISummaryProvider(
            api_key="test-secret",
            http_client=client,
            max_retries=1,
            sleep=lambda _: asyncio.sleep(0),
        )
        with pytest.raises(SummaryError) as error:
            await provider.summarize(study)

    assert calls == 2
    assert "test-secret" not in str(error.value)
    assert "sensitive body" not in str(error.value)


def test_provider_defaults_to_disabled_and_openai_uses_gpt_4_1_mini(monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert isinstance(summary_provider_from_environment(), DisabledSummaryProvider)

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "server-side-test-key")
    provider = summary_provider_from_environment()
    assert isinstance(provider, OpenAISummaryProvider)
    assert provider.model == "gpt-4.1-mini"
    assert provider.base_url == "https://api.openai.com/v1"


@pytest.mark.asyncio
async def test_disabled_provider_fails_gracefully_without_publishing(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[0]
    repository.upsert_studies([study])

    with pytest.raises(SummaryError, match="provider is disabled"):
        await StudySummaryService(repository, DisabledSummaryProvider()).summarize(study)
    assert repository.get_current_summary(study.nct_id, study.source_hash) is None


@pytest.mark.asyncio
async def test_on_demand_generation_supports_unknown_nct_and_deduplicates(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[1].model_copy(
        update={"detail_status": DetailRetrievalStatus.RETRIEVED}
    )
    provider = FixtureProvider()
    loader_calls = 0

    async def loader(nct_id: str):
        nonlocal loader_calls
        loader_calls += 1
        await asyncio.sleep(0)
        assert nct_id == study.nct_id
        return study

    service = OnDemandStudySummaryService(repository, provider, loader)
    results = await asyncio.gather(
        service.summarize_nct_id(study.nct_id),
        service.summarize_nct_id(study.nct_id),
        service.summarize_nct_id(study.nct_id),
    )

    assert loader_calls == 1
    assert provider.calls == 1
    assert len({result[0].plain_language_summary for result in results}) == 1
    assert repository.get_study(study.nct_id) is None

    cached, from_cache = await service.summarize_nct_id(study.nct_id)
    assert from_cache is True
    assert cached.source_hash == study.source_hash
    assert loader_calls == 2
    assert provider.calls == 1

    with pytest.raises(SummaryError, match="invalid NCT"):
        await service.summarize_nct_id("not-an-id")


@pytest.mark.asyncio
async def test_generation_request_limit_is_enforced(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    first, second = _studies()[:2]
    repository.upsert_studies([first, second])
    service = StudySummaryService(
        repository, FixtureProvider(), max_generation_requests=1
    )

    await service.summarize(first)
    with pytest.raises(SummaryError, match="request limit"):
        await service.summarize(second)


@pytest.mark.asyncio
async def test_summary_cli_refuses_more_than_three_or_duplicate_ids(tmp_path, capsys):
    too_many = Namespace(
        database=str(tmp_path / "unused.sqlite3"),
        nct_ids=["NCT00000001", "NCT00000002", "NCT00000003", "NCT00000004"],
    )
    duplicate = Namespace(
        database=str(tmp_path / "unused.sqlite3"),
        nct_ids=["NCT00000001", "NCT00000001"],
    )

    assert await run_summarize(too_many) == 2
    assert await run_summarize(duplicate) == 2
    output = capsys.readouterr().out
    assert "one to three" in output
    assert "unique" in output


@pytest.mark.asyncio
async def test_valid_but_unrelated_evidence_cannot_support_an_invented_claim(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[0]
    repository.upsert_studies([study])

    class UnrelatedEvidenceProvider(FixtureProvider):
        async def summarize(self, current):
            payload = _grounded_payload(current)
            payload["investigating"]["statements"][0]["text"] = (
                "Therapy A causes severe liver damage."
            )
            return payload

    with pytest.raises(SummaryError, match="material words absent"):
        await StudySummaryService(repository, UnrelatedEvidenceProvider()).summarize(study)


@pytest.mark.asyncio
async def test_wrong_section_evidence_and_unjustified_missing_section_are_rejected(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[0]
    repository.upsert_studies([study])

    class WrongSectionProvider(FixtureProvider):
        async def summarize(self, current):
            payload = _grounded_payload(current)
            statement = payload["intended_for"]["statements"][0]
            statement["evidence"] = [
                {"source_path": _STATUS_PATH, "quote": "RECRUITING"}
            ]
            return payload

    with pytest.raises(SummaryError, match="wrong section"):
        await StudySummaryService(repository, WrongSectionProvider()).summarize(study)

    class MissingAvailableFactsProvider(FixtureProvider):
        async def summarize(self, current):
            payload = _grounded_payload(current)
            payload["participation"] = {"statements": []}
            return payload

    with pytest.raises(SummaryError, match="omitted available"):
        await StudySummaryService(repository, MissingAvailableFactsProvider()).summarize(study)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_start_duplicate_paid_generation(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[0]
    repository.upsert_studies([study])
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowProvider(FixtureProvider):
        async def summarize(self, current):
            self.calls += 1
            started.set()
            await release.wait()
            return _grounded_payload(current)

    provider = SlowProvider()
    service = StudySummaryService(repository, provider)
    cancelled_waiter = asyncio.create_task(service.summarize(study))
    await started.wait()
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    remaining_waiter = asyncio.create_task(service.summarize(study))
    await asyncio.sleep(0)
    assert provider.calls == 1
    release.set()
    payload, _ = await remaining_waiter
    assert payload.nct_id == study.nct_id
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_disabled_on_demand_mode_performs_no_source_or_database_work(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    loader_calls = 0

    async def loader(_nct_id: str):
        nonlocal loader_calls
        loader_calls += 1
        return _studies()[0]

    service = OnDemandStudySummaryService(
        repository, DisabledSummaryProvider(), loader
    )
    with pytest.raises(SummaryError, match="disabled"):
        await service.summarize_nct_id("NCT90000001")
    assert loader_calls == 0
    assert repository.get_studies() == []


def test_invalid_provider_environment_is_safely_normalized(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "server-side-test-key")
    monkeypatch.setenv("LLM_MAX_RETRIES", "not-a-number")

    with pytest.raises(SummaryError, match="must be numeric"):
        summary_provider_from_environment()


@pytest.mark.asyncio
async def test_provider_http_attempt_budget_includes_retries():
    study = _studies()[0]
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAISummaryProvider(
            api_key="test-secret",
            http_client=client,
            max_retries=2,
            max_http_requests=1,
            sleep=lambda _: asyncio.sleep(0),
        )
        with pytest.raises(SummaryError, match="HTTP request limit"):
            await provider.summarize(study)
    assert calls == 1


@pytest.mark.asyncio
async def test_on_demand_uses_complete_local_official_record_without_refetch(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[0].model_copy(
        update={"detail_status": DetailRetrievalStatus.RETRIEVED}
    )
    repository.upsert_studies([study])
    provider = FixtureProvider()

    async def unexpected_loader(_nct_id: str):
        raise AssertionError("complete local records must not be fetched again")

    service = OnDemandStudySummaryService(repository, provider, unexpected_loader)
    first, first_cached = await service.summarize_nct_id(study.nct_id)
    second, second_cached = await service.summarize_nct_id(study.nct_id)

    assert first == second
    assert first_cached is False
    assert second_cached is True
    assert provider.calls == 1
    assert repository.get_current_summary(study.nct_id, study.source_hash) == first.plain_language_summary


def test_grounding_accepts_track_only_for_official_collection_or_measurement_wording():
    assert _unsupported_material_words(
        "The study tracks arm measurements.",
        "collecting objective arm measurements in a maintained database",
    ) == set()
    unsupported = _unsupported_material_words(
        "The study tracks treatment benefits.",
        "collecting objective measurements",
    )
    assert "tracks" not in unsupported
    assert "benefits" in unsupported


@pytest.mark.asyncio
async def test_on_demand_rereads_catalog_after_concurrent_detail_persistence(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    complete = _studies()[0].model_copy(
        update={"detail_status": DetailRetrievalStatus.RETRIEVED}
    )
    indexed = complete.model_copy(
        update={
            "source_hash": "0" * 64,
            "detail_status": DetailRetrievalStatus.NOT_RETRIEVED,
        }
    )
    repository.upsert_studies([indexed])
    provider = FixtureProvider()

    async def loader(_nct_id: str):
        repository.upsert_studies([complete])
        return complete

    service = OnDemandStudySummaryService(repository, provider, loader)
    payload, from_cache = await service.summarize_nct_id(complete.nct_id)

    assert from_cache is False
    assert repository.get_current_summary(complete.nct_id, complete.source_hash) == payload.plain_language_summary
    assert service.external_repository.summaries == {}


@pytest.mark.asyncio
async def test_on_demand_known_and_external_paths_share_one_generation_budget(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    known, external = _studies()[:2]
    known = known.model_copy(update={"detail_status": DetailRetrievalStatus.RETRIEVED})
    repository.upsert_studies([known])

    async def loader(nct_id: str):
        assert nct_id == external.nct_id
        return external

    service = OnDemandStudySummaryService(
        repository, FixtureProvider(), loader, max_generation_requests=1
    )
    await service.summarize_nct_id(external.nct_id)
    with pytest.raises(SummaryError, match="request limit"):
        await service.summarize_nct_id(known.nct_id)
