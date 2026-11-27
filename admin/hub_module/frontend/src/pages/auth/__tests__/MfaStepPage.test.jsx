/**
 * Federated sign-in MFA step (`/auth/mfa`): hub-api's OAuth / SSO callback hands the pending
 * token in the URL fragment. The page scrubs the fragment, offers the enrolled methods (TOTP
 * first), completes via the shared verify endpoint and finishes like the password path.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import MfaStepPage, { parseMfaFragment } from '../MfaStepPage';
import * as AuthContext from '../../../contexts/AuthContext';
import { mfaApi } from '../../../services/api';

const navigateSpy = vi.fn();

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => navigateSpy };
});

vi.mock('../../../services/api', () => ({
  mfaApi: { loginSendEmail: vi.fn(), loginVerify: vi.fn() },
}));

const refreshUser = vi.fn();

function mount(hash) {
  window.history.replaceState(null, '', `/auth/mfa${hash}`);
  return render(
    <MemoryRouter initialEntries={['/auth/mfa']}>
      <MfaStepPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  refreshUser.mockReset();
  refreshUser.mockResolvedValue(undefined);
  vi.spyOn(AuthContext, 'useAuth').mockReturnValue({ refreshUser });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  window.history.replaceState(null, '', '/');
});

describe('parseMfaFragment', () => {
  it('reads the pending token and orders known methods TOTP first', () => {
    expect(parseMfaFragment('#pending=tok&methods=recovery,bogus,totp&expiresIn=600')).toEqual({
      mfaToken: 'tok',
      methods: ['totp', 'recovery'],
      expiresIn: 600,
    });
  });

  it('returns null without a pending token and falls back to recovery codes', () => {
    expect(parseMfaFragment('')).toBeNull();
    expect(parseMfaFragment('#methods=totp')).toBeNull();
    expect(parseMfaFragment('#pending=tok&expiresIn=nope')).toEqual({
      mfaToken: 'tok',
      methods: ['recovery'],
      expiresIn: null,
    });
  });
});

describe('MfaStepPage', () => {
  it('scrubs the fragment, verifies with TOTP and finishes like the password path', async () => {
    mfaApi.loginVerify.mockResolvedValue({ data: { success: true, token: 'jwt' } });
    mount('#pending=pend-tok&methods=totp,recovery&expiresIn=600');

    expect(window.location.hash).toBe('');
    expect(screen.getByTestId('mfa-login-step')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Authenticator app' })).toHaveAttribute('aria-selected', 'true');

    fireEvent.change(screen.getByLabelText('Authenticator app'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/dashboard'));
    expect(mfaApi.loginVerify).toHaveBeenCalledWith({ mfaToken: 'pend-tok', method: 'totp', code: '123456' });
    expect(refreshUser).toHaveBeenCalledWith(true);
  });

  it('shows the error and a restart when the token was already used or expired', async () => {
    mfaApi.loginVerify.mockRejectedValue({
      response: { data: { error: { code: 'MFA_CHALLENGE_INVALID', message: 'Sign-in expired.' } } },
    });
    mount('#pending=pend-tok&methods=totp');
    fireEvent.change(screen.getByLabelText('Authenticator app'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Sign-in expired.');
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }));
    expect(navigateSpy).toHaveBeenCalledWith('/login');
    expect(refreshUser).not.toHaveBeenCalled();
  });

  it('shows a busy state while verifying', async () => {
    let resolve;
    mfaApi.loginVerify.mockReturnValue(new Promise((r) => { resolve = r; }));
    mount('#pending=pend-tok&methods=totp');
    fireEvent.change(screen.getByLabelText('Authenticator app'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    expect(await screen.findByRole('button', { name: 'Verifying...' })).toBeDisabled();
    refreshUser.mockReturnValue(new Promise(() => {}));
    await act(async () => resolve({ data: { token: 'jwt' } }));
    expect(await screen.findByTestId('mfa-step-finishing')).toBeInTheDocument();
  });

  it('reports a failed account load after a verified code', async () => {
    mfaApi.loginVerify.mockResolvedValue({ data: { token: 'jwt' } });
    refreshUser.mockRejectedValue({ response: { status: 500 } });
    mount('#pending=pend-tok&methods=totp');
    fireEvent.change(screen.getByLabelText('Authenticator app'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    expect(await screen.findByText(/loading your account failed/)).toBeInTheDocument();
    expect(navigateSpy).not.toHaveBeenCalled();
  });

  it('expires the step locally after expiresIn', () => {
    vi.useFakeTimers();
    mount('#pending=pend-tok&methods=totp&expiresIn=5');
    expect(screen.getByTestId('mfa-login-step')).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByTestId('mfa-step-expired')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Back to sign in' }));
    expect(navigateSpy).toHaveBeenCalledWith('/login');
  });

  it('explains a missing token instead of calling the API', () => {
    mount('');
    expect(screen.getByTestId('mfa-step-missing')).toBeInTheDocument();
    expect(mfaApi.loginVerify).not.toHaveBeenCalled();
  });

  it('never logs the pending token', async () => {
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {});
    mfaApi.loginVerify.mockResolvedValue({ data: { token: 'jwt' } });
    mount('#pending=secret-pending-value&methods=totp');
    fireEvent.change(screen.getByLabelText('Authenticator app'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await waitFor(() => expect(navigateSpy).toHaveBeenCalled());
    expect(JSON.stringify(debug.mock.calls)).not.toContain('secret-pending-value');
  });
});
