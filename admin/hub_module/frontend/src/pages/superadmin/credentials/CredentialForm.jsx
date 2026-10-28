import { useState, useEffect } from 'react';
import { superAdminApi } from '../../../services/api';
import { Save, X, RefreshCw, Eye, EyeOff } from 'lucide-react';
import { getAllPlatformOptions } from '../../../utils/platformConfig';

const PLATFORMS = [
  ...getAllPlatformOptions()
    .filter((p) => p.id !== 'hub')
    .map((p) => ({ value: p.id, label: p.label })),
  { value: 'spotify', label: 'Spotify' },
  { value: 'reddit', label: 'Reddit' },
  { value: 'twitter', label: 'Twitter/X' },
];

export default function CredentialForm({ credential, integrationType, onSave, onCancel }) {
  const [formData, setFormData] = useState({
    platform: credential?.platform || '',
    accessToken: '',
    refreshToken: '',
    clientId: credential?.clientId || '',
    clientSecret: '',
    expiresAt: credential?.expiresAt || '',
    scopes: credential?.scopes?.join(' ') || '',
    configData: credential?.configData ? JSON.stringify(credential.configData, null, 2) : '{}',
    isActive: credential?.isActive !== false,
  });

  const [errors, setErrors] = useState({});
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [showSecrets, setShowSecrets] = useState({
    accessToken: false,
    refreshToken: false,
    clientSecret: false,
  });

  const handleChange = (e) => {
    const { name, value, checked, type } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: type === 'checkbox' ? checked : value,
    }));
    // Clear error for this field
    if (errors[name]) {
      setErrors((prev) => ({ ...prev, [name]: undefined }));
    }
  };

  const toggleSecretVisibility = (field) => {
    setShowSecrets((prev) => ({ ...prev, [field]: !prev[field] }));
  };

  const validateForm = () => {
    const newErrors = {};
    if (!formData.platform) {
      newErrors.platform = 'Platform is required';
    }
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();

    if (!validateForm()) return;

    try {
      const submitData = {
        platform: formData.platform,
        integrationType,
        accessToken: formData.accessToken || undefined,
        refreshToken: formData.refreshToken || undefined,
        clientId: formData.clientId || undefined,
        clientSecret: formData.clientSecret || undefined,
        expiresAt: formData.expiresAt || undefined,
        scopes: formData.scopes
          ? formData.scopes.split(' ').filter((s) => s.trim())
          : [],
        configData: formData.configData ? JSON.parse(formData.configData) : {},
        isActive: formData.isActive,
      };

      await onSave(submitData);
    } catch (error) {
      setErrors({ submit: error.message });
    }
  };

  const handleTest = async () => {
    if (!formData.platform) {
      setErrors({ ...errors, platform: 'Platform is required to test' });
      return;
    }

    setTesting(true);
    try {
      const response = await superAdminApi.testPlatformConnection(formData.platform);
      setTestResult({
        success: response.data.success,
        message: response.data.message,
      });
    } catch (error) {
      setTestResult({
        success: false,
        message: error.response?.data?.message || error.message,
      });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="card">
      <form onSubmit={handleSubmit} className="space-y-6 p-6">
        <div>
          <h3 className="text-lg font-bold text-sky-100 mb-4">
            {credential ? 'Edit' : 'Add'} {integrationType} Credential
          </h3>
        </div>

        {/* Error Summary */}
        {errors.submit && (
          <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-400 text-sm">
            {errors.submit}
          </div>
        )}

        {/* Platform Selection */}
        <div>
          <label htmlFor="credential-form-select" className="block text-sm font-medium text-sky-100 mb-2">
            Platform <span className="text-red-400">*</span>
          </label>
          <select
            id="credential-form-select"
            name="platform"
            value={formData.platform}
            onChange={handleChange}
            disabled={!!credential}
            className="w-full px-3 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:ring-2 focus:ring-gold-500 focus:border-gold-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <option value="">Select a platform...</option>
            {PLATFORMS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          {errors.platform && (
            <p className="text-red-400 text-xs mt-1">{errors.platform}</p>
          )}
        </div>

        {/* Access Token */}
        <div>
          <label htmlFor="credential-form-access-token" className="block text-sm font-medium text-sky-100 mb-2">
            Access Token
          </label>
          <div className="relative">
            <input
              id="credential-form-access-token"
              type={showSecrets.accessToken ? 'text' : 'password'}
              name="accessToken"
              value={formData.accessToken}
              onChange={handleChange}
              placeholder="OAuth access token or API key"
              className="w-full px-3 py-2 pr-10 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:ring-2 focus:ring-gold-500 focus:border-gold-500"
            />
            <button
              type="button"
              onClick={() => toggleSecretVisibility('accessToken')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-navy-400 hover:text-sky-300"
            >
              {showSecrets.accessToken ? (
                <EyeOff className="w-4 h-4" />
              ) : (
                <Eye className="w-4 h-4" />
              )}
            </button>
          </div>
        </div>

        {/* Refresh Token */}
        <div>
          <label htmlFor="credential-form-refresh-token-optional" className="block text-sm font-medium text-sky-100 mb-2">
            Refresh Token <span className="text-xs text-navy-400">(Optional)</span>
          </label>
          <div className="relative">
            <input
              id="credential-form-refresh-token-optional"
              type={showSecrets.refreshToken ? 'text' : 'password'}
              name="refreshToken"
              value={formData.refreshToken}
              onChange={handleChange}
              placeholder="OAuth refresh token"
              className="w-full px-3 py-2 pr-10 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:ring-2 focus:ring-gold-500 focus:border-gold-500"
            />
            <button
              type="button"
              onClick={() => toggleSecretVisibility('refreshToken')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-navy-400 hover:text-sky-300"
            >
              {showSecrets.refreshToken ? (
                <EyeOff className="w-4 h-4" />
              ) : (
                <Eye className="w-4 h-4" />
              )}
            </button>
          </div>
        </div>

        {/* Client ID */}
        <div>
          <label htmlFor="credential-form-client-id-optional" className="block text-sm font-medium text-sky-100 mb-2">
            Client ID <span className="text-xs text-navy-400">(Optional)</span>
          </label>
          <input
            id="credential-form-client-id-optional"
            type="text"
            name="clientId"
            value={formData.clientId}
            onChange={handleChange}
            placeholder="OAuth Client ID"
            className="w-full px-3 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:ring-2 focus:ring-gold-500 focus:border-gold-500"
          />
        </div>

        {/* Client Secret */}
        <div>
          <label htmlFor="credential-form-client-secret-optional" className="block text-sm font-medium text-sky-100 mb-2">
            Client Secret <span className="text-xs text-navy-400">(Optional)</span>
          </label>
          <div className="relative">
            <input
              id="credential-form-client-secret-optional"
              type={showSecrets.clientSecret ? 'text' : 'password'}
              name="clientSecret"
              value={formData.clientSecret}
              onChange={handleChange}
              placeholder="OAuth Client Secret"
              className="w-full px-3 py-2 pr-10 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:ring-2 focus:ring-gold-500 focus:border-gold-500"
            />
            <button
              type="button"
              onClick={() => toggleSecretVisibility('clientSecret')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-navy-400 hover:text-sky-300"
            >
              {showSecrets.clientSecret ? (
                <EyeOff className="w-4 h-4" />
              ) : (
                <Eye className="w-4 h-4" />
              )}
            </button>
          </div>
        </div>

        {/* Expires At */}
        <div>
          <label htmlFor="credential-form-expires-at-optional" className="block text-sm font-medium text-sky-100 mb-2">
            Expires At <span className="text-xs text-navy-400">(Optional)</span>
          </label>
          <input
            id="credential-form-expires-at-optional"
            type="datetime-local"
            name="expiresAt"
            value={formData.expiresAt}
            onChange={handleChange}
            className="w-full px-3 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:ring-2 focus:ring-gold-500 focus:border-gold-500"
          />
        </div>

        {/* Scopes */}
        <div>
          <label htmlFor="credential-form-scopes-space-separated" className="block text-sm font-medium text-sky-100 mb-2">
            Scopes <span className="text-xs text-navy-400">(Space-separated)</span>
          </label>
          <input
            id="credential-form-scopes-space-separated"
            type="text"
            name="scopes"
            value={formData.scopes}
            onChange={handleChange}
            placeholder="e.g., user:read channel:read"
            className="w-full px-3 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 focus:ring-2 focus:ring-gold-500 focus:border-gold-500"
          />
        </div>

        {/* Config Data */}
        <div>
          <label htmlFor="credential-form-config-data-json-optional" className="block text-sm font-medium text-sky-100 mb-2">
            Config Data <span className="text-xs text-navy-400">(JSON, Optional)</span>
          </label>
          <textarea
            id="credential-form-config-data-json-optional"
            name="configData"
            value={formData.configData}
            onChange={handleChange}
            rows={4}
            placeholder="{}"
            className="w-full px-3 py-2 bg-navy-800 border border-navy-600 rounded-lg text-sky-100 font-mono text-xs focus:ring-2 focus:ring-gold-500 focus:border-gold-500"
          />
        </div>

        {/* Active Status */}
        <div className="flex items-center gap-3">
          <input
            type="checkbox"
            id="isActive"
            name="isActive"
            checked={formData.isActive}
            onChange={handleChange}
            className="w-4 h-4 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-2 focus:ring-gold-500"
          />
          <label htmlFor="isActive" className="text-sm font-medium text-sky-100">
            Active
          </label>
        </div>

        {/* Test Result */}
        {testResult && (
          <div
            className={`p-3 rounded-lg ${
              testResult.success
                ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                : 'bg-red-500/20 text-red-400 border border-red-500/30'
            }`}
          >
            <div className="flex items-center gap-2">
              {testResult.success ? '✓' : '✗'} {testResult.message}
            </div>
          </div>
        )}

        {/* Action Buttons */}
        <div className="flex items-center gap-3 pt-4 border-t border-navy-700">
          <button
            type="submit"
            className="flex items-center gap-2 px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-950 font-medium rounded-lg transition-colors"
          >
            <Save className="w-4 h-4" />
            Save
          </button>

          {credential && (
            <button
              type="button"
              onClick={handleTest}
              disabled={testing}
              className="flex items-center gap-2 px-4 py-2 border border-navy-600 text-navy-300 hover:bg-navy-700 rounded-lg transition-colors disabled:opacity-50"
            >
              {testing ? (
                <RefreshCw className="w-4 h-4 animate-spin" />
              ) : (
                <RefreshCw className="w-4 h-4" />
              )}
              Test
            </button>
          )}

          <button
            type="button"
            onClick={onCancel}
            className="flex items-center gap-2 px-4 py-2 border border-navy-600 text-navy-300 hover:bg-navy-700 rounded-lg transition-colors ml-auto"
          >
            <X className="w-4 h-4" />
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}
