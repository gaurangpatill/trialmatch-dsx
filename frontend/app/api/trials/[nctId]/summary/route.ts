import {
  backendUrl,
  isTrialSummaryGenerationResponse,
  unavailableResponse,
  upstreamResponse,
} from "../../../../../lib/server-api";

const SUMMARY_UNAVAILABLE_MESSAGE =
  "AI explanation is temporarily unavailable. You can still read the official study description.";

export const dynamic = "force-dynamic";

export async function POST(
  _request: Request,
  {params}: {params: Promise<{nctId: string}>},
): Promise<Response> {
  try {
    const {nctId} = await params;
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
      isTrialSummaryGenerationResponse,
    );
    if (!response.ok && forwarded.status === 502) {
      return unavailableResponse(SUMMARY_UNAVAILABLE_MESSAGE);
    }
    return forwarded;
  } catch (error) {
    console.error("Clinical trial summary proxy failed", error);
    return unavailableResponse(SUMMARY_UNAVAILABLE_MESSAGE);
  }
}
