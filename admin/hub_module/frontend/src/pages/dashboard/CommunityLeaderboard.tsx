import { useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { Button, DataTable, ErrorState, PageHeader, Tabs, type Column, type TabItem } from '../../components/ui';
import { MyStatsCard } from '../../components/leaderboard/MyStatsCard';
import { PeriodSelector } from '../../components/leaderboard/PeriodSelector';
import { avatarInitial, formatEntryValue } from '../../components/leaderboard/leaderboardFormat';
import { useLeaderboard } from '../../hooks/useCommunityActivity';
import { useMyCommunities } from '../../hooks/useMyCommunities';
import type { LeaderboardEntry, LeaderboardPeriod, LeaderboardType } from '../../services/communityActivityApi';

const PAGE_SIZE = 25;

const TABS: readonly TabItem[] = [
  { id: 'watch-time', label: 'Watch Time' },
  { id: 'messages', label: 'Messages' },
];

/** Narrows the `?type=` query value to a known leaderboard tab. */
function toType(value: string | null): LeaderboardType {
  return value === 'messages' ? 'messages' : 'watch-time';
}

/** Full leaderboard: tabs, period filter, personal stats and server-side pagination. */
function LeaderboardView({ communityId }: { communityId: number }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const type = toType(searchParams.get('type'));
  const [period, setPeriod] = useState<LeaderboardPeriod>('weekly');
  const [offset, setOffset] = useState(0);
  const mine = useMyCommunities();
  const communityName = mine.data?.find((c) => c.id === communityId)?.displayName ?? 'Community';
  const query = useLeaderboard(communityId, type, { period, limit: PAGE_SIZE, offset });

  const columns: readonly Column<LeaderboardEntry>[] = [
    { key: 'rank', header: 'Rank', cell: (entry) => <span className="font-bold text-fg">{entry.rank}</span> },
    {
      key: 'user',
      header: 'User',
      cell: (entry) => (
        <span className="flex items-center gap-3">
          {entry.avatarUrl ? (
            <img src={entry.avatarUrl} alt="" className="h-10 w-10 rounded-full object-cover" />
          ) : (
            <span
              aria-hidden="true"
              className="flex h-10 w-10 items-center justify-center rounded-full bg-line font-semibold text-fg"
            >
              {avatarInitial(entry.username)}
            </span>
          )}
          <span className="font-medium text-fg">{entry.username ?? 'Unknown'}</span>
        </span>
      ),
    },
    {
      key: 'value',
      header: type === 'watch-time' ? 'Watch Time' : 'Messages',
      align: 'right',
      cell: (entry) => <span className="text-lg font-bold text-fg-accent">{formatEntryValue(type, entry)}</span>,
    },
  ];

  const page = query.data;
  const firstShown = page && page.total > 0 ? page.offset + 1 : 0;
  const lastShown = page ? Math.min(page.offset + page.limit, page.total) : 0;

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader
        title="Community Leaderboard"
        description="See who's most active in the community"
        breadcrumbs={[
          { label: 'Dashboard', to: '/dashboard' },
          { label: communityName, to: `/dashboard/community/${communityId}` },
          { label: 'Leaderboard' },
        ]}
      />
      <MyStatsCard communityId={communityId} />
      <div className="mb-4 flex flex-wrap items-center justify-end gap-3">
        <PeriodSelector
          value={period}
          onChange={(next) => {
            setPeriod(next);
            setOffset(0);
          }}
        />
      </div>
      <Tabs
        tabs={TABS}
        value={type}
        label="Leaderboard type"
        onChange={(id) => {
          setSearchParams({ type: id });
          setOffset(0);
        }}
      >
        <DataTable
          caption={`${type === 'watch-time' ? 'Watch time' : 'Message'} leaderboard, ${period}`}
          columns={columns}
          rows={page?.entries}
          getRowId={(entry) => entry.rank}
          loading={query.isPending}
          error={query.isError ? query.error : undefined}
          onRetry={() => void query.refetch()}
          emptyTitle="No activity data yet for this period"
        />
        {page && page.total > page.limit ? (
          <nav aria-label="Leaderboard pages" className="mt-4 flex items-center justify-between gap-3">
            <p className="text-sm text-fg-secondary">
              Showing {firstShown} to {lastShown} of {page.total}
            </p>
            <div className="flex gap-2">
              <Button disabled={offset === 0 || query.isPlaceholderData} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                Previous
              </Button>
              <Button disabled={!page.hasMore || query.isPlaceholderData} onClick={() => setOffset(offset + PAGE_SIZE)}>
                Next
              </Button>
            </div>
          </nav>
        ) : null}
      </Tabs>
    </div>
  );
}

/** Route wrapper: validates `:id` before any query runs. */
export default function CommunityLeaderboard() {
  const { id } = useParams<{ id: string }>();
  const communityId = Number(id);
  if (!Number.isInteger(communityId) || communityId <= 0) {
    return <ErrorState title="Unknown community" message="This leaderboard link is not valid." />;
  }
  return <LeaderboardView communityId={communityId} />;
}
