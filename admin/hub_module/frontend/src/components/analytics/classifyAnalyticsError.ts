import { getErrorMessage } from '../ui/errors';

/** How the webui should present a failed analytics call. */
export type AnalyticsFailureKind = 'unavailable' | 'feature-disabled' | 'not-found' | 'error';

export interface AnalyticsFailure {
  kind: AnalyticsFailureKind;
  /** HTTP status when the failure came from a response, else undefined (network error). */
  status: number | undefined;
  /** hub-api `error.code` when present. */
  code: string | undefined;
  /** Safe, user-presentable message. Never the raw `service_unavailable` token. */
  message: string;
}

/** `error.code` values hub-api emits for analytics (see docs/analytics-states.md). */
export const CODE_ANALYTICS_UNAVAILABLE = 'ANALYTICS_UNAVAILABLE';
export const CODE_ANALYTICS_UPSTREAM_ERROR = 'ANALYTICS_UPSTREAM_ERROR';
export const CODE_FEATURE_NOT_ENABLED = 'FEATURE_NOT_ENABLED';
export const CODE_ANALYTICS_FEATURE_DISABLED = 'ANALYTICS_FEATURE_DISABLED';

export const UNAVAILABLE_MESSAGE = "Analytics aren't available on this deployment yet.";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/**
 * Classifies anything thrown by an analytics call into one of the three presentations:
 * unavailable (backend not deployed/reachable: neutral info), feature-disabled (402 plan gate:
 * neutral notice), or a real error (shown with its message and a Retry). A 404 is a real error
 * (missing route) unless the caller opts in with `{ notFound: true }` (per-entity pages), where it
 * is reported as `not-found` so the page can decide between "no data" and "unknown entity".
 */
export function classifyAnalyticsError(err: unknown, opts: { notFound?: boolean } = {}): AnalyticsFailure {
  const response = isRecord(err) && isRecord(err['response']) ? err['response'] : undefined;
  const status = typeof response?.['status'] === 'number' ? (response['status'] as number) : undefined;
  const data = isRecord(response?.['data']) ? (response['data'] as Record<string, unknown>) : undefined;
  const envelope = isRecord(data?.['error']) ? (data['error'] as Record<string, unknown>) : undefined;
  const code = typeof envelope?.['code'] === 'string' ? (envelope['code'] as string) : undefined;
  const message = getErrorMessage(err, 'Failed to load analytics.');

  if (code === CODE_ANALYTICS_UNAVAILABLE) return { kind: 'unavailable', status, code, message: UNAVAILABLE_MESSAGE };
  if (status === 402 || code === CODE_FEATURE_NOT_ENABLED || code === CODE_ANALYTICS_FEATURE_DISABLED) {
    return { kind: 'feature-disabled', status, code, message };
  }
  // A bare 502/503 (older hub-api, gateway) is "backend not there"; a coded upstream error is real.
  if ((status === 503 || status === 502) && code !== CODE_ANALYTICS_UPSTREAM_ERROR) {
    return { kind: 'unavailable', status, code, message: UNAVAILABLE_MESSAGE };
  }
  if (status === 404 && opts.notFound) return { kind: 'not-found', status, code, message };
  return { kind: 'error', status, code, message };
}
