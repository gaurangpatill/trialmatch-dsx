"""Validated ClinicalTrials.gov Massachusetts recruiting-site export."""

from __future__ import annotations

import json
import math
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .database import ClinicalRepository
from .models import StrictModel, Study
from .normalize import is_massachusetts_state


SOURCE_COORDINATE_LABEL = "ClinicalTrials.gov API v2 geoPoint"
SOURCE_COORDINATE_REFERENCE = "https://clinicaltrials.gov/data-api/api"


def _normalize(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class TrialSiteRecord(StrictModel):
    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    conditions: list[str]
    facility_source_id: str | None
    facility_name: str
    city: str | None
    state: str
    postal_code: str | None
    country: str | None
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    coordinate_status: Literal["verified", "unresolved"]
    coordinate_source: str | None
    coordinate_source_reference: str | None
    resolution_status: Literal["source_coordinates", "unresolved"]
    overall_status: str
    facility_recruitment_status: Literal["RECRUITING"]
    source_url: str
    retrieved_at: datetime

    @model_validator(mode="after")
    def coordinate_fields_reconcile(self) -> TrialSiteRecord:
        has_pair = self.latitude is not None and self.longitude is not None
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be present together")
        if has_pair != (self.coordinate_status == "verified"):
            raise ValueError("coordinate_status must match coordinate availability")
        if has_pair != (self.resolution_status == "source_coordinates"):
            raise ValueError("resolution_status must match coordinate availability")
        if has_pair and (not self.coordinate_source or not self.coordinate_source_reference):
            raise ValueError("source coordinates require provenance")
        if not has_pair and (self.coordinate_source or self.coordinate_source_reference):
            raise ValueError("unresolved coordinates cannot claim coordinate provenance")
        if not self.facility_name.strip():
            raise ValueError("facility_name must not be blank")
        if not is_massachusetts_state(self.state):
            raise ValueError("site must be explicitly located in Massachusetts")
        return self


class GeographyCompatibleSite(StrictModel):
    """Exact backward-compatible mapping for geo-optimization TrialSite."""

    nct_id: str
    conditions: list[str]
    facility_source_id: str | None
    facility_name: str
    city: str | None
    state: str
    postal_code: str | None
    country: str | None
    latitude: float | None
    longitude: float | None
    coordinate_source: str | None
    coordinate_source_reference: str | None
    resolution_status: Literal["source_coordinates", "unresolved"]
    overall_status: str
    source_url: str
    retrieved_at: datetime


class SiteExportMetadata(StrictModel):
    schema_version: Literal["trialmatch_ma_trial_sites_v1"] = "trialmatch_ma_trial_sites_v1"
    generated_at: datetime
    cohort_selection: str
    source: Literal["ClinicalTrials.gov API v2 individual study records"] = (
        "ClinicalTrials.gov API v2 individual study records"
    )
    source_snapshot_study_count: int = Field(ge=0)
    expanded_study_count: int = Field(ge=0)
    expanded_studies_with_ma_recruiting_sites: int = Field(ge=0)
    source_location_count: int = Field(ge=0)
    explicit_massachusetts_location_count: int = Field(ge=0)
    excluded_non_recruiting_or_unknown_site_status_count: int = Field(ge=0)
    excluded_missing_facility_name_count: int = Field(ge=0)
    duplicate_record_count: int = Field(ge=0)
    exported_record_count: int = Field(ge=0)
    exported_study_count: int = Field(ge=0)
    unique_facility_location_count: int = Field(ge=0)
    resolved_coordinate_count: int = Field(ge=0)
    unresolved_coordinate_count: int = Field(ge=0)
    invalid_source_coordinate_count: int = Field(ge=0)
    earliest_retrieved_at: datetime | None
    latest_retrieved_at: datetime | None
    deduplication_key: str
    limitations: list[str]

    @model_validator(mode="after")
    def producer_counts_reconcile(self) -> SiteExportMetadata:
        if self.expanded_study_count > self.source_snapshot_study_count:
            raise ValueError("expanded study count cannot exceed source snapshot study count")
        if self.explicit_massachusetts_location_count > self.source_location_count:
            raise ValueError("Massachusetts location count cannot exceed all source locations")
        accounted_massachusetts = (
            self.excluded_non_recruiting_or_unknown_site_status_count
            + self.excluded_missing_facility_name_count
            + self.duplicate_record_count
            + self.exported_record_count
        )
        if accounted_massachusetts != self.explicit_massachusetts_location_count:
            raise ValueError("Massachusetts source location stages must reconcile")
        if self.exported_study_count > self.expanded_study_count:
            raise ValueError("exported study count cannot exceed expanded study count")
        if self.resolved_coordinate_count + self.unresolved_coordinate_count != self.exported_record_count:
            raise ValueError("coordinate counts must equal exported records")
        if self.invalid_source_coordinate_count > self.unresolved_coordinate_count:
            raise ValueError("invalid source coordinate count cannot exceed unresolved records")
        return self


class TrialSiteExport(StrictModel):
    metadata: SiteExportMetadata
    records: list[TrialSiteRecord]
    geography_records: list[GeographyCompatibleSite]

    @model_validator(mode="after")
    def counts_reconcile(self) -> TrialSiteExport:
        if self.metadata.exported_record_count != len(self.records):
            raise ValueError("exported_record_count must equal records length")
        if len(self.records) != len(self.geography_records):
            raise ValueError("geography compatibility mapping must be one-to-one")
        if self.metadata.resolved_coordinate_count + self.metadata.unresolved_coordinate_count != len(
            self.records
        ):
            raise ValueError("coordinate counts must equal exported records")
        return self


def build_site_export(
    repository: ClinicalRepository,
    *,
    generated_at: datetime | None = None,
    cohort_selection: str = "All expanded individual study records in the local clinical snapshot",
) -> TrialSiteExport:
    studies = repository.get_studies(limit=100_000)
    candidates: list[TrialSiteRecord] = []
    source_location_count = 0
    ma_location_count = 0
    non_recruiting_count = 0
    missing_facility_count = 0
    invalid_coordinate_count = 0
    expanded_studies = 0
    ma_study_ids: set[str] = set()
    retrieved_times: list[datetime] = []

    for study in studies:
        raw_locations = _raw_locations(study)
        if not raw_locations:
            continue
        expanded_studies += 1
        retrieved_times.append(study.retrieved_at)
        for location in raw_locations:
            source_location_count += 1
            if not is_massachusetts_state(_string(location.get("state"))):
                continue
            ma_location_count += 1
            if _normalize(_string(location.get("status"))) != "recruiting":
                non_recruiting_count += 1
                continue
            facility_name = _string(location.get("facility"))
            if not facility_name or not facility_name.strip():
                missing_facility_count += 1
                continue
            latitude, longitude, invalid_coordinate = _coordinates(location.get("geoPoint"))
            invalid_coordinate_count += int(invalid_coordinate)
            has_coordinates = latitude is not None and longitude is not None
            ma_study_ids.add(study.nct_id)
            candidates.append(
                TrialSiteRecord(
                    nct_id=study.nct_id,
                    conditions=study.conditions,
                    facility_source_id=_string(location.get("facilityId")),
                    facility_name=facility_name.strip(),
                    city=_clean_optional(location.get("city")),
                    state=_string(location.get("state")) or "",
                    postal_code=_clean_optional(location.get("zip")),
                    country=_clean_optional(location.get("country")),
                    latitude=latitude,
                    longitude=longitude,
                    coordinate_status="verified" if has_coordinates else "unresolved",
                    coordinate_source=SOURCE_COORDINATE_LABEL if has_coordinates else None,
                    coordinate_source_reference=(
                        SOURCE_COORDINATE_REFERENCE if has_coordinates else None
                    ),
                    resolution_status="source_coordinates" if has_coordinates else "unresolved",
                    overall_status=study.overall_status,
                    facility_recruitment_status="RECRUITING",
                    source_url=study.source_url,
                    retrieved_at=study.retrieved_at,
                )
            )

    deduplicated: dict[tuple[str, ...], TrialSiteRecord] = {}
    for record in sorted(candidates, key=_sort_key):
        deduplicated.setdefault(_deduplication_key(record), record)
    records = sorted(deduplicated.values(), key=_sort_key)
    geography_records = [
        GeographyCompatibleSite(
            **record.model_dump(
                exclude={"coordinate_status", "facility_recruitment_status"}
            )
        )
        for record in records
    ]
    facility_keys = {
        (
            _normalize(record.facility_name),
            _normalize(record.city),
            _normalize(record.state),
            _normalize(record.postal_code),
            _normalize(record.country),
            "" if record.latitude is None else f"{record.latitude:.8f}",
            "" if record.longitude is None else f"{record.longitude:.8f}",
        )
        for record in records
    }
    resolved_count = sum(record.coordinate_status == "verified" for record in records)
    metadata = SiteExportMetadata(
        generated_at=generated_at or datetime.now(UTC),
        cohort_selection=cohort_selection,
        source_snapshot_study_count=len(studies),
        expanded_study_count=expanded_studies,
        expanded_studies_with_ma_recruiting_sites=len(ma_study_ids),
        source_location_count=source_location_count,
        explicit_massachusetts_location_count=ma_location_count,
        excluded_non_recruiting_or_unknown_site_status_count=non_recruiting_count,
        excluded_missing_facility_name_count=missing_facility_count,
        duplicate_record_count=len(candidates) - len(records),
        exported_record_count=len(records),
        exported_study_count=len({record.nct_id for record in records}),
        unique_facility_location_count=len(facility_keys),
        resolved_coordinate_count=resolved_count,
        unresolved_coordinate_count=len(records) - resolved_count,
        invalid_source_coordinate_count=invalid_coordinate_count,
        earliest_retrieved_at=min(retrieved_times) if retrieved_times else None,
        latest_retrieved_at=max(retrieved_times) if retrieved_times else None,
        deduplication_key=(
            "facility_source_id within NCT when available; otherwise normalized "
            "NCT/facility/city/state/postal/country/coordinate tuple"
        ),
        limitations=[
            "The export covers only studies with expanded individual records in the local snapshot, not all recruiting Massachusetts studies.",
            "Only locations explicitly labeled Massachusetts and site status RECRUITING are included.",
            "coordinate_status verified means the source supplied a valid coordinate pair; TrialMatch did not independently geocode or confirm facility-level precision.",
            "Facilities without a source coordinate pair are preserved as unresolved with null coordinates.",
            "Records without a facility name cannot satisfy the geographic handoff schema and are counted as exclusions.",
        ],
    )
    return TrialSiteExport(metadata=metadata, records=records, geography_records=geography_records)


def write_site_export(export: TrialSiteExport, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = export.model_dump_json(indent=2)
    TrialSiteExport.model_validate_json(payload)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(payload + "\n")
    temporary.replace(destination)


def _raw_locations(study: Study) -> list[dict[str, Any]]:
    value = (
        study.raw_source.get("protocolSection", {})
        .get("contactsLocationsModule", {})
        .get("locations", [])
    )
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _coordinates(value: Any) -> tuple[float | None, float | None, bool]:
    if not isinstance(value, dict):
        return None, None, False
    latitude = value.get("lat")
    longitude = value.get("lon")
    valid = (
        isinstance(latitude, (int, float))
        and not isinstance(latitude, bool)
        and isinstance(longitude, (int, float))
        and not isinstance(longitude, bool)
        and math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )
    if not valid:
        supplied = latitude is not None or longitude is not None
        return None, None, supplied
    return float(latitude), float(longitude), False


def _deduplication_key(record: TrialSiteRecord) -> tuple[str, ...]:
    if record.facility_source_id and record.facility_source_id.strip():
        return ("facility_source_id", record.nct_id, _normalize(record.facility_source_id))
    return (
        "normalized_tuple",
        record.nct_id,
        _normalize(record.facility_name),
        _normalize(record.city),
        _normalize(record.state),
        _normalize(record.postal_code),
        _normalize(record.country),
        "" if record.latitude is None else f"{record.latitude:.8f}",
        "" if record.longitude is None else f"{record.longitude:.8f}",
    )


def _sort_key(record: TrialSiteRecord) -> tuple[str, ...]:
    return (
        record.nct_id,
        _normalize(record.facility_name),
        _normalize(record.city),
        _normalize(record.postal_code),
        "" if record.latitude is None else f"{record.latitude:.8f}",
        "" if record.longitude is None else f"{record.longitude:.8f}",
    )


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _clean_optional(value: Any) -> str | None:
    text = _string(value)
    if text is None:
        return None
    cleaned = text.strip()
    return cleaned or None
