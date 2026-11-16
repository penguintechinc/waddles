import { MicrophoneIcon } from '@heroicons/react/24/outline';
import { Button, LoadingState } from '../ui';
import {
  useAcknowledgeHand,
  useKick,
  useMuteAll,
  useRaisedHands,
  useResetHands,
  useRoomParticipants,
  useSetMuted,
} from '../../hooks/useCalls';
import { roomTitle, type CallRoom, type CallsApiError } from '../../services/callsApi';
import { CallsErrorPanel } from './CallsErrors';
import { HandsQueue } from './HandsQueue';
import { ParticipantList } from './ParticipantList';

export interface RoomDetailProps {
  communityId: number;
  room: Pick<CallRoom, 'roomId' | 'label'>;
  onNotice: (text: string) => void;
  onActionError: (error: CallsApiError) => void;
}

/**
 * Moderator pane for one room: the ordered hands queue (acknowledge / clear) and the participant list
 * (mute / remove / mute all), all by display name. Reads are polled; every failure is shown, never hidden.
 */
export function RoomDetail({ communityId, room, onNotice, onActionError }: RoomDetailProps) {
  const participants = useRoomParticipants(communityId, room.roomId);
  const hands = useRaisedHands(communityId, room.roomId);
  const acknowledge = useAcknowledgeHand(communityId);
  const reset = useResetHands(communityId);
  const mute = useSetMuted(communityId);
  const kick = useKick(communityId);
  const muteEveryone = useMuteAll(communityId);

  const names = new Map([...(participants.data?.participants ?? []), ...(hands.data ?? [])].map((p) => [p.participantId, p.displayName]));
  const nameOf = (id: string): string => names.get(id) ?? 'the participant';

  return (
    <div data-testid="room-detail">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="section-title">{roomTitle(room)}</h2>
        <Button
          size="sm"
          variant="outline"
          loading={muteEveryone.isPending}
          leadingIcon={<MicrophoneIcon className="h-4 w-4" />}
          onClick={() =>
            muteEveryone.mutate(room.roomId, {
              onSuccess: (count) => onNotice(`Muted ${count} ${count === 1 ? 'speaker' : 'speakers'}.`),
              onError: onActionError,
            })
          }
        >
          Mute all
        </Button>
      </div>

      <div className="mb-6">
        {hands.isPending ? (
          <LoadingState label="Loading raised hands" className="py-6" />
        ) : hands.isError ? (
          <CallsErrorPanel error={hands.error} onRetry={() => void hands.refetch()} />
        ) : (
          <HandsQueue
            hands={hands.data}
            acknowledgingId={acknowledge.isPending ? acknowledge.variables.participantId : null}
            resetting={reset.isPending}
            onAcknowledge={(participantId) =>
              acknowledge.mutate(
                { roomId: room.roomId, participantId },
                { onSuccess: () => onNotice(`Acknowledged ${nameOf(participantId)}.`), onError: onActionError },
              )
            }
            onReset={() =>
              reset.mutate(room.roomId, {
                onSuccess: (count) => onNotice(`Cleared ${count} ${count === 1 ? 'hand' : 'hands'}.`),
                onError: onActionError,
              })
            }
          />
        )}
      </div>

      <h3 className="mb-2 text-sm font-medium text-fg-secondary">Participants ({participants.data?.participants.length ?? 0})</h3>
      {participants.isPending ? (
        <LoadingState label="Loading participants" className="py-6" />
      ) : participants.isError ? (
        <CallsErrorPanel error={participants.error} onRetry={() => void participants.refetch()} />
      ) : (
        <ParticipantList
          participants={participants.data.participants}
          busyId={mute.isPending ? mute.variables.participantId : kick.isPending ? kick.variables.participantId : null}
          onMute={(participantId, muted) =>
            mute.mutate(
              { roomId: room.roomId, participantId, muted },
              {
                onSuccess: () => onNotice(`${muted ? 'Muted' : 'Unmuted'} ${nameOf(participantId)}.`),
                onError: onActionError,
              },
            )
          }
          onKick={(participantId) =>
            kick.mutate(
              { roomId: room.roomId, participantId },
              { onSuccess: () => onNotice(`Removed ${nameOf(participantId)} from the room.`), onError: onActionError },
            )
          }
        />
      )}
    </div>
  );
}
