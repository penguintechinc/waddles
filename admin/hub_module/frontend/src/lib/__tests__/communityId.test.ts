/** Community ids are opaque UUID strings; numbers and junk are rejected loudly. */
import { describe, expect, it } from 'vitest';
import { isCommunityId, parseCommunityId, requireCommunityId } from '../communityId';

const ID = '0f8fad5b-d9cb-469f-a165-70867728950e';

describe('communityId', () => {
  it('accepts a canonical UUID string only', () => {
    expect(isCommunityId(ID)).toBe(true);
    expect(isCommunityId(ID.toUpperCase())).toBe(true);
    for (const bad of [7, '7', '', 'nope', undefined, null, `${ID}/x`]) expect(isCommunityId(bad)).toBe(false);
  });

  it('parseCommunityId returns the id or null', () => {
    expect(parseCommunityId(ID)).toBe(ID);
    expect(parseCommunityId('7')).toBeNull();
  });

  it('requireCommunityId throws on a missing or non-UUID id', () => {
    expect(requireCommunityId(ID, 'ctx')).toBe(ID);
    expect(() => requireCommunityId(undefined, 'ctx')).toThrow('ctx: expected a community UUID');
  });
});
