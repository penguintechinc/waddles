import { apiClient } from '../lib/apiClient';
import { expectRecord, isRecord, readNumber, readOptString, readString } from '../lib/wire';

// Community creation via the only served route:
//   POST /api/v1/superadmin/communities   (scope communities:admin, 201)
// Its request/response fields match the legacy `POST /communities/create` byte for byte, but
// hub-api serves it to platform admins only. Self-service creation for ordinary users has no
// route (`UNSERVED_FEATURES['community-self-create']`), so the page offers creation to super
// admins and says "not available yet" to everyone else.

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
  id: number;
  name: string;
  displayName: string | null;
}

/** `POST /api/v1/superadmin/communities`. */
export async function createCommunity(payload: CreateCommunityPayload): Promise<CreatedCommunity> {
  const { data } = await apiClient.post<unknown>('/api/v1/superadmin/communities', payload);
  const community = expectRecord(expectRecord(data, 'create community')['community'], 'create community community');
  return {
    id: readNumber(community, 'id', 'create community'),
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
 * than sent as `undefined`; the owner fields are only honoured for super admins.
 */
export function toCreatePayload(raw: unknown, allowOwner: boolean): CreateCommunityPayload {
  const values = isRecord(raw) ? raw : {};
  const name = field(values, 'name');
  if (!name) throw new Error('Community name is required');
  const payload: CreateCommunityPayload = { name, platform: field(values, 'platform') ?? 'discord' };
  const optional = {
    displayName: field(values, 'displayName'),
    description: field(values, 'description'),
    platformServerId: field(values, 'platformServerId'),
    communityType: field(values, 'communityType'),
  };
  for (const [key, value] of Object.entries(optional)) {
    if (value !== undefined) Object.assign(payload, { [key]: value });
  }
  if (typeof values['isPublic'] === 'boolean') payload.isPublic = values['isPublic'];
  const ownerId = field(values, 'ownerId');
  if (allowOwner && ownerId) {
    payload.ownerId = ownerId;
    const ownerName = field(values, 'ownerName');
    if (ownerName) payload.ownerName = ownerName;
  }
  return payload;
}
