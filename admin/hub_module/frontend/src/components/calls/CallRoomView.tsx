import { useEffect, useState } from 'react';
import { Alert, Badge, Card, CardBody, LoadingState } from '../ui';
import { useCallSession } from '../../hooks/useCallSession';
import { useCallPresence } from '../../hooks/useCallPresence';
import { roomTitle, type CallRoom, type CallsApiError } from '../../services/callsApi';
import type { CallPhase, SessionDeps } from '../../services/media/callSession';
import { CallControls } from './CallControls';
import { CallFailurePanel } from './CallFailure';
import { CallLobby } from './CallLobby';
import { CallsActionError, CallsErrorPanel } from './CallsErrors';
import { MediaTile } from './MediaTile';
import { MyHandControl } from './MyHandControl';
import { RoomDetail } from './RoomDetail';
import { RoomPresence } from './RoomPresence';

export interface CallRoomViewProps {
  communityId: number;
  room: Pick<CallRoom, 'roomId' | 'label'>;
  /** Community admins get moderator controls (mute, remove, hands queue); everyone else gets raise/lower hand. */
  moderator: boolean;
  /** Called after the user has left the room (e.g. to return to a room list). */
  onLeft?: () => void;
  /** Test seam: fake devices and peer connections. */
  overrides?: Pick<SessionDeps, 'devices' | 'media'>;
}

const PHASE_TEXT: Record<CallPhase, string> = {
  idle: 'Not connected',
  'requesting-media': 'Waiting for microphone permission',
  joining: 'Joining the room',
  connecting: 'Connecting audio and video',
  connected: 'Connected',
  leaving: 'Leaving',
  failed: 'Disconnected',
};

/**
 * The whole in-room experience over real WHIP/WHEP media: lobby, connection state, participant tiles, self controls,
 * member hand toggle or moderator controls, and every failure state. Used by the Town Hall page and voice channels.
 */
export function CallRoomView({ communityId, room, moderator, onLeft, overrides }: CallRoomViewProps) {
  const { session, snapshot } = useCallSession(communityId, room.roomId, overrides);
  const connected = snapshot.phase === 'connected';
  const presence = useCallPresence(communityId, room.roomId, moderator, connected);
  const [modNotice, setModNotice] = useState<string | null>(null);
  const [modError, setModError] = useState<CallsApiError | null>(null);

  const publisherKey = presence.publisherIds?.join(',');
  const updatedAt = presence.updatedAt;
  useEffect(() => {
    if (session !== null && connected && publisherKey !== undefined) void session.syncRemotes(publisherKey === '' ? [] : publisherKey.split(','));
    // `updatedAt` re-runs the sync on every poll so a failed remote is retried.
  }, [session, connected, publisherKey, updatedAt]);

  if (session === null) return <LoadingState label="Preparing" />;

  const nameOf = (participantId: string, index: number): string =>
    presence.names.get(participantId) ?? (moderator ? 'Guest' : `Participant ${index + 1}`);
  const title = roomTitle(room);
  const busy = snapshot.phase === 'requesting-media' || snapshot.phase === 'joining' || snapshot.phase === 'connecting';
  const leave = async (): Promise<void> => {
    await session.leave();
    onLeft?.();
  };

  return (
    <section aria-label={`${title} call`} className="flex flex-col gap-4" data-testid="call-room">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="section-title">{title}</h2>
        <div role="status" aria-live="polite" data-testid="connection-status">
          <Badge tone={connected ? 'green' : snapshot.phase === 'failed' ? 'red' : 'gray'}>{PHASE_TEXT[snapshot.phase]}</Badge>
        </div>
      </div>

      {snapshot.failure?.kind === 'leave' ? (
        <Alert tone="warning" title="You left, but the server did not confirm it">
          {snapshot.failure.message}
        </Alert>
      ) : null}
      {snapshot.notice ? <Alert tone="info">{snapshot.notice}</Alert> : null}

      {snapshot.phase === 'idle' ? (
        <CallLobby title={title} onJoin={(options) => void session.join(options)} />
      ) : null}

      {busy ? <LoadingState label={PHASE_TEXT[snapshot.phase]} className="py-10" /> : null}

      {snapshot.phase === 'failed' && snapshot.failure ? (
        <CallFailurePanel
          failure={snapshot.failure}
          onRetry={() => void session.reconnect()}
          onListenOnly={() => void session.joinListenOnly()}
        />
      ) : null}

      {connected ? (
        <div className="grid gap-6 lg:grid-cols-[2fr_1fr]" data-testid="call-connected">
          <div className="flex flex-col gap-4">
            {snapshot.remoteProblems > 0 ? (
              <Alert tone="warning">
                Could not receive audio or video from {snapshot.remoteProblems} {snapshot.remoteProblems === 1 ? 'participant' : 'participants'}. Retrying.
              </Alert>
            ) : null}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2" aria-label="Participants video">
              {snapshot.localStream ? <MediaTile name="You" stream={snapshot.localStream} local muted={snapshot.micMuted} /> : null}
              {snapshot.remotes.map((remote, index) => (
                <MediaTile
                  key={remote.participantId}
                  name={nameOf(remote.participantId, index)}
                  stream={remote.stream}
                  connecting={remote.state === 'connecting'}
                />
              ))}
            </div>
            {snapshot.localStream === null && snapshot.remotes.length === 0 ? (
              <p className="text-sm text-fg-muted">
                {snapshot.listenOnly ? 'You are listening only. Nobody is speaking yet.' : 'Waiting for others to speak.'}
              </p>
            ) : null}
            <CallControls session={session} snapshot={snapshot} onLeave={() => void leave()} leaving={snapshot.phase === 'leaving'} devices={overrides?.devices} />
          </div>
          <Card as="section" aria-label="People and hands">
            <CardBody className="flex flex-col gap-6">
              {moderator ? (
                <>
                  {modError ? <CallsActionError error={modError} onDismiss={() => setModError(null)} /> : null}
                  {modNotice ? (
                    <Alert tone="success" onDismiss={() => setModNotice(null)}>
                      {modNotice}
                    </Alert>
                  ) : null}
                  <RoomDetail
                    communityId={communityId}
                    room={room}
                    onNotice={(text) => {
                      setModError(null);
                      setModNotice(text);
                    }}
                    onActionError={(error) => {
                      setModNotice(null);
                      setModError(error);
                    }}
                  />
                </>
              ) : (
                <>
                  <MyHandControl communityId={communityId} roomId={room.roomId} />
                  {presence.error ? (
                    <CallsErrorPanel error={presence.error} onRetry={presence.refetch} />
                  ) : (
                    <RoomPresence total={presence.total} speaking={presence.speaking} />
                  )}
                </>
              )}
            </CardBody>
          </Card>
        </div>
      ) : null}
    </section>
  );
}
