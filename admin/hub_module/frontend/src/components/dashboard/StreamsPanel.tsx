import type { UseQueryResult } from '@tanstack/react-query';
import LiveStreamGrid from '../streams/LiveStreamGrid';
import { PanelState } from '../ui';
import type { LiveStream } from '../../services/communityOverviewApi';

export interface StreamsPanelProps {
  communityId: number;
  query: Pick<UseQueryResult<LiveStream[]>, 'data' | 'isPending' | 'isError' | 'error' | 'refetch'>;
  /** Offer the connection-settings CTA (only for users allowed to manage connections). */
  canManage: boolean;
}

/**
 * Live streams for the dashboard. Shows an explicit state for every API outcome: empty
 * (nothing configured, with a connect CTA for admins), plan-gated, unavailable, error, or the grid.
 */
export function StreamsPanel({ communityId, query, canManage }: StreamsPanelProps) {
  return (
    <section className="mb-8" aria-label="Live streams" data-testid="streams-panel">
      <PanelState
        query={query}
        feature="Live streams"
        isEmpty={(streams) => streams.length === 0}
        emptyTitle="No streams configured for this community"
        emptyDescription="Live streams show up here once a streaming platform is connected and someone goes live."
        {...(canManage ? { emptyAction: { to: `/admin/${communityId}/connections`, label: 'Connect Twitch or Discord' } } : {})}
        className="py-8"
      >
        {(streams) => (
          <LiveStreamGrid streams={streams} onRefresh={() => void query.refetch()} loading={false} error={null} />
        )}
      </PanelState>
    </section>
  );
}
