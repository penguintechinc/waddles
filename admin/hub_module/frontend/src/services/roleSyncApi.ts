import { apiClient } from '../lib/apiClient';

// Typed client for the guild-pairing / role-sync binding CRUD --
// mirrors `hub_api/blueprints/v1/guild_pairing.py` + `services/guild_pairing.py`
// exactly (field names, casing, status codes, error envelope). Unlike
// `bundleActivationApi.ts`'s DTO (which hand-picked camelCase field names),
// this blueprint's `GuildPairing`/`RoleSyncBinding` dataclasses serialize
// snake_case as-is through `quart-schema` -- these interfaces mirror that
// wire shape verbatim rather than guessing a transform that doesn't exist.
//
// `communityId`/`pairingId`/`bindingId` are numeric platform identifiers
// (never PII); `discordGuildId`/`discordRoleId` are Discord snowflakes.

/** Body for `POST .../guild-pairings` -- hub-api validates the guild ID and bot membership. */
export interface CreateGuildPairingPayload {
  discord_guild_id: string;
  sync_enabled: boolean;
  direction?: GuildPairingDirection;
}

/** Shared TanStack Query keys for role-sync data, so every writer invalidates the same cache entry. */
export const roleSyncKeys = {
  pairings: (communityId: number) => ['role-sync-pairings', communityId] as const,
  bindings: (communityId: number, pairingId: number) =>
    ['role-sync-bindings', communityId, pairingId] as const,
};

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
  event_sync_enabled: boolean;
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

interface GuildPairingResponse {
  success: boolean;
  pairing: GuildPairing;
}

/** Mutable subset of a pairing accepted by `PATCH .../guild-pairings/<id>`. */
export interface UpdateGuildPairingPayload {
  sync_enabled?: boolean;
  direction?: GuildPairingDirection;
  role_name_prefix?: string;
}

interface RoleSyncBindingListResponse {
  success: boolean;
  bindings: RoleSyncBinding[];
}

interface RoleSyncBindingResponse {
  success: boolean;
  binding: RoleSyncBinding;
}

/** One Discord guild role (`GET .../discord/roles`) -- `id` is a string snowflake. */
export interface DiscordRole {
  id: string;
  name: string;
  color: number;
  position: number;
  permissions: number;
  managed: boolean;
  /** Server-computed: carries a privileged permission or is a managed role -- never a mirror target. */
  privileged: boolean;
}

/** One cross-guild role mirror mapping (`discord_role_mirror_mappings`) + last sync counters. */
export interface RoleMirrorMapping {
  id: number;
  community_id: number;
  source_guild_id: string;
  source_role_id: string;
  target_guild_id: string;
  target_role_id: string;
  last_synced_at: string | null;
  last_roles_added: number;
  last_roles_removed: number;
  last_skipped_not_member: number;
  last_error: string | null;
  created_at: string | null;
}

export interface CreateRoleMirrorPayload {
  source_guild_id: string;
  source_role_id: string;
  target_guild_id: string;
  target_role_id: string;
}

export const roleSyncApi = {
  /** `GET .../discord/roles?guild_id=` -- a paired guild's roles; requires `community.guild_pairing:read`. */
  listGuildRoles: (communityId: number, guildId: string) =>
    apiClient
      .get<{ success: boolean; roles: DiscordRole[] }>(
        `/api/v1/communities/${communityId}/discord/roles`,
        { params: { guild_id: guildId } },
      )
      .then((res) => res.data.roles),

  /** `GET .../discord/role-mirrors` -- mirror mappings + last sync counters. */
  listMirrors: (communityId: number) =>
    apiClient
      .get<{ success: boolean; mappings: RoleMirrorMapping[] }>(
        `/api/v1/communities/${communityId}/discord/role-mirrors`,
      )
      .then((res) => res.data.mappings),

  /** `POST .../discord/role-mirrors` -- one-way source -> target mapping; requires `...:write`. */
  createMirror: (communityId: number, payload: CreateRoleMirrorPayload) =>
    apiClient
      .post<{ success: boolean; mapping: RoleMirrorMapping }>(
        `/api/v1/communities/${communityId}/discord/role-mirrors`,
        payload,
      )
      .then((res) => res.data.mapping),

  /** `DELETE .../discord/role-mirrors/<mappingId>`. */
  deleteMirror: (communityId: number, mappingId: number) =>
    apiClient.delete<void>(`/api/v1/communities/${communityId}/discord/role-mirrors/${mappingId}`),

  /** `GET /api/v1/communities/<communityId>/guild-pairings` -- requires `community.guild_pairing:read`. */
  listPairings: (communityId: number) =>
    apiClient
      .get<GuildPairingListResponse>(`/api/v1/communities/${communityId}/guild-pairings`)
      .then((res) => res.data.pairings),

  /**
   * `POST /api/v1/communities/<communityId>/guild-pairings` -- pair a Discord
   * server by ID (hub-api verifies the bot is in it); requires `community.guild_pairing:write`.
   */
  createPairing: (communityId: number, payload: CreateGuildPairingPayload) =>
    apiClient
      .post<GuildPairingResponse>(`/api/v1/communities/${communityId}/guild-pairings`, payload)
      .then((res) => res.data.pairing),

  /**
   * `POST .../discord/server-install/authorize` -- returns the Discord OAuth
   * install URL the browser must be sent to; the callback pairs the server.
   */
  startServerInstall: (communityId: number, payload: { sync_enabled: boolean }) =>
    apiClient
      .post<{ success: boolean; authorize_url: string }>(
        `/api/v1/communities/${communityId}/discord/server-install/authorize`,
        payload,
      )
      .then((res) => res.data.authorize_url),

  /** `PATCH .../guild-pairings/<pairingId>` -- requires `community.guild_pairing:write`. */
  updatePairing: (communityId: number, pairingId: number, payload: UpdateGuildPairingPayload) =>
    apiClient
      .patch<GuildPairingResponse>(
        `/api/v1/communities/${communityId}/guild-pairings/${pairingId}`,
        payload,
      )
      .then((res) => res.data.pairing),

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

/**
 * Pulls the HTTP status and hub-api error `code` off an axios-style error,
 * so callers can map known failures to friendly text without re-parsing.
 */
export function extractErrorInfo(err: unknown): { status: number | null; code: string | null } {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const response = (err as { response?: { status?: unknown; data?: Partial<ApiErrorBody> } })
      .response;
    const code = response?.data?.error?.code;
    return {
      status: typeof response?.status === 'number' ? response.status : null,
      code: typeof code === 'string' ? code : null,
    };
  }
  return { status: null, code: null };
}
