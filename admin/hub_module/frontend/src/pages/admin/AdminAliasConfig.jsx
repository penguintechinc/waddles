import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeftIcon, CheckIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

function AdminAliasConfig() {
  const { communityId } = useParams();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [moduleId, setModuleId] = useState(null);

  const [config, setConfig] = useState({
    enabled: true,
    maxAliasesPerUser: 5,
    allowCustomAliases: true,
  });

  useEffect(() => { loadConfig(); }, [communityId]);

  const loadConfig = async () => {
    try {
      setLoading(true);
      const response = await adminApi.getModules(communityId);
      const mod = (response.data.modules || []).find((m) => m.name?.toLowerCase() === 'alias');
      if (mod) { setModuleId(mod.moduleId); if (mod.config) setConfig({ ...config, ...mod.config }); }
    } catch { setMessage({ type: 'error', text: 'Failed to load configuration' }); }
    finally { setLoading(false); }
  };

  const handleSave = async () => {
    if (!moduleId) return;
    try {
      setSaving(true);
      await adminApi.updateModuleConfig(communityId, moduleId, { config });
      setMessage({ type: 'success', text: 'Alias configuration saved successfully' });
    } catch (err) { setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to save' }); }
    finally { setSaving(false); }
  };

  if (loading) return <div className="flex items-center justify-center h-64"><div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div></div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center space-x-4">
        <Link to={`/admin/${communityId}/modules`} className="p-2 text-navy-400 hover:text-sky-300 rounded-lg hover:bg-navy-800"><ArrowLeftIcon className="w-5 h-5" /></Link>
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Alias Configuration</h1>
          <p className="text-navy-400 mt-1">Configure the Alias module</p>
        </div>
      </div>

      {message && (
        <div className={`rounded-lg p-4 flex items-center space-x-3 ${message.type === 'success' ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'bg-red-500/20 text-red-300 border border-red-500/30'}`}>
          {message.type === 'success' ? <CheckIcon className="w-5 h-5" /> : <ExclamationTriangleIcon className="w-5 h-5" />}
          <span>{message.text}</span>
        </div>
      )}

      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6 space-y-6">
        <div className="flex items-center justify-between">
          <div><label className="text-sm font-medium text-sky-100">Enabled</label><p className="text-xs text-navy-400">Enable or disable the Alias module</p></div>
          <button onClick={() => setConfig({ ...config, enabled: !config.enabled })} className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${config.enabled ? 'bg-gold-500' : 'bg-navy-600'}`}>
            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${config.enabled ? 'translate-x-6' : 'translate-x-1'}`} />
          </button>
        </div>

        <div>
          <label htmlFor="admin-alias-config-max-aliases-per-user" className="block text-sm font-medium text-sky-100 mb-2">Max Aliases Per User</label>
          <input id="admin-alias-config-max-aliases-per-user" type="number" value={config.maxAliasesPerUser} onChange={(e) => setConfig({ ...config, maxAliasesPerUser: parseInt(e.target.value) || 1 })} min={1} max={50}
            className="w-32 px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 focus:border-gold-500" />
          <p className="text-xs text-navy-400 mt-1">Maximum aliases a user can create (1-50)</p>
        </div>

        <div className="flex items-center justify-between">
          <div><label className="text-sm font-medium text-sky-100">Allow Custom Aliases</label><p className="text-xs text-navy-400">Let users create their own alias names</p></div>
          <button onClick={() => setConfig({ ...config, allowCustomAliases: !config.allowCustomAliases })} className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${config.allowCustomAliases ? 'bg-gold-500' : 'bg-navy-600'}`}>
            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${config.allowCustomAliases ? 'translate-x-6' : 'translate-x-1'}`} />
          </button>
        </div>
      </div>

      <div className="flex justify-end">
        <button onClick={handleSave} disabled={saving} className="px-6 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg disabled:opacity-50">
          {saving ? 'Saving...' : 'Save Configuration'}
        </button>
      </div>
    </div>
  );
}

export default AdminAliasConfig;
