/**
 * Join flow on the public community page. hub-api serves join REQUESTS for approval-mode
 * communities (`POST /api/v1/:id/join-requests`, `GET .../mine`); the legacy
 * `POST /communities/:id/join` direct-join does not exist, so open communities show an
 * explicit "not available yet" state instead of a button that 404s.
 */
import { useState, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import CommunityPublicPage from '../CommunityPublicPage';
import { apiClient } from '../../../lib/apiClient';
import { createTestQueryClient } from '../../../test/renderWithProviders';
import { httpError, mockApiGet, ok, unexpectedUrl } from '../../../test/api';

const auth = vi.hoisted(() => ({ isAuthenticated: true }));
const profile = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: auth.isAuthenticated ? { username: 'tux' } : null, isAuthenticated: auth.isAuthenticated }),
}));

vi.mock('../../../services/api', () => ({ publicApi: { getCommunityProfile: profile.get } }));

// react-libs' dist is a directory-import ESM package vitest cannot resolve; mimic the real
// FormModalBuilder contract: await onSubmit, then onClose() (it only logs if onSubmit throws).
vi.mock('@penguintechinc/react-libs', () => ({
  FormModalBuilder: ({
    isOpen,
    onSubmit,
    onClose,
    submitButtonText,
  }: {
    isOpen: boolean;
    onSubmit: (data: { message: string }) => Promise<void>;
    onClose: () => void;
    submitButtonText: string;
  }): ReactNode => {
    const [message, setMessage] = useState('');
    if (!isOpen) return null;
    return (
      <form
        data-testid="join-modal"
        onSubmit={async (event) => {
          event.preventDefault();
          try {
            await onSubmit({ message });
            onClose();
          } catch {
            /* the real lib logs and keeps the modal open */
          }
        }}
      >
        <textarea aria-label="Message" value={message} onChange={(e) => setMessage(e.target.value)} />
        <button type="submit">{submitButtonText}</button>
      </form>
    );
  },
}));

const community = (joinMode: string) => ({
  data: {
    success: true,
    community: {
      id: 7,
      name: 'bc',
      displayName: 'Bar Citizen',
      description: 'Social club',
      memberCount: 10,
      createdAt: '2025-01-01T00:00:00Z',
      joinMode,
    },
  },
});

function renderPage() {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter initialEntries={['/communities/7']}>
        <Routes>
          <Route path="/communities/:id" element={<CommunityPublicPage />} />
          <Route path="/login" element={<div>login page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const mine = (request: unknown) => ({ success: true, request });

describe('CommunityPublicPage join', () => {
  beforeEach(() => {
    auth.isAuthenticated = true;
    profile.get.mockReset();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it('submits a join request to the served route and confirms it', async () => {
    profile.get.mockResolvedValue(community('approval'));
    mockApiGet((url) => (url === '/api/v1/7/join-requests/mine' ? mine(null) : unexpectedUrl(url)));
    const post = vi
      .spyOn(apiClient, 'post')
      .mockResolvedValue(ok({ success: true, request: { id: 3, status: 'pending', created_at: '2026-10-21T00:00:00Z' } }, 201));
    renderPage();

    fireEvent.click(await screen.findByTestId('join-button'));
    fireEvent.change(await screen.findByLabelText('Message'), { target: { value: 'let me in' } });
    fireEvent.click(screen.getByRole('button', { name: 'Submit Request' }));

    expect(await screen.findByText('Your request was sent. Community admins will review it.')).toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith('/api/v1/7/join-requests', { message: 'let me in' });
    expect(screen.queryByTestId('join-modal')).not.toBeInTheDocument();
    expect(post.mock.calls.map(([url]) => url)).not.toContain('/api/v1/communities/7/join');
  });

  it('shows the server reason when the request is refused, and closes the modal', async () => {
    profile.get.mockResolvedValue(community('approval'));
    mockApiGet(() => mine(null));
    vi.spyOn(apiClient, 'post').mockRejectedValue(httpError(409, { success: false, error: { code: 'CONFLICT', message: 'Already a member' } }));
    renderPage();
    fireEvent.click(await screen.findByTestId('join-button'));
    fireEvent.click(await screen.findByRole('button', { name: 'Submit Request' }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('We could not send your request')).toBeInTheDocument();
    expect(within(alert).getByText('Already a member')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId('join-modal')).not.toBeInTheDocument());
  });

  it.each([
    ['pending', 'Request Pending', /pending review/i, true],
    ['rejected', 'Request Again', /declined/i, false],
  ])('reflects an existing %s request', async (status, label, note, disabled) => {
    profile.get.mockResolvedValue(community('approval'));
    mockApiGet(() => mine({ id: 3, status, message: null, created_at: '2026-10-20T00:00:00Z', reviewed_at: null }));
    renderPage();
    const button = await screen.findByRole('button', { name: label });
    expect(button).toHaveProperty('disabled', disabled);
    expect(await screen.findByText(note)).toBeInTheDocument();
  });

  it('offers the dashboard once a request was approved', async () => {
    profile.get.mockResolvedValue(community('approval'));
    mockApiGet(() => mine({ id: 3, status: 'approved', message: null, created_at: '2026-10-20T00:00:00Z', reviewed_at: '2026-10-21T00:00:00Z' }));
    renderPage();
    const link = await screen.findByTestId('open-dashboard');
    expect(link).toHaveAttribute('href', '/dashboard/community/7');
    expect(screen.queryByTestId('join-button')).not.toBeInTheDocument();
  });

  it('flags open communities as not joinable yet instead of calling a dead route', async () => {
    profile.get.mockResolvedValue(community('open'));
    const get = mockApiGet((url) => unexpectedUrl(url));
    const post = vi.spyOn(apiClient, 'post');
    renderPage();
    const state = await screen.findByTestId('not-available-yet');
    expect(state).toHaveAttribute('data-feature', 'community-open-join');
    expect(screen.queryByTestId('join-button')).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });

  it('sends a signed-out visitor to log in instead of posting', async () => {
    auth.isAuthenticated = false;
    profile.get.mockResolvedValue(community('approval'));
    const get = mockApiGet((url) => unexpectedUrl(url));
    const post = vi.spyOn(apiClient, 'post');
    renderPage();
    fireEvent.click(await screen.findByTestId('join-button'));
    expect(await screen.findByText('login page')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });

  it('surfaces a failure to read the existing request', async () => {
    profile.get.mockResolvedValue(community('approval'));
    mockApiGet(() => {
      throw httpError(500, { error: 'status check broke' });
    });
    renderPage();
    expect(await screen.findByText('We could not check your join request')).toBeInTheDocument();
    expect(screen.getByText('status check broke')).toBeInTheDocument();
  });
});
