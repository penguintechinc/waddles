import { isRecord } from '../lib/wire';

/** Page-level state a tier API failure maps to. */
export interface TierErrorState {
  kind: 'exhausted' | 'unavailable' | 'disabled' | 'forbidden' | 'conflict' | 'generic';
  details?: { requiredTier?: number; limit?: number; used?: number } | undefined;
  message?: string | undefined;
}

/** Classifies an axios-style failure: 402 pool full, 503 no team axis, 404 flag off, 403, 409, else generic. */
export function classifyTierError(err: unknown): TierErrorState {
  const response = isRecord(err) && isRecord(err['response']) ? err['response'] : {};
  const status = response['status'];
  const data = isRecord(response['data']) ? response['data'] : {};
  const error = data['error'];
  const code = isRecord(error) ? error['code'] : undefined;
  if (status === 402 || code === 'TEAM_POOL_EXHAUSTED') {
    const details = isRecord(error) && isRecord(error['details']) ? error['details'] : {};
    return { kind: 'exhausted', details: details as TierErrorState['details'] };
  }
  if (status === 503 || code === 'TEAM_POOL_UNAVAILABLE') return { kind: 'unavailable' };
  if (status === 404) return { kind: 'disabled' };
  if (status === 403) return { kind: 'forbidden' };
  if (status === 409) return { kind: 'conflict' };
  const message = typeof error === 'string' ? error : isRecord(error) && typeof error['message'] === 'string' ? error['message'] : undefined;
  return { kind: 'generic', message: message || 'Something went wrong. Please try again.' };
}

/** Renders a pool limit: null = not licensed, -1 = unlimited, otherwise the number. */
export function limitText(limit: number | null | undefined): string {
  if (limit === null || limit === undefined) return 'not licensed';
  if (limit === -1) return 'unlimited';
  return String(limit);
}
