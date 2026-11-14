import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryKey,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import {
  acknowledgeHand,
  createRoom,
  deleteRoom,
  ensureTownHall,
  fetchMemberParticipants,
  fetchMyHand,
  fetchParticipants,
  fetchTownHall,
  fetchRaisedHands,
  fetchRooms,
  joinRoom,
  kickParticipant,
  leaveRoom,
  lowerHand,
  muteAll,
  raiseHand,
  resetHands,
  setParticipantMuted,
  setRoomLocked,
  toCallsError,
  type CallParticipantList,
  type CallRoom,
  type CallsApiError,
  type CallsScope,
  type JoinedRoom,
  type RaisedHand,
  type TownHall,
} from '../services/callsApi';

/** How often the moderator view refreshes participants and the hands queue (ms). */
export const CALLS_POLL_MS = 5000;

/** Query keys for calls reads, scoped by route family and community. */
export const callKeys = {
  rooms: (scope: CallsScope, communityId: number) => ['calls', scope, communityId, 'rooms'] as const,
  participants: (communityId: number, roomId: string) => ['calls', 'admin', communityId, 'rooms', roomId, 'participants'] as const,
  hands: (communityId: number, roomId: string) => ['calls', 'admin', communityId, 'rooms', roomId, 'hands'] as const,
  townHall: (communityId: number) => ['calls', 'member', communityId, 'town-hall'] as const,
  memberParticipants: (communityId: number, roomId: string) => ['calls', 'member', communityId, 'rooms', roomId, 'participants'] as const,
  myHand: (communityId: number, roomId: string) => ['calls', 'member', communityId, 'rooms', roomId, 'my-hand'] as const,
};

/** Stops polling once a read has failed, so an error state stays put until the user retries. */
export function pollUnlessFailed(state: { status: string }): number | false {
  return state.status === 'error' ? false : CALLS_POLL_MS;
}

/** Rooms of a community. Failures are an error state (never an empty list); no automatic retries, the UI offers one. */
export function useCallRooms(scope: CallsScope, communityId: number): UseQueryResult<CallRoom[], CallsApiError> {
  return useQuery<CallRoom[], CallsApiError>({
    queryKey: callKeys.rooms(scope, communityId),
    queryFn: () => fetchRooms(scope, communityId),
    retry: false,
    refetchInterval: (query) => pollUnlessFailed(query.state),
  });
}

/** Participants of the selected room (moderator view), polled. */
export function useRoomParticipants(communityId: number, roomId: string | null): UseQueryResult<CallParticipantList, CallsApiError> {
  return useQuery<CallParticipantList, CallsApiError>({
    queryKey: callKeys.participants(communityId, roomId ?? ''),
    queryFn: () => fetchParticipants(communityId, roomId ?? ''),
    enabled: roomId !== null,
    retry: false,
    refetchInterval: (query) => pollUnlessFailed(query.state),
  });
}

/** The selected room's raised-hands queue, oldest first (moderator view), polled. */
export function useRaisedHands(communityId: number, roomId: string | null): UseQueryResult<RaisedHand[], CallsApiError> {
  return useQuery<RaisedHand[], CallsApiError>({
    queryKey: callKeys.hands(communityId, roomId ?? ''),
    queryFn: () => fetchRaisedHands(communityId, roomId ?? ''),
    enabled: roomId !== null,
    retry: false,
    refetchInterval: (query) => pollUnlessFailed(query.state),
  });
}

/** One mutation whose success refreshes the given query keys; failures surface as `CallsApiError`. */
function useCallsMutation<TVars, TData>(
  mutationFn: (vars: TVars) => Promise<TData>,
  invalidate: readonly QueryKey[],
): UseMutationResult<TData, CallsApiError, TVars> {
  const queryClient = useQueryClient();
  return useMutation<TData, CallsApiError, TVars>({
    mutationFn: async (vars) => {
      try {
        return await mutationFn(vars);
      } catch (err) {
        throw toCallsError(err);
      }
    },
    onSuccess: async () => {
      await Promise.all(invalidate.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
    },
  });
}

/** Create a room, then refresh the room list. */
export function useCreateRoom(scope: CallsScope, communityId: number) {
  return useCallsMutation(
    (input: { roomName: string; maxParticipants?: number }) => createRoom(scope, communityId, input),
    [callKeys.rooms(scope, communityId)],
  );
}

/** Delete a room (admin), then refresh the room list. */
export function useDeleteRoom(communityId: number) {
  return useCallsMutation((roomId: string) => deleteRoom(communityId, roomId), [callKeys.rooms('admin', communityId)]);
}

/** Lock or unlock a room (admin), then refresh rooms and participants. */
export function useSetLocked(communityId: number) {
  const base = ['calls', 'admin', communityId] as const;
  return useCallsMutation(
    (vars: { roomId: string; locked: boolean }) => setRoomLocked(communityId, vars.roomId, vars.locked),
    [base],
  );
}

/** Remove a participant (admin), then refresh the room's participants, hands and the room list. */
export function useKick(communityId: number) {
  return useCallsMutation(
    (vars: { roomId: string; participantId: string }) => kickParticipant(communityId, vars.roomId, vars.participantId),
    [['calls', 'admin', communityId]],
  );
}

/** Mute or unmute one participant (admin), then refresh participants. */
export function useSetMuted(communityId: number) {
  return useCallsMutation(
    (vars: { roomId: string; participantId: string; muted: boolean }) =>
      setParticipantMuted(communityId, vars.roomId, vars.participantId, vars.muted),
    [['calls', 'admin', communityId]],
  );
}

/** Mute every publisher but the acting admin; resolves to the muted count. */
export function useMuteAll(communityId: number) {
  return useCallsMutation(
    (roomId: string) => muteAll(communityId, roomId),
    [['calls', 'admin', communityId]],
  );
}

/** Acknowledge one raised hand (admin), then refresh the queue. */
export function useAcknowledgeHand(communityId: number) {
  return useCallsMutation(
    (vars: { roomId: string; participantId: string }) => acknowledgeHand(communityId, vars.roomId, vars.participantId),
    [['calls', 'admin', communityId]],
  );
}

/** Clear the whole queue (admin); resolves to the cleared count. */
export function useResetHands(communityId: number) {
  return useCallsMutation(
    (roomId: string) => resetHands(communityId, roomId),
    [['calls', 'admin', communityId]],
  );
}

/** Join a room as the signed-in member. */
export function useJoinRoom(communityId: number): UseMutationResult<JoinedRoom, CallsApiError, string> {
  return useCallsMutation((roomId: string) => joinRoom(communityId, roomId), [callKeys.rooms('member', communityId)]);
}

/** Leave a room, then refresh the room list. */
export function useLeaveRoom(communityId: number) {
  return useCallsMutation((roomId: string) => leaveRoom(communityId, roomId), [callKeys.rooms('member', communityId)]);
}

/** Raise the member's own hand; resolves to the queued hand. */
export function useRaiseHand(communityId: number) {
  return useCallsMutation((roomId: string) => raiseHand(communityId, roomId), [['calls', 'member', communityId]]);
}

/** Lower the member's own hand; resolves to whether a hand was actually raised. */
export function useLowerHand(communityId: number) {
  return useCallsMutation((roomId: string) => lowerHand(communityId, roomId), [['calls', 'member', communityId]]);
}

/** The community's Town Hall room (PROVISIONAL route, see `callsApi.ts`); a 404 means it has not been opened yet. */
export function useTownHall(communityId: number): UseQueryResult<TownHall, CallsApiError> {
  return useQuery<TownHall, CallsApiError>({
    queryKey: callKeys.townHall(communityId),
    queryFn: () => fetchTownHall(communityId),
    retry: false,
  });
}

/** Open (ensure) the Town Hall, then refresh it. */
export function useEnsureTownHall(communityId: number) {
  return useCallsMutation(() => ensureTownHall(communityId), [callKeys.townHall(communityId)]);
}

/**
 * Participants of a room for the call view, polled: the moderator (admin) route for moderators, the member route
 * (PROVISIONAL) for everyone else. `enabled` is false until the call is connected.
 */
export function useCallParticipants(
  communityId: number,
  roomId: string,
  moderator: boolean,
  enabled: boolean,
): UseQueryResult<CallParticipantList, CallsApiError> {
  return useQuery<CallParticipantList, CallsApiError>({
    queryKey: moderator ? callKeys.participants(communityId, roomId) : callKeys.memberParticipants(communityId, roomId),
    queryFn: () => (moderator ? fetchParticipants(communityId, roomId) : fetchMemberParticipants(communityId, roomId)),
    enabled,
    retry: false,
    refetchInterval: (query) => pollUnlessFailed(query.state),
  });
}

/** The member's own hand and whether a moderator acknowledged it (PROVISIONAL route), polled while connected. */
export function useMyHand(communityId: number, roomId: string, enabled: boolean): UseQueryResult<RaisedHand | null, CallsApiError> {
  return useQuery<RaisedHand | null, CallsApiError>({
    queryKey: callKeys.myHand(communityId, roomId),
    queryFn: () => fetchMyHand(communityId, roomId),
    enabled,
    retry: false,
    refetchInterval: (query) => pollUnlessFailed(query.state),
  });
}
