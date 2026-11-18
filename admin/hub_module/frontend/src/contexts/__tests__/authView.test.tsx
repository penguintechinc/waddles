import { describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { toAuthView, useAuthView } from '../authView';

const ctx = vi.hoisted(() => ({ value: null as unknown }));
vi.mock('../AuthContext', () => ({ useAuth: () => ctx.value }));

describe('toAuthView', () => {
  it('reads display fields through guards', () => {
    const view = toAuthView({ user: { username: 'tux' }, isAuthenticated: true, isSuperAdmin: true });
    expect(view).toMatchObject({ username: 'tux', isAuthenticated: true, isSuperAdmin: true });
  });

  it('falls back to safe defaults for missing or malformed context', () => {
    for (const raw of [null, undefined, 'x', 3, { user: 'nope', isAuthenticated: 'yes', isSuperAdmin: 1 }, { user: { username: '' } }]) {
      expect(toAuthView(raw)).toMatchObject({ username: null, isAuthenticated: false, isSuperAdmin: false });
    }
  });

  it('wraps isCommunityAdmin, passing the community id and treating anything but true as false', () => {
    const isCommunityAdmin = vi.fn((id: number) => id === 7);
    const view = toAuthView({ isCommunityAdmin });
    expect(view.isCommunityAdmin(7)).toBe(true);
    expect(view.isCommunityAdmin(8)).toBe(false);
    expect(isCommunityAdmin).toHaveBeenCalledWith(7);
    expect(toAuthView({ isCommunityAdmin: () => 'yes' }).isCommunityAdmin(1)).toBe(false);
    expect(toAuthView({}).isCommunityAdmin(7)).toBe(false);
  });

  it('refreshUser calls the context function and is a no-op when absent', async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    await toAuthView({ refreshUser: refresh }).refreshUser();
    expect(refresh).toHaveBeenCalledTimes(1);
    await expect(toAuthView({}).refreshUser()).resolves.toBeUndefined();
  });
});

describe('toAuthView communityTier', () => {
  it('wraps the context function and clamps unknown values to Free', () => {
    const view = toAuthView({ communityTier: (id: number) => (id === 7 ? 3 : id === 8 ? 2 : 42) });
    expect(view.communityTier(7)).toBe(3);
    expect(view.communityTier(8)).toBe(2);
    expect(view.communityTier(9)).toBe(1);
  });

  it('defaults to Free when the context has no communityTier', () => {
    expect(toAuthView({}).communityTier(7)).toBe(1);
  });
});

describe('useAuthView', () => {
  it('keeps refreshUser identity stable while the context function is unchanged', () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    ctx.value = { refreshUser: refresh, user: { username: 'a' } };
    const { result, rerender } = renderHook(() => useAuthView());
    const first = result.current.refreshUser;
    ctx.value = { refreshUser: refresh, user: { username: 'b' } };
    rerender();
    expect(result.current.refreshUser).toBe(first);
    expect(result.current.username).toBe('b');
  });

  it('changes identity only when the underlying function changes', () => {
    ctx.value = { refreshUser: vi.fn() };
    const { result, rerender } = renderHook(() => useAuthView());
    const first = result.current.refreshUser;
    ctx.value = { refreshUser: vi.fn() };
    rerender();
    expect(result.current.refreshUser).not.toBe(first);
  });
});
