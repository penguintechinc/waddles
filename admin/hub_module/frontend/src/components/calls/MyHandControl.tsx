import { useEffect, useRef, useState } from 'react';
import { HandRaisedIcon } from '@heroicons/react/24/outline';
import { Alert, Button } from '../ui';
import { useLowerHand, useMyHand, useRaiseHand } from '../../hooks/useCalls';
import type { CallsApiError } from '../../services/callsApi';
import { CallsActionError } from './CallsErrors';

export interface MyHandControlProps {
  communityId: number;
  roomId: string;
}

/**
 * Member raise/lower-hand toggle. The server state (own hand + acknowledgement, polled) wins; the action responses
 * give instant feedback. Changes are announced through a polite live region.
 */
export function MyHandControl({ communityId, roomId }: MyHandControlProps) {
  const raise = useRaiseHand(communityId);
  const lower = useLowerHand(communityId);
  const mine = useMyHand(communityId, roomId, true);
  const [localRaised, setLocalRaised] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const [error, setError] = useState<CallsApiError | null>(null);

  const raised = mine.data !== undefined ? mine.data !== null : localRaised;
  const acknowledged = mine.data?.acknowledgedAt != null;

  const wasAcknowledged = useRef(false);
  useEffect(() => {
    if (acknowledged && !wasAcknowledged.current) setAnnouncement('A moderator acknowledged your raised hand.');
    wasAcknowledged.current = acknowledged;
  }, [acknowledged]);

  const toggle = () => {
    setError(null);
    if (raised) {
      lower.mutate(roomId, {
        onSuccess: (lowered) => {
          setLocalRaised(false);
          setAnnouncement(lowered ? 'You lowered your hand.' : 'Your hand was already lowered.');
        },
        onError: setError,
      });
      return;
    }
    raise.mutate(roomId, {
      onSuccess: () => {
        setLocalRaised(true);
        setAnnouncement('You raised your hand. A moderator will acknowledge you.');
      },
      onError: setError,
    });
  };

  return (
    <section aria-label="Your hand" data-testid="my-hand">
      {error ? <CallsActionError error={error} onDismiss={() => setError(null)} /> : null}
      <Button
        variant={raised ? 'primary' : 'secondary'}
        aria-pressed={raised}
        loading={raise.isPending || lower.isPending}
        leadingIcon={<HandRaisedIcon className="h-4 w-4" />}
        onClick={toggle}
      >
        {raised ? 'Lower hand' : 'Raise hand'}
      </Button>
      <p className="mt-2 text-sm text-fg-secondary" data-testid="hand-state">
        {!raised ? 'Your hand is down.' : acknowledged ? 'A moderator acknowledged your hand.' : 'Your hand is raised. A moderator will acknowledge it.'}
      </p>
      {mine.isError ? (
        <Alert tone="warning" className="mt-2">
          Could not check whether a moderator acknowledged your hand: {mine.error.message}
        </Alert>
      ) : null}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only" data-testid="hand-live-region">
        {announcement}
      </div>
    </section>
  );
}
