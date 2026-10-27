import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { LinkIcon } from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';
import DiscordBotInstallLink from '../../components/settings/DiscordBotInstallLink';

/**
 * Per-community OAuth connections page (gh-320) — lets a community admin
 * link/unlink the platform's shared OAuth apps (YouTube, Spotify, Twitch,
 * Discord, Kick, Slack) for this community. Connect opens a centered popup
 * to the backend-issued `authorize_url`; the popup posts an `OAUTH_CALLBACK`
 * message back to this window on completion (same-origin only) and this
 * page refetches on receipt, with a bounded polling fallback in case the
 * popup is blocked from posting (e.g. third-party cookie/popup restrictions).
 */

const PROVIDERS = {
  youtube: { name: 'YouTube' },
  spotify: { name: 'Spotify' },
  twitch: { name: 'Twitch' },
  discord: { name: 'Discord' },
  kick: { name: 'Kick' },
  slack: { name: 'Slack' },
};

const PROVIDER_ORDER = ['youtube', 'spotify', 'twitch', 'discord', 'kick', 'slack'];

const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 60000;

function formatTimestamp(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString();
}

function CommunityConnections() {
  const { communityId } = useParams();
  const [connections, setConnections] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [actionLoading, setActionLoading] = useState(null);
  const [notConfigured, setNotConfigured] = useState({});

  const popupRef = useRef(null);
  const pollIntervalRef = useRef(null);
  const pollElapsedRef = useRef(0);

  const clearPolling = useCallback(() => {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    pollElapsedRef.current = 0;
  }, []);

  const fetchConnections = useCallback(async () => {
    setError(null);
    try {
      const response = await adminApi.listCommunityConnections(communityId);
      const byProvider = {};
      for (const entry of response.data?.connections || []) {
        byProvider[entry.provider] = entry;
      }
      setConnections(byProvider);
    } catch (err) {
      console.error('[CommunityConnections] Load failed', { communityId, status: err.response?.status });
      setError('Failed to load connections');
    } finally {
      setLoading(false);
    }
  }, [communityId]);

  useEffect(() => {
    setLoading(true);
    fetchConnections();

    const handleMessage = (event) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== 'OAUTH_CALLBACK') return;
      console.debug('[CommunityConnections] OAuth callback received', {
        provider: event.data.provider,
        ok: event.data.ok,
      });
      clearPolling();
      setActionLoading(null);
      if (event.data.ok === false) {
        const providerName = PROVIDERS[event.data.provider]?.name || event.data.provider;
        setMessage({ type: 'error', text: `Failed to connect ${providerName}` });
      } else {
        setMessage(null);
      }
      fetchConnections();
    };

    window.addEventListener('message', handleMessage);
    return () => {
      window.removeEventListener('message', handleMessage);
      clearPolling();
    };
  }, [communityId, fetchConnections, clearPolling]);

  function startPollingFallback(provider) {
    clearPolling();
    pollIntervalRef.current = setInterval(() => {
      pollElapsedRef.current += POLL_INTERVAL_MS;
      const popup = popupRef.current;
      if (!popup || popup.closed || pollElapsedRef.current >= POLL_TIMEOUT_MS) {
        clearPolling();
        setActionLoading((current) => (current === provider ? null : current));
        fetchConnections();
      }
    }, POLL_INTERVAL_MS);
  }

  async function handleConnect(provider) {
    setActionLoading(provider);
    setMessage(null);
    setNotConfigured((prev) => ({ ...prev, [provider]: false }));
    try {
      const response = await adminApi.authorizeCommunityConnection(communityId, provider);
      const { authorize_url: authorizeUrl } = response.data;
      const width = 600;
      const height = 750;
      const left = window.screenX + (window.outerWidth - width) / 2;
      const top = window.screenY + (window.outerHeight - height) / 2;
      popupRef.current = window.open(
        authorizeUrl,
        'waddlebot-oauth',
        `width=${width},height=${height},left=${left},top=${top}`,
      );
      startPollingFallback(provider);
    } catch (err) {
      console.error('[CommunityConnections] Authorize failed', { communityId, provider, status: err.response?.status });
      if (err.response?.status === 503 && err.response?.data?.error === 'provider_not_configured') {
        setNotConfigured((prev) => ({ ...prev, [provider]: true }));
      } else {
        setMessage({ type: 'error', text: `Failed to start ${PROVIDERS[provider].name} connection` });
      }
      setActionLoading(null);
    }
  }

  async function handleDisconnect(provider) {
    const providerName = PROVIDERS[provider].name;
    if (!confirm(`Disconnect ${providerName}? This community will lose access until it's reconnected.`)) {
      return;
    }
    setActionLoading(provider);
    setMessage(null);
    try {
      await adminApi.disconnectCommunityConnection(communityId, provider);
      setMessage({ type: 'success', text: `${providerName} disconnected` });
      await fetchConnections();
    } catch (err) {
      console.error('[CommunityConnections] Disconnect failed', { communityId, provider, status: err.response?.status });
      setMessage({ type: 'error', text: `Failed to disconnect ${providerName}` });
    } finally {
      setActionLoading(null);
    }
  }

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
          <LinkIcon className="w-8 h-8" />
          Connections
        </h1>
        <p className="text-navy-400 mt-1">
          Link this community&apos;s OAuth accounts to the platforms it uses
        </p>
      </div>

      {error && (
        <div
          data-testid="connections-error"
          className="p-4 rounded-lg border bg-red-500/20 text-red-300 border-red-500/30"
        >
          {error}
        </div>
      )}

      {message && (
        <div
          data-testid="connections-feedback"
          className={`p-4 rounded-lg border ${
            message.type === 'success'
              ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
              : 'bg-red-500/20 text-red-300 border-red-500/30'
          }`}
        >
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right" aria-label="Dismiss message">
            x
          </button>
        </div>
      )}

      <DiscordBotInstallLink />

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {PROVIDER_ORDER.map((provider) => {
          const info = PROVIDERS[provider];
          const connection = connections[provider] || {
            connected: false,
            scopes: [],
            expires_at: null,
            updated_at: null,
            account_label: null,
          };
          const isBusy = actionLoading === provider;
          const expiresAt = formatTimestamp(connection.expires_at);
          const updatedAt = formatTimestamp(connection.updated_at);

          return (
            <div
              key={provider}
              data-testid={`connection-card-${provider}`}
              className="bg-navy-800 border border-navy-700 rounded-lg p-6 hover:border-navy-600 transition-colors"
            >
              <div className="flex items-start justify-between mb-4">
                <h3 className="font-semibold text-sky-100">{info.name}</h3>
                <span
                  data-testid={`connection-status-${provider}`}
                  className={`inline-block px-3 py-1 rounded-full text-xs font-medium border ${
                    connection.connected
                      ? 'bg-green-500/20 text-green-400 border-green-500/30'
                      : 'bg-red-500/20 text-red-400 border-red-500/30'
                  }`}
                >
                  {connection.connected
                    ? `Connected as ${connection.account_label || 'unknown account'}`
                    : 'Not connected'}
                </span>
              </div>

              {connection.scopes?.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mb-3">
                  {connection.scopes.map((scope) => (
                    <span
                      key={scope}
                      className="px-2 py-0.5 rounded-full text-xs font-medium bg-navy-900 text-navy-300 border border-navy-700"
                    >
                      {scope}
                    </span>
                  ))}
                </div>
              )}

              <div className="space-y-1 text-xs text-navy-400 mb-4">
                {expiresAt && <div>Expires {expiresAt}</div>}
                {updatedAt && <div>Updated {updatedAt}</div>}
              </div>

              {notConfigured[provider] && (
                <div
                  data-testid={`connection-not-configured-${provider}`}
                  className="mb-4 p-3 bg-yellow-500/10 rounded border border-yellow-500/20 text-xs text-yellow-300"
                >
                  Ask the platform admin to configure the {info.name} OAuth client
                </div>
              )}

              <div className="flex gap-2">
                <button
                  onClick={() => handleConnect(provider)}
                  disabled={isBusy}
                  data-testid={`connection-connect-${provider}`}
                  aria-label={`${connection.connected ? 'Reconnect' : 'Connect'} ${info.name}`}
                  className="flex-1 btn btn-primary flex items-center justify-center gap-2 text-sm disabled:opacity-50"
                >
                  <LinkIcon className="w-4 h-4" />
                  {isBusy
                    ? 'Connecting...'
                    : connection.connected
                      ? 'Reconnect'
                      : 'Connect'}
                </button>
                {connection.connected && (
                  <button
                    onClick={() => handleDisconnect(provider)}
                    disabled={isBusy}
                    data-testid={`connection-disconnect-${provider}`}
                    aria-label={`Disconnect ${info.name}`}
                    className="flex-1 btn bg-red-500/20 text-red-300 hover:bg-red-500/30 text-sm disabled:opacity-50"
                  >
                    {isBusy ? 'Disconnecting...' : 'Disconnect'}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default CommunityConnections;
