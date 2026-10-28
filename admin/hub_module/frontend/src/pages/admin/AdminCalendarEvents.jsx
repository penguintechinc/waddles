import { useState, useEffect, useMemo } from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  CalendarDaysIcon,
  PlusIcon,
  PencilIcon,
  TrashIcon,
  CheckIcon,
  XMarkIcon,
  TicketIcon,
  QrCodeIcon,
  ClipboardDocumentListIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import EventDiscordSyncToggle from '../../components/calendar/EventDiscordSyncToggle';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

const CATEGORY_OPTIONS = [
  { value: 'meetup', label: 'Meetup' },
  { value: 'workshop', label: 'Workshop' },
  { value: 'stream', label: 'Stream' },
  { value: 'social', label: 'Social' },
  { value: 'competition', label: 'Competition' },
  { value: 'other', label: 'Other' },
];

const STATUS_STYLES = {
  pending: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
  approved: 'bg-green-500/20 text-green-300 border-green-500/30',
  rejected: 'bg-red-500/20 text-red-300 border-red-500/30',
  cancelled: 'bg-navy-500/20 text-fg-muted border-navy-500/30',
};

function AdminCalendarEvents() {
  const { communityId } = useParams();
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [showModal, setShowModal] = useState(false);
  const [editingEvent, setEditingEvent] = useState(null);

  useEffect(() => {
    fetchEvents();
  }, [communityId]);

  const fetchEvents = async () => {
    try {
      setLoading(true);
      const response = await adminApi.getCalendarEvents(communityId);
      setEvents(response.data?.events || []);
    } catch (err) {
      setError('Failed to load events');
    } finally {
      setLoading(false);
    }
  };

  const handleCreate = async (data) => {
    try {
      await adminApi.createCalendarEvent(communityId, {
        title: data.title?.trim(),
        description: data.description?.trim() || '',
        start_date: data.start_date,
        end_date: data.end_date || null,
        location: data.location?.trim() || '',
        category: data.category || 'other',
        max_attendees: data.max_attendees ? parseInt(data.max_attendees, 10) : null,
        requires_approval: data.requires_approval || false,
      });
      setMessage({ type: 'success', text: 'Event created' });
      setShowModal(false);
      setEditingEvent(null);
      fetchEvents();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create event');
      throw err;
    }
  };

  const handleUpdate = async (data) => {
    try {
      await adminApi.updateCalendarEvent(communityId, editingEvent.id, {
        title: data.title?.trim(),
        description: data.description?.trim() || '',
        start_date: data.start_date,
        end_date: data.end_date || null,
        location: data.location?.trim() || '',
        category: data.category || 'other',
        max_attendees: data.max_attendees ? parseInt(data.max_attendees, 10) : null,
        requires_approval: data.requires_approval || false,
      });
      setMessage({ type: 'success', text: 'Event updated' });
      setShowModal(false);
      setEditingEvent(null);
      fetchEvents();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to update event');
      throw err;
    }
  };

  const handleDelete = async (eventId) => {
    if (!window.confirm('Delete this event?')) return;
    try {
      await adminApi.deleteCalendarEvent(communityId, eventId);
      setMessage({ type: 'success', text: 'Event deleted' });
      fetchEvents();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to delete event');
    }
  };

  const handleApprove = async (eventId) => {
    try {
      await adminApi.approveCalendarEvent(communityId, eventId);
      setMessage({ type: 'success', text: 'Event approved' });
      fetchEvents();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to approve event');
    }
  };

  const handleReject = async (eventId) => {
    const reason = window.prompt('Rejection reason (optional):');
    if (reason === null) return;
    try {
      await adminApi.rejectCalendarEvent(communityId, eventId, { reason });
      setMessage({ type: 'success', text: 'Event rejected' });
      fetchEvents();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to reject event');
    }
  };

  const openEditModal = (event) => {
    setEditingEvent(event);
    setShowModal(true);
  };

  const openCreateModal = () => {
    setEditingEvent(null);
    setShowModal(true);
  };

  const formFields = useMemo(() => [
    { name: 'title', type: 'text', label: 'Title', required: true, placeholder: 'Community Meetup' },
    { name: 'description', type: 'textarea', label: 'Description', placeholder: 'Event details...', rows: 3 },
    { name: 'start_date', type: 'text', label: 'Start Date', required: true, placeholder: 'YYYY-MM-DDTHH:MM (datetime-local format)' },
    { name: 'end_date', type: 'text', label: 'End Date', placeholder: 'YYYY-MM-DDTHH:MM (datetime-local format)' },
    { name: 'location', type: 'text', label: 'Location', placeholder: 'Discord, Zoom, or physical address' },
    { name: 'category', type: 'select', label: 'Category', defaultValue: 'other', options: CATEGORY_OPTIONS },
    { name: 'max_attendees', type: 'number', label: 'Max Attendees (optional)', placeholder: 'Leave blank for unlimited' },
    { name: 'requires_approval', type: 'checkbox', label: 'Require admin approval for RSVPs', defaultValue: false },
  ], []);

  const initialValues = useMemo(() => {
    if (!editingEvent) return {};
    return {
      title: editingEvent.title || '',
      description: editingEvent.description || '',
      start_date: editingEvent.start_date || '',
      end_date: editingEvent.end_date || '',
      location: editingEvent.location || '',
      category: editingEvent.category || 'other',
      max_attendees: editingEvent.max_attendees || '',
      requires_approval: editingEvent.requires_approval || false,
    };
  }, [editingEvent]);

  if (loading) {
    return (
      <div className="flex justify-center items-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-sky-500"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white flex items-center gap-2">
          <CalendarDaysIcon className="h-8 w-8 text-sky-500" />
          Calendar Events
        </h1>
        <button onClick={openCreateModal} className="btn btn-primary">
          <PlusIcon className="h-5 w-5 mr-2" />
          Create Event
        </button>
      </div>

      <EventDiscordSyncToggle communityId={communityId} />

      {error && (
        <div className="bg-red-500/20 border border-red-500 text-red-300 px-4 py-3 rounded">
          {error}
          <button onClick={() => setError(null)} className="float-right">&times;</button>
        </div>
      )}

      {message && (
        <div className={`px-4 py-3 rounded ${message.type === 'success' ? 'bg-green-500/20 text-green-300' : 'bg-red-500/20 text-red-300'}`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">&times;</button>
        </div>
      )}

      {events.length === 0 ? (
        <div className="card p-8 text-center">
          <CalendarDaysIcon className="h-16 w-16 text-fg-muted mx-auto mb-4" />
          <p className="text-fg-muted">No events yet. Create your first event!</p>
        </div>
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full text-left">
            <thead className="bg-navy-800 border-b border-navy-700">
              <tr>
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Title</th>
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Date</th>
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Status</th>
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Category</th>
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">RSVPs</th>
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-navy-800">
              {events.map((event) => (
                <tr key={event.id} className="hover:bg-navy-800/50">
                  <td className="px-4 py-3">
                    <div className="text-white font-medium">{event.title}</div>
                    {event.location && (
                      <div className="text-xs text-navy-400 mt-0.5">{event.location}</div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-sm text-navy-300">
                    {event.start_date ? new Date(event.start_date).toLocaleString() : '-'}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`inline-block text-xs px-2 py-0.5 rounded border ${STATUS_STYLES[event.status] || STATUS_STYLES.pending}`}>
                      {event.status || 'pending'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-sm text-navy-300 capitalize">
                    {event.category || '-'}
                  </td>
                  <td className="px-4 py-3 text-sm text-navy-300">
                    {event.rsvp_count ?? '-'}
                    {event.max_attendees ? ` / ${event.max_attendees}` : ''}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1 flex-wrap">
                      {event.status === 'pending' && (
                        <>
                          <button
                            onClick={() => handleApprove(event.id)}
                            className="p-1.5 rounded text-green-400 hover:bg-green-500/20"
                            title="Approve"
                          >
                            <CheckIcon className="h-4 w-4" />
                          </button>
                          <button
                            onClick={() => handleReject(event.id)}
                            className="p-1.5 rounded text-red-400 hover:bg-red-500/20"
                            title="Reject"
                          >
                            <XMarkIcon className="h-4 w-4" />
                          </button>
                        </>
                      )}
                      <button
                        onClick={() => openEditModal(event)}
                        className="p-1.5 rounded text-sky-400 hover:bg-sky-500/20"
                        title="Edit"
                      >
                        <PencilIcon className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(event.id)}
                        className="p-1.5 rounded text-red-400 hover:bg-red-500/20"
                        title="Delete"
                      >
                        <TrashIcon className="h-4 w-4" />
                      </button>
                      <Link
                        to={`/admin/${communityId}/calendar/events/${event.id}/tickets`}
                        className="p-1.5 rounded text-gold-400 hover:bg-gold-500/20"
                        title="Ticketing"
                      >
                        <TicketIcon className="h-4 w-4" />
                      </Link>
                      <Link
                        to={`/admin/${communityId}/calendar/events/${event.id}/scanner`}
                        className="p-1.5 rounded text-gold-400 hover:bg-gold-500/20"
                        title="Scanner"
                      >
                        <QrCodeIcon className="h-4 w-4" />
                      </Link>
                      <Link
                        to={`/admin/${communityId}/calendar/events/${event.id}/attendance`}
                        className="p-1.5 rounded text-gold-400 hover:bg-gold-500/20"
                        title="Attendance"
                      >
                        <ClipboardDocumentListIcon className="h-4 w-4" />
                      </Link>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <FormModalBuilder
        title={editingEvent ? 'Edit Event' : 'Create Event'}
        fields={formFields}
        initialValues={initialValues}
        isOpen={showModal}
        onClose={() => { setShowModal(false); setEditingEvent(null); }}
        onSubmit={editingEvent ? handleUpdate : handleCreate}
        submitButtonText={editingEvent ? 'Save Changes' : 'Create Event'}
        cancelButtonText="Cancel"
        width="lg"
        themeMode="dark"
        colors={WADDLES_COLORS}
      />
    </div>
  );
}

export default AdminCalendarEvents;
