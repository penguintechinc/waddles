/**
 * Login MFA step: TOTP preselected when enrolled, method switching, email
 * code send, success hand-off, wrong-code error, and the expired/locked
 * challenge path ("Start over").
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import MfaLoginStep from '../MfaLoginStep';
import { mfaApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  mfaApi: { loginSendEmail: vi.fn(), loginVerify: vi.fn() },
}));

const challenge = { mfaToken: 'opaque-token', methods: ['totp', 'email', 'recovery'] };

beforeEach(() => {
  vi.clearAllMocks();
});

describe('MfaLoginStep', () => {
  it('preselects the authenticator app and completes the login', async () => {
    const onSuccess = vi.fn();
    mfaApi.loginVerify.mockResolvedValue({ data: { success: true, token: 't', user: { id: 1 } } });
    render(<MfaLoginStep challenge={challenge} onSuccess={onSuccess} onCancel={vi.fn()} />);
    expect(screen.getByRole('tab', { name: 'Authenticator app' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.change(screen.getByLabelText('Authenticator app'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await waitFor(() =>
      expect(mfaApi.loginVerify).toHaveBeenCalledWith({ mfaToken: 'opaque-token', method: 'totp', code: '123456' }),
    );
    expect(onSuccess).toHaveBeenCalledWith({ success: true, token: 't', user: { id: 1 } });
  });

  it('shows the error for a wrong code and clears the input', async () => {
    mfaApi.loginVerify.mockRejectedValue({
      response: { data: { error: { code: 'MFA_INVALID_CODE', message: 'Invalid verification code' } } },
    });
    render(<MfaLoginStep challenge={challenge} onSuccess={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('Authenticator app'), { target: { value: '000000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid verification code');
    expect(screen.getByLabelText('Authenticator app')).toHaveValue('');
  });

  it('offers Start over once the challenge is invalid', async () => {
    const onCancel = vi.fn();
    mfaApi.loginVerify.mockRejectedValue({
      response: { data: { error: { code: 'MFA_CHALLENGE_INVALID', message: 'Verification session expired.' } } },
    });
    render(<MfaLoginStep challenge={challenge} onSuccess={vi.fn()} onCancel={onCancel} />);
    fireEvent.change(screen.getByLabelText('Authenticator app'), { target: { value: '000000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Start over' }));
    expect(onCancel).toHaveBeenCalled();
  });

  it('sends an email code and verifies with the email method', async () => {
    mfaApi.loginSendEmail.mockResolvedValue({ data: { success: true, expiresIn: 600 } });
    mfaApi.loginVerify.mockResolvedValue({ data: { success: true, token: 't' } });
    render(<MfaLoginStep challenge={challenge} onSuccess={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Email code' }));
    fireEvent.click(screen.getByRole('button', { name: 'Send code' }));
    await waitFor(() => expect(mfaApi.loginSendEmail).toHaveBeenCalledWith('opaque-token'));
    expect(await screen.findByRole('button', { name: 'Resend' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Email code'), { target: { value: '222333' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await waitFor(() =>
      expect(mfaApi.loginVerify).toHaveBeenCalledWith({ mfaToken: 'opaque-token', method: 'email', code: '222333' }),
    );
  });

  it('surfaces an unavailable email transport', async () => {
    mfaApi.loginSendEmail.mockRejectedValue({
      response: { data: { error: { code: 'EMAIL_MFA_UNAVAILABLE', message: 'Email delivery is not configured' } } },
    });
    render(<MfaLoginStep challenge={challenge} onSuccess={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Email code' }));
    fireEvent.click(screen.getByRole('button', { name: 'Send code' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('not configured');
  });

  it('accepts a recovery code', async () => {
    mfaApi.loginVerify.mockResolvedValue({ data: { success: true, token: 't' } });
    render(<MfaLoginStep challenge={challenge} onSuccess={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Recovery code' }));
    fireEvent.change(screen.getByLabelText('Recovery code'), { target: { value: 'ABCDE-FGHJK' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await waitFor(() =>
      expect(mfaApi.loginVerify).toHaveBeenCalledWith({ mfaToken: 'opaque-token', method: 'recovery', code: 'ABCDE-FGHJK' }),
    );
  });
});
