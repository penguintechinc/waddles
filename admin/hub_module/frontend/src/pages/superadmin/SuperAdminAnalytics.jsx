import { useState, useEffect } from 'react';
import { analyticsApi } from '../../services/api';
import PlatformSummaryCards from '../../components/analytics/PlatformSummaryCards';
import ReputationTierChart from '../../components/analytics/ReputationTierChart';
import PlatformGrowthChart from '../../components/analytics/PlatformGrowthChart';
import ActivitySegmentChart from '../../components/analytics/ActivitySegmentChart';
import CommunityHealthTable from '../../components/analytics/CommunityHealthTable';

function SuperAdminAnalytics() {
  const [overview, setOverview] = useState(null);
  const [repData, setRepData] = useState(null);
  const [growthData, setGrowthData] = useState(null);
  const [activityData, setActivityData] = useState(null);
  const [healthData, setHealthData] = useState(null);
  const [growthPeriod, setGrowthPeriod] = useState('90d');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    loadAll();
  }, []);

  useEffect(() => {
    loadGrowth();
  }, [growthPeriod]);

  const loadAll = async () => {
    try {
      setLoading(true);
      setError(null);

      const results = await Promise.allSettled([
        analyticsApi.getPlatformOverview(),
        analyticsApi.getPlatformReputation(),
        analyticsApi.getPlatformGrowth(growthPeriod),
        analyticsApi.getPlatformActivity(),
        analyticsApi.getCommunityHealth(),
      ]);

      const [overviewRes, repRes, growthRes, activityRes, healthRes] = results;
      if (overviewRes.status === 'fulfilled') setOverview(overviewRes.value.data);
      if (repRes.status === 'fulfilled') setRepData(repRes.value.data);
      if (growthRes.status === 'fulfilled') setGrowthData(growthRes.value.data);
      if (activityRes.status === 'fulfilled') setActivityData(activityRes.value.data);
      if (healthRes.status === 'fulfilled') setHealthData(healthRes.value.data);

      // Never fail silently: surface the first rejected platform-analytics call.
      const failed = results.find((r) => r.status === 'rejected');
      if (failed && failed.status === 'rejected') {
        setError(failed.reason?.response?.data?.error?.message || 'Failed to load analytics');
      }
    } finally {
      setLoading(false);
    }
  };

  const loadGrowth = async () => {
    if (loading) return;
    try {
      const res = await analyticsApi.getPlatformGrowth(growthPeriod);
      setGrowthData(res.data);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load growth analytics');
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300">
        {error}
      </div>
    );
  }

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
        buckets: repData.buckets || repData.tiers || (repData.reputationTiers || []).map((t) => ({
          label: t.label,
          count: t.count,
        })),
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

  return (
    <div>
      <h1 className="text-2xl font-bold gradient-text mb-6">Platform Analytics</h1>

      <PlatformSummaryCards data={summaryData} />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        <ReputationTierChart data={repNormalized} />
        <ActivitySegmentChart data={activityNormalized} />
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

      <PlatformGrowthChart
        data={growthNormalized}
        period={growthPeriod}
        onPeriodChange={setGrowthPeriod}
      />

      <CommunityHealthTable data={communities} />
    </div>
  );
}

export default SuperAdminAnalytics;
