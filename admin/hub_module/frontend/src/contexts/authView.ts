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

/** Narrows an unknown context value into an `AuthView`. Exported for tests. */
export function toAuthView(raw: unknown): AuthView {
  const record: WireRecord = isRecord(raw) ? raw : {};
  return {
    username: readUsername(record),
    isAuthenticated: record['isAuthenticated'] === true,
    isSuperAdmin: record['isSuperAdmin'] === true,
    isCommunityAdmin: narrowCommunityAdmin(record['isCommunityAdmin']),
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
