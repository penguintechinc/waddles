import { apiClient } from '../lib/apiClient';

// Typed client for the COMMUNITY tier of the App Bundle 3-tier lifecycle --
// mirrors `hub_api/blueprints/v1/bundle_activation.py` exactly (field names,
// casing, status codes). DB activation/bind is the source of truth here;
// this module does NOT touch PostHog `waddles.command-*` runtime flags --
// those are surfaced read-only elsewhere (see `AdminCommunityBundles.tsx`).
//
// `communityId` is a numeric community identifier (never PII) per the
// blueprint's `<int:community_id>` route param.

/** One `app_active_versions` row for a COMMUNITY-tier activation. */
export interface BundleActivation {
  appId: string;
  communityId: number;
  tenantId: number;
  versionId: number;
  /** Activated version string (null when unknown). */
  version?: string | null;
  activatedAt: string | null;
}

interface ActivationListResponse {
  success: boolean;
  bundles: BundleActivation[];
}

interface MessageResponse {
  success: boolean;
  message: string;
}

/** One requested platform-table access block (`hub_api/services/access_grant_dto.py`). */
export interface TableAccess {
  table: string;
  columns: string[];
  ops: string[];
  reason: string;
}

/** One requested cross-prefix object-storage access block. */
export interface StorageAccess {
  prefixOwner: string;
  ops: string[];
  reason: string;
}

/** Dangerous access grants activating an app would carry (`.../activation/<appId>/access-grants`). */
export interface AccessGrants {
  success: boolean;
  requestedIds: string[];
  approvedIds: string[];
  dangerousIds: string[];
  tableAccess: TableAccess[];
  storageAccess?: StorageAccess[];
}

export const bundleActivationApi = {
  /** `GET /api/v1/apps/community/<communityId>/activation` -- requires active community membership. */
  list: (communityId: number) =>
    apiClient
      .get<ActivationListResponse>(`/api/v1/apps/community/${communityId}/activation`)
      .then((res) => res.data.bundles),

  /**
   * `GET /api/v1/apps/community/<communityId>/activation/<appId>/access-grants` -- the dangerous
   * access grants (platform tables, ...) this activation carries; community-admin only.
   */
  accessGrants: (communityId: number, appId: string) =>
    apiClient
      .get<AccessGrants>(
        `/api/v1/apps/community/${communityId}/activation/${encodeURIComponent(appId)}/access-grants`,
      )
      .then((res) => res.data),

  /**
   * `POST /api/v1/apps/community/<communityId>/activation` -- requires community-admin membership.
   * `acknowledgedAccessGrants` must list every approved dangerous access id (else 422).
   */
  activate: (communityId: number, appId: string, acknowledgedAccessGrants: string[] = []) =>
    apiClient
      .post<MessageResponse>(`/api/v1/apps/community/${communityId}/activation`, {
        appId,
        acknowledgedAccessGrants,
      })
      .then((res) => res.data),

  /** `DELETE /api/v1/apps/community/<communityId>/activation/<appId>` -- requires community-admin membership. */
  deactivate: (communityId: number, appId: string) =>
    apiClient
      .delete<MessageResponse>(`/api/v1/apps/community/${communityId}/activation/${appId}`)
      .then((res) => res.data),
};
