import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query';
import {
  fetchLeaderboard,
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
  leaderboard: (communityId: number, type: LeaderboardType, query: LeaderboardQuery) =>
    ['community', communityId, 'leaderboard', type, query] as const,
  myStats: (communityId: number) => ['community', communityId, 'my-stats'] as const,
  live: (communityId: number, limit: number) => ['community', communityId, 'live-activity', limit] as const,
};

/** Paged leaderboard; the previous page stays on screen while the next one loads. */
export function useLeaderboard(
  communityId: number,
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
export function useMyActivityStats(communityId: number): UseQueryResult<MyActivityStats> {
  return useQuery({ queryKey: activityKeys.myStats(communityId), queryFn: () => fetchMyActivityStats(communityId) });
}

/** Live-activity backfill; the SSE stream appends to it client-side. Never cached stale. */
export function useLiveActivityBackfill(communityId: number, limit: number): UseQueryResult<LiveActivityEvent[]> {
  return useQuery({
    queryKey: activityKeys.live(communityId, limit),
    queryFn: () => fetchLiveActivity(communityId, limit),
    staleTime: 0,
    gcTime: 0,
  });
}
