"""Clinical Intelligence HTTP endpoints."""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse

from .client import ClinicalTrialsClient
from .dependencies import get_summary_service, require_ready_repository
from .matching import MatchingService, UnknownTrialIds, extraction_coverage
from .repository import ClinicalRepositoryProtocol
from .summaries import OnDemandStudySummaryService, SummaryError
from .models import (
    DataFreshness,
    DetailRetrievalStatus,
    ErrorBody,
    ErrorDetail,
    ErrorResponse,
    MatchRequest,
    MatchResponse,
    OnDemandSummaryResponse,
    Pagination,
    Study,
    StudyDetailResponse,
    StudySummary,
    SyntheticPatient,
    TrialSearchResponse,
)
from .normalize import normalize_study, source_structured_criteria


router = APIRouter(prefix="/api", tags=["clinical"])
RepositoryDependency = Annotated[
    ClinicalRepositoryProtocol, Depends(require_ready_repository)
]
SummaryServiceDependency = Annotated[
    OnDemandStudySummaryService, Depends(get_summary_service)
]
RetrievalSource = Literal["local_database", "official_cache", "official_api"]
_detail_requests: dict[tuple[str, str], asyncio.Task[tuple[Study, RetrievalSource]]] = {}


@router.get("/trials", response_model=TrialSearchResponse)
def search_trials(
    repository: RepositoryDependency,
    query: Annotated[str | None, Query(max_length=200)] = None,
    condition: Annotated[str | None, Query(max_length=200)] = None,
    phase: Annotated[str | None, Query(max_length=100)] = None,
    age_years: Annotated[float | None, Query(ge=0, le=125)] = None,
    location: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TrialSearchResponse:
    cleaned_query = query.strip() if query and query.strip() else None
    studies, total = repository.search_studies(
        query=cleaned_query,
        condition=condition,
        phase=phase,
        age_years=age_years,
        location=location,
        page=page,
        page_size=page_size,
    )
    return TrialSearchResponse(
        items=[
            StudySummary(
                nct_id=study.nct_id,
                title=study.title,
                overall_status=study.overall_status,
                conditions=study.conditions,
                phases=study.phases,
                source_description=study.source_description,
                brief_summary=study.brief_summary,
                detailed_description=study.detailed_description,
                plain_language_summary=repository.get_current_summary(
                    study.nct_id, study.source_hash
                ),
                minimum_age=study.minimum_age,
                maximum_age=study.maximum_age,
                sex=study.sex,
                locations=study.locations,
                source_url=study.source_url,
                retrieved_at=study.retrieved_at,
                detail_status=study.detail_status,
                screening_coverage_status=extraction_coverage(repository, study)[0].status,
            )
            for study in studies
        ],
        pagination=Pagination(
            page=page,
            page_size=page_size,
            total_items=total,
            total_pages=math.ceil(total / page_size) if total else 0,
        ),
        data_freshness=DataFreshness(retrieved_at=repository.latest_retrieved_at()),
    )


@router.get(
    "/trials/{nct_id}",
    response_model=StudyDetailResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_trial_detail(
    nct_id: Annotated[str, Path(pattern=r"^NCT\d{8}$")],
    repository: RepositoryDependency,
    refresh: bool = False,
) -> StudyDetailResponse | JSONResponse:
    study = repository.get_study(nct_id)
    if study is None:
        return _error_response(
            status_code=404,
            code="TRIAL_NOT_FOUND",
            message="The requested trial is not present in the local source snapshot.",
            details=[ErrorDetail(field="nct_id", reason="not found")],
            retryable=False,
        )

    retrieval_source: RetrievalSource = "local_database"
    if refresh or study.detail_status != DetailRetrievalStatus.RETRIEVED:
        study, retrieval_source = await _get_or_fetch_detail(
            repository, nct_id, refresh=refresh
        )

    coverage, _ = extraction_coverage(repository, study)
    if study.detail_status == DetailRetrievalStatus.RETRIEVED:
        retrieval_message = (
            "Stored official detail is shown; the latest refresh attempt failed."
            if study.detail_error_code
            else "Official study detail is available."
        )
    elif study.detail_status == DetailRetrievalStatus.RETRIEVAL_FAILED:
        retrieval_message = (
            "Official study detail could not be retrieved right now. Indexed fields are shown, "
            "and missing values remain unknown."
        )
    else:
        retrieval_message = "Official study detail has not yet been retrieved."

    return StudyDetailResponse(
        nct_id=study.nct_id,
        title=study.title,
        overall_status=study.overall_status,
        conditions=study.conditions,
        phases=study.phases,
        source_description=study.source_description,
        brief_summary=study.brief_summary,
        detailed_description=study.detailed_description,
        plain_language_summary=repository.get_current_summary(study.nct_id, study.source_hash),
        eligibility_criteria=study.eligibility_criteria,
        minimum_age=study.minimum_age,
        maximum_age=study.maximum_age,
        sex=study.sex,
        locations=study.locations,
        contacts=study.contacts,
        source_url=study.source_url,
        retrieved_at=study.retrieved_at,
        detail_status=study.detail_status,
        detail_attempted_at=study.detail_attempted_at,
        retrieval_source=retrieval_source,
        retrieval_message=retrieval_message,
        screening_coverage=coverage,
    )


@router.post(
    "/trials/{nct_id}/summary",
    response_model=OnDemandSummaryResponse,
    responses={404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def generate_trial_summary(
    nct_id: Annotated[str, Path(pattern=r"^NCT\d{8}$")],
    repository: RepositoryDependency,
    service: SummaryServiceDependency,
) -> OnDemandSummaryResponse | JSONResponse:
    """Return a validated current-source summary, generating it only on demand."""

    if repository.get_study(nct_id) is None:
        return _error_response(
            status_code=404,
            code="TRIAL_NOT_FOUND",
            message="The requested trial is not present in the local source snapshot.",
            details=[ErrorDetail(field="nct_id", reason="not found")],
            retryable=False,
        )
    try:
        payload, from_cache = await service.summarize_nct_id(nct_id)
    except SummaryError as exc:
        limited = "limit reached" in str(exc).casefold()
        return _error_response(
            status_code=429 if limited else 503,
            code="SUMMARY_RATE_LIMITED" if limited else "AI_SUMMARY_UNAVAILABLE",
            message=(
                "AI summary generation is temporarily rate-limited. The official study description remains available."
                if limited
                else "The AI summary is temporarily unavailable. The official study description remains available."
            ),
            details=[],
            retryable=True,
        )
    return OnDemandSummaryResponse(**payload.model_dump(), from_cache=from_cache)


async def _get_or_fetch_detail(
    repository: ClinicalRepositoryProtocol,
    nct_id: str,
    *,
    refresh: bool,
) -> tuple[Study, RetrievalSource]:
    """Deduplicate concurrent detail requests for one database and NCT identifier."""

    key = (repository.identity, nct_id)
    task = _detail_requests.get(key)
    if task is None:
        task = asyncio.create_task(_fetch_and_store_detail(repository, nct_id, refresh=refresh))
        _detail_requests[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _detail_requests.get(key) is task:
            _detail_requests.pop(key, None)


async def _fetch_and_store_detail(
    repository: ClinicalRepositoryProtocol,
    nct_id: str,
    *,
    refresh: bool,
) -> tuple[Study, RetrievalSource]:
    try:
        async with ClinicalTrialsClient() as client:
            detail = await client.fetch_detail(nct_id, use_cache=not refresh)
        study = normalize_study(
            detail.record,
            detail.retrieved_at,
            detail_status=DetailRetrievalStatus.RETRIEVED,
        )
        repository.upsert_studies([study])
        repository.replace_criteria(
            nct_id,
            "source_structured",
            source_structured_criteria(study, created_at=detail.retrieved_at),
        )
        return study, "official_cache" if detail.from_cache else "official_api"
    except Exception as exc:  # External trust boundary: expose only a safe failure category.
        repository.record_detail_failure(nct_id, datetime.now(UTC), type(exc).__name__[:100])
        fallback = repository.get_study(nct_id)
        if fallback is None:  # The row existed before the request; guard against concurrent deletion.
            raise RuntimeError("trial disappeared during detail retrieval") from None
        return fallback, "local_database"


@router.post(
    "/match",
    response_model=MatchResponse,
    responses={400: {"model": ErrorResponse}},
)
def match_trials(
    request: MatchRequest,
    repository: RepositoryDependency,
) -> MatchResponse | JSONResponse:
    if request.patient.synthetic is not True:
        return _error_response(
            status_code=400,
            code="SYNTHETIC_PROFILE_REQUIRED",
            message="Only explicitly synthetic profiles are accepted.",
            details=[ErrorDetail(field="patient.synthetic", reason="must equal true")],
            retryable=False,
        )
    patient = SyntheticPatient.model_validate(request.patient.model_dump())
    try:
        return MatchingService(repository).match(
            patient,
            trial_ids=request.trial_ids,
            limit=request.limit,
        )
    except UnknownTrialIds as exc:
        return _error_response(
            status_code=404,
            code="TRIALS_NOT_FOUND",
            message="One or more requested trials are not present in the local source snapshot.",
            details=[
                ErrorDetail(field="trial_ids", reason=f"not found: {identifier}")
                for identifier in exc.identifiers
            ],
            retryable=False,
        )


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail],
    retryable: bool,
) -> JSONResponse:
    error = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details,
            retryable=retryable,
            request_id=uuid.uuid4().hex,
        )
    )
    return JSONResponse(status_code=status_code, content=error.model_dump(mode="json"))
