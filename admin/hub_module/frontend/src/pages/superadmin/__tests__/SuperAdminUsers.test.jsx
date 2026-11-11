/**
 * Tests for the super-admin Users page: the Create New User modal must stay
 * open after the opening click (even when the list load failed) and submit
 * the email + password contract of POST /api/v1/superadmin/users.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import SuperAdminUsers from '../SuperAdminUsers';
import { superAdminApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  default: { post: vi.fn() },
  superAdminApi: {
    listUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    deleteUser: vi.fn(),
    assignSuperAdminRole: vi.fn(),
    assignVendorRole: vi.fn(),
    setEmailVerification: vi.fn(),
  },
}));

/** Fire the full pointer sequence a real mouse click produces. */
function realClick(el) {
  fireEvent.pointerDown(el);
  fireEvent.mouseDown(el);
  fireEvent.pointerUp(el);
  fireEvent.mouseUp(el);
  fireEvent.click(el);
}

const OK = { data: { success: true, users: [], pagination: { total: 0, totalPages: 1 } } };
const pause = (ms) => new Promise((r) => setTimeout(r, ms));

describe('SuperAdminUsers create modal', () => {
  beforeEach(() => vi.clearAllMocks());

  it('stays open after the opening click when the list loads', async () => {
    superAdminApi.listUsers.mockResolvedValue(OK);
    render(<SuperAdminUsers />);
    await waitFor(() => expect(superAdminApi.listUsers).toHaveBeenCalled());
    realClick(screen.getByRole('button', { name: /new user/i }));
    await pause(50);
    expect(screen.getByRole('heading', { name: 'Create New User' })).toBeTruthy();
  });

  it('stays open and shows the list error when the list load fails', async () => {
    superAdminApi.listUsers.mockRejectedValue({ response: { status: 401, data: { error: 'nope' } } });
    render(<SuperAdminUsers />);
    realClick(screen.getByRole('button', { name: /new user/i }));
    await screen.findByText(/Failed to load users/);
    expect(screen.getByRole('heading', { name: 'Create New User' })).toBeTruthy();
  });

  it('does not close on a stray backdrop mousedown right after opening', async () => {
    superAdminApi.listUsers.mockResolvedValue(OK);
    render(<SuperAdminUsers />);
    await waitFor(() => expect(superAdminApi.listUsers).toHaveBeenCalled());
    realClick(screen.getByRole('button', { name: /new user/i }));
    const backdrop = document.querySelector('[aria-hidden="true"].fixed.inset-0');
    fireEvent.mouseDown(backdrop);
    await pause(20);
    expect(screen.getByRole('heading', { name: 'Create New User' })).toBeTruthy();
    await pause(450);
    fireEvent.mouseDown(document.querySelector('[aria-hidden="true"].fixed.inset-0'));
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Create New User' })).toBeNull());
  });

  it('submits email + password to createUser', async () => {
    superAdminApi.listUsers.mockResolvedValue(OK);
    superAdminApi.createUser.mockResolvedValue({ data: { success: true } });
    render(<SuperAdminUsers />);
    await waitFor(() => expect(superAdminApi.listUsers).toHaveBeenCalled());
    realClick(screen.getByRole('button', { name: /new user/i }));
    fireEvent.change(screen.getByLabelText(/Email/), { target: { name: 'email', value: 'a@b.co' } });
    fireEvent.change(screen.getByLabelText(/Password/), {
      target: { name: 'password', value: 'Passw0rd!long' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() =>
      expect(superAdminApi.createUser).toHaveBeenCalledWith({ email: 'a@b.co', password: 'Passw0rd!long' }),
    );
  });
});
