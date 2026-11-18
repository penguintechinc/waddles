import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { analyticsApi } from '../../services/api';
import { AnalyticsEmpty, AnalyticsFailureState } from '../../components/analytics/AnalyticsStates';
import { allZero } from '../../components/analytics/usePlatformAnalytics';
import { classifyAnalyticsError } from '../../components/analytics/classifyAnalyticsError';
import {
  ChatBubbleLeftIcon,
  ClockIcon,
  StarIcon,
  UserCircleIcon,
  ArrowLeftIcon,
} from '@heroicons/react/24/outline';

/**
 * Per-member analytics for a community admin. The member lookup 404 ("Member not found") is a real
 * not-found; any other 404 (no analytics rows) is "No analytics for this member yet". An undeployed
 * analytics backend is a neutral note, never a red banner.
 */
function AdminMemberAnalytics() {
  const { communityId, userId } = useParams();
  const [state, setState] = useState({ loading: true, stats: null, reputation: null, statsFailure: null, repFailure: null });

  const loadData = async () => {
    setState({ loading: true, stats: null, reputation: null, statsFailure: null, repFailure: null });
    const [statsRes, repRes] = await Promise.allSettled([
      analyticsApi.getMemberStats(communityId, userId),
      analyticsApi.getMemberReputation(communityId, userId),
    ]);
    setState({
      loading: false,
      stats: statsRes.status === 'fulfilled' ? statsRes.value.data ?? null : null,
      reputation: repRes.status === 'fulfilled' ? repRes.value.data ?? null : null,
      statsFailure: statsRes.status === 'rejected' ? classifyAnalyticsError(statsRes.reason, { notFound: true }) : null,
      repFailure: repRes.status === 'rejected' ? classifyAnalyticsError(repRes.reason, { notFound: true }) : null,
    });
  };

  useEffect(() => {
    loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [communityId, userId]);

  const { loading, stats, reputation, statsFailure, repFailure } = state;

  const backLink = (
    <div className="mb-6">
      <Link
        to={`/admin/${communityId}/members`}
        className="flex items-center gap-2 text-sm text-navy-400 hover:text-sky-300 transition-colors"
      >
        <ArrowLeftIcon className="w-4 h-4" />
        Back to Members
      </Link>
    </div>
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-sky-400"></div>
      </div>
    );
  }

  if (statsFailure) {
    const unknownMember = statsFailure.kind === 'not-found' && /(member|community) not found/i.test(statsFailure.message);
    return (
      <div>
        {backLink}
        <div className="card p-6" data-testid="widget-member-analytics">
          {unknownMember ? (
            <AnalyticsEmpty title={statsFailure.message} description="Check that the member belongs to this community." testId="analytics-not-found" />
          ) : (
            <AnalyticsFailureState failure={statsFailure} onRetry={loadData} emptyTitle="No analytics for this member yet" />
          )}
        </div>
      </div>
    );
  }

  const username = stats?.username || stats?.user?.username || `User #${userId}`;
  const messages = stats?.total_messages ?? stats?.totalMessages ?? 0;
  const watchMinutes = stats?.total_watch_minutes ?? stats?.totalWatchMinutes ?? 0;
  const role = stats?.role ?? '—';
  const firstSeen = stats?.first_seen || stats?.firstSeen;
  const lastSeen = stats?.last_seen || stats?.lastSeen;
  const reputationScore = reputation?.reputation ?? reputation?.score ?? '—';
  const timeline = stats?.activity_30d || stats?.activity || [];
  const communityRepList = reputation?.communities || [];
  const statsEmpty = allZero([messages, watchMinutes]) && timeline.length === 0 && !firstSeen && !lastSeen;

  return (
    <div>
      {backLink}

      {/* Header */}
      <div className="flex items-center gap-4 mb-8">
        <div className="w-12 h-12 rounded-full bg-navy-700 flex items-center justify-center border border-navy-600">
          <UserCircleIcon className="w-8 h-8 text-sky-400" />
        </div>
        <div>
          <h1 className="text-2xl font-bold text-sky-100">{username}</h1>
          <div className="flex items-center gap-3 mt-1">
            <span className="px-2 py-1 bg-navy-700 text-navy-300 rounded text-xs border border-navy-600">
              {role}
            </span>
            {firstSeen && (
              <span className="text-xs text-navy-500">
                Member since {new Date(firstSeen).toLocaleDateString()}
              </span>
            )}
          </div>
        </div>
      </div>

      {statsEmpty ? (
        <div className="card p-6 mb-8" data-testid="widget-member-analytics">
          <AnalyticsEmpty title="No analytics for this member yet" description="Activity will appear here once they take part." />
        </div>
      ) : (
      <>
      {/* Stat Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        <div className="card p-6 border-l-4 border-l-sky-400">
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm text-navy-400">Messages</div>
            <ChatBubbleLeftIcon className="w-5 h-5 text-navy-500" />
          </div>
          <div className="text-3xl font-bold text-sky-100">
            {messages.toLocaleString()}
          </div>
        </div>

        <div className="card p-6 border-l-4 border-l-emerald-400">
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm text-navy-400">Watch Hours</div>
            <ClockIcon className="w-5 h-5 text-navy-500" />
          </div>
          <div className="text-3xl font-bold text-emerald-400">
            {Math.floor(watchMinutes / 60)}h
          </div>
        </div>

        <div className="card p-6 border-l-4 border-l-purple-400">
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm text-navy-400">Reputation</div>
            <StarIcon className="w-5 h-5 text-navy-500" />
          </div>
          <div className="text-3xl font-bold text-purple-400">{reputationScore}</div>
        </div>

        <div className="card p-6 border-l-4 border-l-gold-400">
          <div className="text-sm text-navy-400 mb-2">Last Seen</div>
          <div className="text-lg font-semibold text-gold-400">
            {lastSeen ? new Date(lastSeen).toLocaleDateString() : '—'}
          </div>
        </div>
      </div>

      {/* 30-day Activity Timeline */}
      {timeline.length > 0 && (
        <div className="card p-6 mb-8">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">30-Day Activity</h2>
          <div className="flex items-end gap-1 h-24">
            {(() => {
              const maxVal = Math.max(...timeline.map((t) => t.count || t.messages || 0), 1);
              return timeline.map((t, i) => (
                <div
                  key={i}
                  className="flex-1 bg-sky-500/80 hover:bg-sky-400 rounded-t transition-all relative group"
                  style={{
                    height: `${(((t.count || t.messages || 0) / maxVal) * 100)}%`,
                    minHeight: '4px',
                  }}
                >
                  <div className="absolute bottom-full mb-1 left-1/2 -translate-x-1/2 hidden group-hover:block bg-navy-700 text-sky-200 text-xs px-2 py-1 rounded whitespace-nowrap z-10">
                    {t.date || t.label}: {t.count || t.messages || 0}
                  </div>
                </div>
              ));
            })()}
          </div>
        </div>
      )}

      </>
      )}

      {/* Reputation across communities */}
      {repFailure && repFailure.kind !== 'not-found' && (
        <div className="card p-6" data-testid="widget-member-reputation">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Reputation History</h2>
          <AnalyticsFailureState failure={repFailure} onRetry={loadData} />
        </div>
      )}
      {communityRepList.length > 0 && (
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Reputation History</h2>
          <div className="space-y-2">
            {communityRepList.map((c) => (
              <div key={c.id || c.community_id} className="flex items-center justify-between py-2 border-b border-navy-800 last:border-0">
                <span className="text-sm text-sky-200">
                  {c.name || c.community_name || `Community #${c.id || c.community_id}`}
                </span>
                <span className="text-sm font-semibold text-purple-400">
                  {c.reputation ?? c.score ?? '—'}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminMemberAnalytics;
