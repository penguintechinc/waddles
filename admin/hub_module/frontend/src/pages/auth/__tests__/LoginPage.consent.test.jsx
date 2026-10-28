/**
 * Regression: the login page must not render its own cookie banner on top of
 * the app-wide CookieBanner (duplicate cookie + Do-Not-Sell banners).
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import LoginPage from '../LoginPage';
import * as AuthContext from '../../../contexts/AuthContext';
import CookieBanner from '../../../components/CookieBanner';

vi.mock('@penguintechinc/react-libs', () => ({
  LoginPageBuilder: ({ gdpr }) => (
    <div data-testid="login-builder" data-gdpr-enabled={String(gdpr?.enabled)} />
  ),
}));

vi.mock('../../../hooks/useCookieConsent', () => ({
  useCookieConsent: () => ({
    showBanner: true,
    consent: { do_not_sell: false },
    acceptAll: vi.fn(),
    rejectNonEssential: vi.fn(),
    openPreferences: vi.fn(),
    setDoNotSell: vi.fn(),
  }),
}));

vi.mock('../../../services/api', () => ({
  passkeyApi: { startLogin: vi.fn(), finishLogin: vi.fn() },
  publicApi: { getSignupSettings: vi.fn().mockResolvedValue({ data: { signupEnabled: false } }) },
  ssoApi: { getOptions: vi.fn().mockResolvedValue({ data: { options: [] } }), start: vi.fn() },
  tenantApi: { getLoginInfo: vi.fn().mockResolvedValue({ data: {} }) },
}));

describe('consent banners are not duplicated', () => {
  it('disables the library GDPR banner and renders exactly one app banner + one DNS control', () => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({
      refreshUser: vi.fn(),
      isAuthenticated: false,
    });
    render(
      <MemoryRouter initialEntries={['/login']}>
        <LoginPage />
        <CookieBanner />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('login-builder').dataset.gdprEnabled).toBe('false');
    expect(screen.getAllByTestId('cookie-banner')).toHaveLength(1);
    expect(screen.getAllByText(/Do Not Sell or Share/i)).toHaveLength(1);
  });
});
