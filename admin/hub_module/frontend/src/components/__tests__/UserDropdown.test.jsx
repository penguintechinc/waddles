/**
 * Tests for the header user menu: trigger/avatar rendering, conditional
 * Portals and Communities sections, keyboard + outside-click + route-change
 * dismissal, arrow-key navigation, and logout.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';

import UserDropdown from '../UserDropdown';
import { useAuth } from '../../contexts/AuthContext';

vi.mock('../../contexts/AuthContext', () => ({ useAuth: vi.fn() }));

const logout = vi.fn();

function auth(overrides = {}) {
  useAuth.mockReturnValue({
    user: { displayName: 'Alice A', username: 'alice', communities: [] },
    logout,
    isVendor: false,
    isSuperAdmin: false,
    ...overrides,
  });
}

function Nav() {
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <>
      <span data-testid="path">{location.pathname}</span>
      <button onClick={() => navigate('/elsewhere')}>go-elsewhere</button>
    </>
  );
}

function mount() {
  return render(
    <MemoryRouter initialEntries={['/start']}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <UserDropdown />
              <Nav />
              <button>outside</button>
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

const trigger = () => screen.getByRole('button', { name: 'User menu' });
const open = () => fireEvent.click(trigger());
const menu = () => within(screen.getByRole('menu'));

beforeEach(() => {
  vi.clearAllMocks();
  logout.mockResolvedValue(undefined);
  auth();
});

describe('UserDropdown trigger', () => {
  it('is a collapsed menu button showing the username and an initial avatar', () => {
    mount();
    expect(trigger()).toHaveAttribute('aria-haspopup', 'true');
    expect(trigger()).toHaveAttribute('aria-expanded', 'false');
    expect(within(trigger()).getByText('A')).toBeInTheDocument();
    expect(within(trigger()).getByText('alice')).toBeInTheDocument();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('shows the avatar image when the user has one', () => {
    auth({ user: { username: 'alice', avatarUrl: 'https://img/a.png' } });
    mount();
    expect(within(trigger()).getByAltText('alice')).toHaveAttribute('src', 'https://img/a.png');
  });

  it('falls back to the username initial, then to ?', () => {
    auth({ user: { username: 'zed' } });
    const first = mount();
    expect(within(trigger()).getByText('Z')).toBeInTheDocument();
    first.unmount();

    auth({ user: {} });
    mount();
    expect(within(trigger()).getByText('?')).toBeInTheDocument();
  });

  it('toggles open and closed from the trigger', () => {
    mount();
    open();
    expect(trigger()).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('menu')).toBeInTheDocument();
    open();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});

describe('UserDropdown content', () => {
  it('shows the profile block with the @username under a distinct display name', () => {
    mount();
    open();
    expect(menu().getByText('Alice A')).toBeInTheDocument();
    expect(menu().getByText('@alice')).toBeInTheDocument();
    expect(menu().getByRole('menuitem', { name: 'My Profile' })).toHaveAttribute('href', '/dashboard/profile');
    expect(menu().getByRole('menuitem', { name: 'Account Settings' })).toHaveAttribute('href', '/dashboard/settings');
    expect(menu().getByRole('menuitem', { name: 'Connected Platforms' })).toHaveAttribute('href', '/dashboard/my-channels');
    expect(menu().getByRole('menuitem', { name: 'Personal Access Tokens' })).toHaveAttribute('href', '/account/tokens');
  });

  it('shows the @username once when the display name is the username', () => {
    auth({ user: { username: 'alice', communities: [] } });
    mount();
    open();
    expect(menu().getAllByText('@alice')).toHaveLength(1);
    expect(menu().getByText('alice', { selector: 'p' })).toBeInTheDocument();
  });

  it('uses the avatar image in the menu header too', () => {
    auth({ user: { username: 'alice', avatarUrl: 'https://img/a.png' } });
    mount();
    open();
    expect(screen.getAllByAltText('alice')).toHaveLength(2);
  });

  it('labels an anonymous user "Account"', () => {
    auth({ user: undefined });
    mount();
    open();
    expect(menu().getByText('Account')).toBeInTheDocument();
  });

  it('hides Portals when the user has no portal access', () => {
    mount();
    open();
    expect(menu().queryByText('Portals')).not.toBeInTheDocument();
    expect(menu().queryByText('Communities')).not.toBeInTheDocument();
  });

  it('shows every portal the user may enter', () => {
    auth({
      isVendor: true,
      isSuperAdmin: true,
      user: {
        username: 'alice',
        communities: [
          { id: 4, name: 'Members', role: 'member' },
          { id: 9, name: 'Mods', role: 'moderator' },
        ],
      },
    });
    mount();
    open();
    expect(menu().getByText('Portals')).toBeInTheDocument();
    expect(menu().getByRole('menuitem', { name: 'Vendor Portal' })).toHaveAttribute('href', '/vendor/dashboard');
    expect(menu().getByRole('menuitem', { name: 'Admin Panel' })).toHaveAttribute('href', '/admin/9');
    expect(menu().getByRole('menuitem', { name: 'Super Admin' })).toHaveAttribute('href', '/superadmin');
  });

  it('shows only the Super Admin portal for a super admin with no communities', () => {
    auth({ isSuperAdmin: true });
    mount();
    open();
    expect(menu().getByRole('menuitem', { name: 'Super Admin' })).toBeInTheDocument();
    expect(menu().queryByRole('menuitem', { name: 'Vendor Portal' })).not.toBeInTheDocument();
    expect(menu().queryByRole('menuitem', { name: 'Admin Panel' })).not.toBeInTheDocument();
  });

  it('lists up to five communities, with a fallback label and no overflow link', () => {
    auth({
      user: {
        username: 'alice',
        communities: [{ id: 1, name: 'One', role: 'member' }, { id: 2, role: 'member' }],
      },
    });
    mount();
    open();
    expect(menu().getByRole('menuitem', { name: 'One' })).toHaveAttribute('href', '/dashboard/community/1');
    expect(menu().getByRole('menuitem', { name: 'Community 2' })).toBeInTheDocument();
    expect(menu().queryByText('View All Communities')).not.toBeInTheDocument();
  });

  it('caps the list at five and links to the rest', () => {
    auth({
      user: {
        username: 'alice',
        communities: Array.from({ length: 7 }, (_, i) => ({ id: i + 1, name: `C${i + 1}`, role: 'member' })),
      },
    });
    mount();
    open();
    expect(menu().getByRole('menuitem', { name: 'C5' })).toBeInTheDocument();
    expect(menu().queryByRole('menuitem', { name: 'C6' })).not.toBeInTheDocument();
    expect(menu().getByRole('menuitem', { name: 'View All Communities' })).toHaveAttribute('href', '/dashboard');
  });
});

describe('UserDropdown dismissal and navigation', () => {
  it('moves focus to the first menu item when opened', async () => {
    mount();
    open();
    await waitFor(() => expect(menu().getByRole('menuitem', { name: 'My Profile' })).toHaveFocus());
  });

  it('closes on Escape and returns focus to the trigger', async () => {
    mount();
    open();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
  });

  it('closes on an outside click but not on a click inside', () => {
    mount();
    open();
    fireEvent.mouseDown(menu().getByText('Alice A'));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole('button', { name: 'outside' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes when the route changes', () => {
    mount();
    open();
    fireEvent.click(screen.getByRole('button', { name: 'go-elsewhere' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByTestId('path')).toHaveTextContent('/elsewhere');
  });

  it('closes after following a menu link', () => {
    mount();
    open();
    fireEvent.click(menu().getByRole('menuitem', { name: 'Account Settings' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByTestId('path')).toHaveTextContent('/dashboard/settings');
  });

  it('cycles focus with ArrowDown / ArrowUp, wrapping at both ends', async () => {
    mount();
    open();
    const items = menu().getAllByRole('menuitem');
    await waitFor(() => expect(items[0]).toHaveFocus());

    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(items[1]).toHaveFocus();

    fireEvent.keyDown(document, { key: 'ArrowUp' });
    fireEvent.keyDown(document, { key: 'ArrowUp' });
    expect(items[items.length - 1]).toHaveFocus();

    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(items[0]).toHaveFocus();
  });

  it('ignores unrelated keys', () => {
    mount();
    open();
    fireEvent.keyDown(document, { key: 'a' });
    expect(screen.getByRole('menu')).toBeInTheDocument();
  });

  it('stops listening once closed', () => {
    mount();
    open();
    open();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});

describe('UserDropdown logout', () => {
  it('closes the menu, signs out and goes to /login', async () => {
    mount();
    open();
    await act(async () => {
      fireEvent.click(menu().getByRole('menuitem', { name: 'Logout' }));
    });
    expect(logout).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/login'));
  });
});
