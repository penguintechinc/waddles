import { apiClient } from '../lib/apiClient';
import { requireWireCommunityId } from '../lib/communityId';
import { expectRecord, isRecord, readOptString, readString } from '../lib/wire';

// Community creation:
//   POST /api/v1/superadmin/communities   (platform admins; may set isPublic/owner)
//   POST /api/v1/communities              (everyone else; 403 `feature_disabled` while the
//                                          `waddles.community-self-create` flag is off)
// Both answer 201 `{ success, community: { id, name, displayName } }`.

export interface CreateCommunityPayload {
  name: string;
  platform: string;
  displayName?: string;
  description?: string;
  platformServerId?: string;
  isPublic?: boolean;
  communityType?: string;
  /** Super admins can assign another owner. */
  ownerId?: string;
  ownerName?: string;
}

export interface CreatedCommunity {
  /** Community public UUID (the numeric id never crosses the API). */
  id: string;
  name: string;
  displayName: string | null;
}

/** Drops the fields only the super-admin route accepts. */
function withoutAdminFields(payload: CreateCommunityPayload): CreateCommunityPayload {
  const { isPublic: _isPublic, ownerId: _ownerId, ownerName: _ownerName, ...rest } = payload;
  return rest;
}

/** Creates a community on the super-admin route, or on the self-service route when `superAdmin` is false. */
export async function createCommunity(payload: CreateCommunityPayload, superAdmin: boolean): Promise<CreatedCommunity> {
  const url = superAdmin ? '/api/v1/superadmin/communities' : '/api/v1/communities';
  const body = superAdmin ? payload : withoutAdminFields(payload);
  const { data } = await apiClient.post<unknown>(url, body);
  const community = expectRecord(expectRecord(data, 'create community')['community'], 'create community community');
  return {
    id: requireWireCommunityId(community['id'], 'create community id'),
    name: readString(community, 'name', 'create community'),
    displayName: readOptString(community, 'displayName'),
  };
}

/** Reads a string form field, treating blanks as absent. */
function field(values: Record<string, unknown>, key: string): string | undefined {
  const value = values[key];
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined;
}

/**
 * Turns the untyped `CommunityForm` values into a payload. Optional keys are omitted rather
 * than sent as `undefined`; the owner and platform-server fields are only honoured for super admins.
 */
export function toCreatePayload(raw: unknown, allowOwner: boolean): CreateCommunityPayload {
  const values = isRecord(raw) ? raw : {};
  const name = field(values, 'name');
  if (!name) throw new Error('Community name is required');
  const payload: CreateCommunityPayload = { name, platform: field(values, 'platform') ?? 'discord' };
  const optional = {
    displayName: field(values, 'displayName'),
    description: field(values, 'description'),
    communityType: field(values, 'communityType'),
  };
  for (const [key, value] of Object.entries(optional)) {
    if (value !== undefined) Object.assign(payload, { [key]: value });
  }
  if (typeof values['isPublic'] === 'boolean') payload.isPublic = values['isPublic'];
  // A server id is only accepted from super admins: self-create has no verified binding, so
  // the API ignores it and the pairing/bot-install flow is the only member path.
  const serverId = field(values, 'platformServerId');
  if (allowOwner && serverId) payload.platformServerId = serverId;
  const ownerId = field(values, 'ownerId');
  if (allowOwner && ownerId) {
    payload.ownerId = ownerId;
    const ownerName = field(values, 'ownerName');
    if (ownerName) payload.ownerName = ownerName;
  }
  return payload;
}
