/**
 * Tests for the useCookieConsent facade: consent predicates for every
 * category, null-consent defaults, and pass-through of context state/actions.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';

import useCookieConsentDefault, { useCookieConsent } from '../useCookieConsent';
import { useCookieConsentContext } from '../../contexts/CookieConsentContext';

vi.mock('../../contexts/CookieConsentContext', () => ({
  useCookieConsentContext: vi.fn(),
}));

const ALL_ON = {
  essential_cookies: true,
  functional_cookies: true,
  analytics_cookies: true,
  marketing_cookies: true,
};

function hook(consent) {
  useCookieConsentContext.mockReturnValue({ consent });
  return renderHook(() => useCookieConsent()).result.current;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('useCookieConsent', () => {
  it('is also the default export', () => {
    expect(useCookieConsentDefault).toBe(useCookieConsent);
  });

  describe('before any consent is recorded', () => {
    it('only essential cookies are assumed', () => {
      const c = hook(null);
      expect(c.hasConsent('essential')).toBe(true);
      expect(c.hasConsent('essential_cookies')).toBe(true);
      expect(c.hasConsent('analytics')).toBe(false);
      expect(c.hasAnyNonEssentialConsent()).toBe(false);
      expect(c.hasAllConsents()).toBe(false);
      expect(c.getConsentValue('analytics')).toBeNull();
    });
  });

  describe('with recorded consent', () => {
    it('hasConsent accepts short and suffixed names and always allows essential', () => {
      const c = hook({ ...ALL_ON, essential_cookies: false, functional_cookies: true, analytics_cookies: false });
      expect(c.hasConsent('essential')).toBe(true);
      expect(c.hasConsent('functional')).toBe(true);
      expect(c.hasConsent('functional_cookies')).toBe(true);
      expect(c.hasConsent('analytics')).toBe(false);
    });

    it('hasConsent requires a strict true (not merely truthy)', () => {
      const c = hook({ essential_cookies: true, marketing_cookies: 'yes' });
      expect(c.hasConsent('marketing')).toBe(false);
    });

    it.each([
      ['functional only', { functional_cookies: true }, true],
      ['analytics only', { analytics_cookies: true }, true],
      ['marketing only', { marketing_cookies: true }, true],
      ['nothing optional', { essential_cookies: true }, false],
    ])('hasAnyNonEssentialConsent: %s -> %s', (_label, consent, expected) => {
      expect(hook(consent).hasAnyNonEssentialConsent()).toBe(expected);
    });

    it('hasAllConsents is true only when every category is on', () => {
      expect(hook(ALL_ON).hasAllConsents()).toBe(true);
      expect(hook({ ...ALL_ON, marketing_cookies: false }).hasAllConsents()).toBe(false);
    });

    it('getConsentValue returns the stored flag, false when absent', () => {
      const c = hook({ functional_cookies: true });
      expect(c.getConsentValue('functional')).toBe(true);
      expect(c.getConsentValue('functional_cookies')).toBe(true);
      expect(c.getConsentValue('marketing')).toBe(false);
    });
  });

  it('passes context state and actions straight through', () => {
    const ctx = {
      consent: ALL_ON,
      loading: false,
      error: 'oops',
      showBanner: true,
      showPreferences: false,
      consentId: 'abc',
      acceptAll: vi.fn(),
      rejectNonEssential: vi.fn(),
      savePreferences: vi.fn(),
      setDoNotSell: vi.fn(),
      openPreferences: vi.fn(),
      closeBanner: vi.fn(),
      setShowPreferences: vi.fn(),
    };
    useCookieConsentContext.mockReturnValue(ctx);

    const c = renderHook(() => useCookieConsent()).result.current;

    for (const [key, value] of Object.entries(ctx)) {
      expect(c[key]).toBe(value);
    }
  });
});
