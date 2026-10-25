"""Finite Clinical Intelligence maintenance commands."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .client import ClinicalTrialsClient
from .database import ClinicalRepository
from .dependencies import get_database_url
from .extraction import EligibilityExtractor, ExtractionError, provider_from_environment
from .ingestion import ClinicalIngestionService, update_manifest, write_report
from .migrations import migrate_postgres
from .repository import ClinicalRepositoryProtocol, create_repository
from .sites import build_site_export, write_site_export
from .summaries import StudySummaryService, SummaryError, summary_provider_from_environment


class CLIConfigurationError(RuntimeError):
    """A command target is invalid without retaining its potentially sensitive value."""


def _add_database_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--database",
        default=None,
        metavar="SQLITE_PATH",
        help=(
            "legacy explicit SQLite path override; when omitted, "
            "TRIALMATCH_DATABASE_URL is used"
        ),
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="TrialMatch Clinical Intelligence commands")
    commands = root.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="ingest recruiting Massachusetts trials")
    _add_database_argument(ingest)
    ingest.add_argument("--cache-dir", default="backend/data/cache/clinicaltrials")
    ingest.add_argument("--page-size", type=int, default=1000)
    ingest.add_argument(
        "--max-concurrency",
        type=int,
        default=4,
        help="maximum simultaneous detail requests (default: 4)",
    )
    ingest.add_argument(
        "--min-request-interval-seconds",
        type=float,
        default=0.2,
        help="shared minimum spacing between all source requests (default: 0.2)",
    )
    ingest.add_argument("--timeout-seconds", type=float, default=30.0)
    ingest.add_argument("--max-retries", type=int, default=3)
    ingest.add_argument("--max-retry-delay-seconds", type=float, default=60.0)
    ingest.add_argument(
        "--checkpoint",
        default=None,
        help="atomic JSON checkpoint path (default: CACHE_DIR/ingestion_checkpoint.json)",
    )
    ingest.add_argument(
        "--refresh",
        "--refresh-details",
        action="store_true",
        dest="refresh_details",
        help="bypass valid detail caches; publication remains all-or-nothing",
    )
    ingest.add_argument(
        "--report", default="backend/data/cache/clinicaltrials/latest_ingestion_report.json"
    )
    ingest.add_argument("--update-manifest", default=None)

    extract = commands.add_parser("extract", help="extract criteria for detailed cached studies")
    _add_database_argument(extract)
    extract.add_argument("--limit", type=int, default=20)

    summarize = commands.add_parser(
        "summarize", help="generate cached summaries for one to three explicit study IDs"
    )
    _add_database_argument(summarize)
    summarize.add_argument("--nct-id", action="append", dest="nct_ids", required=True)

    export_sites = commands.add_parser(
        "export-sites", help="export validated Massachusetts recruiting facilities"
    )
    _add_database_argument(export_sites)
    export_sites.add_argument(
        "--output", default="backend/data/exports/ma_trial_sites.json"
    )
    export_sites.add_argument(
        "--cohort-description",
        default="All expanded individual study records in the clinical snapshot",
    )

    migrate = commands.add_parser(
        "migrate", help="explicitly apply non-destructive clinical schema migrations"
    )
    _add_database_argument(migrate)

    readiness = commands.add_parser(
        "readiness", help="check schema and serving-snapshot readiness"
    )
    _add_database_argument(readiness)
    return root


def _database_url(args: argparse.Namespace) -> str:
    legacy_path = getattr(args, "database", None)
    if legacy_path is None:
        return get_database_url()
    if not isinstance(legacy_path, str) or not legacy_path.strip() or "://" in legacy_path:
        raise CLIConfigurationError("Invalid legacy SQLite path override")
    return f"sqlite:///{legacy_path}"


def _backend_name(database_url: str) -> str:
    try:
        scheme = urlsplit(database_url).scheme.casefold()
    except ValueError as exc:
        raise CLIConfigurationError("Invalid clinical database configuration") from None
    if scheme == "sqlite":
        return "sqlite"
    if scheme in {"postgres", "postgresql", "postgresql+psycopg"}:
        return "postgresql"
    raise CLIConfigurationError("Unsupported clinical database configuration")


def _repository(args: argparse.Namespace) -> ClinicalRepositoryProtocol:
    database_url = _database_url(args)
    _backend_name(database_url)
    return create_repository(database_url)


def _sqlite_path(database_url: str) -> Path:
    parsed = urlsplit(database_url)
    if (
        parsed.scheme.casefold() != "sqlite"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not database_url.startswith("sqlite:///")
    ):
        raise CLIConfigurationError("Invalid SQLite database configuration")
    value = database_url.removeprefix("sqlite:///")
    if not value:
        raise CLIConfigurationError("Invalid SQLite database configuration")
    return Path(value)


def _postgres_migration_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    if parsed.fragment or not parsed.path or parsed.path == "/":
        raise CLIConfigurationError("Invalid PostgreSQL database configuration")
    if parsed.scheme.casefold() == "postgresql+psycopg":
        return urlunsplit(parsed._replace(scheme="postgresql"))
    return database_url


async def run_ingest(args: argparse.Namespace) -> int:
    repository = _repository(args)
    async with ClinicalTrialsClient(
        cache_dir=args.cache_dir,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        max_retry_delay_seconds=args.max_retry_delay_seconds,
        min_request_interval_seconds=args.min_request_interval_seconds,
    ) as client:
        report = await ClinicalIngestionService(client, repository).ingest(
            page_size=args.page_size,
            max_concurrency=args.max_concurrency,
            checkpoint_path=args.checkpoint,
            refresh_details=args.refresh_details,
        )
    write_report(report, args.report)
    if args.update_manifest:
        update_manifest(report, args.update_manifest)
    print(report.model_dump_json(indent=2))
    return 0 if report.status.value == "complete" else 1


async def run_extract(args: argparse.Namespace) -> int:
    repository = _repository(args)
    provider = provider_from_environment()
    extractor = EligibilityExtractor(repository, provider)
    studies = [study for study in repository.get_studies(limit=10000) if study.eligibility_criteria][
        : args.limit
    ]
    results: list[dict[str, object]] = []
    failed = False
    for study in studies:
        try:
            criteria, from_cache = await extractor.extract(study)
            state, accepted_payload, coverage_verified, verification_method = (
                repository.get_extraction_state(study.nct_id, study.source_hash)
            )
            coverage = accepted_payload.get("coverage", {}) if accepted_payload else {}
            results.append(
                {
                    "nct_id": study.nct_id,
                    "criteria": len(criteria),
                    "from_cache": from_cache,
                    "coverage_status": coverage.get("status", state),
                    "protocol_item_count": coverage.get("protocol_item_count"),
                    "accounted_item_count": coverage.get("accounted_item_count"),
                    "uncovered_item_count": len(coverage.get("uncovered_items", [])),
                    "independently_verified": coverage_verified,
                    "verification_method": verification_method,
                    "provider": provider.name,
                    "model": provider.model,
                    "token_usage": (
                        getattr(provider, "last_usage").model_dump()
                        if not from_cache and getattr(provider, "last_usage", None)
                        else None
                    ),
                }
            )
        except ExtractionError as exc:
            failed = True
            results.append({"nct_id": study.nct_id, "error": str(exc)})
    print(json.dumps({"studies": results}, indent=2))
    return 1 if failed else 0


async def run_summarize(args: argparse.Namespace) -> int:
    identifiers = list(dict.fromkeys(args.nct_ids))
    if len(identifiers) != len(args.nct_ids):
        print(json.dumps({"error": "--nct-id values must be unique"}))
        return 2
    if not 1 <= len(identifiers) <= 3:
        print(json.dumps({"error": "summarize requires one to three explicit --nct-id values"}))
        return 2
    repository = _repository(args)
    studies = {study.nct_id: study for study in repository.get_studies(identifiers, limit=3)}
    missing = [identifier for identifier in identifiers if identifier not in studies]
    if missing:
        print(json.dumps({"error": "unknown study IDs", "nct_ids": missing}, indent=2))
        return 2
    provider = summary_provider_from_environment()
    service = StudySummaryService(repository, provider)
    results: list[dict[str, object]] = []
    failed = False
    for identifier in identifiers:
        study = studies[identifier]
        try:
            payload, from_cache = await service.summarize(study)
            results.append(
                {
                    "nct_id": identifier,
                    "from_cache": from_cache,
                    "source_hash": payload.source_hash,
                    "provider": provider.name,
                    "model": provider.model,
                }
            )
        except SummaryError as exc:
            failed = True
            results.append({"nct_id": identifier, "error": str(exc)})
    print(json.dumps({"studies": results}, indent=2))
    return 1 if failed else 0


async def run_export_sites(args: argparse.Namespace) -> int:
    repository = _repository(args)
    export = build_site_export(
        repository, cohort_selection=args.cohort_description
    )
    write_site_export(export, args.output)
    print(
        json.dumps(
            {
                "output": str(Path(args.output)),
                "exported_records": export.metadata.exported_record_count,
                "exported_studies": export.metadata.exported_study_count,
                "resolved_coordinates": export.metadata.resolved_coordinate_count,
                "unresolved_coordinates": export.metadata.unresolved_coordinate_count,
                "expanded_studies": export.metadata.expanded_study_count,
            },
            indent=2,
        )
    )
    return 0


async def run_migrate(args: argparse.Namespace) -> int:
    database_url = _database_url(args)
    backend = _backend_name(database_url)
    if backend == "sqlite":
        ClinicalRepository(_sqlite_path(database_url))
        applied: list[str] = []
    else:
        applied = list(migrate_postgres(_postgres_migration_url(database_url)))
    print(json.dumps({"status": "migrated", "backend": backend, "applied": applied}))
    return 0


async def run_readiness(args: argparse.Namespace) -> int:
    database_url = _database_url(args)
    backend = _backend_name(database_url)
    try:
        stats = create_repository(database_url).storage_stats()
        schema_ready = bool(stats.get("schema_ready"))
        data_ready = bool(stats.get("data_ready"))
        studies = int(stats.get("counts", {}).get("studies", 0))
    except Exception:
        schema_ready = False
        data_ready = False
        studies = 0
    ready = schema_ready and data_ready and studies > 0
    print(
        json.dumps(
            {
                "status": "ready" if ready else "not_ready",
                "backend": backend,
                "schema_ready": schema_ready,
                "data_ready": data_ready,
                "studies": studies,
            }
        )
    )
    return 0 if ready else 1


async def async_main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "ingest":
            return await run_ingest(args)
        if args.command == "extract":
            return await run_extract(args)
        if args.command == "summarize":
            return await run_summarize(args)
        if args.command == "export-sites":
            return await run_export_sites(args)
        if args.command == "migrate":
            return await run_migrate(args)
        if args.command == "readiness":
            return await run_readiness(args)
        raise AssertionError(f"unhandled command {args.command}")
    except CLIConfigurationError:
        print(json.dumps({"error": "invalid_database_configuration"}))
        return 2
    except Exception:
        print(json.dumps({"error": "clinical_command_failed", "retryable": True}))
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
