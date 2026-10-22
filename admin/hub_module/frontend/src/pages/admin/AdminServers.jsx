import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { getPlatformIcon, getPlatformColor } from '../../utils/platformConfig';
import { XMarkIcon, PlusIcon } from '@heroicons/react/24/outline';

const PLATFORM_ICONS = new Proxy({}, { get: (_, key) => getPlatformIcon(key) });
const PLATFORM_COLORS = new Proxy({}, { get: (_, key) => getPlatformColor(key) });

function AdminServers() {
  const { communityId } = useParams();
  const [servers, setServers] = useState([]);
  const [requests, setRequests] = useState([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState('servers');
  const [actionLoading, setActionLoading] = useState(null);
  const [message, setMessage] = useState(null);

  // Request Link modal state
  const [showRequestModal, setShowRequestModal] = useState(false);
  const [requestForm, setRequestForm] = useState({
    platform: 'discord',
    platformServerId: '',
    platformServerName: '',
    linkType: 'standard',
  });
  const [requestSubmitting, setRequestSubmitting] = useState(false);
  const [requestError, setRequestError] = useState(null);

  useEffect(() => {
    fetchData();
  }, [communityId]);

  async function fetchData() {
    setLoading(true);
    try {
      const [serversRes, requestsRes] = await Promise.all([
        adminApi.getServers(communityId),
        adminApi.getServerLinkRequests(communityId, { status: 'pending' }),
      ]);
      setServers(serversRes.data.servers || []);
      setRequests(requestsRes.data.requests || []);
    } catch (err) {
      console.error('Failed to fetch servers:', err);
      setMessage({ type: 'error', text: 'Failed to load servers' });
    } finally {
      setLoading(false);
    }
  }

  async function handleApproveRequest(requestId) {
    setActionLoading(requestId);
    try {
      await adminApi.approveServerLinkRequest(communityId, requestId);
      setMessage({ type: 'success', text: 'Server link approved' });
      fetchData();
    } catch (err) {
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to approve request' });
    } finally {
      setActionLoading(null);
    }
  }

  async function handleRejectRequest(requestId) {
    setActionLoading(requestId);
    try {
      await adminApi.rejectServerLinkRequest(communityId, requestId);
      setMessage({ type: 'success', text: 'Server link rejected' });
      fetchData();
    } catch (err) {
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to reject request' });
    } finally {
      setActionLoading(null);
    }
  }

  async function handleRemoveServer(serverId) {
    if (!confirm('Are you sure you want to remove this server?')) return;
    setActionLoading(serverId);
    try {
      await adminApi.removeServer(communityId, serverId);
      setMessage({ type: 'success', text: 'Server removed' });
      fetchData();
    } catch (err) {
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to remove server' });
    } finally {
      setActionLoading(null);
    }
  }

  async function handleSetPrimary(serverId) {
    setActionLoading(serverId);
    try {
      await adminApi.updateServer(communityId, serverId, { isPrimary: true });
      setMessage({ type: 'success', text: 'Primary server updated' });
      fetchData();
    } catch (err) {
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to update server' });
    } finally {
      setActionLoading(null);
    }
  }

  async function handleRequestLink() {
    if (!requestForm.platformServerId.trim()) {
      setRequestError('Server / Channel ID is required');
      return;
    }
    setRequestSubmitting(true);
    setRequestError(null);
    try {
      await adminApi.createServerLinkRequest(communityId, {
        platform: requestForm.platform,
        platformServerId: requestForm.platformServerId.trim(),
        platformServerName: requestForm.platformServerName.trim() || undefined,
        linkType: requestForm.linkType,
      });
      setMessage({ type: 'success', text: 'Server link request submitted' });
      setShowRequestModal(false);
      setRequestForm({ platform: 'discord', platformServerId: '', platformServerName: '', linkType: 'standard' });
      fetchData();
    } catch (err) {
      setRequestError(err.response?.data?.error?.message || 'Failed to submit request');
    } finally {
      setRequestSubmitting(false);
    }
  }

  const pendingCount = requests.length;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-sky-100">Linked Servers</h1>
        <button
          onClick={() => { setShowRequestModal(true); setRequestError(null); }}
          className="inline-flex items-center space-x-2 px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg transition-colors text-sm"
        >
          <PlusIcon className="w-4 h-4" />
          <span>Request Link</span>
        </button>
      </div>

      {message && (
        <div className={`mb-4 p-4 rounded-lg border ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border-red-500/30'
        }`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">×</button>
        </div>
      )}

      {/* Tabs */}
      <div className="flex space-x-1 mb-6 bg-navy-800 p-1 rounded-lg w-fit">
        <button
          onClick={() => setActiveTab('servers')}
          className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
            activeTab === 'servers'
              ? 'bg-navy-700 text-sky-100'
              : 'text-navy-400 hover:text-sky-100'
          }`}
        >
          Linked Servers ({servers.length})
        </button>
        <button
          onClick={() => setActiveTab('requests')}
          className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
            activeTab === 'requests'
              ? 'bg-navy-700 text-sky-100'
              : 'text-navy-400 hover:text-sky-100'
          }`}
        >
          Pending Requests
          {pendingCount > 0 && (
            <span className="ml-2 px-2 py-0.5 text-xs bg-gold-400 text-navy-900 rounded-full">
              {pendingCount}
            </span>
          )}
        </button>
      </div>

      {loading ? (
        <div className="flex justify-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
        </div>
      ) : activeTab === 'servers' ? (
        /* Linked Servers Tab */
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {servers.length === 0 ? (
            <div className="col-span-full card p-12 text-center">
              <div className="text-4xl mb-4">🔗</div>
              <h3 className="text-lg font-medium text-sky-100 mb-2">No Linked Servers</h3>
              <p className="text-navy-400">
                Platform admins can link their servers to this community.
              </p>
            </div>
          ) : (
            servers.map((server) => (
              <div key={server.id} className="card p-4">
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center space-x-3">
                    <span className="text-2xl">{PLATFORM_ICONS[server.platform] || '🌐'}</span>
                    <div>
                      <h3 className="font-medium text-sky-100">{server.platformServerName}</h3>
                      <span className={`text-xs px-2 py-0.5 rounded border ${PLATFORM_COLORS[server.platform] || 'bg-navy-700'}`}>
                        {server.platform}
                      </span>
                    </div>
                  </div>
                  {server.isPrimary && (
                    <span className="badge badge-gold">Primary</span>
                  )}
                </div>

                <div className="text-xs text-navy-500 mb-3">
                  <div>ID: {server.platformServerId}</div>
                  <div>Added by: {server.addedBy || 'Unknown'}</div>
                  <div>Linked: {new Date(server.createdAt).toLocaleDateString()}</div>
                </div>

                <div className="flex space-x-2">
                  {!server.isPrimary && (
                    <button
                      onClick={() => handleSetPrimary(server.id)}
                      disabled={actionLoading === server.id}
                      className="btn btn-secondary text-xs flex-1 disabled:opacity-50"
                    >
                      Set Primary
                    </button>
                  )}
                  <button
                    onClick={() => handleRemoveServer(server.id)}
                    disabled={actionLoading === server.id}
                    className="btn bg-red-500/20 text-red-300 hover:bg-red-500/30 text-xs disabled:opacity-50"
                  >
                    Remove
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      ) : (
        /* Pending Requests Tab */
        <div className="card overflow-hidden">
          {requests.length === 0 ? (
            <div className="p-12 text-center">
              <div className="text-4xl mb-4">✅</div>
              <h3 className="text-lg font-medium text-sky-100 mb-2">No Pending Requests</h3>
              <p className="text-navy-400">
                All server link requests have been processed.
              </p>
            </div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Server</th>
                  <th>Platform</th>
                  <th>Requested By</th>
                  <th>Date</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {requests.map((request) => (
                  <tr key={request.id}>
                    <td>
                      <div className="font-medium text-sky-100">{request.platformServerName}</div>
                      <div className="text-xs text-navy-500">{request.platformServerId}</div>
                    </td>
                    <td>
                      <span className={`text-xs px-2 py-1 rounded border ${PLATFORM_COLORS[request.platform] || 'bg-navy-700'}`}>
                        {PLATFORM_ICONS[request.platform]} {request.platform}
                      </span>
                    </td>
                    <td>
                      <div className="text-sky-100">{request.requestedBy}</div>
                      <div className="text-xs text-navy-500">{request.requestedByEmail}</div>
                      {request.initiatedBy && (
                        <span className={`mt-1 inline-block text-xs px-2 py-0.5 rounded-full border ${
                          request.initiatedBy === 'community'
                            ? 'bg-blue-500/20 text-blue-300 border-blue-500/30'
                            : 'bg-purple-500/20 text-purple-300 border-purple-500/30'
                        }`}>
                          {request.initiatedBy === 'community' ? 'Community Initiated' : 'Server Initiated'}
                        </span>
                      )}
                    </td>
                    <td className="text-sm text-navy-400">
                      {new Date(request.createdAt).toLocaleDateString()}
                    </td>
                    <td>
                      <div className="flex space-x-2">
                        <button
                          onClick={() => handleApproveRequest(request.id)}
                          disabled={actionLoading === request.id}
                          className="btn btn-primary text-xs disabled:opacity-50"
                        >
                          Approve
                        </button>
                        <button
                          onClick={() => handleRejectRequest(request.id)}
                          disabled={actionLoading === request.id}
                          className="btn bg-red-500/20 text-red-300 hover:bg-red-500/30 text-xs disabled:opacity-50"
                        >
                          Reject
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      {/* Request Link Modal */}
      {showRequestModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-navy-800 border border-navy-700 rounded-lg p-6 max-w-md w-full mx-4">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-sky-100">Request Server Link</h3>
              <button
                aria-label="Close"
                onClick={() => setShowRequestModal(false)}
                className="text-navy-400 hover:text-sky-100"
              >
                <XMarkIcon className="w-6 h-6" />
              </button>
            </div>

            {requestError && (
              <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 mb-4 text-red-400 text-sm">
                {requestError}
              </div>
            )}

            <div className="space-y-4">
              <div>
                <label htmlFor="admin-servers-platform" className="block text-sm font-medium text-navy-300 mb-1">Platform</label>
                <select
                  id="admin-servers-platform"
                  value={requestForm.platform}
                  onChange={(e) => setRequestForm({ ...requestForm, platform: e.target.value })}
                  className="w-full px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 text-sm focus:border-gold-500"
                >
                  <option value="discord">Discord</option>
                  <option value="slack">Slack</option>
                  <option value="twitch">Twitch</option>
                  <option value="kick">KICK</option>
                  <option value="youtube">YouTube</option>
                </select>
              </div>

              <div>
                <label htmlFor="admin-servers-input" className="block text-sm font-medium text-navy-300 mb-1">
                  Server / Channel ID <span className="text-red-400">*</span>
                </label>
                <input
                  id="admin-servers-input"
                  type="text"
                  value={requestForm.platformServerId}
                  onChange={(e) => setRequestForm({ ...requestForm, platformServerId: e.target.value })}
                  placeholder="e.g. 123456789012345678"
                  className="w-full px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 placeholder-navy-500 text-sm focus:border-gold-500"
                />
              </div>

              <div>
                <label htmlFor="admin-servers-server-channel-name-optional" className="block text-sm font-medium text-navy-300 mb-1">
                  Server / Channel Name <span className="text-navy-500">(optional)</span>
                </label>
                <input
                  id="admin-servers-server-channel-name-optional"
                  type="text"
                  value={requestForm.platformServerName}
                  onChange={(e) => setRequestForm({ ...requestForm, platformServerName: e.target.value })}
                  placeholder="My Discord Server"
                  className="w-full px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 placeholder-navy-500 text-sm focus:border-gold-500"
                />
              </div>

              <div>
                <label htmlFor="admin-servers-link-type" className="block text-sm font-medium text-navy-300 mb-1">Link Type</label>
                <select
                  id="admin-servers-link-type"
                  value={requestForm.linkType}
                  onChange={(e) => setRequestForm({ ...requestForm, linkType: e.target.value })}
                  className="w-full px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 text-sm focus:border-gold-500"
                >
                  <option value="standard">Standard</option>
                  <option value="read_only">Read Only</option>
                  <option value="announcement_only">Announcement Only</option>
                </select>
              </div>
            </div>

            <div className="flex justify-end space-x-3 mt-6">
              <button
                onClick={() => setShowRequestModal(false)}
                className="px-4 py-2 text-navy-300 hover:text-sky-100"
              >
                Cancel
              </button>
              <button
                onClick={handleRequestLink}
                disabled={requestSubmitting}
                className="px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg disabled:opacity-50 transition-colors"
              >
                {requestSubmitting ? 'Submitting...' : 'Submit Request'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminServers;
