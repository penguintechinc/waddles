import { apiClient } from '../lib/apiClient';

/**
 * Typed client for the TENANT tier of the App Bundle 3-tier lifecycle --
 * `hub_api/blueprints/v1/bundle_tenant_availability.py`. Controls whether a
 * globally-installed app is AVAILABLE in one tenant's marketplace (the
 * layer above per-community activation, which lives in a separate
 * module-allowlist page). Shapes below mirror that blueprint's dataclasses
 * exactly -- do not add fields it doesn't return.
 */

/** One `bundle_tenant_availability` row -- mirrors the blueprint's `AvailabilityDTO`. */
export interface BundleAvailability {
  appId: string;
  tenantId: number;
  available: boolean;
  pinnedVersionId: number | null;
  updatedAt: string | null;
}

/** `GET /availability` response -- mirrors `AvailabilityListResponse`. */
export interface AvailabilityListResponse {
  success: boolean;
  bundles: BundleAvailability[];
}

/** Generic `{success, message}` response the POST/DELETE routes return. */
export interface MessageResponse {
  success: boolean;
  message: string;
}

/** Request body for `POST /availability` -- mirrors `SetAvailableRequest`. */
export interface SetAvailableRequest {
  appId: string;
  pinnedVersionId?: number | null;
}

/** Standardized `flask_core.api_utils.error_response` envelope. */
export interface ApiErrorResponse {
  success: false;
  error: {
    message: string;
    code: string;
    timestamp: string;
    details?: Record<string, unknown>;
  };
}

export const bundleAvailabilityApi = {
  /** `GET /api/v1/apps/tenant/<tenantSlug>/availability` -- every row, enabled and disabled alike. */
  list: (tenantSlug: string) =>
    apiClient
      .get<AvailabilityListResponse>(`/api/v1/apps/tenant/${tenantSlug}/availability`)
      .then((res) => res.data.bundles),

  /**
   * `POST /api/v1/apps/tenant/<tenantSlug>/availability` -- enable (or
   * re-enable) `appId` for this tenant's marketplace. 409 if `appId` has no
   * current platform-level install.
   */
  enable: (tenantSlug: string, body: SetAvailableRequest) =>
    apiClient
      .post<MessageResponse>(`/api/v1/apps/tenant/${tenantSlug}/availability`, body)
      .then((res) => res.data),

  /**
   * `DELETE /api/v1/apps/tenant/<tenantSlug>/availability/<appId>` -- disable
   * `appId` for this tenant. Cascades: deactivates it in every community of
   * this tenant that currently has it activated.
   */
  disable: (tenantSlug: string, appId: string) =>
    apiClient
      .delete<MessageResponse>(
        `/api/v1/apps/tenant/${tenantSlug}/availability/${encodeURIComponent(appId)}`,
      )
      .then((res) => res.data),
};
