const FALLBACK_API_URL = "http://localhost:8000";

type ErrorEnvelope = {
  error: {
    code: string;
    message: string;
    details: Array<{field?: string | null; reason: string}>;
    retryable: boolean;
    request_id: string;
  };
};

type SuccessValidator = (value: unknown) => boolean;

export function backendUrl(path: string): URL {
  const configuredBase =
    process.env.TRIALMATCH_API_BASE_URL ??
    process.env.NEXT_PUBLIC_API_BASE_URL;
  const base = configuredBase ?? FALLBACK_API_URL;
  const origin = new URL(base.endsWith("/") ? base : `${base}/`);
  const isLocalHttp =
    origin.protocol === "http:" && ["localhost", "127.0.0.1", "::1"].includes(origin.hostname);
  if (
    (configuredBase && origin.protocol !== "https:" && !isLocalHttp) ||
    origin.username ||
    origin.password ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash
  ) {
    throw new Error("The clinical API base URL must be an HTTPS origin.");
  }
  return new URL(path, origin);
}

export function upstreamResponse(
  response: Response,
  body: string,
  validateSuccess: SuccessValidator,
): Response {
  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch {
    return invalidUpstreamResponse();
  }
  if (response.ok ? !validateSuccess(payload) : !isErrorEnvelope(payload)) {
    return invalidUpstreamResponse();
  }
  return Response.json(payload, {status: response.status});
}

export function unavailableResponse(message: string): Response {
  return errorResponse("CLINICAL_API_UNAVAILABLE", message, 503, true);
}

export function isTrialSearchResponse(value: unknown): boolean {
  if (!hasExactKeys(value, ["items", "pagination", "data_freshness"])) return false;
  if (!Array.isArray(value.items) || !value.items.every(isTrialSummary)) return false;
  const pagination = value.pagination;
  const freshness = value.data_freshness;
  return (
    hasExactKeys(pagination, ["page", "page_size", "total_items", "total_pages"]) &&
    [pagination.page, pagination.page_size, pagination.total_items, pagination.total_pages].every(
      Number.isInteger,
    ) &&
    hasExactKeys(freshness, ["retrieved_at", "source"]) &&
    (typeof freshness.retrieved_at === "string" || freshness.retrieved_at === null) &&
    freshness.source === "ClinicalTrials.gov"
  );
}

export function isStudyDetailResponse(value: unknown): boolean {
  const baseKeys = [
    "nct_id", "title", "overall_status", "conditions", "phases", "eligibility_criteria",
    "minimum_age", "maximum_age", "sex", "locations", "contacts", "source_url",
    "retrieved_at", "detail_status", "detail_attempted_at", "retrieval_source",
    "retrieval_message", "screening_coverage",
  ] as const;
  const legacySummaryKeys = [...baseKeys, "source_description", "plain_language_summary"] as const;
  const summaryKeys = [...baseKeys, "source_description", "brief_summary", "detailed_description", "plain_language_summary"] as const;
  if (!hasExactKeys(value, baseKeys) && !hasExactKeys(value, legacySummaryKeys) && !hasExactKeys(value, summaryKeys)) return false;
  return (
    [value.nct_id, value.title, value.overall_status, value.source_url, value.retrieved_at,
      value.retrieval_message].every((item) => typeof item === "string") &&
    isStringArray(value.conditions) && isStringArray(value.phases) &&
    [value.eligibility_criteria, value.minimum_age, value.maximum_age, value.sex,
      value.detail_attempted_at, (value as Record<string, unknown>).source_description,
      (value as Record<string, unknown>).brief_summary,
      (value as Record<string, unknown>).detailed_description,
      (value as Record<string, unknown>).plain_language_summary].every(
        (item) => typeof item === "string" || item === null || item === undefined,
      ) &&
    Array.isArray(value.locations) && value.locations.every(isTrialLocation) &&
    Array.isArray(value.contacts) && value.contacts.every(isTrialContact) &&
    ["not_retrieved", "retrieved", "retrieval_failed"].includes(value.detail_status as string) &&
    ["local_database", "official_cache", "official_api"].includes(value.retrieval_source as string) &&
    isExtractionCoverage(value.screening_coverage)
  );
}

export function isTrialSummaryGenerationResponse(value: unknown, expectedNctId?: string): boolean {
  return (
    hasExactKeys(value, ["nct_id", "source_hash", "plain_language_summary", "from_cache"]) &&
    typeof value.nct_id === "string" &&
    /^NCT\d{8}$/.test(value.nct_id) &&
    (expectedNctId === undefined || value.nct_id === expectedNctId) &&
    typeof value.source_hash === "string" &&
    /^[a-f0-9]{64}$/.test(value.source_hash) &&
    typeof value.plain_language_summary === "string" &&
    value.plain_language_summary.trim().length > 0 &&
    typeof value.from_cache === "boolean"
  );
}

export function isMatchResponse(value: unknown): boolean {
  if (!hasExactKeys(value, ["profile_id", "results", "disclaimer", "generated_at"])) return false;
  return (
    typeof value.profile_id === "string" &&
    Array.isArray(value.results) &&
    value.results.every(isTrialMatchResult) &&
    value.disclaimer === "Preliminary screening only. Study staff must confirm eligibility." &&
    typeof value.generated_at === "string"
  );
}

function isTrialSummary(value: unknown): boolean {
  const baseKeys = [
    "nct_id", "title", "overall_status", "conditions", "phases", "minimum_age",
    "maximum_age", "sex", "locations", "source_url", "retrieved_at", "detail_status",
    "screening_coverage_status",
  ] as const;
  const legacySummaryKeys = [...baseKeys, "source_description", "plain_language_summary"] as const;
  const summaryKeys = [...baseKeys, "source_description", "brief_summary", "detailed_description", "plain_language_summary"] as const;
  if (!hasExactKeys(value, baseKeys) && !hasExactKeys(value, legacySummaryKeys) && !hasExactKeys(value, summaryKeys)) return false;
  return (
    [value.nct_id, value.title, value.overall_status, value.source_url, value.retrieved_at].every(
      (item) => typeof item === "string",
    ) &&
    isStringArray(value.conditions) &&
    isStringArray(value.phases) &&
    [value.minimum_age, value.maximum_age, value.sex,
      (value as Record<string, unknown>).source_description,
      (value as Record<string, unknown>).brief_summary,
      (value as Record<string, unknown>).detailed_description,
      (value as Record<string, unknown>).plain_language_summary].every(
      (item) => typeof item === "string" || item === null || item === undefined,
    ) &&
    Array.isArray(value.locations) &&
    value.locations.every(isTrialLocation) &&
    ["not_retrieved", "retrieved", "retrieval_failed"].includes(value.detail_status as string) &&
    ["not_attempted", "partial", "complete", "failed", "stale"].includes(
      value.screening_coverage_status as string,
    )
  );
}

function isTrialLocation(value: unknown): boolean {
  if (
    !hasExactKeys(value, [
      "facility",
      "city",
      "state",
      "postal_code",
      "country",
      "latitude",
      "longitude",
      "recruiting_status",
    ])
  ) return false;
  return (
    [value.facility, value.city, value.state, value.postal_code, value.country, value.recruiting_status].every(
      (item) => typeof item === "string" || item === null,
    ) &&
    [value.latitude, value.longitude].every(
      (item) => typeof item === "number" || item === null,
    )
  );
}

function isTrialContact(value: unknown): boolean {
  if (!hasExactKeys(value, ["name", "role", "phone", "email", "facility"])) return false;
  return [value.name, value.role, value.phone, value.email, value.facility].every(
    (item) => typeof item === "string" || item === null,
  );
}

function isTrialMatchResult(value: unknown): boolean {
  if (
    !hasExactKeys(value, [
      "nct_id",
      "title",
      "screening_status",
      "extraction_coverage",
      "criteria",
      "known_mismatches",
      "unresolved_requirements",
      "missing_questions",
      "source_url",
    ])
  ) return false;
  return (
    [value.nct_id, value.title, value.source_url].every((item) => typeof item === "string") &&
    ["potential_match", "known_mismatch", "unresolved"].includes(
      value.screening_status as string,
    ) &&
    isExtractionCoverage(value.extraction_coverage) &&
    Array.isArray(value.criteria) &&
    value.criteria.every(isCriterionEvaluation) &&
    isStringArray(value.known_mismatches) &&
    isStringArray(value.unresolved_requirements) &&
    isStringArray(value.missing_questions)
  );
}

function isExtractionCoverage(value: unknown): boolean {
  if (
    !hasExactKeys(value, [
      "status",
      "protocol_item_count",
      "accounted_item_count",
      "evaluable_criterion_count",
      "unsupported_protocol_requirement_count",
      "uncovered_protocol_requirement_count",
      "independently_verified",
      "verification_method",
      "reason",
    ])
  ) return false;
  return (
    ["not_attempted", "partial", "complete", "failed", "stale"].includes(
      value.status as string,
    ) &&
    (value.protocol_item_count === null || Number.isInteger(value.protocol_item_count)) &&
    [
      value.accounted_item_count,
      value.evaluable_criterion_count,
      value.unsupported_protocol_requirement_count,
      value.uncovered_protocol_requirement_count,
    ].every(Number.isInteger) &&
    typeof value.independently_verified === "boolean" &&
    (value.verification_method === null || typeof value.verification_method === "string") &&
    typeof value.reason === "string"
  );
}

function isCriterionEvaluation(value: unknown): boolean {
  if (
    !hasExactKeys(value, [
      "criterion_id",
      "classification",
      "status",
      "original_text",
      "explanation",
      "source_url",
      "extraction_uncertainty",
    ])
  ) return false;
  return (
    [value.criterion_id, value.original_text, value.explanation, value.source_url].every(
      (item) => typeof item === "string",
    ) &&
    ["inclusion", "exclusion"].includes(value.classification as string) &&
    ["met", "not_met", "unknown"].includes(value.status as string) &&
    (typeof value.extraction_uncertainty === "string" || value.extraction_uncertainty === null)
  );
}

function invalidUpstreamResponse(): Response {
  return errorResponse(
    "INVALID_UPSTREAM_RESPONSE",
    "The clinical API returned an invalid response.",
    502,
    true,
  );
}

function errorResponse(
  code: string,
  message: string,
  status: number,
  retryable: boolean,
): Response {
  const body: ErrorEnvelope = {
    error: {
      code,
      message,
      details: [],
      retryable,
      request_id: crypto.randomUUID(),
    },
  };
  return Response.json(body, {status});
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (!hasExactKeys(value, ["error"])) return false;
  const error = value.error;
  if (!hasExactKeys(error, ["code", "message", "details", "retryable", "request_id"])) {
    return false;
  }
  return (
    typeof error.code === "string" &&
    typeof error.message === "string" &&
    Array.isArray(error.details) &&
    error.details.every(
      (detail) =>
        hasAllowedKeys(detail, ["field", "reason"], ["reason"]) &&
        typeof detail.reason === "string" &&
        (detail.field === undefined || detail.field === null || typeof detail.field === "string"),
    ) &&
    typeof error.retryable === "boolean" &&
    typeof error.request_id === "string" &&
    error.request_id.length > 0
  );
}

function hasExactKeys<T extends string>(
  value: unknown,
  keys: readonly T[],
): value is Record<T, unknown> {
  return isRecord(value) && Object.keys(value).length === keys.length && keys.every((key) => key in value);
}

function hasAllowedKeys<T extends string>(
  value: unknown,
  allowed: readonly T[],
  required: readonly T[],
): value is Record<T, unknown> {
  return (
    isRecord(value) &&
    Object.keys(value).every((key) => allowed.includes(key as T)) &&
    required.every((key) => key in value)
  );
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
