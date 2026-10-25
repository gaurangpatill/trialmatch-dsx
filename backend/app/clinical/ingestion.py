"""ClinicalTrials.gov ingestion orchestration and provenance reporting."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .client import ClinicalTrialsClient, PartialFetchError
from .database import ClinicalRepository
from .models import (
    DetailRetrievalStatus,
    IngestionCheckpoint,
    IngestionReport,
    IngestionStatus,
    Study,
)
from .normalize import (
    StudyNormalizationError,
    normalize_study,
    source_missing_values,
    source_structured_criteria,
    verify_massachusetts_location,
)


class ClinicalIngestionService:
    def __init__(self, client: ClinicalTrialsClient, repository: ClinicalRepository):
        self.client = client
        self.repository = repository

    async def ingest(
        self,
        *,
        page_size: int = 1000,
        max_concurrency: int = 4,
        checkpoint_path: str | Path | None = None,
        refresh_details: bool = False,
    ) -> IngestionReport:
        """Publish only a complete, validated detail snapshot for the accepted index cohort."""

        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        started_at = datetime.now(UTC)
        starting_metrics = getattr(self.client, "metrics", None)
        starting_request_count = getattr(starting_metrics, "request_count", 0)
        starting_retry_count = getattr(starting_metrics, "retry_count", 0)
        starting_rate_limited_count = getattr(starting_metrics, "rate_limited_count", 0)
        errors: list[str] = []
        exclusions: list[dict[str, str]] = []
        source_complete = True
        try:
            fetched = await self.client.fetch_all_recruiting_massachusetts(page_size=page_size)
            records = fetched.records
            page_count = fetched.page_count
            duplicates = fetched.duplicate_identifier_count
        except PartialFetchError as exc:
            records = exc.records
            page_count = exc.page_count
            duplicates = exc.duplicate_identifier_count
            errors.append(str(exc))
            source_complete = False

        index_retrieved_at = datetime.now(UTC)
        indexed: dict[str, Study] = {}
        for index, record in enumerate(records):
            try:
                study = normalize_study(record, index_retrieved_at)
            except StudyNormalizationError as exc:
                exclusions.append({"record": str(index), "reason": str(exc)})
                continue
            if study.nct_id in indexed:
                duplicates += 1
                continue
            indexed[study.nct_id] = study

        expected_ids = sorted(indexed)
        query_url = self.client.list_query_url(page_size)
        cohort_fingerprint = _cohort_fingerprint(query_url, indexed) if expected_ids else None
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(
            getattr(self.client, "cache_dir", "backend/data/cache/clinicaltrials")
        ) / "ingestion_checkpoint.json"
        checkpoint: IngestionCheckpoint | None = None
        checkpoint_resumed = False
        initial_completed: dict[str, str] = {}
        detailed: dict[str, Study] = {}
        detail_hashes: dict[str, str] = {}
        detail_cache_hits = 0
        detail_network = 0
        detail_retrieved = 0
        detail_resumed = 0
        ma_verified = 0
        ma_unverified_records: list[dict[str, str]] = []
        checkpoint_lock = asyncio.Lock()

        can_retrieve_details = bool(expected_ids) and source_complete and not exclusions
        if can_retrieve_details and cohort_fingerprint is not None:
            checkpoint = _load_checkpoint(
                checkpoint_file,
                query_url=query_url,
                cohort_fingerprint=cohort_fingerprint,
                expected_ids=expected_ids,
            )
            if checkpoint is None:
                checkpoint = IngestionCheckpoint(
                    cohort_fingerprint=cohort_fingerprint,
                    query_url=query_url,
                    expected_ids=expected_ids,
                    started_at=started_at,
                    updated_at=started_at,
                )
                _write_checkpoint(checkpoint_file, checkpoint)
            else:
                initial_completed = dict(checkpoint.completed_detail_hashes)
                checkpoint_resumed = bool(initial_completed)

            queue: asyncio.Queue[str] = asyncio.Queue()
            for nct_id in expected_ids:
                queue.put_nowait(nct_id)

            async def worker() -> None:
                nonlocal detail_cache_hits, detail_network, detail_retrieved
                nonlocal detail_resumed, ma_verified, checkpoint
                while True:
                    try:
                        nct_id = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    index_study = indexed[nct_id]
                    try:
                        detail = await self.client.fetch_detail(
                            nct_id,
                            use_cache=not refresh_details,
                            expected_overall_status=index_study.overall_status,
                            expected_last_update=index_study.source_last_updated,
                            expected_index_hash=index_study.source_hash,
                        )
                        detail_retrieved += 1
                        if detail.from_cache:
                            detail_cache_hits += 1
                        else:
                            detail_network += 1
                        detailed_study = normalize_study(
                            detail.record,
                            detail.retrieved_at,
                            detail_status=DetailRetrievalStatus.RETRIEVED,
                        )
                        if detailed_study.nct_id != nct_id:
                            raise StudyNormalizationError(
                                f"detail identifier mismatch: requested {nct_id}, received {detailed_study.nct_id}"
                            )
                        resolved_detail_hash = detail.detail_hash or detailed_study.source_hash
                        verified, reason = verify_massachusetts_location(detailed_study)
                        if not verified:
                            ma_unverified_records.append({"nct_id": nct_id, "reason": reason})
                            errors.append(f"detail {nct_id}: Massachusetts verification failed: {reason}")
                            continue
                        ma_verified += 1
                        detailed[nct_id] = detailed_study
                        detail_hashes[nct_id] = resolved_detail_hash
                        if (
                            detail.from_cache
                            and initial_completed.get(nct_id) == resolved_detail_hash
                        ):
                            detail_resumed += 1
                        async with checkpoint_lock:
                            if checkpoint is None:
                                raise RuntimeError("checkpoint disappeared during detail retrieval")
                            completed = dict(checkpoint.completed_detail_hashes)
                            completed[nct_id] = resolved_detail_hash
                            checkpoint = checkpoint.model_copy(
                                update={
                                    "completed_detail_hashes": completed,
                                    "updated_at": datetime.now(UTC),
                                    "publication_completed": False,
                                }
                            )
                            _write_checkpoint(checkpoint_file, checkpoint)
                    except Exception as exc:  # each source failure belongs in run provenance
                        errors.append(f"detail {nct_id}: {type(exc).__name__}: {exc}")
                    finally:
                        queue.task_done()

            workers = [asyncio.create_task(worker()) for _ in range(min(max_concurrency, len(expected_ids)))]
            await asyncio.gather(*workers)

        incomplete_ids = sorted(set(expected_ids) - set(detailed))
        publication_ready = (
            can_retrieve_details
            and len(detailed) == len(expected_ids)
            and not incomplete_ids
            and len(detail_hashes) == len(expected_ids)
        )
        publication_attempted = publication_ready
        publication_completed = False
        published_at: datetime | None = None
        if publication_ready:
            studies = [detailed[nct_id] for nct_id in expected_ids]
            source_criteria = {
                study.nct_id: source_structured_criteria(
                    study, created_at=index_retrieved_at
                )
                for study in studies
            }
            try:
                self.repository.publish_complete_snapshot(studies, source_criteria)
                published_at = datetime.now(UTC)
                publication_completed = True
                if checkpoint is not None:
                    checkpoint = checkpoint.model_copy(
                        update={
                            "updated_at": datetime.now(UTC),
                            "publication_completed": True,
                        }
                    )
                    _write_checkpoint(checkpoint_file, checkpoint)
            except Exception as exc:  # repository publication is expected to be transactional
                errors.append(f"publication: {type(exc).__name__}: {exc}")

        if publication_completed:
            status = IngestionStatus.COMPLETE
        elif not indexed:
            status = IngestionStatus.FAILED
        else:
            status = IngestionStatus.PARTIAL

        published_studies = [detailed[nct_id] for nct_id in expected_ids] if publication_completed else []
        missing_counts: Counter[str] = Counter()
        for study in published_studies or indexed.values():
            missing_counts.update(source_missing_values(study))
        client_metrics = getattr(self.client, "metrics", None)
        report = IngestionReport(
            source_url=query_url,
            effective_page_size=page_size,
            pagination_completed=source_complete,
            started_at=started_at,
            index_retrieved_at=index_retrieved_at,
            published_at=published_at,
            completed_at=datetime.now(UTC),
            status=status,
            page_count=page_count,
            raw_record_count=len(records) + duplicates,
            accepted_record_count=len(indexed),
            duplicate_identifier_count=duplicates,
            missing_value_counts={key: max(0, value) for key, value in sorted(missing_counts.items())},
            excluded_records=exclusions,
            errors=sorted(errors),
            detail_requested_count=len(expected_ids),
            detail_retrieved_count=detail_retrieved,
            index_retrieved_count=len(records),
            detail_cache_hit_count=detail_cache_hits,
            detail_network_count=detail_network,
            detail_resumed_count=detail_resumed,
            detail_failed_count=len(incomplete_ids),
            incomplete_detail_ids=incomplete_ids,
            ma_verified_count=ma_verified,
            ma_unverified_count=len(ma_unverified_records),
            ma_unverified_records=sorted(ma_unverified_records, key=lambda item: item["nct_id"]),
            request_count=max(
                0, getattr(client_metrics, "request_count", 0) - starting_request_count
            ),
            retry_count=max(0, getattr(client_metrics, "retry_count", 0) - starting_retry_count),
            rate_limited_count=max(
                0,
                getattr(client_metrics, "rate_limited_count", 0)
                - starting_rate_limited_count,
            ),
            cohort_fingerprint=cohort_fingerprint,
            checkpoint_resumed=checkpoint_resumed,
            publication_attempted=publication_attempted,
            publication_completed=publication_completed,
            published_record_count=len(published_studies),
            previous_snapshot_preserved=not publication_completed,
        )
        self.repository.save_ingestion_report(report)
        return report


def _cohort_fingerprint(query_url: str, indexed: dict[str, Study]) -> str:
    payload = {
        "query_url": query_url,
        "records": [
            {
                "nct_id": nct_id,
                "overall_status": indexed[nct_id].overall_status,
                "source_last_updated": indexed[nct_id].source_last_updated,
                "index_record_hash": indexed[nct_id].source_hash,
            }
            for nct_id in sorted(indexed)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _load_checkpoint(
    path: Path,
    *,
    query_url: str,
    cohort_fingerprint: str,
    expected_ids: list[str],
) -> IngestionCheckpoint | None:
    if not path.exists():
        return None
    try:
        checkpoint = IngestionCheckpoint.model_validate_json(path.read_text())
    except (OSError, ValidationError, ValueError):
        return None
    if (
        checkpoint.query_url != query_url
        or checkpoint.cohort_fingerprint != cohort_fingerprint
        or checkpoint.expected_ids != expected_ids
    ):
        return None
    return checkpoint


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _write_checkpoint(path: Path, checkpoint: IngestionCheckpoint) -> None:
    payload = checkpoint.model_dump_json(indent=2)
    IngestionCheckpoint.model_validate_json(payload)
    _atomic_write(path, payload + "\n")


def write_report(report: IngestionReport, path: str | Path) -> None:
    destination = Path(path)
    payload = report.model_dump_json(indent=2)
    IngestionReport.model_validate_json(payload)
    _atomic_write(destination, payload + "\n")


def update_manifest(report: IngestionReport, path: str | Path) -> None:
    """Update verified acquisition metadata only after complete publication."""

    if not report.publication_completed:
        return
    if report.index_retrieved_at is None or report.published_at is None:
        raise ValueError("complete publication is missing provenance timestamps")
    manifest_path = Path(path)
    manifest: dict[str, Any] = json.loads(manifest_path.read_text())
    source = next(
        item for item in manifest["sources"] if item["id"] == "clinicaltrials-gov-ma-recruiting"
    )
    source["acquisition"] = {
        "status": report.status.value,
        "acquired_at_utc": report.index_retrieved_at.isoformat(),
        "index_retrieved_at": report.index_retrieved_at.isoformat(),
        "published_at": report.published_at.isoformat(),
        "observed_record_count": report.raw_record_count,
        "accepted_record_count": report.accepted_record_count,
        "published_record_count": report.published_record_count,
        "duplicate_identifier_count": report.duplicate_identifier_count,
        "missing_value_counts": report.missing_value_counts,
        "schema_valid": report.pagination_completed and not report.excluded_records,
        "raw_sha256": None,
        "effective_query_url": report.source_url,
        "effective_page_size": report.effective_page_size,
        "page_count": report.page_count,
        "pagination_completed": report.pagination_completed,
        "cohort_fingerprint": report.cohort_fingerprint,
        "filters_or_exclusions": [
            "query.locn=Massachusetts",
            "filter.overallStatus=RECRUITING",
            f"excluded_source_records={len(report.excluded_records)}",
            f"validated_detail_records={report.detail_retrieved_count - report.ma_unverified_count}",
            f"ma_verified_records={report.ma_verified_count}",
        ],
    }
    manifest["last_updated_utc"] = report.published_at.isoformat()
    _atomic_write(manifest_path, json.dumps(manifest, indent=2) + "\n")
