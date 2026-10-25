from __future__ import annotations

from pathlib import Path

import pytest

import backend.app.clinical.dependencies as dependencies


@pytest.fixture(autouse=True)
def clear_repository_cache():
    dependencies.get_repository.cache_clear()
    yield
    dependencies.get_repository.cache_clear()


def test_project_environment_loader_uses_root_file_without_override(monkeypatch):
    captured = {}

    def fake_load_dotenv(path, *, override):
        captured.update(path=path, override=override)
        return True

    monkeypatch.setattr(dependencies, "load_dotenv", fake_load_dotenv)

    assert dependencies.load_project_environment() is True
    assert captured == {
        "path": dependencies.PROJECT_ROOT / ".env",
        "override": False,
    }
    assert dependencies.PROJECT_ROOT == Path(__file__).resolve().parents[3]


def test_repository_uses_environment_url_and_factory(monkeypatch):
    database_url = "postgresql://runtime-user:runtime-secret@database.invalid/clinical"
    sentinel = object()
    captured = []
    monkeypatch.setenv("TRIALMATCH_DATABASE_URL", database_url)
    monkeypatch.setattr(
        dependencies,
        "create_repository",
        lambda value: captured.append(value) or sentinel,
    )

    assert dependencies.get_repository() is sentinel
    assert dependencies.get_repository() is sentinel
    assert captured == [database_url]


def test_repository_uses_safe_default_when_environment_is_absent(monkeypatch):
    captured = []
    monkeypatch.delenv("TRIALMATCH_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        dependencies,
        "create_repository",
        lambda value: captured.append(value) or object(),
    )

    dependencies.get_repository()

    assert captured == [dependencies.DEFAULT_DATABASE_URL]


def test_repository_factory_failure_is_sanitized(monkeypatch):
    secret = "postgresql://sensitive-user:sensitive-password@database.invalid/clinical"
    monkeypatch.setenv("TRIALMATCH_DATABASE_URL", secret)

    def fail(_value):
        raise RuntimeError(secret)

    monkeypatch.setattr(dependencies, "create_repository", fail)

    with pytest.raises(dependencies.ClinicalRepositoryUnavailable) as caught:
        dependencies.get_repository()

    assert "sensitive" not in str(caught.value)
    assert "postgresql" not in str(caught.value)


class _ReadinessRepository:
    def __init__(self, *, schema_ready: bool, data_ready: bool):
        self._schema_ready = schema_ready
        self._data_ready = data_ready

    def schema_ready(self):
        return self._schema_ready

    def data_ready(self):
        return self._data_ready


def test_ready_dependency_rejects_empty_snapshot():
    repository = _ReadinessRepository(schema_ready=True, data_ready=False)

    with pytest.raises(dependencies.ClinicalRepositoryNotReady):
        dependencies.require_ready_repository(repository)
