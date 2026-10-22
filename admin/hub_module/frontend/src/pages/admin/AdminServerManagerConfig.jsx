import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeftIcon, CheckIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

function AdminServerManagerConfig() {
  const { communityId } = useParams();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [moduleId, setModuleId] = useState(null);

  const [config, setConfig] = useState({
    enabled: true,
    adminOnly: true,
    allowedCommands: '',
    rconTimeout: 10,
  });

  useEffect(() => { loadConfig(); }, [communityId]);

  const loadConfig = async () => {
    try {
      setLoading(true);
      const response = await adminApi.getModules(communityId);
      const mod = (response.data.modules || []).find((m) => m.name?.toLowerCase() === 'server-manager');
      if (mod) {
        setModuleId(mod.moduleId);
        if (mod.config) {
          setConfig({
            ...config,
            ...mod.config,
            allowedCommands: Array.isArray(mod.config.allowedCommands) ? mod.config.allowedCommands.join(', ') : (mod.config.allowedCommands || ''),
          });
        }
      }
    } catch { setMessage({ type: 'error', text: 'Failed to load configuration' }); }
    finally { setLoading(false); }
  };

  const handleSave = async () => {
    if (!moduleId) return;
    const saveConfig = {
      ...config,
      allowedCommands: config.allowedCommands.split(',').map((c) => c.trim()).filter(Boolean),
    };
    try {
      setSaving(true);
      await adminApi.updateModuleConfig(communityId, moduleId, { config: saveConfig });
      setMessage({ type: 'success', text: 'Server Manager configuration saved successfully' });
    } catch (err) { setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to save' }); }
    finally { setSaving(false); }
  };

  if (loading) return <div className="flex items-center justify-center h-64"><div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div></div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center space-x-4">
        <Link to={`/admin/${communityId}/modules`} className="p-2 text-navy-400 hover:text-sky-300 rounded-lg hover:bg-navy-800"><ArrowLeftIcon className="w-5 h-5" /></Link>
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Server Manager Configuration</h1>
          <p className="text-navy-400 mt-1">Configure the Server Manager module</p>
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
          <div><label className="text-sm font-medium text-sky-100">Enabled</label><p className="text-xs text-navy-400">Enable or disable the Server Manager module</p></div>
          <button onClick={() => setConfig({ ...config, enabled: !config.enabled })} className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${config.enabled ? 'bg-gold-500' : 'bg-navy-600'}`}>
            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${config.enabled ? 'translate-x-6' : 'translate-x-1'}`} />
          </button>
        </div>

        <div className="flex items-center justify-between">
          <div><label className="text-sm font-medium text-sky-100">Admin Only</label><p className="text-xs text-navy-400">Restrict server management to admins only</p></div>
          <button onClick={() => setConfig({ ...config, adminOnly: !config.adminOnly })} className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${config.adminOnly ? 'bg-gold-500' : 'bg-navy-600'}`}>
            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${config.adminOnly ? 'translate-x-6' : 'translate-x-1'}`} />
          </button>
        </div>

        <div>
          <label htmlFor="admin-server-manager-config-allowed-commands" className="block text-sm font-medium text-sky-100 mb-2">Allowed Commands</label>
          <input id="admin-server-manager-config-allowed-commands" type="text" value={config.allowedCommands} onChange={(e) => setConfig({ ...config, allowedCommands: e.target.value })}
            placeholder="status, restart, stop, backup"
            className="w-full px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 focus:border-gold-500" />
          <p className="text-xs text-navy-400 mt-1">Comma-separated list of allowed RCON commands</p>
        </div>

        <div>
          <label htmlFor="admin-server-manager-config-rcon-timeout-seconds" className="block text-sm font-medium text-sky-100 mb-2">RCON Timeout (seconds)</label>
          <input id="admin-server-manager-config-rcon-timeout-seconds" type="number" value={config.rconTimeout} onChange={(e) => setConfig({ ...config, rconTimeout: parseInt(e.target.value) || 5 })} min={5} max={60}
            className="w-32 px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 focus:border-gold-500" />
          <p className="text-xs text-navy-400 mt-1">Timeout for RCON connections (5-60 seconds)</p>
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

export default AdminServerManagerConfig;
