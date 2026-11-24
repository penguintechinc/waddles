import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { TIER } from '../../services/communityTierConstants';
import {
  ChartBarIcon,
  UserGroupIcon,
  ChatBubbleLeftIcon,
  ClockIcon,
  SparklesIcon,
} from '@heroicons/react/24/outline';

function AdminAnalytics() {
  const { communityId } = useParams();
  const { communityTier } = useAuth();
  const [analytics, setAnalytics] = useState(null);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState(null);
  // UX hint only: the server's tier gates are authoritative (an ungated client still gets 402/403).
  const tier = communityTier(communityId);
  const isPremium = tier >= TIER.premium;
  const [pollInterval, setPollInterval] = useState(null);

  useEffect(() => {
    fetchAnalytics();
    // Set up polling every 30 seconds
    const interval = setInterval(fetchAnalytics, 30000);
    setPollInterval(interval);

    return () => {
      if (interval) clearInterval(interval);
    };
  }, [communityId, isPremium]);

  async function fetchAnalytics() {
    setLoading(true);
    try {
      const basicResponse = await adminApi.getAnalyticsBasic(communityId);
      let fullAnalytics = basicResponse.data.data || basicResponse.data || {};

      // Fetch additional data if premium
      if (isPremium) {
        try {
          const healthResponse = await adminApi.getAnalyticsHealthScore(communityId);
          fullAnalytics.healthScore = healthResponse.data.data || healthResponse.data.healthScore;
        } catch (err) {
          console.warn('Failed to fetch health score:', err);
        }

        try {
          const badActorsResponse = await adminApi.getAnalyticsBadActors(communityId);
          fullAnalytics.badActors = badActorsResponse.data.data || [];
        } catch (err) {
          console.warn('Failed to fetch bad actors:', err);
        }

        try {
          const retentionResponse = await adminApi.getAnalyticsRetention(communityId);
          fullAnalytics.retention = retentionResponse.data.data || retentionResponse.data.retention;
        } catch (err) {
          console.warn('Failed to fetch retention:', err);
        }
      }

      setAnalytics(fullAnalytics);
    } catch (err) {
      console.error('Failed to fetch analytics:', err);
      setMessage({ type: 'error', text: 'Failed to load analytics data' });
    } finally {
      setLoading(false);
    }
  }

  async function handleRefresh() {
    await fetchAnalytics();
    setMessage({ type: 'success', text: 'Analytics refreshed' });
    setTimeout(() => setMessage(null), 3000);
  }

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  if (!analytics) {
    return (
      <div className="text-center py-12 text-red-400">
        Failed to load analytics data
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Analytics Dashboard</h1>
          <p className="text-navy-400 mt-1">Community engagement and activity metrics</p>
        </div>
        <button
          onClick={handleRefresh}
          className="btn btn-primary"
        >
          Refresh Data
        </button>
      </div>

      {message && (
        <div className={`mb-6 p-4 rounded-lg border ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border-red-500/30'
        }`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">×</button>
        </div>
      )}

      {/* Basic Metrics Grid */}
      <div className="grid md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        {/* Total Chatters */}
        <div className="card p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-medium text-navy-300">Total Chatters</h3>
            <UserGroupIcon className="w-5 h-5 text-sky-400" />
          </div>
          <div className="text-3xl font-bold text-sky-100">
            {analytics.totalChatters || 0}
          </div>
          <p className="text-xs text-navy-400 mt-2">
            Unique users this period
          </p>
        </div>

        {/* Stream Time */}
        <div className="card p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-medium text-navy-300">Stream Time</h3>
            <ClockIcon className="w-5 h-5 text-emerald-400" />
          </div>
          <div className="text-3xl font-bold text-sky-100">
            {analytics.totalStreamTime ? `${Math.floor(analytics.totalStreamTime / 60)}h` : '0h'}
          </div>
          <p className="text-xs text-navy-400 mt-2">
            Total hours streamed
          </p>
        </div>

        {/* Messages */}
        <div className="card p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-medium text-navy-300">Total Messages</h3>
            <ChatBubbleLeftIcon className="w-5 h-5 text-purple-400" />
          </div>
          <div className="text-3xl font-bold text-sky-100">
            {analytics.totalMessages?.toLocaleString() || 0}
          </div>
          <p className="text-xs text-navy-400 mt-2">
            Messages sent
          </p>
        </div>

        {/* Avg Messages Per User */}
        <div className="card p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-medium text-navy-300">Avg Messages</h3>
            <ChartBarIcon className="w-5 h-5 text-gold-400" />
          </div>
          <div className="text-3xl font-bold text-sky-100">
            {analytics.avgMessagesPerUser?.toFixed(1) || 0}
          </div>
          <p className="text-xs text-navy-400 mt-2">
            Per user average
          </p>
        </div>
      </div>

      {/* Activity Chart */}
      {analytics.activityChart && (
        <div className="card p-6 mb-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Activity Chart</h2>
          <div className="bg-navy-800 rounded-lg p-4 text-center text-navy-400">
            <p className="text-sm">Activity chart visualization coming soon</p>
            <p className="text-xs mt-2">
              {analytics.activityChart.length} data points available
            </p>
          </div>
        </div>
      )}

      {/* Premium Features */}
      {isPremium ? (
        <>
          {/* Health Score */}
          {analytics.healthScore && (
            <div className="card p-6 mb-6">
              <div className="flex items-center gap-2 mb-4">
                <h2 className="text-lg font-semibold text-sky-100">Community Health Score</h2>
                <span className="badge badge-gold text-xs">Premium</span>
              </div>
              <div className="bg-gradient-to-r from-emerald-500/20 to-sky-500/20 rounded-lg p-6 border border-emerald-500/30">
                <div className="text-4xl font-bold text-emerald-400 mb-2">
                  {analytics.healthScore.score || 0}/100
                </div>
                <p className="text-sm text-navy-300 mb-4">
                  {analytics.healthScore.status || 'Calculating...'}
                </p>
                <div className="w-full bg-navy-800 rounded-full h-2">
                  <div
                    className="bg-gradient-to-r from-emerald-500 to-sky-500 h-2 rounded-full transition-all"
                    style={{ width: `${analytics.healthScore.score || 0}%` }}
                  />
                </div>
              </div>
            </div>
          )}

          {/* Bad Actors */}
          {analytics.badActors && analytics.badActors.length > 0 && (
            <div className="card p-6 mb-6">
              <div className="flex items-center gap-2 mb-4">
                <h2 className="text-lg font-semibold text-sky-100">Detected Bad Actors</h2>
                <span className="badge badge-gold text-xs">Premium</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-navy-400 border-b border-navy-700">
                    <tr>
                      <th className="text-left py-2 px-4">User</th>
                      <th className="text-left py-2 px-4">Risk Level</th>
                      <th className="text-left py-2 px-4">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {analytics.badActors.slice(0, 5).map((actor, idx) => (
                      <tr key={idx} className="border-b border-navy-800 hover:bg-navy-800/50">
                        <td className="py-3 px-4 text-sky-100">{actor.username || actor.userId}</td>
                        <td className="py-3 px-4">
                          <span className={`px-2 py-1 rounded text-xs font-medium ${
                            actor.riskLevel === 'high'
                              ? 'bg-red-500/20 text-red-300'
                              : actor.riskLevel === 'medium'
                              ? 'bg-yellow-500/20 text-yellow-300'
                              : 'bg-emerald-500/20 text-emerald-300'
                          }`}>
                            {actor.riskLevel}
                          </span>
                        </td>
                        <td className="py-3 px-4 text-navy-300 text-xs">
                          {actor.reason || 'Suspicious behavior'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {analytics.badActors.length > 5 && (
                <p className="text-xs text-navy-400 mt-4">
                  And {analytics.badActors.length - 5} more...
                </p>
              )}
            </div>
          )}

          {/* Retention */}
          {analytics.retention && (
            <div className="card p-6">
              <div className="flex items-center gap-2 mb-4">
                <h2 className="text-lg font-semibold text-sky-100">Retention Metrics</h2>
                <span className="badge badge-gold text-xs">Premium</span>
              </div>
              <div className="grid md:grid-cols-3 gap-4">
                <div className="bg-navy-800 rounded-lg p-4">
                  <div className="text-sm text-navy-400 mb-2">Day 1 Retention</div>
                  <div className="text-2xl font-bold text-sky-100">
                    {analytics.retention.day1 || 0}%
                  </div>
                </div>
                <div className="bg-navy-800 rounded-lg p-4">
                  <div className="text-sm text-navy-400 mb-2">Day 7 Retention</div>
                  <div className="text-2xl font-bold text-sky-100">
                    {analytics.retention.day7 || 0}%
                  </div>
                </div>
                <div className="bg-navy-800 rounded-lg p-4">
                  <div className="text-sm text-navy-400 mb-2">Day 30 Retention</div>
                  <div className="text-2xl font-bold text-sky-100">
                    {analytics.retention.day30 || 0}%
                  </div>
                </div>
              </div>
            </div>
          )}
        </>
      ) : (
        <div className="card p-6 bg-gold-500/10 border border-gold-500/30">
          <div className="flex items-center gap-3">
            <SparklesIcon className="w-6 h-6 text-gold-400" />
            <div>
              <div className="font-medium text-gold-300">Advanced analytics requires Premium</div>
              <div className="text-sm text-gold-200">
                Health scores, bad actor detection and retention analytics require Premium (this community is on Free). Ask your tenant administrator to allocate a Premium slot
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="mt-4 p-4 bg-navy-800 rounded-lg text-sm text-navy-400">
        <p>Data refreshes automatically every 30 seconds. Last updated: {new Date().toLocaleTimeString()}</p>
      </div>
    </div>
  );
}

export default AdminAnalytics;
