import { useState, useEffect } from 'react';
import { superAdminApi } from '../../services/api';
import { RefreshCw, AlertCircle, Check, X } from 'lucide-react';
import BotCredentialTab from './credentials/BotCredentialTab';
import '../../styles/credentials.css';

export default function SuperAdminPlatformConfig() {
  const [activeTab, setActiveTab] = useState('bot');
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [loading, setLoading] = useState(false);

  // Signup & Auth tab state
  const [settings, setSettings] = useState({});
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);

  const tabs = [
    { id: 'bot', label: 'Bot Credentials', icon: '🤖', description: 'Manage platform bot tokens and credentials' },
    { id: 'signup', label: 'Signup & Auth', icon: '🔑', description: 'Signup settings, CAPTCHA, and passkeys' },
    { id: 'banner', label: 'Site Banner', icon: '📢', description: 'Global announcement banner shown on all pages' },
  ];

  useEffect(() => {
    if (activeTab === 'signup' || activeTab === 'banner') {
      loadSettings();
    }
  }, [activeTab]);

  const loadSettings = async () => {
    setSettingsLoading(true);
    try {
      const res = await superAdminApi.getHubSettings();
      setSettings(res.data?.settings || res.data || {});
    } catch {
      setError('Failed to load settings');
    } finally {
      setSettingsLoading(false);
    }
  };

  const handleSettingChange = (key, value) => {
    setSettings(prev => ({ ...prev, [key]: value }));
  };

  const handleSaveSettings = async () => {
    setSavingSettings(true);
    setError(null);
    try {
      await superAdminApi.updateHubSettings(settings);
      handleSuccess('Settings saved successfully');
    } catch {
      setError('Failed to save settings');
    } finally {
      setSavingSettings(false);
    }
  };

  const handleTabChange = (tabId) => {
    setActiveTab(tabId);
    setError(null);
    setSuccess(null);
  };

  const handleSuccess = (message) => {
    setSuccess(message);
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleError = (message) => {
    setError(message);
    setTimeout(() => setError(null), 5000);
  };

  const currentTab = tabs.find(t => t.id === activeTab);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">
            🔐 Unified Credential Management
          </h1>
          <p className="text-navy-400 mt-1">
            Manage bot credentials and OAuth integrations across all platforms
          </p>
        </div>
        <button
          onClick={() => window.location.reload()}
          className="flex items-center gap-2 px-4 py-2 text-navy-300 hover:bg-navy-700 rounded-lg transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
          Refresh
        </button>
      </div>

      {/* Alerts */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center gap-3">
          <AlertCircle className="w-5 h-5 text-red-400" />
          <span className="text-red-400">{error}</span>
          <button onClick={() => setError(null)} className="ml-auto">
            <X className="w-4 h-4 text-red-400" />
          </button>
        </div>
      )}

      {success && (
        <div className="bg-emerald-500/20 border border-emerald-500/30 rounded-lg p-4 flex items-center gap-3">
          <Check className="w-5 h-5 text-emerald-400" />
          <span className="text-emerald-400">{success}</span>
        </div>
      )}

      {/* Tab Navigation */}
      <div className="card overflow-hidden">
        <div className="flex border-b border-navy-700">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => handleTabChange(tab.id)}
              className={`flex-1 px-6 py-4 text-left transition-all ${
                activeTab === tab.id
                  ? 'border-b-2 border-b-gold-500 bg-navy-800/50'
                  : 'border-b-2 border-b-transparent hover:bg-navy-800/30'
              }`}
            >
              <div className="flex items-center gap-3">
                <span className="text-xl">{tab.icon}</span>
                <div>
                  <h3 className={`font-semibold ${activeTab === tab.id ? 'text-gold-400' : 'text-sky-100'}`}>
                    {tab.label}
                  </h3>
                  <p className="text-xs text-navy-400">{tab.description}</p>
                </div>
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Tab Content */}
      <div className="credential-tab-content">
        {activeTab === 'bot' && (
          <BotCredentialTab
            onSuccess={handleSuccess}
            onError={handleError}
          />
        )}
        {activeTab === 'banner' && (
          <div className="space-y-6">
            {settingsLoading ? (
              <div className="flex items-center justify-center py-12">
                <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-gold-400"></div>
              </div>
            ) : (
              <>
                {/* Enable toggle */}
                <div className="card p-6">
                  <h3 className="text-lg font-semibold text-sky-100 mb-4">Banner Visibility</h3>
                  <label className="flex items-center gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={settings.banner_enabled === 'true' || settings.banner_enabled === true}
                      onChange={(e) => handleSettingChange('banner_enabled', e.target.checked ? 'true' : 'false')}
                      className="w-5 h-5 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
                    />
                    <div>
                      <div className="font-medium text-sky-100">Show banner</div>
                      <div className="text-sm text-navy-400">Display the announcement banner across all pages</div>
                    </div>
                  </label>
                </div>

                {/* Banner text */}
                <div className="card p-6 space-y-4">
                  <h3 className="text-lg font-semibold text-sky-100">Banner Content</h3>
                  <div>
                    <label htmlFor="super-admin-platform-config-banner-text" className="block text-sm font-medium text-sky-200 mb-1">Banner text</label>
                    <input
                      id="super-admin-platform-config-banner-text"
                      type="text"
                      value={settings.banner_text || ''}
                      onChange={(e) => handleSettingChange('banner_text', e.target.value)}
                      className="input w-full"
                      placeholder='e.g. 🚀 We&apos;re in beta! [Learn more](https://docs.example.com)'
                    />
                    <p className="text-xs text-navy-400 mt-1">Supports emoji and [label](url) links</p>
                  </div>

                  {/* Colour pickers */}
                  <div className="flex gap-6 flex-wrap">
                    <div>
                      <label htmlFor="super-admin-platform-config-background-colour" className="block text-sm font-medium text-sky-200 mb-1">Background colour</label>
                      <div className="flex items-center gap-2">
                        <input
                          id="super-admin-platform-config-background-colour"
                          type="color"
                          value={settings.banner_bg_color || '#F5C518'}
                          onChange={(e) => handleSettingChange('banner_bg_color', e.target.value)}
                          className="w-10 h-10 rounded cursor-pointer border border-navy-600"
                        />
                        <span className="text-sm text-navy-300 font-mono">{settings.banner_bg_color || '#F5C518'}</span>
                      </div>
                    </div>
                    <div>
                      <label htmlFor="super-admin-platform-config-text-colour" className="block text-sm font-medium text-sky-200 mb-1">Text colour</label>
                      <div className="flex items-center gap-2">
                        <input
                          id="super-admin-platform-config-text-colour"
                          type="color"
                          value={settings.banner_text_color || '#000000'}
                          onChange={(e) => handleSettingChange('banner_text_color', e.target.value)}
                          className="w-10 h-10 rounded cursor-pointer border border-navy-600"
                        />
                        <span className="text-sm text-navy-300 font-mono">{settings.banner_text_color || '#000000'}</span>
                      </div>
                    </div>
                  </div>
                </div>

                {/* Live preview */}
                {settings.banner_text && (
                  <div className="card p-6">
                    <h3 className="text-lg font-semibold text-sky-100 mb-3">Live Preview</h3>
                    <div
                      style={{
                        backgroundColor: settings.banner_bg_color || '#F5C518',
                        color: settings.banner_text_color || '#000000',
                      }}
                      className="w-full px-4 py-2 text-sm font-medium flex items-center justify-center gap-2 relative rounded"
                    >
                      <span>{settings.banner_text}</span>
                      <span
                        style={{ color: settings.banner_text_color || '#000000', opacity: 0.7 }}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-lg leading-none"
                      >
                        ✕
                      </span>
                    </div>
                    <p className="text-xs text-navy-400 mt-2">Links will be clickable on the live site. The ✕ dismisses the banner per session.</p>
                  </div>
                )}

                <div className="flex justify-end">
                  <button
                    onClick={handleSaveSettings}
                    disabled={savingSettings}
                    className="btn btn-primary"
                  >
                    {savingSettings ? 'Saving...' : 'Save Banner Settings'}
                  </button>
                </div>
              </>
            )}
          </div>
        )}
        {activeTab === 'signup' && (
          <div className="space-y-6">
            {settingsLoading ? (
              <div className="flex items-center justify-center py-12">
                <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-gold-400"></div>
              </div>
            ) : (
              <>
                {/* Signup Toggle */}
                <div className="card p-6">
                  <h3 className="text-lg font-semibold text-sky-100 mb-4">Public Signup</h3>
                  <label className="flex items-center gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={!!settings.allow_public_signup}
                      onChange={(e) => handleSettingChange('allow_public_signup', e.target.checked)}
                      className="w-5 h-5 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
                    />
                    <div>
                      <div className="font-medium text-sky-100">Allow public signup</div>
                      <div className="text-sm text-navy-400">Allow new users to register for an account</div>
                    </div>
                  </label>
                </div>

                {/* CAPTCHA Section */}
                <div className="card p-6">
                  <h3 className="text-lg font-semibold text-sky-100 mb-4">CAPTCHA</h3>
                  <div className="space-y-4">
                    <div>
                      <label htmlFor="super-admin-platform-config-captcha-provider" className="block text-sm font-medium text-sky-200 mb-1">CAPTCHA Provider</label>
                      <select
                        id="super-admin-platform-config-captcha-provider"
                        value={settings.captcha_provider || 'none'}
                        onChange={(e) => handleSettingChange('captcha_provider', e.target.value)}
                        className="input"
                      >
                        <option value="none">None</option>
                        <option value="recaptcha_v2">reCAPTCHA v2</option>
                        <option value="turnstile">Cloudflare Turnstile</option>
                      </select>
                    </div>
                    {settings.captcha_provider && settings.captcha_provider !== 'none' && (
                      <>
                        <div>
                          <label htmlFor="super-admin-platform-config-site-key" className="block text-sm font-medium text-sky-200 mb-1">Site Key</label>
                          <input
                            id="super-admin-platform-config-site-key"
                            type="text"
                            value={settings.captcha_site_key || ''}
                            onChange={(e) => handleSettingChange('captcha_site_key', e.target.value)}
                            className="input"
                            placeholder="Enter site key"
                          />
                        </div>
                        <div>
                          <label htmlFor="super-admin-platform-config-secret-key" className="block text-sm font-medium text-sky-200 mb-1">Secret Key</label>
                          <input
                            id="super-admin-platform-config-secret-key"
                            type="password"
                            value={settings.captcha_secret_key || ''}
                            onChange={(e) => handleSettingChange('captcha_secret_key', e.target.value)}
                            className="input"
                            placeholder="Enter secret key"
                          />
                        </div>
                      </>
                    )}
                  </div>
                </div>

                {/* Passkey Toggle */}
                <div className="card p-6">
                  <h3 className="text-lg font-semibold text-sky-100 mb-4">Passkeys</h3>
                  <label className="flex items-center gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={!!settings.passkey_enabled}
                      onChange={(e) => handleSettingChange('passkey_enabled', e.target.checked)}
                      className="w-5 h-5 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
                    />
                    <div>
                      <div className="font-medium text-sky-100">Enable passkeys</div>
                      <div className="text-sm text-navy-400">Allow users to register and sign in with passkeys (WebAuthn)</div>
                    </div>
                  </label>
                </div>

                <div className="flex justify-end">
                  <button
                    onClick={handleSaveSettings}
                    disabled={savingSettings}
                    className="btn btn-primary"
                  >
                    {savingSettings ? 'Saving...' : 'Save Settings'}
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {/* Info Note */}
      <div className="card p-4 border-l-4 border-l-sky-400">
        <h4 className="font-medium text-sky-300 mb-2">Security Note</h4>
        <p className="text-sm text-sky-400">
          All credentials are encrypted at rest and require authentication to access.
          Database values take precedence over environment variables. Values set through this interface will override any corresponding environment variables. For initial deployment, environment variables can be used as defaults until configured here.
        </p>
      </div>
    </div>
  );
}
