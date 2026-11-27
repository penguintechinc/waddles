/**
 * Security settings MFA card: TOTP listed first and marked Recommended, email
 * second (unavailable without SMTP / a verified address), loading + error
 * states, the TOTP enroll flow (QR rendered locally, first-code confirm,
 * one-time recovery codes), and the re-auth disable flow.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import MfaSettings from '../MfaSettings';
import { mfaApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  mfaApi: {
    status: vi.fn(),
    enrollTotp: vi.fn(),
    confirmTotp: vi.fn(),
    enrollEmail: vi.fn(),
    confirmEmail: vi.fn(),
    sendEmailCode: vi.fn(),
    disable: vi.fn(),
    regenerateRecoveryCodes: vi.fn(),
  },
}));

const status = (over = {}) => ({
  data: {
    success: true,
    enabled: true,
    totp: { status: 0, available: true },
    email: { status: 0, available: true },
    emailVerified: true,
    hasPassword: true,
    recoveryCodesRemaining: 0,
    preferredMethod: null,
    ...over,
  },
});

const CODES = Array.from({ length: 10 }, (_, i) => `AAAA${i}-BBBB${i}`);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('MfaSettings', () => {
  it('shows a loading state, then TOTP first with a Recommended badge and email second', async () => {
    mfaApi.status.mockResolvedValue(status());
    render(<MfaSettings />);
    expect(screen.getByRole('status')).toHaveTextContent(/Loading/);
    const totp = await screen.findByTestId('mfa-method-totp');
    const email = screen.getByTestId('mfa-method-email');
    expect(within(totp).getByText('Recommended')).toBeInTheDocument();
    expect(totp.compareDocumentPosition(email) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(email).queryByText('Recommended')).toBeNull();
  });

  it('marks email unavailable when SMTP is not configured', async () => {
    mfaApi.status.mockResolvedValue(status({ email: { status: 0, available: false } }));
    render(<MfaSettings />);
    expect(await screen.findByTestId('email-unavailable')).toHaveTextContent(/not configured/);
    expect(within(screen.getByTestId('mfa-method-email')).queryByRole('button', { name: 'Set up' })).toBeNull();
  });

  it('asks for a verified email before offering email codes', async () => {
    mfaApi.status.mockResolvedValue(status({ emailVerified: false }));
    render(<MfaSettings />);
    expect(await screen.findByTestId('email-unavailable')).toHaveTextContent(/Verify your email/);
  });

  it('shows an error with retry when status fails to load', async () => {
    mfaApi.status.mockRejectedValueOnce({ response: { data: { error: { code: 'X', message: 'Boom' } } } });
    mfaApi.status.mockResolvedValueOnce(status());
    render(<MfaSettings />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Boom');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByTestId('mfa-method-totp')).toBeInTheDocument();
  });

  it('enrolls TOTP: renders the QR locally, confirms, then shows recovery codes once', async () => {
    mfaApi.status.mockResolvedValueOnce(status());
    mfaApi.status.mockResolvedValue(status({ totp: { status: 2, available: true }, recoveryCodesRemaining: 10 }));
    mfaApi.enrollTotp.mockResolvedValue({
      data: { otpauthUri: 'otpauth://totp/Waddles:a?secret=JBSWY3DPEHPK3PXP&issuer=Waddles', secret: 'JBSWY3DPEHPK3PXP' },
    });
    mfaApi.confirmTotp.mockResolvedValue({ data: { success: true, recoveryCodes: CODES } });
    const { container } = render(<MfaSettings />);
    fireEvent.click(within(await screen.findByTestId('mfa-method-totp')).getByRole('button', { name: 'Set up' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Set up authenticator app' }));
    expect(await screen.findByTestId('totp-secret')).toHaveTextContent('JBSWY3DPEHPK3PXP');
    expect(container.querySelector('svg title')?.textContent).toBe('Authenticator QR code');
    fireEvent.change(screen.getByLabelText('Code from your app'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify and enable' }));
    await waitFor(() => expect(mfaApi.confirmTotp).toHaveBeenCalledWith('123456'));
    const panel = await screen.findByTestId('recovery-codes');
    expect(within(panel).getAllByRole('listitem')).toHaveLength(10);
    const done = within(panel).getByRole('button', { name: 'Done' });
    expect(done).toBeDisabled();
    fireEvent.click(within(panel).getByRole('checkbox'));
    fireEvent.click(done);
    expect(screen.queryByTestId('recovery-codes')).toBeNull();
    expect(within(screen.getByTestId('mfa-method-totp')).getByText('On')).toBeInTheDocument();
  });

  it('shows the server error when the first code is wrong', async () => {
    mfaApi.status.mockResolvedValue(status());
    mfaApi.enrollTotp.mockResolvedValue({ data: { otpauthUri: 'otpauth://totp/x?secret=A', secret: 'A' } });
    mfaApi.confirmTotp.mockRejectedValue({
      response: { data: { error: { code: 'MFA_INVALID_CODE', message: 'Invalid verification code' } } },
    });
    render(<MfaSettings />);
    fireEvent.click(within(await screen.findByTestId('mfa-method-totp')).getByRole('button', { name: 'Set up' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Set up authenticator app' }));
    fireEvent.change(await screen.findByLabelText('Code from your app'), { target: { value: '000000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify and enable' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid verification code');
  });

  it('disables TOTP with password + code', async () => {
    mfaApi.status.mockResolvedValueOnce(status({ totp: { status: 2, available: true }, recoveryCodesRemaining: 10 }));
    mfaApi.status.mockResolvedValue(status());
    mfaApi.disable.mockResolvedValue({ data: { success: true } });
    render(<MfaSettings />);
    fireEvent.click(within(await screen.findByTestId('mfa-method-totp')).getByRole('button', { name: 'Turn off' }));
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'pw-1234567' } });
    fireEvent.change(screen.getByLabelText(/Authenticator, email or recovery code/), { target: { value: '654321' } });
    fireEvent.click(within(screen.getByTestId('mfa-change-form')).getByRole('button', { name: 'Turn off' }));
    await waitFor(() =>
      expect(mfaApi.disable).toHaveBeenCalledWith({ method: 'totp', password: 'pw-1234567', code: '654321' }),
    );
    await waitFor(() => expect(screen.queryByTestId('mfa-change-form')).toBeNull());
  });

  it('enrolls email codes', async () => {
    mfaApi.status.mockResolvedValueOnce(status());
    mfaApi.status.mockResolvedValue(status({ email: { status: 2, available: true } }));
    mfaApi.enrollEmail.mockResolvedValue({ data: { success: true, expiresIn: 600 } });
    mfaApi.confirmEmail.mockResolvedValue({ data: { success: true, recoveryCodes: [] } });
    render(<MfaSettings />);
    fireEvent.click(within(await screen.findByTestId('mfa-method-email')).getByRole('button', { name: 'Set up' }));
    fireEvent.click(screen.getByRole('button', { name: 'Email me a code' }));
    fireEvent.change(await screen.findByLabelText('Email code'), { target: { value: '111222' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify and enable' }));
    await waitFor(() => expect(mfaApi.confirmEmail).toHaveBeenCalledWith('111222'));
    expect(await within(screen.getByTestId('mfa-method-email')).findByText('On')).toBeInTheDocument();
  });

  it('adding TOTP while email is on requires password + a code first', async () => {
    mfaApi.status.mockResolvedValue(status({ email: { status: 2, available: true }, recoveryCodesRemaining: 10 }));
    mfaApi.enrollTotp.mockResolvedValue({ data: { otpauthUri: 'otpauth://totp/x?secret=A', secret: 'A' } });
    render(<MfaSettings />);
    fireEvent.click(within(await screen.findByTestId('mfa-method-totp')).getByRole('button', { name: 'Set up' }));
    const form = await screen.findByTestId('mfa-change-form');
    expect(mfaApi.enrollTotp).not.toHaveBeenCalled();
    fireEvent.change(within(form).getByLabelText('Current password'), { target: { value: 'pw-1234567' } });
    fireEvent.change(within(form).getByLabelText(/Authenticator, email or recovery code/), {
      target: { value: '445566' },
    });
    fireEvent.click(within(form).getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(mfaApi.enrollTotp).toHaveBeenCalledWith({ password: 'pw-1234567', code: '445566' }));
    expect(await screen.findByTestId('totp-secret')).toHaveTextContent('A');
  });

  it('adding email while TOTP is on requires re-auth before a code is sent', async () => {
    mfaApi.status.mockResolvedValue(status({ totp: { status: 2, available: true }, recoveryCodesRemaining: 10 }));
    mfaApi.enrollEmail.mockRejectedValueOnce({
      response: { data: { error: { code: 'MFA_INVALID_CODE', message: 'Invalid verification code' } } },
    });
    render(<MfaSettings />);
    fireEvent.click(within(await screen.findByTestId('mfa-method-email')).getByRole('button', { name: 'Set up' }));
    const form = await screen.findByTestId('mfa-change-form');
    fireEvent.change(within(form).getByLabelText('Current password'), { target: { value: 'pw-1234567' } });
    fireEvent.change(within(form).getByLabelText(/Authenticator, email or recovery code/), {
      target: { value: '000000' },
    });
    fireEvent.click(within(form).getByRole('button', { name: 'Continue' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid verification code');
    expect(screen.queryByLabelText('Email code')).toBeNull();
  });
});
