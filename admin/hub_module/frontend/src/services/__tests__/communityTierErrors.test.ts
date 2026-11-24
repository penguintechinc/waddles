import { describe, expect, it } from 'vitest';

import { classifyTierError, isTenantTierRequired, TENANT_TIER_REQUIRED_MESSAGE } from '../communityTierErrors';

const fail = (status: number, code: string) => ({ response: { status, data: { error: { code, message: 'm' } } } });

describe('communityTierErrors', () => {
  it('maps 402 TENANT_TIER_REQUIRED before the pool-exhausted 402', () => {
    const c = classifyTierError(fail(402, 'TENANT_TIER_REQUIRED'));
    expect(c.kind).toBe('tier_required');
    expect(c.message).toBe(TENANT_TIER_REQUIRED_MESSAGE);
    expect(TENANT_TIER_REQUIRED_MESSAGE).toBe('Premium communities require a Professional or Enterprise license');
  });

  it('keeps 402 TEAM_POOL_EXHAUSTED as exhausted and 503 as unavailable', () => {
    expect(classifyTierError(fail(402, 'TEAM_POOL_EXHAUSTED')).kind).toBe('exhausted');
    expect(classifyTierError(fail(503, 'TEAM_POOL_UNAVAILABLE')).kind).toBe('unavailable');
  });

  it('detects the tenant-tier-required envelope only', () => {
    expect(isTenantTierRequired(fail(402, 'TENANT_TIER_REQUIRED'))).toBe(true);
    expect(isTenantTierRequired(fail(402, 'TEAM_POOL_EXHAUSTED'))).toBe(false);
    expect(isTenantTierRequired(new Error('x'))).toBe(false);
  });
});
