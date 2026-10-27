import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { describe, it, expect, vi } from 'vitest';

let capturedProps = {};
vi.mock('@penguintechinc/react-libs', () => ({
  SidebarMenu: (props) => {
    capturedProps = props;
    return <div data-testid="sidebar-menu" />;
  },
  SidebarMenuTrigger: ({ onClick, isOpen }) => (
    <button data-testid="sidebar-trigger" aria-label={isOpen ? 'Close sidebar' : 'Open sidebar'} onClick={onClick} />
  ),
}));
vi.mock('../../components/GlobalBanner', () => ({ default: () => null }));
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'admin' }, logout: vi.fn(), isPlatformAdmin: true, isSuperAdmin: false }),
}));

import AdminLayout from '../AdminLayout';

const renderAt = (path) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/admin/:communityId/*" element={<AdminLayout />} />
        <Route path="*" element={<AdminLayout />} />
      </Routes>
    </MemoryRouter>
  );

// jsdom cannot evaluate Tailwind breakpoints; visual confirmation of the <lg drawer
// behaviour is pending a real browser. These tests assert the wiring instead.
describe('AdminLayout — responsive sidebar wiring', () => {
  it('renders the shared SidebarMenu and a hamburger trigger, not a forced-visible <aside>', () => {
    const { container } = renderAt('/platform');
    expect(screen.getByTestId('sidebar-menu')).toBeInTheDocument();
    expect(screen.getByTestId('sidebar-trigger')).toBeInTheDocument();
    expect(container.querySelector('aside')).toBeNull();
  });

  it('toggles mobileOpen via the trigger and closes via onMobileClose', () => {
    renderAt('/platform');
    expect(capturedProps.mobileOpen).toBe(false);
    fireEvent.click(screen.getByTestId('sidebar-trigger'));
    expect(capturedProps.mobileOpen).toBe(true);
    capturedProps.onMobileClose();
  });

  it('main content is offset only at lg+ and padded smaller on mobile', () => {
    const { container } = renderAt('/platform');
    const main = container.querySelector('main');
    expect(main.className).not.toMatch(/(^|\s)(lg:)?ml-64/);
    expect(screen.getByTestId('admin-content-shell').className).toContain('lg:pl-64');
  });

  it('builds a flat category for platform paths', () => {
    renderAt('/platform');
    expect(capturedProps.categories).toHaveLength(1);
    expect(capturedProps.categories[0].items.map((i) => i.href)).toContain('/platform/users');
  });

  it('builds collapsible sections + quick actions for community admin, PRO suffixed', () => {
    renderAt('/admin/42/members');
    const cats = capturedProps.categories;
    expect(cats.find((c) => c.key === 'community').defaultOpen).toBe(true);
    expect(cats.find((c) => c.key === 'quick-actions').items.map((i) => i.href)).toContain('/platform');
    const names = cats.flatMap((c) => c.items.map((i) => i.name));
    expect(names).toContain('Workflows (PRO)');
  });
});
