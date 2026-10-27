/**
 * Admin Announcements Page
 * Manage community announcements with chat bubble display (iMessage style, newest on top)
 */
import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import AnnouncementModal from '../../components/AnnouncementModal';
import BroadcastStatusModal from '../../components/BroadcastStatusModal';
import {
  PlusIcon,
  PencilIcon,
  TrashIcon,
  MegaphoneIcon,
  MapPinIcon,
} from '@heroicons/react/24/outline';

export default function AdminAnnouncements() {
  const { communityId } = useParams();

  // State
  const [announcements, setAnnouncements] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [filter, setFilter] = useState('all'); // 'all', 'published', 'draft', 'archived'
  const [showModal, setShowModal] = useState(false);
  const [editingAnnouncement, setEditingAnnouncement] = useState(null);
  const [showBroadcastModal, setShowBroadcastModal] = useState(false);
  const [broadcastAnnouncement, setBroadcastAnnouncement] = useState(null);
  const [connectedPlatforms, setConnectedPlatforms] = useState([]);
  const [actionLoading, setActionLoading] = useState({});

  // Type color mapping
  const typeColors = {
    general: 'bg-navy-700 border-navy-600',
    important: 'bg-red-500/20 border-red-500/30 border-l-4 border-l-red-500',
    event: 'bg-purple-500/20 border-purple-500/30 border-l-4 border-l-purple-500',
    update: 'bg-sky-500/20 border-sky-500/30 border-l-4 border-l-sky-500',
  };

  // Status badge colors
  const statusColors = {
    draft: { bg: 'bg-yellow-500/20', text: 'text-yellow-400', label: 'Draft' },
    published: { bg: 'bg-green-500/20', text: 'text-green-400', label: 'Published' },
    archived: { bg: 'bg-gray-500/20', text: 'text-gray-400', label: 'Archived' },
  };

  // Fetch announcements
  useEffect(() => {
    fetchAnnouncements();
  }, [communityId, page, filter]);

  const fetchAnnouncements = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await adminApi.getAnnouncements(communityId, {
        page,
        status: filter !== 'all' ? filter : undefined,
        limit: 20,
      });
      setAnnouncements(response.data.data || []);
      setTotalPages(response.data.pagination?.totalPages || 1);
      setConnectedPlatforms(response.data.connectedPlatforms || []);
    } catch (err) {
      console.error('Failed to load announcements:', err);
      setError(
        err.response?.data?.error?.message || 'Failed to load announcements'
      );
    } finally {
      setLoading(false);
    }
  };

  const handleCreate = () => {
    setEditingAnnouncement(null);
    setShowModal(true);
  };

  const handleEdit = (announcement) => {
    setEditingAnnouncement(announcement);
    setShowModal(true);
  };

  const handleSaveAnnouncement = async (data) => {
    try {
      setActionLoading({ ...actionLoading, modal: true });

      if (editingAnnouncement) {
        // Update announcement
        await adminApi.updateAnnouncement(
          communityId,
          editingAnnouncement.id,
          data
        );
      } else {
        // Create announcement
        await adminApi.createAnnouncement(communityId, data);
      }

      fetchAnnouncements();
      setShowModal(false);
      setEditingAnnouncement(null);
    } catch (err) {
      console.error('Failed to save announcement:', err);
      setError(
        err.response?.data?.error?.message || 'Failed to save announcement'
      );
    } finally {
      setActionLoading({ ...actionLoading, modal: false });
    }
  };

  const handleDelete = async (announcementId) => {
    if (
      !window.confirm(
        'Are you sure you want to delete this announcement? This cannot be undone.'
      )
    ) {
      return;
    }

    try {
      setActionLoading({ ...actionLoading, [announcementId]: 'deleting' });
      await adminApi.deleteAnnouncement(communityId, announcementId);
      fetchAnnouncements();
    } catch (err) {
      console.error('Failed to delete announcement:', err);
      setError(err.response?.data?.error?.message || 'Failed to delete announcement');
    } finally {
      setActionLoading({ ...actionLoading, [announcementId]: null });
    }
  };

  const handleBroadcast = (announcement) => {
    setBroadcastAnnouncement(announcement);
    setShowBroadcastModal(true);
  };

  const handleBroadcastSubmit = async (platforms) => {
    try {
      await adminApi.broadcastAnnouncement(
        communityId,
        broadcastAnnouncement.id,
        platforms
      );
      setShowBroadcastModal(false);
      setBroadcastAnnouncement(null);
    } catch (err) {
      console.error('Failed to broadcast announcement:', err);
      throw err;
    }
  };

  const handlePublish = async (announcementId) => {
    try {
      setActionLoading({ ...actionLoading, [announcementId]: 'publishing' });
      await adminApi.publishAnnouncement(communityId, announcementId);
      fetchAnnouncements();
    } catch (err) {
      console.error('Failed to publish announcement:', err);
      setError(err.response?.data?.error?.message || 'Failed to publish announcement');
    } finally {
      setActionLoading({ ...actionLoading, [announcementId]: null });
    }
  };

  const handlePin = async (announcementId) => {
    try {
      setActionLoading({ ...actionLoading, [announcementId]: 'pinning' });
      await adminApi.pinAnnouncement(communityId, announcementId);
      fetchAnnouncements();
    } catch (err) {
      console.error('Failed to pin announcement:', err);
      setError(err.response?.data?.error?.message || 'Failed to pin announcement');
    } finally {
      setActionLoading({ ...actionLoading, [announcementId]: null });
    }
  };

  const handleUnpin = async (announcementId) => {
    try {
      setActionLoading({ ...actionLoading, [announcementId]: 'unpinning' });
      await adminApi.unpinAnnouncement(communityId, announcementId);
      fetchAnnouncements();
    } catch (err) {
      console.error('Failed to unpin announcement:', err);
      setError(err.response?.data?.error?.message || 'Failed to unpin announcement');
    } finally {
      setActionLoading({ ...actionLoading, [announcementId]: null });
    }
  };

  const handlePageChange = (newPage) => {
    if (newPage > 0 && newPage <= totalPages) {
      setPage(newPage);
    }
  };

  // Separate pinned and unpinned announcements
  const pinnedAnnouncements = announcements.filter((a) => a.is_pinned);
  const unpinnedAnnouncements = announcements.filter((a) => !a.is_pinned);

  // Loading spinner
  if (loading && announcements.length === 0) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-navy-900">
        <div className="animate-spin">
          <div className="w-12 h-12 border-4 border-navy-700 border-t-sky-400 rounded-full"></div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-navy-900 text-white p-6">
      <div className="max-w-4xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-gold-400 mb-2">
            Community Announcements
          </h1>
          <p className="text-navy-300">
            Manage and broadcast announcements to your community
          </p>
        </div>

        {/* Error Message */}
        {error && (
          <div className="mb-6 bg-red-900/20 border border-red-500 text-red-400 px-4 py-3 rounded-lg flex items-center justify-between">
            <span>{error}</span>
            <button
              onClick={() => setError(null)}
              className="font-bold hover:text-red-300"
            >
              ×
            </button>
          </div>
        )}

        {/* Top Bar */}
        <div className="mb-6 flex items-center justify-between flex-wrap gap-4">
          {/* Create Button */}
          <button
            onClick={handleCreate}
            className="flex items-center gap-2 px-4 py-2 bg-gold-500 hover:bg-gold-600 text-navy-900 rounded-lg font-semibold transition-colors"
          >
            <PlusIcon className="w-5 h-5" />
            Create Announcement
          </button>

          {/* Filter Tabs */}
          <div className="flex gap-2">
            {[
              { value: 'all', label: 'All' },
              { value: 'published', label: 'Published' },
              { value: 'draft', label: 'Draft' },
              { value: 'archived', label: 'Archived' },
            ].map((tab) => (
              <button
                key={tab.value}
                onClick={() => {
                  setFilter(tab.value);
                  setPage(1);
                }}
                className={`px-4 py-2 rounded-lg font-medium transition-colors ${
                  filter === tab.value
                    ? 'bg-sky-600 text-white'
                    : 'bg-navy-800 hover:bg-navy-700 text-sky-100 border border-navy-700'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* Empty State */}
        {announcements.length === 0 && (
          <div className="text-center py-12">
            <MegaphoneIcon className="w-12 h-12 text-navy-600 mx-auto mb-4" />
            <p className="text-navy-300 text-lg">
              No announcements yet. Create one to get started!
            </p>
          </div>
        )}

        {/* Announcements Display */}
        {announcements.length > 0 && (
          <div className="space-y-3">
            {/* Pinned Announcements Section */}
            {pinnedAnnouncements.length > 0 && (
              <div className="mb-6">
                <div className="flex items-center gap-2 mb-4">
                  <MapPinIcon className="w-5 h-5 text-gold-400" />
                  <h2 className="text-lg font-semibold text-gold-400">
                    Pinned Announcements
                  </h2>
                </div>

                <div className="space-y-3">
                  {pinnedAnnouncements.map((announcement) => (
                    <AnnouncementBubble
                      key={announcement.id}
                      announcement={announcement}
                      typeColors={typeColors}
                      statusColors={statusColors}
                      actionLoading={actionLoading}
                      isPinned={true}
                      onEdit={handleEdit}
                      onDelete={handleDelete}
                      onBroadcast={handleBroadcast}
                      onPin={handleUnpin}
                      onPublish={handlePublish}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* Regular Announcements Section */}
            {unpinnedAnnouncements.length > 0 && (
              <div>
                {pinnedAnnouncements.length > 0 && (
                  <h2 className="text-lg font-semibold text-sky-100 mb-4">
                    Recent Announcements
                  </h2>
                )}

                <div className="space-y-3">
                  {unpinnedAnnouncements.map((announcement) => (
                    <AnnouncementBubble
                      key={announcement.id}
                      announcement={announcement}
                      typeColors={typeColors}
                      statusColors={statusColors}
                      actionLoading={actionLoading}
                      isPinned={false}
                      onEdit={handleEdit}
                      onDelete={handleDelete}
                      onBroadcast={handleBroadcast}
                      onPin={handlePin}
                      onPublish={handlePublish}
                    />
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="mt-8 flex justify-center gap-2">
            <button
              onClick={() => handlePageChange(page - 1)}
              disabled={page === 1}
              className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg disabled:opacity-50 hover:border-sky-500 transition-colors text-sky-100 font-medium"
            >
              Previous
            </button>
            <span className="px-4 py-2 text-navy-300">
              Page {page} of {totalPages}
            </span>
            <button
              onClick={() => handlePageChange(page + 1)}
              disabled={page === totalPages}
              className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg disabled:opacity-50 hover:border-sky-500 transition-colors text-sky-100 font-medium"
            >
              Next
            </button>
          </div>
        )}
      </div>

      {/* Announcement Modal */}
      <AnnouncementModal
        isOpen={showModal}
        onClose={() => {
          setShowModal(false);
          setEditingAnnouncement(null);
        }}
        onSave={handleSaveAnnouncement}
        announcement={editingAnnouncement}
        connectedPlatforms={connectedPlatforms}
      />

      {/* Broadcast Status Modal */}
      <BroadcastStatusModal
        isOpen={showBroadcastModal}
        onClose={() => {
          setShowBroadcastModal(false);
          setBroadcastAnnouncement(null);
        }}
        announcementId={broadcastAnnouncement?.id}
        communityId={communityId}
        onBroadcast={handleBroadcastSubmit}
      />
    </div>
  );
}

/**
 * Announcement Bubble Component (iMessage style)
 */
function AnnouncementBubble({
  announcement,
  typeColors,
  statusColors,
  actionLoading,
  isPinned,
  onEdit,
  onDelete,
  onBroadcast,
  onPin,
  onPublish,
}) {
  const typeColor = typeColors[announcement.announcement_type] || typeColors.general;
  const statusColor = statusColors[announcement.status];
  const createdAt = new Date(announcement.created_at);
  const formattedDate = createdAt.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: createdAt.getFullYear() !== new Date().getFullYear() ? 'numeric' : undefined,
  });
  const formattedTime = createdAt.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
  });

  const contentPreview =
    announcement.content.length > 150
      ? announcement.content.substring(0, 150) + '...'
      : announcement.content;

  const isLoading = actionLoading[announcement.id];

  return (
    <div className={`${typeColor} border rounded-lg p-4 hover:shadow-lg transition-all`}>
      {/* Top row: Title and badges */}
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="flex-1 min-w-0">
          <h3 className="font-bold text-sky-100 break-words">{announcement.title}</h3>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {isPinned && (
            <span className="text-lg" title="Pinned">📌</span>
          )}
          <span className="bg-navy-600 text-sky-200 text-xs font-medium px-2 py-1 rounded capitalize">
            {announcement.announcement_type}
          </span>
          <span className={`${statusColor.bg} ${statusColor.text} text-xs font-medium px-2 py-1 rounded`}>
            {statusColor.label}
          </span>
        </div>
      </div>

      {/* Content preview */}
      <p className="text-sky-200 text-sm mb-3 break-words leading-relaxed">
        {contentPreview}
      </p>

      {/* Meta */}
      <div className="flex items-center text-xs text-navy-300 gap-3 mb-3">
        <span>by {announcement.author_name || 'Unknown'}</span>
        <span>{formattedDate}</span>
        <span>{formattedTime}</span>
      </div>

      {/* Actions — always visible, explicit type="button" to prevent form submission */}
      <div className="flex flex-wrap items-center gap-2 border-t border-navy-600/40 pt-3">
        {announcement.status === 'draft' && onPublish && (
          <button
            type="button"
            onClick={() => onPublish(announcement.id)}
            disabled={!!isLoading}
            className="flex items-center gap-1 px-3 py-1 bg-green-600 hover:bg-green-700 text-white text-sm rounded transition-colors disabled:opacity-50 font-medium"
          >
            Publish
          </button>
        )}
        <button
          type="button"
          onClick={() => onEdit(announcement)}
          disabled={!!isLoading}
          className="flex items-center gap-1 px-3 py-1 bg-sky-600 hover:bg-sky-700 text-white text-sm rounded transition-colors disabled:opacity-50 font-medium"
        >
          <PencilIcon className="w-4 h-4" />
          Edit
        </button>
        <button
          type="button"
          onClick={() => onBroadcast(announcement)}
          disabled={!!isLoading}
          className="flex items-center gap-1 px-3 py-1 bg-purple-600 hover:bg-purple-700 text-white text-sm rounded transition-colors disabled:opacity-50 font-medium"
        >
          <MegaphoneIcon className="w-4 h-4" />
          Broadcast
        </button>
        <button
          type="button"
          onClick={() => onPin(announcement.id)}
          disabled={!!isLoading}
          className="flex items-center gap-1 px-3 py-1 bg-gold-500 hover:bg-gold-600 text-navy-900 text-sm rounded transition-colors disabled:opacity-50 font-medium"
        >
          <MapPinIcon className="w-4 h-4" />
          {isPinned ? 'Unpin' : 'Pin'}
        </button>
        <button
          type="button"
          onClick={() => onDelete(announcement.id)}
          disabled={!!isLoading}
          className="flex items-center gap-1 px-3 py-1 bg-red-600 hover:bg-red-700 text-white text-sm rounded transition-colors disabled:opacity-50 font-medium"
        >
          <TrashIcon className="w-4 h-4" />
          Delete
        </button>
        {isLoading && (
          <span className="text-xs text-navy-300 ml-auto animate-pulse">{isLoading}...</span>
        )}
      </div>
    </div>
  );
}
