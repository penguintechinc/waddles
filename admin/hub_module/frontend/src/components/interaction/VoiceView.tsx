import { useState } from 'react';
import { PlusIcon, SpeakerWaveIcon, UserGroupIcon } from '@heroicons/react/24/outline';
import { Badge, Button, EmptyState, ErrorState, LoadingState } from '../ui';
import { useAuthView } from '../../contexts/authView';
import { CallRoomView } from '../calls/CallRoomView';
import { CallsActionError, CallsErrorPanel } from '../calls/CallsErrors';
import { CreateRoomForm } from '../calls/CreateRoomForm';
import { useCallRooms, useCreateRoom } from '../../hooks/useCalls';
import { roomTitle, type CallRoom, type CallsApiError } from '../../services/callsApi';
import type { SessionDeps } from '../../services/media/callSession';

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

/** Member voice channel: rooms listed by label; opening one shows the real audio/video call view. */
export default function VoiceView({ channel, overrides }: { channel: VoiceChannel; overrides?: Pick<SessionDeps, 'devices' | 'media'> }) {
  const communityId = Number(channel.communityId);
  if (!Number.isInteger(communityId) || communityId <= 0) {
    return <ErrorState title="Unknown community" message="This voice channel link is not valid." />;
  }
  return <VoiceRooms channel={channel} communityId={communityId} overrides={overrides} />;
}

/** The room list / call switch for one validated community. */
function VoiceRooms({
  channel,
  communityId,
  overrides,
}: {
  channel: VoiceChannel;
  communityId: number;
  overrides: Pick<SessionDeps, 'devices' | 'media'> | undefined;
}) {
  const { isCommunityAdmin } = useAuthView();
  const rooms = useCallRooms('member', communityId);
  const create = useCreateRoom('member', communityId);
  const [showCreate, setShowCreate] = useState(false);
  const [active, setActive] = useState<CallRoom | null>(null);
  const [actionError, setActionError] = useState<CallsApiError | null>(null);

  if (active) {
    return (
      <div className="flex h-full flex-col overflow-y-auto p-4">
        <CallRoomView
          communityId={communityId}
          room={active}
          moderator={isCommunityAdmin(communityId)}
          onLeft={() => setActive(null)}
          {...(overrides ? { overrides } : {})}
        />
      </div>
    );
  }

  const flagOff = rooms.isError && rooms.error.kind === 'feature-disabled';

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
            onSubmit={(input) => create.mutate(input, { onSuccess: () => setShowCreate(false), onError: setActionError })}
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
                <Button variant="primary" size="sm" disabled={room.locked} onClick={() => setActive(room)} aria-label={`Open ${roomTitle(room)}`}>
                  Open
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
