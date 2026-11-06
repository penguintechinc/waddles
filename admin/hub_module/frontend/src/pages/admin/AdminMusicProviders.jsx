import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import {
  MusicalNoteIcon,
  CheckCircleIcon,
  XCircleIcon,
  ExclamationTriangleIcon,
  ClockIcon,
  LinkIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';
import { NotAvailableYet } from '../../components/ui';
import { isFeatureServed } from '../../lib/unservedFeatures';

/** hub-api has no `PUT .../music/providers/:provider/config` route yet. */
const PROVIDER_CONFIG_FEATURE = 'music-provider-config';

const MUSIC_PROVIDERS = {
  spotify: {
    name: 'Spotify',
    icon: '🎵',
    color: 'bg-green-500/20 text-green-300 border-green-500/30',
    description: 'Stream music directly from Spotify',
  },
  youtube: {
    name: 'YouTube Music',
    icon: '▶️',
    color: 'bg-red-500/20 text-red-300 border-red-500/30',
    description: 'Access your YouTube music library',
  },
  soundcloud: {
    name: 'SoundCloud',
    icon: '☁️',
    color: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
    description: 'Discover music from SoundCloud',
  },
};

function AdminMusicProviders() {
  const { communityId } = useParams();
  const [providers, setProviders] = useState({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [actionLoading, setActionLoading] = useState(null);
  const [message, setMessage] = useState(null);
  const [oauthWindow, setOauthWindow] = useState(null);

  useEffect(() => {
    loadProviders();
    // Listen for OAuth callback
    const handleMessage = (event) => {
      if (event.data.type === 'OAUTH_CALLBACK') {
        loadProviders();
      }
    };
    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [communityId]);

  const loadProviders = async () => {
    try {
      setLoading(true);
      setMessage(null);
      const response = await adminApi.getMusicProviders(communityId);
      setProviders(response.data.providers || {});
    } catch (err) {
      console.error('Failed to load music providers:', err);
      setMessage({
        type: 'error',
        text: err.response?.data?.error?.message || 'Failed to load music providers',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleConnect = async (providerName) => {
    setActionLoading(providerName);
    try {
      setMessage(null);
      const response = await adminApi.initiateMusicProviderOAuth(communityId, providerName);
      const { authUrl } = response.data;
      // Open OAuth window
      const width = 500;
      const height = 600;
      const left = window.screenX + (window.outerWidth - width) / 2;
      const top = window.screenY + (window.outerHeight - height) / 2;
      const popup = window.open(
        authUrl,
        `${providerName}_oauth`,
        `width=${width},height=${height},left=${left},top=${top}`
      );
      setOauthWindow(popup);
      // Poll for completion
      const pollInterval = setInterval(() => {
        if (popup?.closed) {
          clearInterval(pollInterval);
          loadProviders();
          setActionLoading(null);
        }
      }, 500);
    } catch (err) {
      console.error('Failed to initiate OAuth:', err);
      setMessage({
        type: 'error',
        text: err.response?.data?.error?.message || 'Failed to initiate connection',
      });
      setActionLoading(null);
    }
  };

  const handleDisconnect = async (providerName) => {
    if (!confirm(`Are you sure you want to disconnect ${MUSIC_PROVIDERS[providerName]?.name}?`)) {
      return;
    }
    setActionLoading(providerName);
    try {
      setMessage(null);
      await adminApi.disconnectMusicProvider(communityId, providerName);
      setMessage({
        type: 'success',
        text: `${MUSIC_PROVIDERS[providerName]?.name} disconnected successfully`,
      });
      loadProviders();
    } catch (err) {
      console.error('Failed to disconnect provider:', err);
      setMessage({
        type: 'error',
        text: err.response?.data?.error?.message || 'Failed to disconnect provider',
      });
    } finally {
      setActionLoading(null);
    }
  };

  const handleToggleEnabled = async (providerName, enabled) => {
    setActionLoading(providerName);
    try {
      setMessage(null);
      await adminApi.updateMusicProviderConfig(communityId, providerName, { enabled });
      setMessage({
        type: 'success',
        text: `${MUSIC_PROVIDERS[providerName]?.name} ${enabled ? 'enabled' : 'disabled'}`,
      });
      loadProviders();
    } catch (err) {
      console.error('Failed to update provider config:', err);
      setMessage({
        type: 'error',
        text: err.response?.data?.error?.message || 'Failed to update provider settings',
      });
    } finally {
      setActionLoading(null);
    }
  };

  const getStatusIcon = (provider) => {
    if (!provider?.connected) {
      return <XCircleIcon className="w-6 h-6 text-red-400" />;
    }
    if (provider?.tokenExpired) {
      return <ExclamationTriangleIcon className="w-6 h-6 text-yellow-400" />;
    }
    return <CheckCircleIcon className="w-6 h-6 text-green-400" />;
  };

  const getStatusBadge = (provider) => {
    if (!provider?.connected) {
      return (
        <span className="inline-block px-3 py-1 rounded-full text-xs font-medium bg-red-500/20 text-red-400 border border-red-500/30">
          Not Connected
        </span>
      );
    }
    if (provider?.tokenExpired) {
      return (
        <span className="inline-block px-3 py-1 rounded-full text-xs font-medium bg-yellow-500/20 text-yellow-400 border border-yellow-500/30">
          Token Expired
        </span>
      );
    }
    return (
      <span className="inline-block px-3 py-1 rounded-full text-xs font-medium bg-green-500/20 text-green-400 border border-green-500/30">
        Connected
      </span>
    );
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-sky-100 flex items-center gap-3">
          <MusicalNoteIcon className="w-8 h-8" />
          Music Providers
        </h1>
        <p className="text-navy-400 mt-1">
          Connect and manage music streaming services for your community
        </p>
      </div>
      {!isFeatureServed(PROVIDER_CONFIG_FEATURE) && <NotAvailableYet feature={PROVIDER_CONFIG_FEATURE} />}

      {message && (
        <div
          className={`p-4 rounded-lg border ${
            message.type === 'success'
              ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
              : 'bg-red-500/20 text-red-300 border-red-500/30'
          }`}
        >
          {message.text}
          <button
            onClick={() => setMessage(null)}
            className="float-right"
          >
            ×
          </button>
        </div>
      )}

      {/* Providers Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {Object.entries(MUSIC_PROVIDERS).map(([key, providerInfo]) => {
          const provider = providers[key] || {};
          return (
            <div
              key={key}
              className="bg-navy-800 border border-navy-700 rounded-lg p-6 hover:border-navy-600 transition-colors"
            >
              {/* Header with Icon and Status */}
              <div className="flex items-start justify-between mb-4">
                <div className="flex items-center space-x-3">
                  <span className="text-3xl">{providerInfo.icon}</span>
                  <div>
                    <h3 className="font-semibold text-sky-100">{providerInfo.name}</h3>
                    <p className="text-xs text-navy-400 mt-1">{providerInfo.description}</p>
                  </div>
                </div>
                {getStatusIcon(provider)}
              </div>

              {/* Status Information */}
              <div className="space-y-3 mb-4">
                <div className="flex justify-between items-center">
                  <span className="text-navy-400 text-sm">Status</span>
                  {getStatusBadge(provider)}
                </div>

                {provider?.connected && provider?.connectedAt && (
                  <div className="flex justify-between items-center text-sm">
                    <span className="text-navy-400">Connected</span>
                    <span className="text-sky-300">
                      {new Date(provider.connectedAt).toLocaleDateString()}
                    </span>
                  </div>
                )}

                {provider?.tokenExpired && (
                  <div className="flex items-start space-x-2 p-3 bg-yellow-500/10 rounded border border-yellow-500/20">
                    <ClockIcon className="w-4 h-4 text-yellow-400 flex-shrink-0 mt-0.5" />
                    <div className="text-xs text-yellow-300">
                      Your {providerInfo.name} token has expired. Reconnect to continue.
                    </div>
                  </div>
                )}

                {provider?.connected && !provider?.tokenExpired && (
                  <div className="flex justify-between items-center">
                    <span className="text-navy-400 text-sm">Account</span>
                    <span className="text-sky-300 text-sm">
                      {provider?.accountName || 'Verified'}
                    </span>
                  </div>
                )}
              </div>

              {/* Enable/Disable Toggle (only if connected) */}
              {provider?.connected && isFeatureServed(PROVIDER_CONFIG_FEATURE) && (
                <label className="flex items-center justify-between p-3 bg-navy-900 rounded-lg cursor-pointer mb-4 border border-navy-700">
                  <span className="text-sm font-medium text-sky-100">Enable Provider</span>
                  <input
                    type="checkbox"
                    checked={provider?.enabled || false}
                    onChange={(e) => handleToggleEnabled(key, e.target.checked)}
                    disabled={actionLoading === key}
                    className="w-4 h-4 rounded border-navy-600 text-sky-500 focus:ring-sky-500 disabled:opacity-50"
                  />
                </label>
              )}

              {/* Action Buttons */}
              <div className="flex space-x-2">
                {!provider?.connected ? (
                  <button
                    onClick={() => handleConnect(key)}
                    disabled={actionLoading === key}
                    className="flex-1 btn btn-primary flex items-center justify-center gap-2 text-sm disabled:opacity-50"
                  >
                    <LinkIcon className="w-4 h-4" />
                    {actionLoading === key ? 'Connecting...' : 'Connect'}
                  </button>
                ) : provider?.tokenExpired ? (
                  <button
                    onClick={() => handleConnect(key)}
                    disabled={actionLoading === key}
                    className="flex-1 btn btn-primary flex items-center justify-center gap-2 text-sm disabled:opacity-50"
                  >
                    <ArrowPathIcon className="w-4 h-4" />
                    {actionLoading === key ? 'Reconnecting...' : 'Reconnect'}
                  </button>
                ) : (
                  <button
                    onClick={() => handleDisconnect(key)}
                    disabled={actionLoading === key}
                    className="flex-1 btn bg-red-500/20 text-red-300 hover:bg-red-500/30 text-sm disabled:opacity-50"
                  >
                    {actionLoading === key ? 'Disconnecting...' : 'Disconnect'}
                  </button>
                )}
              </div>

              {/* Provider Status Indicator */}
              {provider?.connected && (
                <div className="mt-4 pt-4 border-t border-navy-700">
                  <div className="flex items-center space-x-2">
                    <div className="w-2 h-2 rounded-full bg-green-400"></div>
                    <span className="text-xs text-navy-400">
                      {provider?.enabled ? 'Enabled & Active' : 'Connected but disabled'}
                    </span>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Info Section */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
        <h3 className="text-lg font-semibold text-sky-100 mb-4">About Music Providers</h3>
        <div className="space-y-3 text-sm text-navy-300">
          <div>
            <h4 className="font-medium text-sky-100 mb-1">Connection Security</h4>
            <p>We use OAuth 2.0 to securely connect to music providers. Your credentials are never stored on our servers.</p>
          </div>
          <div>
            <h4 className="font-medium text-sky-100 mb-1">Token Expiration</h4>
            <p>Music provider tokens may expire periodically. If your connection shows as expired, simply reconnect using your music provider account.</p>
          </div>
          <div>
            <h4 className="font-medium text-sky-100 mb-1">Per-Provider Control</h4>
            <p>Enable or disable any provider independently. Disabled providers won't be available for music requests, but your connection will be preserved.</p>
          </div>
        </div>
      </div>

      {/* Status Summary */}
      {Object.keys(providers).length > 0 && (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
          <h3 className="text-lg font-semibold text-sky-100 mb-4">Connection Summary</h3>
          <div className="grid grid-cols-3 gap-4">
            <div className="text-center">
              <div className="text-2xl font-bold text-gold-400">
                {Object.values(providers).filter((p) => p?.connected).length}
              </div>
              <div className="text-sm text-navy-400 mt-1">Connected</div>
            </div>
            <div className="text-center">
              <div className="text-2xl font-bold text-green-400">
                {Object.values(providers).filter((p) => p?.connected && p?.enabled).length}
              </div>
              <div className="text-sm text-navy-400 mt-1">Enabled</div>
            </div>
            <div className="text-center">
              <div className="text-2xl font-bold text-yellow-400">
                {Object.values(providers).filter((p) => p?.tokenExpired).length}
              </div>
              <div className="text-sm text-navy-400 mt-1">Expired Tokens</div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminMusicProviders;
