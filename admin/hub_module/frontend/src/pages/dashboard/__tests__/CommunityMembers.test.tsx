/**
 * Roster page. The legacy `GET /communities/:id/members` is not served; the only served roster
 * is the admin-scoped `GET /admin/:id/members` (camelCase DTO, `totalPages`, exact-match `role`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import CommunityMembers from '../CommunityMembers';
import { isFeatureServed } from '../../../lib/unservedFeatures';
import { httpError, mockApiGet, unexpectedUrl } from '../../../test/api';
import { renderWithProviders } from '../../../test/renderWithProviders';

const auth = vi.hoisted(() => ({ isSuperAdmin: false }));

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'tux' }, isAuthenticated: true, isSuperAdmin: auth.isSuperAdmin, refreshUser: vi.fn() }),
}));

vi.mock('../../../lib/unservedFeatures', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../lib/unservedFeatures')>();
  return { ...actual, isFeatureServed: vi.fn(actual.isFeatureServed) };
});

const me = (role: string, id = 7) => ({
  success: true,
  user: { id: 1, communities: [{ id, name: 'bc', displayName: 'Bar Citizen', role }] },
});

const member = (id: number, over: Record<string, unknown> = {}) => ({
  id,
  userId: `${id}`,
  username: `member${id}`,
  email: `member${id}@example.test`,
  avatarUrl: null,
  role: 'member',
  reputation: { score: 600 },
  joinedAt: '2026-01-05T12:00:00Z',
  ...over,
});

const roster = (members: unknown[], pagination: Record<string, unknown> = {}) => ({
  success: true,
  members,
  pagination: { page: 1, limit: 25, total: members.length, totalPages: 1, ...pagination },
});

function renderPage(route = '/dashboard/community/7/members') {
  return renderWithProviders(<CommunityMembers />, { route, path: '/dashboard/community/:id/members' });
}

describe('CommunityMembers', () => {
  beforeEach(() => {
    auth.isSuperAdmin = false;
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it('lists the roster for an admin from the served admin route, without carrying emails', async () => {
    const get = mockApiGet((url) => {
      if (url === '/api/v1/auth/me') return me('community-admin');
      if (url === '/api/v1/admin/7/members') return roster([member(1, { role: 'community-owner' }), member(2)]);
      return unexpectedUrl(url);
    });
    const { container } = renderPage();
    const rows = await screen.findAllByTestId('member-row');
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText('member1')).toBeInTheDocument();
    expect(within(rows[0]!).getByText('Owner')).toBeInTheDocument();
    expect(within(rows[0]!).getByText('Joined Jan 5, 2026')).toBeInTheDocument();
    expect(screen.getByText('2 members in this community')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/api/v1/admin/7/members', { params: { page: 1, limit: 25 } });
    expect(get.mock.calls.map(([url]) => url)).not.toContain('/api/v1/communities/7/members');
    expect(container.textContent).not.toContain('@example.test');
    // Member profiles are not served yet, so rows must not be links into a dead page.
    expect(within(rows[0]!).queryByRole('link')).not.toBeInTheDocument();
  });

  it('links rows to profiles once hub-api serves them', async () => {
    vi.mocked(isFeatureServed).mockReturnValue(true);
    mockApiGet((url) => (url === '/api/v1/auth/me' ? me('moderator') : roster([member(5)])));
    renderPage();
    const row = await screen.findByTestId('member-row');
    expect(within(row).getByRole('link')).toHaveAttribute('href', '/users/5');
  });

  it('tells a plain member the roster is not available yet, without calling the roster route', async () => {
    const get = mockApiGet((url) => (url === '/api/v1/auth/me' ? me('member') : unexpectedUrl(url)));
    renderPage();
    const state = await screen.findByTestId('not-available-yet');
    expect(state).toHaveAttribute('data-feature', 'member-roster');
    expect(within(state).getByText(/only available to community owners, admins and moderators/i)).toBeInTheDocument();
    expect(get.mock.calls.map(([url]) => url)).toEqual(['/api/v1/auth/me']);
  });

  it('lets a super admin who is not a member see the roster', async () => {
    auth.isSuperAdmin = true;
    mockApiGet((url) => (url === '/api/v1/auth/me' ? me('member', 99) : roster([member(1)])));
    renderPage();
    expect(await screen.findByText('member1')).toBeInTheDocument();
  });

  it('sends one exact role and the search term, and resets to page 1', async () => {
    const get = mockApiGet((url) => (url === '/api/v1/auth/me' ? me('community-admin') : roster([member(1)])));
    renderPage();
    await screen.findByText('member1');
    fireEvent.click(screen.getByRole('button', { name: 'Moderators' }));
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith('/api/v1/admin/7/members', { params: { page: 1, limit: 25, role: 'moderator' } }),
    );
    expect(screen.getByRole('button', { name: 'Moderators' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search by username' }), { target: { value: ' ann ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith('/api/v1/admin/7/members', { params: { page: 1, limit: 25, search: 'ann', role: 'moderator' } }),
    );
  });

  it('pages with the server `totalPages`', async () => {
    const get = mockApiGet((url, params) => {
      if (url === '/api/v1/auth/me') return me('community-owner');
      const page = (params as { page: number }).page;
      return roster([member(page)], { page, total: 50, totalPages: 2 });
    });
    renderPage();
    await screen.findByText('Page 1 of 2');
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByText('Page 2 of 2')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/api/v1/admin/7/members', { params: { page: 2, limit: 25 } });
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('distinguishes an empty community from an empty filter result', async () => {
    mockApiGet((url) => (url === '/api/v1/auth/me' ? me('community-admin') : roster([])));
    renderPage();
    expect(await screen.findByText('No members in this community yet')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'VIPs' }));
    expect(await screen.findByText('No members found matching your filters')).toBeInTheDocument();
  });

  it('shows the server error with a retry instead of an empty roster', async () => {
    let calls = 0;
    mockApiGet((url) => {
      if (url === '/api/v1/auth/me') return me('community-admin');
      calls += 1;
      if (calls === 1) throw httpError(403, { success: false, error: { code: 'FORBIDDEN', message: 'Admin scope required' } });
      return roster([member(1)]);
    });
    renderPage();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Admin scope required')).toBeInTheDocument();
    expect(screen.queryByTestId('members-empty')).not.toBeInTheDocument();
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('member1')).toBeInTheDocument();
  });

  it('surfaces a failure to determine access', async () => {
    mockApiGet(() => {
      throw httpError(500, { error: 'boom' });
    });
    renderPage();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('We could not check your access')).toBeInTheDocument();
  });

  it('rejects an invalid id without querying', () => {
    const get = mockApiGet(() => ({}));
    renderPage('/dashboard/community/abc/members');
    expect(screen.getByText('This members link is not valid.')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
