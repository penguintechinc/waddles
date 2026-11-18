/** Wire shapes for the tenant community tier API and the shared tier constants. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../lib/apiClient', () => ({ apiClient: { get: vi.fn(), put: vi.fn() } }));
import { apiClient } from '../../lib/apiClient';
import { REASON_CODES, REASON_LABEL, TIER_LABEL, normalizeTier, tierLabel } from '../communityTierConstants';
import { tenantCommunityTiersApi } from '../tenantCommunityTiersApi';

const get = vi.mocked(apiClient.get);
const put = vi.mocked(apiClient.put);
beforeEach(() => vi.clearAllMocks());

describe('tenantCommunityTiersApi', () => {
  it('getPool hits license/pool under the encoded slug', async () => {
    get.mockResolvedValue({ data: { tiers: [] } });
    await tenantCommunityTiersApi.getPool('a b');
    expect(get).toHaveBeenCalledWith('/api/v1/tenant/a%20b/license/pool');
  });

  it('listAllocations drops unset filters', async () => {
    get.mockResolvedValue({ data: { allocations: [], meta: { total: 0, limit: 10, offset: 0 } } });
    await tenantCommunityTiersApi.listAllocations('acme', { tier: 2, limit: 10, offset: 0 });
    expect(get).toHaveBeenCalledWith('/api/v1/tenant/acme/communities/tier-allocations', { params: { tier: 2, limit: 10, offset: 0 } });
  });

  it('setTier PUTs {tier} to the community UUID path', async () => {
    put.mockResolvedValue({ data: { changed: true } });
    await tenantCommunityTiersApi.setTier('acme', 'uuid-1', 3);
    expect(put).toHaveBeenCalledWith('/api/v1/tenant/acme/communities/uuid-1/tier', { tier: 3 });
  });
});

describe('communityTierConstants', () => {
  it('names the three codes', () => {
    expect(TIER_LABEL).toEqual({ 1: 'Free', 2: 'Professional', 3: 'Enterprise' });
  });

  it.each([[undefined, 1], [null, 1], [0, 1], [4, 1], ['2', 1], [2, 2], [3, 3]])('normalizeTier(%s) = %s', (v, out) => {
    expect(normalizeTier(v)).toBe(out);
  });

  it('tierLabel falls back to Free', () => {
    expect(tierLabel(undefined)).toBe('Free');
    expect(tierLabel(3)).toBe('Enterprise');
  });

  it('every reason code has a human label', () => {
    expect(REASON_CODES).toHaveLength(7);
    for (const r of REASON_CODES) expect(REASON_LABEL[r]).toBeTruthy();
  });
});
