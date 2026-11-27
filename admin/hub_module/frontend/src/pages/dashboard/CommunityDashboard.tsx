import { Link, useParams } from 'react-router-dom';
import { AnnouncementsPanel } from '../../components/dashboard/AnnouncementsPanel';
import { CommunityHeader } from '../../components/dashboard/CommunityHeader';
import CommunityReputationPanel from '../../components/dashboard/CommunityReputationPanel';
import QuickActionsWidget from '../../components/dashboard/QuickActionsWidget';
import { LeaderboardCard } from '../../components/leaderboard/LeaderboardCard';
import { StreamsPanel } from '../../components/dashboard/StreamsPanel';
import { Card, CardHeader, EmptyState, ErrorState, QueryBoundary } from '../../components/ui';
import { useRecentActivity } from '../../hooks/useCommunityActivity';
import { useCommunityScopes } from '../../hooks/useCommunityScopes';
import { useLiveStreams } from '../../hooks/useCommunityPanels';
import { COMMUNITY_SCOPES, hasAdminSurface } from '../../lib/communityScopes';
import { RECENT_ACTIVITY_KIND, type RecentActivityItem } from '../../services/communityActivityApi';
import { useMyCommunities, usePublicCommunity } from '../../hooks/useMyCommunities';
import type { MyCommunity } from '../../services/myCommunitiesApi';
import { parseCommunityId } from '../../lib/communityId';

function StatTile({ value, label, tone }: { value: string; label: string; tone: string }) {
  return (
    <div className={`card border-l-4 p-4 ${tone}`}>
      <div className="text-2xl font-bold text-fg">{value}</div>
      <div className="text-sm text-fg-secondary">{label}</div>
    </div>
  );
}

/** One-line description of a feed entry, by kind code. */
function describeActivity(item: RecentActivityItem): string {
  const who = item.actorName ?? 'Someone';
  if (item.kind === RECENT_ACTIVITY_KIND.memberJoined) return `${who} joined the community`;
  if (item.kind === RECENT_ACTIVITY_KIND.announcement) return item.title ? `Announcement: ${item.title}` : 'New announcement';
  return item.title ?? who;
}

/** Everything on the dashboard that needs the caller to be a member. */
function DashboardBody({ membership }: { membership: MyCommunity }) {
  const communityId = membership.id;
  const { has, scopes } = useCommunityScopes(communityId);
  const details = usePublicCommunity(communityId);
  const streams = useLiveStreams(communityId);
  const activity = useRecentActivity(communityId);
  const canConnect = has(COMMUNITY_SCOPES.providerConnections);
  const canAdminister = has(COMMUNITY_SCOPES.admin) || hasAdminSurface([...scopes]);
  const live = streams.data;
  const connect = canConnect ? { connectTo: `/admin/${communityId}/connections` } : {};

  return (
    <div>
      <CommunityHeader membership={membership} details={details.data} canAdminister={canAdminister} />

      <StreamsPanel communityId={communityId} query={streams} canManage={has(COMMUNITY_SCOPES.streamConfig) || canConnect} />

      <AnnouncementsPanel communityId={communityId} canManage={has(COMMUNITY_SCOPES.announcementsRead)} />

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <div className="grid gap-4 sm:grid-cols-3">
            <StatTile
              value={details.data ? details.data.memberCount.toLocaleString() : '—'}
              label="Members"
              tone="border-l-interactive"
            />
            <StatTile value={membership.role} label="Your role" tone="border-l-accent" />
            <StatTile value={live ? String(live.length) : '—'} label="Live Now" tone="border-l-success" />
          </div>
          <Card as="section" aria-label="Recent activity">
            <CardHeader title="Recent Activity" />
            <QueryBoundary
              query={activity}
              loadingLabel="Loading recent activity"
              errorTitle="We could not load recent activity"
              isEmpty={(items: RecentActivityItem[]) => items.length === 0}
              empty={<p className="py-8 text-center text-fg-secondary">Nothing has happened here yet.</p>}
            >
              {(items) => (
                <ul className="divide-y divide-line" aria-label="Recent activity items">
                  {items.map((item) => (
                    <li key={`${item.kind}-${item.at}-${item.actorId ?? ''}-${item.title ?? ''}`} className="py-3 text-sm text-fg" data-testid="activity-item">
                      {describeActivity(item)}
                      <span className="ml-2 text-fg-secondary">{new Date(item.at).toLocaleDateString()}</span>
                    </li>
                  ))}
                </ul>
              )}
            </QueryBoundary>
          </Card>
        </div>

        <div className="space-y-6">
          <CommunityReputationPanel communityId={communityId} />
          <LeaderboardCard communityId={communityId} type="watch-time" title="Top Viewers" limit={5} {...connect} />
          <LeaderboardCard communityId={communityId} type="messages" title="Top Chatters" limit={5} {...connect} />
          <QuickActionsWidget id={String(communityId)} />
        </div>
      </div>
    </div>
  );
}

/** Member dashboard for one community, composed only from routes hub-api serves. */
function DashboardView({ communityId }: { communityId: string }) {
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
  const communityId = parseCommunityId(id);
  if (communityId === null) {
    return <ErrorState title="Unknown community" message="This community link is not valid." />;
  }
  return <DashboardView communityId={communityId} />;
}
