import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  ServerStackIcon,
  CheckCircleIcon,
  XCircleIcon,
  ArrowRightIcon,
  KeyIcon,
  PlusIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';
import { getPlatformConfig } from '../../utils/platformConfig';

function AdminConnectedPlatforms() {
  const { communityId } = useParams();
  const [platforms, setPlatforms] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // OAuth credentials state
  const [oauthCredentials, setOauthCredentials] = useState([]);
  const [oauthLoading, setOauthLoading] = useState(true);
  const [oauthError, setOauthError] = useState(null);
  const [oauthSuccess, setOauthSuccess] = useState(null);
  const [showOAuthForm, setShowOAuthForm] = useState(false);
  const [oauthForm, setOauthForm] = useState({ platform: '', clientId: '', clientSecret: '', scopes: '' });

  useEffect(() => {
    loadPlatforms();
    loadOAuthCredentials();
  }, [communityId]);

  const loadPlatforms = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await adminApi.getConnectedPlatforms(communityId);
      setPlatforms(response.data.connectedPlatforms || []);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load connected platforms');
    } finally {
      setLoading(false);
    }
  };

  const loadOAuthCredentials = async () => {
    try {
      setOauthLoading(true);
      setOauthError(null);
      const response = await adminApi.getCommunityOAuthCredentials(communityId);
      setOauthCredentials(response.data?.data || []);
    } catch (err) {
      setOauthError(err.response?.data?.error || 'Failed to load OAuth credentials');
    } finally {
      setOauthLoading(false);
    }
  };

  const handleOAuthCreate = async (e) => {
    e.preventDefault();
    try {
      setOauthError(null);
      await adminApi.createCommunityOAuthCredential(communityId, {
        platform: oauthForm.platform,
        clientId: oauthForm.clientId,
        clientSecret: oauthForm.clientSecret,
        scopes: oauthForm.scopes ? oauthForm.scopes.split(',').map(s => s.trim()) : [],
      });
      setOauthSuccess('OAuth credential created');
      setShowOAuthForm(false);
      setOauthForm({ platform: '', clientId: '', clientSecret: '', scopes: '' });
      loadOAuthCredentials();
      setTimeout(() => setOauthSuccess(null), 3000);
    } catch (err) {
      setOauthError(err.response?.data?.error || 'Failed to create credential');
    }
  };

  const handleOAuthDelete = async (id) => {
    if (!window.confirm('Delete this OAuth credential?')) return;
    try {
      setOauthError(null);
      await adminApi.deleteCommunityOAuthCredential(communityId, id);
      setOauthSuccess('Credential deleted');
      loadOAuthCredentials();
      setTimeout(() => setOauthSuccess(null), 3000);
    } catch (err) {
      setOauthError(err.response?.data?.error || 'Failed to delete credential');
    }
  };

  const getPlatformInfo = (platformId) => {
    const cfg = getPlatformConfig(platformId);
    return { name: cfg.label, icon: cfg.icon, color: cfg.color };
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
      <div>
        <h1 className="text-2xl font-bold text-sky-100">Connected Platforms</h1>
        <p className="text-navy-400 mt-1">
          Overview of all platforms connected to your community
        </p>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4">
          <span className="text-red-400">{error}</span>
        </div>
      )}

      {/* Platform Grid */}
      {platforms.length === 0 ? (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
          <ServerStackIcon className="w-12 h-12 text-navy-500 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-sky-100 mb-2">No Platforms Connected</h3>
          <p className="text-navy-400 mb-4">
            Link your first server to get started.
          </p>
          <Link
            to={`/admin/${communityId}/servers`}
            className="inline-flex items-center space-x-2 px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg transition-colors"
          >
            <span>Go to Linked Servers</span>
            <ArrowRightIcon className="w-4 h-4" />
          </Link>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {platforms.map((platform) => {
              const info = getPlatformInfo(platform.platform);
              return (
                <div
                  key={platform.platform}
                  className="bg-navy-800 border border-navy-700 rounded-lg p-6 hover:border-navy-600 transition-colors"
                >
                  <div className="flex items-start justify-between mb-4">
                    <div className="flex items-center space-x-3">
                      <span className="text-3xl">{info.icon}</span>
                      <div>
                        <h3 className="font-semibold text-sky-100">{info.name}</h3>
                        <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium border ${info.color}`}>
                          {platform.platform}
                        </span>
                      </div>
                    </div>
                    {platform.isActive ? (
                      <CheckCircleIcon className="w-6 h-6 text-green-400" />
                    ) : (
                      <XCircleIcon className="w-6 h-6 text-red-400" />
                    )}
                  </div>

                  <div className="space-y-3">
                    <div className="flex justify-between items-center">
                      <span className="text-navy-400 text-sm">Linked Servers</span>
                      <span className="text-gold-400 font-semibold text-lg">{platform.serverCount}</span>
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="text-navy-400 text-sm">Status</span>
                      <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                        platform.isActive
                          ? 'bg-green-500/20 text-green-400 border border-green-500/30'
                          : 'bg-red-500/20 text-red-400 border border-red-500/30'
                      }`}>
                        {platform.isActive ? 'Active' : 'Inactive'}
                      </span>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Summary Card */}
          <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-lg font-semibold text-sky-100">Platform Summary</h3>
                <p className="text-navy-400 text-sm mt-1">
                  {platforms.length} platform{platforms.length !== 1 ? 's' : ''} connected with{' '}
                  {platforms.reduce((sum, p) => sum + p.serverCount, 0)} total server{platforms.reduce((sum, p) => sum + p.serverCount, 0) !== 1 ? 's' : ''}
                </p>
              </div>
              <Link
                to={`/admin/${communityId}/servers`}
                className="inline-flex items-center space-x-2 px-4 py-2 bg-navy-700 hover:bg-navy-600 text-sky-100 rounded-lg transition-colors"
              >
                <span>Manage Servers</span>
                <ArrowRightIcon className="w-4 h-4" />
              </Link>
            </div>
          </div>
        </>
      )}
      {/* OAuth Credentials */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <KeyIcon className="w-6 h-6 text-gold-400" />
            <div>
              <h2 className="text-xl font-semibold text-sky-100">OAuth Credentials</h2>
              <p className="text-navy-400 text-sm mt-0.5">Community-level OAuth client credentials for platform integrations</p>
            </div>
          </div>
          <button
            onClick={() => setShowOAuthForm(v => !v)}
            className="inline-flex items-center gap-2 px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg transition-colors text-sm"
          >
            <PlusIcon className="w-4 h-4" />
            Add Credential
          </button>
        </div>

        {oauthError && (
          <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-400 text-sm">{oauthError}</div>
        )}
        {oauthSuccess && (
          <div className="bg-emerald-500/10 border border-emerald-500/30 rounded-lg p-3 text-emerald-400 text-sm">{oauthSuccess}</div>
        )}

        {showOAuthForm && (
          <form onSubmit={handleOAuthCreate} className="bg-navy-800 border border-navy-700 rounded-lg p-6 space-y-4">
            <h3 className="font-semibold text-sky-100">New OAuth Credential</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label htmlFor="admin-connected-platforms-platform" className="block text-sm font-medium text-sky-200 mb-1">Platform</label>
                <input
                  id="admin-connected-platforms-platform"
                  className="input w-full"
                  placeholder="e.g. twitch, discord"
                  value={oauthForm.platform}
                  onChange={e => setOauthForm(f => ({ ...f, platform: e.target.value }))}
                  required
                />
              </div>
              <div>
                <label htmlFor="admin-connected-platforms-scopes-comma-separated" className="block text-sm font-medium text-sky-200 mb-1">Scopes (comma-separated)</label>
                <input
                  id="admin-connected-platforms-scopes-comma-separated"
                  className="input w-full"
                  placeholder="e.g. read:user, channel:manage"
                  value={oauthForm.scopes}
                  onChange={e => setOauthForm(f => ({ ...f, scopes: e.target.value }))}
                />
              </div>
              <div>
                <label htmlFor="admin-connected-platforms-client-id" className="block text-sm font-medium text-sky-200 mb-1">Client ID</label>
                <input
                  id="admin-connected-platforms-client-id"
                  className="input w-full"
                  placeholder="Client ID"
                  value={oauthForm.clientId}
                  onChange={e => setOauthForm(f => ({ ...f, clientId: e.target.value }))}
                  required
                />
              </div>
              <div>
                <label htmlFor="admin-connected-platforms-client-secret" className="block text-sm font-medium text-sky-200 mb-1">Client Secret</label>
                <input
                  id="admin-connected-platforms-client-secret"
                  type="password"
                  className="input w-full"
                  placeholder="Client Secret"
                  value={oauthForm.clientSecret}
                  onChange={e => setOauthForm(f => ({ ...f, clientSecret: e.target.value }))}
                />
              </div>
            </div>
            <div className="flex gap-2">
              <button type="submit" className="btn btn-primary text-sm">Save</button>
              <button type="button" onClick={() => setShowOAuthForm(false)} className="btn btn-secondary text-sm">Cancel</button>
            </div>
          </form>
        )}

        {oauthLoading ? (
          <div className="flex items-center justify-center py-8">
            <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-gold-400"></div>
          </div>
        ) : oauthCredentials.length === 0 ? (
          <div className="bg-navy-800 border border-navy-700 rounded-lg p-8 text-center">
            <KeyIcon className="w-10 h-10 text-navy-500 mx-auto mb-3" />
            <p className="text-navy-400 text-sm">No OAuth credentials configured for this community.</p>
          </div>
        ) : (
          <div className="space-y-2">
            {oauthCredentials.map(cred => (
              <div key={cred.id} className="flex items-center justify-between bg-navy-800 border border-navy-700 rounded-lg px-5 py-4">
                <div>
                  <div className="font-medium text-sky-100 capitalize">{cred.platform}</div>
                  <div className="text-xs text-navy-400 mt-0.5">
                    Client ID: {cred.clientId || '—'}
                    {cred.scopes?.length > 0 && ` · Scopes: ${cred.scopes.join(', ')}`}
                  </div>
                </div>
                <button
                  onClick={() => handleOAuthDelete(cred.id)}
                  className="p-2 text-navy-400 hover:text-red-400 transition-colors rounded"
                  title="Delete credential"
                >
                  <TrashIcon className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default AdminConnectedPlatforms;
