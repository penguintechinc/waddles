import { apiClient } from '../lib/apiClient';

/**
 * Typed client for tenant-admin community tier allocation
 * (`/api/v1/tenant/<slug>/license/pool`, `.../communities/tier-allocations`,
 * `.../communities/<community_uuid>/tier`; scope `tenant:admin`). Enum fields are int codes (see
 * communityTierConstants.ts); only community `publicId` UUIDs identify communities. Cookie-auth
 * via the shared apiClient.
 */

export interface PoolTier {
  tier: number;
  /** null = not licensed, -1 = unlimited. */
  limit: number | null;
  used: number;
  available: number | null;
  overage: number;
}

export interface PoolResponse {
  tiers: PoolTier[];
  observedAt: string | null;
  stale: boolean;
  source: string | number | null;
}

export interface TierAllocation {
  communityId: string;
  tier: number;
  source: number | string;
  allocatedAt: string;
  stale: boolean;
}

export interface AllocationsResponse {
  allocations: TierAllocation[];
  meta: { total: number; limit: number; offset: number };
}

export interface SetTierResult {
  communityId: string;
  tier: number;
  previousTier: number;
  changed: boolean;
  used: number;
  limit: number | null;
  overage: number;
}

export interface AllocationFilters {
  tier?: number;
  source?: number | string;
  limit?: number;
  offset?: number;
}

const base = (slug: string): string => `/api/v1/tenant/${encodeURIComponent(slug)}`;

export const tenantCommunityTiersApi = {
  /** GET the tenant's single Premium (tier 2) pool (limit / used / available / overage). */
  getPool: (slug: string): Promise<PoolResponse> =>
    apiClient.get<PoolResponse>(`${base(slug)}/license/pool`).then((r) => r.data),

  /** GET active Premium (tier 2) allocations (Free communities have no row); pass `tier: 2`. */
  listAllocations: (slug: string, filters: AllocationFilters = {}): Promise<AllocationsResponse> => {
    const params: Record<string, string | number> = {};
    for (const [k, v] of Object.entries(filters)) {
      if (v !== undefined && v !== null && v !== '') params[k] = v;
    }
    return apiClient.get<AllocationsResponse>(`${base(slug)}/communities/tier-allocations`, { params }).then((r) => r.data);
  },

  /** PUT a community's tier by its UUID (1 Free releases the slot, 2 Premium; never any other code). */
  setTier: (slug: string, communityPublicId: string, tier: 1 | 2): Promise<SetTierResult> =>
    apiClient
      .put<SetTierResult>(`${base(slug)}/communities/${encodeURIComponent(communityPublicId)}/tier`, { tier })
      .then((r) => r.data),
};
