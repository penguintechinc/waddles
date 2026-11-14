import { CallsApiError } from '../services/callsApi';
import { useCallRooms, useRoomParticipants } from './useCalls';

/** Who is in a room, as far as the caller is allowed to know. */
export interface CallPresence {
  /** Participants currently publishing (what to subscribe to); `undefined` until the first successful read. */
  publisherIds: string[] | undefined;
  /** Display names by participant id: moderators only (hub-api exposes names on the admin route alone). */
  names: ReadonlyMap<string, string>;
  total: number | undefined;
  speaking: number | undefined;
  error: CallsApiError | null;
  refetch: () => void;
  /** Changes on every successful poll, so a failed remote subscription is retried. */
  updatedAt: number;
}

const GONE = new CallsApiError('This room no longer exists.', 404, 'ROOM_NOT_FOUND', false);

/**
 * Polled presence for the call view. Moderators read the admin participants route (names, mute state).
 * Members have no participants route today, so they read the member room list: publisher ids (enough to subscribe)
 * and head counts, without names. `enabled` is false until the call is connected.
 */
export function useCallPresence(communityId: number, roomId: string, moderator: boolean, enabled: boolean): CallPresence {
  const admin = useRoomParticipants(communityId, roomId, enabled && moderator);
  const member = useCallRooms('member', communityId, enabled && !moderator);

  if (moderator) {
    const participants = admin.data?.participants;
    return {
      publisherIds: participants?.filter((p) => p.publisher).map((p) => p.participantId),
      names: new Map((participants ?? []).map((p) => [p.participantId, p.displayName])),
      total: participants?.length,
      speaking: participants?.filter((p) => p.publisher).length,
      error: admin.isError ? admin.error : null,
      refetch: () => void admin.refetch(),
      updatedAt: admin.dataUpdatedAt,
    };
  }
  const room = member.data?.find((candidate) => candidate.roomId === roomId);
  const missing = member.data !== undefined && room === undefined;
  return {
    publisherIds: room?.publisherIds,
    names: new Map(),
    total: room ? room.publisherIds.length + room.subscriberCount : undefined,
    speaking: room?.publisherIds.length,
    error: member.isError ? member.error : missing ? GONE : null,
    refetch: () => void member.refetch(),
    updatedAt: member.dataUpdatedAt,
  };
}
