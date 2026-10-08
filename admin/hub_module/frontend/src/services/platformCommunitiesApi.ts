import { apiClient } from '../lib/apiClient';

/**
 * Typed client for `GET/PUT/DELETE /api/v1/platform/communities*`
 * (`hub_api/blueprints/v1/platform.py`). Cross-tenant by design -- this
 * group is gated behind `platform:admin` (super-admin only), see that
 * blueprint's module docstring.
 *
 * NOTE (data-model gap, not guessed around): `communities.tenant_id` is a
 * real, NOT NULL column (`hub_api/services/schema.py`), but
 * `CommunityDTO`/`CommunityDetailDTO` do not expose `tenantId`/
 * `tenantName` yet -- the M3 platform-admin port predates that need.
 * There is therefore no way to group this list by its owning tenant (the
 * 1 tenant -> N communities model) from this endpoint alone today. This
 * module intentionally does NOT fabricate a `tenantId` field. See the PR
 * description for the proposed backend follow-up.
 */

export interface PlatformPaginationDTO {
  page: number;
  limit: number;
  total: number;
  totalPages: number;
}

export interface PlatformCommunityDTO {
  id: number;
  name: string | null;
  displayName: string | null;
  description: string | null;
  primaryPlatform: string | null;
  memberCount: number;
  isPublic: boolean;
  isActive: boolean;
  createdAt: string | null;
}

export interface PlatformCommunityOwnerDTO {
  userId: string;
  displayName: string | null;
  platform: string | null;
}

export interface PlatformCommunityDetailDTO extends PlatformCommunityDTO {
  logoUrl: string | null;
  bannerUrl: string | null;
  owner: PlatformCommunityOwnerDTO | null;
  moduleCount: number;
  domainCount: number;
}

export interface ListPlatformCommunitiesResponse {
  success: boolean;
  communities: PlatformCommunityDTO[];
  pagination: PlatformPaginationDTO;
}

export interface GetPlatformCommunityResponse {
  success: boolean;
  community: PlatformCommunityDetailDTO;
}

export interface MessageResponse {
  success: boolean;
  message: string;
}

export interface ListPlatformCommunitiesParams {
  page?: number;
  limit?: number;
  search?: string;
  /** Backend has no "all" mode -- omitted defaults to `true` server-side. */
  isActive?: boolean;
}

export interface UpdatePlatformCommunityPayload {
  displayName?: string;
  description?: string;
  isPublic?: boolean;
  isActive?: boolean;
}

export interface DeactivatePlatformCommunityPayload {
  reason?: string;
}

export const platformCommunitiesApi = {
  list: (params: ListPlatformCommunitiesParams = {}) =>
    apiClient
      .get<ListPlatformCommunitiesResponse>('/api/v1/platform/communities', {
        params: {
          page: params.page,
          limit: params.limit,
          search: params.search || undefined,
          isActive: params.isActive === undefined ? undefined : String(params.isActive),
        },
      })
      .then((res) => res.data),

  get: (communityId: number) =>
    apiClient
      .get<GetPlatformCommunityResponse>(`/api/v1/platform/communities/${communityId}`)
      .then((res) => res.data),

  update: (communityId: number, payload: UpdatePlatformCommunityPayload) =>
    apiClient
      .put<MessageResponse>(`/api/v1/platform/communities/${communityId}`, payload)
      .then((res) => res.data),

  deactivate: (communityId: number, reason?: string) =>
    apiClient
      .delete<MessageResponse>(`/api/v1/platform/communities/${communityId}`, {
        data: reason ? ({ reason } satisfies DeactivatePlatformCommunityPayload) : {},
      })
      .then((res) => res.data),
};
