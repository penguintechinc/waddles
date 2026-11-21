import { apiClient } from '../lib/apiClient';

/**
 * Typed clients for the tenant license key (`/api/v1/tenant/<slug>/license`, scope `tenant:admin`)
 * and the operator-managed team pools (`/api/v1/superadmin/tenants/<id>/license-pools`, scope
 * `tenants:admin`). The license key is WRITE-ONLY: no response ever contains it, and this module
 * never logs, stores or echoes it.
 */

export interface LicenseStatus {
  configured: boolean;
  fingerprint: string | null;
  setAt: string | null;
}

export interface OperatorPool {
  tierCode: number;
  /** null = not licensed, -1 = unlimited. */
  limit: number | null;
  source: string;
  observedAt: string;
}

export interface OperatorPoolsResponse {
  success: boolean;
  tenantId: string;
  pools: OperatorPool[];
  /** True only on bypass-domain deployments, where the operator sets pool sizes by hand. */
  bypassDomain: boolean;
}

/** Server rule mirrored from `tenant_license_service._KEY_RE`: 16-256 chars of letters, digits and . _ : - */
export const LICENSE_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,255}$/;

/** Trims and validates a pasted key; returns the key or null when it would be rejected by the server. */
export function normalizeLicenseKey(raw: string): string | null {
  const key = raw.trim();
  return LICENSE_KEY_PATTERN.test(key) ? key : null;
}

const base = (slug: string): string => `/api/v1/tenant/${encodeURIComponent(slug)}/license`;

/** Accepts either `{license: {...}}` (documented) or a bare status object. */
function toStatus(data: unknown): LicenseStatus {
  const d = (data ?? {}) as { license?: Partial<LicenseStatus> } & Partial<LicenseStatus>;
  const s = d.license ?? d;
  return {
    configured: Boolean(s.configured),
    fingerprint: typeof s.fingerprint === 'string' ? s.fingerprint : null,
    setAt: typeof s.setAt === 'string' ? s.setAt : null,
  };
}

export const tenantLicenseApi = {
  /** GET whether a key is configured (fingerprint + set time only). */
  get: (slug: string): Promise<LicenseStatus> => apiClient.get<unknown>(base(slug)).then((r) => toStatus(r.data)),

  /** PUT a new or replacement key. The key goes in the body only and is never echoed back. */
  set: (slug: string, licenseKey: string): Promise<LicenseStatus> =>
    apiClient.put<unknown>(base(slug), { licenseKey }).then((r) => toStatus(r.data)),

  /** DELETE the stored key. */
  remove: (slug: string): Promise<void> => apiClient.delete(base(slug)).then(() => undefined),
};

export const operatorLicensePoolsApi = {
  /** GET stored pool rows for a tenant (public UUID). */
  get: (tenantId: string): Promise<OperatorPoolsResponse> =>
    apiClient.get<OperatorPoolsResponse>(`/api/v1/superadmin/tenants/${tenantId}/license-pools`).then((r) => r.data),

  /** PUT a pool size for tier 2 (Premium); `limit` null clears to not-licensed, -1 is unlimited. */
  set: (tenantId: string, tierCode: 2, limit: number | null): Promise<OperatorPoolsResponse> =>
    apiClient
      .put<OperatorPoolsResponse>(`/api/v1/superadmin/tenants/${tenantId}/license-pools/${tierCode}`, { limit })
      .then((r) => r.data),
};
