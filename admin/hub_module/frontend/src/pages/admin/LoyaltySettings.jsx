import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';

/**
 * Admin config editor for one community's MVP core-currency loyalty system
 * (gh-317) — binds directly to `services.community_loyalty.ConfigDTO`
 * (`currency_name`/`currency_symbol`/`earn_chat_points`/
 * `earn_chat_cooldown_s`/`earn_watch_points_per_min`/`earn_watch_enabled`/
 * `max_balance`/`enabled`). The pre-MVP gambling/duel/gear fields this page
 * used to expose have no server-side equivalent anymore — see
 * `hub_api/blueprints/v1/community_loyalty.py`'s module docstring.
 */
function LoyaltySettings() {
  const { communityId } = useParams();
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    fetchConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [communityId]);

  async function fetchConfig() {
    setLoading(true);
    try {
      const response = await adminApi.getLoyaltyConfig(communityId);
      setConfig(response.data.data);
    } catch (err) {
      console.error('[LoyaltySettings] Load config failed', { communityId, status: err.response?.status });
      setMessage({ type: 'error', text: 'Failed to load loyalty configuration' });
    } finally {
      setLoading(false);
    }
  }

  /** Client-side mirror of `services.community_loyalty.set_config()`'s own validation. */
  function validate(cfg) {
    if (!cfg.currency_name || !cfg.currency_name.trim()) {
      return 'Currency name must not be blank';
    }
    if (!Number.isInteger(cfg.earn_chat_points) || cfg.earn_chat_points < 0) {
      return 'Chat earn points must be a whole number >= 0';
    }
    if (!Number.isInteger(cfg.earn_chat_cooldown_s) || cfg.earn_chat_cooldown_s < 0) {
      return 'Chat cooldown must be a whole number of seconds >= 0';
    }
    if (!Number.isInteger(cfg.earn_watch_points_per_min) || cfg.earn_watch_points_per_min < 0) {
      return 'Watch points per minute must be a whole number >= 0';
    }
    if (cfg.max_balance !== null && (!Number.isInteger(cfg.max_balance) || cfg.max_balance < 0)) {
      return 'Max balance must be a whole number >= 0, or blank for unlimited';
    }
    return null;
  }

  async function handleSave() {
    const validationError = validate(config);
    if (validationError) {
      setMessage({ type: 'error', text: validationError });
      return;
    }

    setSaving(true);
    setMessage(null);
    try {
      const response = await adminApi.updateLoyaltyConfig(communityId, {
        currency_name: config.currency_name.trim(),
        currency_symbol: config.currency_symbol,
        earn_chat_points: config.earn_chat_points,
        earn_chat_cooldown_s: config.earn_chat_cooldown_s,
        earn_watch_points_per_min: config.earn_watch_points_per_min,
        earn_watch_enabled: config.earn_watch_enabled,
        max_balance: config.max_balance,
        enabled: config.enabled,
      });
      setConfig(response.data.data);
      setMessage({ type: 'success', text: 'Loyalty configuration saved successfully' });
    } catch (err) {
      console.error('[LoyaltySettings] Save config failed', { communityId, status: err.response?.status });
      const errorMsg = err.response?.data?.error?.message || 'Failed to save configuration';
      setMessage({ type: 'error', text: errorMsg });
    } finally {
      setSaving(false);
    }
  }

  function updateField(key, value) {
    setConfig({ ...config, [key]: value });
  }

  function updateInteger(key, value) {
    const numValue = parseInt(value, 10);
    updateField(key, Number.isNaN(numValue) ? 0 : numValue);
  }

  function updateNullableInteger(key, value) {
    if (value === '') {
      updateField(key, null);
      return;
    }
    const numValue = parseInt(value, 10);
    updateField(key, Number.isNaN(numValue) ? null : numValue);
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
          <h1 className="text-2xl font-bold text-sky-100">Loyalty Settings</h1>
          <p className="text-navy-400 mt-1">
            Configure the community&apos;s currency and earning rates
          </p>
        </div>
        <button
          onClick={handleSave}
          disabled={saving}
          data-testid="loyalty-save"
          className="btn btn-primary disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
      </div>

      {message && (
        <div
          data-testid="loyalty-settings-feedback"
          className={`mb-6 p-4 rounded-lg border ${
            message.type === 'success'
              ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
              : 'bg-red-500/20 text-red-300 border-red-500/30'
          }`}
        >
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">x</button>
        </div>
      )}

      <div className="space-y-6">
        {/* System Toggle */}
        <div className="card p-6">
          <label className="flex items-center justify-between cursor-pointer">
            <div>
              <div className="font-medium text-sky-100">Loyalty Enabled</div>
              <div className="text-sm text-navy-400">
                Turn the whole loyalty currency system on or off for this community
              </div>
            </div>
            <input
              type="checkbox"
              checked={config.enabled ?? true}
              onChange={(e) => updateField('enabled', e.target.checked)}
              data-testid="loyalty-enabled"
              className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
            />
          </label>
        </div>

        {/* Currency Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Currency Settings</h2>
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <label htmlFor="loyalty-settings-currency-name" className="block text-sm font-medium text-navy-300 mb-2">
                Currency Name
              </label>
              <input
                id="loyalty-settings-currency-name"
                type="text"
                value={config.currency_name ?? ''}
                onChange={(e) => updateField('currency_name', e.target.value)}
                placeholder="Points"
                data-testid="loyalty-currency-name"
                className="w-full px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                  text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <p className="text-xs text-navy-500 mt-1">E.g., &quot;Points&quot;, &quot;Coins&quot;, &quot;Waddles&quot;</p>
            </div>

            <div>
              <label htmlFor="loyalty-settings-currency-symbol" className="block text-sm font-medium text-navy-300 mb-2">
                Currency Symbol
              </label>
              <input
                id="loyalty-settings-currency-symbol"
                type="text"
                value={config.currency_symbol ?? ''}
                onChange={(e) => updateField('currency_symbol', e.target.value)}
                placeholder="$"
                maxLength={8}
                data-testid="loyalty-currency-symbol"
                className="w-full px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                  text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <p className="text-xs text-navy-500 mt-1">E.g., &quot;$&quot;, &quot;W&quot;, &quot;🪙&quot;</p>
            </div>
          </div>
        </div>

        {/* Chat Earning Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Chat Earning</h2>
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <label htmlFor="loyalty-settings-points-per-chat-message" className="block text-sm font-medium text-navy-300 mb-2">
                Points Per Chat Message
              </label>
              <input
                id="loyalty-settings-points-per-chat-message"
                type="number"
                min="0"
                step="1"
                value={config.earn_chat_points ?? 0}
                onChange={(e) => updateInteger('earn_chat_points', e.target.value)}
                data-testid="loyalty-earn-chat-points"
                className="w-full px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                  text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
            </div>

            <div>
              <label htmlFor="loyalty-settings-chat-cooldown-seconds" className="block text-sm font-medium text-navy-300 mb-2">
                Chat Cooldown (seconds)
              </label>
              <input
                id="loyalty-settings-chat-cooldown-seconds"
                type="number"
                min="0"
                step="1"
                value={config.earn_chat_cooldown_s ?? 0}
                onChange={(e) => updateInteger('earn_chat_cooldown_s', e.target.value)}
                data-testid="loyalty-earn-chat-cooldown"
                className="w-full px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                  text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
            </div>
          </div>
        </div>

        {/* Watch Earning Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Watch Time Earning</h2>
          <div className="space-y-4">
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div>
                <div className="font-medium text-sky-100">Enable Watch-Time Earning</div>
                <div className="text-sm text-navy-400">Earn currency for time spent watching</div>
              </div>
              <input
                type="checkbox"
                checked={config.earn_watch_enabled ?? false}
                onChange={(e) => updateField('earn_watch_enabled', e.target.checked)}
                data-testid="loyalty-earn-watch-enabled"
                className="w-5 h-5 rounded border-navy-600 text-sky-500 focus:ring-sky-500"
              />
            </label>

            {config.earn_watch_enabled && (
              <div className="ml-4">
                <label htmlFor="loyalty-settings-points-per-minute-watched" className="block text-sm font-medium text-navy-300 mb-2">
                  Points Per Minute Watched
                </label>
                <input
                  id="loyalty-settings-points-per-minute-watched"
                  type="number"
                  min="0"
                  step="1"
                  value={config.earn_watch_points_per_min ?? 0}
                  onChange={(e) => updateInteger('earn_watch_points_per_min', e.target.value)}
                  data-testid="loyalty-earn-watch-points"
                  className="w-full max-w-xs px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                    text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
              </div>
            )}
          </div>
        </div>

        {/* Balance Cap */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Balance Cap</h2>
          <div>
            <label htmlFor="loyalty-settings-maximum-balance" className="block text-sm font-medium text-navy-300 mb-2">
              Maximum Balance
            </label>
            <input
              id="loyalty-settings-maximum-balance"
              type="number"
              min="0"
              step="1"
              value={config.max_balance ?? ''}
              onChange={(e) => updateNullableInteger('max_balance', e.target.value)}
              placeholder="Unlimited"
              data-testid="loyalty-max-balance"
              className="w-full max-w-xs px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
            />
            <p className="text-xs text-navy-500 mt-1">Leave blank for no cap on earned points</p>
          </div>
        </div>
      </div>
    </div>
  );
}

export default LoyaltySettings;
