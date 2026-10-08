import { apiClient } from '../lib/apiClient';

// Typed client for the Bar Citizen guild-pairing / role-sync binding CRUD --
// mirrors `hub_api/blueprints/v1/guild_pairing.py` + `services/guild_pairing.py`
// exactly (field names, casing, status codes, error envelope). Unlike
// `bundleActivationApi.ts`'s DTO (which hand-picked camelCase field names),
// this blueprint's `GuildPairing`/`RoleSyncBinding` dataclasses serialize
// snake_case as-is through `quart-schema` -- these interfaces mirror that
// wire shape verbatim rather than guessing a transform that doesn't exist.
//
// `communityId`/`pairingId`/`bindingId` are numeric platform identifiers
// (never PII); `discordGuildId`/`discordRoleId` are Discord snowflakes.

/** `guild_tenant_pairings.direction` -- migration 0034's CHECK constraint values. */
export type GuildPairingDirection = 'discord_to_twitch' | 'twitch_to_discord' | 'bidirectional';

/**
 * `community_role_sync_bindings.sync_scope` -- fixes ONE lifelong write
 * direction per binding (structural loop-prevention, migration 0034/0036):
 * - `subscriber_tier` / `moderator` -- this platform's Twitch state pushes
 *   TO the bound Discord role (Twitch -> Discord).
 * - `community_role` -- the bound Discord guild role's membership pulls
 *   FROM Discord, setting the linked user's platform community role
 *   (Discord -> platform). Grant-only: losing the Discord role never
 *   auto-demotes; conflict precedence is the highest-priority held role.
 */
export type RoleSyncScope = 'subscriber_tier' | 'moderator' | 'community_role';

/** `community_role_sync_bindings.subscriber_tier` -- migration 0034's CHECK constraint values. */
export type SubscriberTier = 1 | 2 | 3;

/**
 * `community_role_sync_bindings.community_role` -- migration 0036's CHECK
 * constraint values, the same set `services/admin_service.py::
 * update_member_role()` accepts. `community-owner` is deliberately excluded
 * (never assignable by sync).
 */
export type CommunityRole = 'community-admin' | 'moderator' | 'vip' | 'member';

export const SYNC_SCOPES: readonly RoleSyncScope[] = ['subscriber_tier', 'moderator', 'community_role'];
export const SUBSCRIBER_TIERS: readonly SubscriberTier[] = [1, 2, 3];
export const COMMUNITY_ROLES: readonly CommunityRole[] = ['community-admin', 'moderator', 'vip', 'member'];

/** One `guild_tenant_pairings` row. */
export interface GuildPairing {
  id: number;
  community_id: number;
  discord_guild_id: string;
  direction: GuildPairingDirection;
  sync_enabled: boolean;
  role_name_prefix: string;
  created_by_user_id: number | null;
  created_at: string | null;
  updated_at: string | null;
}

/** One `community_role_sync_bindings` row. */
export interface RoleSyncBinding {
  id: number;
  pairing_id: number;
  sync_scope: RoleSyncScope;
  subscriber_tier: SubscriberTier | null;
  community_role: CommunityRole | null;
  discord_role_id: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface CreateRoleSyncBindingPayload {
  sync_scope: RoleSyncScope;
  discord_role_id: string;
  subscriber_tier?: SubscriberTier;
  community_role?: CommunityRole;
}

interface GuildPairingListResponse {
  success: boolean;
  pairings: GuildPairing[];
}

interface RoleSyncBindingListResponse {
  success: boolean;
  bindings: RoleSyncBinding[];
}

interface RoleSyncBindingResponse {
  success: boolean;
  binding: RoleSyncBinding;
}

export const roleSyncApi = {
  /** `GET /api/v1/communities/<communityId>/guild-pairings` -- requires `community.guild_pairing:read`. */
  listPairings: (communityId: number) =>
    apiClient
      .get<GuildPairingListResponse>(`/api/v1/communities/${communityId}/guild-pairings`)
      .then((res) => res.data.pairings),

  /** `GET .../guild-pairings/<pairingId>/role-bindings` -- requires `community.guild_pairing:read`. */
  listBindings: (communityId: number, pairingId: number) =>
    apiClient
      .get<RoleSyncBindingListResponse>(
        `/api/v1/communities/${communityId}/guild-pairings/${pairingId}/role-bindings`,
      )
      .then((res) => res.data.bindings),

  /** `POST .../guild-pairings/<pairingId>/role-bindings` -- requires `community.guild_pairing:write`. */
  createBinding: (communityId: number, pairingId: number, payload: CreateRoleSyncBindingPayload) =>
    apiClient
      .post<RoleSyncBindingResponse>(
        `/api/v1/communities/${communityId}/guild-pairings/${pairingId}/role-bindings`,
        payload,
      )
      .then((res) => res.data.binding),

  /** `DELETE .../guild-pairings/<pairingId>/role-bindings/<bindingId>` -- requires `community.guild_pairing:write`. */
  deleteBinding: (communityId: number, pairingId: number, bindingId: number) =>
    apiClient.delete<void>(
      `/api/v1/communities/${communityId}/guild-pairings/${pairingId}/role-bindings/${bindingId}`,
    ),
};

/** Shape of `error_response()` (`flask_core.api_utils`) -- every non-2xx body from this blueprint. */
export interface ApiErrorBody {
  success: false;
  error: {
    message: string;
    code: string;
    timestamp: string;
  };
}

/** Extracts a user-facing message from an axios error against this API's error envelope. */
export function extractErrorMessage(err: unknown, fallback: string): string {
  if (
    typeof err === 'object' &&
    err !== null &&
    'response' in err &&
    typeof (err as { response?: unknown }).response === 'object'
  ) {
    const response = (err as { response?: { data?: Partial<ApiErrorBody> } }).response;
    const message = response?.data?.error?.message;
    if (typeof message === 'string' && message.length > 0) {
      return message;
    }
  }
  return fallback;
}
