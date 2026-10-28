/**
 * Enterprise tenant SSO discovery on the login page: the option appears only
 * when /auth/sso/options returns connections, and clicking starts the chosen
 * connection (GET /auth/sso/<id>/start) then navigates to the IdP URL.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import LoginPage from '../LoginPage';
import * as AuthContext from '../../../contexts/AuthContext';
import { ssoApi } from '../../../services/api';

vi.mock('@penguintechinc/react-libs', () => ({
  LoginPageBuilder: () => <div data-testid="login-builder" />,
}));

vi.mock('../../../services/api', () => ({
  passkeyApi: { startLogin: vi.fn(), finishLogin: vi.fn() },
  publicApi: { getSignupSettings: vi.fn().mockResolvedValue({ data: { signupEnabled: false } }) },
  tenantApi: { getLoginInfo: vi.fn() },
  ssoApi: { getOptions: vi.fn(), start: vi.fn() },
}));

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/login']}>
      <LoginPage />
    </MemoryRouter>,
  );

describe('LoginPage enterprise SSO', () => {
  const assign = vi.fn();

  beforeEach(() => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({
      refreshUser: vi.fn(),
      isAuthenticated: false,
    });
    vi.stubGlobal('location', { ...window.location, assign });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('hides the SSO option when the tenant has no connections', async () => {
    ssoApi.getOptions.mockResolvedValue({ data: { success: true, options: [] } });
    renderPage();
    await waitFor(() => expect(ssoApi.getOptions).toHaveBeenCalled());
    expect(screen.queryByTestId('sso-options')).toBeNull();
  });

  it('hides the SSO option when discovery fails', async () => {
    ssoApi.getOptions.mockRejectedValue(new Error('boom'));
    renderPage();
    await waitFor(() => expect(ssoApi.getOptions).toHaveBeenCalled());
    expect(screen.queryByTestId('sso-options')).toBeNull();
  });

  it('shows the option and starts the connection on click', async () => {
    ssoApi.getOptions.mockResolvedValue({
      data: { success: true, options: [{ id: 'abc-123', displayName: 'Acme Okta', protocol: 'oidc' }] },
    });
    ssoApi.start.mockResolvedValue({
      data: { success: true, redirectUrl: 'https://idp.example/auth?x=1', protocol: 'oidc' },
    });
    renderPage();
    const button = await screen.findByRole('button', { name: /Sign in with Acme Okta/ });
    fireEvent.click(button);
    await waitFor(() => expect(ssoApi.start).toHaveBeenCalledWith('abc-123'));
    expect(assign).toHaveBeenCalledWith('https://idp.example/auth?x=1');
  });

  it('lets the user pick among multiple connections', async () => {
    ssoApi.getOptions.mockResolvedValue({
      data: {
        success: true,
        options: [
          { id: 'a', displayName: 'Okta', protocol: 'oidc' },
          { id: 'b', displayName: 'Corp SAML', protocol: 'saml' },
        ],
      },
    });
    ssoApi.start.mockResolvedValue({ data: { redirectUrl: 'https://idp.example/saml' } });
    renderPage();
    fireEvent.change(await screen.findByLabelText(/Organization sign-in method/), { target: { value: 'b' } });
    fireEvent.click(screen.getByRole('button', { name: /your organization/ }));
    await waitFor(() => expect(ssoApi.start).toHaveBeenCalledWith('b'));
  });
});
