/**
 * Login page MFA branch: a 202 `{ mfaRequired, mfaToken, methods }` answer from
 * LoginPageBuilder swaps the page for the MFA step (no session yet); a
 * verified code refreshes the user and navigates to the dashboard.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import LoginPage from '../LoginPage';
import * as AuthContext from '../../../contexts/AuthContext';
import { mfaApi } from '../../../services/api';

vi.mock('@penguintechinc/react-libs', () => ({
  LoginPageBuilder: ({ onSuccess }) => (
    <button
      type="button"
      onClick={() => onSuccess({ success: true, mfaRequired: true, mfaToken: 'tok', methods: ['totp', 'recovery'] })}
    >
      submit-password
    </button>
  ),
}));

vi.mock('../../../services/api', () => ({
  oauthApi: { providers: vi.fn().mockResolvedValue({ data: { providers: [] } }) },
  passkeyApi: { startLogin: vi.fn(), finishLogin: vi.fn() },
  publicApi: { getSignupSettings: vi.fn().mockResolvedValue({ data: { signupEnabled: false } }) },
  tenantApi: { getLoginInfo: vi.fn() },
  ssoApi: { getOptions: vi.fn().mockResolvedValue({ data: { options: [] } }), start: vi.fn() },
  mfaApi: { loginSendEmail: vi.fn(), loginVerify: vi.fn() },
}));

describe('LoginPage MFA step', () => {
  const refreshUser = vi.fn();

  beforeEach(() => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({ refreshUser, isAuthenticated: false });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('shows the MFA step after the password and finishes the login', async () => {
    mfaApi.loginVerify.mockResolvedValue({ data: { success: true, token: 'jwt' } });
    render(
      <MemoryRouter initialEntries={['/login']}>
        <LoginPage />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'submit-password' }));
    expect(await screen.findByTestId('mfa-login-step')).toBeInTheDocument();
    expect(refreshUser).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Authenticator app'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await waitFor(() => expect(refreshUser).toHaveBeenCalledWith(true));
  });

  it('cancel returns to the password form', async () => {
    render(
      <MemoryRouter initialEntries={['/login']}>
        <LoginPage />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'submit-password' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'submit-password' })).toBeInTheDocument();
  });
});
