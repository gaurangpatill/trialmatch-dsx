import {
  backendUrl,
  isTrialSearchResponse,
  unavailableResponse,
  upstreamResponse,
} from "../../../lib/server-api";

export const dynamic = "force-dynamic";
export const maxDuration = 35;

const UPSTREAM_TIMEOUT_MS = 30_000;

export async function GET(request: Request): Promise<Response> {
  try {
    const incoming = new URL(request.url);
    const upstream = backendUrl("api/trials");
    incoming.searchParams.forEach((value, key) => upstream.searchParams.append(key, value));
    const response = await fetch(upstream, {
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
    return upstreamResponse(response, await response.text(), isTrialSearchResponse);
  } catch (error) {
    console.error("Clinical trial search proxy failed", error);
    return unavailableResponse("Clinical trial search is temporarily unavailable.");
  }
}
