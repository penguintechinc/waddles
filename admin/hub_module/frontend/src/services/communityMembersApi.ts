import { apiClient } from '../lib/apiClient';
import { expectRecord, readArray, readNumber, readOptNumber, readOptString, readString } from '../lib/wire';

// Typed read of the community roster from the only served route:
//   GET /api/v1/admin/:communityId/members?page=&limit=&search=&role=   (scope community:admin)
// The member-facing `GET /api/v1/communities/:id/members` is not served (see
// `UNSERVED_FEATURES['member-roster']`), so the roster is admin/moderator-only for now. The
// server filters `role` by exact match: one role per request, never a comma list. Member emails
// the route also returns are deliberately not carried into the view-model.

export interface CommunityMember {
  id: number;
  userId: string | null;
  username: string;
  avatarUrl: string | null;
  role: string;
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
  /** Exact community role to filter by, or empty for everyone. */
  role: string;
}

function parseMember(raw: unknown): CommunityMember {
  const row = expectRecord(raw, 'members[]');
  return {
    id: readNumber(row, 'id', 'members[]'),
    userId: readOptString(row, 'userId'),
    username: readOptString(row, 'username') ?? 'Unknown',
    avatarUrl: readOptString(row, 'avatarUrl'),
    role: readString(row, 'role', 'members[]'),
    joinedAt: readOptString(row, 'joinedAt'),
  };
}

/** Parses a `MembersResponse` body. */
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
export async function fetchCommunityMembers(communityId: number, query: MemberQuery): Promise<MemberPage> {
  const params: Record<string, string | number> = { page: query.page, limit: query.limit };
  if (query.search.trim() !== '') params['search'] = query.search.trim();
  if (query.role !== '') params['role'] = query.role;
  const { data } = await apiClient.get<unknown>(`/api/v1/admin/${communityId}/members`, { params });
  return parseMemberPage(data);
}
