from fastapi.testclient import TestClient

import backend.app.main as main_module
from backend.app.main import app


def test_health_is_process_liveness_not_data_readiness() -> None:
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "trialmatch-api",
        "version": "0.1.0",
    }


def test_cors_allows_local_frontend_and_rejects_unlisted_origins() -> None:
    headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "GET",
    }
    with TestClient(app) as client:
        allowed = client.options("/api/health", headers=headers)
        rejected = client.options(
            "/api/health",
            headers={**headers, "Origin": "https://untrusted.example"},
        )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers


class _ReadinessRepository:
    def __init__(self, *, data_ready: bool, studies: int):
        self._stats = {
            "backend": "sqlite",
            "schema_ready": True,
            "data_ready": data_ready,
            "counts": {"studies": studies},
        }

    def storage_stats(self):
        return self._stats


def test_readiness_succeeds_only_with_nonempty_current_storage(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "get_repository",
        lambda: _ReadinessRepository(data_ready=True, studies=3),
    )

    with TestClient(app) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "trialmatch-api",
        "version": "0.1.0",
        "clinical": {
            "backend": "sqlite",
            "schema_ready": True,
            "data_ready": True,
            "studies": 3,
        },
    }


def test_readiness_returns_sanitized_503_for_empty_storage(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "get_repository",
        lambda: _ReadinessRepository(data_ready=False, studies=0),
    )

    with TestClient(app) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["clinical"] == {
        "backend": "sqlite",
        "schema_ready": True,
        "data_ready": False,
        "studies": 0,
    }


def test_readiness_is_sanitized_when_storage_is_unavailable(monkeypatch) -> None:
    secret = "postgresql://sensitive-user:sensitive-password@db.invalid/clinical"

    def unavailable():
        raise RuntimeError(secret)

    monkeypatch.setattr(main_module, "get_repository", unavailable)

    with TestClient(app) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["clinical"] == {
        "backend": None,
        "schema_ready": False,
        "data_ready": False,
        "studies": 0,
    }
    assert "sensitive" not in response.text
    assert "db.invalid" not in response.text


def test_health_remains_available_when_clinical_storage_is_unavailable(monkeypatch) -> None:
    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main_module, "get_repository", unavailable)

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
