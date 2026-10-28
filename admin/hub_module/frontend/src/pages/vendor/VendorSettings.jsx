/**
 * Vendor Settings
 * Vendor profile settings: display name, description, website, payout method, notifications
 */
import { useEffect, useState } from 'react';
import { Cog6ToothIcon, CheckCircleIcon, XCircleIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';

const PAYOUT_METHODS = [
  { value: 'stripe_connect', label: 'Stripe Connect' },
  { value: 'paypal', label: 'PayPal' },
];

const DEFAULT_NOTIFICATIONS = {
  submissionApproved: true,
  submissionRejected: true,
  newReview: true,
  newInstall: false,
  revenueThreshold: true,
};

function VendorSettings() {
  const [form, setForm] = useState({
    displayName: '',
    description: '',
    websiteUrl: '',
    payoutMethod: 'stripe_connect',
    notifications: { ...DEFAULT_NOTIFICATIONS },
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState(null); // { type: 'success'|'error', message }

  useEffect(() => {
    loadProfile();
  }, []);

  const loadProfile = async () => {
    try {
      setLoading(true);
      const response = await api.get('/vendor/profile');
      const data = response.data;
      setForm({
        displayName: data.displayName || '',
        description: data.description || '',
        websiteUrl: data.websiteUrl || '',
        payoutMethod: data.payoutMethod || 'stripe_connect',
        notifications: { ...DEFAULT_NOTIFICATIONS, ...(data.notifications || {}) },
      });
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Failed to load profile');
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async (e) => {
    e.preventDefault();
    try {
      setSaving(true);
      await api.put('/vendor/profile', form);
      showToast('success', 'Settings saved successfully');
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Failed to save settings');
    } finally {
      setSaving(false);
    }
  };

  const showToast = (type, message) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 4000);
  };

  const setField = (field, value) =>
    setForm((prev) => ({ ...prev, [field]: value }));

  const setNotification = (key, value) =>
    setForm((prev) => ({
      ...prev,
      notifications: { ...prev.notifications, [key]: value },
    }));

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div className="space-y-8 max-w-2xl">
      {/* Header */}
      <div className="flex items-center space-x-3">
        <Cog6ToothIcon className="w-8 h-8 text-gold-400" />
        <div>
          <h1 className="text-3xl font-bold text-white">Vendor Settings</h1>
          <p className="text-navy-300 mt-1">Manage your vendor profile and preferences</p>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div
          className={`flex items-center space-x-3 px-4 py-3 rounded-lg border ${
            toast.type === 'success'
              ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400'
              : 'bg-red-500/10 border-red-500/20 text-red-400'
          }`}
        >
          {toast.type === 'success' ? (
            <CheckCircleIcon className="w-5 h-5 flex-shrink-0" />
          ) : (
            <XCircleIcon className="w-5 h-5 flex-shrink-0" />
          )}
          <span>{toast.message}</span>
        </div>
      )}

      <form onSubmit={handleSave} className="space-y-6">
        {/* Profile section */}
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-6 space-y-5">
          <h2 className="text-lg font-bold text-white border-b border-navy-700 pb-3">
            Public Profile
          </h2>

          {/* Display Name */}
          <div>
            <label htmlFor="vendor-settings-input" className="block text-sm font-medium text-navy-300 mb-2">
              Display Name <span className="text-red-400">*</span>
            </label>
            <input
              id="vendor-settings-input"
              type="text"
              value={form.displayName}
              onChange={(e) => setField('displayName', e.target.value)}
              required
              maxLength={100}
              placeholder="Your vendor display name"
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400"
            />
          </div>

          {/* Description */}
          <div>
            <label htmlFor="vendor-settings-description" className="block text-sm font-medium text-navy-300 mb-2">Description</label>
            <textarea
              id="vendor-settings-description"
              value={form.description}
              onChange={(e) => setField('description', e.target.value)}
              rows={4}
              maxLength={500}
              placeholder="Describe yourself or your organization..."
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400 resize-none"
            />
            <p className="text-xs text-navy-500 mt-1">{form.description.length}/500</p>
          </div>

          {/* Website URL */}
          <div>
            <label htmlFor="vendor-settings-website-url" className="block text-sm font-medium text-navy-300 mb-2">Website URL</label>
            <input
              id="vendor-settings-website-url"
              type="url"
              value={form.websiteUrl}
              onChange={(e) => setField('websiteUrl', e.target.value)}
              placeholder="https://your-website.com"
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400"
            />
          </div>
        </div>

        {/* Payout section */}
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-6 space-y-5">
          <h2 className="text-lg font-bold text-white border-b border-navy-700 pb-3">
            Payout Settings
          </h2>

          <div>
            <label htmlFor="vendor-settings-payout-method" className="block text-sm font-medium text-navy-300 mb-2">Payout Method</label>
            <select
              id="vendor-settings-payout-method"
              value={form.payoutMethod}
              onChange={(e) => setField('payoutMethod', e.target.value)}
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white focus:border-gold-400"
            >
              {PAYOUT_METHODS.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
            <p className="text-xs text-navy-500 mt-2">
              Payout account linking is configured separately. Contact support if you need help
              connecting your payout account.
            </p>
          </div>
        </div>

        {/* Notification preferences */}
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-6 space-y-4">
          <h2 className="text-lg font-bold text-white border-b border-navy-700 pb-3">
            Notification Preferences
          </h2>

          {[
            { key: 'submissionApproved', label: 'Module approved', description: 'When a submission is approved for the marketplace' },
            { key: 'submissionRejected', label: 'Module rejected', description: 'When a submission is rejected with feedback' },
            { key: 'newReview', label: 'New review posted', description: 'When a user posts a review on one of your modules' },
            { key: 'newInstall', label: 'New install', description: 'Each time a community installs one of your modules' },
            { key: 'revenueThreshold', label: 'Revenue milestone', description: 'When your monthly revenue crosses a threshold' },
          ].map(({ key, label, description }) => (
            <label key={key} className="flex items-start space-x-3 cursor-pointer group">
              <div className="pt-0.5">
                <input
                  type="checkbox"
                  checked={form.notifications[key] ?? false}
                  onChange={(e) => setNotification(key, e.target.checked)}
                  className="w-4 h-4 rounded border-navy-600 bg-navy-900 text-sky-500 focus:ring-sky-500"
                />
              </div>
              <div>
                <p className="text-white text-sm font-medium group-hover:text-sky-300 transition-colors">
                  {label}
                </p>
                <p className="text-xs text-navy-400">{description}</p>
              </div>
            </label>
          ))}
        </div>

        {/* Save button */}
        <div className="flex justify-end">
          <button
            type="submit"
            disabled={saving}
            className="bg-sky-600 hover:bg-sky-700 disabled:opacity-50 disabled:cursor-not-allowed text-white px-6 py-2 rounded-lg transition-colors font-medium"
          >
            {saving ? 'Saving…' : 'Save Settings'}
          </button>
        </div>
      </form>
    </div>
  );
}

export default VendorSettings;
