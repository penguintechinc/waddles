/**
 * Admin join-request queue. The legacy `joinRequestApi` called UNPREFIXED paths
 * (`/admin/:id/join-requests`, no `/api/v1`) that the SPA host answers with index.html, and POSTed
 * approve/reject where hub-api serves PUT. These tests pin the served routes + methods.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import AdminJoinRequests from '../AdminJoinRequests';
import { apiClient } from '../../../lib/apiClient';
import { httpError, mockApiGet, ok, unexpectedUrl } from '../../../test/api';
import { renderWithProviders } from '../../../test/renderWithProviders';

const row = (id: number, over: Record<string, unknown> = {}) => ({
  id,
  status: 'pending',
  message: `please ${id}`,
  created_at: '2026-10-20T10:00:00Z',
  reviewed_at: null,
  username: `applicant${id}`,
  email: `a${id}@example.test`,
  avatarUrl: null,
  ...over,
});

const list = (rows: unknown[]) => ({
  success: true,
  requests: rows,
  pagination: { total: rows.length, page: 1, limit: 25, totalPages: 1 },
});

function renderPage(route = '/admin/7/join-requests') {
  return renderWithProviders(<AdminJoinRequests />, { route, path: '/admin/:communityId/join-requests' });
}

describe('AdminJoinRequests', () => {
  beforeEach(() => vi.spyOn(console, 'error').mockImplementation(() => {}));
  afterEach(() => vi.restoreAllMocks());

  it('lists pending requests from the served admin route', async () => {
    const get = mockApiGet((url) => (url === '/api/v1/admin/7/join-requests' ? list([row(1), row(2, { message: null })]) : unexpectedUrl(url)));
    renderPage();
    expect(await screen.findByText('applicant1')).toBeInTheDocument();
    expect(screen.getByText('please 1')).toBeInTheDocument();
    expect(screen.getByText('No message')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/api/v1/admin/7/join-requests', { params: { status: 'pending', page: 1, limit: 25 } });
  });

  it('approves with PUT to the served route, then refreshes the queue', async () => {
    let remaining = [row(1), row(2)];
    mockApiGet(() => list(remaining));
    const put = vi.spyOn(apiClient, 'put').mockImplementation(async () => {
      remaining = [row(2)];
      return ok({ success: true });
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Approve applicant1' }));
    await waitFor(() => expect(screen.queryByText('applicant1')).not.toBeInTheDocument());
    expect(put).toHaveBeenCalledWith('/api/v1/admin/7/join-requests/1/approve');
    expect(screen.getByText('applicant2')).toBeInTheDocument();
  });

  it('rejects with PUT to the served reject route', async () => {
    mockApiGet(() => list([row(4)]));
    const put = vi.spyOn(apiClient, 'put').mockResolvedValue(ok({ success: true }));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Reject applicant4' }));
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/v1/admin/7/join-requests/4/reject'));
  });

  it('shows the server reason when a decision fails, and lets the admin dismiss it', async () => {
    mockApiGet(() => list([row(1)]));
    vi.spyOn(apiClient, 'put').mockRejectedValue(httpError(404, { success: false, error: { code: 'NOT_FOUND', message: 'Join request not found' } }));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Approve applicant1' }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Join request not found')).toBeInTheDocument();
    expect(screen.getByText('applicant1')).toBeInTheDocument();
    fireEvent.click(within(alert).getByRole('button', { name: 'Dismiss message' }));
    await waitFor(() => expect(screen.queryByText('Join request not found')).not.toBeInTheDocument());
  });

  it('shows the empty state when nothing is pending', async () => {
    mockApiGet(() => list([]));
    renderPage();
    expect(await screen.findByText('No pending join requests')).toBeInTheDocument();
  });

  it('shows a retryable error instead of an empty queue when loading fails', async () => {
    let calls = 0;
    mockApiGet(() => {
      calls += 1;
      if (calls === 1) throw httpError(403, { success: false, error: { code: 'FORBIDDEN', message: 'Admin role required' } });
      return list([row(1)]);
    });
    renderPage();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Admin role required')).toBeInTheDocument();
    expect(screen.queryByText('No pending join requests')).not.toBeInTheDocument();
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('applicant1')).toBeInTheDocument();
  });

  it('rejects an invalid community id without querying', () => {
    const get = mockApiGet(() => ({}));
    renderPage('/admin/abc/join-requests');
    expect(screen.getByText('This join requests link is not valid.')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
