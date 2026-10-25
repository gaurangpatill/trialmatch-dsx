"""Explicit, ordered PostgreSQL schema migrations."""

from __future__ import annotations

from importlib.resources import files

import psycopg

LATEST_POSTGRES_SCHEMA_VERSION = "0003_snapshot_freshness"
_MIGRATION_NAMES = (
    "0001_initial",
    "0002_ingestion_reliability",
    LATEST_POSTGRES_SCHEMA_VERSION,
)
_MIGRATION_LOCK_ID = 2_024_102_404


def migrate_postgres(database_url: str) -> tuple[str, ...]:
    """Apply pending PostgreSQL migrations atomically and return applied versions.

    This function is intentionally never called by normal repository construction.
    Deployments and tests must invoke it as an explicit schema-management step.
    """

    applied: list[str] = []
    with psycopg.connect(database_url) as connection:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS clinical_schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT version FROM clinical_schema_migrations"
                ).fetchall()
            }
            migration_root = files(__package__)
            for version in _MIGRATION_NAMES:
                if version in existing:
                    continue
                sql = migration_root.joinpath(f"{version}.sql").read_text(encoding="utf-8")
                connection.execute(sql)
                connection.execute(
                    "INSERT INTO clinical_schema_migrations (version) VALUES (%s)",
                    (version,),
                )
                applied.append(version)
    return tuple(applied)
