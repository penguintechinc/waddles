import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useLeaderboard } from '../../hooks/useCommunityActivity';
import type { LeaderboardEntry, LeaderboardPeriod, LeaderboardType } from '../../services/communityActivityApi';
import { EmptyState, QueryBoundary } from '../ui';
import { avatarInitial, formatEntryValue } from './leaderboardFormat';
import { PeriodSelector } from './PeriodSelector';

export interface LeaderboardCardProps {
  communityId: number;
  type?: LeaderboardType;
  title?: string;
  limit?: number;
}

const RANK_STYLE: Record<number, string> = {
  1: 'bg-accent text-fg-inverse',
  2: 'bg-line-strong text-fg',
  3: 'bg-warning/80 text-fg-inverse',
};

function EntryRow({ entry, type }: { entry: LeaderboardEntry; type: LeaderboardType }) {
  return (
    <li className="flex items-center gap-3 rounded-lg bg-surface-raised/60 p-2" data-testid="leaderboard-card-row">
      <span
        className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold ${RANK_STYLE[entry.rank] ?? 'bg-line text-fg-secondary'}`}
      >
        {entry.rank}
      </span>
      {entry.avatarUrl ? (
        <img src={entry.avatarUrl} alt="" className="h-8 w-8 rounded-full object-cover" />
      ) : (
        <span
          aria-hidden="true"
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-line text-sm font-semibold text-fg"
        >
          {avatarInitial(entry.username)}
        </span>
      )}
      <span className="min-w-0 flex-1 truncate text-sm font-medium text-fg">{entry.username ?? 'Unknown'}</span>
      <span className="text-sm font-semibold text-fg-accent">{formatEntryValue(type, entry)}</span>
    </li>
  );
}

/**
 * Compact top-N leaderboard for the community dashboard sidebar. Reads the served
 * `/community/:id/leaderboard/*` routes; failure shows a retryable error, never a blank card.
 */
export function LeaderboardCard({ communityId, type = 'watch-time', title, limit = 5 }: LeaderboardCardProps) {
  const [period, setPeriod] = useState<LeaderboardPeriod>('weekly');
  const query = useLeaderboard(communityId, type, { period, limit, offset: 0 });
  const heading = title ?? (type === 'watch-time' ? 'Watch Time' : 'Messages');

  return (
    <section className="card p-4" aria-label={heading} data-testid={`leaderboard-card-${type}`}>
      <div className="mb-4 flex items-center justify-between gap-2">
        <h3 className="font-semibold text-fg">{heading}</h3>
        <PeriodSelector value={period} onChange={setPeriod} compact />
      </div>
      <QueryBoundary
        query={query}
        loadingLabel={`Loading ${heading}`}
        errorTitle={`We could not load ${heading}`}
        isEmpty={(page) => page.entries.length === 0}
        empty={<EmptyState title="No data yet for this period" headingLevel={4} className="py-8" />}
      >
        {(page) => (
          <ol className="space-y-2" aria-label={`${heading} ranking`}>
            {page.entries.map((entry) => (
              <EntryRow key={entry.rank} entry={entry} type={type} />
            ))}
          </ol>
        )}
      </QueryBoundary>
      <Link
        to={`/dashboard/community/${communityId}/leaderboard?type=${type}`}
        className="mt-4 block text-center text-sm text-interactive hover:text-interactive-hover"
      >
        View Full Leaderboard
      </Link>
    </section>
  );
}
