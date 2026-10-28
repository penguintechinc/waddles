import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import {
  LanguageIcon,
  AdjustmentsHorizontalIcon,
  KeyIcon,
  VideoCameraIcon,
  ClipboardDocumentIcon,
  FaceSmileIcon,
  CpuChipIcon,
  ShieldCheckIcon,
} from '@heroicons/react/24/outline';

const LANGUAGES = [
  { code: 'en', name: 'English' },
  { code: 'es', name: 'Spanish' },
  { code: 'fr', name: 'French' },
  { code: 'de', name: 'German' },
  { code: 'ja', name: 'Japanese' },
  { code: 'ko', name: 'Korean' },
  { code: 'zh', name: 'Chinese' },
  { code: 'pt', name: 'Portuguese' },
  { code: 'ru', name: 'Russian' },
  { code: 'ar', name: 'Arabic' },
  { code: 'hi', name: 'Hindi' },
  { code: 'it', name: 'Italian' },
];

function AdminTranslation() {
  const { communityId } = useParams();
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [overlayUrl, setOverlayUrl] = useState('');

  useEffect(() => {
    loadConfig();
  }, [communityId]);

  async function loadConfig() {
    setLoading(true);
    try {
      const response = await adminApi.getTranslationConfig(communityId);
      if (response.data.success) {
        setConfig(response.data.config);
        // Generate overlay URL if available
        if (response.data.config.overlay_key) {
          const baseUrl = window.location.origin;
          setOverlayUrl(`${baseUrl}/overlay/${communityId}/captions?key=${response.data.config.overlay_key}`);
        }
      }
    } catch (err) {
      console.error('Failed to load translation config:', err);
      setMessage({ type: 'error', text: 'Failed to load translation configuration' });
    } finally {
      setLoading(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setMessage(null);
    try {
      await adminApi.updateTranslationConfig(communityId, config);
      setMessage({ type: 'success', text: 'Translation settings saved successfully' });
    } catch (err) {
      console.error('Failed to save translation config:', err);
      const errorMsg = err.response?.data?.error?.message || 'Failed to save translation settings';
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

  function copyOverlayUrl() {
    if (overlayUrl) {
      navigator.clipboard.writeText(overlayUrl);
      setMessage({ type: 'success', text: 'Overlay URL copied to clipboard!' });
      setTimeout(() => setMessage(null), 3000);
    }
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
        Failed to load translation configuration
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Translation & Captions</h1>
          <p className="text-navy-400 mt-1">
            Configure real-time translation and closed captions for your streams
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
        {/* Translation Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <LanguageIcon className="w-5 h-5" />
            Translation Settings
          </h2>
          <div className="space-y-4">
            {/* Enable Translation */}
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div className="flex items-center gap-3">
                <div>
                  <div className="font-medium text-sky-100">Enable Translation</div>
                  <div className="text-sm text-navy-400">
                    Automatically translate chat messages to selected target language
                  </div>
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.enabled || false}
                onChange={(e) => updateConfig('enabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>

            {/* Target Language */}
            <div>
              <label htmlFor="admin-translation-target-language" className="block text-sm font-medium text-navy-300 mb-2">
                Target Language
              </label>
              <select
                id="admin-translation-target-language"
                value={config.target_language || 'en'}
                onChange={(e) => updateConfig('target_language', e.target.value)}
                disabled={!config.enabled}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500 disabled:opacity-50"
              >
                {LANGUAGES.map((lang) => (
                  <option key={lang.code} value={lang.code}>
                    {lang.name}
                  </option>
                ))}
              </select>
              <p className="text-xs text-navy-500 mt-1">
                Messages will be translated to this language
              </p>
            </div>
          </div>
        </div>

        {/* Advanced Translation Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <AdjustmentsHorizontalIcon className="w-5 h-5" />
            Advanced Translation Settings
          </h2>
          <div className="space-y-4">
            {/* Language Detection Method */}
            <div>
              <label htmlFor="admin-translation-language-detection-method" className="block text-sm font-medium text-navy-300 mb-2">
                Language Detection Method
              </label>
              <select
                id="admin-translation-language-detection-method"
                value={config.detection_method || 'ensemble'}
                onChange={(e) => updateConfig('detection_method', e.target.value)}
                disabled={!config.enabled}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500 disabled:opacity-50"
              >
                <option value="ensemble">Ensemble (FastText + Lingua + Langdetect) - Recommended</option>
                <option value="fasttext_only">FastText Only (Fast, good for long messages)</option>
                <option value="provider">Provider Detection (Google Cloud / Googletrans fallback)</option>
              </select>
              <p className="text-xs text-navy-500 mt-1">
                Ensemble provides the best accuracy by combining multiple detection libraries with consensus voting
              </p>
            </div>

            {/* Confidence Threshold */}
            <div>
              <label htmlFor="admin-translation-input" className="block text-sm font-medium text-navy-300 mb-2">
                Confidence Threshold: {config.confidence_threshold?.toFixed(2) || '0.70'}
              </label>
              <div className="flex items-center gap-4">
                <input
                  id="admin-translation-input"
                  type="range"
                  min="0.5"
                  max="1.0"
                  step="0.05"
                  value={config.confidence_threshold || 0.7}
                  onChange={(e) => updateConfig('confidence_threshold', parseFloat(e.target.value))}
                  disabled={!config.enabled}
                  className="flex-1 h-2 bg-navy-700 rounded-lg appearance-none cursor-pointer disabled:opacity-50"
                />
                <div className="w-16 text-center">
                  <span className="font-bold text-sky-100">
                    {((config.confidence_threshold || 0.7) * 100).toFixed(0)}%
                  </span>
                </div>
              </div>
              <p className="text-xs text-navy-500 mt-1">
                Only translate messages with confidence above this threshold
              </p>
            </div>

            {/* Minimum Words */}
            <div>
              <label htmlFor="admin-translation-minimum-words-to-translate" className="block text-sm font-medium text-navy-300 mb-2">
                Minimum Words to Translate
              </label>
              <input
                id="admin-translation-minimum-words-to-translate"
                type="number"
                min="1"
                max="20"
                value={config.min_words || 5}
                onChange={(e) => updateConfig('min_words', parseInt(e.target.value))}
                disabled={!config.enabled}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500 disabled:opacity-50"
              />
              <p className="text-xs text-navy-500 mt-1">
                Only translate messages with at least this many words
              </p>
            </div>

            {/* Ensemble Method Info */}
            {config.detection_method === 'ensemble' && (
              <div className="p-3 bg-sky-500/10 border border-sky-500/30 rounded-lg">
                <p className="text-xs text-sky-300">
                  <strong>Ensemble Method:</strong> Combines FastText (fast), Lingua (accurate), and Langdetect (validation) for high-confidence detection.
                  Confidence scores: All 3 agree (95%), 2 agree (85%), 1 only (70%).
                </p>
              </div>
            )}
          </div>
        </div>

        {/* Token Preservation & Emotes */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <ShieldCheckIcon className="w-5 h-5" />
            Token Preservation
          </h2>
          <p className="text-sm text-navy-400 mb-4">
            Preserve these elements during translation (they won&apos;t be translated)
          </p>
          <div className="grid grid-cols-2 gap-3">
            <label className="flex items-center p-3 bg-navy-800 rounded-lg cursor-pointer">
              <input
                type="checkbox"
                checked={config.preprocessing?.preserve_mentions ?? true}
                onChange={(e) => updateNestedConfig('preprocessing', 'preserve_mentions', e.target.checked)}
                disabled={!config.enabled}
                className="w-4 h-4 rounded border-navy-600 text-sky-500 focus:ring-sky-500 mr-3"
              />
              <div>
                <div className="text-sm font-medium text-sky-100">@Mentions</div>
                <div className="text-xs text-navy-500">@username patterns</div>
              </div>
            </label>

            <label className="flex items-center p-3 bg-navy-800 rounded-lg cursor-pointer">
              <input
                type="checkbox"
                checked={config.preprocessing?.preserve_commands ?? true}
                onChange={(e) => updateNestedConfig('preprocessing', 'preserve_commands', e.target.checked)}
                disabled={!config.enabled}
                className="w-4 h-4 rounded border-navy-600 text-sky-500 focus:ring-sky-500 mr-3"
              />
              <div>
                <div className="text-sm font-medium text-sky-100">!Commands</div>
                <div className="text-xs text-navy-500">!help, #tag patterns</div>
              </div>
            </label>

            <label className="flex items-center p-3 bg-navy-800 rounded-lg cursor-pointer">
              <input
                type="checkbox"
                checked={config.preprocessing?.preserve_emails ?? true}
                onChange={(e) => updateNestedConfig('preprocessing', 'preserve_emails', e.target.checked)}
                disabled={!config.enabled}
                className="w-4 h-4 rounded border-navy-600 text-sky-500 focus:ring-sky-500 mr-3"
              />
              <div>
                <div className="text-sm font-medium text-sky-100">Emails</div>
                <div className="text-xs text-navy-500">user@example.com</div>
              </div>
            </label>

            <label className="flex items-center p-3 bg-navy-800 rounded-lg cursor-pointer">
              <input
                type="checkbox"
                checked={config.preprocessing?.preserve_urls ?? true}
                onChange={(e) => updateNestedConfig('preprocessing', 'preserve_urls', e.target.checked)}
                disabled={!config.enabled}
                className="w-4 h-4 rounded border-navy-600 text-sky-500 focus:ring-sky-500 mr-3"
              />
              <div>
                <div className="text-sm font-medium text-sky-100">URLs</div>
                <div className="text-xs text-navy-500">http/https links</div>
              </div>
            </label>
          </div>
        </div>

        {/* Emote Detection */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <FaceSmileIcon className="w-5 h-5" />
            Emote Detection
          </h2>
          <div className="space-y-4">
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div>
                <div className="font-medium text-sky-100">Preserve Platform Emotes</div>
                <div className="text-sm text-navy-400">
                  Detect and preserve emotes from Twitch, BTTV, FFZ, 7TV, Discord, and Slack
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.preprocessing?.preserve_emotes ?? true}
                onChange={(e) => updateNestedConfig('preprocessing', 'preserve_emotes', e.target.checked)}
                disabled={!config.enabled}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>

            {config.preprocessing?.preserve_emotes && (
              <div>
                <label className="block text-sm font-medium text-navy-300 mb-2">
                  Emote Sources
                </label>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                  {['global', 'bttv', 'ffz', '7tv'].map((source) => (
                    <label
                      key={source}
                      className="flex items-center p-2 bg-navy-800 rounded-lg cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        checked={(config.preprocessing?.emote_sources || ['global', 'bttv', 'ffz', '7tv']).includes(source)}
                        onChange={(e) => {
                          const current = config.preprocessing?.emote_sources || ['global', 'bttv', 'ffz', '7tv'];
                          const updated = e.target.checked
                            ? [...current, source]
                            : current.filter((s) => s !== source);
                          updateNestedConfig('preprocessing', 'emote_sources', updated);
                        }}
                        disabled={!config.enabled}
                        className="w-4 h-4 rounded border-navy-600 text-sky-500 focus:ring-sky-500 mr-2"
                      />
                      <span className="text-sm text-sky-100 uppercase">{source}</span>
                    </label>
                  ))}
                </div>
                <p className="text-xs text-navy-500 mt-2">
                  Select which emote providers to check (Twitch global, BTTV, FFZ, 7TV)
                </p>
              </div>
            )}
          </div>
        </div>

        {/* AI Decision Mode */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <CpuChipIcon className="w-5 h-5" />
            AI-Powered Emote Detection
          </h2>
          <p className="text-sm text-navy-400 mb-4">
            Use AI to determine if unknown patterns (like new emotes or slang) should be translated
          </p>
          <div className="space-y-4">
            <div>
              <label htmlFor="admin-translation-ai-decision-mode" className="block text-sm font-medium text-navy-300 mb-2">
                AI Decision Mode
              </label>
              <select
                id="admin-translation-ai-decision-mode"
                value={config.ai_decision?.mode || 'never'}
                onChange={(e) => updateNestedConfig('ai_decision', 'mode', e.target.value)}
                disabled={!config.enabled}
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500 disabled:opacity-50"
              >
                <option value="never">Never - Only use cached emotes</option>
                <option value="uncertain_only">Uncertain Only - Ask AI for unknown patterns</option>
                <option value="always">Always - AI checks all potential emotes</option>
              </select>
              <p className="text-xs text-navy-500 mt-1">
                &quot;Uncertain Only&quot; is recommended for best balance of accuracy and performance
              </p>
            </div>

            {config.ai_decision?.mode && config.ai_decision.mode !== 'never' && (
              <div>
                <label htmlFor="admin-translation-input-2" className="block text-sm font-medium text-navy-300 mb-2">
                  AI Confidence Threshold: {(config.ai_decision?.confidence_threshold || 0.7).toFixed(2)}
                </label>
                <div className="flex items-center gap-4">
                  <input
                    id="admin-translation-input-2"
                    type="range"
                    min="0.5"
                    max="0.95"
                    step="0.05"
                    value={config.ai_decision?.confidence_threshold || 0.7}
                    onChange={(e) => updateNestedConfig('ai_decision', 'confidence_threshold', parseFloat(e.target.value))}
                    disabled={!config.enabled}
                    className="flex-1 h-2 bg-navy-700 rounded-lg appearance-none cursor-pointer disabled:opacity-50"
                  />
                  <div className="w-16 text-center">
                    <span className="font-bold text-sky-100">
                      {((config.ai_decision?.confidence_threshold || 0.7) * 100).toFixed(0)}%
                    </span>
                  </div>
                </div>
                <p className="text-xs text-navy-500 mt-1">
                  Minimum AI confidence required to trust the decision
                </p>
              </div>
            )}
          </div>
        </div>

        {/* API Configuration */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <KeyIcon className="w-5 h-5" />
            API Configuration
          </h2>
          <div className="space-y-4">
            <div>
              <label htmlFor="admin-translation-google-cloud-translation-api-key" className="block text-sm font-medium text-navy-300 mb-2">
                Google Cloud Translation API Key
              </label>
              <input
                id="admin-translation-google-cloud-translation-api-key"
                type="password"
                value={config.google_api_key || ''}
                onChange={(e) => updateConfig('google_api_key', e.target.value)}
                placeholder="Enter your Google Cloud API key (optional)"
                className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 placeholder-navy-500 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <p className="text-xs text-navy-500 mt-1">
                Optional: Provide your own Google Cloud Translation API key for better performance
              </p>
            </div>
          </div>
        </div>

        {/* Closed Captions Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2">
            <VideoCameraIcon className="w-5 h-5" />
            Closed Captions
          </h2>
          <div className="space-y-4">
            {/* Enable Captions */}
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div className="flex items-center gap-3">
                <div>
                  <div className="font-medium text-sky-100">Enable Closed Captions</div>
                  <div className="text-sm text-navy-400">
                    Display real-time captions overlay for streams
                  </div>
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.captions?.enabled || false}
                onChange={(e) => updateNestedConfig('captions', 'enabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>

            {config.captions?.enabled && (
              <>
                {/* Display Duration */}
                <div>
                  <label htmlFor="admin-translation-input-3" className="block text-sm font-medium text-navy-300 mb-2">
                    Caption Display Duration: {config.captions?.display_duration || 5000}ms
                  </label>
                  <div className="flex items-center gap-4">
                    <input
                      id="admin-translation-input-3"
                      type="range"
                      min="1000"
                      max="30000"
                      step="1000"
                      value={config.captions?.display_duration || 5000}
                      onChange={(e) => updateNestedConfig('captions', 'display_duration', parseInt(e.target.value))}
                      className="flex-1 h-2 bg-navy-700 rounded-lg appearance-none cursor-pointer"
                    />
                    <div className="w-20 text-center">
                      <span className="font-bold text-sky-100">
                        {((config.captions?.display_duration || 5000) / 1000).toFixed(1)}s
                      </span>
                    </div>
                  </div>
                  <p className="text-xs text-navy-500 mt-1">
                    How long each caption should remain visible
                  </p>
                </div>

                {/* Max Captions */}
                <div>
                  <label htmlFor="admin-translation-maximum-captions-on-screen" className="block text-sm font-medium text-navy-300 mb-2">
                    Maximum Captions on Screen
                  </label>
                  <input
                    id="admin-translation-maximum-captions-on-screen"
                    type="number"
                    min="1"
                    max="10"
                    value={config.captions?.max_captions || 3}
                    onChange={(e) => updateNestedConfig('captions', 'max_captions', parseInt(e.target.value))}
                    className="w-full px-4 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                  />
                  <p className="text-xs text-navy-500 mt-1">
                    Maximum number of caption lines to display simultaneously
                  </p>
                </div>

                {/* Show Original */}
                <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
                  <div className="flex items-center gap-3">
                    <div>
                      <div className="font-medium text-sky-100">Show Original Text</div>
                      <div className="text-sm text-navy-400">
                        Display the original message alongside translation
                      </div>
                    </div>
                  </div>
                  <input
                    type="checkbox"
                    checked={config.captions?.show_original || false}
                    onChange={(e) => updateNestedConfig('captions', 'show_original', e.target.checked)}
                    className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
                  />
                </label>

                {/* Overlay URL */}
                {overlayUrl && (
                  <div className="p-4 bg-navy-800 rounded-lg border border-navy-700">
                    <div className="flex items-center justify-between mb-2">
                      <label className="text-sm font-medium text-navy-300">
                        Caption Overlay URL
                      </label>
                      <button
                        onClick={copyOverlayUrl}
                        className="btn btn-secondary text-sm flex items-center gap-2"
                      >
                        <ClipboardDocumentIcon className="w-4 h-4" />
                        Copy URL
                      </button>
                    </div>
                    <div className="p-3 bg-navy-900 rounded border border-navy-600 font-mono text-xs text-sky-300 break-all">
                      {overlayUrl}
                    </div>
                    <p className="text-xs text-navy-500 mt-2">
                      Add this URL as a Browser Source in OBS or your streaming software
                    </p>
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        {/* Save Button (bottom) */}
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

export default AdminTranslation;
