import {afterEach, describe, expect, it, vi} from "vitest";

import {GET as GET_DETAIL} from "../app/api/trials/[nctId]/route";
import {POST as POST_SUMMARY} from "../app/api/trials/[nctId]/summary/route";
import {POST} from "../app/api/match/route";
import {GET} from "../app/api/trials/route";

describe("clinical API proxies", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("replaces malformed upstream errors with the shared sanitized envelope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("private backend diagnostic", {
        status: 500,
        headers: {"content-type": "text/plain"},
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET(new Request("http://trialmatch.test/api/trials?query=cancer"));
    const body = await response.json();

    expect(response.status).toBe(502);
    expect(body.error.code).toBe("INVALID_UPSTREAM_RESPONSE");
    expect(body.error.retryable).toBe(true);
    expect(JSON.stringify(body)).not.toContain("private backend diagnostic");
    expect(fetchMock.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  });

  it("preserves a valid shared upstream error envelope", async () => {
    const upstream = {
      error: {
        code: "VALIDATION_ERROR",
        message: "Request validation failed.",
        details: [{field: "page", reason: "must be at least 1"}],
        retryable: false,
        request_id: "request-1",
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json(upstream, {status: 422})),
    );

    const response = await GET(new Request("http://trialmatch.test/api/trials?page=0"));

    expect(response.status).toBe(422);
    expect(await response.json()).toEqual(upstream);
  });

  it("sanitizes transport failures and forwards match requests", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("http://private-api:8000 failed")));
    const failed = await POST(
      new Request("http://trialmatch.test/api/match", {
        method: "POST",
        body: JSON.stringify({patient: {profile_id: "synthetic", synthetic: true}}),
      }),
    );
    const failedBody = await failed.json();
    expect(failed.status).toBe(503);
    expect(failedBody.error.retryable).toBe(true);
    expect(JSON.stringify(failedBody)).not.toContain("private-api");

    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        profile_id: "synthetic",
        results: [],
        disclaimer: "Preliminary screening only. Study staff must confirm eligibility.",
        generated_at: "2026-10-24T18:00:00Z",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const succeeded = await POST(
      new Request("http://trialmatch.test/api/match", {
        method: "POST",
        body: JSON.stringify({patient: {profile_id: "synthetic", synthetic: true}}),
      }),
    );
    expect(succeeded.status).toBe(200);
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
    expect(fetchMock.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  });

  it("rejects contract-invalid successful payloads", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json({diagnostic: "private success payload"})),
    );

    const response = await GET(new Request("http://trialmatch.test/api/trials"));
    const body = await response.json();

    expect(response.status).toBe(502);
    expect(body.error.code).toBe("INVALID_UPSTREAM_RESPONSE");
    expect(JSON.stringify(body)).not.toContain("private success payload");
  });

  it("rejects error envelopes carrying extra diagnostics", async () => {
    const upstream = {
      error: {
        code: "INTERNAL_ERROR",
        message: "Failed.",
        details: [],
        retryable: false,
        request_id: "request-2",
        stack: "private stack trace",
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json(upstream, {status: 500})),
    );

    const response = await GET(new Request("http://trialmatch.test/api/trials"));
    const body = await response.json();

    expect(response.status).toBe(502);
    expect(body.error.code).toBe("INVALID_UPSTREAM_RESPONSE");
    expect(JSON.stringify(body)).not.toContain("private stack trace");
  });

  it("accepts coverage-aware match results and rejects missing coverage", async () => {
    const result = {
      nct_id: "NCT00000001",
      title: "Synthetic study",
      screening_status: "unresolved",
      extraction_coverage: {
        status: "partial",
        protocol_item_count: 2,
        accounted_item_count: 1,
        evaluable_criterion_count: 1,
        unsupported_protocol_requirement_count: 0,
        uncovered_protocol_requirement_count: 1,
        independently_verified: false,
        verification_method: null,
        reason: "One source requirement remains uncovered.",
      },
      criteria: [],
      known_mismatches: [],
      unresolved_requirements: ["One source requirement remains uncovered."],
      missing_questions: [],
      source_url: "https://clinicaltrials.gov/study/NCT00000001",
    };
    const payload = {
      profile_id: "synthetic",
      results: [result],
      disclaimer: "Preliminary screening only. Study staff must confirm eligibility.",
      generated_at: "2026-10-24T18:00:00Z",
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(payload))
      .mockResolvedValueOnce(
        Response.json({...payload, results: [{...result, extraction_coverage: undefined}]}),
      );
    vi.stubGlobal("fetch", fetchMock);

    const valid = await POST(new Request("http://trialmatch.test/api/match", {method: "POST", body: "{}"}));
    const invalid = await POST(new Request("http://trialmatch.test/api/match", {method: "POST", body: "{}"}));

    expect(valid.status).toBe(200);
    expect(invalid.status).toBe(502);
  });
  it("accepts source descriptions and cached backend summaries in search results", async () => {
    const item = {
      nct_id: "NCT00000001",
      title: "Synthetic study",
      overall_status: "RECRUITING",
      conditions: ["Example condition"],
      phases: ["NOT_APPLICABLE"],
      source_description: "Official source purpose text.",
      brief_summary: "Official source purpose text.",
      detailed_description: "Full official technical description.",
      plain_language_summary: "Cached source-grounded plain-English summary.",
      minimum_age: "18 Years",
      maximum_age: null,
      sex: "ALL",
      locations: [],
      source_url: "https://clinicaltrials.gov/study/NCT00000001",
      retrieved_at: "2026-10-24T18:00:00Z",
      detail_status: "retrieved",
      screening_coverage_status: "partial",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({
      items: [item],
      pagination: {page: 1, page_size: 20, total_items: 1, total_pages: 1},
      data_freshness: {retrieved_at: "2026-10-24T18:00:00Z", source: "ClinicalTrials.gov"},
    })));

    const response = await GET(new Request("http://trialmatch.test/api/trials"));

    expect(response.status).toBe(200);
    expect((await response.json()).items[0].plain_language_summary).toContain("source-grounded");
  });
});


describe("clinical study detail proxy", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("accepts source-faithful detail and forwards an explicit refresh", async () => {
    const detail = {
      nct_id: "NCT00471978",
      title: "DNA Analysis of Blood and Tissue From Patients With Lung Cancer",
      overall_status: "RECRUITING",
      conditions: ["Lung Cancer"],
      phases: [],
      eligibility_criteria: "Newly diagnosed primary lung cancer",
      minimum_age: "18 Years",
      maximum_age: "120 Years",
      sex: "ALL",
      locations: [{
        facility: "Massachusetts General Hospital",
        recruiting_status: "RECRUITING",
        city: "Boston",
        state: "Massachusetts",
        postal_code: "02114",
        country: "United States",
        latitude: 42.35843,
        longitude: -71.05977,
      }],
      contacts: [{
        name: "Clinical Trials Office",
        role: "CONTACT",
        phone: "000-000-0000",
        email: null,
        facility: "Massachusetts General Hospital",
      }],
      source_url: "https://clinicaltrials.gov/study/NCT00471978",
      retrieved_at: "2026-10-24T19:00:00Z",
      detail_status: "retrieved",
      detail_attempted_at: "2026-10-24T19:00:00Z",
      retrieval_source: "official_api",
      retrieval_message: "Official study detail is available.",
      screening_coverage: {
        status: "partial",
        protocol_item_count: 4,
        accounted_item_count: 3,
        evaluable_criterion_count: 3,
        unsupported_protocol_requirement_count: 0,
        uncovered_protocol_requirement_count: 1,
        independently_verified: false,
        verification_method: null,
        reason: "Eligibility extraction is partial.",
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(Response.json(detail));
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET_DETAIL(
      new Request("http://trialmatch.test/api/trials/NCT00471978?refresh=true"),
      {params: Promise.resolve({nctId: "NCT00471978"})},
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(detail);
    expect(String(fetchMock.mock.calls[0][0])).toContain("refresh=true");
    expect(fetchMock.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  });
});


describe("clinical study summary proxy", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("forwards an on-demand POST and accepts the strict summary contract", async () => {
    const payload = {
      nct_id: "NCT00471978",
      source_hash: "a".repeat(64),
      plain_language_summary: "What is this study investigating? A grounded explanation.",
      from_cache: false,
    };
    const fetchMock = vi.fn().mockResolvedValue(Response.json(payload));
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST_SUMMARY(
      new Request("http://trialmatch.test/api/trials/NCT00471978/summary", {method: "POST"}),
      {params: Promise.resolve({nctId: "NCT00471978"})},
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(payload);
    expect(String(fetchMock.mock.calls[0][0])).toContain("api/trials/NCT00471978/summary");
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
    expect(fetchMock.mock.calls[0][1].cache).toBe("no-store");
    expect(fetchMock.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  });

  it("rejects malformed summary payloads and preserves safe upstream errors", async () => {
    const safeError = {
      error: {
        code: "AI_SUMMARY_UNAVAILABLE",
        message: "AI explanation is temporarily unavailable. You can still read the official study description.",
        details: [],
        retryable: true,
        request_id: "summary-request-1",
      },
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({diagnostic: "private model output"}))
      .mockResolvedValueOnce(Response.json(safeError, {status: 503}));
    vi.stubGlobal("fetch", fetchMock);
    const context = {params: Promise.resolve({nctId: "NCT00471978"})};

    const invalid = await POST_SUMMARY(new Request("http://trialmatch.test", {method: "POST"}), context);
    const unavailable = await POST_SUMMARY(
      new Request("http://trialmatch.test", {method: "POST"}),
      {params: Promise.resolve({nctId: "NCT00471978"})},
    );

    expect(invalid.status).toBe(502);
    expect(JSON.stringify(await invalid.json())).not.toContain("private model output");
    expect(unavailable.status).toBe(503);
    expect(await unavailable.json()).toEqual(safeError);

    fetchMock.mockResolvedValueOnce(
      Response.json({detail: "unstructured backend failure"}, {status: 503}),
    );
    const malformedError = await POST_SUMMARY(
      new Request("http://trialmatch.test", {method: "POST"}),
      {params: Promise.resolve({nctId: "NCT00471978"})},
    );
    const malformedBody = await malformedError.json();
    expect(malformedError.status).toBe(503);
    expect(malformedBody.error.message).toBe(
      "AI explanation is temporarily unavailable. You can still read the official study description.",
    );
    expect(JSON.stringify(malformedBody)).not.toContain("unstructured backend failure");
  });
});
