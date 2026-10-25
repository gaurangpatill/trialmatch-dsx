"""Validated source, domain, API, and eligibility models."""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    """Base model for structures controlled by TrialMatch."""

    model_config = ConfigDict(extra="forbid")


class SourcePage(BaseModel):
    """Minimal validation for a ClinicalTrials.gov page envelope."""

    model_config = ConfigDict(extra="ignore")

    studies: list[dict[str, Any]]
    next_page_token: str | None = Field(default=None, alias="nextPageToken")


class SourceIdentification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    nct_id: str = Field(alias="nctId", pattern=r"^NCT\d{8}$")
    brief_title: str | None = Field(default=None, alias="briefTitle")
    official_title: str | None = Field(default=None, alias="officialTitle")


class SourceDateStruct(BaseModel):
    """ClinicalTrials.gov date structure preserved at source precision."""

    model_config = ConfigDict(extra="ignore")

    date: str | None = None
    date_type: str | None = Field(default=None, alias="type")


class SourceStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    overall_status: str | None = Field(default=None, alias="overallStatus")
    status_verified_date: str | None = Field(default=None, alias="statusVerifiedDate")
    last_update_submit_date: str | None = Field(default=None, alias="lastUpdateSubmitDate")
    last_update_post_date_struct: SourceDateStruct | None = Field(
        default=None, alias="lastUpdatePostDateStruct"
    )


class SourceDescription(BaseModel):
    model_config = ConfigDict(extra="ignore")

    brief_summary: str | None = Field(default=None, alias="briefSummary")
    detailed_description: str | None = Field(default=None, alias="detailedDescription")


class SourceConditions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conditions: list[str] = Field(default_factory=list)


class SourceDesign(BaseModel):
    model_config = ConfigDict(extra="ignore")

    phases: list[str] = Field(default_factory=list)
    study_type: str | None = Field(default=None, alias="studyType")


class SourceIntervention(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intervention_type: str | None = Field(default=None, alias="type")
    name: str | None = None
    description: str | None = None
    other_names: list[str] = Field(default_factory=list, alias="otherNames")


class SourceArmsInterventions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    interventions: list[SourceIntervention] = Field(default_factory=list)


class SourceEligibility(BaseModel):
    model_config = ConfigDict(extra="ignore")

    eligibility_criteria: str | None = Field(default=None, alias="eligibilityCriteria")
    minimum_age: str | None = Field(default=None, alias="minimumAge")
    maximum_age: str | None = Field(default=None, alias="maximumAge")
    sex: str | None = None


class SourceGeoPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    latitude: float | None = Field(default=None, alias="lat")
    longitude: float | None = Field(default=None, alias="lon")


class SourceContact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    role: str | None = None
    phone: str | None = None
    email: str | None = None


class SourceLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    facility: str | None = None
    status: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = Field(default=None, alias="zip")
    country: str | None = None
    geo_point: SourceGeoPoint | None = Field(default=None, alias="geoPoint")
    contacts: list[SourceContact] = Field(default_factory=list)


class SourceContactsLocations(BaseModel):
    model_config = ConfigDict(extra="ignore")

    central_contacts: list[SourceContact] = Field(default_factory=list, alias="centralContacts")
    locations: list[SourceLocation] = Field(default_factory=list)


class SourceProtocol(BaseModel):
    model_config = ConfigDict(extra="ignore")

    identification: SourceIdentification = Field(alias="identificationModule")
    status: SourceStatus = Field(default_factory=SourceStatus, alias="statusModule")
    description: SourceDescription = Field(
        default_factory=SourceDescription, alias="descriptionModule"
    )
    conditions: SourceConditions = Field(default_factory=SourceConditions, alias="conditionsModule")
    design: SourceDesign = Field(default_factory=SourceDesign, alias="designModule")
    arms_interventions: SourceArmsInterventions = Field(
        default_factory=SourceArmsInterventions, alias="armsInterventionsModule"
    )
    eligibility: SourceEligibility = Field(default_factory=SourceEligibility, alias="eligibilityModule")
    contacts_locations: SourceContactsLocations = Field(
        default_factory=SourceContactsLocations, alias="contactsLocationsModule"
    )


class SourceStudy(BaseModel):
    """Supported source fields; unknown official fields are retained separately as raw JSON."""

    model_config = ConfigDict(extra="ignore")

    protocol: SourceProtocol = Field(alias="protocolSection")


class DetailRetrievalStatus(str, Enum):
    NOT_RETRIEVED = "not_retrieved"
    RETRIEVED = "retrieved"
    RETRIEVAL_FAILED = "retrieval_failed"


class StudyLocation(StrictModel):
    facility: str | None = None
    recruiting_status: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class StudyContact(StrictModel):
    name: str | None = None
    role: str | None = None
    phone: str | None = None
    email: str | None = None
    facility: str | None = None


class StudyIntervention(StrictModel):
    """Official intervention metadata without inferred classifications."""

    intervention_type: str | None = None
    name: str | None = None
    description: str | None = None
    other_names: list[str] = Field(default_factory=list)


class Study(StrictModel):
    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    title: str = Field(min_length=1)
    overall_status: str = Field(min_length=1)
    conditions: list[str] = Field(default_factory=list)
    phases: list[str] = Field(default_factory=list)
    source_last_updated: str | None = None
    status_verified_date: str | None = None
    study_type: str | None = None
    interventions: list[StudyIntervention] = Field(default_factory=list)
    source_description: str | None = None
    brief_summary: str | None = None
    detailed_description: str | None = None
    eligibility_criteria: str | None = None
    minimum_age: str | None = None
    maximum_age: str | None = None
    sex: str | None = None
    locations: list[StudyLocation] = Field(default_factory=list)
    contacts: list[StudyContact] = Field(default_factory=list)
    source_url: str
    retrieved_at: datetime
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_source: dict[str, Any]
    detail_status: DetailRetrievalStatus = DetailRetrievalStatus.NOT_RETRIEVED
    detail_attempted_at: datetime | None = None
    detail_error_code: str | None = None

    @model_validator(mode="before")
    @classmethod
    def restore_internal_source_metadata(cls, value: Any) -> Any:
        """Restore non-column metadata from the persisted official raw payload."""

        if not isinstance(value, dict) or not isinstance(value.get("raw_source"), dict):
            return value
        restored = dict(value)
        protocol = restored["raw_source"].get("protocolSection", {})
        if not isinstance(protocol, dict):
            return restored
        status = protocol.get("statusModule", {})
        design = protocol.get("designModule", {})
        arms = protocol.get("armsInterventionsModule", {})
        if isinstance(status, dict):
            posted = status.get("lastUpdatePostDateStruct")
            restored.setdefault(
                "source_last_updated",
                posted.get("date") if isinstance(posted, dict) else status.get("lastUpdateSubmitDate"),
            )
            restored.setdefault("status_verified_date", status.get("statusVerifiedDate"))
        if isinstance(design, dict):
            restored.setdefault("study_type", design.get("studyType"))
        if isinstance(arms, dict) and isinstance(arms.get("interventions"), list):
            restored.setdefault(
                "interventions",
                [
                    {
                        "intervention_type": item.get("type"),
                        "name": item.get("name"),
                        "description": item.get("description"),
                        "other_names": item.get("otherNames", []),
                    }
                    for item in arms["interventions"]
                    if isinstance(item, dict)
                ],
            )
        return restored


class PlainLanguageSummaryPayload(StrictModel):
    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plain_language_summary: str = Field(min_length=1, max_length=8000)

    @field_validator("plain_language_summary")
    @classmethod
    def summary_must_not_be_blank(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("plain_language_summary must not be blank")
        return cleaned


class OnDemandSummaryResponse(PlainLanguageSummaryPayload):
    """Validated generated summary plus cache provenance for the requesting client."""

    from_cache: bool


class DetailCacheEnvelope(StrictModel):
    schema_version: Literal["trialmatch_ctg_detail_v2"] = "trialmatch_ctg_detail_v2"
    retrieved_at: datetime
    source_url: str
    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    expected_overall_status: str | None = None
    expected_last_update: str | None = None
    index_record_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    detail_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    study: dict[str, Any]


class IngestionCheckpoint(StrictModel):
    schema_version: Literal["trialmatch_ctg_ingestion_checkpoint_v1"] = (
        "trialmatch_ctg_ingestion_checkpoint_v1"
    )
    cohort_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    query_url: str
    expected_ids: list[str]
    completed_detail_hashes: dict[str, str] = Field(default_factory=dict)
    started_at: datetime
    updated_at: datetime
    publication_completed: bool = False

    @model_validator(mode="after")
    def checkpoint_ids_reconcile(self) -> IngestionCheckpoint:
        if self.expected_ids != sorted(set(self.expected_ids)):
            raise ValueError("checkpoint expected_ids must be sorted and unique")
        if not set(self.completed_detail_hashes).issubset(self.expected_ids):
            raise ValueError("checkpoint completed IDs must belong to the expected cohort")
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in self.completed_detail_hashes.values()
        ):
            raise ValueError("checkpoint detail hashes must be lowercase SHA-256 values")
        return self


class IngestionStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class IngestionReport(StrictModel):
    source_url: str
    effective_page_size: int = Field(ge=1, le=1000)
    pagination_completed: bool
    started_at: datetime
    index_retrieved_at: datetime | None = None
    published_at: datetime | None = None
    completed_at: datetime
    status: IngestionStatus
    page_count: int = Field(ge=0)
    raw_record_count: int = Field(ge=0)
    accepted_record_count: int = Field(ge=0)
    duplicate_identifier_count: int = Field(ge=0)
    missing_value_counts: dict[str, int]
    excluded_records: list[dict[str, str]]
    errors: list[str]
    detail_requested_count: int = Field(ge=0)
    detail_retrieved_count: int = Field(ge=0)
    index_retrieved_count: int = Field(default=0, ge=0)
    detail_cache_hit_count: int = Field(default=0, ge=0)
    detail_network_count: int = Field(default=0, ge=0)
    detail_resumed_count: int = Field(default=0, ge=0)
    detail_failed_count: int = Field(default=0, ge=0)
    incomplete_detail_ids: list[str] = Field(default_factory=list)
    ma_verified_count: int = Field(default=0, ge=0)
    ma_unverified_count: int = Field(default=0, ge=0)
    ma_unverified_records: list[dict[str, str]] = Field(default_factory=list)
    request_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    rate_limited_count: int = Field(default=0, ge=0)
    cohort_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    checkpoint_resumed: bool = False
    publication_attempted: bool = False
    publication_completed: bool = False
    published_record_count: int = Field(default=0, ge=0)
    previous_snapshot_preserved: bool = False

    @model_validator(mode="after")
    def reliability_counts_reconcile(self) -> IngestionReport:
        if self.detail_cache_hit_count + self.detail_network_count != self.detail_retrieved_count:
            raise ValueError("cache and network detail counts must equal retrieved details")
        if self.detail_resumed_count > self.detail_cache_hit_count:
            raise ValueError("resumed details must be validated cache hits")
        if self.detail_failed_count != len(self.incomplete_detail_ids):
            raise ValueError("failed detail count must equal incomplete detail IDs")
        if self.ma_unverified_count != len(self.ma_unverified_records):
            raise ValueError("MA unverified count must equal MA unverified records")
        if self.publication_completed:
            if self.index_retrieved_at is None or self.published_at is None:
                raise ValueError(
                    "completed publication requires index retrieval and publication timestamps"
                )
            if self.published_at < self.index_retrieved_at:
                raise ValueError("publication timestamp cannot precede index retrieval")
            if self.status != IngestionStatus.COMPLETE:
                raise ValueError("completed publication requires complete status")
            if self.published_record_count != self.accepted_record_count:
                raise ValueError("complete publication must contain the accepted cohort")
        else:
            if self.published_at is not None:
                raise ValueError("incomplete publication cannot have a publication timestamp")
            if self.published_record_count:
                raise ValueError("incomplete publication cannot report published records")
        return self


class CriterionClassification(str, Enum):
    INCLUSION = "inclusion"
    EXCLUSION = "exclusion"


class CriterionStatus(str, Enum):
    MET = "met"
    NOT_MET = "not_met"
    UNKNOWN = "unknown"


class ScreeningStatus(str, Enum):
    POTENTIAL_MATCH = "potential_match"
    KNOWN_MISMATCH = "known_mismatch"
    UNRESOLVED = "unresolved"


class CriterionAttribute(str, Enum):
    AGE_YEARS = "age_years"
    SEX = "sex"
    CONDITION = "condition"
    MEDICATION = "medication"
    PROCEDURE = "procedure"
    LABORATORY_VALUE = "laboratory_value"
    UNSUPPORTED = "unsupported"


class CriterionOperator(str, Enum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    PRESENT = "present"
    ABSENT = "absent"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    BETWEEN = "between"
    WITHIN_DAYS = "within_days"
    ALL = "all"
    ANY = "any"
    UNSUPPORTED = "unsupported"


Scalar = StrictStr | StrictInt | StrictFloat | StrictBool


class Predicate(StrictModel):
    attribute: CriterionAttribute
    operator: CriterionOperator
    target: str | None = None
    value: Scalar | None = None
    lower: float | None = None
    upper: float | None = None
    include_lower: bool | None = None
    include_upper: bool | None = None
    unit: str | None = None
    temporal_window_days: int | None = Field(default=None, gt=0)
    children: list[Predicate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operator_shape(self) -> Predicate:
        compound = {CriterionOperator.ALL, CriterionOperator.ANY}
        scalar = {
            CriterionOperator.EQUALS,
            CriterionOperator.NOT_EQUALS,
            CriterionOperator.GREATER_THAN,
            CriterionOperator.GREATER_THAN_OR_EQUAL,
            CriterionOperator.LESS_THAN,
            CriterionOperator.LESS_THAN_OR_EQUAL,
        }
        numeric = scalar | {CriterionOperator.BETWEEN}
        facts = {CriterionOperator.PRESENT, CriterionOperator.ABSENT, CriterionOperator.WITHIN_DAYS}

        numeric_values = [
            item
            for item in (self.value, self.lower, self.upper)
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        ]
        if any(not math.isfinite(item) for item in numeric_values):
            raise ValueError("numeric predicate values and bounds must be finite")

        if self.operator != CriterionOperator.BETWEEN and (
            self.include_lower is not None or self.include_upper is not None
        ):
            raise ValueError("include_lower/include_upper are allowed only for between")

        if self.operator in compound:
            if len(self.children) < 2:
                raise ValueError("compound predicates require at least two children")
            if self.attribute != CriterionAttribute.UNSUPPORTED:
                raise ValueError("compound predicates must use the unsupported container attribute")
            if any(
                value is not None
                for value in (self.target, self.value, self.lower, self.upper, self.unit, self.temporal_window_days)
            ):
                raise ValueError("compound predicates cannot contain scalar fields")
            return self
        if self.children:
            raise ValueError("only compound predicates may contain children")

        if self.attribute == CriterionAttribute.UNSUPPORTED or self.operator == CriterionOperator.UNSUPPORTED:
            if self.attribute != CriterionAttribute.UNSUPPORTED or self.operator != CriterionOperator.UNSUPPORTED:
                raise ValueError("unsupported predicates require both unsupported attribute and operator")
            if any(
                value is not None
                for value in (self.target, self.value, self.lower, self.upper, self.unit, self.temporal_window_days)
            ):
                raise ValueError("unsupported predicates cannot contain evaluable fields")
            return self

        if self.operator == CriterionOperator.BETWEEN:
            if self.lower is None or self.upper is None:
                raise ValueError("between requires lower and upper")
            if self.lower > self.upper:
                raise ValueError("between lower cannot exceed upper")
            if self.value is not None:
                raise ValueError("between cannot also contain value")
        elif self.lower is not None or self.upper is not None:
            raise ValueError("lower and upper are allowed only for between")

        if self.attribute == CriterionAttribute.AGE_YEARS:
            if self.operator not in numeric:
                raise ValueError("age_years requires a numeric comparison operator")
            if self.target is not None or self.temporal_window_days is not None:
                raise ValueError("age_years cannot contain target or temporal fields")
            if self.unit not in {None, "years"}:
                raise ValueError("age_years unit must be years")
        elif self.attribute == CriterionAttribute.SEX:
            if self.operator not in {CriterionOperator.EQUALS, CriterionOperator.NOT_EQUALS}:
                raise ValueError("sex requires equals or not_equals")
            if not isinstance(self.value, str):
                raise ValueError("sex comparison value must be a string")
            if any(value is not None for value in (self.target, self.lower, self.upper, self.unit, self.temporal_window_days)):
                raise ValueError("sex cannot contain numeric, target, unit, or temporal fields")
        elif self.attribute in {
            CriterionAttribute.CONDITION,
            CriterionAttribute.MEDICATION,
            CriterionAttribute.PROCEDURE,
        }:
            if self.operator not in facts:
                raise ValueError(f"{self.attribute.value} requires a fact operator")
            if not self.target:
                raise ValueError(f"{self.attribute.value} predicates require target")
            if any(value is not None for value in (self.value, self.lower, self.upper, self.unit)):
                raise ValueError("fact predicates cannot contain scalar comparison fields")
            if self.operator == CriterionOperator.WITHIN_DAYS:
                if self.temporal_window_days is None:
                    raise ValueError("within_days requires temporal_window_days")
            elif self.temporal_window_days is not None:
                raise ValueError("temporal_window_days is allowed only for within_days")
        elif self.attribute == CriterionAttribute.LABORATORY_VALUE:
            if self.operator not in numeric:
                raise ValueError("laboratory_value requires a numeric comparison operator")
            if not self.target or not self.unit:
                raise ValueError("laboratory_value requires target and unit")
            if self.temporal_window_days is not None:
                raise ValueError("laboratory_value temporal comparison is unsupported")

        if self.operator in scalar:
            if self.value is None:
                raise ValueError(f"{self.operator.value} requires value")
            if self.attribute in {CriterionAttribute.AGE_YEARS, CriterionAttribute.LABORATORY_VALUE} and (
                not isinstance(self.value, (int, float)) or isinstance(self.value, bool)
            ):
                raise ValueError("numeric predicates require a numeric comparison value")
        return self


class StructuredCriterion(StrictModel):
    criterion_id: str = Field(min_length=1)
    source_item_id: str | None = None
    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    classification: CriterionClassification
    original_text: str = Field(min_length=1)
    predicate: Predicate
    source_url: str
    extraction_uncertainty: str | None = None
    extraction_method: Literal["source_structured", "llm", "test_fixture"]
    schema_version: str = "1.0"
    provider: str | None = None
    model: str | None = None
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime


class ExtractionCoverageStatus(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"
    STALE = "stale"


class CoverageGap(StrictModel):
    source_item_id: str = Field(pattern=r"^item-\d{3,}$")
    original_text: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ExtractionCoverageDeclaration(StrictModel):
    status: Literal["partial", "complete"]
    protocol_item_count: int = Field(ge=0)
    accounted_item_count: int = Field(ge=0)
    uncovered_items: list[CoverageGap] = Field(default_factory=list)


class ExtractionCandidate(StrictModel):
    source_item_id: str = Field(pattern=r"^item-\d{3,}$")
    classification: CriterionClassification
    original_text: str = Field(min_length=1)
    predicate: Predicate
    uncertainty: str | None = None


class ExtractionPayload(StrictModel):
    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    coverage: ExtractionCoverageDeclaration
    criteria: list[ExtractionCandidate]

    @model_validator(mode="after")
    def coverage_counts_reconcile(self) -> ExtractionPayload:
        if self.coverage.accounted_item_count != len(self.criteria):
            raise ValueError("accounted_item_count must equal criteria length")
        if self.coverage.protocol_item_count != len(self.criteria) + len(
            self.coverage.uncovered_items
        ):
            raise ValueError("protocol_item_count must reconcile criteria and uncovered items")
        if self.coverage.status == "complete" and self.coverage.uncovered_items:
            raise ValueError("complete coverage cannot contain uncovered items")
        if self.coverage.status == "partial" and not self.coverage.uncovered_items:
            raise ValueError("partial coverage requires explicit uncovered items")
        item_ids = [item.source_item_id for item in self.criteria] + [
            item.source_item_id for item in self.coverage.uncovered_items
        ]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("source_item_id values must be unique")
        return self


class PatientFacts(StrictModel):
    age_years: float | None = Field(default=None, ge=0, le=125)
    sex: str | None = None
    conditions: list[str] = Field(default_factory=list)
    medications: list[str] = Field(default_factory=list)
    procedures: list[str] = Field(default_factory=list)
    absent_conditions: list[str] = Field(default_factory=list)
    absent_medications: list[str] = Field(default_factory=list)
    absent_procedures: list[str] = Field(default_factory=list)
    condition_days_ago: dict[str, StrictFloat | StrictInt] = Field(default_factory=dict)
    medication_days_ago: dict[str, StrictFloat | StrictInt] = Field(default_factory=dict)
    procedure_days_ago: dict[str, StrictFloat | StrictInt] = Field(default_factory=dict)
    laboratory_values: dict[str, StrictFloat | StrictInt] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "conditions",
        "medications",
        "procedures",
        "absent_conditions",
        "absent_medications",
        "absent_procedures",
    )
    @classmethod
    def unique_normalized_facts(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = value.strip()
            if not cleaned:
                raise ValueError("facts cannot be blank")
            key = cleaned.casefold()
            if key not in seen:
                seen.add(key)
                result.append(cleaned)
        return result

    @field_validator("condition_days_ago", "medication_days_ago", "procedure_days_ago")
    @classmethod
    def validate_days_ago(
        cls, values: dict[str, float | int]
    ) -> dict[str, float | int]:
        cleaned: dict[str, float | int] = {}
        normalized_keys: set[str] = set()
        for key, value in values.items():
            cleaned_key = key.strip()
            normalized = cleaned_key.casefold()
            if not cleaned_key:
                raise ValueError("dated fact names cannot be blank")
            if normalized in normalized_keys:
                raise ValueError("dated fact names must be unique ignoring case")
            if not math.isfinite(value) or value < 0:
                raise ValueError("days-ago values must be finite and non-negative")
            normalized_keys.add(normalized)
            cleaned[cleaned_key] = value
        return cleaned

    @field_validator("laboratory_values")
    @classmethod
    def validate_laboratory_values(
        cls, values: dict[str, float | int]
    ) -> dict[str, float | int]:
        if any(not math.isfinite(value) for value in values.values()):
            raise ValueError("laboratory values must be finite")
        return values

    @model_validator(mode="after")
    def reject_contradictory_facts(self) -> PatientFacts:
        for name in ("condition", "medication", "procedure"):
            present = {value.casefold() for value in getattr(self, f"{name}s")}
            absent = {value.casefold() for value in getattr(self, f"absent_{name}s")}
            conflicts = present & absent
            if conflicts:
                raise ValueError(f"facts cannot be both present and absent: {sorted(conflicts)}")
            dated = {value.casefold() for value in getattr(self, f"{name}_days_ago")}
            if not dated <= present:
                raise ValueError(
                    f"dated {name} facts must also be explicitly present: {sorted(dated - present)}"
                )
        return self


class SyntheticPatient(PatientFacts):
    profile_id: str = Field(min_length=1, max_length=100)
    synthetic: Literal[True]


class PatientRequest(PatientFacts):
    profile_id: str = Field(min_length=1, max_length=100)
    synthetic: StrictBool


class MatchRequest(StrictModel):
    patient: PatientRequest
    trial_ids: list[str] | None = Field(default=None, max_length=100)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("trial_ids")
    @classmethod
    def validate_trial_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if len(values) != len(set(values)):
            raise ValueError("trial_ids must be unique")
        for value in values:
            if not value.startswith("NCT") or len(value) != 11 or not value[3:].isdigit():
                raise ValueError(f"invalid NCT identifier: {value}")
        return values


class StudySummary(StrictModel):
    nct_id: str
    title: str
    overall_status: str
    conditions: list[str]
    phases: list[str]
    source_description: str | None
    brief_summary: str | None
    detailed_description: str | None
    plain_language_summary: str | None
    minimum_age: str | None
    maximum_age: str | None
    sex: str | None
    locations: list[StudyLocation]
    source_url: str
    retrieved_at: datetime
    detail_status: DetailRetrievalStatus
    screening_coverage_status: ExtractionCoverageStatus


class Pagination(StrictModel):
    page: int
    page_size: int
    total_items: int
    total_pages: int


class DataFreshness(StrictModel):
    retrieved_at: datetime | None
    source: Literal["ClinicalTrials.gov"] = "ClinicalTrials.gov"


class TrialSearchResponse(StrictModel):
    items: list[StudySummary]
    pagination: Pagination
    data_freshness: DataFreshness


class CriterionEvaluation(StrictModel):
    criterion_id: str
    classification: CriterionClassification
    status: CriterionStatus
    original_text: str
    explanation: str
    source_url: str
    extraction_uncertainty: str | None


class ExtractionCoverageSummary(StrictModel):
    status: ExtractionCoverageStatus
    protocol_item_count: int | None = Field(default=None, ge=0)
    accounted_item_count: int = Field(default=0, ge=0)
    evaluable_criterion_count: int = Field(default=0, ge=0)
    unsupported_protocol_requirement_count: int = Field(default=0, ge=0)
    uncovered_protocol_requirement_count: int = Field(default=0, ge=0)
    independently_verified: bool = False
    verification_method: str | None = None
    reason: str


class StudyDetailResponse(StrictModel):
    nct_id: str
    title: str
    overall_status: str
    conditions: list[str]
    phases: list[str]
    source_description: str | None
    brief_summary: str | None
    detailed_description: str | None
    plain_language_summary: str | None
    eligibility_criteria: str | None
    minimum_age: str | None
    maximum_age: str | None
    sex: str | None
    locations: list[StudyLocation]
    contacts: list[StudyContact]
    source_url: str
    retrieved_at: datetime
    detail_status: DetailRetrievalStatus
    detail_attempted_at: datetime | None
    retrieval_source: Literal["local_database", "official_cache", "official_api"]
    retrieval_message: str
    screening_coverage: ExtractionCoverageSummary


class TrialMatchResult(StrictModel):
    nct_id: str
    title: str
    screening_status: ScreeningStatus
    extraction_coverage: ExtractionCoverageSummary
    criteria: list[CriterionEvaluation]
    known_mismatches: list[str]
    unresolved_requirements: list[str]
    missing_questions: list[str]
    source_url: str


class MatchResponse(StrictModel):
    profile_id: str
    results: list[TrialMatchResult]
    disclaimer: Literal["Preliminary screening only. Study staff must confirm eligibility."] = (
        "Preliminary screening only. Study staff must confirm eligibility."
    )
    generated_at: datetime


class ErrorDetail(StrictModel):
    field: str | None = None
    reason: str


class ErrorBody(StrictModel):
    code: str
    message: str
    details: list[ErrorDetail]
    retryable: bool
    request_id: str


class ErrorResponse(StrictModel):
    error: ErrorBody
