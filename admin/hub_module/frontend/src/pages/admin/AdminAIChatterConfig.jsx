import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { CheckIcon, XMarkIcon } from '@heroicons/react/24/outline';

function AdminAIChatterConfig() {
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
      const response = await adminApi.getAIChatterConfig(communityId);
      setConfig(response.data);
    } catch (err) {
      console.error('Failed to fetch AI Chatter config:', err);
      setMessage({ type: 'error', text: 'Failed to load AI Chatter configuration' });
    } finally {
      setLoading(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setMessage(null);
    try {
      await adminApi.updateAIChatterConfig(communityId, config);
      setMessage({ type: 'success', text: 'AI Chatter configuration saved' });
    } catch (err) {
      console.error('Failed to save config:', err);
      const errorMsg = err.response?.data?.error?.message || 'Failed to save configuration';
      setMessage({ type: 'error', text: errorMsg });
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
          <h1 className="text-2xl font-bold text-sky-100">AI Chatter</h1>
          <p className="text-navy-400 mt-1">
            Allow the AI to proactively respond to chat messages in your community. Configure how often and how many times it responds.
          </p>
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
        <div className={`mb-6 p-4 rounded-lg border flex items-center justify-between ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border-red-500/30'
        }`}>
          <div className="flex items-center space-x-3">
            {message.type === 'success' ? (
              <CheckIcon className="w-5 h-5" />
            ) : (
              <XMarkIcon className="w-5 h-5" />
            )}
            <span>{message.text}</span>
          </div>
          <button aria-label="Close" onClick={() => setMessage(null)} className="hover:opacity-75">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      <div className="space-y-6">
        {/* AI Chatter Settings Card */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Configuration</h2>
          <div className="space-y-6">
            {/* Enable Toggle */}
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div className="flex items-center gap-3">
                <svg className="w-6 h-6 text-sky-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M13 10V3L4 14h7v7l9-11h-7z" />
                </svg>
                <div>
                  <div className="font-medium text-sky-100">Enable AI Chatter</div>
                  <div className="text-sm text-navy-400">
                    Allow AI to proactively respond to chat messages
                  </div>
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.enabled || false}
                onChange={(e) => updateConfig('enabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-gold-500 focus:ring-gold-500"
              />
            </label>

            {/* Max Responses Per Window */}
            <div>
              <label htmlFor="admin-aichatter-config-max-ai-responses-per-time-window" className="block text-sm font-medium text-navy-300 mb-2">
                Max AI responses per time window
              </label>
              <div className="flex items-center gap-4">
                <input
                  id="admin-aichatter-config-max-ai-responses-per-time-window"
                  type="number"
                  min="1"
                  max="100"
                  value={config.max_responses_per_window || 10}
                  onChange={(e) => updateConfig('max_responses_per_window', parseInt(e.target.value) || 10)}
                  className="flex-1 px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                    text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
              </div>
              <p className="text-xs text-navy-500 mt-1">
                Maximum number of AI responses allowed in a single time window
              </p>
            </div>

            {/* Window Duration */}
            <div>
              <label htmlFor="admin-aichatter-config-time-window-duration-minutes" className="block text-sm font-medium text-navy-300 mb-2">
                Time window duration (minutes)
              </label>
              <div className="flex items-center gap-4">
                <input
                  id="admin-aichatter-config-time-window-duration-minutes"
                  type="number"
                  min="1"
                  max="60"
                  value={Math.round((config.window_seconds || 600) / 60)}
                  onChange={(e) => updateConfig('window_seconds', (parseInt(e.target.value) || 10) * 60)}
                  className="flex-1 px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                    text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
              </div>
              <p className="text-xs text-navy-500 mt-1">
                Duration of the rate-limiting window in minutes
              </p>
            </div>

            {/* Per-User Limit */}
            <div>
              <label htmlFor="admin-aichatter-config-max-responses-per-user-per-window" className="block text-sm font-medium text-navy-300 mb-2">
                Max responses per user per window
              </label>
              <div className="flex items-center gap-4">
                <input
                  id="admin-aichatter-config-max-responses-per-user-per-window"
                  type="number"
                  min="1"
                  max="20"
                  value={config.max_per_user_per_window || 2}
                  onChange={(e) => updateConfig('max_per_user_per_window', parseInt(e.target.value) || 2)}
                  className="flex-1 px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                    text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
              </div>
              <p className="text-xs text-navy-500 mt-1">
                Maximum AI responses per individual user in each time window
              </p>
            </div>

            {/* Response Probability */}
            <div>
              <label htmlFor="admin-aichatter-config-input" className="block text-sm font-medium text-navy-300 mb-2">
                Response probability ({Math.round((config.response_probability || 0.3) * 100)}%)
              </label>
              <div className="flex items-center gap-4">
                <input
                  id="admin-aichatter-config-input"
                  type="range"
                  min="0.05"
                  max="1"
                  step="0.05"
                  value={config.response_probability || 0.3}
                  onChange={(e) => updateConfig('response_probability', parseFloat(e.target.value))}
                  className="flex-1 h-2 bg-navy-700 rounded-lg appearance-none cursor-pointer"
                />
                <div className="w-20 text-center">
                  <span className="font-medium text-sky-100">
                    {Math.round((config.response_probability || 0.3) * 100)}%
                  </span>
                </div>
              </div>
              <p className="text-xs text-navy-500 mt-1">
                Probability (5%-100%) that AI will respond to eligible messages
              </p>
            </div>

            {/* Min Message Length */}
            <div>
              <label htmlFor="admin-aichatter-config-minimum-message-length-characters" className="block text-sm font-medium text-navy-300 mb-2">
                Minimum message length (characters)
              </label>
              <div className="flex items-center gap-4">
                <input
                  id="admin-aichatter-config-minimum-message-length-characters"
                  type="number"
                  min="1"
                  max="100"
                  value={config.min_message_length || 10}
                  onChange={(e) => updateConfig('min_message_length', parseInt(e.target.value) || 10)}
                  className="flex-1 px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                    text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
              </div>
              <p className="text-xs text-navy-500 mt-1">
                Minimum message length required for AI to consider responding
              </p>
            </div>
          </div>
        </div>

        {/* Configuration Summary */}
        <div className="card p-6 bg-navy-800/50 border border-navy-700">
          <h3 className="text-lg font-semibold text-sky-100 mb-4">Current Settings Summary</h3>
          <div className="grid md:grid-cols-2 gap-4">
            <div className="p-3 bg-navy-900 rounded-lg">
              <div className="text-sm text-navy-400">Status</div>
              <div className={`text-lg font-bold ${config.enabled ? 'text-emerald-400' : 'text-red-400'}`}>
                {config.enabled ? 'Enabled' : 'Disabled'}
              </div>
            </div>
            <div className="p-3 bg-navy-900 rounded-lg">
              <div className="text-sm text-navy-400">Responses per window</div>
              <div className="text-lg font-bold text-sky-100">
                {config.max_responses_per_window || 10}
              </div>
            </div>
            <div className="p-3 bg-navy-900 rounded-lg">
              <div className="text-sm text-navy-400">Window duration</div>
              <div className="text-lg font-bold text-sky-100">
                {Math.round((config.window_seconds || 600) / 60)} min
              </div>
            </div>
            <div className="p-3 bg-navy-900 rounded-lg">
              <div className="text-sm text-navy-400">Per-user limit</div>
              <div className="text-lg font-bold text-sky-100">
                {config.max_per_user_per_window || 2}
              </div>
            </div>
            <div className="p-3 bg-navy-900 rounded-lg">
              <div className="text-sm text-navy-400">Response probability</div>
              <div className="text-lg font-bold text-sky-100">
                {Math.round((config.response_probability || 0.3) * 100)}%
              </div>
            </div>
            <div className="p-3 bg-navy-900 rounded-lg">
              <div className="text-sm text-navy-400">Min message length</div>
              <div className="text-lg font-bold text-sky-100">
                {config.min_message_length || 10} chars
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default AdminAIChatterConfig;
