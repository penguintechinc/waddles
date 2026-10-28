import { useState, useEffect, useMemo } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  CalendarIcon,
  ClockIcon,
  CheckCircleIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline';
import { calendarApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

function BookingPagePublic() {
  const { slug } = useParams();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [bookingPage, setBookingPage] = useState(null);
  const [selectedDate, setSelectedDate] = useState(null);
  const [availableSlots, setAvailableSlots] = useState([]);
  const [selectedSlot, setSelectedSlot] = useState(null);
  const [booking, setBooking] = useState(null);
  const [loadingSlots, setLoadingSlots] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [showBookingModal, setShowBookingModal] = useState(false);

  const [currentMonth, setCurrentMonth] = useState(new Date());

  useEffect(() => {
    loadBookingPage();
  }, [slug]);

  useEffect(() => {
    if (selectedDate && bookingPage) {
      loadSlots();
    }
  }, [selectedDate, bookingPage]);

  const loadBookingPage = async () => {
    try {
      setLoading(true);
      const response = await calendarApi.getBookingPage(slug);
      setBookingPage(response.data.booking_page);
      // Auto-select today
      setSelectedDate(new Date().toISOString().split('T')[0]);
    } catch (err) {
      setError('Booking page not found');
    } finally {
      setLoading(false);
    }
  };

  const loadSlots = async () => {
    try {
      setLoadingSlots(true);
      const response = await calendarApi.getBookingSlots(slug, selectedDate);
      setAvailableSlots(response.data.slots || []);
    } catch (err) {
      setError('Failed to load available slots');
    } finally {
      setLoadingSlots(false);
    }
  };

  const handleBookingSubmit = async (data) => {
    if (!selectedSlot) return;

    try {
      setSubmitting(true);
      setError(null);

      // Separate core fields from custom form responses
      const { guest_name, guest_email, ...customResponses } = data;

      const response = await calendarApi.createBooking(slug, {
        start_time: selectedSlot,
        guest_name: guest_name?.trim(),
        guest_email: guest_email?.trim(),
        form_responses: Object.keys(customResponses).length > 0 ? customResponses : undefined,
      });
      setBooking(response.data.booking);
      setShowBookingModal(false);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create booking');
      throw err;
    } finally {
      setSubmitting(false);
    }
  };

  // Build FormModalBuilder fields: guest_name + guest_email + custom form_fields from booking page
  const bookingFormFields = useMemo(() => {
    const baseFields = [
      {
        name: 'guest_name',
        type: 'text',
        label: 'Your Name',
        required: true,
        placeholder: 'John Doe',
        defaultValue: '',
      },
      {
        name: 'guest_email',
        type: 'email',
        label: 'Email',
        required: true,
        placeholder: 'john@example.com',
        defaultValue: '',
      },
    ];

    // Append custom form fields from the booking page config (Phase 4E)
    const customFields = bookingPage?.form_fields || [];
    const mappedCustomFields = customFields.slice(0, 8).map((field) => ({
      name: field.name,
      type: field.type || 'text',
      label: field.label || field.name,
      required: field.required || false,
      placeholder: field.placeholder || '',
      defaultValue: '',
      ...(field.options ? { options: field.options } : {}),
      ...(field.helpText ? { helpText: field.helpText } : {}),
    }));

    return [...baseFields, ...mappedCustomFields];
  }, [bookingPage]);

  const getDaysInMonth = (date) => {
    const year = date.getFullYear();
    const month = date.getMonth();
    const firstDay = new Date(year, month, 1);
    const lastDay = new Date(year, month + 1, 0);
    const days = [];

    // Add empty cells for days before the first day of the month
    const firstDayOfWeek = firstDay.getDay();
    for (let i = 0; i < firstDayOfWeek; i++) {
      days.push(null);
    }

    // Add days of the month
    for (let day = 1; day <= lastDay.getDate(); day++) {
      days.push(new Date(year, month, day));
    }

    return days;
  };

  const isDateSelectable = (date) => {
    if (!date) return false;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return date >= today;
  };

  const formatTime = (isoString) => {
    const date = new Date(isoString);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-navy-950 flex items-center justify-center">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-sky-400"></div>
      </div>
    );
  }

  if (error && !bookingPage) {
    return (
      <div className="min-h-screen bg-navy-950 flex items-center justify-center p-4">
        <div className="card p-8 max-w-md w-full text-center">
          <h2 className="text-xl font-bold text-red-400 mb-2">Error</h2>
          <p className="text-navy-400">{error}</p>
        </div>
      </div>
    );
  }

  if (booking) {
    return (
      <div className="min-h-screen bg-navy-950 flex items-center justify-center p-4">
        <div className="card p-8 max-w-md w-full text-center">
          <CheckCircleIcon className="w-16 h-16 text-emerald-400 mx-auto mb-4" />
          <h2 className="text-2xl font-bold text-sky-100 mb-2">Booking Confirmed!</h2>
          <p className="text-navy-400 mb-6">
            Your appointment has been scheduled. A confirmation has been sent to {booking.guest_email}.
          </p>
          <div className="p-4 bg-navy-800 rounded-lg border border-navy-700 text-left mb-6">
            <div className="text-sm text-navy-400 mb-2">Booking Details</div>
            <div className="text-sky-100 font-medium">{bookingPage.title}</div>
            <div className="text-sm text-navy-400 mt-2">
              {new Date(booking.start_time).toLocaleString()}
            </div>
            <div className="text-xs text-navy-500 mt-2">
              Booking ID: {booking.uuid}
            </div>
          </div>
        </div>
      </div>
    );
  }

  const days = getDaysInMonth(currentMonth);

  return (
    <div className="min-h-screen bg-navy-950 py-12 px-4">
      <div className="max-w-4xl mx-auto">
        <div className="card p-8">
          <div className="mb-8">
            <h1 className="text-3xl font-bold text-sky-100 mb-2">{bookingPage.title}</h1>
            {bookingPage.description && (
              <p className="text-navy-400">{bookingPage.description}</p>
            )}
            <div className="flex items-center gap-4 mt-4 text-sm text-navy-500">
              <div className="flex items-center gap-1">
                <ClockIcon className="w-4 h-4" />
                <span>{bookingPage.slot_duration} minutes</span>
              </div>
            </div>
          </div>

          {error && (
            <div className="p-4 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 mb-6">
              {error}
            </div>
          )}

          {/* Booking Form Modal */}
          {showBookingModal && selectedSlot && (
            <FormModalBuilder
              title={`Book: ${new Date(selectedSlot).toLocaleString()}`}
              fields={bookingFormFields}
              onSubmit={handleBookingSubmit}
              onCancel={() => {
                setShowBookingModal(false);
                setSelectedSlot(null);
              }}
              themeMode="dark"
              colors={WADDLES_COLORS}
            />
          )}

          <div className="grid md:grid-cols-2 gap-8">
            {/* Calendar */}
              <div>
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-lg font-semibold text-sky-100">Select a Date</h3>
                  <div className="flex items-center gap-2">
                    <button
                      aria-label="Previous month"
                      onClick={() => setCurrentMonth(new Date(currentMonth.getFullYear(), currentMonth.getMonth() - 1))}
                      className="p-1 hover:bg-navy-800 rounded"
                    >
                      <ChevronLeftIcon className="w-5 h-5 text-sky-400" />
                    </button>
                    <div className="text-sky-100 font-medium min-w-[140px] text-center">
                      {currentMonth.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })}
                    </div>
                    <button
                      aria-label="Next month"
                      onClick={() => setCurrentMonth(new Date(currentMonth.getFullYear(), currentMonth.getMonth() + 1))}
                      className="p-1 hover:bg-navy-800 rounded"
                    >
                      <ChevronRightIcon className="w-5 h-5 text-sky-400" />
                    </button>
                  </div>
                </div>

                <div className="grid grid-cols-7 gap-2">
                  {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((day) => (
                    <div key={day} className="text-center text-xs font-medium text-navy-500 py-2">
                      {day}
                    </div>
                  ))}
                  {days.map((day, index) => {
                    if (!day) {
                      return <div key={`empty-${index}`} />;
                    }
                    const dateStr = day.toISOString().split('T')[0];
                    const isSelected = dateStr === selectedDate;
                    const isSelectable = isDateSelectable(day);
                    return (
                      <button
                        key={dateStr}
                        onClick={() => isSelectable && setSelectedDate(dateStr)}
                        disabled={!isSelectable}
                        className={`aspect-square flex items-center justify-center rounded-lg text-sm transition-colors ${
                          isSelected
                            ? 'bg-sky-500 text-white font-semibold'
                            : isSelectable
                            ? 'bg-navy-800 text-sky-100 hover:bg-navy-700'
                            : 'bg-navy-900 text-navy-600 cursor-not-allowed'
                        }`}
                      >
                        {day.getDate()}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Time Slots */}
              <div>
                <h3 className="text-lg font-semibold text-sky-100 mb-4">Available Times</h3>
                {loadingSlots ? (
                  <div className="flex justify-center py-8">
                    <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-sky-400"></div>
                  </div>
                ) : availableSlots.length === 0 ? (
                  <div className="text-center py-8 text-navy-400">
                    <CalendarIcon className="w-12 h-12 mx-auto mb-2 text-navy-600" />
                    <p>No available slots for this date</p>
                  </div>
                ) : (
                  <div className="space-y-2 max-h-96 overflow-y-auto">
                    {availableSlots.map((slot) => (
                      <button
                        key={slot}
                        onClick={() => {
                          setSelectedSlot(slot);
                          setShowBookingModal(true);
                        }}
                        className="w-full p-3 bg-navy-800 hover:bg-sky-600 rounded-lg text-sky-100 transition-colors text-left"
                      >
                        {formatTime(slot)}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
        </div>
      </div>
    </div>
  );
}

export default BookingPagePublic;
