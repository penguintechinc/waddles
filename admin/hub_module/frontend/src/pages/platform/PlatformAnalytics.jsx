import PlatformSummaryCards from '../../components/analytics/PlatformSummaryCards';
import ReputationTierChart from '../../components/analytics/ReputationTierChart';
import PlatformGrowthChart from '../../components/analytics/PlatformGrowthChart';
import ActivitySegmentChart from '../../components/analytics/ActivitySegmentChart';
import CommunityHealthTable from '../../components/analytics/CommunityHealthTable';
import { AnalyticsPanel } from '../../components/analytics/AnalyticsStates';
import { allZero, usePlatformAnalytics } from '../../components/analytics/usePlatformAnalytics';

/**
 * Platform-wide analytics. Each widget loads and fails independently: one failed call (or an
 * undeployed analytics backend) never blanks the page; empty data renders "No data yet".
 */
function PlatformAnalytics() {
  const { widgets, period: growthPeriod, changePeriod, retry } = usePlatformAnalytics('30d');
  const { overview: ov, reputation: rep, growth: gr, activity: act, health: hl } = widgets;
  const overview = ov.data;
  const repData = rep.data;
  const growthData = gr.data;
  const activityData = act.data;
  const healthData = hl.data;

  // Normalize overview data shape
  const summaryData = overview
    ? {
        total_users: overview.total_users ?? overview.totalUsers ?? overview.summary?.totalUsers,
        active_users_30d: overview.active_users_30d ?? overview.activeUsers30d ?? overview.summary?.activeUsers30d,
        total_communities: overview.total_communities ?? overview.totalCommunities ?? overview.summary?.totalCommunities,
        avg_reputation: overview.avg_reputation ?? overview.avgReputation ?? overview.summary?.avgPlatformReputation,
      }
    : null;

  // Normalize reputation data
  const repNormalized = repData
    ? {
        buckets:
          repData.buckets ||
          repData.tiers ||
          repData.reputationTiers ||
          (repData.histogram || []).map((h) => ({ label: h.range, count: h.count })),
        stats: repData.stats || repData.statistics || {},
      }
    : null;

  // Normalize activity data
  const activityNormalized = activityData
    ? {
        active_24h: activityData.active_24h ?? activityData.active24h,
        active_7d: activityData.active_7d ?? activityData.active7d,
        active_30d: activityData.active_30d ?? activityData.active30d,
        active_90d: activityData.active_90d ?? activityData.active90d,
        inactive: activityData.inactive,
      }
    : null;

  // Normalize growth data
  const growthNormalized = growthData
    ? {
        buckets: growthData.buckets || growthData.users?.map((u, i) => ({
          label: u.label || u.date || String(i),
          new_users: u.count || u.new_users || 0,
          new_communities: growthData.communities?.[i]?.count || 0,
        })) || [],
      }
    : null;

  const communities = healthData?.communities || (Array.isArray(healthData) ? healthData : []);

  const summaryEmpty = !summaryData || allZero(Object.values(summaryData));
  const repEmpty = !repNormalized || (repNormalized.buckets.length === 0 && allZero([repNormalized.stats.total]));
  const activityEmpty = !activityNormalized || allZero(Object.values(activityNormalized));

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold gradient-text">Platform Analytics</h1>
        <p className="text-navy-400 mt-1">Platform-wide engagement and growth metrics</p>
      </div>

      <AnalyticsPanel
        loading={ov.loading}
        failure={ov.failure}
        empty={summaryEmpty}
        emptyTitle="No data yet"
        onRetry={() => retry('overview')}
        testId="widget-overview"
        className="mb-8"
      >
        <PlatformSummaryCards data={summaryData} />
      </AnalyticsPanel>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        <AnalyticsPanel
          title="Reputation Distribution"
          loading={rep.loading}
          failure={rep.failure}
          empty={repEmpty}
          onRetry={() => retry('reputation')}
          testId="widget-reputation"
        >
          <ReputationTierChart data={repNormalized} />
        </AnalyticsPanel>
        <AnalyticsPanel
          title="Activity Breakdown"
          loading={act.loading}
          failure={act.failure}
          empty={activityEmpty}
          emptyTitle="No events yet"
          onRetry={() => retry('activity')}
          testId="widget-activity"
        >
          <ActivitySegmentChart data={activityNormalized} />
        </AnalyticsPanel>
      </div>

      <AnalyticsPanel
        title="Platform Growth"
        loading={gr.loading}
        failure={gr.failure}
        empty={false}
        onRetry={() => retry('growth')}
        testId="widget-growth"
        className="mb-8"
      >
        <PlatformGrowthChart data={growthNormalized} period={growthPeriod} onPeriodChange={changePeriod} />
      </AnalyticsPanel>

      <AnalyticsPanel
        title="Community Health"
        loading={hl.loading}
        failure={hl.failure}
        empty={communities.length === 0}
        onRetry={() => retry('health')}
        testId="widget-health"
      >
        <CommunityHealthTable data={communities} />
      </AnalyticsPanel>
    </div>
  );
}

export default PlatformAnalytics;
