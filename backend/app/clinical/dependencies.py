"""Runtime configuration and FastAPI dependencies for Clinical Intelligence."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends

from .repository import ClinicalRepositoryProtocol, create_repository

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE_URL = "sqlite:///./backend/data/trialmatch.sqlite3"

def load_project_environment() -> bool:
    """Load local development values without overriding the process environment."""

    return load_dotenv(PROJECT_ROOT / ".env", override=False)


# Local development reads the repository-root file. Exported/process-manager values win.
load_project_environment()


class ClinicalRepositoryUnavailable(RuntimeError):
    """The configured clinical storage cannot safely serve requests."""


class ClinicalRepositoryNotReady(ClinicalRepositoryUnavailable):
    """The clinical schema exists but has no serving snapshot."""


def get_database_url() -> str:
    """Return the configured database URL without logging or otherwise exposing it."""

    return os.getenv("TRIALMATCH_DATABASE_URL", DEFAULT_DATABASE_URL)


@lru_cache(maxsize=1)
def get_repository() -> ClinicalRepositoryProtocol:
    """Build the configured repository without creating or migrating its schema."""

    try:
        return create_repository(get_database_url())
    except (RuntimeError, ValueError):
        raise ClinicalRepositoryUnavailable(
            "Clinical storage is unavailable; initialize it explicitly and retry"
        ) from None


def require_ready_repository(
    repository: Annotated[ClinicalRepositoryProtocol, Depends(get_repository)],
) -> ClinicalRepositoryProtocol:
    """Require a current schema and a non-empty serving snapshot for clinical APIs."""

    try:
        schema_ready = repository.schema_ready()
        data_ready = repository.data_ready()
    except Exception:
        raise ClinicalRepositoryUnavailable(
            "Clinical storage is unavailable; initialize it explicitly and retry"
        ) from None
    if not schema_ready or not data_ready:
        raise ClinicalRepositoryNotReady(
            "Clinical storage has no ready serving snapshot"
        )
    return repository
