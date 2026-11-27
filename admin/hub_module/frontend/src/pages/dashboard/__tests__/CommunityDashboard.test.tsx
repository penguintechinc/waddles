/**
 * Community dashboard composed from SERVED routes only, gated on `/auth/me` SCOPES (never role
 * names). Recent activity is served by `GET /communities/:id/activity`; the bot-score panel is
 * not rendered here because hub-api does not serve it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, within } from '@testing-library/react';
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

const ADMIN_SCOPES = ['community:admin', 'provider.connections:manage', 'stream:config.manage', 'community.announcements:read'];

const me = (role: string, scopes: string[]) => ({
  success: true,
  user: {
    id: 1,
    communities: [{ id: '00000007-0000-4000-8000-000000000000', name: 'acme', displayName: 'Acme', role, roleId: null, scopes }],
  },
});

const ACTIVITY = {
  success: true,
  items: [
    { kind: 1, at: '2026-11-20T10:00:00Z', actorId: '0000000a-0000-4000-8000-000000000000', actorName: 'Dana', title: null },
    { kind: 2, at: '2026-11-21T10:00:00Z', actorId: null, actorName: null, title: 'Patch notes' },
  ],
};

const PUBLIC = {
  success: true,
  community: {
    id: '00000007-0000-4000-8000-000000000000',
    name: 'acme',
    displayName: 'Acme HQ',
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
  community_id: '00000007-0000-4000-8000-000000000000',
  title: 'Maintenance window',
  content: 'Server restarts at noon',
  announcement_type: 'important',
  status: 'published',
  is_pinned: true,
  created_by_name: 'admin',
  created_at: '2026-10-20T10:00:00Z',
};

type Handler = (url: string) => unknown;

function serve(scopes: string[], overrides: Handler = () => undefined, role = 'member') {
  return mockApiGet((url) => {
    const custom = overrides(url);
    if (custom !== undefined) return custom;
    if (url === '/api/v1/auth/me') return me(role, scopes);
    if (url === '/api/v1/communities/00000007-0000-4000-8000-000000000000/activity') return ACTIVITY;
    if (url === '/api/v1/public/communities/00000007-0000-4000-8000-000000000000') return PUBLIC;
    if (url === '/api/v1/communities/00000007-0000-4000-8000-000000000000/streams') return { success: true, streams: [STREAM] };
    if (url === '/api/v1/admin/00000007-0000-4000-8000-000000000000/announcements') return { success: true, data: [ANNOUNCEMENT], pagination: {} };
    if (url.startsWith('/api/v1/community/00000007-0000-4000-8000-000000000000/leaderboard/')) {
      return { success: true, leaderboard: [], pagination: { offset: 0, limit: 5, total: 0, has_more: false }, period: 'weekly' };
    }
    return unexpectedUrl(url);
  });
}

function renderPage(route = '/dashboard/community/00000007-0000-4000-8000-000000000000') {
  return renderWithProviders(<CommunityDashboard />, { route, path: '/dashboard/community/:id' });
}

describe('CommunityDashboard', () => {
  beforeEach(() => vi.spyOn(console, 'error').mockImplementation(() => {}));
  afterEach(() => vi.restoreAllMocks());

  it('composes the admin view from served routes, including recent activity', async () => {
    const get = serve(ADMIN_SCOPES, undefined, 'Warden');
    renderPage();

    expect(await screen.findByRole('heading', { level: 1, name: 'Acme HQ' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Admin Panel' })).toHaveAttribute('href', '/admin/00000007-0000-4000-8000-000000000000');
    expect(await screen.findByText('1,234')).toBeInTheDocument();
    expect(await screen.findByText('Maintenance window')).toBeInTheDocument();
    expect(await screen.findByText('penguinzplays')).toBeInTheDocument();
    expect(screen.getByTestId('reputation-panel')).toBeInTheDocument();
    expect(screen.getByTestId('leaderboard-card-watch-time')).toBeInTheDocument();
    expect(screen.getByTestId('leaderboard-card-messages')).toBeInTheDocument();

    expect(screen.queryByTestId('not-available-yet')).not.toBeInTheDocument();
    const items = await screen.findAllByTestId('activity-item');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('Dana joined the community');
    expect(items[1]).toHaveTextContent('Announcement: Patch notes');
    expect(screen.getByText('Your role').previousElementSibling).toHaveTextContent('Warden');

    const urls = get.mock.calls.map(([url]) => url);
    for (const dead of ['/api/v1/communities/00000007-0000-4000-8000-000000000000/dashboard', '/api/v1/admin/00000007-0000-4000-8000-000000000000/bot-score']) {
      expect(urls).not.toContain(dead);
    }
  });

  it('gates admin entry points on scopes, not role names', async () => {
    // A role literally named community-owner with no scopes gets no admin surface...
    serve([], undefined, 'community-owner');
    const first = renderPage();
    await screen.findByRole('heading', { level: 1, name: 'Acme HQ' });
    expect(screen.queryByRole('link', { name: 'Admin Panel' })).not.toBeInTheDocument();
    first.unmount();
    // ...while a plainly named role holding the scopes does.
    serve(ADMIN_SCOPES, undefined, 'Warden');
    renderPage();
    expect(await screen.findByRole('link', { name: 'Admin Panel' })).toBeInTheDocument();
  });

  it('shows a recent-activity error visibly without breaking the page', async () => {
    serve([], (url) => {
      if (url === '/api/v1/communities/00000007-0000-4000-8000-000000000000/activity') throw httpError(500, { error: 'Activity exploded' });
      return undefined;
    });
    renderPage();
    expect(await screen.findByText('We could not load recent activity')).toBeInTheDocument();
    expect(screen.getByText('Activity exploded')).toBeInTheDocument();
    expect(screen.getByTestId('leaderboard-card-watch-time')).toBeInTheDocument();
  });

  it('gives a plain member no admin entry points and no bot-score request', async () => {
    const get = serve([]);
    renderPage();
    await screen.findByRole('heading', { level: 1, name: 'Acme HQ' });
    expect(screen.queryByRole('link', { name: 'Admin Panel' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Manage' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('not-available-yet')).not.toBeInTheDocument();
    expect(await screen.findAllByTestId('activity-item')).toHaveLength(2);
    expect(get.mock.calls.some(([url]) => String(url).includes('bot-score'))).toBe(false);
  });

  it('tells a non-member they are not a member, with a way forward', async () => {
    serve([], (url) => (url === '/api/v1/auth/me' ? { success: true, user: { id: 1, communities: [] } } : undefined));
    renderPage();
    const state = await screen.findByTestId('not-a-member');
    expect(within(state).getByRole('link', { name: 'View community' })).toHaveAttribute('href', '/communities/00000007-0000-4000-8000-000000000000');
  });

  it('shows a retryable error when memberships cannot be loaded', async () => {
    let calls = 0;
    serve([], (url) => {
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
    serve([], (url) => {
      if (url === '/api/v1/public/communities/00000007-0000-4000-8000-000000000000') throw httpError(404, { error: 'Community not found' });
      return undefined;
    });
    renderPage();
    expect(await screen.findByRole('heading', { level: 1, name: 'Acme' })).toBeInTheDocument();
    expect(screen.getByText('Members').previousElementSibling).toHaveTextContent('—');
  });

  it('isolates a live-streams failure: error with retry, rest of the page intact', async () => {
    let calls = 0;
    serve([], (url) => {
      if (url !== '/api/v1/communities/00000007-0000-4000-8000-000000000000/streams') return undefined;
      calls += 1;
      if (calls <= 1) throw httpError(402, { success: false, error: { code: 'FEATURE_NOT_ENABLED', message: 'Live streams are not enabled for this plan' } });
      return { success: true, streams: [STREAM] };
    });
    renderPage();
    const state = await screen.findByTestId('panel-upgrade');
    expect(within(state).getByText('Live streams are not enabled for this plan')).toBeInTheDocument();
    expect(screen.getByTestId('leaderboard-card-watch-time')).toBeInTheDocument();
  });

  it('does not query announcements for plain members, and reports admin failures visibly', async () => {
    const get = serve([]);
    const first = renderPage();
    await screen.findByRole('heading', { level: 1 });
    await act(async () => {});
    expect(get.mock.calls.some(([url]) => url === '/api/v1/admin/00000007-0000-4000-8000-000000000000/announcements')).toBe(false);
    first.unmount();

    serve(ADMIN_SCOPES, (url) => {
      if (url === '/api/v1/admin/00000007-0000-4000-8000-000000000000/announcements') throw httpError(500, { error: 'Announcements exploded' });
      return undefined;
    });
    renderPage();
    expect(await screen.findByText('We could not load Announcements')).toBeInTheDocument();
    expect(screen.getByText('Announcements exploded')).toBeInTheDocument();
  });

  it('rejects an invalid id without querying', () => {
    const get = serve([]);
    renderPage('/dashboard/community/abc');
    expect(screen.getByText('This community link is not valid.')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
