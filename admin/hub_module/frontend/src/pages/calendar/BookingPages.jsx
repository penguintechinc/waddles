import { useState, useEffect, useMemo } from 'react';
import {
  CalendarIcon,
  PlusIcon,
  LinkIcon,
  TrashIcon,
  PencilIcon,
  ClipboardDocumentIcon,
} from '@heroicons/react/24/outline';
import { calendarApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

function BookingPages() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [bookingPages, setBookingPages] = useState([]);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [editingPage, setEditingPage] = useState(null);

  useEffect(() => {
    loadBookingPages();
  }, []);

  const loadBookingPages = async () => {
    try {
      setLoading(true);
      const response = await calendarApi.getBookingPages();
      setBookingPages(response.data.booking_pages || []);
    } catch (err) {
      setError('Failed to load booking pages');
    } finally {
      setLoading(false);
    }
  };

  const handleCreatePage = async (data) => {
    try {
      await calendarApi.createBookingPage({
        title: data.title?.trim(),
        description: data.description?.trim() || '',
        slug: data.slug?.trim(),
        slot_duration: parseInt(data.slot_duration),
        access_scope: data.access_scope,
      });
      setSuccess('Booking page created');
      setShowCreateModal(false);
      loadBookingPages();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create booking page');
      throw err;
    }
  };

  const handleUpdatePage = async (data) => {
    try {
      await calendarApi.updateBookingPage(editingPage.id, {
        title: data.title?.trim(),
        description: data.description?.trim() || '',
        slot_duration: parseInt(data.slot_duration),
        access_scope: data.access_scope,
        is_active: data.is_active,
      });
      setSuccess('Booking page updated');
      setEditingPage(null);
      loadBookingPages();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to update booking page');
      throw err;
    }
  };

  const handleDelete = async (pageId) => {
    if (!window.confirm('Delete this booking page?')) return;
    try {
      await calendarApi.deleteBookingPage(pageId);
      setSuccess('Booking page deleted');
      loadBookingPages();
    } catch (err) {
      setError('Failed to delete booking page');
    }
  };

  const handleCopyLink = (slug) => {
    const url = `${window.location.origin}/book/${slug}`;
    navigator.clipboard.writeText(url);
    setSuccess('Link copied to clipboard');
  };

  const createFields = useMemo(() => [
    {
      name: 'title',
      type: 'text',
      label: 'Title',
      required: true,
      placeholder: '30 Minute Meeting',
    },
    {
      name: 'description',
      type: 'textarea',
      label: 'Description',
      placeholder: 'Quick chat about...',
      rows: 3,
    },
    {
      name: 'slug',
      type: 'text',
      label: 'Slug (URL)',
      required: true,
      placeholder: '30min-meeting',
      helpText: 'Letters, numbers, and hyphens only',
    },
    {
      name: 'slot_duration',
      type: 'select',
      label: 'Duration',
      required: true,
      defaultValue: '30',
      options: [
        { value: '15', label: '15 minutes' },
        { value: '30', label: '30 minutes' },
        { value: '60', label: '60 minutes' },
      ],
    },
    {
      name: 'access_scope',
      type: 'select',
      label: 'Access',
      required: true,
      defaultValue: 'public',
      options: [
        { value: 'public', label: 'Public' },
        { value: 'registered', label: 'Registered Users' },
        { value: 'community', label: 'Community Members' },
      ],
    },
  ], []);

  const editFields = useMemo(() => {
    if (!editingPage) return [];
    return [
      {
        name: 'title',
        type: 'text',
        label: 'Title',
        required: true,
        defaultValue: editingPage.title,
      },
      {
        name: 'description',
        type: 'textarea',
        label: 'Description',
        defaultValue: editingPage.description,
        rows: 3,
      },
      {
        name: 'slot_duration',
        type: 'select',
        label: 'Duration',
        required: true,
        defaultValue: String(editingPage.slot_duration),
        options: [
          { value: '15', label: '15 minutes' },
          { value: '30', label: '30 minutes' },
          { value: '60', label: '60 minutes' },
        ],
      },
      {
        name: 'access_scope',
        type: 'select',
        label: 'Access',
        required: true,
        defaultValue: editingPage.access_scope,
        options: [
          { value: 'public', label: 'Public' },
          { value: 'registered', label: 'Registered Users' },
          { value: 'community', label: 'Community Members' },
        ],
      },
      {
        name: 'is_active',
        type: 'checkbox',
        label: 'Active',
        defaultValue: editingPage.is_active,
      },
    ];
  }, [editingPage]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-sky-400"></div>
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-sky-100">Booking Pages</h1>
        <button onClick={() => setShowCreateModal(true)} className="btn btn-primary flex items-center gap-2">
          <PlusIcon className="w-5 h-5" />
          Create Booking Page
        </button>
      </div>

      {error && (
        <div className="p-4 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 mb-6">
          {error}
        </div>
      )}

      {success && (
        <div className="p-4 bg-emerald-500/20 border border-emerald-500/30 rounded-lg text-emerald-300 mb-6">
          {success}
        </div>
      )}

      {bookingPages.length === 0 ? (
        <div className="card p-12 text-center">
          <CalendarIcon className="w-16 h-16 text-navy-600 mx-auto mb-4" />
          <h2 className="text-xl font-semibold mb-2 text-sky-100">No Booking Pages</h2>
          <p className="text-navy-400 mb-6">Create your first booking page to start accepting appointments.</p>
          <button onClick={() => setShowCreateModal(true)} className="btn btn-primary">
            Create Booking Page
          </button>
        </div>
      ) : (
        <div className="grid md:grid-cols-2 gap-6">
          {bookingPages.map((page) => (
            <div key={page.id} className="card p-6">
              <div className="flex items-start justify-between mb-4">
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <h3 className="text-lg font-semibold text-sky-100">{page.title}</h3>
                    {!page.is_active && (
                      <span className="badge badge-red text-xs">Inactive</span>
                    )}
                  </div>
                  {page.description && (
                    <p className="text-sm text-navy-400 mb-2">{page.description}</p>
                  )}
                  <div className="text-sm text-navy-500">
                    {page.slot_duration} min · {page.access_scope}
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-2 p-3 bg-navy-800 rounded-lg mb-4 border border-navy-700">
                <LinkIcon className="w-4 h-4 text-navy-500 flex-shrink-0" />
                <code className="text-xs text-sky-300 flex-1 truncate">/book/{page.slug}</code>
                <button
                  onClick={() => handleCopyLink(page.slug)}
                  className="btn btn-secondary text-xs py-1 px-2 flex items-center gap-1 flex-shrink-0"
                >
                  <ClipboardDocumentIcon className="w-4 h-4" />
                  Copy
                </button>
              </div>

              <div className="flex gap-2">
                <button
                  onClick={() => setEditingPage(page)}
                  className="btn btn-secondary text-sm flex-1 flex items-center justify-center gap-1"
                >
                  <PencilIcon className="w-4 h-4" />
                  Edit
                </button>
                <button
                  aria-label={`Delete booking page ${page.title}`}
                  onClick={() => handleDelete(page.id)}
                  className="btn btn-secondary text-sm px-3"
                >
                  <TrashIcon className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {showCreateModal && (
        <FormModalBuilder
          title="Create Booking Page"
          fields={createFields}
          onSubmit={handleCreatePage}
          onCancel={() => setShowCreateModal(false)}
          themeMode="dark"
          colors={WADDLES_COLORS}
        />
      )}

      {editingPage && (
        <FormModalBuilder
          title="Edit Booking Page"
          fields={editFields}
          onSubmit={handleUpdatePage}
          onCancel={() => setEditingPage(null)}
          themeMode="dark"
          colors={WADDLES_COLORS}
        />
      )}
    </div>
  );
}

export default BookingPages;
