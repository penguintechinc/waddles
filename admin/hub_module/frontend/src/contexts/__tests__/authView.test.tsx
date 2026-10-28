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

  it('refreshUser calls the context function and is a no-op when absent', async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    await toAuthView({ refreshUser: refresh }).refreshUser();
    expect(refresh).toHaveBeenCalledTimes(1);
    await expect(toAuthView({}).refreshUser()).resolves.toBeUndefined();
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
