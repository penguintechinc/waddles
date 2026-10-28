/**
 * Community dashboard composed from SERVED routes only. The legacy single
 * `GET /communities/:id/dashboard` (+ bot-score, recent activity) is not served by hub-api;
 * those panels show an explicit "not available yet" state rather than a blank/failed page.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import CommunityDashboard from '../CommunityDashboard';
import { httpError, mockApiGet, unexpectedUrl } from '../../../test/api';
import { renderWithProviders } from '../../../test/renderWithProviders';

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'tux' }, isAuthenticated: true, isSuperAdmin: false, refreshUser: vi.fn() }),
}));

// Reputation panel talks to the legacy axios instance and has its own tests.
vi.mock('../../../components/dashboard/CommunityReputationPanel', () => ({
  default: () => <div data-testid="reputation-panel" />,
}));

const me = (role: string) => ({
  success: true,
  user: { id: 1, communities: [{ id: 7, name: 'bar-citizen', displayName: 'Bar Citizen', role }] },
});

const PUBLIC = {
  success: true,
  community: {
    id: 7,
    name: 'bar-citizen',
    displayName: 'Bar Citizen HQ',
    description: 'd',
    logoUrl: null,
    bannerUrl: null,
    platform: 'discord',
    memberCount: 1234,
    joinMode: 'open',
    createdAt: '2025-01-01T00:00:00Z',
  },
};

const STREAM = {
  entityId: 'twitch-1',
  platform: 'twitch',
  channelId: '1',
  channelName: 'penguinzplays',
  isLive: true,
  liveSince: '2026-10-21T10:00:00Z',
  viewerCount: 42,
  title: 'Friday night',
  game: 'Star Citizen',
  thumbnailUrl: '',
};

const ANNOUNCEMENT = {
  id: 1,
  community_id: 7,
  title: 'Maintenance window',
  content: 'Server restarts at noon',
  announcement_type: 'important',
  status: 'published',
  is_pinned: true,
  created_by_name: 'admin',
  created_at: '2026-10-20T10:00:00Z',
};

type Handler = (url: string) => unknown;

function serve(role: string, overrides: Handler = () => undefined) {
  return mockApiGet((url) => {
    const custom = overrides(url);
    if (custom !== undefined) return custom;
    if (url === '/api/v1/auth/me') return me(role);
    if (url === '/api/v1/public/communities/7') return PUBLIC;
    if (url === '/api/v1/communities/7/streams') return { success: true, streams: [STREAM] };
    if (url === '/api/v1/admin/7/announcements') return { success: true, data: [ANNOUNCEMENT], pagination: {} };
    if (url.startsWith('/api/v1/community/7/leaderboard/')) {
      return { success: true, leaderboard: [], pagination: { offset: 0, limit: 5, total: 0, has_more: false }, period: 'weekly' };
    }
    return unexpectedUrl(url);
  });
}

function renderPage(route = '/dashboard/community/7') {
  return renderWithProviders(<CommunityDashboard />, { route, path: '/dashboard/community/:id' });
}

describe('CommunityDashboard', () => {
  beforeEach(() => vi.spyOn(console, 'error').mockImplementation(() => {}));
  afterEach(() => vi.restoreAllMocks());

  it('composes the admin view from served routes and flags the panels that have none', async () => {
    const get = serve('community-admin');
    renderPage();

    expect(await screen.findByRole('heading', { level: 1, name: 'Bar Citizen HQ' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Admin Panel' })).toHaveAttribute('href', '/admin/7');
    expect(await screen.findByText('1,234')).toBeInTheDocument();
    expect(await screen.findByText('Maintenance window')).toBeInTheDocument();
    expect(await screen.findByText('penguinzplays')).toBeInTheDocument();
    expect(screen.getByTestId('reputation-panel')).toBeInTheDocument();
    expect(screen.getByTestId('leaderboard-card-watch-time')).toBeInTheDocument();
    expect(screen.getByTestId('leaderboard-card-messages')).toBeInTheDocument();

    const gated = screen.getAllByTestId('not-available-yet').map((el) => el.getAttribute('data-feature'));
    expect(gated.sort()).toEqual(['bot-score', 'community-recent-activity']);

    const urls = get.mock.calls.map(([url]) => url);
    for (const dead of ['/api/v1/communities/7/dashboard', '/api/v1/admin/7/bot-score', '/api/v1/communities/7/activity']) {
      expect(urls).not.toContain(dead);
    }
  });

  it('gives a plain member no admin entry points and no bot-score panel', async () => {
    serve('member');
    renderPage();
    await screen.findByRole('heading', { level: 1, name: 'Bar Citizen HQ' });
    expect(screen.queryByRole('link', { name: 'Admin Panel' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Manage' })).not.toBeInTheDocument();
    expect(screen.getAllByTestId('not-available-yet').map((el) => el.getAttribute('data-feature'))).toEqual(['community-recent-activity']);
  });

  it('tells a non-member they are not a member, with a way forward', async () => {
    serve('member', (url) => (url === '/api/v1/auth/me' ? { success: true, user: { id: 1, communities: [] } } : undefined));
    renderPage();
    const state = await screen.findByTestId('not-a-member');
    expect(within(state).getByRole('link', { name: 'View community' })).toHaveAttribute('href', '/communities/7');
  });

  it('shows a retryable error when memberships cannot be loaded', async () => {
    let calls = 0;
    serve('member', (url) => {
      if (url !== '/api/v1/auth/me') return undefined;
      calls += 1;
      if (calls === 1) throw httpError(500, { error: 'Database unavailable' });
      return undefined;
    });
    renderPage();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Database unavailable')).toBeInTheDocument();
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('heading', { level: 1 })).toBeInTheDocument();
  });

  it('still renders from /auth/me when the public record is unavailable', async () => {
    serve('member', (url) => {
      if (url === '/api/v1/public/communities/7') throw httpError(404, { error: 'Community not found' });
      return undefined;
    });
    renderPage();
    expect(await screen.findByRole('heading', { level: 1, name: 'Bar Citizen' })).toBeInTheDocument();
    expect(screen.getByText('Members').previousElementSibling).toHaveTextContent('—');
  });

  it('isolates a live-streams failure: error with retry, rest of the page intact', async () => {
    let calls = 0;
    serve('member', (url) => {
      if (url !== '/api/v1/communities/7/streams') return undefined;
      calls += 1;
      if (calls <= 1) throw httpError(402, { success: false, error: { code: 'FEATURE_NOT_ENABLED', message: 'Live streams are not enabled for this plan' } });
      return { success: true, streams: [STREAM] };
    });
    renderPage();
    const state = await screen.findByTestId('streams-error');
    expect(within(state).getByText('Live streams are not enabled for this plan')).toBeInTheDocument();
    expect(screen.getByTestId('leaderboard-card-watch-time')).toBeInTheDocument();
    fireEvent.click(within(state).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('penguinzplays')).toBeInTheDocument();
  });

  it('hides announcements on an expected denial but reports unexpected failures', async () => {
    const get = serve('member', (url) => {
      if (url === '/api/v1/admin/7/announcements') throw httpError(403, { error: 'no' });
      return undefined;
    });
    const first = renderPage();
    await screen.findByRole('heading', { level: 1 });
    await waitFor(() => expect(get.mock.calls.some(([url]) => url === '/api/v1/admin/7/announcements')).toBe(true));
    await act(async () => {});
    expect(screen.queryByText('We could not load announcements')).not.toBeInTheDocument();
    expect(screen.queryByTestId('announcement')).not.toBeInTheDocument();
    first.unmount();

    serve('member', (url) => {
      if (url === '/api/v1/admin/7/announcements') throw httpError(500, { error: 'Announcements exploded' });
      return undefined;
    });
    renderPage();
    expect(await screen.findByText('We could not load announcements')).toBeInTheDocument();
    expect(screen.getByText('Announcements exploded')).toBeInTheDocument();
  });

  it('rejects an invalid id without querying', () => {
    const get = serve('member');
    renderPage('/dashboard/community/abc');
    expect(screen.getByText('This community link is not valid.')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
