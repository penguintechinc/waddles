import { apiClient } from '../lib/apiClient';
import { isCommunityId } from '../lib/communityId';
import {
  WireShapeError,
  expectRecord,
  readArray,
  readNumber,
  readOptNumber,
  readOptString,
  readString,
  type WireRecord,
} from '../lib/wire';

// Typed client for the community roster and its two mutations (UUIDs only on the wire):
//   GET    /api/v1/admin/:communityId/members?page&limit&search&roleId   (scope community.members:view)
//   PUT    /api/v1/admin/:communityId/members/:userId/role  { roleId }   (scope members:roles.manage)
//   DELETE /api/v1/admin/:communityId/members/:userId                    (scope members:remove)
// The server returns neither numeric ids nor emails, and the view-model carries none.

export interface CommunityMember {
  /** User public_id (UUID). */
  userId: string;
  username: string;
  avatarUrl: string | null;
  roleId: string | null;
  roleName: string | null;
  joinedAt: string | null;
}

export interface MemberPage {
  members: CommunityMember[];
  page: number;
  limit: number;
  total: number;
  totalPages: number;
}

export interface MemberQuery {
  page: number;
  limit: number;
  search: string;
  /** Role public_id to filter by, or empty for everyone. */
  roleId: string;
}

function readUuid(row: WireRecord, key: string, what: string): string {
  const value = readString(row, key, what);
  if (!isCommunityId(value)) throw new WireShapeError(what, `${key} is not a UUID`);
  return value;
}

function parseMember(raw: unknown): CommunityMember {
  const row = expectRecord(raw, 'members[]');
  const roleId = readOptString(row, 'roleId');
  if (roleId !== null && !isCommunityId(roleId)) throw new WireShapeError('members[]', 'roleId is not a UUID');
  return {
    userId: readUuid(row, 'userId', 'members[]'),
    username: readOptString(row, 'username') ?? 'Unknown',
    avatarUrl: readOptString(row, 'avatarUrl'),
    roleId,
    roleName: readOptString(row, 'roleName'),
    joinedAt: readOptString(row, 'joinedAt'),
  };
}

/** Parses a members response body. */
export function parseMemberPage(data: unknown): MemberPage {
  const body = expectRecord(data, 'members');
  const pagination = expectRecord(body['pagination'], 'members pagination');
  return {
    members: readArray(body, 'members', 'members').map(parseMember),
    page: readNumber(pagination, 'page', 'members pagination'),
    limit: readNumber(pagination, 'limit', 'members pagination'),
    total: readNumber(pagination, 'total', 'members pagination'),
    totalPages: readOptNumber(pagination, 'totalPages') ?? 1,
  };
}

/** `GET /api/v1/admin/:id/members`. */
export async function fetchCommunityMembers(communityId: string, query: MemberQuery): Promise<MemberPage> {
  const params: Record<string, string | number> = { page: query.page, limit: query.limit };
  if (query.search.trim() !== '') params['search'] = query.search.trim();
  if (query.roleId !== '') params['roleId'] = query.roleId;
  const { data } = await apiClient.get<unknown>(`/api/v1/admin/${communityId}/members`, { params });
  return parseMemberPage(data);
}

/** `PUT /api/v1/admin/:id/members/:userId/role`. */
export async function changeMemberRole(communityId: string, userId: string, roleId: string): Promise<void> {
  await apiClient.put<unknown>(`/api/v1/admin/${communityId}/members/${userId}/role`, { roleId });
}

/** `DELETE /api/v1/admin/:id/members/:userId`. */
export async function removeMember(communityId: string, userId: string): Promise<void> {
  await apiClient.delete<unknown>(`/api/v1/admin/${communityId}/members/${userId}`);
}
