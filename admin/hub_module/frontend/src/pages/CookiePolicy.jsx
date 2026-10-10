/**
 * Cookie Policy Page
 * Displays comprehensive cookie policy with all categories and specific cookies used
 */
import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeftIcon } from '@heroicons/react/24/outline';
import api from '../services/api';
import { useCookieConsentContext } from '../contexts/CookieConsentContext';

const COOKIE_TABLE_DATA = [
  {
    category: 'Essential',
    cookies: [
      {
        name: 'wb_session',
        purpose: 'Keeps you signed in. HttpOnly, Secure, SameSite=Lax; never readable by page scripts',
        duration: '24 hours',
      },
      {
        name: 'waddlebot_consent_id',
        purpose: 'Links this browser to your saved cookie-consent record (an opaque identifier)',
        duration: '12 months',
      },
      {
        name: 'wb_sso_bind',
        purpose: 'Secures a single sign-on login attempt against forged logins. Only set while signing in',
        duration: 'A few minutes (the length of the sign-in attempt)',
      },
    ],
  },
  {
    category: 'Functional',
    cookies: [],
    notice: 'Waddles does not currently set any functional cookies.',
  },
  {
    category: 'Analytics',
    cookies: [],
    notice:
      'Waddles does not set analytics cookies and does not load third-party analytics scripts '
      + '(no Google Analytics, Mixpanel, or session-recording tools). Feature flags are evaluated on '
      + 'our own servers using PostHog with only your workspace (tenant) identifier; no cookie is set '
      + 'and no personal data is sent to PostHog.',
  },
  {
    category: 'Marketing',
    cookies: [],
    notice: 'Waddles does not set marketing or advertising cookies.',
  },
];

export default function CookiePolicy() {
  const [policy, setPolicy] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const { openPreferences } = useCookieConsentContext();

  // Fetch cookie policy from API
  useEffect(() => {
    const fetchPolicy = async () => {
      setLoading(true);
      try {
        const response = await api.get('/api/v1/cookie/policy');
        if (response.data?.success) {
          setPolicy(response.data.policy);
        }
        setError(null);
      } catch (err) {
        console.error('Failed to fetch cookie policy:', err);
        setError(err.message);
        // Set default policy values on error
        setPolicy({
          version: '1.0',
          lastUpdated: new Date().toISOString(),
        });
      } finally {
        setLoading(false);
      }
    };

    fetchPolicy();
  }, []);

  const formatDate = (dateString) => {
    if (!dateString) return 'N/A';
    return new Date(dateString).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600"></div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <div className="bg-white border-b border-gray-200">
        <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          {/* Back Navigation */}
          <Link
            to="/"
            className="inline-flex items-center text-blue-600 hover:text-blue-700 font-medium mb-4"
          >
            <ArrowLeftIcon className="w-4 h-4 mr-2" />
            Back to Home
          </Link>

          <h1 className="text-4xl font-bold text-gray-900 mb-2">
            Cookie Policy
          </h1>
          <p className="text-gray-600">
            Learn about the cookies we use and how to manage your preferences
          </p>
        </div>
      </div>

      {/* Main Content */}
      <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-12">
        {error && !policy && (
          <div className="mb-8 bg-red-50 border border-red-200 rounded-lg p-4">
            <p className="text-red-900 text-sm">
              {error}
            </p>
          </div>
        )}

        {/* Introduction Section */}
        <section className="mb-12">
          <h2 className="text-2xl font-bold text-gray-900 mb-4">
            What Are Cookies?
          </h2>
          <div className="prose prose-sm max-w-none text-gray-600 space-y-4">
            <p>
              Cookies are small text files that are placed on your device when you visit our website.
              They are widely used to make websites work more efficiently, provide information to the
              owners of the site, and help improve your user experience.
            </p>
            <p>
              We use cookies only to keep you signed in, secure sign-in, and remember your cookie
              choices. We do not use advertising cookies and we do not load third-party analytics
              scripts. Some cookies are deleted when you close your web browser (session cookies),
              while others persist for a set period (persistent cookies).
            </p>
          </div>
        </section>

        {/* Cookie Categories Section */}
        <section className="mb-12">
          <h2 className="text-2xl font-bold text-gray-900 mb-6">
            Cookie Categories
          </h2>

          <div className="space-y-8">
            {/* Essential Cookies */}
            <div className="bg-white rounded-lg border border-gray-200 p-6">
              <div className="flex items-start justify-between mb-3">
                <h3 className="text-xl font-semibold text-gray-900">
                  Essential Cookies
                </h3>
                <span className="px-3 py-1 bg-red-100 text-red-800 text-xs font-semibold rounded-full">
                  Required
                </span>
              </div>
              <p className="text-gray-600 mb-4">
                These cookies are necessary for the website to function and cannot be disabled.
                They enable critical functionality such as authentication, security, and access control.
                Without these cookies, our website cannot operate properly.
              </p>
              <div className="text-sm text-gray-500">
                You cannot opt out of essential cookies as they are required for site functionality.
              </div>
            </div>

            {/* Functional Cookies */}
            <div className="bg-white rounded-lg border border-gray-200 p-6">
              <div className="flex items-start justify-between mb-3">
                <h3 className="text-xl font-semibold text-gray-900">
                  Functional Cookies
                </h3>
                <span className="px-3 py-1 bg-blue-100 text-blue-800 text-xs font-semibold rounded-full">
                  Optional
                </span>
              </div>
              <p className="text-gray-600 mb-4">
                Functional cookies improve your user experience by remembering your preferences and
                settings across visits. These allow us to maintain your chosen theme, language,
                and layout preferences so you don't have to set them again.
              </p>
              <div className="text-sm text-gray-500">
                You can disable functional cookies, but this may reduce your experience on our website.
              </div>
            </div>

            {/* Analytics Cookies */}
            <div className="bg-white rounded-lg border border-gray-200 p-6">
              <div className="flex items-start justify-between mb-3">
                <h3 className="text-xl font-semibold text-gray-900">
                  Analytics Cookies
                </h3>
                <span className="px-3 py-1 bg-green-100 text-green-800 text-xs font-semibold rounded-full">
                  Optional
                </span>
              </div>
              <p className="text-gray-600 mb-4">
                Analytics cookies help us understand how users interact with our website, including
                which pages are most visited, how long users spend on pages, and what actions they take.
                This information helps us improve our features, performance, and overall service.
              </p>
              <div className="text-sm text-gray-500">
                You can disable analytics cookies if you prefer not to share usage data.
              </div>
            </div>

            {/* Marketing Cookies */}
            <div className="bg-white rounded-lg border border-gray-200 p-6">
              <div className="flex items-start justify-between mb-3">
                <h3 className="text-xl font-semibold text-gray-900">
                  Marketing Cookies
                </h3>
                <span className="px-3 py-1 bg-purple-100 text-purple-800 text-xs font-semibold rounded-full">
                  Optional
                </span>
              </div>
              <p className="text-gray-600 mb-4">
                Marketing cookies enable personalized advertising and help us measure the effectiveness
                of our advertising campaigns. They track your behavior across different websites to
                show you relevant ads and measure conversion actions.
              </p>
              <div className="text-sm text-gray-500">
                You can disable marketing cookies to opt out of personalized advertising.
              </div>
            </div>
          </div>
        </section>

        {/* Cookie Details Table */}
        <section className="mb-12">
          <h2 className="text-2xl font-bold text-gray-900 mb-6">
            Specific Cookies Used
          </h2>

          <div className="space-y-8">
            {COOKIE_TABLE_DATA.map((categoryData) => (
              <div key={categoryData.category} className="overflow-x-auto">
                <h3 className="text-lg font-semibold text-gray-900 mb-3">
                  {categoryData.category} Cookies
                </h3>
                {categoryData.cookies.length === 0 ? (
                  <p className="text-sm text-gray-600">{categoryData.notice}</p>
                ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-100 border-b border-gray-200">
                      <th className="px-4 py-3 text-left font-semibold text-gray-900">
                        Cookie Name
                      </th>
                      <th className="px-4 py-3 text-left font-semibold text-gray-900">
                        Purpose
                      </th>
                      <th className="px-4 py-3 text-left font-semibold text-gray-900">
                        Duration
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {categoryData.cookies.map((cookie) => (
                      <tr key={cookie.name} className="bg-white hover:bg-gray-50 transition-colors">
                        <td className="px-4 py-3">
                          <code className="font-mono text-gray-900 bg-gray-100 px-2 py-1 rounded text-xs">
                            {cookie.name}
                          </code>
                        </td>
                        <td className="px-4 py-3 text-gray-600">
                          {cookie.purpose}
                        </td>
                        <td className="px-4 py-3 text-gray-600">
                          {cookie.duration}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                )}
              </div>
            ))}
          </div>
        </section>

        {/* Manage Preferences Section */}
        <section className="mb-12 bg-blue-50 border border-blue-200 rounded-lg p-8">
          <h2 className="text-2xl font-bold text-gray-900 mb-4">
            Manage Your Cookie Preferences
          </h2>
          <p className="text-gray-700 mb-6">
            You can update your cookie preferences at any time. Click the button below to open
            the cookie preferences modal where you can enable or disable optional cookie categories.
            Note that essential cookies cannot be disabled as they are required for the site to function.
          </p>
          <button
            onClick={openPreferences}
            className="inline-flex items-center px-6 py-3 bg-blue-600 text-white font-semibold rounded-lg hover:bg-blue-700 transition-colors"
          >
            Update Cookie Preferences
          </button>
        </section>

        {/* How to Delete Cookies */}
        <section className="mb-12">
          <h2 className="text-2xl font-bold text-gray-900 mb-4">
            How to Delete Cookies from Your Browser
          </h2>
          <p className="text-gray-600 mb-6">
            You can manage and delete cookies using your browser's settings. Here's how to do it
            in the most common browsers:
          </p>

          <div className="grid md:grid-cols-2 gap-6">
            {[
              {
                browser: 'Chrome',
                steps: [
                  'Open Chrome and click the menu icon (three lines)',
                  'Select Settings',
                  'Click Privacy and security in the left sidebar',
                  'Click Clear browsing data',
                  'Select "Cookies and other site data"',
                  'Click Clear data',
                ],
              },
              {
                browser: 'Firefox',
                steps: [
                  'Open Firefox and click the menu button (three horizontal lines)',
                  'Select Settings',
                  'Click Privacy & Security',
                  'Under Cookies and Site Data, click Clear Data',
                  'Check "Cookies and Site Data"',
                  'Click Clear',
                ],
              },
              {
                browser: 'Safari',
                steps: [
                  'Open Safari and click Safari in the menu',
                  'Select Preferences',
                  'Click Privacy',
                  'Click Manage Website Data',
                  'Select cookies you want to remove',
                  'Click Remove',
                ],
              },
              {
                browser: 'Edge',
                steps: [
                  'Open Edge and click the menu icon (three dots)',
                  'Select Settings',
                  'Click Privacy, search, and services',
                  'Under Clear browsing data, click Choose what to clear',
                  'Select "Cookies and other site data"',
                  'Click Clear',
                ],
              },
            ].map((item) => (
              <div key={item.browser} className="bg-white border border-gray-200 rounded-lg p-6">
                <h3 className="font-semibold text-gray-900 mb-3">{item.browser}</h3>
                <ol className="list-decimal list-inside space-y-2 text-sm text-gray-600">
                  {item.steps.map((step, idx) => (
                    <li key={idx}>{step}</li>
                  ))}
                </ol>
              </div>
            ))}
          </div>
        </section>

        {/* Contact Section */}
        <section className="mb-12">
          <h2 className="text-2xl font-bold text-gray-900 mb-4">
            Questions About Cookies?
          </h2>
          <p className="text-gray-600 mb-4">
            If you have any questions about our cookie usage or privacy practices, please contact us:
          </p>
          <div className="bg-white border border-gray-200 rounded-lg p-6">
            <p className="text-gray-900">
              Email:
              {' '}
              <a
                href="mailto:support@waddlebot.com"
                className="font-medium text-blue-600 hover:text-blue-700"
              >
                support@waddlebot.com
              </a>
            </p>
          </div>
        </section>

        {/* Policy Version */}
        <section className="border-t border-gray-200 pt-8 text-sm text-gray-500">
          <p>
            <strong>Policy Version:</strong>
            {' '}
            {policy?.version || '1.0'}
          </p>
          <p>
            <strong>Last Updated:</strong>
            {' '}
            {formatDate(policy?.lastUpdated)}
          </p>
        </section>
      </div>
    </div>
  );
}
