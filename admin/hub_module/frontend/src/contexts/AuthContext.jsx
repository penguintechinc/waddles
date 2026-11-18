import { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import api from '../services/api';

const AuthContext = createContext(null);

// Threshold in milliseconds before user data is considered stale
const USER_STALE_THRESHOLD_MS = 30 * 1000; // 30 seconds

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const lastFetchedAt = useRef(null);

  // SECURITY (security.md C4): the session JWT lives only in the HttpOnly
  // `wb_session` cookie hub-api sets on login/OAuth-exchange/refresh — this
  // page has no way to read it (that is the point: an XSS payload can't
  // either). There is no client-side signal for "a session cookie might
  // exist", so every mount unconditionally asks the server via `/me`,
  // which already handles "not logged in" as a normal `{user: null}`
  // response rather than an error.
  useEffect(() => {
    fetchCurrentUser();
  }, []);

  const fetchCurrentUser = async () => {
    try {
      const response = await api.get('/api/v1/auth/me');
      if (response.data.success && response.data.user) {
        setUser(response.data.user);
        lastFetchedAt.current = Date.now();
      } else {
        setUser(null);
      }
    } catch (err) {
      console.error('[AuthContext] Failed to fetch current user');
      // Only clear the client's user state on auth errors (401/403).
      // Transient errors like 429 rate limiting or network failures should
      // not log the user out.
      const status = err.response?.status;
      if (status === 401 || status === 403) {
        setUser(null);
      }
    } finally {
      setLoading(false);
    }
  };

  /**
   * Re-fetch current user data from the server.
   * Skips if data was fetched within the staleness threshold
   * unless force=true is passed.
   */
  const refreshUser = useCallback(async (force = false) => {
    const now = Date.now();
    if (!force && lastFetchedAt.current && (now - lastFetchedAt.current) < USER_STALE_THRESHOLD_MS) {
      return; // Data is still fresh
    }

    await fetchCurrentUser();
  }, []);

  const loginWithOAuth = useCallback(async (platform) => {
    try {
      const response = await api.get(`/api/v1/auth/oauth/${platform}`);
      if (response.data.authorizeUrl) {
        window.location.href = response.data.authorizeUrl;
      }
    } catch (err) {
      setError(err.response?.data?.error || 'OAuth login failed');
      throw err;
    }
  }, []);

  const login = useCallback(async (email, password) => {
    try {
      setError(null);
      const response = await api.post('/api/v1/auth/login', { email, password });
      if (response.data.success) {
        // The session JWT arrived as an HttpOnly cookie (hub-api's
        // Set-Cookie on this same response) — nothing to persist client-side.
        setUser(response.data.user);
        return response.data;
      }
      // Handle requires verification response (403 with requiresVerification)
      if (response.data.requiresVerification) {
        const error = new Error(response.data.message || 'Email verification required');
        error.requiresVerification = true;
        throw error;
      }
    } catch (err) {
      // Check for verification required in error response
      if (err.response?.data?.requiresVerification) {
        const error = new Error(err.response.data.message || 'Email verification required');
        error.requiresVerification = true;
        throw error;
      }
      const message = err.response?.data?.error?.message || err.response?.data?.error || err.response?.data?.message || err.message || 'Login failed';
      setError(message);
      throw new Error(message);
    }
  }, []);

  const register = useCallback(async (email, password, username) => {
    try {
      setError(null);
      const response = await api.post('/api/v1/auth/register', { email, password, username });
      if (response.data.success) {
        // Handle email verification required case
        if (response.data.requiresVerification) {
          return { requiresVerification: true, message: response.data.message };
        }
        setUser(response.data.user);
        return response.data;
      }
    } catch (err) {
      const message = err.response?.data?.error?.message || err.response?.data?.error || err.response?.data?.message || 'Registration failed';
      setError(message);
      throw new Error(message);
    }
  }, []);

  // Legacy admin login (backwards compatibility)
  const loginWithAdmin = useCallback(async (username, password) => {
    try {
      setError(null);
      const response = await api.post('/api/v1/auth/admin', { username, password });
      if (response.data.success) {
        setUser(response.data.user);
        return response.data;
      }
    } catch (err) {
      const message = err.response?.data?.error?.message || err.response?.data?.error || 'Login failed';
      setError(message);
      throw new Error(message);
    }
  }, []);

  const loginWithTempPassword = useCallback(async (identifier, password) => {
    try {
      setError(null);
      const response = await api.post('/api/v1/auth/temp-password', { identifier, password });
      if (response.data.success) {
        await fetchCurrentUser();
        return response.data;
      }
    } catch (err) {
      const message = err.response?.data?.error?.message || err.response?.data?.error || 'Login failed';
      setError(message);
      throw new Error(message);
    }
  }, []);

  const handleOAuthCallback = useCallback(async (code) => {
    if (!code) return;
    // Exchange the short-lived, single-use code the OAuth callback redirect
    // carried in the URL for the real session JWT, delivered over the
    // response body instead of a query string (query strings leak into
    // proxy/access logs, browser history, and the Referer header).
    const response = await api.post('/api/v1/auth/exchange', { code });
    if (response.data.success && response.data.token) {
      // The exchange response also set the HttpOnly session cookie —
      // nothing to persist client-side, just pick up the resulting session.
      await fetchCurrentUser();
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post('/api/v1/auth/logout');
    } catch {
      console.error('[AuthContext] Logout request failed');
    } finally {
      // hub-api clears the session cookie on /logout regardless of outcome
      // above; always clear the client's own view of the session too.
      setUser(null);
    }
  }, []);

  const refreshToken = useCallback(async () => {
    try {
      const response = await api.post('/api/v1/auth/refresh');
      if (!response.data.success) {
        logout();
      }
      // hub-api rotates the session cookie on success — nothing to persist.
    } catch {
      console.error('[AuthContext] Token refresh failed');
      logout();
    }
  }, [logout]);

  const value = {
    user,
    loading,
    error,
    login,
    register,
    loginWithOAuth,
    loginWithAdmin,
    loginWithTempPassword,
    handleOAuthCallback,
    logout,
    refreshToken,
    refreshUser,
    isAuthenticated: !!user,
    // Role checks - use roles array directly
    hasRole: (role) => user?.roles?.includes(role),
    isAdmin: user?.roles?.includes('admin'),
    isSuperAdmin: user?.roles?.includes('super_admin'),
    isPlatformAdmin: user?.roles?.includes('platform-admin'),
    isVendor: user?.roles?.includes('vendor'),
    isAnalyticsConsumer: user?.isAnalyticsConsumer || false,
    // Scope-based authz (never role names). `scopes` and `tenantSlug` come from
    // the server-issued session (GET /auth/me); the webui never invents them.
    scopes: Array.isArray(user?.scopes) ? user.scopes : [],
    tenantSlug: typeof user?.tenantSlug === 'string' ? user.tenantSlug : null,
    hasScope: (scope) => Array.isArray(user?.scopes) && user.scopes.includes(scope),
    // License tier (1 Free, 2 Premium) of one of the caller's communities, from
    // /auth/me `communities[].licenseTier`. A missing/unknown value is Free. UX hint only: the
    // server's tier gates are authoritative.
    communityTier: (communityId) => {
      const c = user?.communities?.find((x) => x.id === Number(communityId));
      return c?.licenseTier === 2 ? 2 : 1;
    },
    // Community-level admin check (for any community, or a specific one)
    isCommunityAdmin: (communityId) => {
      const adminRoles = ['community-owner', 'community-admin', 'moderator'];
      if (communityId) {
        return user?.communities?.some(c => c.id === Number(communityId) && adminRoles.includes(c.role));
      }
      return user?.communities?.some(c => adminRoles.includes(c.role));
    },
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}

export default AuthContext;
