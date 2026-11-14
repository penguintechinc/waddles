import { useEffect, useState, useSyncExternalStore } from 'react';
import { joinRoom, leaveRoom } from '../services/callsApi';
import { CallSession, IDLE_SNAPSHOT, type CallSnapshot, type SessionDeps } from '../services/media/callSession';

const NO_SESSION = {
  subscribe: () => () => undefined,
  getSnapshot: () => IDLE_SNAPSHOT,
};

/**
 * React adapter for one room call. The session is created in an effect (so StrictMode's mount/unmount/mount never
 * reuses a disposed one) and disposed on unmount, which stops all media and unregisters the participant.
 * `overrides` lets tests inject fake devices / peer connections.
 */
export function useCallSession(
  communityId: number,
  roomId: string,
  overrides?: Pick<SessionDeps, 'devices' | 'media'>,
): { session: CallSession | null; snapshot: CallSnapshot } {
  const [session, setSession] = useState<CallSession | null>(null);

  useEffect(() => {
    const created = new CallSession({
      join: () => joinRoom(communityId, roomId),
      leave: () => leaveRoom(communityId, roomId),
      ...overrides,
    });
    setSession(created);
    return () => {
      created.dispose();
      setSession(null);
    };
    // `overrides` is a test seam supplied once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [communityId, roomId]);

  const store = session ?? NO_SESSION;
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot);
  return { session, snapshot };
}
