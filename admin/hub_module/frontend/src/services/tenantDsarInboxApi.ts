import { apiClient } from '../lib/apiClient';
import type { DsarResult } from './tenantPrivacyApi';
import { parseResult } from './tenantPrivacyApi';
import { expectRecord } from '../lib/wire';

/**
 * Typed client for the tenant-admin DSAR inbox (`/api/v1/tenant/<slug>/privacy/requests*`,
 * scope `tenant:admin`, every tier). Enum fields are int codes (see dsarConstants.ts); only UUIDs
 * identify requests, assignees and linked subjects. Cookie-auth via the shared apiClient.
 */

export interface DsarRequest {
  id: string;
  requestType: number;
  status: number;
  regime: number;
  source: number;
  requestedAt: string;
  verifiedAt: string | null;
  dueAt: string | null;
  extendedUntil: string | null;
  effectiveDueAt: string | null;
  extensionReason: string | null;
  dueState: string | null;
  completedAt: string | null;
  expiresAt: string | null;
  assigneeId: string | null;
  subjectUserId: string | null;
  responseNotes: string | null;
  requesterEmail: string | null;
  requesterMessage: string | null;
  contactPurged: boolean;
  unverified: boolean;
}

export interface DsarEvent {
  id: string;
  eventType: number;
  actorKind: number;
  actorId: string | null;
  fromStatus: number | null;
  toStatus: number | null;
  detail: Record<string, unknown> | null;
  createdAt: string;
}

export interface DsarListFilters {
  status?: number;
  type?: number;
  regime?: number;
  dueState?: string;
  assignee?: string;
  sort?: 'requestedAt' | 'dueAt';
  order?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
}

export interface DsarListResponse {
  items: DsarRequest[];
  total: number;
  limit: number;
  offset: number;
}

export interface DsarUpdateBody {
  status?: number;
  assigneeId?: string;
  clearAssignee?: boolean;
  responseNotes?: string;
  /** Link (or, with `subjectOverrideReason`, re-link) the subject user's UUID. */
  subjectUserId?: string;
  /** Required by the server to change an already-linked subject; recorded in the audit trail. */
  subjectOverrideReason?: string;
}

export interface DsarCreateBody {
  requestType: number;
  regime: number;
  requesterEmail?: string;
  subjectUserId?: string;
  notes?: string;
  receivedAt?: string;
}

const base = (slug: string): string => `/api/v1/tenant/${encodeURIComponent(slug)}/privacy/requests`;

/** Drops unset filters so only meaningful query params reach the server (unknown/invalid = 400). */
function cleanParams(filters: DsarListFilters): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== '' && v !== null) out[k] = v as string | number;
  }
  return out;
}

export const tenantDsarInboxApi = {
  /** GET the inbox page; unverified/expired rows only appear when `status` asks for 2 / 8. */
  list: (slug: string, filters: DsarListFilters = {}): Promise<DsarListResponse> =>
    apiClient.get<DsarListResponse>(base(slug), { params: cleanParams(filters) }).then((r) => r.data),

  /** GET one request with its append-only audit trail. */
  get: (slug: string, id: string): Promise<{ request: DsarRequest; events: DsarEvent[] }> =>
    apiClient
      .get<{ request: DsarRequest; events?: DsarEvent[] }>(`${base(slug)}/${encodeURIComponent(id)}`)
      .then((r) => ({ request: r.data.request, events: r.data.events ?? [] })),

  /** POST an out-of-band request (letter, phone...). */
  create: (slug: string, body: DsarCreateBody): Promise<DsarRequest> =>
    apiClient.post<{ request: DsarRequest }>(base(slug), body).then((r) => r.data.request),

  /** PATCH status (4-7), assignee, notes. */
  update: (slug: string, id: string, body: DsarUpdateBody): Promise<DsarRequest> =>
    apiClient.patch<{ request: DsarRequest }>(`${base(slug)}/${encodeURIComponent(id)}`, body).then((r) => r.data.request),

  /** POST the single allowed deadline extension; reason required. */
  extend: (slug: string, id: string, reason: string): Promise<DsarRequest> =>
    apiClient
      .post<{ request: DsarRequest }>(`${base(slug)}/${encodeURIComponent(id)}/extend`, { reason })
      .then((r) => r.data.request),

  /**
   * POST export / erase / do_not_sell for the request's linked subject; erase needs confirm.
   * `overrideReason` is the recorded admin override for an email-mismatched public-form erase.
   */
  act: async (
    slug: string,
    id: string,
    action: 'export' | 'erase' | 'do_not_sell',
    confirm = false,
    overrideReason?: string,
  ): Promise<{ request: DsarRequest; result: DsarResult }> => {
    const payload: { action: string; confirm: boolean; overrideReason?: string } = { action, confirm };
    if (overrideReason) payload.overrideReason = overrideReason;
    const { data } = await apiClient.post<unknown>(`${base(slug)}/${encodeURIComponent(id)}/act`, payload);
    const body = expectRecord(data, 'dsar act response');
    return { request: body['request'] as DsarRequest, result: parseResult(body['result']) };
  },
};
