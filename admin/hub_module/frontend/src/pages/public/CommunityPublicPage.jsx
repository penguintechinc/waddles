import { useState, useEffect } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { publicApi } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { Alert, NotAvailableYet } from '../../components/ui';
import { getErrorMessage } from '../../components/ui/errors';
import { useMyJoinRequest, useSubmitJoinRequest } from '../../hooks/useJoinRequests';
import {
  GlobeAltIcon,
  LockClosedIcon,
} from '@heroicons/react/24/outline';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

// Field definitions for Join Request modal
const joinRequestFields = [
  {
    name: 'message',
    type: 'textarea',
    label: 'Message (optional)',
    placeholder: 'Tell the admins why you want to join...',
    rows: 3,
    required: false,
  },
];

const SOCIAL_ICONS = {
  twitter: { name: 'Twitter/X', color: '#1DA1F2' },
  youtube: { name: 'YouTube', color: '#FF0000' },
  tiktok: { name: 'TikTok', color: '#000000' },
  instagram: { name: 'Instagram', color: '#E4405F' },
};

function CommunityPublicPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user, isAuthenticated } = useAuth();
  const [community, setCommunity] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showJoinModal, setShowJoinModal] = useState(false);
  const communityId = Number(id);
  // hub-api serves join REQUESTS for approval-mode communities only; there is no direct-join
  // route for open communities (see UNSERVED_FEATURES['community-open-join']).
  const requiresApproval = community?.joinMode === 'approval';
  const submitJoin = useSubmitJoinRequest(communityId);
  const myRequest = useMyJoinRequest(communityId, isAuthenticated && requiresApproval);
  const requestStatus = myRequest.data?.status;

  useEffect(() => {
    async function fetchCommunity() {
      try {
        const response = await publicApi.getCommunityProfile(id);
        setCommunity(response.data.community);
      } catch (err) {
        if (err.response?.status === 403) {
          setError('restricted');
        } else {
          setError(err.response?.data?.error || 'Community not found');
        }
      } finally {
        setLoading(false);
      }
    }
    fetchCommunity();
  }, [id]);

  const handleJoin = (data = {}) => {
    if (!isAuthenticated) {
      navigate('/login', { state: { returnTo: `/communities/${id}` } });
      return Promise.resolve();
    }
    // The failure is not swallowed: it is held in `submitJoin.error` and rendered as an Alert.
    // Resolving either way lets FormModalBuilder close the modal so the result is visible
    // (it keeps the modal open and only logs when its onSubmit rejects).
    return submitJoin.mutateAsync(data.message || '').then(
      () => undefined,
      () => undefined,
    );
  };

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  if (error === 'restricted') {
    return (
      <div className="max-w-xl mx-auto px-4 py-20 text-center">
        <LockClosedIcon className="w-16 h-16 mx-auto text-navy-500 mb-4" />
        <h1 className="text-2xl font-bold mb-2 text-sky-100">Community Restricted</h1>
        <p className="text-navy-400 mb-6">
          This community has restricted who can view their profile.
        </p>
        <Link to="/communities" className="btn btn-primary">
          Browse Communities
        </Link>
      </div>
    );
  }

  if (error) {
    return (
      <div className="max-w-xl mx-auto px-4 py-20 text-center">
        <div className="mb-4 flex justify-center">
          <img src="/waddlebot-logo.png" alt="Community Logo" className="w-24 h-24" />
        </div>
        <h1 className="text-2xl font-bold mb-2 text-sky-100">Community Not Found</h1>
        <p className="text-navy-400 mb-6">{error}</p>
        <Link to="/communities" className="btn btn-primary">
          Browse Communities
        </Link>
      </div>
    );
  }

  // Guard against null community (e.g. API returned success but empty body)
  if (!community) {
    return (
      <div className="max-w-xl mx-auto px-4 py-20 text-center">
        <div className="mb-4 flex justify-center">
          <img src="/waddlebot-logo.png" alt="Community Logo" className="w-24 h-24" />
        </div>
        <h1 className="text-2xl font-bold mb-2 text-sky-100">Community Not Found</h1>
        <p className="text-navy-400 mb-6">This community could not be loaded.</p>
        <Link to="/communities" className="btn btn-primary">Browse Communities</Link>
      </div>
    );
  }

  // Handle restricted view (minimal info returned)
  if (community?.restricted) {
    return (
      <div className="max-w-xl mx-auto px-4 py-20 text-center">
        <div className="w-24 h-24 rounded-xl bg-navy-800 mx-auto mb-6 flex items-center justify-center">
          {community.logoUrl ? (
            <img src={community.logoUrl} alt="" className="w-full h-full rounded-xl object-cover" />
          ) : (
            <img src="/waddlebot-logo.png" alt="Community Logo" className="w-16 h-16" />
          )}
        </div>
        <h1 className="text-2xl font-bold mb-2 text-sky-100">{community.displayName || community.name}</h1>
        <p className="text-navy-400 mb-6">
          {community.visibility === 'members_only'
            ? 'This community is only visible to members.'
            : 'You need to be logged in to view this community.'}
        </p>
        {!isAuthenticated && (
          <Link to="/login" className="btn btn-primary">
            Log In to View
          </Link>
        )}
      </div>
    );
  }

  return (
    <div>
      {/* Banner */}
      <div className="h-48 bg-gradient-to-r from-navy-800 via-sky-900 to-navy-800 relative">
        {community.bannerUrl && (
          <img
            src={community.bannerUrl}
            alt=""
            className="w-full h-full object-cover"
          />
        )}
      </div>

      {/* Header */}
      <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="relative -mt-16 flex flex-col md:flex-row md:items-end md:space-x-6 pb-6 border-b border-navy-700">
          <div className="w-32 h-32 rounded-xl bg-navy-800 shadow-lg flex items-center justify-center overflow-hidden border border-navy-600">
            {community.logoUrl ? (
              <img src={community.logoUrl} alt={community.displayName} className="w-full h-full object-cover" />
            ) : (
              <img src="/waddlebot-logo.png" alt="Community Logo" className="w-24 h-24" />
            )}
          </div>
          <div className="mt-4 md:mt-0 flex-1">
            <h1 className="text-3xl font-bold text-sky-100">{community.displayName}</h1>
            <p className="text-navy-400 mt-1">{community.description || 'No description'}</p>
            <div className="flex items-center space-x-4 mt-3 text-sm text-navy-500">
              <span>{community.memberCount} members</span>
              <span>Since {new Date(community.createdAt).toLocaleDateString()}</span>
            </div>
          </div>
          {requiresApproval && requestStatus !== 'approved' && (
            <button
              type="button"
              onClick={() => (isAuthenticated ? setShowJoinModal(true) : handleJoin())}
              disabled={submitJoin.isPending || requestStatus === 'pending'}
              className="btn btn-primary mt-4 md:mt-0 disabled:opacity-50"
              data-testid="join-button"
            >
              {submitJoin.isPending
                ? 'Sending...'
                : requestStatus === 'pending'
                  ? 'Request Pending'
                  : requestStatus === 'rejected'
                    ? 'Request Again'
                    : 'Request to Join'}
            </button>
          )}
          {requestStatus === 'approved' && (
            <Link to={`/dashboard/community/${id}`} className="btn btn-primary mt-4 md:mt-0" data-testid="open-dashboard">
              Open Community
            </Link>
          )}
        </div>

        {/* Join status */}
        {!requiresApproval && (
          <div className="mt-4" data-testid="open-join-unavailable">
            <NotAvailableYet feature="community-open-join" className="py-6" />
          </div>
        )}
        {submitJoin.isSuccess && (
          <Alert tone="success" className="mt-4">
            Your request was sent. Community admins will review it.
          </Alert>
        )}
        {submitJoin.isError && (
          <Alert tone="danger" title="We could not send your request" className="mt-4">
            {getErrorMessage(submitJoin.error, 'Failed to send join request')}
          </Alert>
        )}
        {!submitJoin.isSuccess && requestStatus === 'pending' && (
          <Alert tone="info" className="mt-4">
            Your request to join is pending review.
          </Alert>
        )}
        {!submitJoin.isSuccess && requestStatus === 'rejected' && (
          <Alert tone="warning" className="mt-4">
            Your last request was declined. You can ask again.
          </Alert>
        )}
        {myRequest.isError && (
          <Alert tone="warning" title="We could not check your join request" className="mt-4">
            {getErrorMessage(myRequest.error)}
          </Alert>
        )}

        {/* Content */}
        <div className="py-8 space-y-8">
          {/* About Section */}
          <div>
            <h2 className="text-xl font-semibold mb-4 text-sky-100">About</h2>
            <div className="card p-6">
              {community.aboutExtended ? (
                <p className="text-navy-300 whitespace-pre-wrap">{community.aboutExtended}</p>
              ) : (
                <p className="text-navy-500">
                  {community.description || 'This community has not added a description yet.'}
                </p>
              )}
            </div>
          </div>

          {/* Links Section */}
          {(community.websiteUrl || community.discordInviteUrl || Object.keys(community.socialLinks || {}).length > 0) && (
            <div>
              <h2 className="text-xl font-semibold mb-4 text-sky-100">Links</h2>
              <div className="card p-6">
                <div className="flex flex-wrap gap-3">
                  {community.websiteUrl && (
                    <a
                      href={community.websiteUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-2 px-4 py-2 bg-navy-700 hover:bg-navy-600 rounded-lg text-sky-200 transition-colors"
                    >
                      <GlobeAltIcon className="w-5 h-5" />
                      Website
                    </a>
                  )}
                  {community.discordInviteUrl && (
                    <a
                      href={community.discordInviteUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-white transition-colors"
                      style={{ backgroundColor: '#5865F2' }}
                    >
                      <span className="font-bold">D</span>
                      Discord
                    </a>
                  )}
                  {Object.entries(community.socialLinks || {}).map(([key, url]) => {
                    if (!url) return null;
                    const info = SOCIAL_ICONS[key] || { name: key, color: '#6B7280' };
                    return (
                      <a
                        key={key}
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-white transition-opacity hover:opacity-80"
                        style={{ backgroundColor: info.color }}
                      >
                        {info.name}
                      </a>
                    );
                  })}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Join Request Modal */}
      <FormModalBuilder
        isOpen={showJoinModal}
        onClose={() => setShowJoinModal(false)}
        onSubmit={handleJoin}
        title="Request to Join"
        fields={joinRequestFields}
        submitButtonText="Submit Request"
        cancelButtonText="Cancel"
        themeMode="dark"
        colors={WADDLES_COLORS}
      />
    </div>
  );
}

export default CommunityPublicPage;
