/**
 * Google consumer social login: the login page must offer a "Sign in with
 * Google" button pointing at the backend OAuth proxy (`/auth/oauth/google`).
 */
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import LoginPage from '../LoginPage';
import * as AuthContext from '../../../contexts/AuthContext';

const captured = vi.hoisted(() => ({ socialLogins: [] }));

vi.mock('@penguintechinc/react-libs', () => ({
  LoginPageBuilder: ({ socialLogins }) => {
    captured.socialLogins = socialLogins;
    return (
      <div data-testid="login-builder">
        {socialLogins.map((p) => (
          <a key={p.authUrl} href={p.authUrl}>
            {p.label || p.provider}
          </a>
        ))}
      </div>
    );
  },
}));

vi.mock('../../../services/api', () => ({
  passkeyApi: { startLogin: vi.fn(), finishLogin: vi.fn() },
  publicApi: { getSignupSettings: vi.fn().mockResolvedValue({ data: { signupEnabled: false } }) },
  tenantApi: { getLoginInfo: vi.fn() },
}));

describe('LoginPage Google button', () => {
  it('renders a Sign in with Google link to /auth/oauth/google', () => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({
      refreshUser: vi.fn(),
      isAuthenticated: false,
    });
    const { getByText } = render(
      <MemoryRouter initialEntries={['/login']}>
        <LoginPage />
      </MemoryRouter>,
    );
    const link = getByText('Sign in with Google').closest('a');
    expect(link.getAttribute('href')).toBe('/api/v1/auth/oauth/google');
    expect(captured.socialLogins.some((p) => p.authUrl.endsWith('/auth/oauth/google'))).toBe(true);
  });
});
