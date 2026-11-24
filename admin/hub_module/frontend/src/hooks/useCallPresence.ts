import { CallsApiError } from '../services/callsApi';
import { useMemberParticipants, useRoomParticipants } from './useCalls';

/** Who is in a room, as far as the caller is allowed to know. */
export interface CallPresence {
  /** Participants currently publishing (what to subscribe to); `undefined` until the first successful read. */
  publisherIds: string[] | undefined;
  /** Display names by participant id, resolved by hub-api ("guest" when unknown). */
  names: ReadonlyMap<string, string>;
  total: number | undefined;
  speaking: number | undefined;
  error: CallsApiError | null;
  refetch: () => void;
  /** Changes on every successful poll, so a failed remote subscription is retried. */
  updatedAt: number;
}

/**
 * Polled presence for the call view. Moderators read the admin participants route (names, mute state); members
 * read the member participants route (names + media state, no moderation internals). `enabled` is false until
 * the call is connected.
 */
export function useCallPresence(communityId: string, roomId: string, moderator: boolean, enabled: boolean): CallPresence {
  const admin = useRoomParticipants(communityId, roomId, enabled && moderator);
  const member = useMemberParticipants(communityId, roomId, enabled && !moderator);
  const source = moderator ? admin : member;
  const participants = source.data?.participants;
  return {
    publisherIds: participants?.filter((p) => p.publisher).map((p) => p.participantId),
    names: new Map((participants ?? []).map((p) => [p.participantId, p.displayName])),
    total: participants?.length,
    speaking: participants?.filter((p) => p.publisher).length,
    error: source.isError ? source.error : null,
    refetch: () => void source.refetch(),
    updatedAt: source.dataUpdatedAt,
  };
}
