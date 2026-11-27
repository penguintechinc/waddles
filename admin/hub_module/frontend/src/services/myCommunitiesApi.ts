import { apiClient } from '../lib/apiClient';
import {
  WireShapeError,
  expectRecord,
  readArray,
  readOptNumber,
  readOptString,
  readString,
  type WireRecord,
} from '../lib/wire';
import { isCommunityId } from '../lib/communityId';

// Typed reads for "my communities" and public community details, built ONLY on
// routes hub-api serves:
//   GET /api/v1/auth/me                      -> the caller's memberships (id, name, displayName, role)
//   GET /api/v1/public/communities/:id       -> logo / description / member count
//   GET /api/v1/public/communities?search=   -> discovery
// `GET /api/v1/communities/my` is served for penguind (CAT auth, #1536), but the webui
// uses `/auth/me`, which already carries the membership list.

/** One community the signed-in user belongs to, as served by `/auth/me`. */
export interface MyCommunity {
  /** Community public_id (UUID); the numeric key never reaches the webui. */
  id: string;
  name: string;
  displayName: string;
  /** Role display name only; authorization reads `scopes`, never this. */
  role: string;
  /** The caller's community role public_id (UUID), or null when the server has none. */
  roleId: string | null;
  /** Effective scopes the caller holds in this community (global/tenant admins hold every scope). */
  scopes: string[];
}

/** Public-facing community fields from `/public/communities/:id` and the list route. */
export interface PublicCommunity {
  /** Community public_id (UUID). */
  id: string;
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

/** Reads a community UUID from a wire record; throws (never falls back) if absent or not a UUID. */
function readCommunityId(row: WireRecord, key: string, what: string): string {
  const value = readString(row, key, what);
  if (!isCommunityId(value)) throw new WireShapeError(what, `${key} is not a community UUID`);
  return value;
}

/** Reads a required string array (every element a string); throws rather than dropping bad entries. */
function readStringArray(row: WireRecord, key: string, what: string): string[] {
  return readArray(row, key, what).map((item, index) => {
    if (typeof item !== 'string') throw new WireShapeError(what, `${key}[${index}] is not a string`);
    return item;
  });
}

/** Reads an optional UUID (null when absent/null); a present non-UUID throws. */
function readOptUuid(row: WireRecord, key: string, what: string): string | null {
  const value = readOptString(row, key);
  if (value === null) return null;
  if (!isCommunityId(value)) throw new WireShapeError(what, `${key} is not a UUID`);
  return value;
}

function parseMyCommunity(raw: unknown): MyCommunity {
  const row = expectRecord(raw, 'auth/me communities[]');
  const name = readString(row, 'name', 'auth/me communities[]');
  return {
    id: readCommunityId(row, 'id', 'auth/me communities[]'),
    name,
    displayName: readOptString(row, 'displayName') ?? name,
    role: readString(row, 'role', 'auth/me communities[]'),
    roleId: readOptUuid(row, 'roleId', 'auth/me communities[]'),
    scopes: readStringArray(row, 'scopes', 'auth/me communities[]'),
  };
}

/** Parses one community object (detail or list row) into a `PublicCommunity`. */
export function parsePublicCommunity(raw: unknown, what = 'public community'): PublicCommunity {
  const row: WireRecord = expectRecord(raw, what);
  const name = readOptString(row, 'name');
  return {
    id: readCommunityId(row, 'id', what),
    name,
    displayName: readOptString(row, 'displayName') ?? name ?? `Community ${readCommunityId(row, 'id', what)}`,
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
export async function fetchPublicCommunity(communityId: string): Promise<PublicCommunity> {
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
