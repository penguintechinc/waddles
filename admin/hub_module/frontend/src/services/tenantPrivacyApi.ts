import axios from 'axios';
import { apiClient } from '../lib/apiClient';
import { expectRecord, isRecord, readArray, readNumber, readString } from '../lib/wire';

// Typed client for the tenant-admin DSAR console (hub_api/blueprints/v1/admin_data_privacy.py):
//   GET  /api/v1/tenant/:slug/privacy/users/:uuid/export
//   POST /api/v1/tenant/:slug/privacy/users/:uuid/erase       {confirm: true}
//   PUT  /api/v1/tenant/:slug/privacy/users/:uuid/do-not-sell
//   POST /api/v1/tenant/:slug/privacy/bulk                   {action, userIds, confirm}
// Scope `tenant:admin`; URL slug must equal the JWT tenant. There is no list/history route,
// so request tracking is per-session in the page. Subjects are user UUIDs (`hub_users.uuid`); numeric ids never cross the API (IDOR), never PII.

export type DsarAction = 'export' | 'erase' | 'do_not_sell';
export type DsarStatus =
  | 'completed'
  | 'already_done'
  | 'partial'
  | 'not_found'
  | 'conflict'
  | 'forbidden'
  | 'failed'
  | 'audit_unavailable';

/** Largest bulk batch per action (mirrors `MAX_BULK_USERS` / `MAX_BULK_EXPORT_USERS` server-side). */
export const BULK_LIMITS: Record<DsarAction, number> = { export: 25, erase: 100, do_not_sell: 100 };

export interface DsarResult {
  /** The user's UUID (the only identifier the API returns). */
  userId: string;
  status: DsarStatus;
  detail: string | null;
  data: Record<string, unknown[]> | null;
  incomplete: Array<Record<string, string>> | null;
}

export interface BulkDsarResponse {
  action: DsarAction;
  requested: number;
  succeeded: number;
  failed: number;
  results: DsarResult[];
}

/** True when the status counts as a success (`completed` or `already_done`); `partial` is incomplete. */
export function isSuccessStatus(status: DsarStatus): boolean {
  return status === 'completed' || status === 'already_done';
}

/** Parses one `DsarResultDTO`. */
export function parseResult(raw: unknown): DsarResult {
  const row = expectRecord(raw, 'dsar result');
  const detail = row['detail'];
  const data = row['data'];
  const incomplete = row['incomplete'];
  return {
    userId: readString(row, 'userId', 'dsar result'),
    status: readString(row, 'status', 'dsar result') as DsarStatus,
    detail: typeof detail === 'string' ? detail : null,
    data: isRecord(data) ? (data as Record<string, unknown[]>) : null,
    incomplete: Array.isArray(incomplete) ? (incomplete as Array<Record<string, string>>) : null,
  };
}

function parseSingle(body: unknown): DsarResult {
  return parseResult(expectRecord(body, 'dsar response')['result']);
}

/**
 * Runs a single-user request. The server answers per-user failures (404/409/403/500/503) with a
 * normal `SingleDsarResponse` body, so those are returned as results; route-level errors (no
 * `result` in the body: 400/402/403 scope/tenant) are rethrown for the caller.
 */
async function single(call: () => Promise<{ data: unknown }>): Promise<DsarResult> {
  try {
    const { data } = await call();
    return parseSingle(data);
  } catch (err) {
    if (axios.isAxiosError(err) && isRecord(err.response?.data) && isRecord(err.response?.data['result'])) {
      return parseSingle(err.response?.data);
    }
    throw err;
  }
}

const base = (slug: string): string => `/api/v1/tenant/${encodeURIComponent(slug)}/privacy`;

export const tenantPrivacyApi = {
  /** GET export for one user. */
  exportUser: (slug: string, userId: string): Promise<DsarResult> =>
    single(() => apiClient.get(`${base(slug)}/users/${encodeURIComponent(userId)}/export`)),

  /** POST erase for one user; `confirm` must be true or the server refuses. */
  eraseUser: (slug: string, userId: string, confirm: boolean): Promise<DsarResult> =>
    single(() => apiClient.post(`${base(slug)}/users/${encodeURIComponent(userId)}/erase`, { confirm })),

  /** PUT a Do-Not-Sell opt-out for one user (idempotent). */
  doNotSell: (slug: string, userId: string): Promise<DsarResult> =>
    single(() => apiClient.put(`${base(slug)}/users/${encodeURIComponent(userId)}/do-not-sell`)),

  /** POST one action over many users; always 200 with per-user results. */
  async bulk(slug: string, action: DsarAction, userIds: string[], confirm: boolean): Promise<BulkDsarResponse> {
    const { data } = await apiClient.post<unknown>(`${base(slug)}/bulk`, { action, userIds, confirm });
    const body = expectRecord(data, 'dsar bulk response');
    return {
      action: readString(body, 'action', 'dsar bulk response') as DsarAction,
      requested: readNumber(body, 'requested', 'dsar bulk response'),
      succeeded: readNumber(body, 'succeeded', 'dsar bulk response'),
      failed: readNumber(body, 'failed', 'dsar bulk response'),
      results: readArray(body, 'results', 'dsar bulk response').map(parseResult),
    };
  },
};

/** Extracts `{error:{message}}` text from an axios error, with a fallback. */
export function privacyErrorMessage(err: unknown, fallback: string): string {
  if (axios.isAxiosError(err)) {
    const e = (err.response?.data as { error?: { message?: unknown } } | undefined)?.error;
    if (e && typeof e.message === 'string') return e.message;
  }
  return fallback;
}

/** HTTP status of an axios error, if any. */
export function privacyErrorStatus(err: unknown): number | undefined {
  return axios.isAxiosError(err) ? err.response?.status : undefined;
}
