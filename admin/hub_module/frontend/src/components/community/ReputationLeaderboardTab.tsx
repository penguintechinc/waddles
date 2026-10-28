import { useReputationLeaderboard } from '../../hooks/useCommunityPanels';
import type { ReputationEntry } from '../../services/reputationApi';
import { scorePercent } from './reputationScore';
import { Badge, DataTable, type BadgeTone, type Column } from '../ui';

/** Bar/badge colour from the FICO-style 300-850 score; the tier NAME always comes from the server. */
function toneForScore(score: number): BadgeTone {
  if (score >= 740) return 'green';
  if (score >= 670) return 'sky';
  if (score >= 580) return 'gold';
  return 'red';
}

const BAR_FILL: Record<BadgeTone, string> = {
  green: 'bg-success',
  sky: 'bg-info',
  gold: 'bg-warning',
  red: 'bg-danger',
  gray: 'bg-line-strong',
};

/**
 * Top reputation scorers for a community, from the served
 * `GET /api/v1/community/:id/reputation/leaderboard` (display names only; the server exposes no ids or avatars).
 */
export function ReputationLeaderboardTab({ communityId }: { communityId: number }) {
  const query = useReputationLeaderboard(communityId, 50);

  const columns: readonly Column<ReputationEntry & { rank: number }>[] = [
    { key: 'rank', header: 'Rank', className: 'w-16', cell: (row) => <span className={`font-bold ${row.rank <= 3 ? 'text-fg-accent' : 'text-fg-secondary'}`}>#{row.rank}</span> },
    { key: 'member', header: 'Member', cell: (row) => <span className="font-medium text-fg">{row.displayName}</span> },
    { key: 'tier', header: 'Tier', cell: (row) => <Badge tone={toneForScore(row.score)}>{row.tier}</Badge> },
    {
      key: 'score',
      header: 'Score',
      align: 'right',
      cell: (row) => {
        const tone = toneForScore(row.score);
        return (
          <span className="flex items-center justify-end gap-3">
            <span aria-hidden="true" className="h-2 w-24 overflow-hidden rounded-full bg-line">
              <span className={`block h-full ${BAR_FILL[tone]}`} style={{ width: `${scorePercent(row.score)}%` }} />
            </span>
            <span className="w-12 text-right font-bold text-fg">{row.score}</span>
          </span>
        );
      },
    },
  ];

  return (
    <DataTable
      caption="Reputation leaderboard"
      columns={columns}
      rows={query.data?.map((entry, index) => ({ ...entry, rank: index + 1 }))}
      getRowId={(row) => row.rank}
      loading={query.isPending}
      error={query.isError ? query.error : undefined}
      onRetry={() => void query.refetch()}
      emptyTitle="No Reputation Data"
      emptyDescription="No members with reputation scores yet."
    />
  );
}
