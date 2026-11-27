/** Wire contracts for roles, recent activity and member mutations (UUID-only, strictly parsed). */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchCommunityRoles } from '../communityRolesApi';
import { fetchRecentActivity } from '../communityActivityApi';
import { changeMemberRole, removeMember } from '../communityMembersApi';
import { apiClient } from '../../lib/apiClient';
import { hasAdminSurface } from '../../lib/communityScopes';
import { mockApiGet, ok } from '../../test/api';

const CID = '00000007-0000-4000-8000-000000000000';
const RID = '000000a1-0000-4000-8000-000000000000';
const UID = '0000000b-0000-4000-8000-000000000000';

afterEach(() => vi.restoreAllMocks());

describe('communityRolesApi', () => {
  it('parses roles from GET /admin/:id/roles', async () => {
    const get = mockApiGet(() => ({ success: true, roles: [{ id: RID, name: 'steward', roleKind: 1, priority: 9, memberCount: 2 }] }));
    await expect(fetchCommunityRoles(CID)).resolves.toEqual([
      { id: RID, name: 'steward', displayName: 'steward', roleKind: 1, priority: 9, memberCount: 2 },
    ]);
    expect(get).toHaveBeenCalledWith(`/api/v1/admin/${CID}/roles`);
  });

  it('rejects a non-UUID role id and a missing roleKind', async () => {
    mockApiGet(() => ({ roles: [{ id: 3, name: 'x', roleKind: 1, priority: 1, memberCount: 0 }] }));
    await expect(fetchCommunityRoles(CID)).rejects.toThrow();
    mockApiGet(() => ({ roles: [{ id: RID, name: 'x', priority: 1, memberCount: 0 }] }));
    await expect(fetchCommunityRoles(CID)).rejects.toThrow('"roleKind" is not a number');
  });
});

describe('fetchRecentActivity', () => {
  it('parses items and passes the limit', async () => {
    const get = mockApiGet(() => ({
      success: true,
      items: [{ kind: 1, at: '2026-11-20T10:00:00Z', actorId: UID, actorName: 'Dana', title: null }],
    }));
    await expect(fetchRecentActivity(CID, 5)).resolves.toEqual([{ kind: 1, at: '2026-11-20T10:00:00Z', actorId: UID, actorName: 'Dana', title: null }]);
    expect(get).toHaveBeenCalledWith(`/api/v1/communities/${CID}/activity`, { params: { limit: 5 } });
  });

  it('rejects a non-UUID actor id and missing items', async () => {
    mockApiGet(() => ({ items: [{ kind: 1, at: 'x', actorId: '7' }] }));
    await expect(fetchRecentActivity(CID, 5)).rejects.toThrow('actorId is not a UUID');
    mockApiGet(() => ({ success: true }));
    await expect(fetchRecentActivity(CID, 5)).rejects.toThrow();
  });
});

describe('member mutations', () => {
  it('PUTs the role id and DELETEs by user uuid', async () => {
    const put = vi.spyOn(apiClient, 'put').mockResolvedValue(ok({ success: true }));
    const del = vi.spyOn(apiClient, 'delete').mockResolvedValue(ok({ success: true }));
    await changeMemberRole(CID, UID, RID);
    await removeMember(CID, UID);
    expect(put).toHaveBeenCalledWith(`/api/v1/admin/${CID}/members/${UID}/role`, { roleId: RID });
    expect(del).toHaveBeenCalledWith(`/api/v1/admin/${CID}/members/${UID}`);
  });
});

describe('hasAdminSurface', () => {
  it('is true only for an admin-surface scope', () => {
    expect(hasAdminSurface(['community:admin'])).toBe(true);
    expect(hasAdminSurface(['bundles.admin:moderate'])).toBe(true);
    expect(hasAdminSurface(['community.roles:view'])).toBe(false);
    expect(hasAdminSurface([])).toBe(false);
    expect(hasAdminSurface(undefined)).toBe(false);
  });
});
