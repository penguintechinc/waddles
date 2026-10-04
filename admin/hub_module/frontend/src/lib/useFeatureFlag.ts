import { useQuery } from '@tanstack/react-query';
import { apiClient } from './apiClient';

/**
 * Resolved-flags hook (S0 foundation, wired in a follow-up) -- backed by the
 * hub-api `GET /api/v1/flags` proxy (`hub_api/blueprints/v1/flags.py`), which
 * resolves a curated `CLIENT_FLAG_KEYS` allowlist through the same
 * `EntitlementClient`/PostHog path every other `waddles.<module>.<feature>`
 * gate in this service uses. Per client.md Authentication & Tokens, no
 * PostHog key ever lives in this bundle -- the backend is the only thing
 * that talks to PostHog/the license server.
 *
 * Fetched via TanStack Query (shared `apiClient`/`queryClient`, same pattern
 * as every other server-state read in this app) and cached for
 * `FLAGS_STALE_TIME_MS` so every `useFeatureFlag()` call site on a page
 * shares one network request instead of one per flag.
 *
 * Per house rule (unseen flag = OFF), every path below defaults to `false`:
 * while the query is loading, if the request fails (network error, 401,
 * tenant mismatch, etc.), and for any key absent from the response map
 * (e.g. not in hub-api's `CLIENT_FLAG_KEYS` allowlist). This hook never
 * throws and never blocks render.
 */

type FlagKey = `${string}.${string}`;

interface ResolvedFlagsResponse {
  flags: Record<string, boolean>;
}

const FLAGS_QUERY_KEY = ['feature-flags'] as const;
const FLAGS_STALE_TIME_MS = 60 * 1000;

/**
 * Fetches the resolved flag map for the authenticated tenant. Never throws --
 * any failure (network, auth, server error) is caught and logged at debug
 * (sanitized: status/message only, never response body/headers/cookies) and
 * resolved as an empty map, which every call site turns into `false` via
 * `?? false`.
 */
async function fetchResolvedFlags(): Promise<Record<string, boolean>> {
  try {
    const response = await apiClient.get<ResolvedFlagsResponse>('/api/v1/flags');
    return response.data.flags ?? {};
  } catch (error) {
    const message = error instanceof Error ? error.message : 'unknown error';
    console.debug('[useFeatureFlag] Fetch failed', { message });
    return {};
  }
}

/**
 * Returns whether a PostHog-backed feature flag is enabled for the current
 * tenant. Defaults to `false` while loading, on fetch error, and for any key
 * not present in the resolved map -- absence is never treated as "on".
 */
export function useFeatureFlag(key: FlagKey): boolean {
  const { data } = useQuery({
    queryKey: FLAGS_QUERY_KEY,
    queryFn: fetchResolvedFlags,
    staleTime: FLAGS_STALE_TIME_MS,
  });

  return data?.[key] ?? false;
}
