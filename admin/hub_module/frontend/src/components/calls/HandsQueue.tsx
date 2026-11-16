import { HandRaisedIcon } from '@heroicons/react/24/outline';
import { Badge, Button } from '../ui';
import type { RaisedHand } from '../../services/callsApi';
import { useHandAnnouncements } from './useHandAnnouncements';

export interface HandsQueueProps {
  hands: readonly RaisedHand[];
  /** Id of the hand currently being acknowledged, to show its busy state. */
  acknowledgingId?: string | null;
  resetting?: boolean;
  onAcknowledge: (participantId: string) => void;
  onReset: () => void;
}

/** `10:42:07`-style local time for a unix-seconds timestamp. */
function clock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString();
}

/**
 * Moderator view of the raised-hands queue: ordered oldest first (position 1 speaks next), each hand
 * with an Acknowledge action, plus Clear all. Changes are announced through a polite live region.
 */
export function HandsQueue({ hands, acknowledgingId = null, resetting = false, onAcknowledge, onReset }: HandsQueueProps) {
  const announcement = useHandAnnouncements(hands);
  return (
    <section aria-labelledby="hands-queue-title" data-testid="hands-queue">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 id="hands-queue-title" className="flex items-center gap-1.5 text-sm font-medium text-fg-secondary">
          <HandRaisedIcon aria-hidden="true" className="h-4 w-4 text-warning-fg" />
          Raised hands ({hands.length})
        </h3>
        {hands.length > 0 ? (
          <Button size="sm" variant="outline" loading={resetting} onClick={onReset}>
            Clear all hands
          </Button>
        ) : null}
      </div>
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only" data-testid="hands-live-region">
        {announcement}
      </div>
      {hands.length === 0 ? (
        <p className="text-sm text-fg-muted">No raised hands.</p>
      ) : (
        <ol className="space-y-2">
          {hands.map((hand, index) => (
            <li key={hand.participantId} className="flex items-center justify-between gap-3 rounded-control bg-surface-raised p-2">
              <div className="min-w-0">
                <span className="text-sm font-medium text-fg">
                  <span className="mr-2 text-fg-muted">{index + 1}.</span>
                  {hand.displayName}
                </span>
                <span className="ml-2 text-xs text-fg-muted">raised at {clock(hand.raisedAt)}</span>
                {hand.acknowledgedAt !== null ? (
                  <Badge tone="green" className="ml-2">
                    Acknowledged
                  </Badge>
                ) : null}
              </div>
              {hand.acknowledgedAt === null ? (
                <Button
                  size="sm"
                  variant="primary"
                  loading={acknowledgingId === hand.participantId}
                  onClick={() => onAcknowledge(hand.participantId)}
                  aria-label={`Acknowledge ${hand.displayName}`}
                >
                  Acknowledge
                </Button>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
