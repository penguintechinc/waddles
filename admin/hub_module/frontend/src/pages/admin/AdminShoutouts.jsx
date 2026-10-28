import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';

const PERMISSION_LEVELS = [
  { id: 'admin_only', name: 'Admin Only', description: 'Only community admins can use' },
  { id: 'mod', name: 'Moderators', description: 'Admins and moderators can use' },
  { id: 'vip', name: 'VIPs', description: 'Admins, mods, and VIPs can use' },
  { id: 'subscriber', name: 'Subscribers', description: 'Admins, mods, VIPs, and subscribers can use' },
  { id: 'everyone', name: 'Everyone', description: 'All users can use' },
];

const AUTO_SHOUTOUT_MODES = [
  { id: 'disabled', name: 'Disabled', description: 'No automatic shoutouts' },
  { id: 'list_only', name: 'Creator List Only', description: 'Only shout out creators in your list' },
  { id: 'role_based', name: 'Role-Based', description: 'Auto-shoutout users with specific roles' },
  { id: 'all_creators', name: 'All Verified Creators', description: 'Auto-shoutout any verified creator' },
];

const WIDGET_POSITIONS = [
  { id: 'top-left', name: 'Top Left' },
  { id: 'top-right', name: 'Top Right' },
  { id: 'bottom-left', name: 'Bottom Left' },
  { id: 'bottom-right', name: 'Bottom Right' },
];

function AdminShoutouts() {
  const { communityId } = useParams();
  const [config, setConfig] = useState(null);
  const [creators, setCreators] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [activeTab, setActiveTab] = useState('settings');
  const [newCreator, setNewCreator] = useState({ platform: 'twitch', username: '' });
  const [addingCreator, setAddingCreator] = useState(false);

  // History tab state
  const [history, setHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState(null);
  const [historyFilterFrom, setHistoryFilterFrom] = useState('');
  const [historyFilterTo, setHistoryFilterTo] = useState('');
  const [historyFilterType, setHistoryFilterType] = useState('');

  useEffect(() => {
    fetchConfig();
    fetchCreators();
  }, [communityId]);

  useEffect(() => {
    if (activeTab === 'history') {
      fetchHistory();
    }
  }, [activeTab, communityId]); // eslint-disable-line react-hooks/exhaustive-deps

  // `filters` lets a caller (Clear) pass the values it is about to set: reading
  // the `historyFilter*` state here would use this render's stale closure.
  async function fetchHistory(
    filters = { from: historyFilterFrom, to: historyFilterTo, type: historyFilterType },
  ) {
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const params = {};
      if (filters.from) params.from = filters.from;
      if (filters.to) params.to = filters.to;
      if (filters.type) params.type = filters.type;
      const res = await adminApi.getShoutoutHistory(communityId, params);
      setHistory(res.data?.history || res.data?.shoutouts || []);
    } catch (err) {
      setHistoryError('Failed to load shoutout history.');
      console.error(err);
    } finally {
      setHistoryLoading(false);
    }
  }

  async function fetchConfig() {
    setLoading(true);
    try {
      const response = await adminApi.getShoutoutConfig(communityId);
      if (response.data.success) {
        setConfig(response.data.config || getDefaultConfig());
      }
    } catch (err) {
      console.error('Failed to fetch config:', err);
      setConfig(getDefaultConfig());
      if (err.response?.status !== 404) {
        setMessage({ type: 'error', text: 'Failed to load shoutout configuration' });
      }
    } finally {
      setLoading(false);
    }
  }

  async function fetchCreators() {
    try {
      const response = await adminApi.getShoutoutCreators(communityId);
      if (response.data.success) {
        setCreators(response.data.creators || []);
      }
    } catch (err) {
      console.error('Failed to fetch creators:', err);
    }
  }

  function getDefaultConfig() {
    return {
      soEnabled: true,
      soPermission: 'mod',
      vsoEnabled: true,
      vsoPermission: 'mod',
      autoShoutoutMode: 'disabled',
      triggerFirstMessage: false,
      triggerRaidHost: true,
      widgetPosition: 'bottom-right',
      widgetDurationSeconds: 30,
      cooldownMinutes: 60,
    };
  }

  async function handleSave() {
    setSaving(true);
    setMessage(null);
    try {
      await adminApi.updateShoutoutConfig(communityId, config);
      setMessage({ type: 'success', text: 'Shoutout configuration saved' });
    } catch (err) {
      console.error('Failed to save config:', err);
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to save' });
    } finally {
      setSaving(false);
    }
  }

  async function handleAddCreator() {
    if (!newCreator.username.trim()) return;
    setAddingCreator(true);
    try {
      await adminApi.addShoutoutCreator(communityId, newCreator);
      setNewCreator({ platform: 'twitch', username: '' });
      fetchCreators();
      setMessage({ type: 'success', text: 'Creator added successfully' });
    } catch (err) {
      console.error('Failed to add creator:', err);
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to add' });
    } finally {
      setAddingCreator(false);
    }
  }

  async function handleRemoveCreator(creatorId) {
    try {
      await adminApi.removeShoutoutCreator(communityId, creatorId);
      fetchCreators();
      setMessage({ type: 'success', text: 'Creator removed' });
    } catch (err) {
      console.error('Failed to remove creator:', err);
      setMessage({ type: 'error', text: 'Failed to remove creator' });
    }
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
          <h1 className="text-2xl font-bold text-sky-100">Shoutout Settings</h1>
          <p className="text-navy-400 mt-1">Configure text and video shoutout commands</p>
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
          <button onClick={() => setMessage(null)} className="float-right">&times;</button>
        </div>
      )}

      {/* Tabs */}
      <div className="flex space-x-4 mb-6 border-b border-navy-700">
        <button
          onClick={() => setActiveTab('settings')}
          className={`pb-3 px-1 text-sm font-medium border-b-2 transition-colors ${
            activeTab === 'settings'
              ? 'border-gold-400 text-gold-400'
              : 'border-transparent text-navy-400 hover:text-sky-300'
          }`}
        >
          Settings
        </button>
        <button
          onClick={() => setActiveTab('creators')}
          className={`pb-3 px-1 text-sm font-medium border-b-2 transition-colors ${
            activeTab === 'creators'
              ? 'border-gold-400 text-gold-400'
              : 'border-transparent text-navy-400 hover:text-sky-300'
          }`}
        >
          Creator List ({creators.length})
        </button>
        <button
          onClick={() => setActiveTab('history')}
          className={`pb-3 px-1 text-sm font-medium border-b-2 transition-colors ${
            activeTab === 'history'
              ? 'border-gold-400 text-gold-400'
              : 'border-transparent text-navy-400 hover:text-sky-300'
          }`}
        >
          History
        </button>
      </div>

      {activeTab === 'settings' && (
        <div className="space-y-6">
          {/* Text Shoutout (!so) Settings */}
          <div className="card p-6">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-lg font-semibold text-sky-100">Text Shoutout (!so)</h2>
                <p className="text-sm text-navy-400">Send text-based shoutouts in chat</p>
              </div>
              <input
                type="checkbox"
                checked={config.soEnabled}
                onChange={(e) => updateConfig('soEnabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </div>
            {config.soEnabled && (
              <div>
                <label className="block text-sm font-medium text-navy-300 mb-2">
                  Who can use !so
                </label>
                <div className="space-y-2">
                  {PERMISSION_LEVELS.map((level) => (
                    <label
                      key={level.id}
                      className={`flex items-center justify-between p-3 rounded-lg border cursor-pointer transition-all ${
                        config.soPermission === level.id
                          ? 'bg-sky-500/20 border-sky-500/30'
                          : 'bg-navy-800 border-navy-700 hover:border-navy-500'
                      }`}
                    >
                      <div>
                        <div className="font-medium text-sky-100">{level.name}</div>
                        <div className="text-xs text-navy-400">{level.description}</div>
                      </div>
                      <input
                        type="radio"
                        name="soPermission"
                        value={level.id}
                        checked={config.soPermission === level.id}
                        onChange={(e) => updateConfig('soPermission', e.target.value)}
                        className="w-4 h-4 text-sky-500 focus:ring-sky-500"
                      />
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Video Shoutout (!vso) Settings */}
          <div className="card p-6">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-lg font-semibold text-sky-100">Video Shoutout (!vso)</h2>
                <p className="text-sm text-navy-400">Display video clips on stream overlay</p>
              </div>
              <input
                type="checkbox"
                checked={config.vsoEnabled}
                onChange={(e) => updateConfig('vsoEnabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </div>
            {config.vsoEnabled && (
              <div>
                <label className="block text-sm font-medium text-navy-300 mb-2">
                  Who can use !vso
                </label>
                <div className="space-y-2">
                  {PERMISSION_LEVELS.map((level) => (
                    <label
                      key={level.id}
                      className={`flex items-center justify-between p-3 rounded-lg border cursor-pointer transition-all ${
                        config.vsoPermission === level.id
                          ? 'bg-purple-500/20 border-purple-500/30'
                          : 'bg-navy-800 border-navy-700 hover:border-navy-500'
                      }`}
                    >
                      <div>
                        <div className="font-medium text-sky-100">{level.name}</div>
                        <div className="text-xs text-navy-400">{level.description}</div>
                      </div>
                      <input
                        type="radio"
                        name="vsoPermission"
                        value={level.id}
                        checked={config.vsoPermission === level.id}
                        onChange={(e) => updateConfig('vsoPermission', e.target.value)}
                        className="w-4 h-4 text-purple-500 focus:ring-purple-500"
                      />
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Auto-Shoutout Settings */}
          <div className="card p-6">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">Auto-Shoutout</h2>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-navy-300 mb-2">
                  Auto-Shoutout Mode
                </label>
                <select
                  value={config.autoShoutoutMode}
                  onChange={(e) => updateConfig('autoShoutoutMode', e.target.value)}
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                >
                  {AUTO_SHOUTOUT_MODES.map((mode) => (
                    <option key={mode.id} value={mode.id}>
                      {mode.name} - {mode.description}
                    </option>
                  ))}
                </select>
              </div>

              {config.autoShoutoutMode !== 'disabled' && (
                <div className="space-y-3 pt-4 border-t border-navy-700">
                  <label className="flex items-center justify-between p-3 bg-navy-800 rounded-lg cursor-pointer">
                    <div>
                      <div className="font-medium text-sky-100">First Message Trigger</div>
                      <div className="text-xs text-navy-400">
                        Auto-shoutout when a creator sends their first message
                      </div>
                    </div>
                    <input
                      type="checkbox"
                      checked={config.triggerFirstMessage}
                      onChange={(e) => updateConfig('triggerFirstMessage', e.target.checked)}
                      className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
                    />
                  </label>

                  <label className="flex items-center justify-between p-3 bg-navy-800 rounded-lg cursor-pointer">
                    <div>
                      <div className="font-medium text-sky-100">Raid/Host Trigger</div>
                      <div className="text-xs text-navy-400">
                        Auto-shoutout when receiving a raid or host
                      </div>
                    </div>
                    <input
                      type="checkbox"
                      checked={config.triggerRaidHost}
                      onChange={(e) => updateConfig('triggerRaidHost', e.target.checked)}
                      className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
                    />
                  </label>
                </div>
              )}
            </div>
          </div>

          {/* Widget Settings */}
          <div className="card p-6">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">Video Widget Settings</h2>
            <div className="grid md:grid-cols-3 gap-4">
              <div>
                <label className="block text-sm font-medium text-navy-300 mb-2">
                  Widget Position
                </label>
                <select
                  value={config.widgetPosition}
                  onChange={(e) => updateConfig('widgetPosition', e.target.value)}
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                >
                  {WIDGET_POSITIONS.map((pos) => (
                    <option key={pos.id} value={pos.id}>{pos.name}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-navy-300 mb-2">
                  Duration (seconds)
                </label>
                <input
                  type="number"
                  min="10"
                  max="120"
                  value={config.widgetDurationSeconds}
                  onChange={(e) => updateConfig('widgetDurationSeconds', parseInt(e.target.value) || 30)}
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
                <p className="text-xs text-navy-500 mt-1">How long to display (10-120s)</p>
              </div>

              <div>
                <label className="block text-sm font-medium text-navy-300 mb-2">
                  Cooldown (minutes)
                </label>
                <input
                  type="number"
                  min="1"
                  max="1440"
                  value={config.cooldownMinutes}
                  onChange={(e) => updateConfig('cooldownMinutes', parseInt(e.target.value) || 60)}
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
                <p className="text-xs text-navy-500 mt-1">Per-user cooldown (1-1440 min)</p>
              </div>
            </div>

            {/* Widget Preview */}
            <div className="mt-6 p-4 bg-navy-800 rounded-lg">
              <h3 className="text-sm font-medium text-navy-300 mb-3">Widget Preview Position</h3>
              <div className="relative w-full h-32 bg-navy-900 rounded border border-navy-700">
                <div
                  className={`absolute w-24 h-16 bg-purple-500/30 border border-purple-500/50 rounded flex items-center justify-center text-xs text-purple-300 ${
                    config.widgetPosition === 'top-left' ? 'top-2 left-2' :
                    config.widgetPosition === 'top-right' ? 'top-2 right-2' :
                    config.widgetPosition === 'bottom-left' ? 'bottom-2 left-2' :
                    'bottom-2 right-2'
                  }`}
                >
                  Video Widget
                </div>
                <div className="absolute inset-0 flex items-center justify-center text-navy-600 text-sm">
                  Stream Content
                </div>
              </div>
            </div>
          </div>

          {/* Browser Source URL */}
          <div className="card p-6">
            <h2 className="text-lg font-semibold text-sky-100 mb-2">Browser Source URL</h2>
            <p className="text-sm text-navy-400 mb-4">
              Add this URL as a Browser Source in OBS to display video shoutouts
            </p>
            <div className="flex items-center space-x-2">
              <input
                type="text"
                readOnly
                value={`${window.location.origin}/overlay/video-shoutout?community=${communityId}&position=${config.widgetPosition}`}
                className="flex-1 px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 font-mono text-sm"
              />
              <button
                onClick={() => {
                  navigator.clipboard.writeText(
                    `${window.location.origin}/overlay/video-shoutout?community=${communityId}&position=${config.widgetPosition}`
                  );
                  setMessage({ type: 'success', text: 'URL copied to clipboard' });
                }}
                className="btn btn-secondary"
              >
                Copy
              </button>
            </div>
          </div>
        </div>
      )}

      {activeTab === 'creators' && (
        <div className="space-y-6">
          {/* Add Creator */}
          <div className="card p-6">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">Add Creator to Auto-Shoutout List</h2>
            <div className="flex space-x-4">
              <select
                value={newCreator.platform}
                onChange={(e) => setNewCreator({ ...newCreator, platform: e.target.value })}
                className="px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              >
                <option value="twitch">Twitch</option>
                <option value="youtube">YouTube</option>
              </select>
              <input
                type="text"
                value={newCreator.username}
                onChange={(e) => setNewCreator({ ...newCreator, username: e.target.value })}
                placeholder="Username"
                className="flex-1 px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                onKeyPress={(e) => e.key === 'Enter' && handleAddCreator()}
              />
              <button
                onClick={handleAddCreator}
                disabled={addingCreator || !newCreator.username.trim()}
                className="btn btn-primary disabled:opacity-50"
              >
                {addingCreator ? 'Adding...' : 'Add Creator'}
              </button>
            </div>
          </div>

          {/* Creator List */}
          <div className="card p-6">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">
              Auto-Shoutout Creators ({creators.length})
            </h2>
            {creators.length === 0 ? (
              <div className="text-center py-8 text-navy-400">
                No creators added yet. Add creators above to auto-shoutout them.
              </div>
            ) : (
              <div className="space-y-2">
                {creators.map((creator) => (
                  <div
                    key={creator.id}
                    className="flex items-center justify-between p-3 bg-navy-800 rounded-lg"
                  >
                    <div className="flex items-center space-x-3">
                      <span className={`px-2 py-1 rounded text-xs font-bold uppercase ${
                        creator.platform === 'twitch'
                          ? 'bg-purple-500/20 text-purple-400'
                          : 'bg-red-500/20 text-red-400'
                      }`}>
                        {creator.platform}
                      </span>
                      <span className="text-sky-100 font-medium">
                        {creator.platformUsername}
                      </span>
                    </div>
                    <div className="flex items-center space-x-4">
                      <span className="text-xs text-navy-500">
                        Added {new Date(creator.createdAt).toLocaleDateString()}
                      </span>
                      <button
                        onClick={() => handleRemoveCreator(creator.id)}
                        className="text-red-400 hover:text-red-300 text-sm"
                      >
                        Remove
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {activeTab === 'history' && (
        <div className="space-y-4">
          {/* Filters */}
          <div className="card p-4">
            <div className="flex flex-wrap gap-4 items-end">
              <div>
                <label className="block text-xs text-navy-400 mb-1">From</label>
                <input
                  type="date"
                  value={historyFilterFrom}
                  onChange={(e) => setHistoryFilterFrom(e.target.value)}
                  className="input text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-navy-400 mb-1">To</label>
                <input
                  type="date"
                  value={historyFilterTo}
                  onChange={(e) => setHistoryFilterTo(e.target.value)}
                  className="input text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-navy-400 mb-1">Type</label>
                <select
                  value={historyFilterType}
                  onChange={(e) => setHistoryFilterType(e.target.value)}
                  className="input text-sm"
                >
                  <option value="">All Types</option>
                  <option value="text">Text (!so)</option>
                  <option value="visual">Visual</option>
                  <option value="auto">Auto</option>
                  <option value="manual">Manual</option>
                </select>
              </div>
              <button onClick={() => fetchHistory()} className="btn btn-secondary text-sm">
                Apply Filters
              </button>
              <button
                onClick={() => {
                  setHistoryFilterFrom('');
                  setHistoryFilterTo('');
                  setHistoryFilterType('');
                  fetchHistory({ from: '', to: '', type: '' });
                }}
                className="text-navy-400 hover:text-sky-300 text-sm transition-colors"
              >
                Clear
              </button>
            </div>
          </div>

          {/* History Table */}
          <div className="card overflow-hidden">
            {historyLoading ? (
              <div className="flex items-center justify-center py-12">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400" />
              </div>
            ) : historyError ? (
              <div className="p-6 text-red-400 text-sm">{historyError}</div>
            ) : history.length === 0 ? (
              <div className="p-12 text-center text-navy-400">No shoutout history found.</div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Target</th>
                    <th>Type</th>
                    <th>Triggered By</th>
                    <th>Platform</th>
                    <th>Date</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((entry) => (
                    <tr key={entry.id}>
                      <td className="font-medium text-sky-100">{entry.targetUsername || entry.target_username || '—'}</td>
                      <td>
                        <span className="badge bg-navy-700 text-navy-300 border border-navy-600 capitalize">
                          {entry.shoutoutType || entry.type || '—'}
                        </span>
                      </td>
                      <td className="text-navy-400 text-sm">{entry.triggeredBy || entry.triggered_by || '—'}</td>
                      <td className="text-navy-400 text-sm capitalize">{entry.platform || '—'}</td>
                      <td className="text-navy-500 text-sm">
                        {entry.createdAt || entry.created_at
                          ? new Date(entry.createdAt || entry.created_at).toLocaleString()
                          : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminShoutouts;
