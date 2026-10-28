import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { SignalIcon } from '@heroicons/react/24/outline';
import { LiveActivityRow } from '../../components/community/LiveActivityRow';
import { Alert, EmptyState, ErrorState, PageHeader, QueryBoundary } from '../../components/ui';
import { useLiveActivityBackfill } from '../../hooks/useCommunityActivity';
import { mergeEvents } from '../../components/community/liveActivityMerge';
import { parseLiveActivityEvent, type LiveActivityEvent } from '../../services/communityActivityApi';
import { getErrorMessage } from '../../components/ui/errors';

const BACKFILL_LIMIT = 50;

type ConnectionState = 'connecting' | 'live' | 'reconnecting';

const CONNECTION_LABEL: Record<ConnectionState, { label: string; dot: string }> = {
  connecting: { label: 'Connecting…', dot: 'bg-fg-muted animate-pulse' },
  live: { label: 'Live', dot: 'bg-success' },
  reconnecting: { label: 'Reconnecting…', dot: 'bg-warning animate-pulse' },
};

/**
 * Subscribes to the SSE feed (same-origin, cookie auth) and accumulates new events.
 * A frame that does not parse is reported to the page rather than dropped silently.
 */
function useLiveStream(communityId: number) {
  const [streamed, setStreamed] = useState<LiveActivityEvent[]>([]);
  const [connection, setConnection] = useState<ConnectionState>('connecting');
  const [frameError, setFrameError] = useState<string | null>(null);

  useEffect(() => {
    setStreamed([]);
    setFrameError(null);
    setConnection('connecting');
    const source = new EventSource(`/api/v1/community/${communityId}/live-activity/stream`, { withCredentials: true });
    source.onopen = () => setConnection('live');
    source.onerror = () => setConnection('reconnecting');
    source.onmessage = (message: MessageEvent<string>) => {
      try {
        const event = parseLiveActivityEvent(JSON.parse(message.data));
        setStreamed((previous) => mergeEvents([event], previous));
      } catch (error) {
        console.error('[LiveActivity] Dropped an unreadable stream frame', { reason: getErrorMessage(error) });
        setFrameError(getErrorMessage(error));
      }
    };
    return () => source.close();
  }, [communityId]);

  return { streamed, connection, frameError };
}

/** Backfill (TanStack Query) plus the live SSE tail for one community. */
function LiveActivityFeed({ communityId }: { communityId: number }) {
  const backfill = useLiveActivityBackfill(communityId, BACKFILL_LIMIT);
  const { streamed, connection, frameError } = useLiveStream(communityId);
  const status = CONNECTION_LABEL[connection];

  const statusPill = useMemo(
    () => (
      <div
        role="status"
        className="flex items-center gap-2 rounded-full border border-line bg-surface-raised px-3 py-1.5"
        data-testid="live-connection"
      >
        <span className={`h-2 w-2 rounded-full ${status.dot}`} aria-hidden="true" />
        <span className="text-sm text-fg-secondary">{status.label}</span>
      </div>
    ),
    [status],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Live Activity"
        description="Real-time Discord and Twitch messages and Waddles' replies"
        actions={statusPill}
      />
      {frameError ? (
        <Alert tone="warning" title="Some live events could not be read">
          {frameError}
        </Alert>
      ) : null}
      <QueryBoundary
        query={backfill}
        loadingLabel="Loading recent activity"
        errorTitle="We could not load recent activity"
      >
        {(history) => {
          const events = mergeEvents(streamed, history);
          return events.length === 0 ? (
            <EmptyState
              title="No Activity Yet"
              description="Messages will appear here as they come in from connected platforms."
              icon={<SignalIcon />}
            />
          ) : (
            <ul className="space-y-2" aria-label="Recent activity">
              {events.map((event) => (
                <LiveActivityRow key={event.id} event={event} />
              ))}
            </ul>
          );
        }}
      </QueryBoundary>
    </div>
  );
}

/** Admin live-activity page: validates the route param, then renders the feed. */
export default function LiveActivity() {
  const { communityId } = useParams<{ communityId: string }>();
  const id = Number(communityId);
  if (!Number.isInteger(id) || id <= 0) {
    return <ErrorState title="Unknown community" message="This live activity link is not valid." />;
  }
  return <LiveActivityFeed communityId={id} />;
}
