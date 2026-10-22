import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import {
  ServerStackIcon,
  ExclamationTriangleIcon,
  XMarkIcon,
  CheckIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

const PLATFORM_STYLES = {
  discord: { label: 'Discord', badge: 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30' },
  slack: { label: 'Slack', badge: 'bg-green-500/20 text-green-300 border-green-500/30' },
  twitch: { label: 'Twitch', badge: 'bg-purple-500/20 text-purple-300 border-purple-500/30' },
  youtube: { label: 'YouTube', badge: 'bg-red-500/20 text-red-300 border-red-500/30' },
  kick: { label: 'KICK', badge: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' },
};

const PLATFORM_GROUP_ORDER = ['discord', 'slack', 'twitch', 'youtube', 'kick'];

function groupByPlatform(servers) {
  const groups = {};
  for (const server of servers) {
    const p = server.platform?.toLowerCase() || 'other';
    if (!groups[p]) groups[p] = [];
    groups[p].push(server);
  }
  return groups;
}

function AdminPlatformSettings() {
  const { communityId } = useParams();
  const [servers, setServers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [actionLoading, setActionLoading] = useState(null);

  useEffect(() => {
    loadServers();
  }, [communityId]);

  const loadServers = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await adminApi.getServers(communityId);
      setServers(response.data.servers || []);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load linked servers');
    } finally {
      setLoading(false);
    }
  };

  const handleSetDefault = async (serverId) => {
    setActionLoading(serverId);
    try {
      await adminApi.updateServer(communityId, serverId, { isPrimary: true });
      setMessage({ type: 'success', text: 'Default community server updated' });
      loadServers();
    } catch (err) {
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to update server' });
    } finally {
      setActionLoading(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  const groups = groupByPlatform(servers);
  const platforms = PLATFORM_GROUP_ORDER.filter((p) => groups[p]);
  const otherPlatforms = Object.keys(groups).filter((p) => !PLATFORM_GROUP_ORDER.includes(p));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-sky-100">Platform Settings</h1>
        <p className="text-navy-400 mt-1">
          Manage per-server settings for linked platforms. Platform owners manage their connections from
          their personal <span className="text-sky-300">My Channels</span> page.
        </p>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <ExclamationTriangleIcon className="w-5 h-5 text-red-400" />
            <span className="text-red-400">{error}</span>
          </div>
          <button aria-label="Close" onClick={() => setError(null)} className="text-red-400 hover:text-red-300">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {message && (
        <div className={`rounded-lg p-4 flex items-center justify-between ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border border-red-500/30'
        }`}>
          <div className="flex items-center space-x-3">
            <CheckIcon className="w-5 h-5" />
            <span>{message.text}</span>
          </div>
          <button aria-label="Close" onClick={() => setMessage(null)} className="hover:opacity-75">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {servers.length === 0 ? (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
          <ServerStackIcon className="w-12 h-12 text-navy-500 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-sky-100 mb-2">No Linked Servers</h3>
          <p className="text-navy-400">
            No platform servers are linked to this community yet. Platform owners can link their
            servers from the Linked Servers page or their personal My Channels page.
          </p>
        </div>
      ) : (
        <div className="space-y-8">
          {[...platforms, ...otherPlatforms].map((platformKey) => {
            const style = PLATFORM_STYLES[platformKey] || {
              label: platformKey.charAt(0).toUpperCase() + platformKey.slice(1),
              badge: 'bg-navy-600/60 text-navy-300 border-navy-500/30',
            };
            return (
              <div key={platformKey}>
                <div className="flex items-center space-x-2 mb-3">
                  <span className={`px-3 py-1 rounded-full text-sm font-semibold border ${style.badge}`}>
                    {style.label}
                  </span>
                  <span className="text-navy-500 text-sm">
                    {groups[platformKey].length} server{groups[platformKey].length !== 1 ? 's' : ''}
                  </span>
                </div>

                <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
                  <table className="w-full">
                    <thead className="bg-navy-900">
                      <tr>
                        <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Server Name</th>
                        <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Server ID</th>
                        <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Added By</th>
                        <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Default Community</th>
                        <th className="text-right py-3 px-4 text-navy-400 font-medium text-sm">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-navy-700">
                      {groups[platformKey].map((server) => (
                        <tr key={server.id} className="hover:bg-navy-700/50">
                          <td className="py-3 px-4">
                            <span className="font-medium text-sky-100">{server.platformServerName || '(unnamed)'}</span>
                          </td>
                          <td className="py-3 px-4 font-mono text-xs text-navy-400">
                            {server.platformServerId}
                          </td>
                          <td className="py-3 px-4 text-sm text-navy-300">
                            {server.addedBy || '—'}
                          </td>
                          <td className="py-3 px-4">
                            {server.isPrimary ? (
                              <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-gold-500/20 text-gold-400 border border-gold-500/30">
                                Default
                              </span>
                            ) : (
                              <span className="text-navy-500 text-xs">—</span>
                            )}
                          </td>
                          <td className="py-3 px-4 text-right">
                            {!server.isPrimary && (
                              <button
                                onClick={() => handleSetDefault(server.id)}
                                disabled={actionLoading === server.id}
                                className="px-3 py-1.5 bg-navy-700 hover:bg-navy-600 text-sky-100 rounded-lg text-xs transition-colors disabled:opacity-50"
                              >
                                {actionLoading === server.id ? 'Updating...' : 'Set as Default'}
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default AdminPlatformSettings;
