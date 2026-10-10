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
      <div className="bg-white rounded-lg max-w-2xl w-full max-h-[90vh] overflow-y-auto shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-gray-200 sticky top-0 bg-white">
          <h2 className="text-2xl font-bold text-gray-900">Cookie Preferences</h2>
          <button
            onClick={() => setShowPreferences(false)}
            className="p-1 hover:bg-gray-100 rounded-lg transition-colors"
          >
            <XMarkIcon className="w-6 h-6 text-gray-600 hover:text-gray-900" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-6">
          {/* Introduction */}
          <p className="text-gray-600 text-sm leading-relaxed">
            Cookies are small files stored on your device that help us improve your experience.
            Below you can choose which types of cookies you'd like to accept. Essential cookies
            cannot be disabled as they're required for the site to function properly.
          </p>

          {/* Cookie Categories */}
          <div className="space-y-4">
            {COOKIE_CATEGORIES.map((category) => (
              <div
                key={category.id}
                className="border border-gray-200 rounded-lg p-4 hover:border-gray-300 transition-colors"
              >
                {/* Category Header with Toggle */}
                <div className="flex items-start justify-between mb-3">
                  <div className="flex-1">
                    <h3 className="font-semibold text-gray-900">{category.name}</h3>
                    {category.required && (
                      <span className="inline-block mt-1 px-2 py-1 bg-gray-100 text-gray-700 text-xs font-medium rounded">
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
                          ? 'bg-gray-200 border-gray-300 cursor-not-allowed'
                          : 'bg-white border-gray-300 hover:border-blue-500 cursor-pointer'
                      } ${
                        preferences[category.id]
                          ? 'bg-blue-600 border-blue-600'
                          : ''
                      }`}
                      aria-label={`Toggle ${category.name}`}
                    />
                    <span
                      className={`ml-2 text-sm font-medium ${
                        preferences[category.id]
                          ? 'text-blue-600'
                          : 'text-gray-500'
                      }`}
                    >
                      {preferences[category.id] ? 'Enabled' : 'Disabled'}
                    </span>
                  </label>
                </div>

                {/* Category Description */}
                <p className="text-gray-600 text-sm mb-3">{category.description}</p>

                {/* Cookie Examples */}
                <div className="bg-gray-50 rounded p-3 space-y-2">
                  <p className="text-xs font-semibold text-gray-700 uppercase">
                    Cookies Set
                  </p>
                  {category.examples.length === 0 && (
                    <p className="text-xs text-gray-600">{category.noneNote}</p>
                  )}
                  <ul className="space-y-1">
                    {category.examples.map((example, idx) => (
                      <li key={idx} className="text-xs text-gray-600">
                        <span className="font-mono font-medium text-gray-700">
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
          <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
            <p className="text-sm text-blue-900">
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
        <div className="p-6 border-t border-gray-200 bg-gray-50 space-y-3 sticky bottom-0">
          {/* Quick Action Buttons */}
          <div className="grid grid-cols-2 gap-3 mb-3">
            <button
              onClick={handleRejectAll}
              disabled={saving}
              className="px-4 py-2 border border-gray-300 text-gray-700 rounded-lg font-medium hover:bg-gray-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Reject All
            </button>
            <button
              onClick={handleAcceptAll}
              disabled={saving}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Accept All
            </button>
          </div>

          {/* Save Button */}
          <button
            onClick={handleSave}
            disabled={saving}
            className="w-full px-4 py-3 bg-blue-600 text-white rounded-lg font-semibold hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center"
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
