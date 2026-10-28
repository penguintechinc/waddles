import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';

import { getAllPlatformOptions } from '../../utils/platformConfig';

const PLATFORMS = getAllPlatformOptions().map((p) => ({
  id: p.id,
  name: p.label,
  icon: p.icon,
  color: p.color,
}));

function AdminLeaderboardConfig() {
  const { communityId } = useParams();
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    fetchConfig();
  }, [communityId]);

  async function fetchConfig() {
    setLoading(true);
    try {
      const response = await adminApi.getLeaderboardConfig(communityId);
      if (response.data.success) {
        setConfig(response.data.config);
      }
    } catch (err) {
      console.error('Failed to fetch config:', err);
      setMessage({ type: 'error', text: 'Failed to load leaderboard configuration' });
    } finally {
      setLoading(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setMessage(null);
    try {
      await adminApi.updateLeaderboardConfig(communityId, config);
      setMessage({ type: 'success', text: 'Leaderboard configuration saved' });
    } catch (err) {
      console.error('Failed to save config:', err);
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to save configuration' });
    } finally {
      setSaving(false);
    }
  }

  function togglePlatform(platformId) {
    const platforms = config.enabledPlatforms || [];
    const newPlatforms = platforms.includes(platformId)
      ? platforms.filter(p => p !== platformId)
      : [...platforms, platformId];
    setConfig({ ...config, enabledPlatforms: newPlatforms });
  }

  function updateConfig(key, value) {
    setConfig({ ...config, [key]: value });
  }

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  if (!config) {
    return (
      <div className="text-center py-12 text-red-400">
        Failed to load configuration
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Leaderboard Configuration</h1>
          <p className="text-navy-400 mt-1">Configure activity tracking and leaderboard settings</p>
        </div>
        <button
          onClick={handleSave}
          disabled={saving}
          className="btn btn-primary disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
      </div>

      {message && (
        <div className={`mb-6 p-4 rounded-lg border ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border-red-500/30'
        }`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">×</button>
        </div>
      )}

      <div className="space-y-6">
        {/* Leaderboard Types */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Leaderboard Types</h2>
          <div className="space-y-4">
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div className="flex items-center gap-3">
                <svg className="w-6 h-6 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <div>
                  <div className="font-medium text-sky-100">Watch Time Leaderboard</div>
                  <div className="text-sm text-navy-400">Track stream viewing duration</div>
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.watchTimeEnabled}
                onChange={(e) => updateConfig('watchTimeEnabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-purple-500 focus:ring-purple-500"
              />
            </label>

            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div className="flex items-center gap-3">
                <svg className="w-6 h-6 text-sky-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                </svg>
                <div>
                  <div className="font-medium text-sky-100">Messages Leaderboard</div>
                  <div className="text-sm text-navy-400">Track chat message count</div>
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.messagesEnabled}
                onChange={(e) => updateConfig('messagesEnabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>
          </div>
        </div>

        {/* Platform Selection */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-2">Tracked Platforms</h2>
          <p className="text-sm text-navy-400 mb-4">
            Select which platforms count toward your community leaderboards
          </p>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            {PLATFORMS.map((platform) => {
              const isEnabled = (config.enabledPlatforms || []).includes(platform.id);
              return (
                <button
                  key={platform.id}
                  onClick={() => togglePlatform(platform.id)}
                  className={`p-4 rounded-lg border transition-all ${
                    isEnabled
                      ? platform.color
                      : 'bg-navy-800 text-navy-400 border-navy-700 hover:border-navy-500'
                  }`}
                >
                  <div className="flex items-center gap-3">
                    <span className="text-2xl">{platform.icon}</span>
                    <span className="font-medium">{platform.name}</span>
                    {isEnabled && (
                      <svg className="w-5 h-5 ml-auto" fill="currentColor" viewBox="0 0 20 20">
                        <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                      </svg>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Visibility Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Visibility</h2>
          <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
            <div>
              <div className="font-medium text-sky-100">Public Leaderboard</div>
              <div className="text-sm text-navy-400">
                Allow non-members to view leaderboards on the public community page
              </div>
            </div>
            <input
              type="checkbox"
              checked={config.publicLeaderboard}
              onChange={(e) => updateConfig('publicLeaderboard', e.target.checked)}
              className="w-5 h-5 rounded border-navy-600 text-gold-500 focus:ring-gold-500"
            />
          </label>
        </div>

        {/* Thresholds */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-2">Display Thresholds</h2>
          <p className="text-sm text-navy-400 mb-4">
            Minimum requirements to appear on leaderboards (helps filter bots/lurkers)
          </p>
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <label htmlFor="admin-leaderboard-config-minimum-watch-time-minutes" className="block text-sm font-medium text-navy-300 mb-2">
                Minimum Watch Time (minutes)
              </label>
              <input
                id="admin-leaderboard-config-minimum-watch-time-minutes"
                type="number"
                min="0"
                max="1440"
                value={config.minWatchTimeMinutes || 0}
                onChange={(e) => updateConfig('minWatchTimeMinutes', parseInt(e.target.value) || 0)}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <p className="text-xs text-navy-500 mt-1">
                Users need at least this much watch time to appear (0 = no minimum)
              </p>
            </div>
            <div>
              <label htmlFor="admin-leaderboard-config-minimum-message-count" className="block text-sm font-medium text-navy-300 mb-2">
                Minimum Message Count
              </label>
              <input
                id="admin-leaderboard-config-minimum-message-count"
                type="number"
                min="0"
                max="10000"
                value={config.minMessageCount || 0}
                onChange={(e) => updateConfig('minMessageCount', parseInt(e.target.value) || 0)}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <p className="text-xs text-navy-500 mt-1">
                Users need at least this many messages to appear (0 = no minimum)
              </p>
            </div>
          </div>
        </div>

        {/* Display Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Display Settings</h2>
          <div>
            <label htmlFor="admin-leaderboard-config-leaderboard-display-limit" className="block text-sm font-medium text-navy-300 mb-2">
              Leaderboard Display Limit
            </label>
            <select
              id="admin-leaderboard-config-leaderboard-display-limit"
              value={config.displayLimit || 25}
              onChange={(e) => updateConfig('displayLimit', parseInt(e.target.value))}
              className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
            >
              <option value="10">Top 10</option>
              <option value="25">Top 25</option>
              <option value="50">Top 50</option>
              <option value="100">Top 100</option>
            </select>
            <p className="text-xs text-navy-500 mt-1">
              Maximum number of users shown on the full leaderboard page
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

export default AdminLeaderboardConfig;
