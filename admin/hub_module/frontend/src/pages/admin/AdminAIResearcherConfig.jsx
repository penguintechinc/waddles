import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';

const AI_PROVIDERS = [
  { id: 'ollama', name: 'Ollama', tier: 'free' },
  { id: 'openai', name: 'OpenAI', tier: 'premium' },
  { id: 'anthropic', name: 'Anthropic', tier: 'premium' },
  { id: 'gemini', name: 'Google Gemini', tier: 'premium' },
];

const RESPONSE_DESTINATIONS = [
  { id: 'same_chat', name: 'Same Chat Channel', description: 'Send responses in the same channel' },
  { id: 'dm', name: 'Direct Message', description: 'Send via DM to the user' },
  { id: 'dedicated_channel', name: 'Dedicated Channel', description: 'Send to a dedicated research channel' },
];

const DAYS_OF_WEEK = [
  { id: 0, name: 'Sunday' },
  { id: 1, name: 'Monday' },
  { id: 2, name: 'Tuesday' },
  { id: 3, name: 'Wednesday' },
  { id: 4, name: 'Thursday' },
  { id: 5, name: 'Friday' },
  { id: 6, name: 'Saturday' },
];

const BLOCKED_TOPICS = [
  'politics',
  'religion',
  'violence',
  'adult-content',
  'illegal-activities',
  'hate-speech',
  'personal-information',
  'medical-advice',
  'financial-advice',
  'crypto-trading',
];

function AdminAIResearcherConfig() {
  const { communityId } = useParams();
  const { user } = useAuth();
  const [config, setConfig] = useState(null);
  const [availableModels, setAvailableModels] = useState({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const isPremium = user?.isPremium || false;

  useEffect(() => {
    fetchConfig();
    fetchAvailableModels();
  }, [communityId]);

  async function fetchConfig() {
    setLoading(true);
    try {
      const response = await adminApi.getAIResearcherConfig(communityId);
      if (response.data.success) {
        setConfig(response.data.config || getDefaultConfig());
      }
    } catch (err) {
      console.error('Failed to fetch config:', err);
      setConfig(getDefaultConfig());
      setMessage({ type: 'error', text: 'Failed to load AI Researcher configuration' });
    } finally {
      setLoading(false);
    }
  }

  async function fetchAvailableModels() {
    try {
      const response = await adminApi.getAvailableAIModels(communityId);
      if (response.data.success) {
        setAvailableModels(response.data.data);
      }
    } catch (err) {
      console.error('Failed to fetch available models:', err);
      // Set defaults if fetch fails
      setAvailableModels({
        ollama: [
          { id: 'tinyllama', name: 'TinyLlama', description: 'Lightweight 1.1B model' },
          { id: 'llama3.2', name: 'Llama 3.2', description: '3B parameter model' }
        ],
        openai: [{ id: 'gpt-4o', name: 'GPT-4o' }],
        anthropic: [{ id: 'claude-opus-4-5-20251101', name: 'Claude Opus 4.5' }],
        gemini: [{ id: 'gemini-2.0-flash', name: 'Gemini 2.0 Flash' }]
      });
    }
  }

  function getDefaultConfig() {
    return {
      aiProvider: 'ollama',
      aiModel: 'tinyllama',
      researchEnabled: false,
      responseDestination: 'same_chat',
      rateLimitPerUser: 5,
      rateLimitPerCommunity: 100,
      contextTracking: true,
      streamSummaryEnabled: false,
      weeklySummaryEnabled: false,
      weeklySummaryDay: 1,
      weeklySummaryHour: 9,
      botDetectionEnabled: false,
      botConfidenceThreshold: 85,
      blockedTopics: [],
      webhookUrl: '',
    };
  }

  async function handleSave() {
    setSaving(true);
    setMessage(null);
    try {
      await adminApi.updateAIResearcherConfig(communityId, config);
      setMessage({ type: 'success', text: 'AI Researcher configuration saved' });
    } catch (err) {
      console.error('Failed to save config:', err);
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to save configuration' });
    } finally {
      setSaving(false);
    }
  }

  function updateConfig(key, value) {
    setConfig({ ...config, [key]: value });
  }

  function toggleBlockedTopic(topic) {
    const topics = config.blockedTopics || [];
    const newTopics = topics.includes(topic)
      ? topics.filter(t => t !== topic)
      : [...topics, topic];
    updateConfig('blockedTopics', newTopics);
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
          <h1 className="text-2xl font-bold text-sky-100">AI Researcher Configuration</h1>
          <p className="text-navy-400 mt-1">Configure AI-powered research and bot detection</p>
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
        {/* Basic Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Basic Settings</h2>
          <div className="space-y-4">
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div>
                <div className="font-medium text-sky-100">Enable AI Researcher</div>
                <div className="text-sm text-navy-400">
                  Allow AI to automatically research topics mentioned in chat
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.researchEnabled}
                onChange={(e) => updateConfig('researchEnabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>
          </div>
        </div>

        {/* AI Provider Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">AI Provider</h2>
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <label htmlFor="admin-airesearcher-config-ai-provider" className="block text-sm font-medium text-navy-300 mb-2">
                AI Provider
              </label>
              <select
                id="admin-airesearcher-config-ai-provider"
                value={config.aiProvider}
                onChange={(e) => updateConfig('aiProvider', e.target.value)}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              >
                {AI_PROVIDERS.map((provider) => {
                  const isLocked = provider.tier === 'premium' && !isPremium;
                  return (
                    <option key={provider.id} value={provider.id} disabled={isLocked}>
                      {provider.name} {isLocked ? '(Premium)' : ''}
                    </option>
                  );
                })}
              </select>
              {!isPremium && (
                <p className="text-xs text-navy-500 mt-1">
                  Upgrade to Premium for access to all AI providers
                </p>
              )}
            </div>
            <div>
              <label htmlFor="admin-airesearcher-config-ai-model" className="block text-sm font-medium text-navy-300 mb-2">
                AI Model
              </label>
              <select
                id="admin-airesearcher-config-ai-model"
                value={config.aiModel}
                onChange={(e) => updateConfig('aiModel', e.target.value)}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              >
                <option value="">Select a model</option>
                {config.aiProvider && availableModels[config.aiProvider]?.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name} {model.description && `- ${model.description}`}
                  </option>
                ))}
              </select>
              <p className="text-xs text-navy-500 mt-1">
                Available models for {config.aiProvider || 'selected provider'}
              </p>
            </div>
          </div>
        </div>

        {/* Custom Endpoint (Premium Only) */}
        {isPremium && config.customEndpoint !== null && (
          <div className="card p-6 border-sky-500/30">
            <div className="flex items-center gap-2 mb-4">
              <h2 className="text-lg font-semibold text-sky-100">Custom AI Endpoint</h2>
              <span className="px-2 py-1 text-xs bg-sky-500/20 text-sky-400 rounded-full">Premium</span>
            </div>
            <p className="text-sm text-navy-400 mb-4">
              Configure a custom AI endpoint for enterprise deployments (Azure OpenAI, private Ollama instance, etc.)
            </p>
            <div className="space-y-4">
              <div>
                <label className="flex items-center gap-2 mb-3">
                  <input
                    type="checkbox"
                    checked={config.customEndpoint?.enabled || false}
                    onChange={(e) => updateConfig('customEndpoint', {
                      ...config.customEndpoint,
                      enabled: e.target.checked
                    })}
                    className="w-4 h-4 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
                  />
                  <span className="text-sm font-medium text-navy-300">Use custom endpoint</span>
                </label>
              </div>
              {config.customEndpoint?.enabled && (
                <>
                  <div>
                    <label htmlFor="admin-airesearcher-config-endpoint-url" className="block text-sm font-medium text-navy-300 mb-2">
                      Endpoint URL
                    </label>
                    <input
                      id="admin-airesearcher-config-endpoint-url"
                      type="url"
                      value={config.customEndpoint?.url || ''}
                      onChange={(e) => updateConfig('customEndpoint', {
                        ...config.customEndpoint,
                        url: e.target.value
                      })}
                      placeholder="https://your-instance.openai.azure.com/"
                      className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                    />
                  </div>
                  <div>
                    <label htmlFor="admin-airesearcher-config-api-key" className="block text-sm font-medium text-navy-300 mb-2">
                      API Key
                    </label>
                    <input
                      id="admin-airesearcher-config-api-key"
                      type="password"
                      value={config.customEndpoint?.apiKey || ''}
                      onChange={(e) => updateConfig('customEndpoint', {
                        ...config.customEndpoint,
                        apiKey: e.target.value
                      })}
                      placeholder={config.customEndpoint?.hasApiKey ? '••••••••••••••••' : 'Enter API key'}
                      className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                    />
                    {config.customEndpoint?.hasApiKey && (
                      <p className="text-xs text-navy-500 mt-1">
                        Leave blank to keep existing API key
                      </p>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>
        )}

        {/* Response Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Response Destination</h2>
          <div className="space-y-3">
            {RESPONSE_DESTINATIONS.map((dest) => (
              <label
                key={dest.id}
                className={`flex items-center justify-between p-4 rounded-lg border cursor-pointer transition-all ${
                  config.responseDestination === dest.id
                    ? 'bg-sky-500/20 border-sky-500/30'
                    : 'bg-navy-800 border-navy-700 hover:border-navy-500'
                }`}
              >
                <div>
                  <div className="font-medium text-sky-100">{dest.name}</div>
                  <div className="text-sm text-navy-400">{dest.description}</div>
                </div>
                <input
                  type="radio"
                  name="responseDestination"
                  value={dest.id}
                  checked={config.responseDestination === dest.id}
                  onChange={(e) => updateConfig('responseDestination', e.target.value)}
                  className="w-5 h-5 text-sky-500 focus:ring-sky-500"
                />
              </label>
            ))}
          </div>
        </div>

        {/* Rate Limits */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Rate Limits</h2>
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <label htmlFor="admin-airesearcher-config-per-user-per-hour" className="block text-sm font-medium text-navy-300 mb-2">
                Per User (per hour)
              </label>
              <input
                id="admin-airesearcher-config-per-user-per-hour"
                type="number"
                min="1"
                max="100"
                value={config.rateLimitPerUser}
                onChange={(e) => updateConfig('rateLimitPerUser', parseInt(e.target.value) || 1)}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <p className="text-xs text-navy-500 mt-1">
                Maximum research requests per user per hour
              </p>
            </div>
            <div>
              <label htmlFor="admin-airesearcher-config-per-community-per-hour" className="block text-sm font-medium text-navy-300 mb-2">
                Per Community (per hour)
              </label>
              <input
                id="admin-airesearcher-config-per-community-per-hour"
                type="number"
                min="10"
                max="1000"
                value={config.rateLimitPerCommunity}
                onChange={(e) => updateConfig('rateLimitPerCommunity', parseInt(e.target.value) || 10)}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <p className="text-xs text-navy-500 mt-1">
                Maximum total research requests per hour
              </p>
            </div>
          </div>
        </div>

        {/* Advanced Features */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Advanced Features</h2>
          <div className="space-y-4">
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div>
                <div className="font-medium text-sky-100">Context Tracking</div>
                <div className="text-sm text-navy-400">
                  Remember previous conversations for contextual research
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.contextTracking}
                onChange={(e) => updateConfig('contextTracking', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>

            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div>
                <div className="font-medium text-sky-100">Stream Summary</div>
                <div className="text-sm text-navy-400">
                  Generate summaries at the end of each stream
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.streamSummaryEnabled}
                onChange={(e) => updateConfig('streamSummaryEnabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>
          </div>
        </div>

        {/* Weekly Summary */}
        <div className="card p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-sky-100">Weekly Summary</h2>
            <input
              aria-label="Weekly Summary"
              type="checkbox"
              checked={config.weeklySummaryEnabled}
              onChange={(e) => updateConfig('weeklySummaryEnabled', e.target.checked)}
              className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
            />
          </div>
          {config.weeklySummaryEnabled && (
            <div className="grid md:grid-cols-2 gap-4">
              <div>
                <label htmlFor="admin-airesearcher-config-day-of-week" className="block text-sm font-medium text-navy-300 mb-2">
                  Day of Week
                </label>
                <select
                  id="admin-airesearcher-config-day-of-week"
                  value={config.weeklySummaryDay}
                  onChange={(e) => updateConfig('weeklySummaryDay', parseInt(e.target.value))}
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                >
                  {DAYS_OF_WEEK.map((day) => (
                    <option key={day.id} value={day.id}>{day.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="admin-airesearcher-config-hour-0-23" className="block text-sm font-medium text-navy-300 mb-2">
                  Hour (0-23)
                </label>
                <input
                  id="admin-airesearcher-config-hour-0-23"
                  type="number"
                  min="0"
                  max="23"
                  value={config.weeklySummaryHour}
                  onChange={(e) => updateConfig('weeklySummaryHour', parseInt(e.target.value) || 0)}
                  className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
                <p className="text-xs text-navy-500 mt-1">
                  24-hour format (e.g., 9 for 9:00 AM, 17 for 5:00 PM)
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Bot Detection */}
        <div className="card p-6">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-semibold text-sky-100">Bot Detection</h2>
              {isPremium && (
                <span className="badge badge-gold text-xs">Premium</span>
              )}
              {!isPremium && (
                <svg className="w-5 h-5 text-gold-400" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clipRule="evenodd" />
                </svg>
              )}
            </div>
            <input
              aria-label="Bot Detection"
              type="checkbox"
              checked={config.botDetectionEnabled}
              onChange={(e) => updateConfig('botDetectionEnabled', e.target.checked)}
              disabled={!isPremium}
              className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500 disabled:opacity-50"
            />
          </div>
          {!isPremium && (
            <div className="mb-4 p-3 bg-gold-500/10 border border-gold-500/30 rounded-lg text-sm text-gold-300">
              Upgrade to Premium to enable advanced bot detection
            </div>
          )}
          {config.botDetectionEnabled && (
            <div>
              <label htmlFor="admin-airesearcher-config-input" className="block text-sm font-medium text-navy-300 mb-2">
                Confidence Threshold ({config.botConfidenceThreshold}%)
              </label>
              <input
                id="admin-airesearcher-config-input"
                type="range"
                min="50"
                max="99"
                value={config.botConfidenceThreshold}
                onChange={(e) => updateConfig('botConfidenceThreshold', parseInt(e.target.value))}
                className="w-full h-2 bg-navy-700 rounded-lg appearance-none cursor-pointer"
                style={{
                  background: `linear-gradient(to right, rgb(34 197 94) 0%, rgb(251 191 36) 50%, rgb(239 68 68) 100%)`,
                }}
              />
              <div className="flex justify-between text-xs text-navy-400 mt-1">
                <span>Low (50%)</span>
                <span>Medium (75%)</span>
                <span>High (99%)</span>
              </div>
              <p className="text-xs text-navy-500 mt-2">
                Users with confidence scores above this threshold will be flagged for review
              </p>
            </div>
          )}
        </div>

        {/* Blocked Topics */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-2">Blocked Topics</h2>
          <p className="text-sm text-navy-400 mb-4">
            Select topics that the AI should refuse to research
          </p>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            {BLOCKED_TOPICS.map((topic) => {
              const isBlocked = (config.blockedTopics || []).includes(topic);
              return (
                <button
                  key={topic}
                  onClick={() => toggleBlockedTopic(topic)}
                  className={`p-3 rounded-lg border text-sm transition-all ${
                    isBlocked
                      ? 'bg-red-500/20 text-red-300 border-red-500/30'
                      : 'bg-navy-800 text-navy-400 border-navy-700 hover:border-navy-500'
                  }`}
                >
                  <div className="flex items-center gap-2">
                    {isBlocked && (
                      <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                        <path fillRule="evenodd" d="M13.477 14.89A6 6 0 015.11 6.524l8.367 8.368zm1.414-1.414L6.524 5.11a6 6 0 018.367 8.367zM18 10a8 8 0 11-16 0 8 8 0 0116 0z" clipRule="evenodd" />
                      </svg>
                    )}
                    <span className="capitalize">{topic.replace('-', ' ')}</span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Webhook Integration */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Webhook Integration</h2>
          <div>
            <label htmlFor="admin-airesearcher-config-webhook-url-optional" className="block text-sm font-medium text-navy-300 mb-2">
              Webhook URL (Optional)
            </label>
            <input
              id="admin-airesearcher-config-webhook-url-optional"
              type="url"
              value={config.webhookUrl}
              onChange={(e) => updateConfig('webhookUrl', e.target.value)}
              placeholder="https://example.com/webhook"
              className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
            />
            <p className="text-xs text-navy-500 mt-1">
              Send research results to an external webhook for additional processing
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

export default AdminAIResearcherConfig;
