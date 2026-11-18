import PlatformSummaryCards from '../../components/analytics/PlatformSummaryCards';
import ReputationTierChart from '../../components/analytics/ReputationTierChart';
import PlatformGrowthChart from '../../components/analytics/PlatformGrowthChart';
import ActivitySegmentChart from '../../components/analytics/ActivitySegmentChart';
import CommunityHealthTable from '../../components/analytics/CommunityHealthTable';
import { AnalyticsPanel } from '../../components/analytics/AnalyticsStates';
import { allZero, usePlatformAnalytics } from '../../components/analytics/usePlatformAnalytics';

/**
 * Super-admin platform analytics. Each widget loads and fails independently (no page-level
 * error banner); empty data renders "No data yet" and an undeployed backend renders a neutral note.
 */
function SuperAdminAnalytics() {
  const { widgets, period: growthPeriod, changePeriod, retry } = usePlatformAnalytics('90d');
  const { overview: ov, reputation: rep, growth: gr, activity: act, health: hl } = widgets;
  const overview = ov.data;
  const repData = rep.data;
  const growthData = gr.data;
  const activityData = act.data;
  const healthData = hl.data;

  // Normalize overview — handle both new analyticsApi shape and legacy superAdminApi shape
  const summaryData = overview
    ? {
        total_users: overview.total_users ?? overview.totalUsers ?? overview.summary?.totalUsers,
        active_users_30d: overview.active_users_30d ?? overview.activeUsers30d ?? overview.summary?.activeUsers30d,
        total_communities: overview.total_communities ?? overview.totalCommunities ?? overview.summary?.totalCommunities,
        avg_reputation: overview.avg_reputation ?? overview.avgReputation ?? overview.summary?.avgPlatformReputation,
      }
    : null;

  // Normalize reputation data — new shape uses buckets, legacy uses reputationTiers
  const repNormalized = repData
    ? {
        buckets: repData.buckets ||
          repData.tiers ||
          (repData.reputationTiers || repData.histogram?.map((h) => ({ label: h.range, count: h.count })) || []).map(
            (t) => ({ label: t.label, count: t.count }),
          ),
        stats: repData.stats || repData.statistics || {},
      }
    : null;

  // Normalize activity — new shape uses active_24h etc., legacy uses segments array
  const activityNormalized = (() => {
    if (!activityData) return null;
    if (activityData.active_24h !== undefined) return activityData;
    if (activityData.segments) {
      const seg = activityData.segments;
      const find = (key) => seg.find((s) => s.key === key)?.count || 0;
      return {
        active_24h: find('active_24h'),
        active_7d: find('active_7d'),
        active_30d: find('active_30d'),
        active_90d: find('active_90d'),
        inactive: find('inactive'),
      };
    }
    return activityData;
  })();

  // Normalize growth — new shape uses buckets with new_users/new_communities
  const growthNormalized = (() => {
    if (!growthData) return null;
    if (growthData.buckets) return growthData;
    if (growthData.users) {
      return {
        buckets: growthData.users.map((u, i) => ({
          label: u.label || u.date || String(i),
          new_users: u.count || 0,
          new_communities: growthData.communities?.[i]?.count || 0,
        })),
      };
    }
    return { buckets: [] };
  })();

  // Community health
  const communities = healthData
    ? (healthData.communities || (Array.isArray(healthData) ? healthData : []))
    : [];

  // Legacy-only data from overview (platform breakdown, community types)
  const platformBreakdown = overview?.platformBreakdown || [];
  const communityTypes = overview?.communityTypes || [];
  const maxPlatformCount = Math.max(...platformBreakdown.map((p) => p.count), 1);
  const maxTypeCount = Math.max(...communityTypes.map((t) => t.count), 1);

  const summaryEmpty = !summaryData || allZero(Object.values(summaryData));
  const repEmpty = !repNormalized || (repNormalized.buckets.length === 0 && allZero([repNormalized.stats.total]));
  const activityEmpty = !activityNormalized || allZero(Object.values(activityNormalized));

  return (
    <div>
      <h1 className="text-2xl font-bold gradient-text mb-6">Platform Analytics</h1>

      <AnalyticsPanel
        loading={ov.loading}
        failure={ov.failure}
        empty={summaryEmpty}
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

      {/* Platform Breakdown (legacy superadmin-specific data) */}
      {platformBreakdown.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
          <div className="card p-6">
            <h2 className="text-lg font-semibold mb-4 text-sky-100">Platform Breakdown</h2>
            <div className="space-y-3">
              {platformBreakdown.map((p) => (
                <div key={p.platform}>
                  <div className="flex justify-between text-sm mb-1">
                    <span className="text-sky-200 capitalize">{p.platform || 'Unknown'}</span>
                    <span className="text-navy-400">{p.count}</span>
                  </div>
                  <div className="w-full bg-navy-800 rounded-full h-3">
                    <div
                      className="bg-sky-500 h-3 rounded-full transition-all"
                      style={{ width: `${(p.count / maxPlatformCount) * 100}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="card p-6">
            <h2 className="text-lg font-semibold mb-4 text-sky-100">Community Types</h2>
            <div className="space-y-3">
              {communityTypes.map((t) => (
                <div key={t.type}>
                  <div className="flex justify-between text-sm mb-1">
                    <span className="text-sky-200 capitalize">{t.type.replace(/_/g, ' ')}</span>
                    <span className="text-navy-400">{t.count}</span>
                  </div>
                  <div className="w-full bg-navy-800 rounded-full h-3">
                    <div
                      className="bg-gold-500 h-3 rounded-full transition-all"
                      style={{ width: `${(t.count / maxTypeCount) * 100}%` }}
                    />
                  </div>
                </div>
              ))}
              {communityTypes.length === 0 && (
                <p className="text-navy-500 text-sm">No communities yet.</p>
              )}
            </div>
          </div>
        </div>
      )}

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

export default SuperAdminAnalytics;
