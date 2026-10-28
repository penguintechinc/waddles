import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';

// Capture props passed to SidebarMenu; SidebarMenuTrigger renders a real button
// so we can test toggling behaviour without stubbing it out.
let capturedProps = {};
vi.mock('@penguintechinc/react-libs', () => ({
  SidebarMenu: (props) => {
    capturedProps = props;
    return <div data-testid="sidebar-menu" />;
  },
  SidebarMenuTrigger: ({ onClick, isOpen }) => (
    <button
      data-testid="sidebar-trigger"
      aria-label={isOpen ? 'Close sidebar' : 'Open sidebar'}
      onClick={onClick}
    />
  ),
}));
vi.mock('../../components/GlobalBanner', () => ({ default: () => null }));
vi.mock('../../components/VendorRequestFooter', () => ({ default: () => null }));

const mockAuth = {
  user: { username: 'testuser', displayName: 'Test User' },
  logout: vi.fn(),
  isSuperAdmin: false,
  isVendor: false,
  isAnalyticsConsumer: false,
  isCommunityAdmin: vi.fn(() => false),
};

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => mockAuth,
}));

import DashboardLayout from '../DashboardLayout';

// Helper: renders DashboardLayout at the given path with routes that expose :id param
const renderAt = (path) => {
  capturedProps = {};
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/dashboard/community/:id/*" element={<DashboardLayout />} />
        <Route path="/admin/platform/*" element={<DashboardLayout />} />
        <Route path="/admin/:id/*" element={<DashboardLayout />} />
        <Route path="*" element={<DashboardLayout />} />
      </Routes>
    </MemoryRouter>
  );
  return capturedProps;
};

describe('DashboardLayout — activeGroupKey', () => {
  beforeEach(() => {
    mockAuth.isCommunityAdmin.mockReturnValue(false);
    mockAuth.isSuperAdmin = false;
    mockAuth.isVendor = false;
  });

  it('is "main" on /dashboard (no communityId)', () => {
    const props = renderAt('/dashboard');
    expect(props.activeGroupKey).toBe('main');
  });

  it('is "community" on /dashboard/community/:id', () => {
    const props = renderAt('/dashboard/community/abc');
    expect(props.activeGroupKey).toBe('community');
  });

  it('is "community-admin" on /admin/:id (not /admin/platform)', () => {
    const props = renderAt('/admin/abc');
    expect(props.activeGroupKey).toBe('community-admin');
  });

  it('is "super-admin" on /superadmin', () => {
    const props = renderAt('/superadmin');
    expect(props.activeGroupKey).toBe('super-admin');
  });

  it('is "super-admin" on /admin/platform', () => {
    const props = renderAt('/admin/platform/settings');
    expect(props.activeGroupKey).toBe('super-admin');
  });

  it('is "main" on unknown route with no communityId', () => {
    const props = renderAt('/some/unknown/route');
    expect(props.activeGroupKey).toBe('main');
  });
});

describe('DashboardLayout — categories', () => {
  beforeEach(() => {
    mockAuth.isCommunityAdmin.mockReturnValue(false);
    mockAuth.isSuperAdmin = false;
    mockAuth.isVendor = false;
    mockAuth.isAnalyticsConsumer = false;
  });

  it('produces "main" category when no communityId', () => {
    const props = renderAt('/dashboard');
    expect(props.categories.find((c) => c.key === 'main')).toBeTruthy();
  });

  it('"main" category is not collapsible', () => {
    const props = renderAt('/dashboard');
    const main = props.categories.find((c) => c.key === 'main');
    expect(main.collapsible).toBe(false);
  });

  it('produces "community" category when communityId present', () => {
    const props = renderAt('/dashboard/community/abc');
    expect(props.categories.find((c) => c.key === 'community')).toBeTruthy();
  });

  it('does not produce "main" when communityId present', () => {
    const props = renderAt('/dashboard/community/abc');
    expect(props.categories.find((c) => c.key === 'main')).toBeFalsy();
  });

  it('does not include "community-admin" when isCommunityAdmin returns false', () => {
    const props = renderAt('/dashboard/community/abc');
    expect(props.categories.find((c) => c.key === 'community-admin')).toBeFalsy();
  });

  it('includes "community-admin" when isCommunityAdmin returns true', () => {
    mockAuth.isCommunityAdmin.mockReturnValue(true);
    const props = renderAt('/dashboard/community/abc');
    expect(props.categories.find((c) => c.key === 'community-admin')).toBeTruthy();
  });

  it('does not include "vendor" when isVendor is false', () => {
    const props = renderAt('/dashboard');
    expect(props.categories.find((c) => c.key === 'vendor')).toBeFalsy();
  });

  it('includes "vendor" when isVendor is true', () => {
    mockAuth.isVendor = true;
    const props = renderAt('/dashboard');
    expect(props.categories.find((c) => c.key === 'vendor')).toBeTruthy();
  });

  it('does not include "super-admin" when isSuperAdmin is false', () => {
    const props = renderAt('/dashboard');
    expect(props.categories.find((c) => c.key === 'super-admin')).toBeFalsy();
  });

  it('includes "super-admin" when isSuperAdmin is true', () => {
    mockAuth.isSuperAdmin = true;
    const props = renderAt('/dashboard');
    expect(props.categories.find((c) => c.key === 'super-admin')).toBeTruthy();
  });
});

describe('DashboardLayout — landmarks and keyboard access', () => {
  it('renders a skip link that targets a focusable main landmark', () => {
    renderAt('/dashboard');
    const skip = screen.getByRole('link', { name: 'Skip to main content' });
    const main = screen.getByRole('main');
    expect(skip.getAttribute('href')).toBe(`#${main.id}`);
    expect(main.getAttribute('tabindex')).toBe('-1');
  });

  it('exposes the route announcer live region', () => {
    renderAt('/dashboard');
    expect(screen.getByTestId('route-announcer')).toBeTruthy();
  });

  it('points the community Moderation entry at a route that exists (admin security)', () => {
    mockAuth.isCommunityAdmin.mockReturnValue(true);
    capturedProps = {};
    render(
      <MemoryRouter initialEntries={['/dashboard/community/7']}>
        <Routes>
          <Route path="/dashboard/community/:id/*" element={<DashboardLayout />} />
          <Route path="/admin/:communityId/security" element={<div data-testid="security-page" />} />
        </Routes>
      </MemoryRouter>,
    );
    const admin = capturedProps.categories.find((c) => c.key === 'community-admin');
    const moderation = admin.items.find((i) => i.name === 'Moderation');
    act(() => capturedProps.onNavigate(moderation.href));
    expect(screen.getByTestId('security-page')).toBeTruthy();
  });
});

describe('DashboardLayout — SidebarMenu props', () => {
  beforeEach(() => {
    mockAuth.isCommunityAdmin.mockReturnValue(false);
    mockAuth.isSuperAdmin = false;
    mockAuth.isVendor = false;
  });

  it('passes autoCollapse=true to SidebarMenu', () => {
    const props = renderAt('/dashboard');
    expect(props.autoCollapse).toBe(true);
  });

  it('marks exactly the route-matching item active (longest match, not every prefix)', () => {
    // SidebarMenu treats `currentPath.startsWith(item.href)` as active; the layout
    // hands it tokenised hrefs so only the best match lights up.
    const props = renderAt('/dashboard/profile');
    const items = [...props.categories.flatMap((c) => c.items), ...props.footerItems];
    const active = items.filter((i) => props.currentPath === i.href || props.currentPath.startsWith(i.href));
    expect(active.map((i) => i.name)).toEqual(['My Profile', 'Test User'].slice(0, 1));
  });

  it('passes footerItems array with at least one item', () => {
    const props = renderAt('/dashboard');
    expect(Array.isArray(props.footerItems)).toBe(true);
    expect(props.footerItems.length).toBeGreaterThan(0);
  });

  it('keeps the fixed desktop sidebar outside the offset content shell', () => {
    renderAt('/dashboard');
    expect(screen.getByTestId('dashboard-content-shell').className).toContain('lg:pl-64');
  });

  it('onNavigate callback invokes navigate (does not throw)', () => {
    // The onNavigate prop is `(href) => navigate(href)`. Calling it exercises
    // line 172. The real MemoryRouter provides useNavigate, so no extra mock needed.
    const props = renderAt('/dashboard');
    expect(() => props.onNavigate('/some-path')).not.toThrow();
  });
});

describe('DashboardLayout — footerItems displayName fallbacks', () => {
  beforeEach(() => {
    mockAuth.isCommunityAdmin.mockReturnValue(false);
    mockAuth.isSuperAdmin = false;
    mockAuth.isVendor = false;
  });

  it('uses displayName when present', () => {
    mockAuth.user = { username: 'testuser', displayName: 'Test User' };
    const props = renderAt('/dashboard');
    expect(props.footerItems[0].name).toBe('Test User');
  });

  it('falls back to username when displayName is absent', () => {
    mockAuth.user = { username: 'testuser' };
    const props = renderAt('/dashboard');
    expect(props.footerItems[0].name).toBe('testuser');
  });

  it('falls back to "Account" when both displayName and username are absent', () => {
    mockAuth.user = {};
    const props = renderAt('/dashboard');
    expect(props.footerItems[0].name).toBe('Account');
  });
});

describe('DashboardLayout — avatarUrl branch', () => {
  beforeEach(() => {
    mockAuth.isCommunityAdmin.mockReturnValue(false);
    mockAuth.isSuperAdmin = false;
    mockAuth.isVendor = false;
    mockAuth.isAnalyticsConsumer = false;
  });

  it('renders avatar img when user has avatarUrl', () => {
    mockAuth.user = { username: 'testuser', displayName: 'Test User', avatarUrl: 'https://example.com/avatar.png' };
    render(
      <MemoryRouter initialEntries={['/dashboard']}>
        <Routes>
          <Route path="*" element={<DashboardLayout />} />
        </Routes>
      </MemoryRouter>
    );
    expect(screen.getByAltText('testuser')).toBeTruthy();
  });
});

describe('DashboardLayout — isAnalyticsConsumer branch', () => {
  beforeEach(() => {
    mockAuth.isCommunityAdmin.mockReturnValue(false);
    mockAuth.isSuperAdmin = false;
    mockAuth.isVendor = false;
    mockAuth.user = { username: 'testuser', displayName: 'Test User' };
  });

  it('does not include Platform Analytics item when isAnalyticsConsumer is false and not super admin', () => {
    mockAuth.isAnalyticsConsumer = false;
    const props = renderAt('/dashboard');
    const main = props.categories.find((c) => c.key === 'main');
    const hasAnalytics = main.items.some((i) => i.name === 'Platform Analytics');
    expect(hasAnalytics).toBe(false);
  });

  it('includes Platform Analytics item when isAnalyticsConsumer is true', () => {
    mockAuth.isAnalyticsConsumer = true;
    const props = renderAt('/dashboard');
    const main = props.categories.find((c) => c.key === 'main');
    const hasAnalytics = main.items.some((i) => i.name === 'Platform Analytics');
    expect(hasAnalytics).toBe(true);
  });

  it('includes Platform Analytics item when isSuperAdmin is true (even if not analytics consumer)', () => {
    mockAuth.isAnalyticsConsumer = false;
    mockAuth.isSuperAdmin = true;
    const props = renderAt('/dashboard');
    const main = props.categories.find((c) => c.key === 'main');
    const hasAnalytics = main.items.some((i) => i.name === 'Platform Analytics');
    expect(hasAnalytics).toBe(true);
  });
});

describe('DashboardLayout — renders SidebarMenu and SidebarMenuTrigger', () => {
  beforeEach(() => {
    mockAuth.isCommunityAdmin.mockReturnValue(false);
    mockAuth.isSuperAdmin = false;
    mockAuth.isVendor = false;
    mockAuth.user = { username: 'testuser', displayName: 'Test User' };
  });

  it('renders SidebarMenu in the DOM', () => {
    renderAt('/dashboard');
    expect(screen.getByTestId('sidebar-menu')).toBeTruthy();
  });

  it('renders SidebarMenuTrigger in the DOM', () => {
    renderAt('/dashboard');
    expect(screen.getByTestId('sidebar-trigger')).toBeTruthy();
  });

  it('passes mobileOpen=false initially to SidebarMenu', () => {
    const props = renderAt('/dashboard');
    expect(props.mobileOpen).toBe(false);
  });

  it('passes onMobileClose function to SidebarMenu', () => {
    const props = renderAt('/dashboard');
    expect(typeof props.onMobileClose).toBe('function');
  });
});

describe('DashboardLayout — mobile sidebar toggle', () => {
  beforeEach(() => {
    mockAuth.isCommunityAdmin.mockReturnValue(false);
    mockAuth.isSuperAdmin = false;
    mockAuth.isVendor = false;
    mockAuth.user = { username: 'testuser', displayName: 'Test User' };
    capturedProps = {};
  });

  it('SidebarMenuTrigger click sets mobileOpen to true', () => {
    renderAt('/dashboard');
    const trigger = screen.getByTestId('sidebar-trigger');
    expect(trigger.getAttribute('aria-label')).toBe('Open sidebar');
    fireEvent.click(trigger);
    // capturedProps is updated on re-render
    expect(capturedProps.mobileOpen).toBe(true);
    expect(trigger.getAttribute('aria-label')).toBe('Close sidebar');
  });

  it('second trigger click closes the sidebar', () => {
    renderAt('/dashboard');
    const trigger = screen.getByTestId('sidebar-trigger');
    fireEvent.click(trigger); // open
    fireEvent.click(trigger); // close
    expect(capturedProps.mobileOpen).toBe(false);
  });

  it('onMobileClose callback closes the sidebar when called', () => {
    renderAt('/dashboard');
    const trigger = screen.getByTestId('sidebar-trigger');
    fireEvent.click(trigger); // open
    expect(capturedProps.mobileOpen).toBe(true);
    // Calling onMobileClose triggers a setState — must flush via act
    act(() => {
      capturedProps.onMobileClose();
    });
    expect(capturedProps.mobileOpen).toBe(false);
  });
});
