import { useState, useEffect } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { ArrowLeftIcon, CheckIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

function AdminLfgConfig() {
  const { communityId } = useParams();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [moduleId, setModuleId] = useState(null);

  const [config, setConfig] = useState({
    enabled: true,
    maxPartySize: 6,
    timeoutMinutes: 30,
    platforms: { discord: true, twitch: false, slack: false },
    allowCrossServer: false,
  });

  useEffect(() => {
    loadConfig();
  }, [communityId]);

  const loadConfig = async () => {
    try {
      setLoading(true);
      const response = await adminApi.getModules(communityId);
      const mod = (response.data.modules || []).find(
        (m) => m.name?.toLowerCase() === 'lfg' || m.displayName?.toLowerCase() === 'looking for group'
      );
      if (mod) {
        setModuleId(mod.moduleId);
        if (mod.config) setConfig({ ...config, ...mod.config });
      }
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to load configuration' });
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    if (!moduleId) return;
    try {
      setSaving(true);
      await adminApi.updateModuleConfig(communityId, moduleId, { config });
      setMessage({ type: 'success', text: 'LFG configuration saved successfully' });
    } catch (err) {
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to save' });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center space-x-4">
        <Link
          to={`/admin/${communityId}/modules`}
          className="p-2 text-navy-400 hover:text-sky-300 rounded-lg hover:bg-navy-800"
        >
          <ArrowLeftIcon className="w-5 h-5" />
        </Link>
        <div>
          <h1 className="text-2xl font-bold text-sky-100">LFG Configuration</h1>
          <p className="text-navy-400 mt-1">Configure the Looking For Group module</p>
        </div>
      </div>

      {message && (
        <div className={`rounded-lg p-4 flex items-center space-x-3 ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border border-red-500/30'
        }`}>
          {message.type === 'success' ? <CheckIcon className="w-5 h-5" /> : <ExclamationTriangleIcon className="w-5 h-5" />}
          <span>{message.text}</span>
        </div>
      )}

      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6 space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <label className="text-sm font-medium text-sky-100">Enabled</label>
            <p className="text-xs text-navy-400">Enable or disable the LFG module</p>
          </div>
          <button
            onClick={() => setConfig({ ...config, enabled: !config.enabled })}
            className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${config.enabled ? 'bg-gold-500' : 'bg-navy-600'}`}
          >
            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${config.enabled ? 'translate-x-6' : 'translate-x-1'}`} />
          </button>
        </div>

        <div>
          <label htmlFor="admin-lfg-config-max-party-size" className="block text-sm font-medium text-sky-100 mb-2">Max Party Size</label>
          <input
            id="admin-lfg-config-max-party-size"
            type="number"
            value={config.maxPartySize}
            onChange={(e) => setConfig({ ...config, maxPartySize: parseInt(e.target.value) || 1 })}
            min={2}
            max={100}
            className="w-32 px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 focus:border-gold-500"
          />
          <p className="text-xs text-navy-400 mt-1">Maximum number of players in a group (2-100)</p>
        </div>

        <div>
          <label htmlFor="admin-lfg-config-timeout-minutes" className="block text-sm font-medium text-sky-100 mb-2">Timeout (minutes)</label>
          <input
            id="admin-lfg-config-timeout-minutes"
            type="number"
            value={config.timeoutMinutes}
            onChange={(e) => setConfig({ ...config, timeoutMinutes: parseInt(e.target.value) || 5 })}
            min={5}
            max={1440}
            className="w-32 px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 focus:border-gold-500"
          />
          <p className="text-xs text-navy-400 mt-1">How long a group listing stays active</p>
        </div>

        <div>
          <label className="block text-sm font-medium text-sky-100 mb-2">Platforms</label>
          <div className="space-y-2">
            {Object.entries(config.platforms).map(([platform, enabled]) => (
              <label key={platform} className="flex items-center space-x-3">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setConfig({ ...config, platforms: { ...config.platforms, [platform]: e.target.checked } })}
                  className="rounded border-navy-600 bg-navy-900 text-gold-500 focus:ring-gold-500"
                />
                <span className="text-sm text-sky-100 capitalize">{platform}</span>
              </label>
            ))}
          </div>
        </div>

        <div className="flex items-center justify-between">
          <div>
            <label className="text-sm font-medium text-sky-100">Allow Cross-Server</label>
            <p className="text-xs text-navy-400">Allow LFG listings to span multiple servers</p>
          </div>
          <button
            onClick={() => setConfig({ ...config, allowCrossServer: !config.allowCrossServer })}
            className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${config.allowCrossServer ? 'bg-gold-500' : 'bg-navy-600'}`}
          >
            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${config.allowCrossServer ? 'translate-x-6' : 'translate-x-1'}`} />
          </button>
        </div>
      </div>

      <div className="flex justify-end">
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-6 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save Configuration'}
        </button>
      </div>
    </div>
  );
}

export default AdminLfgConfig;
