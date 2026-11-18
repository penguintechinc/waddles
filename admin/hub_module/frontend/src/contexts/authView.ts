import { useCallback } from 'react';
import { useAuth } from './AuthContext';
import { isRecord, type WireRecord } from '../lib/wire';

/**
 * Typed, narrowed view of `AuthContext` for TypeScript pages. The context is plain JS
 * (`checkJs: false`, `createContext(null)` infers `never`), so rather than casting it
 * this reads each field through a guard and falls back to a safe default.
 */
export interface AuthView {
  /** Display fields only; the full user object stays in the context. */
  username: string | null;
  isAuthenticated: boolean;
  isSuperAdmin: boolean;
  /** Whether the caller administers the given community (false when the context cannot say). */
  isCommunityAdmin: (communityId: number) => boolean;
  /** License tier code (1 Free, 2 Professional, 3 Enterprise) of one of the caller's communities; missing = 1. UX hint only. */
  communityTier: (communityId: number) => number;
  /** Re-fetches `/auth/me` into the context (skipped by the context if fresh). */
  refreshUser: () => Promise<void>;
}

function readUsername(raw: WireRecord): string | null {
  const user = raw['user'];
  if (!isRecord(user)) return null;
  const username = user['username'];
  return typeof username === 'string' && username !== '' ? username : null;
}

/** Calls the context's `refreshUser` if it is a function; a no-op otherwise. */
async function callRefresh(refresh: unknown): Promise<void> {
  if (typeof refresh === 'function') await Promise.resolve(Reflect.apply(refresh, undefined, []));
}

/** Wraps the context's `isCommunityAdmin(communityId)` when it is a function; "not an admin" otherwise. */
function narrowCommunityAdmin(fn: unknown): (communityId: number) => boolean {
  if (typeof fn !== 'function') return () => false;
  return (communityId) => Reflect.apply(fn, undefined, [communityId]) === true;
}

/** Wraps the context's `communityTier(communityId)` when it is a function; Free (1) otherwise. */
function narrowCommunityTier(fn: unknown): (communityId: number) => number {
  if (typeof fn !== 'function') return () => 1;
  return (communityId) => {
    const tier: unknown = Reflect.apply(fn, undefined, [communityId]);
    return tier === 2 || tier === 3 ? tier : 1;
  };
}

/** Narrows an unknown context value into an `AuthView`. Exported for tests. */
export function toAuthView(raw: unknown): AuthView {
  const record: WireRecord = isRecord(raw) ? raw : {};
  return {
    username: readUsername(record),
    isAuthenticated: record['isAuthenticated'] === true,
    isSuperAdmin: record['isSuperAdmin'] === true,
    isCommunityAdmin: narrowCommunityAdmin(record['isCommunityAdmin']),
    communityTier: narrowCommunityTier(record['communityTier']),
    refreshUser: () => callRefresh(record['refreshUser']),
  };
}

/**
 * `useAuth()` narrowed to the handful of fields the typed pages need. `refreshUser` keeps a
 * stable identity for as long as the context's own function does, so it is safe in effect deps.
 */
export function useAuthView(): AuthView {
  const raw: unknown = useAuth();
  const underlying: unknown = isRecord(raw) ? raw['refreshUser'] : undefined;
  const refreshUser = useCallback(() => callRefresh(underlying), [underlying]);
  return { ...toAuthView(raw), refreshUser };
}
