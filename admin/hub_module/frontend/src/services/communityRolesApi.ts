import { apiClient } from '../lib/apiClient';
import { isCommunityId } from '../lib/communityId';
import { WireShapeError, expectRecord, readArray, readNumber, readOptString, readString } from '../lib/wire';

// Typed read of a community's roles:
//   GET /api/v1/admin/:communityId/roles   (scope community.members:view)
// Roles are data in hub-api (`community_roles`); the webui never hard-codes a role list.

/** `roleKind` code for the admin role kind (display tone only, never authorization). */
export const ROLE_KIND_ADMIN = 1;

export interface CommunityRoleOption {
  /** Role public_id (UUID). */
  id: string;
  name: string;
  displayName: string;
  roleKind: number;
  priority: number;
  memberCount: number;
}

function parseRole(raw: unknown): CommunityRoleOption {
  const row = expectRecord(raw, 'roles[]');
  const id = readString(row, 'id', 'roles[]');
  if (!isCommunityId(id)) throw new WireShapeError('roles[]', 'id is not a UUID');
  const name = readString(row, 'name', 'roles[]');
  return {
    id,
    name,
    displayName: readOptString(row, 'displayName') ?? name,
    roleKind: readNumber(row, 'roleKind', 'roles[]'),
    priority: readNumber(row, 'priority', 'roles[]'),
    memberCount: readNumber(row, 'memberCount', 'roles[]'),
  };
}

/** `GET /api/v1/admin/:id/roles`. */
export async function fetchCommunityRoles(communityId: string): Promise<CommunityRoleOption[]> {
  const { data } = await apiClient.get<unknown>(`/api/v1/admin/${communityId}/roles`);
  return readArray(expectRecord(data, 'roles'), 'roles', 'roles').map(parseRole);
}
