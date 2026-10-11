/**
 * Whitelabel branding on the login page (Professional-tier `tenancy.whitelabel`).
 *
 * The server is the gate: `GET /api/v1/auth/tenant/:slug` only reports
 * `whitelabeled: true` (and returns the tenant's custom logo / welcome
 * message) for an entitled tenant. These tests pin that the page applies
 * custom branding strictly when the server says so -- a Free tenant (or any
 * failure) gets the stock Waddles branding.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import LoginPage from '../LoginPage';
import { DEFAULT_LOGO, resolveBranding } from '../tenantBranding';
import * as AuthContext from '../../../contexts/AuthContext';

const { getLoginInfo } = vi.hoisted(() => ({ getLoginInfo: vi.fn() }));

vi.mock('@penguintechinc/react-libs', () => ({
  LoginPageBuilder: ({ branding }) => (
    <div data-testid="login-builder" data-branding={JSON.stringify(branding)} />
  ),
}));

vi.mock('../../../services/api', () => ({
  passkeyApi: { startLogin: vi.fn(), finishLogin: vi.fn() },
  publicApi: { getSignupSettings: vi.fn().mockResolvedValue({ data: { signupEnabled: false } }) },
  tenantApi: { getLoginInfo },
}));

const CUSTOM_TENANT = {
  slug: 'acme',
  displayName: 'Acme Corp',
  logoUrl: 'https://cdn.acme.example/logo.png',
  config: { theme: 'midnight', welcomeMessage: 'Welcome, Acme folks' },
  whitelabeled: true,
};

function mount(path) {
  vi.spyOn(AuthContext, 'useAuth').mockReturnValue({
    refreshUser: vi.fn(),
    isAuthenticated: false,
  });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/login/:tenantSlug" element={<LoginPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderedBranding() {
  return JSON.parse(screen.getByTestId('login-builder').getAttribute('data-branding'));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('resolveBranding', () => {
  it('is the stock branding with no tenant', () => {
    expect(resolveBranding(null, undefined)).toEqual({
      appName: 'Welcome to Waddles',
      logo: DEFAULT_LOGO,
      tagline: 'Access your communities',
    });
  });

  it('keeps the tenant slug tagline for a non-whitelabeled tenant', () => {
    expect(resolveBranding({ ...CUSTOM_TENANT, whitelabeled: false }, 'acme')).toEqual({
      appName: 'Welcome to Waddles',
      logo: DEFAULT_LOGO,
      tagline: 'Signing into: acme',
    });
  });

  it('never trusts custom fields unless whitelabeled is exactly true', () => {
    for (const flag of [false, undefined, null, 'true', 1]) {
      const branding = resolveBranding({ ...CUSTOM_TENANT, whitelabeled: flag }, 'acme');
      expect(branding.logo).toBe(DEFAULT_LOGO);
    }
  });

  it('applies the custom logo, name and welcome message when whitelabeled', () => {
    expect(resolveBranding(CUSTOM_TENANT, 'acme')).toEqual({
      appName: 'Welcome to Acme Corp',
      logo: 'https://cdn.acme.example/logo.png',
      tagline: 'Welcome, Acme folks',
    });
  });

  it('falls back field-by-field when a whitelabeled tenant has only some branding', () => {
    expect(resolveBranding({ whitelabeled: true, logoUrl: null, config: {} }, 'acme')).toEqual({
      appName: 'Welcome to Waddles',
      logo: DEFAULT_LOGO,
      tagline: 'Signing into: acme',
    });
  });
});

describe('LoginPage tenant branding', () => {
  it('applies custom branding for an entitled (whitelabeled) tenant', async () => {
    getLoginInfo.mockResolvedValue({ data: { tenant: CUSTOM_TENANT } });
    mount('/login/acme');

    await waitFor(() => expect(renderedBranding().logo).toBe(CUSTOM_TENANT.logoUrl));
    expect(getLoginInfo).toHaveBeenCalledWith('acme');
    expect(renderedBranding().tagline).toBe('Welcome, Acme folks');
  });

  it('keeps stock branding for a non-entitled tenant even if custom fields leak through', async () => {
    getLoginInfo.mockResolvedValue({
      data: { tenant: { ...CUSTOM_TENANT, whitelabeled: false } },
    });
    mount('/login/acme');

    await waitFor(() => expect(getLoginInfo).toHaveBeenCalled());
    expect(renderedBranding().logo).toBe(DEFAULT_LOGO);
    expect(renderedBranding().tagline).toBe('Signing into: acme');
  });

  it('keeps stock branding when the login-info request fails', async () => {
    getLoginInfo.mockRejectedValue(new Error('404'));
    mount('/login/acme');

    await waitFor(() => expect(getLoginInfo).toHaveBeenCalled());
    expect(renderedBranding().logo).toBe(DEFAULT_LOGO);
  });

  it('does not request tenant info without a tenant slug', async () => {
    mount('/login');

    await waitFor(() => expect(screen.getByTestId('login-builder')).toBeTruthy());
    expect(getLoginInfo).not.toHaveBeenCalled();
    expect(renderedBranding().logo).toBe(DEFAULT_LOGO);
  });
});
