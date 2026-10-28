import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { adminApi as api } from '../../services/api';
import {
  ShieldCheckIcon,
  TrashIcon,
  PlusIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';

function AdminSecurity() {
  const { communityId } = useParams();
  const [config, setConfig] = useState(null);
  const [blockedWords, setBlockedWords] = useState([]);
  const [warnings, setWarnings] = useState([]);
  const [moderationLog, setModerationLog] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [newWord, setNewWord] = useState('');
  const [newWordReason, setNewWordReason] = useState('');
  const [activeTab, setActiveTab] = useState('config');

  useEffect(() => {
    fetchSecurityData();
  }, [communityId]);

  async function fetchSecurityData() {
    setLoading(true);
    try {
      const [configRes, wordsRes, warningsRes, logRes] = await Promise.all([
        api.getSecurityConfig(communityId).catch(() => ({ data: { data: {} } })),
        api.getSecurityBlockedWords(communityId).catch(() => ({ data: { data: [] } })),
        api.getSecurityWarnings(communityId).catch(() => ({ data: { data: [] } })),
        api.getSecurityModerationLog(communityId).catch(() => ({ data: { data: [] } })),
      ]);

      setConfig(configRes.data.data || configRes.data || {});
      setBlockedWords(wordsRes.data.data || wordsRes.data || []);
      setWarnings(warningsRes.data.data || warningsRes.data || []);
      setModerationLog(logRes.data.data || logRes.data || []);
    } catch (err) {
      console.error('Failed to fetch security data:', err);
      setMessage({ type: 'error', text: 'Failed to load security settings' });
    } finally {
      setLoading(false);
    }
  }

  async function handleSaveConfig() {
    setSaving(true);
    setMessage(null);
    try {
      await api.updateSecurityConfig(communityId, config);
      setMessage({ type: 'success', text: 'Security configuration saved' });
    } catch (err) {
      console.error('Failed to save config:', err);
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to save configuration' });
    } finally {
      setSaving(false);
    }
  }

  async function handleAddBlockedWord() {
    if (!newWord.trim()) {
      setMessage({ type: 'error', text: 'Please enter a word' });
      return;
    }

    setSaving(true);
    setMessage(null);
    try {
      await api.addSecurityBlockedWord(communityId, {
        word: newWord.trim(),
        reason: newWordReason || 'User blocked',
      });
      setNewWord('');
      setNewWordReason('');
      await fetchSecurityData();
      setMessage({ type: 'success', text: 'Word added to block list' });
    } catch (err) {
      console.error('Failed to add blocked word:', err);
      setMessage({ type: 'error', text: 'Failed to add word to block list' });
    } finally {
      setSaving(false);
    }
  }

  async function handleDeleteBlockedWord(wordId) {
    if (!confirm('Are you sure you want to remove this word from the block list?')) {
      return;
    }

    setSaving(true);
    try {
      await api.deleteSecurityBlockedWord(communityId, wordId);
      await fetchSecurityData();
      setMessage({ type: 'success', text: 'Word removed from block list' });
    } catch (err) {
      console.error('Failed to delete blocked word:', err);
      setMessage({ type: 'error', text: 'Failed to remove word' });
    } finally {
      setSaving(false);
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

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Security Dashboard</h1>
          <p className="text-navy-400 mt-1">Manage spam detection and content filtering</p>
        </div>
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

      {/* Tab Navigation */}
      <div className="flex gap-2 mb-6 border-b border-navy-700">
        {[
          { id: 'config', label: 'Configuration' },
          { id: 'blocked', label: 'Blocked Words' },
          { id: 'warnings', label: 'Warnings' },
          { id: 'log', label: 'Moderation Log' },
        ].map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`px-4 py-3 font-medium border-b-2 transition-all ${
              activeTab === tab.id
                ? 'border-gold-400 text-gold-400'
                : 'border-transparent text-navy-400 hover:text-sky-300'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Configuration Tab */}
      {activeTab === 'config' && (
        <div className="space-y-6">
          <div className="card p-6">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">Spam Detection</h2>
            <div className="space-y-4">
              <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
                <div>
                  <div className="font-medium text-sky-100">Enable Spam Detection</div>
                  <div className="text-sm text-navy-400">
                    Automatically detect and flag spam messages
                  </div>
                </div>
                <input
                  type="checkbox"
                  checked={config.spamDetectionEnabled || false}
                  onChange={(e) => updateConfig('spamDetectionEnabled', e.target.checked)}
                  className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
                />
              </label>

              <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
                <div>
                  <div className="font-medium text-sky-100">Auto-Delete Spam</div>
                  <div className="text-sm text-navy-400">
                    Automatically delete detected spam messages
                  </div>
                </div>
                <input
                  type="checkbox"
                  checked={config.autoDeleteSpam || false}
                  onChange={(e) => updateConfig('autoDeleteSpam', e.target.checked)}
                  disabled={!config.spamDetectionEnabled}
                  className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500 disabled:opacity-50"
                />
              </label>
            </div>
          </div>

          <div className="card p-6">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">Content Filtering</h2>
            <div className="space-y-4">
              <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
                <div>
                  <div className="font-medium text-sky-100">Enable Content Filter</div>
                  <div className="text-sm text-navy-400">
                    Filter messages against blocked words list
                  </div>
                </div>
                <input
                  type="checkbox"
                  checked={config.contentFilterEnabled || false}
                  onChange={(e) => updateConfig('contentFilterEnabled', e.target.checked)}
                  className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
                />
              </label>

              <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
                <div>
                  <div className="font-medium text-sky-100">Enable Built-in Profanity Filter</div>
                  <div className="text-sm text-navy-400">
                    Adds ~130 common English profanity terms to your block list
                  </div>
                </div>
                <input
                  type="checkbox"
                  checked={config.use_default_profanity_list || false}
                  onChange={(e) => updateConfig('use_default_profanity_list', e.target.checked)}
                  disabled={!config.contentFilterEnabled}
                  className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500 disabled:opacity-50"
                />
              </label>

              <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
                <div>
                  <div className="font-medium text-sky-100">Case Sensitive</div>
                  <div className="text-sm text-navy-400">
                    Match words with exact case
                  </div>
                </div>
                <input
                  type="checkbox"
                  checked={config.filterCaseSensitive || false}
                  onChange={(e) => updateConfig('filterCaseSensitive', e.target.checked)}
                  disabled={!config.contentFilterEnabled}
                  className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500 disabled:opacity-50"
                />
              </label>

              <div>
                <label htmlFor="admin-security-filter-action" className="block text-sm font-medium text-navy-300 mb-2">
                  Filter Action
                </label>
                <select
                  id="admin-security-filter-action"
                  value={config.filterAction || 'warn'}
                  onChange={(e) => updateConfig('filterAction', e.target.value)}
                  disabled={!config.contentFilterEnabled}
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500 disabled:opacity-50"
                >
                  <option value="warn">Warn User</option>
                  <option value="delete">Delete Message</option>
                  <option value="timeout">Timeout User</option>
                </select>
                <p className="text-xs text-navy-500 mt-1">
                  Action to take when blocked words are detected
                </p>
              </div>
            </div>
          </div>

          <button
            onClick={handleSaveConfig}
            disabled={saving}
            className="btn btn-primary w-full disabled:opacity-50"
          >
            {saving ? 'Saving...' : 'Save Configuration'}
          </button>
        </div>
      )}

      {/* Blocked Words Tab */}
      {activeTab === 'blocked' && (
        <div className="space-y-6">
          <div className="card p-6">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">Add Blocked Word</h2>
            <div className="space-y-4">
              <input
                aria-label="Enter word or phrase to block"
                type="text"
                value={newWord}
                onChange={(e) => setNewWord(e.target.value)}
                onKeyPress={(e) => e.key === 'Enter' && handleAddBlockedWord()}
                placeholder="Enter word or phrase to block"
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <input
                aria-label="Reason for blocking (optional)"
                type="text"
                value={newWordReason}
                onChange={(e) => setNewWordReason(e.target.value)}
                placeholder="Reason for blocking (optional)"
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <button
                onClick={handleAddBlockedWord}
                disabled={saving}
                className="btn btn-primary w-full disabled:opacity-50"
              >
                <PlusIcon className="w-5 h-5 mr-2" />
                Add to Block List
              </button>
            </div>
          </div>

          <div className="card p-6">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">
              Blocked Words ({blockedWords.length})
            </h2>
            {blockedWords.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-navy-400 border-b border-navy-700">
                    <tr>
                      <th className="text-left py-2 px-4">Word/Phrase</th>
                      <th className="text-left py-2 px-4">Reason</th>
                      <th className="text-left py-2 px-4">Added</th>
                      <th className="text-center py-2 px-4">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {blockedWords.map((word) => (
                      <tr key={word.id} className="border-b border-navy-800 hover:bg-navy-800/50">
                        <td className="py-3 px-4 text-sky-100 font-mono">{word.word || word.text}</td>
                        <td className="py-3 px-4 text-navy-300 text-xs">{word.reason || 'User blocked'}</td>
                        <td className="py-3 px-4 text-navy-400 text-xs">
                          {new Date(word.createdAt).toLocaleDateString()}
                        </td>
                        <td className="py-3 px-4 text-center">
                          <button
                            onClick={() => handleDeleteBlockedWord(word.id)}
                            className="text-red-400 hover:text-red-300"
                            title="Remove word"
                          >
                            <TrashIcon className="w-4 h-4" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-center py-8 text-navy-400">No blocked words yet</p>
            )}
          </div>
        </div>
      )}

      {/* Warnings Tab */}
      {activeTab === 'warnings' && (
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">
            Recent Warnings ({warnings.length})
          </h2>
          {warnings.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-navy-400 border-b border-navy-700">
                  <tr>
                    <th className="text-left py-2 px-4">User</th>
                    <th className="text-left py-2 px-4">Reason</th>
                    <th className="text-left py-2 px-4">Count</th>
                    <th className="text-left py-2 px-4">Date</th>
                  </tr>
                </thead>
                <tbody>
                  {warnings.slice(0, 20).map((warning, idx) => (
                    <tr key={idx} className="border-b border-navy-800 hover:bg-navy-800/50">
                      <td className="py-3 px-4 text-sky-100">{warning.username || warning.userId}</td>
                      <td className="py-3 px-4 text-navy-300 flex items-center gap-2">
                        <ExclamationTriangleIcon className="w-4 h-4 text-yellow-400" />
                        {warning.reason || 'Content violation'}
                      </td>
                      <td className="py-3 px-4 text-navy-400">
                        <span className="bg-navy-800 px-2 py-1 rounded text-xs">{warning.count || 1}</span>
                      </td>
                      <td className="py-3 px-4 text-navy-400 text-xs">
                        {new Date(warning.createdAt).toLocaleDateString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-center py-8 text-navy-400">No warnings yet</p>
          )}
        </div>
      )}

      {/* Moderation Log Tab */}
      {activeTab === 'log' && (
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">
            Moderation Log ({moderationLog.length})
          </h2>
          {moderationLog.length > 0 ? (
            <div className="space-y-3">
              {moderationLog.slice(0, 20).map((log, idx) => (
                <div key={idx} className="bg-navy-800 rounded-lg p-4 border border-navy-700">
                  <div className="flex items-start justify-between mb-2">
                    <div>
                      <div className="text-sm font-medium text-sky-100">{log.action || 'Action'}</div>
                      <div className="text-xs text-navy-400">
                        User: {log.username || log.userId}
                      </div>
                    </div>
                    <span className={`text-xs px-2 py-1 rounded ${
                      log.status === 'success'
                        ? 'bg-emerald-500/20 text-emerald-300'
                        : 'bg-red-500/20 text-red-300'
                    }`}>
                      {log.status || 'pending'}
                    </span>
                  </div>
                  <p className="text-xs text-navy-400 mb-2">{log.details || log.reason}</p>
                  <div className="text-xs text-navy-500">
                    {new Date(log.createdAt).toLocaleString()}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-center py-8 text-navy-400">No moderation actions yet</p>
          )}
        </div>
      )}
    </div>
  );
}

export default AdminSecurity;
