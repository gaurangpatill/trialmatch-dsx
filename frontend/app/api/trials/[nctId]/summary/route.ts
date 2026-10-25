import {
  backendUrl,
  isTrialSummaryGenerationResponse,
  unavailableResponse,
  upstreamResponse,
} from "../../../../../lib/server-api";

const development = process.env.NODE_ENV !== "production";

const SUMMARY_UNAVAILABLE_MESSAGE =
  "AI explanation is temporarily unavailable. You can still read the official study description.";

export const dynamic = "force-dynamic";

export async function POST(
  _request: Request,
  {params}: {params: Promise<{nctId: string}>},
): Promise<Response> {
  const started = performance.now();
  let nctId = "unknown";
  try {
    ({nctId} = await params);
    if (development) console.info(`clinical_summary nct_id=${nctId} stage=proxy_request elapsed_ms=0 http_status=pending category=none`);
    const response = await fetch(
      backendUrl(`api/trials/${encodeURIComponent(nctId)}/summary`),
      {
        method: "POST",
        cache: "no-store",
        signal: AbortSignal.timeout(150_000),
      },
    );
    const body = await response.text();
    const forwarded = upstreamResponse(
      response,
      body,
      (value) => isTrialSummaryGenerationResponse(value, nctId),
    );
    if (!response.ok && forwarded.status === 502) {
      if (development) console.warn(`clinical_summary nct_id=${nctId} stage=proxy_response elapsed_ms=${Math.round(performance.now() - started)} http_status=503 category=invalid_backend_error`);
      return unavailableResponse(SUMMARY_UNAVAILABLE_MESSAGE);
    }
    if (development) console.info(`clinical_summary nct_id=${nctId} stage=proxy_response elapsed_ms=${Math.round(performance.now() - started)} http_status=${forwarded.status} category=${forwarded.ok ? "usable_response" : "backend_error"}`);
    return forwarded;
  } catch (error) {
    if (development) console.error(`clinical_summary nct_id=${nctId} stage=proxy_failed elapsed_ms=${Math.round(performance.now() - started)} http_status=503 category=${error instanceof Error ? error.name : "unknown"}`);
    return unavailableResponse(SUMMARY_UNAVAILABLE_MESSAGE);
  }
}
