import { apiClient } from '../lib/apiClient';
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

// Typed reads for "my communities" and public community details, built ONLY on
// routes hub-api serves:
//   GET /api/v1/auth/me                      -> the caller's memberships (id, name, displayName, role)
//   GET /api/v1/public/communities/:id       -> logo / description / member count
//   GET /api/v1/public/communities?search=   -> discovery
// The legacy `GET /api/v1/communities/my` does not exist on hub-api; `/auth/me`
// already carries the membership list, so there is no second route to invent.

/** One community the signed-in user belongs to, as served by `/auth/me`. */
export interface MyCommunity {
  id: number;
  name: string;
  displayName: string;
  role: string;
}

/** Public-facing community fields from `/public/communities/:id` and the list route. */
export interface PublicCommunity {
  id: number;
  name: string | null;
  displayName: string;
  description: string | null;
  logoUrl: string | null;
  bannerUrl: string | null;
  platform: string | null;
  memberCount: number;
  joinMode: string | null;
  createdAt: string | null;
}

/** Thrown when `/auth/me` answers `{ user: null }`: the session cookie is gone or expired. */
export class SessionExpiredError extends Error {
  constructor() {
    super('Your session has expired. Please sign in again.');
    this.name = 'SessionExpiredError';
  }
}

function parseMyCommunity(raw: unknown): MyCommunity {
  const row = expectRecord(raw, 'auth/me communities[]');
  const name = readString(row, 'name', 'auth/me communities[]');
  return {
    id: readNumber(row, 'id', 'auth/me communities[]'),
    name,
    displayName: readOptString(row, 'displayName') ?? name,
    role: readString(row, 'role', 'auth/me communities[]'),
  };
}

/** Parses one community object (detail or list row) into a `PublicCommunity`. */
export function parsePublicCommunity(raw: unknown, what = 'public community'): PublicCommunity {
  const row: WireRecord = expectRecord(raw, what);
  const name = readOptString(row, 'name');
  return {
    id: readNumber(row, 'id', what),
    name,
    displayName: readOptString(row, 'displayName') ?? name ?? `Community ${readNumber(row, 'id', what)}`,
    description: readOptString(row, 'description'),
    logoUrl: readOptString(row, 'logoUrl'),
    bannerUrl: readOptString(row, 'bannerUrl'),
    platform: readOptString(row, 'platform'),
    memberCount: readOptNumber(row, 'memberCount') ?? 0,
    joinMode: readOptString(row, 'joinMode'),
    createdAt: readOptString(row, 'createdAt'),
  };
}

/** `GET /api/v1/auth/me` reduced to the caller's community memberships. */
export async function fetchMyCommunities(): Promise<MyCommunity[]> {
  const { data } = await apiClient.get<unknown>('/api/v1/auth/me');
  const body = expectRecord(data, 'auth/me');
  const user = body['user'];
  if (user === null || user === undefined) throw new SessionExpiredError();
  return readArray(expectRecord(user, 'auth/me user'), 'communities', 'auth/me user').map(parseMyCommunity);
}

/** `GET /api/v1/public/communities/:id`. */
export async function fetchPublicCommunity(communityId: number): Promise<PublicCommunity> {
  const { data } = await apiClient.get<unknown>(`/api/v1/public/communities/${communityId}`);
  const body = expectRecord(data, 'public community');
  if (!('community' in body)) throw new WireShapeError('public community', 'missing "community"');
  return parsePublicCommunity(body['community']);
}

/** `GET /api/v1/public/communities?search=&limit=` for the dashboard's discovery strip. */
export async function searchPublicCommunities(search: string, limit: number): Promise<PublicCommunity[]> {
  const { data } = await apiClient.get<unknown>('/api/v1/public/communities', { params: { search, limit } });
  const body = expectRecord(data, 'public communities');
  return readArray(body, 'communities', 'public communities').map((row) => parsePublicCommunity(row, 'public communities[]'));
}
