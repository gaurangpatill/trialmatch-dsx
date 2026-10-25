from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import backend.app.clinical.router as clinical_router
from backend.app.clinical.client import DetailResult
from backend.app.clinical.database import ClinicalRepository
from backend.app.clinical.dependencies import (
    ClinicalRepositoryUnavailable,
    get_repository,
    get_summary_service,
)
from backend.app.clinical.models import DetailRetrievalStatus, PlainLanguageSummaryPayload
from backend.app.clinical.normalize import normalize_study, source_structured_criteria
from backend.app.main import app


def client_with_repository(repository):
    app.dependency_overrides[get_repository] = lambda: repository
    return TestClient(app)


def test_trial_search_contract(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = study_factory(conditions=["Cancer"])
    repository.upsert_studies([study])
    with client_with_repository(repository) as client:
        response = client.get("/api/trials", params={"query": "cancer", "page_size": 10})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["nct_id"] == study.nct_id
    assert body["items"][0]["source_description"] == study.source_description
    assert body["items"][0]["brief_summary"] == study.brief_summary
    assert body["items"][0]["detailed_description"] == study.detailed_description
    assert body["items"][0]["plain_language_summary"] is None
    assert body["pagination"] == {"page": 1, "page_size": 10, "total_items": 1, "total_pages": 1}
    assert body["data_freshness"]["source"] == "ClinicalTrials.gov"


def test_empty_trial_search_contract(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies([study_factory()])
    with client_with_repository(repository) as client:
        response = client.get("/api/trials", params={"query": "does-not-exist"})
    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["pagination"]["total_items"] == 0
    assert response.json()["items"] == []


def test_match_contract_and_evidence(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = study_factory()
    repository.upsert_studies([study])
    repository.replace_criteria(
        study.nct_id, "source_structured", source_structured_criteria(study)
    )
    payload = {
        "patient": {
            "profile_id": "synthetic-api",
            "synthetic": True,
            "age_years": 40,
            "conditions": [],
            "medications": [],
            "procedures": [],
            "laboratory_values": {},
            "attributes": {},
        },
        "trial_ids": [study.nct_id],
        "limit": 20,
    }
    with client_with_repository(repository) as client:
        response = client.post("/api/match", json=payload)
    app.dependency_overrides.clear()

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["nct_id"] == study.nct_id
    assert result["screening_status"] == "unresolved"
    assert result["extraction_coverage"]["status"] == "not_attempted"
    criterion_texts = {item["original_text"] for item in result["criteria"]}
    assert "Minimum Age: 18 Years" in criterion_texts
    assert "Maximum Age: 65 Years" in criterion_texts
    assert result["source_url"] == study.source_url


def test_non_synthetic_profile_is_rejected_with_contract_error(
    study_factory, tmp_path
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies([study_factory()])
    payload = {
        "patient": {"profile_id": "real-profile", "synthetic": False},
        "limit": 20,
    }
    with client_with_repository(repository) as client:
        response = client.post("/api/match", json=payload)
    app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SYNTHETIC_PROFILE_REQUIRED"
    assert response.json()["error"]["retryable"] is False


def test_validation_error_uses_shared_envelope(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    repository.upsert_studies([study_factory()])
    with client_with_repository(repository) as client:
        response = client.get("/api/trials", params={"page_size": 101})
    app.dependency_overrides.clear()

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"][0]["field"] == "page_size"
    assert body["request_id"]


def test_trial_detail_uses_expanded_local_record(source_record_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    record = source_record_factory()
    record["protocolSection"]["contactsLocationsModule"]["locations"].append(
        {
            "facility": "Synthetic International Facility",
            "status": "ACTIVE_NOT_RECRUITING",
            "city": "Toronto",
            "state": "Ontario",
            "country": "Canada",
        }
    )
    study = normalize_study(record)
    repository.upsert_studies([study])

    with client_with_repository(repository) as client:
        response = client.get(f"/api/trials/{study.nct_id}")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["detail_status"] == "retrieved"
    assert body["retrieval_source"] == "local_database"
    assert body["minimum_age"] == "18 Years"
    assert body["sex"] == "ALL"
    assert body["source_description"] == (
        "This official source text describes the synthetic study purpose."
    )
    assert body["brief_summary"] == body["source_description"]
    assert body["detailed_description"] is None
    assert body["plain_language_summary"] is None
    assert len(body["locations"]) == 2
    assert body["locations"][0]["facility"] == "Synthetic Facility"
    assert body["locations"][0]["recruiting_status"] == "RECRUITING"
    assert body["locations"][1]["country"] == "Canada"
    assert body["contacts"][0]["email"] == "test@example.invalid"


@pytest.mark.asyncio
async def test_detail_fetch_is_deduplicated_and_persisted(
    source_record_factory, tmp_path, monkeypatch
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    indexed = source_record_factory()
    indexed["protocolSection"].pop("eligibilityModule")
    indexed["protocolSection"].pop("contactsLocationsModule")
    indexed["protocolSection"].pop("descriptionModule")
    repository.upsert_studies([normalize_study(indexed)])
    detail_record = source_record_factory()
    calls = 0

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def fetch_detail(self, nct_id, *, use_cache=True):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return DetailResult(
                record=detail_record,
                retrieved_at=datetime(2026, 10, 24, 19, 0, tzinfo=UTC),
                from_cache=False,
            )

    class ProtocolOnlyRepository:
        identity = repository.identity

        @property
        def path(self):
            raise AssertionError("detail coalescing must use repository identity, not path")

        def __getattr__(self, name):
            return getattr(repository, name)

    protocol_repository = ProtocolOnlyRepository()
    monkeypatch.setattr(clinical_router, "ClinicalTrialsClient", FakeClient)
    first, second = await asyncio.gather(
        clinical_router._get_or_fetch_detail(
            protocol_repository, "NCT00000001", refresh=False
        ),
        clinical_router._get_or_fetch_detail(
            protocol_repository, "NCT00000001", refresh=False
        ),
    )

    assert calls == 1
    assert first[0].detail_status == DetailRetrievalStatus.RETRIEVED
    assert second[0].locations[0].city == "Boston"
    stored = repository.get_study("NCT00000001")
    assert stored is not None
    assert stored.minimum_age == "18 Years"
    assert stored.contacts[0].email == "test@example.invalid"


def test_trial_detail_retrieval_failure_keeps_missing_values_unknown(
    source_record_factory, tmp_path, monkeypatch
):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    indexed = source_record_factory()
    indexed["protocolSection"].pop("eligibilityModule")
    indexed["protocolSection"].pop("contactsLocationsModule")
    indexed["protocolSection"].pop("descriptionModule")
    study = normalize_study(indexed)
    repository.upsert_studies([study])

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def fetch_detail(self, nct_id, *, use_cache=True):
            raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(clinical_router, "ClinicalTrialsClient", FailingClient)
    with client_with_repository(repository) as client:
        response = client.get(f"/api/trials/{study.nct_id}")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["detail_status"] == "retrieval_failed"
    assert body["minimum_age"] is None
    assert body["sex"] is None
    assert body["locations"] == []
    assert "does not mean" not in body["retrieval_message"].lower()
    assert "missing values remain unknown" in body["retrieval_message"].lower()


def test_empty_clinical_snapshot_returns_safe_not_ready_error(tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")

    with client_with_repository(repository) as client:
        response = client.get("/api/trials")
    app.dependency_overrides.clear()

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "CLINICAL_STORAGE_NOT_READY"
    assert body["retryable"] is True
    assert body["details"] == []


def test_unavailable_clinical_storage_returns_sanitized_error():
    secret = "postgresql://sensitive-user:sensitive-password@db.invalid/clinical"

    def unavailable():
        raise ClinicalRepositoryUnavailable(secret)

    app.dependency_overrides[get_repository] = unavailable
    with TestClient(app) as client:
        response = client.get("/api/trials")
    app.dependency_overrides.clear()

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "CLINICAL_STORAGE_UNAVAILABLE"
    assert body["message"] == "Clinical storage is unavailable."
    assert body["retryable"] is True
    assert body["request_id"]
    assert "sensitive" not in response.text
    assert "db.invalid" not in response.text


def test_on_demand_summary_endpoint_returns_cache_provenance(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = study_factory()
    repository.upsert_studies([study])
    calls = 0

    class FakeSummaryService:
        async def summarize_nct_id(self, nct_id):
            nonlocal calls
            calls += 1
            assert nct_id == study.nct_id
            return (
                PlainLanguageSummaryPayload(
                    nct_id=nct_id,
                    source_hash=study.source_hash,
                    plain_language_summary=(
                        "What is this study investigating? Official purpose. "
                        "What might participation involve? Official procedures. "
                        "Who is the study intended for? Published population. "
                        "Important things to know. Review the official record."
                    ),
                ),
                calls > 1,
            )

    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_summary_service] = lambda: FakeSummaryService()
    with TestClient(app) as client:
        first = client.post(f"/api/trials/{study.nct_id}/summary")
        second = client.post(f"/api/trials/{study.nct_id}/summary")
        missing = client.post("/api/trials/NCT99999999/summary")
    app.dependency_overrides.clear()

    assert first.status_code == 200
    assert first.json()["from_cache"] is False
    assert second.status_code == 200
    assert second.json()["from_cache"] is True
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TRIAL_NOT_FOUND"


def test_on_demand_summary_endpoint_sanitizes_provider_failure(study_factory, tmp_path):
    repository = ClinicalRepository(tmp_path / "clinical.sqlite3")
    study = study_factory()
    repository.upsert_studies([study])

    class FailingSummaryService:
        async def summarize_nct_id(self, _nct_id):
            from backend.app.clinical.summaries import SummaryError
            raise SummaryError("provider secret and diagnostic")

    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_summary_service] = lambda: FailingSummaryService()
    with TestClient(app) as client:
        response = client.post(f"/api/trials/{study.nct_id}/summary")
    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_SUMMARY_UNAVAILABLE"
    assert "secret" not in response.text
    assert "AI explanation is temporarily unavailable" in response.text
    assert "You can still read the official study description" in response.text
