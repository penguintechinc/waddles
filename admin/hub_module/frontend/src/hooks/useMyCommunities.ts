import { useQueries, useQuery, type UseQueryResult } from '@tanstack/react-query';
import {
  fetchMyCommunities,
  fetchPublicCommunity,
  searchPublicCommunities,
  type MyCommunity,
  type PublicCommunity,
} from '../services/myCommunitiesApi';

/** Query keys for community reads, as const tuples so invalidation stays exact. */
export const communityKeys = {
  mine: ['communities', 'mine'] as const,
  detail: (communityId: number) => ['communities', 'detail', communityId] as const,
  search: (term: string) => ['communities', 'search', term] as const,
};

const FIVE_MINUTES = 5 * 60 * 1000;

/** The signed-in user's communities (`/auth/me`). Errors surface; nothing is swallowed. */
export function useMyCommunities(): UseQueryResult<MyCommunity[]> {
  return useQuery({ queryKey: communityKeys.mine, queryFn: fetchMyCommunities });
}

/** One community's public details. */
export function usePublicCommunity(communityId: number): UseQueryResult<PublicCommunity> {
  return useQuery({
    queryKey: communityKeys.detail(communityId),
    queryFn: () => fetchPublicCommunity(communityId),
    enabled: Number.isInteger(communityId) && communityId > 0,
  });
}

/**
 * Best-effort public details (logo, description, member count) for the user's own
 * communities. These only decorate cards that `/auth/me` already made renderable, so
 * a community whose public record is unavailable (for example a private one) simply
 * shows without them rather than failing the page.
 */
export function usePublicCommunityDetails(communityIds: readonly number[]): ReadonlyMap<number, PublicCommunity> {
  return useQueries({
    queries: communityIds.map((id) => ({
      queryKey: communityKeys.detail(id),
      queryFn: () => fetchPublicCommunity(id),
      staleTime: FIVE_MINUTES,
      retry: false,
    })),
    combine: (results) => {
      const details = new Map<number, PublicCommunity>();
      results.forEach((result, index) => {
        const id = communityIds[index];
        if (id !== undefined && result.data) details.set(id, result.data);
      });
      return details;
    },
  });
}

/** Public community discovery for a (debounced) search term; idle while the term is empty. */
export function useCommunitySearch(term: string, limit = 6): UseQueryResult<PublicCommunity[]> {
  const trimmed = term.trim();
  return useQuery({
    queryKey: communityKeys.search(trimmed),
    queryFn: () => searchPublicCommunities(trimmed, limit),
    enabled: trimmed !== '',
  });
}
