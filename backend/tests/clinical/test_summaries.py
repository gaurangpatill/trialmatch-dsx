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
    _official_study_information,
    _parse_generated_content,
    summary_provider_from_environment,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "summary_source_records.json"


def _studies():
    records = json.loads(_FIXTURES.read_text())
    return [
        normalize_study(record, datetime(2026, 10, 25, 12, 0, tzinfo=UTC))
        for record in records
    ]


def _sections(study) -> dict[str, str]:
    return {
        "nct_id": study.nct_id,
        "source_hash": study.source_hash,
        "investigating": f"This study is investigating information described in {study.title}.",
        "participation": "Participation may involve the activities listed in the official study record.",
        "intended_for": "The intended participants are described in the official eligibility information.",
        "important": "Study details can change, so participants should verify them with the research team.",
    }


def _rendered(study) -> str:
    sections = _sections(study)
    return "\n\n".join(
        (
            f"What is this study investigating? {sections['investigating']}",
            f"What might participation involve? {sections['participation']}",
            f"Who is the study intended for? {sections['intended_for']}",
            f"Important things to know. {sections['important']}",
        )
    )


class FixtureProvider:
    name = "test-provider"
    model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, study):
        self.calls += 1
        return _rendered(study)


@pytest.mark.asyncio
async def test_cache_miss_generates_and_cache_hit_avoids_second_model_call(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[0]
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
    assert repository.get_current_summary(study.nct_id, study.source_hash) == first.plain_language_summary


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_generation(tmp_path):
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
            return _rendered(current)

    provider = SlowProvider()
    service = StudySummaryService(repository, provider)
    requests = [asyncio.create_task(service.summarize(study)) for _ in range(3)]
    await started.wait()
    assert provider.calls == 1
    release.set()
    results = await asyncio.gather(*requests)
    assert len({item[0].plain_language_summary for item in results}) == 1


@pytest.mark.asyncio
async def test_cache_never_serves_different_nct_id(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[0]
    repository.upsert_studies([study])
    provider = FixtureProvider()
    service = StudySummaryService(repository, provider)
    repository.publish_summary(
        cache_key=service.cache_key(study),
        nct_id=study.nct_id,
        source_hash=study.source_hash,
        provider=provider.name,
        model=provider.model,
        schema_version="4.0",
        prompt_version="4.0",
        payload={
            "nct_id": "NCT99999999",
            "source_hash": study.source_hash,
            "plain_language_summary": "Wrong study summary",
        },
        created_at=datetime.now(UTC),
    )

    payload, from_cache = await service.summarize(study)
    assert from_cache is False
    assert payload.nct_id == study.nct_id
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_on_demand_fetches_official_record_then_uses_durable_cache(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[0].model_copy(update={"detail_status": DetailRetrievalStatus.RETRIEVED})
    repository.upsert_studies([study])
    provider = FixtureProvider()
    loader_calls = 0

    async def loader(nct_id: str):
        nonlocal loader_calls
        loader_calls += 1
        assert nct_id == study.nct_id
        return study

    service = OnDemandStudySummaryService(repository, provider, loader)
    first, first_cached = await service.summarize_nct_id(study.nct_id)
    second, second_cached = await service.summarize_nct_id(study.nct_id)

    assert first_cached is False
    assert second_cached is True
    assert first == second
    assert loader_calls == 1
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_cache_write_failure_does_not_hide_generated_explanation(tmp_path):
    inner = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = _studies()[0]
    inner.upsert_studies([study])

    class WriteFailingRepository:
        identity = inner.identity

        def __getattr__(self, name):
            return getattr(inner, name)

        def publish_summary(self, **_kwargs):
            raise RuntimeError("database unavailable")

    provider = FixtureProvider()
    payload, from_cache = await StudySummaryService(WriteFailingRepository(), provider).summarize(study)

    assert from_cache is False
    assert payload.plain_language_summary == " ".join(_rendered(study).split())
    assert provider.calls == 1
    assert inner.get_current_summary(study.nct_id, study.source_hash) is None


def test_generated_content_normalizes_structured_aliases_and_plain_text():
    study = _studies()[0]
    structured = json.dumps(_sections(study))
    rendered = _parse_generated_content(study, structured)
    assert "What is this study investigating?" in rendered
    assert "Important things to know." in rendered

    aliased = json.dumps({
        "nct_id": study.nct_id,
        "source_hash": study.source_hash,
        "what_is_this_study_investigating": "A readable purpose.",
        "what_might_participation_involve": "A readable participation description.",
        "who_is_the_study_intended_for": "A readable population description.",
        "important_things_to_know": "A readable caution.",
    })
    assert "A readable purpose." in _parse_generated_content(study, aliased)
    assert _parse_generated_content(study, "Readable plain text explanation.") == "Readable plain text explanation."


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"nct_id": "NCT99999999"}, "NCT identifier mismatch"),
        ({"source_hash": "0" * 64}, "source hash mismatch"),
    ],
)
def test_generated_content_rejects_wrong_identifiers(patch, message):
    study = _studies()[0]
    raw = {**_sections(study), **patch}
    with pytest.raises(SummaryError, match=message):
        _parse_generated_content(study, json.dumps(raw))


def test_generated_content_rejects_empty_or_unusable_payloads():
    study = _studies()[0]
    with pytest.raises(SummaryError, match="empty"):
        _parse_generated_content(study, "  ")
    with pytest.raises(SummaryError, match="unusable"):
        _parse_generated_content(study, "[]")


@pytest.mark.asyncio
async def test_openai_adapter_sends_official_study_information_and_parses_output():
    study = _studies()[0]
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer test-secret"
        body = json.loads(request.content)
        assert body["model"] == DEFAULT_OPENAI_MODEL
        assert body["response_format"]["type"] == "json_schema"
        prompt = json.loads(body["messages"][1]["content"])
        assert prompt["nct_id"] == study.nct_id
        assert prompt["source_hash"] == study.source_hash
        official = prompt["official_study_information"]
        assert official["identificationModule"]
        assert official["descriptionModule"]
        assert official["eligibilityModule"]
        assert official["armsInterventionsModule"]
        assert official["outcomesModule"]
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(_sections(study))}}],
                "usage": {"prompt_tokens": 300, "completion_tokens": 180, "total_tokens": 480},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAISummaryProvider(api_key="test-secret", http_client=client)
        result = await provider.summarize(study)

    assert calls == 1
    assert result == _rendered(study)
    assert provider.last_usage["total_tokens"] == 480


@pytest.mark.asyncio
async def test_openai_adapter_returns_readable_non_json_content():
    study = _studies()[0]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "Readable explanation"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAISummaryProvider(api_key="test-secret", http_client=client)
        assert await provider.summarize(study) == "Readable explanation"


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


@pytest.mark.asyncio
async def test_disabled_and_invalid_nct_fail_without_fetching(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies([_studies()[0]])
    loader_calls = 0

    async def loader(_nct_id: str):
        nonlocal loader_calls
        loader_calls += 1
        return _studies()[0]

    service = OnDemandStudySummaryService(repository, DisabledSummaryProvider(), loader)
    with pytest.raises(SummaryError, match="disabled"):
        await service.summarize_nct_id(_studies()[0].nct_id)
    with pytest.raises(SummaryError, match="invalid NCT"):
        await service.summarize_nct_id("not-an-id")
    assert loader_calls == 0


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


def test_official_information_contains_only_actual_protocol_modules():
    study = _studies()[0]
    information = _official_study_information(study)
    assert information["identificationModule"]
    assert information["eligibilityModule"]
    assert "derivedSection" not in information


@pytest.mark.asyncio
async def test_generation_request_limit_is_enforced(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    first, second = _studies()[:2]
    repository.upsert_studies([first, second])
    service = StudySummaryService(repository, FixtureProvider(), max_generation_requests=1)
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
