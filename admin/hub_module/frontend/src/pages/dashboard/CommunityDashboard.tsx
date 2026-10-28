import { useCallback } from 'react';
import { Link, useParams } from 'react-router-dom';
import { AnnouncementsPanel } from '../../components/dashboard/AnnouncementsPanel';
import { CommunityHeader } from '../../components/dashboard/CommunityHeader';
import { isCommunityAdminRole } from '../../components/dashboard/communityRoles';
import CommunityReputationPanel from '../../components/dashboard/CommunityReputationPanel';
import QuickActionsWidget from '../../components/dashboard/QuickActionsWidget';
import { LeaderboardCard } from '../../components/leaderboard/LeaderboardCard';
import LiveStreamGrid from '../../components/streams/LiveStreamGrid';
import BotScoreCard from '../../components/BotScoreCard';
import { Card, CardHeader, EmptyState, ErrorState, NotAvailableYet, QueryBoundary, ServedFeature } from '../../components/ui';
import { useAuthView } from '../../contexts/authView';
import { useLiveStreams } from '../../hooks/useCommunityPanels';
import { useMyCommunities, usePublicCommunity } from '../../hooks/useMyCommunities';
import type { MyCommunity } from '../../services/myCommunitiesApi';

function StatTile({ value, label, tone }: { value: string; label: string; tone: string }) {
  return (
    <div className={`card border-l-4 p-4 ${tone}`}>
      <div className="text-2xl font-bold text-fg">{value}</div>
      <div className="text-sm text-fg-secondary">{label}</div>
    </div>
  );
}

/** Everything on the dashboard that needs the caller to be a member. */
function DashboardBody({ membership }: { membership: MyCommunity }) {
  const communityId = membership.id;
  const { isSuperAdmin } = useAuthView();
  const details = usePublicCommunity(communityId);
  const streams = useLiveStreams(communityId);
  const refetchStreams = streams.refetch;
  const refreshStreams = useCallback(() => void refetchStreams(), [refetchStreams]);
  const canManage = isCommunityAdminRole(membership.role);
  const isOwnerOrAdmin = membership.role === 'community-owner' || membership.role === 'community-admin';
  const live = streams.data;

  return (
    <div>
      <CommunityHeader membership={membership} details={details.data} canAdminister={canManage || isSuperAdmin} />

      {live && live.length > 0 ? (
        <div className="mb-8">
          <LiveStreamGrid streams={live} onRefresh={refreshStreams} loading={false} error={null} />
        </div>
      ) : null}
      {streams.isError ? (
        <div className="mb-8" data-testid="streams-error">
          <ErrorState
            title="We could not load live streams"
            error={streams.error}
            onRetry={refreshStreams}
            className="py-6"
          />
        </div>
      ) : null}

      <AnnouncementsPanel communityId={communityId} canManage={canManage} />

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <div className="grid gap-4 sm:grid-cols-3">
            <StatTile
              value={details.data ? details.data.memberCount.toLocaleString() : '—'}
              label="Members"
              tone="border-l-interactive"
            />
            <StatTile value={membership.role.replace('community-', '')} label="Your role" tone="border-l-accent" />
            <StatTile value={live ? String(live.length) : '—'} label="Live Now" tone="border-l-success" />
          </div>
          <Card as="section" aria-label="Recent activity">
            <CardHeader title="Recent Activity" />
            <NotAvailableYet feature="community-recent-activity" className="py-8" />
          </Card>
        </div>

        <div className="space-y-6">
          {isOwnerOrAdmin ? (
            <ServedFeature feature="bot-score" fallbackClassName="card py-8">
              <BotScoreCard communityId={communityId} isPremium={false} />
            </ServedFeature>
          ) : null}
          <CommunityReputationPanel communityId={communityId} />
          <LeaderboardCard communityId={communityId} type="watch-time" title="Top Viewers" limit={5} />
          <LeaderboardCard communityId={communityId} type="messages" title="Top Chatters" limit={5} />
          <QuickActionsWidget id={String(communityId)} />
        </div>
      </div>
    </div>
  );
}

/** Member dashboard for one community, composed only from routes hub-api serves. */
function DashboardView({ communityId }: { communityId: number }) {
  const mine = useMyCommunities();
  return (
    <QueryBoundary
      query={mine}
      loadingLabel="Loading your community"
      errorTitle="We could not load this community"
      isEmpty={(communities: MyCommunity[]) => !communities.some((c) => c.id === communityId)}
      empty={
        <div data-testid="not-a-member">
          <EmptyState
            title="You are not a member of this community"
            description="Join it first to see its dashboard."
            action={
              <Link to={`/communities/${communityId}`} className="btn btn-primary">
                View community
              </Link>
            }
          />
        </div>
      }
    >
      {(communities) => {
        const membership = communities.find((c) => c.id === communityId);
        return membership ? <DashboardBody membership={membership} /> : null;
      }}
    </QueryBoundary>
  );
}

/** Route wrapper: validates `:id` before any query runs. */
export default function CommunityDashboard() {
  const { id } = useParams<{ id: string }>();
  const communityId = Number(id);
  if (!Number.isInteger(communityId) || communityId <= 0) {
    return <ErrorState title="Unknown community" message="This community link is not valid." />;
  }
  return <DashboardView communityId={communityId} />;
}
