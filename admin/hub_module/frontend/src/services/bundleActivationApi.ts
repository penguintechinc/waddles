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

export const bundleActivationApi = {
  /** `GET /api/v1/apps/community/<communityId>/activation` -- requires active community membership. */
  list: (communityId: number) =>
    apiClient
      .get<ActivationListResponse>(`/api/v1/apps/community/${communityId}/activation`)
      .then((res) => res.data.bundles),

  /** `POST /api/v1/apps/community/<communityId>/activation` -- requires community-admin membership. */
  activate: (communityId: number, appId: string) =>
    apiClient
      .post<MessageResponse>(`/api/v1/apps/community/${communityId}/activation`, { appId })
      .then((res) => res.data),

  /** `DELETE /api/v1/apps/community/<communityId>/activation/<appId>` -- requires community-admin membership. */
  deactivate: (communityId: number, appId: string) =>
    apiClient
      .delete<MessageResponse>(`/api/v1/apps/community/${communityId}/activation/${appId}`)
      .then((res) => res.data),
};
