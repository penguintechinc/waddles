/**
 * Cookie Preferences Modal Component
 * Detailed cookie preferences dialog with per-category toggles
 */
import { useState, useEffect } from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';
import { useCookieConsentContext } from '../contexts/CookieConsentContext';

const COOKIE_CATEGORIES = [
  {
    id: 'essential_cookies',
    name: 'Essential Cookies',
    required: true,
    description: 'Essential cookies are required for the website to function. They enable core functionality like keeping you signed in, security, and remembering your cookie choices.',
    examples: [
      { name: 'wb_session', purpose: 'Keeps you signed in (HttpOnly, expires after 24 hours)' },
      { name: 'waddlebot_consent_id', purpose: 'Links this browser to your saved cookie-consent record (12 months)' },
      { name: 'wb_sso_bind', purpose: 'Secures a single sign-on login attempt (HttpOnly, only present while signing in)' },
    ],
  },
  {
    id: 'functional_cookies',
    name: 'Functional Cookies',
    required: false,
    description: 'Functional cookies would remember preferences and settings across visits.',
    examples: [],
    noneNote: 'Waddles does not currently set any functional cookies.',
  },
  {
    id: 'analytics_cookies',
    name: 'Analytics Cookies',
    required: false,
    description: 'Analytics cookies would help us understand how users interact with our website.',
    examples: [],
    noneNote: 'Waddles does not set analytics cookies and does not load third-party analytics scripts. Feature flags are evaluated on our servers (PostHog) using only your workspace (tenant) identifier; nothing is stored in your browser.',
  },
  {
    id: 'marketing_cookies',
    name: 'Marketing Cookies',
    required: false,
    description: 'Marketing cookies would enable personalized advertising and measure advertising campaigns.',
    examples: [],
    noneNote: 'Waddles does not set marketing or advertising cookies.',
  },
];

export default function CookiePreferencesModal() {
  const {
    consent,
    showPreferences,
    setShowPreferences,
    savePreferences,
    acceptAll,
    rejectNonEssential,
  } = useCookieConsentContext();

  const [preferences, setPreferences] = useState({
    essential_cookies: true,
    functional_cookies: false,
    analytics_cookies: false,
    marketing_cookies: false,
  });
  const [saving, setSaving] = useState(false);

  // Initialize preferences from context when modal opens
  useEffect(() => {
    if (showPreferences && consent) {
      setPreferences({
        essential_cookies: consent.essential_cookies || true,
        functional_cookies: consent.functional_cookies || false,
        analytics_cookies: consent.analytics_cookies || false,
        marketing_cookies: consent.marketing_cookies || false,
      });
    }
  }, [showPreferences, consent]);

  if (!showPreferences) return null;

  const handleToggle = (categoryId) => {
    // Prevent toggling essential cookies
    if (categoryId === 'essential_cookies') return;

    setPreferences((prev) => ({
      ...prev,
      [categoryId]: !prev[categoryId],
    }));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await savePreferences(preferences);
    } catch (error) {
      console.error('Failed to save preferences:', error);
    } finally {
      setSaving(false);
    }
  };

  const handleAcceptAll = async () => {
    setSaving(true);
    try {
      await acceptAll();
    } catch (error) {
      console.error('Failed to accept all cookies:', error);
    } finally {
      setSaving(false);
    }
  };

  const handleRejectAll = async () => {
    setSaving(true);
    try {
      await rejectNonEssential();
    } catch (error) {
      console.error('Failed to reject non-essential cookies:', error);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div role="dialog" aria-modal="true" aria-labelledby="cookie-preferences-modal-dialog-1-title" className="bg-surface rounded-lg max-w-2xl w-full max-h-[90vh] overflow-y-auto shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-line sticky top-0 bg-surface">
          <h2 id="cookie-preferences-modal-dialog-1-title" className="text-2xl font-bold text-fg">Cookie Preferences</h2>
          <button
            aria-label="Close"
            onClick={() => setShowPreferences(false)}
            className="p-1 hover:bg-surface-raised rounded-lg transition-colors"
          >
            <XMarkIcon className="w-6 h-6 text-fg-muted hover:text-fg" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-6">
          {/* Introduction */}
          <p className="text-fg-muted text-sm leading-relaxed">
            Cookies are small files stored on your device that help us improve your experience.
            Below you can choose which types of cookies you'd like to accept. Essential cookies
            cannot be disabled as they're required for the site to function properly.
          </p>

          {/* Cookie Categories */}
          <div className="space-y-4">
            {COOKIE_CATEGORIES.map((category) => (
              <div
                key={category.id}
                className="border border-line rounded-lg p-4 hover:border-line-strong transition-colors"
              >
                {/* Category Header with Toggle */}
                <div className="flex items-start justify-between mb-3">
                  <div className="flex-1">
                    <h3 className="font-semibold text-fg">{category.name}</h3>
                    {category.required && (
                      <span className="inline-block mt-1 px-2 py-1 bg-surface-raised text-fg-secondary text-xs font-medium rounded">
                        Required
                      </span>
                    )}
                  </div>

                  {/* Toggle Switch */}
                  <label className="flex items-center cursor-pointer ml-4 flex-shrink-0">
                    <input
                      type="checkbox"
                      checked={preferences[category.id]}
                      onChange={() => handleToggle(category.id)}
                      disabled={category.required}
                      className={`w-5 h-5 rounded border-2 transition-colors ${
                        category.required
                          ? 'bg-navy-700 border-line-strong cursor-not-allowed'
                          : 'bg-surface border-line-strong hover:border-sky-400 cursor-pointer'
                      } ${
                        preferences[category.id]
                          ? 'bg-sky-700 border-sky-500'
                          : ''
                      }`}
                      aria-label={`Toggle ${category.name}`}
                    />
                    <span
                      className={`ml-2 text-sm font-medium ${
                        preferences[category.id]
                          ? 'text-interactive'
                          : 'text-fg-muted'
                      }`}
                    >
                      {preferences[category.id] ? 'Enabled' : 'Disabled'}
                    </span>
                  </label>
                </div>

                {/* Category Description */}
                <p className="text-fg-muted text-sm mb-3">{category.description}</p>

                {/* Cookie Examples */}
                <div className="bg-surface-raised rounded p-3 space-y-2">
                  <p className="text-xs font-semibold text-fg-secondary uppercase">
                    Cookies Set
                  </p>
                  {category.examples.length === 0 && (
                    <p className="text-xs text-fg-muted">{category.noneNote}</p>
                  )}
                  <ul className="space-y-1">
                    {category.examples.map((example, idx) => (
                      <li key={idx} className="text-xs text-fg-muted">
                        <span className="font-mono font-medium text-fg-secondary">
                          {example.name}
                        </span>
                        {' '}
                        —
                        {' '}
                        {example.purpose}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            ))}
          </div>

          {/* Info Box */}
          <div className="bg-info/10 border border-info/30 rounded-lg p-4">
            <p className="text-sm text-info-fg">
              You can change your cookie preferences at any time by visiting our
              {' '}
              <a href="/cookie-policy" className="font-semibold underline hover:no-underline">
                Cookie Policy page
              </a>
              .
            </p>
          </div>
        </div>

        {/* Footer */}
        <div className="p-6 border-t border-line bg-surface-raised space-y-3 sticky bottom-0">
          {/* Quick Action Buttons */}
          <div className="grid grid-cols-2 gap-3 mb-3">
            <button
              onClick={handleRejectAll}
              disabled={saving}
              className="px-4 py-2 border border-line-strong text-fg-secondary rounded-lg font-medium hover:bg-surface-raised transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Reject All
            </button>
            <button
              onClick={handleAcceptAll}
              disabled={saving}
              className="px-4 py-2 bg-sky-700 text-white rounded-lg font-medium hover:bg-sky-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Accept All
            </button>
          </div>

          {/* Save Button */}
          <button
            onClick={handleSave}
            disabled={saving}
            className="w-full px-4 py-3 bg-sky-700 text-white rounded-lg font-semibold hover:bg-sky-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center"
          >
            {saving ? (
              <>
                <div className="animate-spin rounded-full h-4 w-4 border-2 border-white border-t-transparent mr-2"></div>
                Saving...
              </>
            ) : (
              'Save Preferences'
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
