import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query';
import {
  fetchLeaderboard,
  fetchRecentActivity,
  type RecentActivityItem,
  fetchLiveActivity,
  fetchMyActivityStats,
  type LeaderboardPage,
  type LeaderboardQuery,
  type LeaderboardType,
  type LiveActivityEvent,
  type MyActivityStats,
} from '../services/communityActivityApi';

/** Query keys for community activity reads. */
export const activityKeys = {
  leaderboard: (communityId: string, type: LeaderboardType, query: LeaderboardQuery) =>
    ['community', communityId, 'leaderboard', type, query] as const,
  myStats: (communityId: string) => ['community', communityId, 'my-stats'] as const,
  live: (communityId: string, limit: number) => ['community', communityId, 'live-activity', limit] as const,
};

/** Paged leaderboard; the previous page stays on screen while the next one loads. */
export function useLeaderboard(
  communityId: string,
  type: LeaderboardType,
  query: LeaderboardQuery,
): UseQueryResult<LeaderboardPage> {
  return useQuery({
    queryKey: activityKeys.leaderboard(communityId, type, query),
    queryFn: () => fetchLeaderboard(communityId, type, query),
    placeholderData: keepPreviousData,
  });
}

/** The caller's own watch-time/message stats and ranks. */
export function useMyActivityStats(communityId: string): UseQueryResult<MyActivityStats> {
  return useQuery({ queryKey: activityKeys.myStats(communityId), queryFn: () => fetchMyActivityStats(communityId) });
}

/** Live-activity backfill; the SSE stream appends to it client-side. Never cached stale. */
export function useLiveActivityBackfill(communityId: string, limit: number): UseQueryResult<LiveActivityEvent[]> {
  return useQuery({
    queryKey: activityKeys.live(communityId, limit),
    queryFn: () => fetchLiveActivity(communityId, limit),
    staleTime: 0,
    gcTime: 0,
  });
}

/** Recent member-facing activity (joins, announcements) for the dashboard. */
export function useRecentActivity(communityId: string, limit = 10): UseQueryResult<RecentActivityItem[]> {
  return useQuery({
    queryKey: ['community', communityId, 'recent-activity', limit] as const,
    queryFn: () => fetchRecentActivity(communityId, limit),
  });
}
