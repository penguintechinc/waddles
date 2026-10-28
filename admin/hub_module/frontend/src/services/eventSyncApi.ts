import { apiClient } from '../lib/apiClient';

/**
 * Typed client for the per-community Discord scheduled-event sync toggle
 * (`hub_api/blueprints/v1/event_discord_sync.py`). Responses are the
 * `EventSyncStateResponse` dataclass serialized as-is (snake_case).
 */

/** Current event-sync state aggregated across the community's guild pairings. */
export interface EventSyncState {
  event_sync_enabled: boolean;
  pairings_total: number;
  pairings_enabled: number;
}

export const eventSyncApi = {
  /** `GET /api/v1/communities/<id>/calendar/event-sync` -- requires `community.guild_pairing:read`. */
  get: (communityId: string | number) =>
    apiClient
      .get<EventSyncState>(`/api/v1/communities/${communityId}/calendar/event-sync`)
      .then((res) => res.data),

  /** `PUT /api/v1/communities/<id>/calendar/event-sync` body `{enabled}` -- requires `community.guild_pairing:write`. */
  set: (communityId: string | number, enabled: boolean) =>
    apiClient
      .put<EventSyncState>(`/api/v1/communities/${communityId}/calendar/event-sync`, { enabled })
      .then((res) => res.data),
};
