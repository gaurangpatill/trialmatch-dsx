export type DetailRetrievalStatus = "not_retrieved" | "retrieved" | "retrieval_failed";

export type TrialLocation = {
  facility: string | null;
  recruiting_status: string | null;
  city: string | null;
  state: string | null;
  postal_code: string | null;
  country: string | null;
  latitude: number | null;
  longitude: number | null;
};

export type TrialContact = {
  name: string | null;
  role: string | null;
  phone: string | null;
  email: string | null;
  facility: string | null;
};

export type ExtractionCoverageStatus =
  | "not_attempted"
  | "partial"
  | "complete"
  | "failed"
  | "stale";

export type ExtractionCoverage = {
  status: ExtractionCoverageStatus;
  protocol_item_count: number | null;
  accounted_item_count: number;
  evaluable_criterion_count: number;
  unsupported_protocol_requirement_count: number;
  uncovered_protocol_requirement_count: number;
  independently_verified: boolean;
  verification_method: string | null;
  reason: string;
};

export type TrialSummary = {
  nct_id: string;
  title: string;
  overall_status: string;
  conditions: string[];
  phases: string[];
  source_description?: string | null;
  brief_summary?: string | null;
  detailed_description?: string | null;
  plain_language_summary?: string | null;
  minimum_age: string | null;
  maximum_age: string | null;
  sex: string | null;
  locations: TrialLocation[];
  source_url: string;
  retrieved_at: string;
  detail_status: DetailRetrievalStatus;
  screening_coverage_status: ExtractionCoverageStatus;
};

export type TrialDetail = {
  nct_id: string;
  title: string;
  overall_status: string;
  conditions: string[];
  phases: string[];
  eligibility_criteria: string | null;
  source_description?: string | null;
  brief_summary?: string | null;
  detailed_description?: string | null;
  plain_language_summary?: string | null;
  minimum_age: string | null;
  maximum_age: string | null;
  sex: string | null;
  locations: TrialLocation[];
  contacts: TrialContact[];
  source_url: string;
  retrieved_at: string;
  detail_status: DetailRetrievalStatus;
  detail_attempted_at: string | null;
  retrieval_source: "local_database" | "official_cache" | "official_api";
  retrieval_message: string;
  screening_coverage: ExtractionCoverage;
};

export type TrialSummaryGenerationResponse = {
  nct_id: string;
  source_hash: string;
  plain_language_summary: string;
  from_cache: boolean;
};

export type TrialSearchResponse = {
  items: TrialSummary[];
  pagination: {
    page: number;
    page_size: number;
    total_items: number;
    total_pages: number;
  };
  data_freshness: {
    retrieved_at: string | null;
    source: "ClinicalTrials.gov";
  };
};

export type CriterionStatus = "met" | "not_met" | "unknown";
export type ScreeningStatus = "potential_match" | "known_mismatch" | "unresolved";

export type MatchResponse = {
  profile_id: string;
  results: Array<{
    nct_id: string;
    title: string;
    screening_status: ScreeningStatus;
    extraction_coverage: ExtractionCoverage;
    criteria: Array<{
      criterion_id: string;
      classification: "inclusion" | "exclusion";
      status: CriterionStatus;
      original_text: string;
      explanation: string;
      source_url: string;
      extraction_uncertainty: string | null;
    }>;
    known_mismatches: string[];
    unresolved_requirements: string[];
    missing_questions: string[];
    source_url: string;
  }>;
  disclaimer: string;
  generated_at: string;
};

export type ApiError = {
  error?: {
    code?: string;
    message?: string;
    details?: Array<{field?: string | null; reason?: string}>;
    retryable?: boolean;
    request_id?: string;
  };
};
