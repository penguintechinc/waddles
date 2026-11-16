import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import {
  MusicalNoteIcon,
  RadioIcon,
  AdjustmentsHorizontalIcon,
  ExclamationTriangleIcon,
  TrashIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';

function AdminMusicSettings() {
  const { communityId } = useParams();
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [newBlacklistItem, setNewBlacklistItem] = useState('');
  const [blacklistType, setBlacklistType] = useState('word');
  const [blacklistItems, setBlacklistItems] = useState([]);

  useEffect(() => {
    loadConfig();
  }, [communityId]);

  async function loadConfig() {
    setLoading(true);
    try {
      const response = await adminApi.updateModuleConfig(communityId, 'music', {});
      if (response.data.success) {
        const musicConfig = response.data.config || getDefaultConfig();
        setConfig(musicConfig);
        setBlacklistItems(musicConfig.blacklist || []);
      }
    } catch (err) {
      console.error('Failed to load music config:', err);
      // Initialize with default config if load fails
      const defaultConfig = getDefaultConfig();
      setConfig(defaultConfig);
      setBlacklistItems(defaultConfig.blacklist || []);
    } finally {
      setLoading(false);
    }
  }

  function getDefaultConfig() {
    return {
      music_player_enabled: true,
      radio_player_enabled: true,
      default_volume: 50,
      song_request_enabled: true,
      max_requests_per_user: 5,
      request_cooldown_minutes: 30,
      blacklist: [],
    };
  }

  async function handleSave() {
    setSaving(true);
    setMessage(null);
    try {
      const configToSave = {
        ...config,
        blacklist: blacklistItems,
      };
      await adminApi.updateModuleConfig(communityId, 'music', configToSave);
      setMessage({ type: 'success', text: 'Music settings saved successfully' });
    } catch (err) {
      console.error('Failed to save music config:', err);
      const errorMsg = err.response?.data?.error?.message || 'Failed to save music settings';
      setMessage({ type: 'error', text: errorMsg });
    } finally {
      setSaving(false);
    }
  }

  function updateConfig(key, value) {
    setConfig({ ...config, [key]: value });
  }

  function updateNestedConfig(parent, key, value) {
    setConfig({
      ...config,
      [parent]: {
        ...config[parent],
        [key]: value,
      },
    });
  }

  function handleAddBlacklistItem() {
    if (!newBlacklistItem.trim()) {
      setMessage({ type: 'error', text: 'Please enter a word or artist name' });
      return;
    }

    const item = {
      id: Date.now(),
      value: newBlacklistItem.trim(),
      type: blacklistType,
      createdAt: new Date().toISOString(),
    };

    setBlacklistItems([...blacklistItems, item]);
    setNewBlacklistItem('');
    setMessage({ type: 'success', text: `${blacklistType === 'word' ? 'Word' : 'Artist'} added to blacklist` });
    setTimeout(() => setMessage(null), 3000);
  }

  function handleRemoveBlacklistItem(id) {
    setBlacklistItems(blacklistItems.filter(item => item.id !== id));
    setMessage({ type: 'success', text: 'Item removed from blacklist' });
    setTimeout(() => setMessage(null), 3000);
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
        Failed to load music settings
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Music Settings</h1>
          <p className="text-navy-400 mt-1">
            Configure music player, radio player, and song request settings
          </p>
        </div>
        <button
          onClick={handleSave}
          disabled={saving}
          className="btn btn-primary disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save Settings'}
        </button>
      </div>

      {message && (
        <div
          className={`mb-6 p-4 rounded-lg border ${
            message.type === 'success'
              ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
              : 'bg-red-500/20 text-red-300 border-red-500/30'
          }`}
        >
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">&times;</button>
        </div>
      )}

      <div className="space-y-6">
        {/* Player Toggle Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <MusicalNoteIcon className="w-5 h-5" />
            Player Configuration
          </h2>
          <div className="space-y-4">
            {/* Music Player Toggle */}
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer hover:bg-navy-750 transition">
              <div className="flex items-center gap-3">
                <MusicalNoteIcon className="w-6 h-6 text-sky-400" />
                <div>
                  <div className="font-medium text-sky-100">Enable Music Player</div>
                  <div className="text-sm text-navy-400">
                    Allow users to play music in the community
                  </div>
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.music_player_enabled ?? true}
                onChange={(e) => updateConfig('music_player_enabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>

            {/* Radio Player Toggle */}
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer hover:bg-navy-750 transition">
              <div className="flex items-center gap-3">
                <RadioIcon className="w-6 h-6 text-sky-400" />
                <div>
                  <div className="font-medium text-sky-100">Enable Radio Player</div>
                  <div className="text-sm text-navy-400">
                    Allow users to listen to radio streams
                  </div>
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.radio_player_enabled ?? true}
                onChange={(e) => updateConfig('radio_player_enabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>
          </div>
        </div>

        {/* Volume Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <AdjustmentsHorizontalIcon className="w-5 h-5" />
            Volume Settings
          </h2>
          <div className="space-y-4">
            <div>
              <label htmlFor="admin-music-settings-input" className="block text-sm font-medium text-navy-300 mb-4">
                Default Volume: <span className="text-sky-300 font-bold">{config.default_volume ?? 50}%</span>
              </label>
              <div className="flex items-center gap-4">
                <input
                  id="admin-music-settings-input"
                  type="range"
                  min="0"
                  max="100"
                  step="5"
                  value={config.default_volume ?? 50}
                  onChange={(e) => updateConfig('default_volume', parseInt(e.target.value))}
                  className="flex-1 h-2 bg-navy-700 rounded-lg appearance-none cursor-pointer"
                />
              </div>
              <p className="text-xs text-navy-500 mt-2">
                The default volume level for all music and radio players (0-100%)
              </p>
            </div>
          </div>
        </div>

        {/* Song Request Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <MusicalNoteIcon className="w-5 h-5" />
            Song Request Settings
          </h2>
          <div className="space-y-4">
            {/* Enable Song Requests */}
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer hover:bg-navy-750 transition">
              <div>
                <div className="font-medium text-sky-100">Enable Song Requests</div>
                <div className="text-sm text-navy-400">
                  Allow users to request songs to be played
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.song_request_enabled ?? true}
                onChange={(e) => updateConfig('song_request_enabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>

            {config.song_request_enabled && (
              <>
                {/* Max Requests Per User */}
                <div>
                  <label htmlFor="admin-music-settings-maximum-requests-per-user" className="block text-sm font-medium text-navy-300 mb-2">
                    Maximum Requests Per User
                  </label>
                  <input
                    id="admin-music-settings-maximum-requests-per-user"
                    type="number"
                    min="1"
                    max="50"
                    value={config.max_requests_per_user ?? 5}
                    onChange={(e) => updateConfig('max_requests_per_user', parseInt(e.target.value))}
                    className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                  />
                  <p className="text-xs text-navy-500 mt-1">
                    Maximum number of songs a user can request
                  </p>
                </div>

                {/* Request Cooldown */}
                <div>
                  <label htmlFor="admin-music-settings-request-cooldown-minutes" className="block text-sm font-medium text-navy-300 mb-2">
                    Request Cooldown (minutes)
                  </label>
                  <input
                    id="admin-music-settings-request-cooldown-minutes"
                    type="number"
                    min="0"
                    max="1440"
                    value={config.request_cooldown_minutes ?? 30}
                    onChange={(e) => updateConfig('request_cooldown_minutes', parseInt(e.target.value))}
                    className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                  />
                  <p className="text-xs text-navy-500 mt-1">
                    Time users must wait before requesting another song (0 for no cooldown)
                  </p>
                </div>
              </>
            )}
          </div>
        </div>

        {/* Blacklist Configuration */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <ExclamationTriangleIcon className="w-5 h-5" />
            Blacklist Management
          </h2>
          <p className="text-sm text-navy-400 mb-4">
            Block specific words or artists from being played or requested
          </p>

          {/* Add New Blacklist Item */}
          <div className="mb-6 p-4 bg-navy-800 rounded-lg">
            <label htmlFor="admin-music-settings-add-to-blacklist" className="block text-sm font-medium text-navy-300 mb-3">
              Add to Blacklist
            </label>
            <div className="flex gap-3">
              <select
                id="admin-music-settings-add-to-blacklist"
                value={blacklistType}
                onChange={(e) => setBlacklistType(e.target.value)}
                className="px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              >
                <option value="word">Word</option>
                <option value="artist">Artist</option>
              </select>
              <input
                aria-label="Blacklist entry"
                type="text"
                value={newBlacklistItem}
                onChange={(e) => setNewBlacklistItem(e.target.value)}
                onKeyPress={(e) => {
                  if (e.key === 'Enter') {
                    handleAddBlacklistItem();
                  }
                }}
                placeholder={`Enter ${blacklistType} name...`}
                className="flex-1 px-4 py-2 bg-navy-700 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <button
                onClick={handleAddBlacklistItem}
                className="btn btn-secondary flex items-center gap-2"
              >
                <PlusIcon className="w-4 h-4" />
                Add
              </button>
            </div>
          </div>

          {/* Blacklist Items */}
          {blacklistItems.length > 0 ? (
            <div className="space-y-2">
              {blacklistItems.map((item) => (
                <div
                  key={item.id}
                  className="flex items-center justify-between p-3 bg-navy-800 rounded-lg border border-navy-700"
                >
                  <div className="flex items-center gap-3">
                    <span className="px-2 py-1 text-xs font-semibold rounded-full bg-navy-700 text-sky-300">
                      {item.type === 'word' ? 'WORD' : 'ARTIST'}
                    </span>
                    <span className="text-sky-100">{item.value}</span>
                  </div>
                  <button
                    aria-label={`Remove ${item.type} ${item.value} from blacklist`}
                    onClick={() => handleRemoveBlacklistItem(item.id)}
                    className="btn btn-danger p-2 hover:bg-red-500/30"
                  >
                    <TrashIcon className="w-4 h-4" />
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-center py-8">
              <p className="text-navy-400">No blacklist items configured</p>
              <p className="text-xs text-navy-500 mt-1">Add words or artists to block them from the music player</p>
            </div>
          )}
        </div>

        {/* Info Box */}
        <div className="p-4 bg-sky-500/10 border border-sky-500/30 rounded-lg">
          <div className="flex gap-3">
            <div className="flex-shrink-0 mt-0.5">
              <div className="w-5 h-5 rounded-full bg-sky-500 flex items-center justify-center">
                <span className="text-xs font-bold text-navy-900">i</span>
              </div>
            </div>
            <div className="text-sm text-sky-200">
              <p className="font-semibold mb-1">Music Settings Tips:</p>
              <ul className="space-y-1 text-xs">
                <li>Keep default volume between 30-70% for best user experience</li>
                <li>Set cooldown to prevent song request spam</li>
                <li>Use blacklist to prevent inappropriate or unwanted content</li>
                <li>Changes are saved immediately and apply to all users</li>
              </ul>
            </div>
          </div>
        </div>

        {/* Save Button */}
        <div className="flex justify-end">
          <button
            onClick={handleSave}
            disabled={saving}
            className="btn btn-primary px-8 disabled:opacity-50"
          >
            {saving ? 'Saving...' : 'Save Settings'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default AdminMusicSettings;
