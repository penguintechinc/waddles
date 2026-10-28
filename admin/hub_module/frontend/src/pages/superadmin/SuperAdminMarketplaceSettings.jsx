import { useState, useEffect } from 'react';
import {
  Cog6ToothIcon,
  CheckCircleIcon,
  XCircleIcon,
} from '@heroicons/react/24/outline';
import { marketplaceAdminApi } from '../../services/api';

function centsToDisplayDollars(cents) {
  if (cents == null || cents === '') return '';
  return (Number(cents) / 100).toFixed(2);
}

function dollarsToCents(dollars) {
  if (dollars === '' || dollars == null) return 0;
  return Math.round(parseFloat(dollars) * 100);
}

export default function SuperAdminMarketplaceSettings() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Raw form state (cents for cent fields, raw numbers for others)
  const [settings, setSettings] = useState({
    platform_fee_percent: '',
    community_premium_base_price_dollars: '',
    community_premium_base_seat_limit: '',
    community_premium_overage_price_cents: '',
    payout_threshold_dollars: '',
    minimum_price_cents: '',
  });

  useEffect(() => {
    loadSettings();
  }, []);

  const loadSettings = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await marketplaceAdminApi.getSettings();
      const data = response.data?.settings || response.data || {};
      setSettings({
        platform_fee_percent: data.platform_fee_percent ?? '',
        community_premium_base_price_dollars: centsToDisplayDollars(data.community_premium_base_price_cents),
        community_premium_base_seat_limit: data.community_premium_base_seat_limit ?? '',
        community_premium_overage_price_cents: data.community_premium_overage_price_cents ?? '',
        payout_threshold_dollars: centsToDisplayDollars(data.payout_threshold_cents),
        minimum_price_cents: data.minimum_price_cents ?? '',
      });
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load settings');
    } finally {
      setLoading(false);
    }
  };

  const handleChange = (e) => {
    const { name, value } = e.target;
    setSettings((prev) => ({ ...prev, [name]: value }));
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      setError(null);
      setSuccess(null);

      const payload = {
        platform_fee_percent: parseFloat(settings.platform_fee_percent) || 0,
        community_premium_base_price_cents: dollarsToCents(settings.community_premium_base_price_dollars),
        community_premium_base_seat_limit: parseInt(settings.community_premium_base_seat_limit) || 0,
        community_premium_overage_price_cents: parseInt(settings.community_premium_overage_price_cents) || 0,
        payout_threshold_cents: dollarsToCents(settings.payout_threshold_dollars),
        minimum_price_cents: parseInt(settings.minimum_price_cents) || 0,
      };

      await marketplaceAdminApi.updateSettings(payload);
      setSuccess('Settings saved successfully.');
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to save settings');
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-surface">
        <div className="text-yellow-400">Loading marketplace settings...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-surface text-white p-6">
      <div className="max-w-2xl mx-auto">
        {/* Header */}
        <div className="mb-8 flex items-center gap-3">
          <Cog6ToothIcon className="w-8 h-8 text-yellow-400" />
          <div>
            <h1 className="text-3xl font-bold text-yellow-400">Marketplace Settings</h1>
            <p className="text-fg-muted">Global marketplace configuration</p>
          </div>
        </div>

        {/* Alerts */}
        {error && (
          <div className="mb-6 flex items-center gap-2 bg-red-900/20 border border-red-500 text-red-400 px-4 py-3 rounded-lg">
            <XCircleIcon className="w-5 h-5 flex-shrink-0" />
            <span>{error}</span>
            <button onClick={() => setError(null)} className="ml-auto font-bold">×</button>
          </div>
        )}
        {success && (
          <div className="mb-6 flex items-center gap-2 bg-green-900/20 border border-green-500 text-green-400 px-4 py-3 rounded-lg">
            <CheckCircleIcon className="w-5 h-5 flex-shrink-0" />
            <span>{success}</span>
            <button onClick={() => setSuccess(null)} className="ml-auto font-bold">×</button>
          </div>
        )}

        {/* Settings Form */}
        <div className="bg-surface-raised border border-line rounded-xl p-6 space-y-6">

          {/* Platform Fee */}
          <div>
            <label htmlFor="super-admin-marketplace-settings-platform-fee" className="block text-sm font-medium text-fg-secondary mb-1">
              Platform Fee (%)
            </label>
            <div className="flex items-center gap-2">
              <input
                id="super-admin-marketplace-settings-platform-fee"
                type="number"
                name="platform_fee_percent"
                value={settings.platform_fee_percent}
                onChange={handleChange}
                min="0"
                max="100"
                step="0.1"
                className="w-40 bg-navy-700 border border-line-strong rounded-lg px-3 py-2 text-white focus:border-yellow-500"
              />
              <span className="text-fg-muted">%</span>
            </div>
            <p className="text-fg-muted text-xs mt-1">Percentage taken from each vendor sale</p>
          </div>

          {/* Community Premium Base Price */}
          <div>
            <label className="block text-sm font-medium text-fg-secondary mb-1">
              Community Premium Base Price (USD / month)
            </label>
            <div className="flex items-center gap-2">
              <span className="text-fg-muted">$</span>
              <input
                aria-label="Community premium base price (USD per month)"
                type="number"
                name="community_premium_base_price_dollars"
                value={settings.community_premium_base_price_dollars}
                onChange={handleChange}
                min="0"
                step="0.01"
                className="w-40 bg-navy-700 border border-line-strong rounded-lg px-3 py-2 text-white focus:border-yellow-500"
              />
            </div>
            <p className="text-fg-muted text-xs mt-1">Monthly base subscription price for community premium</p>
          </div>

          {/* Base Seat Limit */}
          <div>
            <label htmlFor="super-admin-marketplace-settings-community-premium-base-seat-limit" className="block text-sm font-medium text-fg-secondary mb-1">
              Community Premium Base Seat Limit
            </label>
            <input
              id="super-admin-marketplace-settings-community-premium-base-seat-limit"
              type="number"
              name="community_premium_base_seat_limit"
              value={settings.community_premium_base_seat_limit}
              onChange={handleChange}
              min="0"
              className="w-40 bg-navy-700 border border-line-strong rounded-lg px-3 py-2 text-white focus:border-yellow-500"
            />
            <p className="text-fg-muted text-xs mt-1">Number of seats included in the base price</p>
          </div>

          {/* Overage Price per Seat */}
          <div>
            <label htmlFor="super-admin-marketplace-settings-community-premium-overage-price-per-seat" className="block text-sm font-medium text-fg-secondary mb-1">
              Community Premium Overage Price per Seat (cents)
            </label>
            <div className="flex items-center gap-2">
              <input
                id="super-admin-marketplace-settings-community-premium-overage-price-per-seat"
                type="number"
                name="community_premium_overage_price_cents"
                value={settings.community_premium_overage_price_cents}
                onChange={handleChange}
                min="0"
                className="w-40 bg-navy-700 border border-line-strong rounded-lg px-3 py-2 text-white focus:border-yellow-500"
              />
              <span className="text-fg-muted">¢ / seat</span>
            </div>
            <p className="text-fg-muted text-xs mt-1">Additional cost in cents per seat above the base limit</p>
          </div>

          {/* Payout Threshold */}
          <div>
            <label className="block text-sm font-medium text-fg-secondary mb-1">
              Payout Threshold (USD)
            </label>
            <div className="flex items-center gap-2">
              <span className="text-fg-muted">$</span>
              <input
                aria-label="Payout threshold (USD)"
                type="number"
                name="payout_threshold_dollars"
                value={settings.payout_threshold_dollars}
                onChange={handleChange}
                min="0"
                step="0.01"
                className="w-40 bg-navy-700 border border-line-strong rounded-lg px-3 py-2 text-white focus:border-yellow-500"
              />
            </div>
            <p className="text-fg-muted text-xs mt-1">Minimum balance before vendor payout is triggered</p>
          </div>

          {/* Minimum Price */}
          <div>
            <label htmlFor="super-admin-marketplace-settings-minimum-module-price-cents" className="block text-sm font-medium text-fg-secondary mb-1">
              Minimum Module Price (cents)
            </label>
            <div className="flex items-center gap-2">
              <input
                id="super-admin-marketplace-settings-minimum-module-price-cents"
                type="number"
                name="minimum_price_cents"
                value={settings.minimum_price_cents}
                onChange={handleChange}
                min="0"
                className="w-40 bg-navy-700 border border-line-strong rounded-lg px-3 py-2 text-white focus:border-yellow-500"
              />
              <span className="text-fg-muted">¢</span>
            </div>
            <p className="text-fg-muted text-xs mt-1">Minimum price allowed for paid modules (0 = no minimum)</p>
          </div>

          {/* Save Button */}
          <div className="pt-4 border-t border-line">
            <button
              onClick={handleSave}
              disabled={saving}
              className="flex items-center gap-2 px-6 py-3 bg-yellow-500 hover:bg-yellow-400 text-gray-900 font-semibold rounded-lg transition-colors disabled:opacity-50"
            >
              {saving ? 'Saving...' : 'Save Settings'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
