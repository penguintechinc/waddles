import { useMyActivityStats } from '../../hooks/useCommunityActivity';
import { getErrorMessage } from '../ui/errors';
import { Alert, Skeleton } from '../ui';

function Stat({ value, label, tone }: { value: string; label: string; tone: string }) {
  return (
    <div className="text-center">
      <div className={`text-2xl font-bold ${tone}`}>{value}</div>
      <div className="text-xs text-fg-secondary">{label}</div>
    </div>
  );
}

/** The caller's weekly stats and ranks. A failure is shown inline; it never hides the leaderboard below. */
export function MyStatsCard({ communityId }: { communityId: number }) {
  const query = useMyActivityStats(communityId);
  if (query.isPending) return <Skeleton className="mb-6 h-24" />;
  if (query.isError) {
    return (
      <Alert tone="warning" title="We could not load your stats" className="mb-6">
        {getErrorMessage(query.error)}
      </Alert>
    );
  }
  const { weekly, ranks } = query.data;
  return (
    <section className="card mb-6 p-6" aria-labelledby="my-stats-heading" data-testid="my-stats">
      <h2 id="my-stats-heading" className="mb-4 font-semibold text-fg">
        Your Stats
      </h2>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat value={weekly.watchTimeFormatted} label="Weekly Watch Time" tone="text-fg-accent" />
        <Stat value={weekly.messageCount.toLocaleString()} label="Weekly Messages" tone="text-interactive" />
        <Stat value={ranks.watchTime ? `#${ranks.watchTime}` : '-'} label="Watch Time Rank" tone="text-fg-accent" />
        <Stat value={ranks.messages ? `#${ranks.messages}` : '-'} label="Messages Rank" tone="text-success-fg" />
      </div>
    </section>
  );
}
