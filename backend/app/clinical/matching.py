"""Preliminary synthetic-patient matching with evidence-linked explanations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

from .database import ClinicalRepository
from .evaluator import evaluate_criterion, missing_question
from .models import (
    CriterionAttribute,
    CriterionClassification,
    CriterionOperator,
    CriterionStatus,
    ExtractionCoverageStatus,
    ExtractionCoverageSummary,
    ExtractionPayload,
    MatchResponse,
    Predicate,
    ScreeningStatus,
    StructuredCriterion,
    Study,
    SyntheticPatient,
    TrialMatchResult,
)


class UnknownTrialIds(ValueError):
    def __init__(self, identifiers: list[str]):
        self.identifiers = identifiers
        super().__init__(f"trials are not present in the local source snapshot: {', '.join(identifiers)}")


class MatchingService:
    def __init__(
        self,
        repository: ClinicalRepository,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def match(
        self,
        patient: SyntheticPatient,
        *,
        trial_ids: list[str] | None = None,
        limit: int = 20,
    ) -> MatchResponse:
        if trial_ids is not None:
            missing = sorted(set(trial_ids) - self.repository.existing_study_ids(trial_ids))
            if missing:
                raise UnknownTrialIds(missing)
            studies = self.repository.get_studies(trial_ids, limit=len(trial_ids))
        else:
            studies = self.repository.get_studies(limit=limit)
        results: list[TrialMatchResult] = []
        for study in studies:
            criteria = self.repository.get_criteria(study.nct_id, source_hash=study.source_hash)
            coverage, coverage_gaps = extraction_coverage(self.repository, study)
            evaluations = [evaluate_criterion(criterion, patient) for criterion in criteria]
            mismatches = [
                evaluation.original_text
                for evaluation in evaluations
                if evaluation.status == CriterionStatus.NOT_MET
            ]
            unresolved = [
                evaluation.original_text
                for evaluation in evaluations
                if evaluation.status == CriterionStatus.UNKNOWN
            ]
            unresolved.extend(coverage_gaps)
            if coverage.status != ExtractionCoverageStatus.COMPLETE:
                unresolved.append(coverage.reason)
            questions = [
                missing_question(criterion)
                for criterion, evaluation in zip(criteria, evaluations, strict=True)
                if evaluation.status == CriterionStatus.UNKNOWN
            ]
            contradictions = find_contradictions(criteria)
            unresolved.extend(contradictions)
            if mismatches:
                status = ScreeningStatus.KNOWN_MISMATCH
            elif (
                coverage.status == ExtractionCoverageStatus.COMPLETE
                and coverage.evaluable_criterion_count > 0
                and evaluations
                and not unresolved
                and all(evaluation.status == CriterionStatus.MET for evaluation in evaluations)
            ):
                status = ScreeningStatus.POTENTIAL_MATCH
            else:
                status = ScreeningStatus.UNRESOLVED
            if not criteria:
                unresolved.append("No current validated structured eligibility criteria are cached for this study.")
                questions.append("Which protocol requirements should study staff review for this profile?")
            results.append(
                TrialMatchResult(
                    nct_id=study.nct_id,
                    title=study.title,
                    screening_status=status,
                    extraction_coverage=coverage,
                    criteria=evaluations,
                    known_mismatches=list(dict.fromkeys(mismatches)),
                    unresolved_requirements=list(dict.fromkeys(unresolved)),
                    missing_questions=list(dict.fromkeys(questions)),
                    source_url=study.source_url,
                )
            )
        return MatchResponse(profile_id=patient.profile_id, results=results, generated_at=self.clock())


def extraction_coverage(
    repository: ClinicalRepository,
    study: Study,
    extraction_state: tuple[str, dict[str, Any] | None, bool, str | None] | None = None,
) -> tuple[ExtractionCoverageSummary, list[str]]:
    state, raw_payload, coverage_verified, verification_method = (
        extraction_state
        if extraction_state is not None
        else repository.get_extraction_state(study.nct_id, study.source_hash)
    )
    if state == "accepted" and raw_payload is not None:
        try:
            payload = ExtractionPayload.model_validate(raw_payload)
        except Exception:
            state = "failed"
        else:
            evaluable = sum(_predicate_is_evaluable(item.predicate) for item in payload.criteria)
            unsupported = len(payload.criteria) - evaluable
            uncovered = payload.coverage.uncovered_items
            declared_status = ExtractionCoverageStatus(payload.coverage.status)
            status = (
                ExtractionCoverageStatus.COMPLETE
                if declared_status == ExtractionCoverageStatus.COMPLETE and coverage_verified
                else ExtractionCoverageStatus.PARTIAL
            )
            if declared_status == ExtractionCoverageStatus.COMPLETE and not coverage_verified:
                reason = (
                    "The provider declared every segmented protocol item accounted for, but no "
                    "independent complete-coverage verification is recorded."
                )
            elif status == ExtractionCoverageStatus.COMPLETE and not unsupported:
                reason = (
                    "Every explicitly enumerated protocol item in the accepted current-source "
                    "coverage record is independently verified and represented by a supported criterion."
                )
            elif status == ExtractionCoverageStatus.COMPLETE:
                reason = (
                    f"Coverage is independently verified for all enumerated protocol items, but "
                    f"{unsupported} requirement(s) are unsupported by deterministic evaluation."
                )
            else:
                reason = (
                    f"Eligibility extraction is partial: {len(uncovered)} explicitly enumerated "
                    "protocol requirement(s) remain unprocessed."
                )
            return (
                ExtractionCoverageSummary(
                    status=status,
                    protocol_item_count=payload.coverage.protocol_item_count,
                    accounted_item_count=payload.coverage.accounted_item_count,
                    evaluable_criterion_count=evaluable,
                    unsupported_protocol_requirement_count=unsupported,
                    uncovered_protocol_requirement_count=len(uncovered),
                    independently_verified=coverage_verified,
                    verification_method=verification_method,
                    reason=reason,
                ),
                [
                    f"Unprocessed protocol requirement: {item.original_text} ({item.reason})"
                    for item in uncovered
                ],
            )
    if state == "failed":
        status = ExtractionCoverageStatus.FAILED
        reason = "The latest current-source extraction attempt failed validation or provider access."
    elif state == "stale":
        status = ExtractionCoverageStatus.STALE
        reason = "Only extraction coverage from an older study source version is available."
    else:
        status = ExtractionCoverageStatus.NOT_ATTEMPTED
        reason = (
            "Detailed eligibility text has not been acquired for this study."
            if not study.eligibility_criteria
            else "No accepted current-source full-protocol extraction coverage record is available."
        )
    return ExtractionCoverageSummary(status=status, reason=reason), []


def _predicate_is_evaluable(predicate: Predicate) -> bool:
    operator = predicate.operator
    if operator == CriterionOperator.UNSUPPORTED:
        return False
    if operator in {CriterionOperator.ALL, CriterionOperator.ANY}:
        return all(_predicate_is_evaluable(child) for child in predicate.children)
    return predicate.attribute != CriterionAttribute.UNSUPPORTED


def find_contradictions(criteria: list[StructuredCriterion]) -> list[str]:
    """Surface only contradictions provable within the supported predicate vocabulary."""

    warnings: list[str] = []
    classifications: dict[str, set[str]] = {}
    texts: dict[str, list[str]] = {}
    for criterion in criteria:
        key = criterion.predicate.model_dump_json(exclude_none=True)
        classifications.setdefault(key, set()).add(criterion.classification.value)
        texts.setdefault(key, []).append(criterion.original_text)
    for key, values in classifications.items():
        if values == {"inclusion", "exclusion"}:
            warnings.append(_warning(texts[key]))

    inclusions = [
        criterion
        for criterion in criteria
        if criterion.classification == CriterionClassification.INCLUSION
    ]
    age_criteria = [
        criterion
        for criterion in inclusions
        if criterion.predicate.attribute == CriterionAttribute.AGE_YEARS
    ]
    if age_criteria and _age_intersection_is_empty(age_criteria):
        warnings.append(_warning([item.original_text for item in age_criteria]))

    sex_values: dict[str, list[str]] = {}
    sex_exclusions: dict[str, list[str]] = {}
    for criterion in inclusions:
        predicate = criterion.predicate
        if predicate.attribute == CriterionAttribute.SEX and isinstance(predicate.value, str):
            if predicate.operator == CriterionOperator.EQUALS:
                sex_values.setdefault(predicate.value.casefold(), []).append(criterion.original_text)
            elif predicate.operator == CriterionOperator.NOT_EQUALS:
                sex_exclusions.setdefault(predicate.value.casefold(), []).append(criterion.original_text)
    if len(sex_values) > 1:
        warnings.append(_warning([text for group in sex_values.values() for text in group]))
    elif len(sex_values) == 1:
        required = next(iter(sex_values))
        if required in sex_exclusions:
            warnings.append(_warning(sex_values[required] + sex_exclusions[required]))

    fact_requirements: dict[tuple[CriterionAttribute, str], dict[CriterionOperator, list[str]]] = {}
    for criterion in inclusions:
        predicate = criterion.predicate
        if predicate.attribute in {
            CriterionAttribute.CONDITION,
            CriterionAttribute.MEDICATION,
            CriterionAttribute.PROCEDURE,
        } and predicate.target and predicate.operator in {
            CriterionOperator.PRESENT,
            CriterionOperator.ABSENT,
        }:
            key = (predicate.attribute, predicate.target.casefold())
            fact_requirements.setdefault(key, {}).setdefault(predicate.operator, []).append(
                criterion.original_text
            )
    for requirements in fact_requirements.values():
        if CriterionOperator.PRESENT in requirements and CriterionOperator.ABSENT in requirements:
            warnings.append(
                _warning(
                    requirements[CriterionOperator.PRESENT]
                    + requirements[CriterionOperator.ABSENT]
                )
            )
    return list(dict.fromkeys(warnings))


def _age_intersection_is_empty(criteria: list[StructuredCriterion]) -> bool:
    lower = float("-inf")
    upper = float("inf")
    lower_inclusive = True
    upper_inclusive = True
    excluded_values: set[float] = set()
    for criterion in criteria:
        predicate = criterion.predicate
        operator = predicate.operator
        value = predicate.value
        if operator == CriterionOperator.BETWEEN:
            assert predicate.lower is not None and predicate.upper is not None
            candidate_lower, candidate_upper = predicate.lower, predicate.upper
            candidate_lower_inclusive = (
                True if predicate.include_lower is None else predicate.include_lower
            )
            candidate_upper_inclusive = (
                True if predicate.include_upper is None else predicate.include_upper
            )
        elif operator == CriterionOperator.EQUALS and isinstance(value, (int, float)):
            candidate_lower = candidate_upper = float(value)
            candidate_lower_inclusive = candidate_upper_inclusive = True
        elif operator in {
            CriterionOperator.GREATER_THAN,
            CriterionOperator.GREATER_THAN_OR_EQUAL,
        } and isinstance(value, (int, float)):
            candidate_lower, candidate_upper = float(value), float("inf")
            candidate_lower_inclusive = operator == CriterionOperator.GREATER_THAN_OR_EQUAL
            candidate_upper_inclusive = True
        elif operator in {
            CriterionOperator.LESS_THAN,
            CriterionOperator.LESS_THAN_OR_EQUAL,
        } and isinstance(value, (int, float)):
            candidate_lower, candidate_upper = float("-inf"), float(value)
            candidate_lower_inclusive = True
            candidate_upper_inclusive = operator == CriterionOperator.LESS_THAN_OR_EQUAL
        elif operator == CriterionOperator.NOT_EQUALS and isinstance(value, (int, float)):
            excluded_values.add(float(value))
            continue
        else:
            continue
        if candidate_lower > lower:
            lower, lower_inclusive = candidate_lower, candidate_lower_inclusive
        elif candidate_lower == lower:
            lower_inclusive = lower_inclusive and candidate_lower_inclusive
        if candidate_upper < upper:
            upper, upper_inclusive = candidate_upper, candidate_upper_inclusive
        elif candidate_upper == upper:
            upper_inclusive = upper_inclusive and candidate_upper_inclusive
    return (
        lower > upper
        or (lower == upper and not (lower_inclusive and upper_inclusive))
        or (lower == upper and lower in excluded_values)
    )


def _warning(evidence: list[str]) -> str:
    return "Potentially contradictory source criteria require review: " + " | ".join(
        dict.fromkeys(evidence)
    )
