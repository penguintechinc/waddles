/** PrivacyRights "Submit a privacy request" section: list, submit, disabled, error. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../hooks/useCookieConsent', () => ({
  useCookieConsent: () => ({ consent: {}, setDoNotSell: vi.fn(), loading: false }),
}));
const auth = vi.hoisted(() => ({ isAuthenticated: true, loading: false }));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => auth }));
vi.mock('../../services/api', () => ({ default: { get: vi.fn(), delete: vi.fn() } }));
vi.mock('../../services/userPrivacyRequestsApi', () => ({
  userPrivacyRequestsApi: { list: vi.fn(), create: vi.fn() },
}));
import { userPrivacyRequestsApi as api } from '../../services/userPrivacyRequestsApi';
import PrivacyRights from '../PrivacyRights';

const row = { id: 'r1', requestType: 2, status: 3, regime: 1, requestedAt: '2026-11-01T10:00:00Z', dueAt: '2026-12-01T10:00:00Z', completedAt: null };
const mount = () => render(<MemoryRouter><PrivacyRights /></MemoryRouter>);

beforeEach(() => {
  vi.clearAllMocks();
  auth.isAuthenticated = true;
  auth.loading = false;
});

describe('PrivacyRights while signed out (regression: 401 privacy-requests + 400 auth/refresh)', () => {
  it("never fetches the user's requests; shows sign-in prompts and the public do-not-sell control", async () => {
    auth.isAuthenticated = false;
    mount();
    expect(await screen.findByTestId('requests-signin')).toBeTruthy();
    expect(screen.getByTestId('export-signin')).toBeTruthy();
    expect(screen.getByTestId('delete-signin')).toBeTruthy();
    expect(screen.getByTestId('toggle-do-not-sell')).toBeTruthy();
    expect(screen.queryByTestId('export-data')).toBeNull();
    expect(api.list).not.toHaveBeenCalled();
  });

  it('waits for the auth check before fetching', () => {
    auth.isAuthenticated = false;
    auth.loading = true;
    mount();
    expect(api.list).not.toHaveBeenCalled();
    expect(screen.queryByTestId('requests-signin')).toBeNull();
  });
});

describe('PrivacyRights requests section', () => {
  it('lists own requests with status and due date', async () => {
    api.list.mockResolvedValue([row]);
    mount();
    const list = await screen.findByTestId('requests-list');
    expect(list.textContent).toMatch(/Erasure/);
    expect(list.textContent).toMatch(/Verified/);
    expect(list.textContent).toMatch(/Due /);
  });

  it('shows the empty state', async () => {
    api.list.mockResolvedValue([]);
    mount();
    expect(await screen.findByTestId('requests-empty')).toBeTruthy();
  });

  it('submits a request and reloads', async () => {
    api.list.mockResolvedValue([]);
    api.create.mockResolvedValue(row);
    mount();
    await screen.findByTestId('requests-empty');
    fireEvent.change(screen.getByLabelText('Request type'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('Privacy law'), { target: { value: '2' } });
    fireEvent.click(screen.getByTestId('submit-privacy-request'));
    await waitFor(() => expect(api.create).toHaveBeenCalledWith({ requestType: 2, regime: 2 }));
    expect(await screen.findByText('Your request has been submitted.')).toBeTruthy();
    expect(api.list).toHaveBeenCalledTimes(2);
  });

  it('shows the duplicate-open message', async () => {
    api.list.mockResolvedValue([]);
    api.create.mockRejectedValue({ response: { status: 409, data: { error: { code: 'DSAR_DUPLICATE_OPEN', message: 'x' } } } });
    mount();
    await screen.findByTestId('requests-empty');
    fireEvent.click(screen.getByTestId('submit-privacy-request'));
    expect(await screen.findByText(/already have an open request/i)).toBeTruthy();
  });

  it('shows a friendly state when the inbox is disabled', async () => {
    api.list.mockRejectedValue({ response: { status: 404, data: { error: { code: 'DSAR_INBOX_DISABLED', message: 'x' } } } });
    mount();
    expect(await screen.findByTestId('requests-disabled')).toBeTruthy();
    expect(screen.queryByTestId('submit-privacy-request')).toBeNull();
  });

  it('shows a load error', async () => {
    api.list.mockRejectedValue({ response: { status: 500 } });
    mount();
    expect(await screen.findByText(/could not be loaded/i)).toBeTruthy();
  });
});
