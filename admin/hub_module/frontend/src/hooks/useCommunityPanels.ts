import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query';
import { fetchCommunityMembers, type MemberPage, type MemberQuery } from '../services/communityMembersApi';
import {
  fetchLiveStreams,
  fetchPublishedAnnouncements,
  type Announcement,
  type LiveStream,
} from '../services/communityOverviewApi';
import { fetchReputationLeaderboard, type ReputationEntry } from '../services/reputationApi';

/** Query keys for the community panels (members, streams, announcements, reputation). */
export const panelKeys = {
  members: (communityId: number, query: MemberQuery) => ['community', communityId, 'members', query] as const,
  streams: (communityId: number) => ['community', communityId, 'streams'] as const,
  announcements: (communityId: number, limit: number) => ['community', communityId, 'announcements', limit] as const,
  reputation: (communityId: number, limit: number) => ['community', communityId, 'reputation-board', limit] as const,
};

/** Paged roster; keeps the previous page on screen while the next loads. `enabled` gates non-admins. */
export function useCommunityMembers(
  communityId: number,
  query: MemberQuery,
  enabled: boolean,
): UseQueryResult<MemberPage> {
  return useQuery({
    queryKey: panelKeys.members(communityId, query),
    queryFn: () => fetchCommunityMembers(communityId, query),
    placeholderData: keepPreviousData,
    enabled,
  });
}

/** Live streams for a community, refreshed on demand through `refetch`. */
export function useLiveStreams(communityId: number): UseQueryResult<LiveStream[]> {
  return useQuery({ queryKey: panelKeys.streams(communityId), queryFn: () => fetchLiveStreams(communityId) });
}

/** Published announcements; the server only allows community admins, so callers gate with `enabled`. */
export function useAnnouncements(communityId: number, limit: number, enabled: boolean): UseQueryResult<Announcement[]> {
  return useQuery({
    queryKey: panelKeys.announcements(communityId, limit),
    queryFn: () => fetchPublishedAnnouncements(communityId, limit),
    enabled,
    retry: false,
  });
}

/** Top reputation scorers (display names only). */
export function useReputationLeaderboard(communityId: number, limit: number): UseQueryResult<ReputationEntry[]> {
  return useQuery({
    queryKey: panelKeys.reputation(communityId, limit),
    queryFn: () => fetchReputationLeaderboard(communityId, limit),
  });
}
