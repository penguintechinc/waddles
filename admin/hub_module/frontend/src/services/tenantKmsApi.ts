import { apiClient } from '../lib/apiClient';

/**
 * Typed client for the tenant external-KMS (BYOK) routes in
 * `hub_api/blueprints/v1/tenant_kms.py` (`/api/v1/tenant/<slug>/kms*`, scope
 * `compliance.kms:admin`). DTOs carry identifiers and counts only; this client
 * never handles key material, and nothing here is logged.
 */

export interface KmsConfig {
  provider: string;
  keyRef: string;
  region: string | null;
  principal: string | null;
  /** Proof-of-control token the customer puts in their key trust policy. */
  externalId: string;
  status: string;
  lastVerifiedAt: string | null;
  lastErrorCode: string | null;
  updatedAt: string | null;
}

export interface KmsDek {
  dekVersion: number;
  kekKind: string;
  kekRef: string;
  status: string;
  usageCount: number;
  activatedAt: string | null;
  retiredAt: string | null;
}

export interface KmsRewrap {
  targetKind: string;
  targetRef: string;
  total: number;
  rewrapped: number;
  alreadyCurrent: number;
  failedVersions: number[];
  ok: boolean;
}

export interface KmsStatus {
  success: boolean;
  config: KmsConfig | null;
  keys: KmsDek[];
  supportedProviders: string[];
  platformPrincipals: Record<string, string>;
}

export interface KmsConfigRequest {
  provider: string;
  keyRef: string;
  region?: string | null;
  principal?: string | null;
}

const base = (slug: string) => `/api/v1/tenant/${encodeURIComponent(slug)}/kms`;

export const tenantKmsApi = {
  /** `GET /kms` -- config + no-secret DEK summary; never entitlement-gated. */
  getStatus: (slug: string) => apiClient.get<KmsStatus>(base(slug)).then((r) => r.data),
  /** `PUT /kms` -- record the customer key as `pending` (Enterprise). */
  configure: (slug: string, body: KmsConfigRequest) =>
    apiClient.put<{ success: boolean; config: KmsConfig }>(base(slug), body).then((r) => r.data),
  /** `POST /kms/activate` -- verify the key and re-wrap every DEK onto it (Enterprise). */
  activate: (slug: string) =>
    apiClient
      .post<{ success: boolean; config: KmsConfig; rewrap: KmsRewrap }>(`${base(slug)}/activate`)
      .then((r) => r.data),
  /** `DELETE /kms` -- exit ramp back to the platform key (never entitlement-gated). */
  remove: (slug: string) =>
    apiClient.delete<{ success: boolean; rewrap: KmsRewrap }>(base(slug)).then((r) => r.data),
};

/** Stable error `code` from hub-api's `{ error: { code, message } }` envelope, or null. */
export function kmsErrorCode(err: unknown): string | null {
  const data = (err as { response?: { data?: { error?: { code?: unknown } } } })?.response?.data;
  const code = data?.error?.code;
  return typeof code === 'string' ? code : null;
}

/** True when the API refused because the tenant lacks the Enterprise KMS entitlement. */
export function isNotEntitled(err: unknown): boolean {
  const status = (err as { response?: { status?: number } })?.response?.status;
  return status === 403 && kmsErrorCode(err) === 'EXTERNAL_KMS_NOT_ENTITLED';
}

/** Show only the tail of a key identifier (e.g. an ARN) so the full reference is never rendered. */
export function maskKeyRef(ref: string): string {
  if (!ref) return '';
  return ref.length <= 8 ? '****' : `****${ref.slice(-6)}`;
}
