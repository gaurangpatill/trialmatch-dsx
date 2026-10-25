import {
  backendUrl,
  isTrialSummaryGenerationResponse,
  unavailableResponse,
  upstreamResponse,
} from "../../../../../lib/server-api";

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
    return upstreamResponse(
      response,
      await response.text(),
      isTrialSummaryGenerationResponse,
    );
  } catch (error) {
    console.error("Clinical trial summary proxy failed", error);
    return unavailableResponse(
      "The AI summary is temporarily unavailable. The official study description remains available.",
    );
  }
}
