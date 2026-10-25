from __future__ import annotations

import json
from argparse import Namespace

import pytest

import backend.app.clinical.cli as cli
from backend.app.clinical.repository import create_repository


def test_all_commands_default_to_environment_database_configuration():
    arguments = {
        "ingest": [],
        "extract": [],
        "summarize": ["--nct-id", "NCT00000001"],
        "export-sites": [],
        "migrate": [],
        "readiness": [],
    }

    for command, extra in arguments.items():
        args = cli.parser().parse_args([command, *extra])
        assert args.database is None


def test_repository_selection_prefers_environment_and_preserves_legacy_path(
    monkeypatch, tmp_path
):
    secret_url = "postgresql://runtime-user:runtime-password@database.invalid/clinical"
    captured = []
    sentinel = object()
    monkeypatch.setattr(cli, "get_database_url", lambda: secret_url)
    monkeypatch.setattr(
        cli, "create_repository", lambda value: captured.append(value) or sentinel
    )

    assert cli._repository(Namespace(database=None)) is sentinel
    legacy_path = tmp_path / "legacy.sqlite3"
    assert cli._repository(Namespace(database=str(legacy_path))) is sentinel

    assert captured == [secret_url, f"sqlite:///{legacy_path}"]


@pytest.mark.asyncio
async def test_migrate_initializes_only_an_explicit_sqlite_target(tmp_path, capsys):
    database_path = tmp_path / "clinical.sqlite3"

    assert await cli.run_migrate(Namespace(database=str(database_path))) == 0

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "status": "migrated",
        "backend": "sqlite",
        "applied": [],
    }
    assert str(database_path) not in output
    repository = create_repository(f"sqlite:///{database_path}")
    assert repository.schema_ready() is True
    assert repository.data_ready() is False


@pytest.mark.asyncio
async def test_migrate_dispatches_postgres_without_printing_credentials(monkeypatch, capsys):
    secret_url = "postgresql+psycopg://sensitive-user:sensitive-password@db.invalid/clinical"
    captured = []
    monkeypatch.setattr(cli, "get_database_url", lambda: secret_url)
    monkeypatch.setattr(
        cli,
        "migrate_postgres",
        lambda value: captured.append(value) or ("0001_initial",),
    )

    assert await cli.run_migrate(Namespace(database=None)) == 0

    output = capsys.readouterr().out
    assert captured == [
        "postgresql://sensitive-user:sensitive-password@db.invalid/clinical"
    ]
    assert json.loads(output) == {
        "status": "migrated",
        "backend": "postgresql",
        "applied": ["0001_initial"],
    }
    assert "sensitive" not in output
    assert "db.invalid" not in output


class _Repository:
    def __init__(self, *, schema_ready: bool, data_ready: bool, studies: int):
        self._stats = {
            "backend": "postgresql",
            "schema_ready": schema_ready,
            "data_ready": data_ready,
            "counts": {"studies": studies},
        }

    def storage_stats(self):
        return self._stats


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repository", "expected_status", "expected_code"),
    [
        (_Repository(schema_ready=True, data_ready=True, studies=4), "ready", 0),
        (_Repository(schema_ready=True, data_ready=False, studies=0), "not_ready", 1),
    ],
)
async def test_readiness_reports_ready_and_empty_states(
    repository, expected_status, expected_code, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "get_database_url", lambda: "postgresql://db.invalid/clinical")
    monkeypatch.setattr(cli, "create_repository", lambda _value: repository)

    assert await cli.run_readiness(Namespace(database=None)) == expected_code

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == expected_status
    assert payload["backend"] == "postgresql"
    assert payload["studies"] == repository._stats["counts"]["studies"]


@pytest.mark.asyncio
async def test_readiness_failure_never_prints_database_error_or_url(monkeypatch, capsys):
    secret_url = "postgresql://sensitive-user:sensitive-password@db.invalid/clinical"
    monkeypatch.setattr(cli, "get_database_url", lambda: secret_url)

    def fail(_value):
        raise RuntimeError(f"could not connect to {secret_url}")

    monkeypatch.setattr(cli, "create_repository", fail)

    assert await cli.run_readiness(Namespace(database=None)) == 1

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "status": "not_ready",
        "backend": "postgresql",
        "schema_ready": False,
        "data_ready": False,
        "studies": 0,
    }
    assert "sensitive" not in output
    assert "db.invalid" not in output
