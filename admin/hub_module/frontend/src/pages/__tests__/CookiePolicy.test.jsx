/**
 * Tests that the user-facing cookie disclosures match what Waddles actually sets.
 *
 * regression: the cookie policy page and the preferences modal listed Google
 * Analytics, Mixpanel, Hotjar, Facebook Pixel, Google Ads and LinkedIn cookies
 * (plus csrf_token / user_id / session_token cookies) -- none of which the app
 * ever set or loaded. The only first-party cookies hub-api sets are `wb_session`
 * (hub_api/services/session_cookie.py), `waddlebot_consent_id`
 * (hub_api/services/cookie_consent_service.py) and the SSO binder `wb_sso_bind`
 * (hub_api/blueprints/v1/sso.py). PostHog is used server-side for feature flags
 * only and sets nothing in the browser.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import CookiePolicy from '../CookiePolicy';
import CookiePreferencesModal from '../../components/CookiePreferencesModal';
import * as consentContext from '../../contexts/CookieConsentContext';

vi.mock('../../services/api', () => ({
  default: { get: vi.fn().mockResolvedValue({ data: { success: true, policy: { version: '1.0' } } }) },
}));

const REAL_COOKIES = ['wb_session', 'waddlebot_consent_id', 'wb_sso_bind'];
const NEVER_SET = [
  '_ga',
  '_gid',
  '_mixpanel',
  '_hjid',
  '_fbp',
  '_gcl',
  '_gac_',
  '_linkedin_partner_id',
  'csrf_token',
  'user_id',
  'session_token',
  'theme_preference',
  'sidebar_collapsed',
  'last_visited_community',
];
const VENDOR_CLAIMS = [/mixpanel cookie/i, /hotjar -/i, /facebook pixel -/i, /google ads -/i, /linkedin -/i];

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(consentContext, 'useCookieConsentContext').mockReturnValue({
    consent: null,
    showPreferences: true,
    setShowPreferences: vi.fn(),
    savePreferences: vi.fn(),
    acceptAll: vi.fn(),
    rejectNonEssential: vi.fn(),
    openPreferences: vi.fn(),
  });
});

async function renderPolicy() {
  const view = render(
    <MemoryRouter>
      <CookiePolicy />
    </MemoryRouter>,
  );
  await screen.findByText('Cookie Policy');
  return view;
}

describe('CookiePolicy specific-cookies table', () => {
  it('lists exactly the first-party cookies that are really set', async () => {
    const { container } = await renderPolicy();
    const rows = [...container.querySelectorAll('tbody code')].map((el) => el.textContent);
    expect(rows).toEqual(REAL_COOKIES);
  });

  it('lists no cookie that the app never sets', async () => {
    const { container } = await renderPolicy();
    const text = container.textContent;
    for (const name of NEVER_SET) {
      expect(text).not.toContain(name);
    }
    for (const claim of VENDOR_CLAIMS) {
      expect(text).not.toMatch(claim);
    }
  });

  it('states that analytics runs server-side on PostHog with no browser footprint', async () => {
    await renderPolicy();
    expect(screen.getByText(/does not set analytics cookies/i)).toHaveTextContent(/PostHog/);
    expect(screen.getByText(/does not set analytics cookies/i)).toHaveTextContent(/no cookie is set/i);
  });
});

describe('CookiePreferencesModal examples', () => {
  it('shows only the real essential cookies and a none-note for optional categories', () => {
    const { container } = render(<CookiePreferencesModal />);
    const text = container.textContent;
    for (const name of REAL_COOKIES) {
      expect(text).toContain(name);
    }
    for (const name of NEVER_SET) {
      expect(text).not.toContain(name);
    }
    for (const claim of [...VENDOR_CLAIMS, /google analytics -/i]) {
      expect(text).not.toMatch(claim);
    }
    expect(screen.getByText(/does not currently set any functional cookies/i)).toBeInTheDocument();
    expect(screen.getByText(/does not set marketing or advertising cookies/i)).toBeInTheDocument();
  });
});
