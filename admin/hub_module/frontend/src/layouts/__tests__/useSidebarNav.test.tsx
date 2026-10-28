import { describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useSidebarNav, type NavGroup } from '../useSidebarNav';

const GROUPS: NavGroup[] = [
  {
    key: 'community',
    header: 'Community',
    collapsible: true,
    items: [
      { name: 'Overview', href: '/admin/5', exact: true },
      { name: 'Loyalty', href: '/admin/5/loyalty' },
      { name: 'Loyalty leaderboard', href: '/admin/5/loyalty/leaderboard' },
    ],
  },
];
const FOOTER = [{ name: 'Back to dashboard', href: '/dashboard' }];

/** Name of the item the shared sidebar will treat as active for `pathname`. */
function activeName(pathname: string): string | undefined {
  const navigate = vi.fn();
  const { result } = renderHook(() => useSidebarNav(GROUPS, FOOTER, pathname, navigate));
  const { categories, footerItems, currentPath } = result.current;
  // Mirrors SidebarMenu.isActive: currentPath === href || currentPath.startsWith(href)
  const active = [...categories.flatMap((c) => c.items), ...footerItems].filter(
    (i) => currentPath === i.href || (i.href !== '/' && currentPath.startsWith(i.href)),
  );
  expect(active.length).toBeLessThanOrEqual(1);
  return active[0]?.name;
}

describe('useSidebarNav', () => {
  it('highlights exactly one item: the longest matching route', () => {
    expect(activeName('/admin/5/loyalty/leaderboard')).toBe('Loyalty leaderboard');
    expect(activeName('/admin/5/loyalty')).toBe('Loyalty');
    expect(activeName('/admin/5/loyalty/gear')).toBe('Loyalty');
  });

  it('honours exact entries so an Overview route does not match every child', () => {
    expect(activeName('/admin/5')).toBe('Overview');
    expect(activeName('/admin/5/unknown')).toBeUndefined();
  });

  it('matches footer items too', () => {
    expect(activeName('/dashboard')).toBe('Back to dashboard');
  });

  it('does not treat a sibling that merely shares a prefix as a match', () => {
    expect(activeName('/admin/5/loyaltyfoo')).toBeUndefined();
  });

  it('translates token hrefs back to real routes on navigate and forwards real paths', () => {
    const navigate = vi.fn();
    const { result } = renderHook(() => useSidebarNav(GROUPS, FOOTER, '/admin/5', navigate));
    const loyalty = result.current.categories[0]?.items[1];
    expect(loyalty).toBeDefined();
    result.current.onNavigate(loyalty!.href);
    expect(navigate).toHaveBeenLastCalledWith('/admin/5/loyalty');
    result.current.onNavigate('/some/real/path');
    expect(navigate).toHaveBeenLastCalledWith('/some/real/path');
  });

  it('keeps group metadata and omits an undefined defaultOpen', () => {
    const { result } = renderHook(() => useSidebarNav(GROUPS, FOOTER, '/admin/5', vi.fn()));
    const group = result.current.categories[0];
    expect(group).toMatchObject({ key: 'community', header: 'Community', collapsible: true });
    expect(group && 'defaultOpen' in group).toBe(false);
  });
});
