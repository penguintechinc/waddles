import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeftIcon, CheckIcon, ExclamationTriangleIcon, PlusIcon, TrashIcon } from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

function AdminServerStatusConfig() {
  const { communityId } = useParams();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [moduleId, setModuleId] = useState(null);

  const [config, setConfig] = useState({
    enabled: true,
    pollingIntervalSeconds: 60,
    notifyOnDown: true,
    servers: [],
  });

  useEffect(() => { loadConfig(); }, [communityId]);

  const loadConfig = async () => {
    try {
      setLoading(true);
      const response = await adminApi.getModules(communityId);
      const mod = (response.data.modules || []).find((m) => m.name?.toLowerCase() === 'server-status');
      if (mod) { setModuleId(mod.moduleId); if (mod.config) setConfig({ ...config, ...mod.config }); }
    } catch { setMessage({ type: 'error', text: 'Failed to load configuration' }); }
    finally { setLoading(false); }
  };

  const handleSave = async () => {
    if (!moduleId) return;
    try {
      setSaving(true);
      await adminApi.updateModuleConfig(communityId, moduleId, { config });
      setMessage({ type: 'success', text: 'Server Status configuration saved successfully' });
    } catch (err) { setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to save' }); }
    finally { setSaving(false); }
  };

  const addServer = () => {
    setConfig({ ...config, servers: [...config.servers, { name: '', host: '', port: '', game: '' }] });
  };

  const removeServer = (index) => {
    setConfig({ ...config, servers: config.servers.filter((_, i) => i !== index) });
  };

  const updateServer = (index, field, value) => {
    const updated = [...config.servers];
    updated[index] = { ...updated[index], [field]: value };
    setConfig({ ...config, servers: updated });
  };

  if (loading) return <div className="flex items-center justify-center h-64"><div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div></div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center space-x-4">
        <Link to={`/admin/${communityId}/modules`} className="p-2 text-navy-400 hover:text-sky-300 rounded-lg hover:bg-navy-800"><ArrowLeftIcon className="w-5 h-5" /></Link>
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Server Status Configuration</h1>
          <p className="text-navy-400 mt-1">Configure the Server Status module</p>
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
          <div><label className="text-sm font-medium text-sky-100">Enabled</label><p className="text-xs text-navy-400">Enable or disable Server Status monitoring</p></div>
          <button onClick={() => setConfig({ ...config, enabled: !config.enabled })} className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${config.enabled ? 'bg-gold-500' : 'bg-navy-600'}`}>
            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${config.enabled ? 'translate-x-6' : 'translate-x-1'}`} />
          </button>
        </div>

        <div>
          <label htmlFor="admin-server-status-config-polling-interval-seconds" className="block text-sm font-medium text-sky-100 mb-2">Polling Interval (seconds)</label>
          <input id="admin-server-status-config-polling-interval-seconds" type="number" value={config.pollingIntervalSeconds} onChange={(e) => setConfig({ ...config, pollingIntervalSeconds: parseInt(e.target.value) || 30 })} min={30} max={3600}
            className="w-32 px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 focus:border-gold-500" />
          <p className="text-xs text-navy-400 mt-1">How often to check server status (30-3600s)</p>
        </div>

        <div className="flex items-center justify-between">
          <div><label className="text-sm font-medium text-sky-100">Notify on Down</label><p className="text-xs text-navy-400">Send notifications when a server goes offline</p></div>
          <button onClick={() => setConfig({ ...config, notifyOnDown: !config.notifyOnDown })} className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${config.notifyOnDown ? 'bg-gold-500' : 'bg-navy-600'}`}>
            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${config.notifyOnDown ? 'translate-x-6' : 'translate-x-1'}`} />
          </button>
        </div>

        <div>
          <div className="flex items-center justify-between mb-3">
            <label className="text-sm font-medium text-sky-100">Monitored Servers</label>
            <button onClick={addServer} className="flex items-center space-x-1 text-sm text-gold-400 hover:text-gold-300">
              <PlusIcon className="w-4 h-4" /><span>Add Server</span>
            </button>
          </div>
          {config.servers.length === 0 ? (
            <p className="text-navy-400 text-sm">No servers configured. Add a server to start monitoring.</p>
          ) : (
            <div className="space-y-3">
              {config.servers.map((server, index) => (
                <div key={index} className="flex items-center gap-2 bg-navy-900 p-3 rounded-lg">
                  <input aria-label="Name" type="text" value={server.name} onChange={(e) => updateServer(index, 'name', e.target.value)} placeholder="Name"
                    className="flex-1 px-2 py-1 bg-navy-800 border border-navy-700 rounded text-sky-100 text-sm focus:border-gold-500" />
                  <input aria-label="Host" type="text" value={server.host} onChange={(e) => updateServer(index, 'host', e.target.value)} placeholder="Host"
                    className="flex-1 px-2 py-1 bg-navy-800 border border-navy-700 rounded text-sky-100 text-sm focus:border-gold-500" />
                  <input aria-label="Port" type="text" value={server.port} onChange={(e) => updateServer(index, 'port', e.target.value)} placeholder="Port"
                    className="w-20 px-2 py-1 bg-navy-800 border border-navy-700 rounded text-sky-100 text-sm focus:border-gold-500" />
                  <input aria-label="Game" type="text" value={server.game} onChange={(e) => updateServer(index, 'game', e.target.value)} placeholder="Game"
                    className="w-28 px-2 py-1 bg-navy-800 border border-navy-700 rounded text-sky-100 text-sm focus:border-gold-500" />
                  <button aria-label={`Delete server ${index + 1}`} onClick={() => removeServer(index)} className="p-1 text-red-400 hover:text-red-300"><TrashIcon className="w-4 h-4" /></button>
                </div>
              ))}
            </div>
          )}
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

export default AdminServerStatusConfig;
