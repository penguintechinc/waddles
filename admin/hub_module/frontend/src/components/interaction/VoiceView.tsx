import { useState } from 'react';
import { HandRaisedIcon, PhoneXMarkIcon, PlusIcon, SpeakerWaveIcon, UserGroupIcon } from '@heroicons/react/24/outline';
import { Badge, Button, EmptyState, ErrorState, LoadingState } from '../ui';
import { CallsActionError, CallsErrorPanel } from '../calls/CallsErrors';
import { CreateRoomForm } from '../calls/CreateRoomForm';
import { MediaJoinNotice } from '../calls/MediaJoinNotice';
import { useCallRooms, useCreateRoom, useJoinRoom, useLeaveRoom, useLowerHand, useRaiseHand } from '../../hooks/useCalls';
import { roomTitle, type CallRoom, type CallsApiError } from '../../services/callsApi';

/** Channel record handed over by the community interaction page. */
export interface VoiceChannel {
  /** Route param (string) or number; validated before any request. */
  communityId: number | string;
  name?: string;
  allowAdHocVoice?: boolean;
}

/** People currently in a room: speakers (publishers) plus listeners (subscribers). */
function headcount(room: CallRoom): number {
  return room.publisherIds.length + room.subscriberCount;
}

interface InCallProps {
  communityId: number;
  room: CallRoom;
  onLeft: () => void;
}

/**
 * The joined-room view. Membership and hands are real (hub-api); media is not: the browser WHIP/WHEP
 * client does not exist yet, so the notice says so instead of showing fabricated participants or controls.
 * Members only get raise/lower hand and leave; moderation lives in the admin console.
 */
function InCall({ communityId, room, onLeft }: InCallProps) {
  const leave = useLeaveRoom(communityId);
  const raise = useRaiseHand(communityId);
  const lower = useLowerHand(communityId);
  const [handRaised, setHandRaised] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const [error, setError] = useState<CallsApiError | null>(null);

  const toggleHand = () => {
    setError(null);
    if (handRaised) {
      lower.mutate(room.roomId, {
        onSuccess: (lowered) => {
          setHandRaised(false);
          setAnnouncement(lowered ? 'You lowered your hand.' : 'Your hand was already lowered.');
        },
        onError: setError,
      });
      return;
    }
    raise.mutate(room.roomId, {
      onSuccess: () => {
        setHandRaised(true);
        setAnnouncement('You raised your hand. A moderator will acknowledge you.');
      },
      onError: setError,
    });
  };

  const onLeave = () => {
    setError(null);
    leave.mutate(room.roomId, { onSuccess: onLeft, onError: setError });
  };

  return (
    <div className="flex h-full flex-col" data-testid="voice-in-call">
      <div className="flex items-center gap-3 border-b border-navy-700 px-4 py-3">
        <SpeakerWaveIcon aria-hidden="true" className="h-5 w-5 text-gold-400" />
        <h2 className="font-semibold text-sky-100">{roomTitle(room)}</h2>
        <span className="ml-auto flex items-center gap-1 text-sm text-navy-300">
          <UserGroupIcon aria-hidden="true" className="h-4 w-4" />
          {headcount(room)} {headcount(room) === 1 ? 'participant' : 'participants'}
        </span>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        <MediaJoinNotice />
        {error ? <CallsActionError error={error} onDismiss={() => setError(null)} /> : null}
        <p className="text-sm text-navy-300" data-testid="hand-state">
          {handRaised ? 'Your hand is raised. A moderator will acknowledge it.' : 'Your hand is down.'}
        </p>
        <div role="status" aria-live="polite" aria-atomic="true" className="sr-only" data-testid="voice-live-region">
          {announcement}
        </div>
      </div>

      <div className="flex justify-center gap-3 border-t border-navy-700 bg-navy-800 p-4">
        <Button
          variant={handRaised ? 'primary' : 'secondary'}
          aria-pressed={handRaised}
          loading={raise.isPending || lower.isPending}
          leadingIcon={<HandRaisedIcon className="h-4 w-4" />}
          onClick={toggleHand}
        >
          {handRaised ? 'Lower hand' : 'Raise hand'}
        </Button>
        <Button variant="danger" loading={leave.isPending} leadingIcon={<PhoneXMarkIcon className="h-4 w-4" />} onClick={onLeave}>
          Leave room
        </Button>
      </div>
    </div>
  );
}

/** Member voice view: rooms listed by label, join one, raise/lower a hand. Flag-off and service errors are explicit states. */
export default function VoiceView({ channel }: { channel: VoiceChannel }) {
  const communityId = Number(channel.communityId);
  if (!Number.isInteger(communityId) || communityId <= 0) {
    return <ErrorState title="Unknown community" message="This voice channel link is not valid." />;
  }
  return <VoiceRooms channel={channel} communityId={communityId} />;
}

/** The room list / in-call switch for one validated community. */
function VoiceRooms({ channel, communityId }: { channel: VoiceChannel; communityId: number }) {
  const rooms = useCallRooms('member', communityId);
  const create = useCreateRoom('member', communityId);
  const join = useJoinRoom(communityId);
  const [showCreate, setShowCreate] = useState(false);
  const [active, setActive] = useState<CallRoom | null>(null);
  const [actionError, setActionError] = useState<CallsApiError | null>(null);

  if (active) {
    // Keep the head count live from the polled list while the call view is open.
    const fresh = rooms.data?.find((room) => room.roomId === active.roomId) ?? active;
    return <InCall communityId={communityId} room={fresh} onLeft={() => setActive(null)} />;
  }

  const flagOff = rooms.isError && rooms.error.kind === 'feature-disabled';
  const onJoin = (room: CallRoom) => {
    setActionError(null);
    // The room token is only for a media client, which does not exist yet; it is intentionally dropped here.
    join.mutate(room.roomId, { onSuccess: () => setActive(room), onError: setActionError });
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-navy-700 px-4 py-3">
        <h2 className="flex items-center gap-2 font-semibold text-sky-100">
          <SpeakerWaveIcon aria-hidden="true" className="h-5 w-5 text-gold-400" />
          {channel.name ?? 'Voice'}
        </h2>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="ghost" loading={rooms.isFetching && !rooms.isPending} onClick={() => void rooms.refetch()}>
            Refresh
          </Button>
          {channel.allowAdHocVoice === true && !flagOff ? (
            <Button size="sm" leadingIcon={<PlusIcon className="h-4 w-4" />} onClick={() => setShowCreate(true)} disabled={showCreate}>
              Create room
            </Button>
          ) : null}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {actionError ? <CallsActionError error={actionError} onDismiss={() => setActionError(null)} /> : null}
        {showCreate && !flagOff ? (
          <CreateRoomForm
            submitting={create.isPending}
            onCancel={() => setShowCreate(false)}
            onSubmit={(input) =>
              create.mutate(input, { onSuccess: () => setShowCreate(false), onError: setActionError })
            }
          />
        ) : null}

        {rooms.isPending ? (
          <LoadingState label="Loading voice rooms" />
        ) : rooms.isError ? (
          <CallsErrorPanel error={rooms.error} onRetry={() => void rooms.refetch()} />
        ) : rooms.data.length === 0 ? (
          <EmptyState
            icon={<SpeakerWaveIcon />}
            title="No voice rooms available"
            description={channel.allowAdHocVoice === true ? 'Create a room to start a conversation.' : 'A moderator can open a room for the community.'}
            testId="voice-no-rooms"
          />
        ) : (
          <ul className="flex flex-col gap-3">
            {rooms.data.map((room) => (
              <li key={room.roomId} className="flex items-center justify-between rounded-lg border border-navy-700 bg-navy-800 p-4">
                <div className="min-w-0">
                  <span className="block truncate font-medium text-sky-100">{roomTitle(room)}</span>
                  <span className="mt-1 flex items-center gap-1 text-sm text-navy-300">
                    <UserGroupIcon aria-hidden="true" className="h-3.5 w-3.5" />
                    {headcount(room)} {headcount(room) === 1 ? 'participant' : 'participants'}
                    {room.locked ? (
                      <Badge tone="gold" className="ml-2">
                        Locked
                      </Badge>
                    ) : null}
                  </span>
                </div>
                <Button
                  variant="primary"
                  size="sm"
                  disabled={room.locked}
                  loading={join.isPending && join.variables === room.roomId}
                  onClick={() => onJoin(room)}
                  aria-label={`Join ${roomTitle(room)}`}
                >
                  Join
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
