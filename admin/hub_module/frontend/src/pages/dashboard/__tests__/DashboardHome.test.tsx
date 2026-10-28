/**
 * Post-login landing: "My Communities" must populate from the SERVED `/auth/me`
 * memberships (hub-api has no `/communities/my`), and loading / error / empty /
 * contract-drift must each render a real state instead of a silently empty page.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import DashboardHome from '../DashboardHome';
import { httpError, mockApiGet, unexpectedUrl } from '../../../test/api';
import { renderWithProviders } from '../../../test/renderWithProviders';

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'tux' }, isAuthenticated: true, isSuperAdmin: false, refreshUser: vi.fn() }),
}));

const ME_TWO = {
  success: true,
  user: {
    id: 1,
    communities: [
      { id: 7, name: 'bar-citizen', displayName: 'Bar Citizen', role: 'community-owner' },
      { id: 9, name: 'penguins', displayName: 'Penguin Pals', role: 'member' },
    ],
  },
};

const PUBLIC_7 = {
  success: true,
  community: {
    id: 7,
    name: 'bar-citizen',
    displayName: 'Bar Citizen',
    description: 'Star Citizen social club',
    logoUrl: 'https://cdn.example/bc.png',
    bannerUrl: null,
    platform: 'discord',
    memberCount: 1234,
    joinMode: 'open',
    createdAt: '2025-01-01T00:00:00Z',
  },
};

function renderLanding() {
  return renderWithProviders(<DashboardHome />, { route: '/dashboard', path: '/dashboard' });
}

describe('DashboardHome (post-login landing)', () => {
  beforeEach(() => vi.spyOn(console, 'error').mockImplementation(() => {}));
  afterEach(() => vi.restoreAllMocks());

  it('populates My Communities from /auth/me and enriches with public details', async () => {
    const get = mockApiGet((url) => {
      if (url === '/api/v1/auth/me') return ME_TWO;
      if (url === '/api/v1/public/communities/7') return PUBLIC_7;
      if (url === '/api/v1/public/communities/9') throw httpError(404, { error: 'Community not found' });
      return unexpectedUrl(url);
    });
    renderLanding();

    const grid = await screen.findByTestId('communities-grid');
    const cards = within(grid).getAllByTestId('community-card');
    expect(cards).toHaveLength(2);
    expect(screen.getByRole('heading', { level: 1, name: 'Welcome back, tux!' })).toBeInTheDocument();
    expect(cards[0]).toHaveAttribute('href', '/dashboard/community/7');
    expect(within(cards[0]!).getByText('Owner')).toBeInTheDocument();
    expect(within(cards[1]!).getByText('Penguin Pals')).toBeInTheDocument();
    expect(within(cards[1]!).getByText('Member')).toBeInTheDocument();
    // Public details decorate the card that has them; the other still renders from /auth/me alone.
    expect(await within(cards[0]!).findByText('Star Citizen social club')).toBeInTheDocument();
    expect(within(cards[0]!).getByText('1,234 members')).toBeInTheDocument();
    expect(within(cards[1]!).queryByText(/members$/)).not.toBeInTheDocument();
    // The dead legacy route is never called.
    expect(get.mock.calls.map(([url]) => url)).not.toContain('/api/v1/communities/my');
  });

  it('shows a loading state, then the empty state with a way forward when the user has no communities', async () => {
    mockApiGet((url) => (url === '/api/v1/auth/me' ? { success: true, user: { id: 1, communities: [] } } : unexpectedUrl(url)));
    renderLanding();
    expect(screen.getByTestId('loading-state')).toBeInTheDocument();
    const empty = await screen.findByTestId('no-communities');
    expect(within(empty).getByText('No Communities Yet')).toBeInTheDocument();
    expect(within(empty).getByTestId('browse-communities-btn')).toHaveAttribute('href', '/communities');
    expect(screen.queryByTestId('communities-grid')).not.toBeInTheDocument();
  });

  it('surfaces a server failure with the server message and retries', async () => {
    let calls = 0;
    mockApiGet((url) => {
      if (url !== '/api/v1/auth/me') return unexpectedUrl(url);
      calls += 1;
      if (calls === 1) throw httpError(500, { error: { code: 'INTERNAL', message: 'Database unavailable' } });
      return { success: true, user: { id: 1, communities: [{ id: 7, name: 'bc', displayName: 'Bar Citizen', role: 'member' }] } };
    });
    renderLanding();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('We could not load your communities')).toBeInTheDocument();
    expect(within(alert).getByText('Database unavailable')).toBeInTheDocument();
    // The failed attempt must not masquerade as the empty state.
    expect(screen.queryByTestId('no-communities')).not.toBeInTheDocument();

    mockApiGet((url) => (url === '/api/v1/auth/me' ? ME_TWO : throwNotFound(url)));
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByTestId('communities-grid')).toBeInTheDocument();
  });

  it('reports an expired session instead of rendering an empty list', async () => {
    mockApiGet((url) => (url === '/api/v1/auth/me' ? { success: true, user: null } : unexpectedUrl(url)));
    renderLanding();
    expect(await screen.findByText('Your session has expired. Please sign in again.')).toBeInTheDocument();
    expect(screen.queryByTestId('no-communities')).not.toBeInTheDocument();
  });

  it('turns contract drift into a visible error, not a blank page', async () => {
    mockApiGet((url) => (url === '/api/v1/auth/me' ? { success: true, user: { id: 1, communities: 'nope' } } : unexpectedUrl(url)));
    renderLanding();
    expect(await screen.findByText(/Unexpected response from the server \(auth\/me user: "communities" is not a list\)/)).toBeInTheDocument();
  });

  it('filters own communities locally and discovers public ones the user has not joined', async () => {
    const get = mockApiGet((url, params) => {
      if (url === '/api/v1/auth/me') return ME_TWO;
      if (url.startsWith('/api/v1/public/communities/')) throw httpError(404);
      if (url === '/api/v1/public/communities') {
        expect(params).toEqual({ search: 'pen', limit: 6 });
        return {
          success: true,
          communities: [
            { id: 9, name: 'penguins', displayName: 'Penguin Pals', memberCount: 3 },
            { id: 12, name: 'pen-testers', displayName: 'Pen Testers', description: 'Security folks', platform: 'twitch', memberCount: 40 },
          ],
          pagination: { page: 1, limit: 6, total: 2, totalPages: 1 },
        };
      }
      return unexpectedUrl(url);
    });
    renderLanding();
    await screen.findByTestId('communities-grid');

    fireEvent.change(screen.getByTestId('dashboard-search'), { target: { value: 'pen' } });
    const discover = await screen.findByTestId('discover-grid');
    // Already-joined "Penguin Pals" is excluded from discovery; the unjoined one is offered.
    expect(within(discover).getAllByTestId('discover-card')).toHaveLength(1);
    expect(within(discover).getByText('Pen Testers')).toBeInTheDocument();
    expect(within(discover).getByTestId('discover-card')).toHaveAttribute('href', '/communities/12');
    // Own list is filtered locally: only the matching community remains.
    const own = screen.getByTestId('communities-grid');
    expect(within(own).getAllByTestId('community-card')).toHaveLength(1);
    expect(within(own).getByText('Penguin Pals')).toBeInTheDocument();
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/v1/public/communities', { params: { search: 'pen', limit: 6 } }));
  });

  it('says so when nothing public matches the search', async () => {
    mockApiGet((url) => {
      if (url === '/api/v1/auth/me') return ME_TWO;
      if (url.startsWith('/api/v1/public/communities/')) throw httpError(404);
      if (url === '/api/v1/public/communities') return { success: true, communities: [], pagination: { page: 1, limit: 6, total: 0, totalPages: 0 } };
      return unexpectedUrl(url);
    });
    renderLanding();
    await screen.findByTestId('communities-grid');
    fireEvent.change(screen.getByTestId('dashboard-search'), { target: { value: 'zzz' } });
    expect(await screen.findByText('No public communities found for "zzz"')).toBeInTheDocument();
    expect(screen.getByTestId('no-matching-communities')).toBeInTheDocument();
  });
});

/** A 404 for any URL a retry test does not care about. */
function throwNotFound(_url: string): never {
  throw httpError(404);
}
