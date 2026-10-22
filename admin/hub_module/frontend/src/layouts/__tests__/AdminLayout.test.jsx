import { render, screen, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';

let captured = {};
// Same workaround as DashboardLayout.test.jsx: the library's dist is a
// directory-import ESM package vitest cannot resolve, so stub the two exports.
vi.mock('@penguintechinc/react-libs', () => ({
  SidebarMenu: (props) => {
    captured = props;
    return <div data-testid="sidebar-menu" />;
  },
  SidebarMenuTrigger: ({ onClick, isOpen }) => (
    <button aria-label={isOpen ? 'Close sidebar' : 'Open sidebar'} onClick={onClick} />
  ),
}));
vi.mock('../../components/GlobalBanner', () => ({ default: () => null }));
vi.mock('../../components/UserDropdown', () => ({ default: () => <div data-testid="user-dropdown" /> }));

let bundlesFlag = false;
vi.mock('../../lib/useFeatureFlag', () => ({ useFeatureFlag: () => bundlesFlag }));

const mockAuth = { isPlatformAdmin: false, isSuperAdmin: false };
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => mockAuth }));

import AdminLayout from '../AdminLayout';

const renderAt = (path) => {
  captured = {};
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/admin/:communityId/*" element={<AdminLayout />}>
          <Route path="*" element={<h1>Page</h1>} />
        </Route>
        <Route path="/platform/*" element={<AdminLayout />}>
          <Route path="*" element={<h1>Platform page</h1>} />
        </Route>
        <Route path="/superadmin/*" element={<AdminLayout />}>
          <Route path="*" element={<h1>Super page</h1>} />
        </Route>
        <Route path="/dashboard" element={<div data-testid="dashboard" />} />
      </Routes>
    </MemoryRouter>,
  );
  return captured;
};

/** Items the shared sidebar will treat as active (mirrors SidebarMenu.isActive). */
const activeNames = (props) =>
  [...props.categories.flatMap((c) => c.items), ...props.footerItems]
    .filter((i) => props.currentPath === i.href || props.currentPath.startsWith(i.href))
    .map((i) => i.name);

describe('AdminLayout', () => {
  beforeEach(() => {
    mockAuth.isPlatformAdmin = false;
    mockAuth.isSuperAdmin = false;
    bundlesFlag = false;
  });

  it('only lists Bundle Apps when the bundles flag is on', () => {
    const names = (p) => p.categories.flatMap((c) => c.items).map((i) => i.name);
    expect(names(renderAt('/admin/5'))).not.toContain('Bundle Apps');
    bundlesFlag = true;
    expect(names(renderAt('/admin/5'))).toContain('Bundle Apps');
  });

  it('renders the community admin groups with collapsible headers', () => {
    const props = renderAt('/admin/5');
    expect(props.categories.map((c) => c.key)).toEqual(
      expect.arrayContaining(['community', 'engagement', 'media', 'loyalty', 'moderation']),
    );
    expect(props.categories.every((c) => c.collapsible)).toBe(true);
  });

  it('highlights only the Overview item on the admin home route', () => {
    expect(activeNames(renderAt('/admin/5'))).toEqual(['Overview']);
  });

  it('highlights exactly one item on a nested route and opens its group', () => {
    const props = renderAt('/admin/5/loyalty/leaderboard');
    expect(activeNames(props)).toEqual(['Leaderboard']);
    expect(props.activeGroupKey).toBe('loyalty');
  });

  it('suffixes premium items so the Pro tier is not conveyed by colour/badge alone', () => {
    const props = renderAt('/admin/5');
    const names = props.categories.flatMap((c) => c.items).map((i) => i.name);
    expect(names).toContain('Workflows (Pro)');
  });

  it('offers a way back to the dashboard and navigates there', () => {
    const props = renderAt('/admin/5');
    const back = props.footerItems.find((i) => i.name === 'Back to Dashboard');
    expect(back).toBeTruthy();
    act(() => props.onNavigate(back.href));
    expect(screen.getByTestId('dashboard')).toBeTruthy();
  });

  it('uses a single flat group for platform admin and adds Super Admin only when allowed', () => {
    const platform = renderAt('/platform');
    expect(platform.categories).toHaveLength(1);
    expect(platform.categories[0].header).toBe('Platform Admin');
    expect(platform.footerItems.map((i) => i.name)).not.toContain('Super Admin');

    mockAuth.isSuperAdmin = true;
    const platformAsSuper = renderAt('/platform');
    expect(platformAsSuper.footerItems.map((i) => i.name)).toContain('Super Admin');
  });

  it('shows the super admin nav and hides the Super Admin shortcut on its own routes', () => {
    mockAuth.isSuperAdmin = true;
    const props = renderAt('/superadmin/tenants');
    expect(props.categories[0].header).toBe('Super Admin');
    expect(activeNames(props)).toEqual(['Tenants']);
    expect(props.footerItems.map((i) => i.name)).not.toContain('Super Admin');
  });

  it('provides a skip link and a focusable main landmark', () => {
    renderAt('/admin/5');
    const skip = screen.getByRole('link', { name: 'Skip to main content' });
    const main = screen.getByRole('main');
    expect(skip.getAttribute('href')).toBe(`#${main.id}`);
    expect(main.getAttribute('tabindex')).toBe('-1');
  });

  it('opens the mobile drawer from the header trigger', () => {
    const props = renderAt('/admin/5');
    expect(props.mobileOpen).toBe(false);
    act(() => screen.getByRole('button', { name: 'Open sidebar' }).click());
    expect(captured.mobileOpen).toBe(true);
  });
});
