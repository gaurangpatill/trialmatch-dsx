"""Shared FastAPI composition root for clinical and geographic routes."""

import uuid
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.app.clinical.dependencies import (
    ClinicalRepositoryNotReady,
    ClinicalRepositoryUnavailable,
    get_repository,
)
from backend.app.clinical.models import ErrorBody, ErrorDetail, ErrorResponse
from backend.app.clinical.summaries import SummaryError
from backend.app.clinical.router import router as clinical_router
from backend.app.geography.router import (
    GeographyAPIError,
    geography_api_error_handler,
    geography_router,
)


class HealthResponse(BaseModel):
    """Stable health contract for the shared application process."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    service: Literal["trialmatch-api"] = "trialmatch-api"
    version: str


class ClinicalReadiness(BaseModel):
    """Sanitized clinical storage state with no endpoint or credential fields."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["sqlite", "postgresql"] | None = None
    schema_ready: bool
    data_ready: bool
    studies: int = Field(ge=0)


class ReadinessResponse(BaseModel):
    """Stable readiness contract, separate from process liveness."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "not_ready"]
    service: Literal["trialmatch-api"] = "trialmatch-api"
    version: str
    clinical: ClinicalReadiness


app = FastAPI(
    title="TrialMatch API",
    description="Source-grounded TrialMatch research-access prototype API.",
    version="0.1.0",
)

app.add_exception_handler(GeographyAPIError, geography_api_error_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return the coordinated error envelope for request-shape failures."""

    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    details = [
        ErrorDetail(
            field=".".join(str(part) for part in error["loc"] if part not in {"body", "query"}) or None,
            reason=error["msg"],
        )
        for error in exc.errors()
    ]
    payload = ErrorResponse(
        error=ErrorBody(
            code="VALIDATION_ERROR",
            message="Request validation failed.",
            details=details,
            retryable=False,
            request_id=request_id,
        )
    )
    return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))


@app.exception_handler(ClinicalRepositoryUnavailable)
async def clinical_repository_error_handler(
    request: Request, exc: ClinicalRepositoryUnavailable
) -> JSONResponse:
    """Return a fixed 503 envelope without exposing database configuration or errors."""

    not_ready = isinstance(exc, ClinicalRepositoryNotReady)
    payload = ErrorResponse(
        error=ErrorBody(
            code="CLINICAL_STORAGE_NOT_READY" if not_ready else "CLINICAL_STORAGE_UNAVAILABLE",
            message=(
                "Clinical data is not ready."
                if not_ready
                else "Clinical storage is unavailable."
            ),
            details=[],
            retryable=True,
            request_id=request.headers.get("x-request-id") or uuid.uuid4().hex,
        )
    )
    return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))


@app.exception_handler(SummaryError)
async def summary_configuration_error_handler(
    request: Request, _exc: SummaryError
) -> JSONResponse:
    """Sanitize provider configuration failures raised while resolving dependencies."""

    payload = ErrorResponse(
        error=ErrorBody(
            code="AI_SUMMARY_UNAVAILABLE",
            message="The AI summary is temporarily unavailable. The official study description remains available.",
            details=[],
            retryable=True,
            request_id=request.headers.get("x-request-id") or uuid.uuid4().hex,
        )
    )
    return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))


@app.get("/api/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    """Report that the API process is responsive."""

    return HealthResponse(version=app.version)


@app.get(
    "/api/readiness",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
    tags=["system"],
)
def readiness() -> ReadinessResponse | JSONResponse:
    """Report whether the current clinical schema has a non-empty serving snapshot."""

    try:
        stats = get_repository().storage_stats()
        backend_value = stats.get("backend")
        backend = backend_value if backend_value in {"sqlite", "postgresql"} else None
        schema_ready = bool(stats.get("schema_ready"))
        data_ready = bool(stats.get("data_ready"))
        studies = int(stats.get("counts", {}).get("studies", 0))
    except Exception:
        backend = None
        schema_ready = False
        data_ready = False
        studies = 0

    ready = schema_ready and data_ready and studies > 0
    payload = ReadinessResponse(
        status="ready" if ready else "not_ready",
        version=app.version,
        clinical=ClinicalReadiness(
            backend=backend,
            schema_ready=schema_ready,
            data_ready=data_ready,
            studies=studies,
        ),
    )
    if ready:
        return payload
    return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))


app.include_router(clinical_router)
app.include_router(geography_router)
