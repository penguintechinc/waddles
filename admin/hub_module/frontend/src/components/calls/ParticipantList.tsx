import { Badge, Button } from '../ui';
import type { CallParticipant } from '../../services/callsApi';

export interface ParticipantListProps {
  participants: readonly CallParticipant[];
  /** Participant id with an action in flight, so only that row shows a busy state. */
  busyId?: string | null;
  onMute: (participantId: string, muted: boolean) => void;
  onKick: (participantId: string) => void;
}

/** Moderator list of a room's participants by display name (never by id) with mute and remove actions. */
export function ParticipantList({ participants, busyId = null, onMute, onKick }: ParticipantListProps) {
  if (participants.length === 0) return <p className="text-sm text-fg-muted">Nobody is in this room.</p>;
  return (
    <ul className="max-h-72 space-y-2 overflow-y-auto" aria-label="Participants">
      {participants.map((p) => (
        <li key={p.participantId} className="flex items-center justify-between gap-3 rounded-control bg-surface-raised p-2">
          <div className="min-w-0">
            <span className="text-sm font-medium text-fg">{p.displayName}</span>
            <Badge tone={p.publisher ? 'sky' : 'gray'} className="ml-2">
              {p.publisher ? 'Speaker' : 'Listener'}
            </Badge>
            {p.muted ? (
              <Badge tone="red" className="ml-1">
                Muted
              </Badge>
            ) : null}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              loading={busyId === p.participantId}
              onClick={() => onMute(p.participantId, !p.muted)}
              aria-label={`${p.muted ? 'Unmute' : 'Mute'} ${p.displayName}`}
            >
              {p.muted ? 'Unmute' : 'Mute'}
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={busyId === p.participantId}
              onClick={() => onKick(p.participantId)}
              aria-label={`Remove ${p.displayName} from the room`}
            >
              Remove
            </Button>
          </div>
        </li>
      ))}
    </ul>
  );
}
