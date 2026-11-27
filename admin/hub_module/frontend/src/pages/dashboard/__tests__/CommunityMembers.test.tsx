/**
 * Roster page, gated on `/auth/me` scopes (never role names): `community.members:view` to see
 * it, `members:roles.manage` to change a role, `members:remove` to remove a member. Roster,
 * roles and the two mutations are all hub-api routes with UUID-only wire shapes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import CommunityMembers from '../CommunityMembers';
import { apiClient } from '../../../lib/apiClient';
import { isFeatureServed } from '../../../lib/unservedFeatures';
import { httpError, mockApiGet, ok, unexpectedUrl } from '../../../test/api';
import { renderWithProviders } from '../../../test/renderWithProviders';

const auth = vi.hoisted(() => ({ isSuperAdmin: false }));

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'tux' }, isAuthenticated: true, isSuperAdmin: auth.isSuperAdmin, refreshUser: vi.fn() }),
}));

vi.mock('../../../lib/unservedFeatures', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../lib/unservedFeatures')>();
  return { ...actual, isFeatureServed: vi.fn(actual.isFeatureServed) };
});

const CID = '00000007-0000-4000-8000-000000000000';
const ROLE_ADMIN = '000000a1-0000-4000-8000-000000000000';
const ROLE_MEMBER = '000000a2-0000-4000-8000-000000000000';
const uid = (n: number) => `0000000${n}-0000-4000-8000-0000000000${n}0`;

const VIEW = ['community.members:view'];
const ALL = [...VIEW, 'members:roles.manage', 'members:remove'];

const me = (scopes: string[], id = CID) => ({
  success: true,
  user: { id: 1, communities: [{ id, name: 'acme', displayName: 'Acme', role: 'Warden', roleId: ROLE_ADMIN, scopes }] },
});

const ROLES = {
  success: true,
  roles: [
    { id: ROLE_ADMIN, name: 'community-admin', displayName: 'Admins', roleKind: 1, priority: 90, memberCount: 1 },
    { id: ROLE_MEMBER, name: 'member', displayName: 'Members', roleKind: 3, priority: 10, memberCount: 2 },
  ],
};

const member = (n: number, over: Record<string, unknown> = {}) => ({
  userId: uid(n),
  username: `member${n}`,
  avatarUrl: null,
  roleId: ROLE_MEMBER,
  roleName: 'Members',
  reputation: { score: 600 },
  joinedAt: '2026-01-05T12:00:00Z',
  ...over,
});

const roster = (members: unknown[], pagination: Record<string, unknown> = {}) => ({
  success: true,
  members,
  pagination: { page: 1, limit: 25, total: members.length, totalPages: 1, ...pagination },
});

/** Serves auth/me, roles and a roster; `rosterBody` may inspect the request params. */
function serve(scopes: string[], rosterBody: (params: unknown) => unknown = () => roster([member(1), member(2)])) {
  return mockApiGet((url, params) => {
    if (url === '/api/v1/auth/me') return me(scopes);
    if (url === `/api/v1/admin/${CID}/roles`) return ROLES;
    if (url === `/api/v1/admin/${CID}/members`) return rosterBody(params);
    return unexpectedUrl(url);
  });
}

function renderPage(route = `/dashboard/community/${CID}/members`) {
  return renderWithProviders(<CommunityMembers />, { route, path: '/dashboard/community/:id/members' });
}

describe('CommunityMembers', () => {
  beforeEach(() => {
    auth.isSuperAdmin = false;
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it('lists the roster for a viewer from the served admin route, with no ids or emails shown', async () => {
    const get = serve(VIEW, () => roster([member(1, { roleId: ROLE_ADMIN, roleName: 'Admins', email: 'x@example.test' }), member(2)]));
    const { container } = renderPage();
    const rows = await screen.findAllByTestId('member-row');
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText('member1')).toBeInTheDocument();
    expect(within(rows[0]!).getByText('Admins')).toBeInTheDocument();
    expect(within(rows[0]!).getByText('Joined Jan 5, 2026')).toBeInTheDocument();
    expect(screen.getByText('2 members in this community')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(`/api/v1/admin/${CID}/members`, { params: { page: 1, limit: 25 } });
    expect(container.textContent).not.toContain('@example.test');
    // View-only: no role selects, no remove buttons.
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Remove/ })).not.toBeInTheDocument();
    expect(within(rows[0]!).queryByRole('link')).not.toBeInTheDocument();
  });

  it('links rows to profiles once hub-api serves them', async () => {
    vi.mocked(isFeatureServed).mockReturnValue(true);
    serve(VIEW, () => roster([member(5)]));
    renderPage();
    const row = await screen.findByTestId('member-row');
    expect(within(row).getByRole('link')).toHaveAttribute('href', `/users/${uid(5)}`);
  });

  it('tells a caller without community.members:view so, honestly, without calling the roster or roles routes', async () => {
    // Role NAME is irrelevant: only scopes count.
    const get = serve(['members:remove']);
    renderPage();
    expect(await screen.findByTestId('members-forbidden')).toHaveTextContent(/do not have permission/i);
    expect(get.mock.calls.map(([url]) => url)).toEqual(['/api/v1/auth/me']);
  });

  it('lets a super admin who is not a member see the roster and every action', async () => {
    auth.isSuperAdmin = true;
    mockApiGet((url) => {
      if (url === '/api/v1/auth/me') return me([], '00000063-0000-4000-8000-000000000000');
      if (url === `/api/v1/admin/${CID}/roles`) return ROLES;
      return roster([member(1)]);
    });
    renderPage();
    expect(await screen.findByText('member1')).toBeInTheDocument();
    expect(await screen.findByRole('combobox', { name: 'Role for member1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove member1' })).toBeInTheDocument();
  });

  it('filters by one role id (from the roles endpoint) and the search term, resetting to page 1', async () => {
    const get = serve(VIEW);
    renderPage();
    await screen.findByText('member1');
    fireEvent.click(await screen.findByRole('button', { name: 'Admins' }));
    await waitFor(() => expect(get).toHaveBeenCalledWith(`/api/v1/admin/${CID}/members`, { params: { page: 1, limit: 25, roleId: ROLE_ADMIN } }));
    expect(screen.getByRole('button', { name: 'Admins' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search by username' }), { target: { value: ' ann ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith(`/api/v1/admin/${CID}/members`, { params: { page: 1, limit: 25, search: 'ann', roleId: ROLE_ADMIN } }),
    );
  });

  it('pages with the server `totalPages`', async () => {
    const get = serve(VIEW, (params) => {
      const page = (params as { page: number }).page;
      return roster([member(page)], { page, total: 50, totalPages: 2 });
    });
    renderPage();
    await screen.findByText('Page 1 of 2');
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByText('Page 2 of 2')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(`/api/v1/admin/${CID}/members`, { params: { page: 2, limit: 25 } });
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('distinguishes an empty community from an empty filter result', async () => {
    serve(VIEW, () => roster([]));
    renderPage();
    expect(await screen.findByText('No members in this community yet')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Members' }));
    expect(await screen.findByText('No members found matching your filters')).toBeInTheDocument();
  });

  it('shows the server error with a retry instead of an empty roster', async () => {
    let calls = 0;
    serve(VIEW, () => {
      calls += 1;
      if (calls === 1) throw httpError(403, { success: false, error: { code: 'FORBIDDEN', message: 'Members scope required' } });
      return roster([member(1)]);
    });
    renderPage();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Members scope required')).toBeInTheDocument();
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

  describe('actions', () => {
    it('changes a role with PUT { roleId } only when members:roles.manage is held, then refetches', async () => {
      const get = serve([...VIEW, 'members:roles.manage']);
      const put = vi.spyOn(apiClient, 'put').mockResolvedValue(ok({ success: true }));
      renderPage();
      const select = await screen.findByRole('combobox', { name: 'Role for member1' });
      expect(screen.queryByRole('button', { name: /^Remove/ })).not.toBeInTheDocument();
      const before = get.mock.calls.filter(([url]) => url === `/api/v1/admin/${CID}/members`).length;
      fireEvent.change(select, { target: { value: ROLE_ADMIN } });
      await waitFor(() => expect(put).toHaveBeenCalledWith(`/api/v1/admin/${CID}/members/${uid(1)}/role`, { roleId: ROLE_ADMIN }));
      await waitFor(() => expect(get.mock.calls.filter(([url]) => url === `/api/v1/admin/${CID}/members`).length).toBeGreaterThan(before));
    });

    it('shows the server message when a role change is refused (last admin)', async () => {
      serve(ALL);
      vi.spyOn(apiClient, 'put').mockRejectedValue(httpError(409, { success: false, error: { code: 'LAST_ADMIN', message: 'Cannot move the last admin' } }));
      renderPage();
      fireEvent.change(await screen.findByRole('combobox', { name: 'Role for member1' }), { target: { value: ROLE_ADMIN } });
      expect(await screen.findByTestId('member-action-error')).toHaveTextContent('Cannot move the last admin');
    });

    it('removes a member with DELETE after a confirm, only when members:remove is held', async () => {
      serve([...VIEW, 'members:remove']);
      const del = vi.spyOn(apiClient, 'delete').mockResolvedValue(ok({ success: true }));
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
      renderPage();
      expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
      fireEvent.click(await screen.findByRole('button', { name: 'Remove member2' }));
      expect(confirm).toHaveBeenCalled();
      await waitFor(() => expect(del).toHaveBeenCalledWith(`/api/v1/admin/${CID}/members/${uid(2)}`));
    });

    it('does not delete when the confirm is declined', async () => {
      serve(ALL);
      const del = vi.spyOn(apiClient, 'delete').mockResolvedValue(ok({}));
      vi.spyOn(window, 'confirm').mockReturnValue(false);
      renderPage();
      fireEvent.click(await screen.findByRole('button', { name: 'Remove member1' }));
      expect(del).not.toHaveBeenCalled();
    });

    it('shows the server message when a removal is refused', async () => {
      serve(ALL);
      vi.spyOn(window, 'confirm').mockReturnValue(true);
      vi.spyOn(apiClient, 'delete').mockRejectedValue(httpError(403, { success: false, error: { code: 'FORBIDDEN', message: 'You cannot remove the owner' } }));
      renderPage();
      fireEvent.click(await screen.findByRole('button', { name: 'Remove member1' }));
      expect(await screen.findByTestId('member-action-error')).toHaveTextContent('You cannot remove the owner');
    });
  });
});
