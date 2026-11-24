import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { UpgradeNotice, isLicensedFeatureError } from '../../components/ui/UpgradeNotice';
import { MusicalNoteIcon, AdjustmentsHorizontalIcon, ExclamationTriangleIcon, TrashIcon, PlusIcon } from '@heroicons/react/24/outline';

/** Defaults shown until the community's saved settings load (mirrors hub-api's DTO fields). */
const DEFAULTS = {
  isActive: true,
  autoplayEnabled: false,
  requireDjApproval: false,
  volumeLimit: 100,
  defaultProvider: null,
  allowedGenres: [],
  blockedArtists: [],
};

/**
 * Music Station settings editor. Reads/writes `GET|PUT /api/v1/admin/{id}/music/settings`
 * (the real hub-api contract) -- the old page PUT `/modules/music/config`, a path that never
 * existed (module config is keyed by numeric module id), so every load/save 404'd.
 */
function AdminMusicSettings() {
  const { communityId } = useParams();
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [newArtist, setNewArtist] = useState('');
  const [gated, setGated] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const response = await adminApi.getMusicSettings(communityId);
        if (!cancelled) setConfig({ ...DEFAULTS, ...(response.data?.settings || {}) });
      } catch (err) {
        console.error('[AdminMusicSettings] Load failed', { status: err.response?.status });
        if (!cancelled && isLicensedFeatureError(err)) {
          // 402 = licensed feature not available on this tier: an upsell, not an error.
          setGated(true);
        } else if (!cancelled) {
          setConfig({ ...DEFAULTS });
          setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to load music settings' });
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [communityId]);

  const update = (key, value) => setConfig((c) => ({ ...c, [key]: value }));

  async function handleSave() {
    setSaving(true);
    setMessage(null);
    try {
      const { isActive, autoplayEnabled, requireDjApproval, volumeLimit, allowedGenres, blockedArtists } = config;
      await adminApi.updateMusicSettings(communityId, {
        isActive, autoplayEnabled, requireDjApproval, volumeLimit, allowedGenres, blockedArtists,
      });
      setMessage({ type: 'success', text: 'Music settings saved successfully' });
    } catch (err) {
      console.error('[AdminMusicSettings] Save failed', { status: err.response?.status });
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to save music settings' });
    } finally {
      setSaving(false);
    }
  }

  function addArtist() {
    const name = newArtist.trim();
    if (!name) {
      setMessage({ type: 'error', text: 'Please enter an artist name' });
      return;
    }
    if (!config.blockedArtists.includes(name)) update('blockedArtists', [...config.blockedArtists, name]);
    setNewArtist('');
  }

  if (gated) {
    return <UpgradeNotice feature="Music Station" />;
  }

  if (loading) {
    return <div className="flex justify-center py-12"><div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div></div>;
  }

  const toggle = (key, label, hint) => (
    <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
      <div>
        <div className="font-medium text-sky-100">{label}</div>
        <div className="text-sm text-navy-400">{hint}</div>
      </div>
      <input type="checkbox" aria-label={label} checked={!!config[key]} onChange={(e) => update(key, e.target.checked)} className="w-5 h-5 rounded border-navy-600 text-sky-500" />
    </label>
  );

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Music Settings</h1>
          <p className="text-navy-400 mt-1">Configure the community Music Station</p>
        </div>
        <button onClick={handleSave} disabled={saving} className="btn btn-primary disabled:opacity-50">
          {saving ? 'Saving...' : 'Save Settings'}
        </button>
      </div>

      {message && (
        <div className={`mb-6 p-4 rounded-lg border ${message.type === 'success' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' : 'bg-red-500/20 text-red-300 border-red-500/30'}`}>
          {message.text}
          <button onClick={() => setMessage(null)} aria-label="Dismiss message" className="float-right">&times;</button>
        </div>
      )}

      <div className="space-y-6">
        <div className="card p-6 space-y-4">
          <h2 className="text-lg font-semibold text-sky-100 flex items-center gap-2"><MusicalNoteIcon className="w-5 h-5" />Player</h2>
          {toggle('isActive', 'Enable Music Station', 'Allow the music player in this community')}
          {toggle('autoplayEnabled', 'Autoplay', 'Keep playing when the queue empties')}
          {toggle('requireDjApproval', 'Require DJ approval', 'Song requests wait for a DJ to approve them')}
        </div>

        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2"><AdjustmentsHorizontalIcon className="w-5 h-5" />Volume</h2>
          <label htmlFor="admin-music-volume-limit" className="block text-sm font-medium text-navy-300 mb-4">
            Volume limit: <span className="text-sky-300 font-bold">{config.volumeLimit}%</span>
          </label>
          <input id="admin-music-volume-limit" type="range" min="0" max="100" step="5" value={config.volumeLimit} onChange={(e) => update('volumeLimit', parseInt(e.target.value, 10))} className="w-full h-2 bg-navy-700 rounded-lg cursor-pointer" />
        </div>

        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4 flex items-center gap-2"><ExclamationTriangleIcon className="w-5 h-5" />Blocked artists</h2>
          <div className="flex gap-3 mb-4">
            <input aria-label="Blocked artist" type="text" value={newArtist} onChange={(e) => setNewArtist(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') addArtist(); }} placeholder="Enter artist name..." className="flex-1 px-4 py-2 bg-navy-700 border border-navy-600 rounded-lg text-sky-100" />
            <button onClick={addArtist} className="btn btn-secondary flex items-center gap-2"><PlusIcon className="w-4 h-4" />Add</button>
          </div>
          {config.blockedArtists.length === 0 ? (
            <p className="text-navy-400 text-center py-4">No blocked artists</p>
          ) : (
            <div className="space-y-2">
              {config.blockedArtists.map((artist) => (
                <div key={artist} className="flex items-center justify-between p-3 bg-navy-800 rounded-lg border border-navy-700">
                  <span className="text-sky-100">{artist}</span>
                  <button aria-label={`Remove ${artist} from blocked artists`} onClick={() => update('blockedArtists', config.blockedArtists.filter((a) => a !== artist))} className="btn btn-danger p-2"><TrashIcon className="w-4 h-4" /></button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default AdminMusicSettings;
