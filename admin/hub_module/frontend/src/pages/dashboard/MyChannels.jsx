import { useState, useEffect } from 'react';
import { useAuth } from '../../contexts/AuthContext';
import api, { communityApi, userApi } from '../../services/api';
import {
  LinkIcon,
  XMarkIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';

// Platform definitions (icons + styles) — mirrors AccountSettings
const PLATFORMS = {
  discord: {
    name: 'Discord',
    color: 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
        <path d="M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z" />
      </svg>
    ),
  },
  twitch: {
    name: 'Twitch',
    color: 'bg-purple-500/20 text-purple-300 border-purple-500/30',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
        <path d="M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143h-1.714zM6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714h13.714Z" />
      </svg>
    ),
  },
  slack: {
    name: 'Slack',
    color: 'bg-green-500/20 text-green-300 border-green-500/30',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
        <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z" />
      </svg>
    ),
  },
  youtube: {
    name: 'YouTube',
    color: 'bg-red-500/20 text-red-300 border-red-500/30',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
        <path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z" />
      </svg>
    ),
  },
  kick: {
    name: 'KICK',
    color: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30',
    icon: (
      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm-1-13h2v6h-2zm0 8h2v2h-2z" />
      </svg>
    ),
  },
};

const STATUS_BADGE = {
  pending: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
  approved: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30',
  rejected: 'bg-red-500/20 text-red-300 border-red-500/30',
};

function MyChannels() {
  const { user } = useAuth();
  const [linkedPlatforms, setLinkedPlatforms] = useState([]);
  const [myRequests, setMyRequests] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Join request modal
  const [joinModal, setJoinModal] = useState(null); // platform key
  const [communitySlug, setCommunitySlug] = useState('');
  const [submittingJoin, setSubmittingJoin] = useState(false);
  const [joinError, setJoinError] = useState(null);

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      setLoading(true);
      setError(null);
      const [platformsRes, requestsRes] = await Promise.all([
        userApi.getLinkedPlatforms(),
        communityApi.getMyServerLinkRequests(),
      ]);
      setLinkedPlatforms(platformsRes.data.platforms || []);
      setMyRequests(requestsRes.data.requests || []);
    } catch (err) {
      setError('Failed to load channel data');
    } finally {
      setLoading(false);
    }
  };

  const handleConnectPlatform = (platformKey) => {
    window.location.href = `/api/v1/auth/${platformKey}`;
  };

  const openJoinModal = (platformKey) => {
    setJoinModal(platformKey);
    setCommunitySlug('');
    setJoinError(null);
  };

  const closeJoinModal = () => {
    setJoinModal(null);
    setCommunitySlug('');
    setJoinError(null);
  };

  const handleJoinRequest = async () => {
    if (!communitySlug.trim()) {
      setJoinError('Please enter a community name or slug');
      return;
    }
    setSubmittingJoin(true);
    setJoinError(null);
    try {
      await api.post('/api/v1/communities/server-link-requests', {
        communitySlug: communitySlug.trim(),
        platform: joinModal,
        initiatedBy: 'platform',
      });
      setSuccess(`Join request sent for community "${communitySlug.trim()}"`);
      closeJoinModal();
      loadData();
    } catch (err) {
      setJoinError(err.response?.data?.error?.message || 'Failed to send join request');
    } finally {
      setSubmittingJoin(false);
    }
  };

  const handleCancelRequest = async (requestId) => {
    try {
      await communityApi.cancelServerLinkRequest(requestId);
      setSuccess('Request cancelled');
      loadData();
    } catch (err) {
      setError('Failed to cancel request');
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  const linkedPlatformKeys = new Set(linkedPlatforms.map((p) => p.platform?.toLowerCase()));

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-sky-100">My Channels</h1>
        <p className="text-navy-400 mt-1">
          Manage your connected platform accounts and community link requests
        </p>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <ExclamationTriangleIcon className="w-5 h-5 text-red-400" />
            <span className="text-red-400">{error}</span>
          </div>
          <button aria-label="Close" onClick={() => setError(null)} className="text-red-400 hover:text-red-300">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {success && (
        <div className="bg-emerald-500/20 border border-emerald-500/30 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <CheckCircleIcon className="w-5 h-5 text-emerald-400" />
            <span className="text-emerald-300">{success}</span>
          </div>
          <button aria-label="Close" onClick={() => setSuccess(null)} className="text-emerald-400 hover:text-emerald-300">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {/* Platform accounts */}
      <section>
        <h2 className="text-lg font-semibold text-sky-100 mb-4">Connected Platforms</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(PLATFORMS).map(([key, platform]) => {
            const isLinked = linkedPlatformKeys.has(key);
            const linkedData = linkedPlatforms.find((p) => p.platform?.toLowerCase() === key);
            return (
              <div
                key={key}
                className="bg-navy-800 border border-navy-700 rounded-lg p-4 space-y-3"
              >
                <div className="flex items-center space-x-3">
                  <div className={`w-10 h-10 rounded-lg flex items-center justify-center border ${platform.color}`}>
                    {platform.icon}
                  </div>
                  <div>
                    <p className="font-medium text-sky-100">{platform.name}</p>
                    {isLinked && linkedData?.username && (
                      <p className="text-xs text-navy-400">{linkedData.username}</p>
                    )}
                  </div>
                  {isLinked && (
                    <span className="ml-auto px-2 py-0.5 text-xs rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">
                      Connected
                    </span>
                  )}
                </div>

                {isLinked ? (
                  <button
                    onClick={() => openJoinModal(key)}
                    className="w-full px-3 py-2 bg-navy-700 hover:bg-navy-600 text-sky-100 rounded-lg text-sm transition-colors flex items-center justify-center space-x-2"
                  >
                    <LinkIcon className="w-4 h-4" />
                    <span>Request to Join a Community</span>
                  </button>
                ) : (
                  <button
                    onClick={() => handleConnectPlatform(key)}
                    className="w-full px-3 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg text-sm transition-colors"
                  >
                    Connect {platform.name}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </section>

      {/* Pending requests */}
      <section>
        <h2 className="text-lg font-semibold text-sky-100 mb-4">My Community Link Requests</h2>
        {myRequests.length === 0 ? (
          <div className="bg-navy-800 border border-navy-700 rounded-lg p-8 text-center">
            <LinkIcon className="w-10 h-10 text-navy-500 mx-auto mb-3" />
            <p className="text-navy-400">You have no pending community link requests.</p>
          </div>
        ) : (
          <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
            <table className="w-full">
              <thead className="bg-navy-900">
                <tr>
                  <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Community</th>
                  <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Platform</th>
                  <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Status</th>
                  <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Date</th>
                  <th className="text-right py-3 px-4 text-navy-400 font-medium text-sm">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-navy-700">
                {myRequests.map((req) => {
                  const platform = PLATFORMS[req.platform?.toLowerCase()];
                  return (
                    <tr key={req.id} className="hover:bg-navy-700/50">
                      <td className="py-3 px-4">
                        <p className="font-medium text-sky-100">{req.communityName || req.communitySlug}</p>
                        {req.platformServerName && (
                          <p className="text-xs text-navy-400">{req.platformServerName}</p>
                        )}
                      </td>
                      <td className="py-3 px-4">
                        {platform ? (
                          <span className={`px-2 py-0.5 text-xs font-medium rounded-full border ${platform.color}`}>
                            {platform.name}
                          </span>
                        ) : (
                          <span className="text-navy-400 text-sm">{req.platform}</span>
                        )}
                      </td>
                      <td className="py-3 px-4">
                        <span className={`px-2 py-0.5 text-xs font-medium rounded-full border ${STATUS_BADGE[req.status] || STATUS_BADGE.pending}`}>
                          {req.status ? req.status.charAt(0).toUpperCase() + req.status.slice(1) : 'Pending'}
                        </span>
                      </td>
                      <td className="py-3 px-4 text-sm text-navy-400">
                        {req.createdAt ? new Date(req.createdAt).toLocaleDateString() : '—'}
                      </td>
                      <td className="py-3 px-4 text-right">
                        {req.status === 'pending' && (
                          <button
                            onClick={() => handleCancelRequest(req.id)}
                            className="px-3 py-1.5 text-xs bg-red-500/20 text-red-300 border border-red-500/30 hover:bg-red-500/30 rounded-lg transition-colors"
                          >
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Join Request Modal */}
      {joinModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div role="dialog" aria-modal="true" aria-labelledby="my-channels-dialog-1-title" className="bg-navy-800 border border-navy-700 rounded-lg p-6 max-w-md w-full mx-4">
            <div className="flex items-center justify-between mb-4">
              <h3 id="my-channels-dialog-1-title" className="text-lg font-semibold text-sky-100">
                Request to Join a Community
              </h3>
              <button aria-label="Close" onClick={closeJoinModal} className="text-navy-400 hover:text-sky-100">
                <XMarkIcon className="w-6 h-6" />
              </button>
            </div>

            <p className="text-navy-400 text-sm mb-4">
              Your <span className="text-sky-300">{PLATFORMS[joinModal]?.name}</span> account will be
              used to request a link with the community. The community admin will review your request.
            </p>

            {joinError && (
              <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 mb-4 text-red-400 text-sm">
                {joinError}
              </div>
            )}

            <div className="space-y-4">
              <div>
                <label htmlFor="my-channels-community-name-or-slug" className="block text-sm font-medium text-navy-300 mb-1">
                  Community Name or Slug
                </label>
                <input
                  id="my-channels-community-name-or-slug"
                  type="text"
                  value={communitySlug}
                  onChange={(e) => setCommunitySlug(e.target.value)}
                  placeholder="e.g. my-awesome-community"
                  className="w-full px-3 py-2 bg-navy-900 border border-navy-700 rounded-lg text-sky-100 placeholder-navy-500 focus:border-gold-500 text-sm"
                  onKeyDown={(e) => e.key === 'Enter' && handleJoinRequest()}
                />
              </div>
            </div>

            <div className="flex justify-end space-x-3 mt-6">
              <button
                onClick={closeJoinModal}
                className="px-4 py-2 text-navy-300 hover:text-sky-100"
              >
                Cancel
              </button>
              <button
                onClick={handleJoinRequest}
                disabled={submittingJoin}
                className="px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium rounded-lg disabled:opacity-50 transition-colors"
              >
                {submittingJoin ? 'Sending...' : 'Send Request'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default MyChannels;
