import axios from 'axios';
import { apiClient } from '../lib/apiClient';

// Typed client for the generic bundle-admin gateway -- mirrors
// `hub_api/blueprints/v1/bundle_admin_gateway.py` (field names, casing,
// status codes). hub-api owns ALL validation; this module only moves data.

/** One admin tool the bundle manifest declares, and whether the caller may use it. */
export interface BundleAdminTool {
  name: string;
  kind: string;
  scope: string;
  permitted: boolean;
  targetAppId: string;
}

/** `GET .../admin` -- the bundle's declared config schema + admin tools. */
export interface BundleDescriptor {
  success: boolean;
  appId: string;
  version: string;
  configSchema: Record<string, unknown> | null;
  tools: BundleAdminTool[];
}

/** `GET/PUT .../config` -- latest saved config; `version` 0 means never saved. */
export interface BundleConfig {
  success: boolean;
  appId: string;
  version: number;
  config: Record<string, unknown>;
  updatedBy: number | null;
  createdAt: string | null;
}

/** One saved config version (metadata only). */
export interface BundleConfigHistoryEntry {
  version: number;
  updatedBy: number | null;
  createdAt: string | null;
}

/** One value-free schema violation hub-api reports on a 422 (`$.a.b` JSON path + rule). */
export interface ConfigFieldError {
  path: string;
  rule: string;
}

/** Normalized gateway failure: HTTP status + hub-api error code + optional details. */
export class BundleAdminError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | undefined;

  /** Wraps one non-2xx gateway response so the UI can branch on status/code. */
  constructor(status: number, code: string, message: string, details?: Record<string, unknown>) {
    super(message);
    this.name = 'BundleAdminError';
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** The 422 `{path, rule}` list, or an empty list for any other failure. */
  get fieldErrors(): ConfigFieldError[] {
    const errors = this.details?.errors;
    return Array.isArray(errors) ? (errors as ConfigFieldError[]) : [];
  }
}

/** Error code hub-api returns when a bundle declares no `community_config_schema`. */
export const NO_CONFIG_CODE = 'bundle_has_no_config';

interface ErrorEnvelope {
  error?: { message?: string; code?: string; details?: Record<string, unknown> };
}

/** Rethrows an axios failure as a `BundleAdminError`; anything else propagates unchanged. */
function rethrow(err: unknown): never {
  if (axios.isAxiosError(err) && err.response) {
    const body = (err.response.data ?? {}) as ErrorEnvelope;
    throw new BundleAdminError(
      err.response.status,
      body.error?.code ?? `ERROR_${err.response.status}`,
      body.error?.message ?? err.message,
      body.error?.details,
    );
  }
  throw err;
}

const base = (communityId: number, appId: string) =>
  `/api/v1/apps/community/${communityId}/bundles/${encodeURIComponent(appId)}`;

export const bundleAdminApi = {
  /** `GET .../admin` -- community moderator+. */
  descriptor: (communityId: number, appId: string) =>
    apiClient
      .get<BundleDescriptor>(`${base(communityId, appId)}/admin`)
      .then((res) => res.data)
      .catch(rethrow),

  /** `GET .../config` -- community moderator+; 404 `bundle_has_no_config` when schema-less. */
  getConfig: (communityId: number, appId: string) =>
    apiClient
      .get<BundleConfig>(`${base(communityId, appId)}/config`)
      .then((res) => res.data)
      .catch(rethrow),

  /** `PUT .../config` -- community admin; 409 on a stale `expectedVersion`, 422 on schema errors. */
  saveConfig: (communityId: number, appId: string, config: Record<string, unknown>, expectedVersion: number) =>
    apiClient
      .put<BundleConfig>(`${base(communityId, appId)}/config`, { config, expectedVersion })
      .then((res) => res.data)
      .catch(rethrow),

  /** `GET .../config/history` -- newest first, metadata only. */
  history: (communityId: number, appId: string, limit = 20) =>
    apiClient
      .get<{ success: boolean; versions: BundleConfigHistoryEntry[] }>(
        `${base(communityId, appId)}/config/history`,
        { params: { limit } },
      )
      .then((res) => res.data.versions)
      .catch(rethrow),
};
