import { useMemo } from 'react';
import { useAuthView } from '../contexts/authView';
import type { MyCommunity } from '../services/myCommunitiesApi';
import { useMyCommunities } from './useMyCommunities';

/** What a page needs to gate UI on the caller's scopes in one community. */
export interface CommunityScopes {
  scopes: ReadonlySet<string>;
  /** True when the caller holds `scope` (super admins hold every scope). */
  has: (scope: string) => boolean;
  /** True when the caller holds at least one of `scopes`. */
  hasAny: (...scopes: string[]) => boolean;
  isLoading: boolean;
  /** Set when `/auth/me` failed, so callers show an error instead of a false "no permission". */
  error: Error | null;
  /** The caller's membership row, or undefined when not a member (or still loading). */
  membership: MyCommunity | undefined;
}

/**
 * Effective scopes of the signed-in user in one community, from `/auth/me`. All community
 * gating goes through this hook (never role names); the server still enforces every route.
 */
export function useCommunityScopes(communityId: string): CommunityScopes {
  const { isSuperAdmin } = useAuthView();
  const mine = useMyCommunities();
  const membership = mine.data?.find((c) => c.id === communityId);
  return useMemo(() => {
    const scopes: ReadonlySet<string> = new Set(membership?.scopes ?? []);
    const has = (scope: string): boolean => isSuperAdmin || scopes.has(scope);
    return {
      scopes,
      has,
      hasAny: (...wanted: string[]) => wanted.some(has),
      isLoading: mine.isPending,
      error: mine.error,
      membership,
    };
  }, [isSuperAdmin, membership, mine.isPending, mine.error]);
}
