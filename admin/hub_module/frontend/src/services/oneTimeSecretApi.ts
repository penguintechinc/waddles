import { apiClient } from '../lib/apiClient';

/** Response of `POST /api/v1/one-time-secrets/pull` (served `Cache-Control: no-store`). */
interface PullResponse {
  secret: string;
}

/**
 * Typed client for one-time secret retrieval (#684/#718). The token travels
 * in the POST body only -- the API never accepts it in a URL. The returned
 * secret must never be persisted or logged by callers.
 */
export const oneTimeSecretApi = {
  /** Pulls (and thereby hard-deletes) the secret behind `token`. */
  pull: (token: string) =>
    apiClient
      .post<PullResponse>(
        '/api/v1/one-time-secrets/pull',
        { token },
        { headers: { 'Cache-Control': 'no-store' } },
      )
      .then((res) => res.data.secret),
};

/** HTTP status of an axios-style error, or `undefined` for network/unknown errors. */
export function pullErrorStatus(err: unknown): number | undefined {
  if (typeof err !== 'object' || err === null || !('response' in err)) return undefined;
  return (err as { response?: { status?: number } }).response?.status;
}
