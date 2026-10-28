/**
 * Behavioural tests for the rest of AuthContext (the C4/exchange regression
 * file covers the cookie handoff): current-user bootstrap + error policy,
 * every login/registration variant's success and failure messaging, refresh
 * staleness, logout/token-refresh failure handling, and role/community checks.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, renderHook, waitFor } from '@testing-library/react';

import api from '../../services/api';
import { AuthProvider, useAuth } from '../AuthContext';

vi.mock('../../services/api', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

const USER = {
  id: 7,
  roles: ['admin', 'vendor'],
  isAnalyticsConsumer: true,
  communities: [
    { id: 1, role: 'community-owner' },
    { id: 2, role: 'member' },
    { id: 3, role: 'moderator' },
  ],
};

/** Mounts the provider and returns a live view of the context value. */
async function mountAuth(me = { data: { success: true, user: USER } }) {
  if (me instanceof Error || me?.response) api.get.mockRejectedValue(me);
  else api.get.mockResolvedValue(me);
  const holder = { current: null };
  function Probe() {
    holder.current = useAuth();
    return null;
  }
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );
  await waitFor(() => expect(holder.current.loading).toBe(false));
  return holder;
}

const httpError = (status, data = {}) => ({ response: { status, data } });

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AuthContext bootstrap', () => {
  it('loads the current user from /me', async () => {
    const auth = await mountAuth();
    expect(api.get).toHaveBeenCalledWith('/api/v1/auth/me');
    expect(auth.current.user).toEqual(USER);
    expect(auth.current.isAuthenticated).toBe(true);
  });

  it.each([
    ['success without a user', { data: { success: true, user: null } }],
    ['success:false', { data: { success: false } }],
  ])('treats %s as logged out', async (_label, me) => {
    const auth = await mountAuth(me);
    expect(auth.current.user).toBeNull();
    expect(auth.current.isAuthenticated).toBe(false);
  });

  it.each([401, 403])('clears the user on an auth error (%i)', async (status) => {
    const auth = await mountAuth(httpError(status));
    expect(auth.current.user).toBeNull();
  });

  it.each([429, 500])('keeps the existing session through a transient %i', async (status) => {
    const auth = await mountAuth();
    api.get.mockRejectedValue(httpError(status));
    await act(async () => {
      await auth.current.refreshUser(true);
    });
    expect(auth.current.user).toEqual(USER);
  });

  it('survives a network error without a response', async () => {
    const auth = await mountAuth(new Error('offline'));
    expect(auth.current.user).toBeNull();
    expect(auth.current.loading).toBe(false);
  });
});

describe('AuthContext refreshUser', () => {
  it('skips the request while the user data is still fresh', async () => {
    const auth = await mountAuth();
    api.get.mockClear();
    await act(async () => {
      await auth.current.refreshUser();
    });
    expect(api.get).not.toHaveBeenCalled();
  });

  it('refetches when forced, and once the data has gone stale', async () => {
    const auth = await mountAuth();
    api.get.mockClear();
    await act(async () => {
      await auth.current.refreshUser(true);
    });
    expect(api.get).toHaveBeenCalledTimes(1);

    const realNow = Date.now;
    vi.spyOn(Date, 'now').mockImplementation(() => realNow() + 60000);
    await act(async () => {
      await auth.current.refreshUser();
    });
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('always fetches when nothing has been loaded yet', async () => {
    const auth = await mountAuth({ data: { success: false } });
    api.get.mockClear();
    api.get.mockResolvedValue({ data: { success: true, user: USER } });
    await act(async () => {
      await auth.current.refreshUser();
    });
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(auth.current.user).toEqual(USER);
  });
});

describe('AuthContext login', () => {
  it('posts credentials and stores the returned user', async () => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockResolvedValue({ data: { success: true, user: USER } });

    let result;
    await act(async () => {
      result = await auth.current.login('a@b.c', 'pw');
    });

    expect(api.post).toHaveBeenCalledWith('/api/v1/auth/login', { email: 'a@b.c', password: 'pw' });
    expect(result.user).toEqual(USER);
    expect(auth.current.user).toEqual(USER);
    expect(auth.current.error).toBeNull();
  });

  it('surfaces the verification message for a 200 body that asks for verification', async () => {
    // (Only the 403 path below keeps the `requiresVerification` flag; hub-api
    // answers 403, so this 200-body branch just has to surface the message.)
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockResolvedValue({ data: { success: false, requiresVerification: true, message: 'Verify first' } });

    await expect(auth.current.login('a@b.c', 'pw')).rejects.toThrow('Verify first');
  });

  it('uses a default message when the verification body has none', async () => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockResolvedValue({ data: { success: false, requiresVerification: true } });
    await expect(auth.current.login('a@b.c', 'pw')).rejects.toThrow('Email verification required');
  });

  it('raises a requiresVerification error for a 403 response', async () => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockRejectedValue(httpError(403, { requiresVerification: true, message: 'Check your inbox' }));
    await expect(auth.current.login('a@b.c', 'pw')).rejects.toMatchObject({
      message: 'Check your inbox',
      requiresVerification: true,
    });
  });

  it.each([
    ['nested error message', httpError(401, { error: { message: 'Bad creds' } }), 'Bad creds'],
    ['string error', httpError(401, { error: 'Locked' }), 'Locked'],
    ['top-level message', httpError(400, { message: 'Too many tries' }), 'Too many tries'],
    ['transport error message', new Error('Network Error'), 'Network Error'],
    ['nothing to go on', {}, 'Login failed'],
  ])('surfaces the %s and records it on the context', async (_label, err, message) => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockRejectedValue(err);

    await act(async () => {
      await expect(auth.current.login('a@b.c', 'pw')).rejects.toThrow(message);
    });
    expect(auth.current.error).toBe(message);
  });
});

describe('AuthContext register', () => {
  it('registers and signs the new user in', async () => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockResolvedValue({ data: { success: true, user: USER } });

    await act(async () => {
      await auth.current.register('a@b.c', 'pw', 'alice');
    });

    expect(api.post).toHaveBeenCalledWith('/api/v1/auth/register', { email: 'a@b.c', password: 'pw', username: 'alice' });
    expect(auth.current.user).toEqual(USER);
  });

  it('returns the verification prompt instead of signing in', async () => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockResolvedValue({ data: { success: true, requiresVerification: true, message: 'Check email' } });

    let result;
    await act(async () => {
      result = await auth.current.register('a@b.c', 'pw', 'alice');
    });

    expect(result).toEqual({ requiresVerification: true, message: 'Check email' });
    expect(auth.current.user).toBeNull();
  });

  it.each([
    ['nested error message', httpError(400, { error: { message: 'Email taken' } }), 'Email taken'],
    ['string error', httpError(400, { error: 'Weak password' }), 'Weak password'],
    ['top-level message', httpError(400, { message: 'Slow down' }), 'Slow down'],
    ['nothing to go on', new Error('x'), 'Registration failed'],
  ])('surfaces the %s', async (_label, err, message) => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockRejectedValue(err);
    await act(async () => {
      await expect(auth.current.register('a@b.c', 'pw', 'alice')).rejects.toThrow(message);
    });
    expect(auth.current.error).toBe(message);
  });
});

describe('AuthContext admin and temp-password login', () => {
  it('logs in with the legacy admin endpoint', async () => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockResolvedValue({ data: { success: true, user: USER } });
    await act(async () => {
      await auth.current.loginWithAdmin('root', 'pw');
    });
    expect(api.post).toHaveBeenCalledWith('/api/v1/auth/admin', { username: 'root', password: 'pw' });
    expect(auth.current.user).toEqual(USER);
  });

  it.each([
    ['nested message', httpError(401, { error: { message: 'No such admin' } }), 'No such admin'],
    ['string error', httpError(401, { error: 'Denied' }), 'Denied'],
    ['nothing to go on', new Error('x'), 'Login failed'],
  ])('admin login surfaces the %s', async (_label, err, message) => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockRejectedValue(err);
    await act(async () => {
      await expect(auth.current.loginWithAdmin('root', 'pw')).rejects.toThrow(message);
    });
    expect(auth.current.error).toBe(message);
  });

  it('temp-password login posts identifier+password then loads the session user', async () => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockResolvedValue({ data: { success: true } });
    api.get.mockResolvedValue({ data: { success: true, user: USER } });

    await act(async () => {
      await auth.current.loginWithTempPassword('alice', 'temp');
    });

    expect(api.post).toHaveBeenCalledWith('/api/v1/auth/temp-password', { identifier: 'alice', password: 'temp' });
    expect(auth.current.user).toEqual(USER);
  });

  it.each([
    ['nested message', httpError(401, { error: { message: 'Expired' } }), 'Expired'],
    ['string error', httpError(401, { error: 'Used' }), 'Used'],
    ['nothing to go on', new Error('x'), 'Login failed'],
  ])('temp-password login surfaces the %s', async (_label, err, message) => {
    const auth = await mountAuth({ data: { success: false } });
    api.post.mockRejectedValue(err);
    await act(async () => {
      await expect(auth.current.loginWithTempPassword('alice', 'temp')).rejects.toThrow(message);
    });
    expect(auth.current.error).toBe(message);
  });
});

describe('AuthContext OAuth redirect login', () => {
  beforeEach(() => {
    vi.stubGlobal('location', { href: '/login' });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('navigates to the provider authorize URL', async () => {
    const auth = await mountAuth({ data: { success: false } });
    api.get.mockResolvedValue({ data: { authorizeUrl: 'https://discord.com/oauth2/authorize?x=1' } });
    await act(async () => {
      await auth.current.loginWithOAuth('discord');
    });
    expect(api.get).toHaveBeenLastCalledWith('/api/v1/auth/oauth/discord');
    expect(window.location.href).toBe('https://discord.com/oauth2/authorize?x=1');
  });

  it('stays put when the response has no authorize URL', async () => {
    const auth = await mountAuth({ data: { success: false } });
    api.get.mockResolvedValue({ data: {} });
    await act(async () => {
      await auth.current.loginWithOAuth('discord');
    });
    expect(window.location.href).toBe('/login');
  });

  it.each([
    ['the server error', httpError(500, { error: 'provider down' }), 'provider down'],
    ['a generic fallback', new Error('x'), 'OAuth login failed'],
  ])('records %s and rethrows', async (_label, err, message) => {
    const auth = await mountAuth({ data: { success: false } });
    api.get.mockRejectedValue(err);
    await act(async () => {
      await expect(auth.current.loginWithOAuth('discord')).rejects.toBe(err);
    });
    expect(auth.current.error).toBe(message);
  });
});

describe('AuthContext logout and token refresh', () => {
  it('logout clears the user even when the request fails', async () => {
    const auth = await mountAuth();
    api.post.mockRejectedValue(new Error('server down'));
    await act(async () => {
      await auth.current.logout();
    });
    expect(auth.current.user).toBeNull();
    expect(console.error).toHaveBeenCalledWith('[AuthContext] Logout request failed');
  });

  it('refreshToken leaves the session alone on success', async () => {
    const auth = await mountAuth();
    api.post.mockResolvedValue({ data: { success: true } });
    await act(async () => {
      await auth.current.refreshToken();
    });
    expect(api.post).toHaveBeenCalledWith('/api/v1/auth/refresh');
    expect(auth.current.user).toEqual(USER);
  });

  it('refreshToken logs out when the server declines the refresh', async () => {
    const auth = await mountAuth();
    api.post.mockImplementation((url) =>
      Promise.resolve({ data: { success: url === '/api/v1/auth/logout' } }),
    );
    await act(async () => {
      await auth.current.refreshToken();
    });
    await waitFor(() => expect(auth.current.user).toBeNull());
    expect(api.post).toHaveBeenCalledWith('/api/v1/auth/logout');
  });

  it('refreshToken logs out when the request itself fails', async () => {
    const auth = await mountAuth();
    api.post.mockRejectedValue(new Error('offline'));
    await act(async () => {
      await auth.current.refreshToken();
    });
    await waitFor(() => expect(auth.current.user).toBeNull());
    expect(console.error).toHaveBeenCalledWith('[AuthContext] Token refresh failed');
  });
});

describe('AuthContext role helpers', () => {
  it('derives role flags from the user roles', async () => {
    const auth = await mountAuth();
    expect(auth.current.hasRole('admin')).toBe(true);
    expect(auth.current.hasRole('nope')).toBe(false);
    expect(auth.current.isAdmin).toBe(true);
    expect(auth.current.isVendor).toBe(true);
    expect(auth.current.isSuperAdmin).toBe(false);
    expect(auth.current.isPlatformAdmin).toBe(false);
    expect(auth.current.isAnalyticsConsumer).toBe(true);
  });

  it('is falsy across the board when logged out', async () => {
    const auth = await mountAuth({ data: { success: false } });
    expect(auth.current.hasRole('admin')).toBeUndefined();
    expect(auth.current.isAdmin).toBeUndefined();
    expect(auth.current.isAnalyticsConsumer).toBe(false);
    expect(auth.current.isCommunityAdmin()).toBeUndefined();
    expect(auth.current.isCommunityAdmin(1)).toBeUndefined();
  });

  it('recognises super and platform admins', async () => {
    const auth = await mountAuth({ data: { success: true, user: { roles: ['super_admin', 'platform-admin'] } } });
    expect(auth.current.isSuperAdmin).toBe(true);
    expect(auth.current.isPlatformAdmin).toBe(true);
  });

  it('isCommunityAdmin checks any community, or a specific one by (string or number) id', async () => {
    const auth = await mountAuth();
    expect(auth.current.isCommunityAdmin()).toBe(true);
    expect(auth.current.isCommunityAdmin(1)).toBe(true);
    expect(auth.current.isCommunityAdmin('3')).toBe(true);
    expect(auth.current.isCommunityAdmin(2)).toBe(false);
    expect(auth.current.isCommunityAdmin(99)).toBe(false);
  });

  it('isCommunityAdmin is false for a user whose only role is plain member', async () => {
    const auth = await mountAuth({ data: { success: true, user: { roles: [], communities: [{ id: 2, role: 'member' }] } } });
    expect(auth.current.isCommunityAdmin()).toBe(false);
  });
});

describe('useAuth outside a provider', () => {
  it('throws a descriptive error', () => {
    expect(() => renderHook(() => useAuth())).toThrow('useAuth must be used within an AuthProvider');
  });
});
