/**
 * Server-side OAuth login: each social button must call
 * GET /api/v1/auth/oauth/<platform> and redirect to the returned authorizeUrl.
 * The frontend never builds a provider URL or sends client_id='server-side'.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import LoginPage from '../LoginPage';
import * as AuthContext from '../../../contexts/AuthContext';

const captured = vi.hoisted(() => ({ props: {} }));
const start = vi.hoisted(() => vi.fn());
const providers = vi.hoisted(() => vi.fn());

vi.mock('@penguintechinc/react-libs', () => ({
  LoginPageBuilder: (props) => {
    captured.props = props;
    return <div data-testid="login-builder" />;
  },
}));

vi.mock('../../../services/api', () => ({
  passkeyApi: { startLogin: vi.fn(), finishLogin: vi.fn() },
  publicApi: { getSignupSettings: vi.fn().mockResolvedValue({ data: { signupEnabled: false } }) },
  tenantApi: { getLoginInfo: vi.fn() },
  ssoApi: { getOptions: vi.fn().mockResolvedValue({ data: { options: [] } }), start: vi.fn() },
  oauthApi: { start, providers },
}));

const assign = vi.fn();

function renderPage() {
  vi.spyOn(AuthContext, 'useAuth').mockReturnValue({ refreshUser: vi.fn(), isAuthenticated: false });
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <LoginPage />
    </MemoryRouter>,
  );
}

describe('LoginPage server-side OAuth buttons', () => {
  beforeEach(() => {
    assign.mockReset();
    start.mockReset();
    providers.mockReset();
    providers.mockResolvedValue({
      data: {
        success: true,
        providers: [
          { platform: 'discord', displayName: 'Discord' },
          { platform: 'twitch', displayName: 'Twitch' },
          { platform: 'google', displayName: 'Google' },
        ],
      },
    });
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, assign },
    });
  });

  it.each(['discord', 'twitch', 'google'])(
    '%s click fetches authorizeUrl from hub-api and redirects to it',
    async (platform) => {
      const url = `https://idp.example/authorize?client_id=REAL&state=s-${platform}`;
      start.mockResolvedValue({ data: { success: true, authorizeUrl: url, state: 's' } });
      const { findByTestId } = renderPage();
      fireEvent.click(await findByTestId(`oauth-${platform}`));
      await waitFor(() => expect(assign).toHaveBeenCalledWith(url));
      expect(start).toHaveBeenCalledWith(platform);
    },
  );

  it('does not hand client-side OAuth config (client_id) to LoginPageBuilder', () => {
    renderPage();
    expect(captured.props.socialLogins).toBeUndefined();
    expect(JSON.stringify(Object.keys(captured.props))).not.toMatch(/clientId/);
    expect(JSON.stringify(captured.props.api)).not.toContain('server-side');
  });

  it('hides youtube and kick (no backend)', async () => {
    const { queryByTestId, queryByText, findByTestId } = renderPage();
    await findByTestId('oauth-discord');
    expect(queryByTestId('oauth-youtube')).toBeNull();
    expect(queryByTestId('oauth-kick')).toBeNull();
    expect(queryByText(/YouTube/i)).toBeNull();
    expect(queryByText(/KICK/i)).toBeNull();
  });

  it('never constructs a provider URL in the frontend', async () => {
    start.mockResolvedValue({ data: { authorizeUrl: 'https://hub.example/real' } });
    const { findByTestId } = renderPage();
    fireEvent.click(await findByTestId('oauth-discord'));
    await waitFor(() => expect(assign).toHaveBeenCalled());
    const target = assign.mock.calls[0][0];
    expect(target).not.toContain('discord.com');
    expect(target).not.toContain('server-side');
  });

  it('shows an error and does not redirect when hub-api fails', async () => {
    start.mockRejectedValue(new Error('boom'));
    const { findByTestId, findByRole } = renderPage();
    fireEvent.click(await findByTestId('oauth-discord'));
    expect(await findByRole('alert')).toBeTruthy();
    expect(assign).not.toHaveBeenCalled();
  });

  it('renders only what the providers endpoint returns -- Slack is hidden', async () => {
    const { findByTestId, queryByTestId, queryByText } = renderPage();
    await findByTestId('oauth-discord');
    expect(queryByTestId('oauth-slack')).toBeNull();
    expect(queryByText(/Slack/i)).toBeNull();
  });

  it('renders a provider the endpoint adds, with no frontend change', async () => {
    providers.mockResolvedValue({
      data: { success: true, providers: [{ platform: 'slack', displayName: 'Slack' }] },
    });
    const { findByTestId, queryByTestId } = renderPage();
    await findByTestId('oauth-slack');
    expect(queryByTestId('oauth-discord')).toBeNull();
  });

  it('shows no social buttons when the providers call fails (fails closed)', async () => {
    providers.mockRejectedValue(new Error('down'));
    const { queryByTestId } = renderPage();
    await waitFor(() => expect(providers).toHaveBeenCalled());
    expect(queryByTestId('social-logins')).toBeNull();
  });
});
