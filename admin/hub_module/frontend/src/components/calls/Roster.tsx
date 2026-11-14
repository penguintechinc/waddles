import { Badge } from '../ui';
import type { CallParticipant } from '../../services/callsApi';

/** Read-only member view of who is in the room, by display name. */
export function Roster({ participants }: { participants: readonly CallParticipant[] }) {
  return (
    <section aria-labelledby="roster-title" data-testid="roster">
      <h3 id="roster-title" className="mb-2 text-sm font-medium text-fg-secondary">
        In this room ({participants.length})
      </h3>
      {participants.length === 0 ? (
        <p className="text-sm text-fg-muted">Nobody is here yet.</p>
      ) : (
        <ul className="space-y-1.5">
          {participants.map((p) => (
            <li key={p.participantId} className="flex items-center gap-2 text-sm text-fg">
              <span className="truncate">{p.displayName}</span>
              {p.publisher ? <Badge tone="sky">Speaking</Badge> : <Badge>Listening</Badge>}
              {p.muted ? <Badge tone="red">Muted</Badge> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
