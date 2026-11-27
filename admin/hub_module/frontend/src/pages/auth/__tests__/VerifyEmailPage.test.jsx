/**
 * Email verification webui: the /verify-email landing page (token from the URL fragment, posted
 * exactly once, loading/success/error states), the resend panel (rate-limit + send failure), the
 * register page's "check your inbox" state and the login page's typed OAuth error banner.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { MemoryRouter } from 'react-router-dom';

import VerifyEmailPage from '../VerifyEmailPage';
import ResendVerification from '../ResendVerification';
import RegisterPage from '../RegisterPage';
import LoginPage from '../LoginPage';
import * as AuthContext from '../../../contexts/AuthContext';
import { emailVerificationApi } from '../../../services/api';

vi.mock('@penguintechinc/react-libs', () => ({
  LoginPageBuilder: () => <div data-testid="login-builder" />,
}));

vi.mock('../../../services/api', () => ({
  emailVerificationApi: { verify: vi.fn(), resend: vi.fn(), cancelSignup: vi.fn() },
  oauthApi: { providers: vi.fn().mockResolvedValue({ data: { providers: [] } }) },
  passkeyApi: { startLogin: vi.fn(), finishLogin: vi.fn() },
  publicApi: {
    getSignupSettings: vi.fn().mockResolvedValue({ data: { signupEnabled: true } }),
  },
  tenantApi: { getLoginInfo: vi.fn() },
  ssoApi: { getOptions: vi.fn().mockResolvedValue({ data: { options: [] } }), start: vi.fn() },
}));

const refreshUser = vi.fn();
const register = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(AuthContext, 'useAuth').mockReturnValue({
    refreshUser,
    register,
    isAuthenticated: false,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

const at = (path, ui) => render(<MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>);

describe('VerifyEmailPage', () => {
  it('posts the fragment token exactly once (StrictMode) and asks the user to sign in', async () => {
    emailVerificationApi.verify.mockResolvedValue({ data: { success: true } });
    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/verify-email#token=abc_DEF-123']}>
          <VerifyEmailPage />
        </MemoryRouter>
      </StrictMode>,
    );
    expect(screen.getByTestId('verify-loading')).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId('verify-success')).toBeTruthy());
    expect(emailVerificationApi.verify).toHaveBeenCalledTimes(1);
    expect(emailVerificationApi.verify).toHaveBeenCalledWith('abc_DEF-123');
    // Verifying never signs anyone in: no session refresh, an explicit sign-in step instead.
    expect(refreshUser).not.toHaveBeenCalled();
    expect(screen.getByTestId('verify-success').textContent).toMatch(/Sign in/);
  });

  it("the \"this wasn't me\" link cancels the pending account instead of verifying", async () => {
    emailVerificationApi.cancelSignup.mockResolvedValue({ data: { success: true } });
    at('/verify-email#token=tok_1&action=cancel', <VerifyEmailPage />);
    await waitFor(() => expect(screen.getByTestId('cancel-success')).toBeTruthy());
    expect(emailVerificationApi.cancelSignup).toHaveBeenCalledWith('tok_1');
    expect(emailVerificationApi.verify).not.toHaveBeenCalled();
  });

  it('shows the server error for a dead cancel link', async () => {
    emailVerificationApi.cancelSignup.mockRejectedValue({
      response: { status: 400, data: { error: { message: 'Invalid or expired verification token' } } },
    });
    at('/verify-email#token=old&action=cancel', <VerifyEmailPage />);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/expired/));
  });

  it('shows the server error and a resend form for a bad or expired link', async () => {
    emailVerificationApi.verify.mockRejectedValue({
      response: { status: 400, data: { error: { message: 'Verification token has expired' } } },
    });
    at('/verify-email#token=old', <VerifyEmailPage />);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/expired/));
    expect(screen.getByTestId('resend-verification')).toBeTruthy();
  });

  it('without a token shows the check-your-inbox state and never calls verify', () => {
    at('/verify-email', <VerifyEmailPage />);
    expect(screen.getByTestId('verification-pending')).toBeTruthy();
    expect(emailVerificationApi.verify).not.toHaveBeenCalled();
  });
});

describe('ResendVerification', () => {
  const submit = (email) => {
    fireEvent.change(screen.getByTestId('resend-email-input'), { target: { value: email } });
    fireEvent.click(screen.getByTestId('resend-verification'));
  };

  it('confirms a resend without revealing whether the account exists', async () => {
    emailVerificationApi.resend.mockResolvedValue({ data: { success: true } });
    render(<ResendVerification />);
    submit('someone@example.com');
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/new link/));
    expect(emailVerificationApi.resend).toHaveBeenCalledWith('someone@example.com');
  });

  it('explains the rate limit with the wait time', async () => {
    emailVerificationApi.resend.mockRejectedValue({
      response: { status: 429, data: { error: { details: { retryAfterSeconds: 42 } } } },
    });
    render(<ResendVerification initialEmail="someone@example.com" />);
    fireEvent.click(screen.getByTestId('resend-verification'));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/42 seconds/));
  });

  it('surfaces a send failure as retryable, never as success', async () => {
    emailVerificationApi.resend.mockRejectedValue({
      response: { status: 503, data: { error: { message: "We couldn't send the verification email." } } },
    });
    render(<ResendVerification initialEmail="someone@example.com" />);
    fireEvent.click(screen.getByTestId('resend-verification'));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/couldn't send/));
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByTestId('resend-verification').disabled).toBe(false);
  });
});

describe('RegisterPage verification states', () => {
  const fill = async () => {
    await screen.findByTestId('email-input');
    fireEvent.change(screen.getByTestId('username-input'), { target: { name: 'username', value: 'newuser' } });
    fireEvent.change(screen.getByTestId('email-input'), { target: { name: 'email', value: 'new@example.com' } });
    fireEvent.change(screen.getByTestId('password-input'), { target: { name: 'password', value: 'Hunter22x' } });
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { name: 'confirmPassword', value: 'Hunter22x' },
    });
    fireEvent.submit(screen.getByTestId('auth-submit').closest('form'));
  };

  it('shows check-your-inbox with resend after a pending signup', async () => {
    register.mockResolvedValue({ requiresVerification: true, message: 'Please check your email' });
    at('/register', <RegisterPage />);
    await fill();
    await waitFor(() => expect(screen.getByTestId('verification-pending')).toBeTruthy());
    expect(screen.getByTestId('resend-email-input').value).toBe('new@example.com');
  });

  it("turns a failed verification send into a resend prompt, not a dead end", async () => {
    const err = new Error("We couldn't send the verification email. Please try again in a moment.");
    err.code = 'verification_email_failed';
    register.mockRejectedValue(err);
    at('/register', <RegisterPage />);
    await fill();
    await waitFor(() => expect(screen.getByTestId('verification-pending')).toBeTruthy());
    expect(screen.getByRole('alert').textContent).toMatch(/couldn't send/);
  });
});

describe('LoginPage OAuth verification notices', () => {
  it.each([
    ['email_verification_required', /verify your email/],
    ['email_domain_not_allowed', /restricted to specific email domains/],
  ])('renders a readable notice for %s', async (code, text) => {
    at(`/login?error=${code}`, <LoginPage />);
    expect((await screen.findByTestId('login-notice')).textContent).toMatch(text);
    expect(screen.getByTestId('verify-email-link').getAttribute('href')).toBe('/verify-email');
  });

  it('shows nothing for an unknown code', () => {
    at('/login?error=<script>', <LoginPage />);
    expect(screen.queryByTestId('login-notice')).toBeNull();
  });
});
