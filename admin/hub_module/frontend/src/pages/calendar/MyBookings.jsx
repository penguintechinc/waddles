import { useState, useEffect } from 'react';
import {
  CalendarIcon,
  ClockIcon,
  XMarkIcon,
  UserIcon,
} from '@heroicons/react/24/outline';
import { calendarApi } from '../../services/api';
import { UpgradeNotice, isLicensedFeatureError } from '../../components/ui';

function MyBookings() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [gated, setGated] = useState(false);
  const [activeTab, setActiveTab] = useState('host');
  const [bookings, setBookings] = useState([]);
  const [filter, setFilter] = useState('upcoming');

  useEffect(() => {
    loadBookings();
  }, [activeTab, filter]);

  const loadBookings = async () => {
    try {
      setLoading(true);
      const response = await calendarApi.getMyBookings({
        role: activeTab,
        status: filter === 'upcoming' ? 'confirmed' : filter,
      });
      setBookings(response.data.bookings || []);
    } catch (err) {
      if (isLicensedFeatureError(err)) setGated(true);
      setError('Failed to load bookings');
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = async (uuid) => {
    if (!window.confirm('Cancel this booking?')) return;
    try {
      await calendarApi.cancelBooking(uuid);
      setSuccess('Booking cancelled');
      loadBookings();
    } catch (err) {
      setError('Failed to cancel booking');
    }
  };

  const formatDateTime = (dateStr) => {
    const date = new Date(dateStr);
    return {
      date: date.toLocaleDateString(),
      time: date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    };
  };

  const getStatusBadge = (status) => {
    switch (status) {
      case 'confirmed':
        return <span className="badge badge-green">Confirmed</span>;
      case 'cancelled':
        return <span className="badge badge-red">Cancelled</span>;
      case 'completed':
        return <span className="badge badge-sky">Completed</span>;
      default:
        return <span className="badge">{status}</span>;
    }
  };

  if (gated) {
    return <UpgradeNotice feature="My bookings" />;
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-sky-400"></div>
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto">
      <h1 className="text-2xl font-bold text-sky-100 mb-6">My Bookings</h1>

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

      {/* Tabs */}
      <div className="flex gap-4 border-b border-navy-700 mb-6">
        <button
          onClick={() => setActiveTab('host')}
          className={`px-4 py-2 font-medium border-b-2 transition-colors ${
            activeTab === 'host'
              ? 'text-gold-400 border-gold-500'
              : 'text-navy-400 border-transparent hover:text-navy-300 hover:border-navy-500'
          }`}
        >
          As Host
        </button>
        <button
          onClick={() => setActiveTab('guest')}
          className={`px-4 py-2 font-medium border-b-2 transition-colors ${
            activeTab === 'guest'
              ? 'text-gold-400 border-gold-500'
              : 'text-navy-400 border-transparent hover:text-navy-300 hover:border-navy-500'
          }`}
        >
          As Guest
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-3 mb-6">
        <button
          onClick={() => setFilter('upcoming')}
          className={`btn ${filter === 'upcoming' ? 'btn-primary' : 'btn-secondary'} text-sm`}
        >
          Upcoming
        </button>
        <button
          onClick={() => setFilter('past')}
          className={`btn ${filter === 'past' ? 'btn-primary' : 'btn-secondary'} text-sm`}
        >
          Past
        </button>
        <button
          onClick={() => setFilter('cancelled')}
          className={`btn ${filter === 'cancelled' ? 'btn-primary' : 'btn-secondary'} text-sm`}
        >
          Cancelled
        </button>
      </div>

      {/* Bookings List */}
      {bookings.length === 0 ? (
        <div className="card p-12 text-center">
          <CalendarIcon className="w-16 h-16 text-navy-600 mx-auto mb-4" />
          <h2 className="text-xl font-semibold mb-2 text-sky-100">No Bookings</h2>
          <p className="text-navy-400">No {filter} bookings found.</p>
        </div>
      ) : (
        <div className="space-y-4">
          {bookings.map((booking) => {
            const dateTime = formatDateTime(booking.start_time);
            return (
              <div key={booking.uuid} className="card p-6">
                <div className="flex items-start justify-between mb-4">
                  <div className="flex-1">
                    <h3 className="text-lg font-semibold text-sky-100 mb-2">{booking.booking_page_title}</h3>
                    <div className="flex items-center gap-4 text-sm text-navy-400">
                      <div className="flex items-center gap-1">
                        <UserIcon className="w-4 h-4" />
                        <span>
                          {activeTab === 'host' ? booking.guest_name : booking.host_name}
                        </span>
                      </div>
                      <div className="flex items-center gap-1">
                        <CalendarIcon className="w-4 h-4" />
                        <span>{dateTime.date}</span>
                      </div>
                      <div className="flex items-center gap-1">
                        <ClockIcon className="w-4 h-4" />
                        <span>{dateTime.time}</span>
                      </div>
                    </div>
                    {activeTab === 'host' && booking.guest_email && (
                      <div className="mt-2 text-sm text-navy-500">
                        {booking.guest_email}
                      </div>
                    )}
                  </div>
                  <div className="flex items-center gap-3">
                    {getStatusBadge(booking.status)}
                    {booking.status === 'confirmed' && (
                      <button
                        onClick={() => handleCancel(booking.uuid)}
                        className="btn btn-secondary text-sm py-1 px-3 flex items-center gap-1"
                      >
                        <XMarkIcon className="w-4 h-4" />
                        Cancel
                      </button>
                    )}
                  </div>
                </div>

                {booking.meeting_link && (
                  <div className="p-3 bg-navy-800 rounded-lg border border-navy-700">
                    <div className="text-xs text-navy-400 mb-1">Meeting Link</div>
                    <a
                      href={booking.meeting_link}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-sky-400 hover:text-sky-300 text-sm"
                    >
                      {booking.meeting_link}
                    </a>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default MyBookings;
