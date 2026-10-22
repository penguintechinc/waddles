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
  /** Re-fetches `/auth/me` into the context (skipped by the context if fresh). */
  refreshUser: () => Promise<void>;
}

function readUsername(raw: WireRecord): string | null {
  const user = raw['user'];
  if (!isRecord(user)) return null;
  const username = user['username'];
  return typeof username === 'string' && username !== '' ? username : null;
}

/** Narrows an unknown context value into an `AuthView`. Exported for tests. */
export function toAuthView(raw: unknown): AuthView {
  const record: WireRecord = isRecord(raw) ? raw : {};
  const refresh: unknown = record['refreshUser'];
  return {
    username: readUsername(record),
    isAuthenticated: record['isAuthenticated'] === true,
    isSuperAdmin: record['isSuperAdmin'] === true,
    refreshUser: async () => {
      if (typeof refresh === 'function') await Promise.resolve(Reflect.apply(refresh, undefined, []));
    },
  };
}

/** `useAuth()` narrowed to the handful of fields the typed pages need. */
export function useAuthView(): AuthView {
  const raw: unknown = useAuth();
  return toAuthView(raw);
}
