import { useState, useEffect } from 'react';
import {
  CalendarIcon,
  ClockIcon,
  GlobeAltIcon,
  LinkIcon,
  CheckCircleIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { calendarApi } from '../../services/api';

const TIMEZONES = [
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'America/Phoenix',
  'America/Anchorage',
  'Pacific/Honolulu',
  'Europe/London',
  'Europe/Paris',
  'Europe/Berlin',
  'Asia/Tokyo',
  'Asia/Shanghai',
  'Australia/Sydney',
  'UTC',
];

const DAYS_OF_WEEK = [
  { key: 'monday', label: 'Monday' },
  { key: 'tuesday', label: 'Tuesday' },
  { key: 'wednesday', label: 'Wednesday' },
  { key: 'thursday', label: 'Thursday' },
  { key: 'friday', label: 'Friday' },
  { key: 'saturday', label: 'Saturday' },
  { key: 'sunday', label: 'Sunday' },
];

const VISIBILITY_OPTIONS = [
  { value: 'hidden', label: 'Hidden' },
  { value: 'free_busy', label: 'Free/Busy Only' },
  { value: 'full', label: 'Full Details' },
];

function CalendarSettings() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [syncing, setSyncing] = useState({});

  const [connectedCalendars, setConnectedCalendars] = useState([]);
  const [availabilitySettings, setAvailabilitySettings] = useState({
    timezone: 'America/New_York',
    slot_durations: [30],
    min_notice_minutes: 120,
    max_future_days: 30,
    buffer_minutes: 0,
    visibility_public: 'hidden',
    visibility_registered: 'free_busy',
    visibility_community: 'full',
  });
  const [weeklyAvailability, setWeeklyAvailability] = useState({
    monday: { enabled: true, start_time: '09:00', end_time: '17:00' },
    tuesday: { enabled: true, start_time: '09:00', end_time: '17:00' },
    wednesday: { enabled: true, start_time: '09:00', end_time: '17:00' },
    thursday: { enabled: true, start_time: '09:00', end_time: '17:00' },
    friday: { enabled: true, start_time: '09:00', end_time: '17:00' },
    saturday: { enabled: false, start_time: '09:00', end_time: '17:00' },
    sunday: { enabled: false, start_time: '09:00', end_time: '17:00' },
  });

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      setLoading(true);
      const [calendarsRes, settingsRes, weeklyRes] = await Promise.all([
        calendarApi.getConnectedCalendars().catch(() => ({ data: { calendars: [] } })),
        calendarApi.getAvailabilitySettings().catch(() => ({ data: { settings: {} } })),
        calendarApi.getWeeklyAvailability().catch(() => ({ data: { availability: {} } })),
      ]);
      setConnectedCalendars(calendarsRes.data.calendars || []);
      if (settingsRes.data.settings) {
        setAvailabilitySettings({ ...availabilitySettings, ...settingsRes.data.settings });
      }
      if (weeklyRes.data.availability) {
        setWeeklyAvailability({ ...weeklyAvailability, ...weeklyRes.data.availability });
      }
    } catch (err) {
      setError('Failed to load settings');
    } finally {
      setLoading(false);
    }
  };

  const handleConnectGoogle = async () => {
    try {
      const response = await calendarApi.getGoogleAuthUrl();
      if (response.data.auth_url) {
        window.location.href = response.data.auth_url;
      }
    } catch (err) {
      setError('Failed to connect Google Calendar');
    }
  };

  const handleConnectMicrosoft = async () => {
    try {
      const response = await calendarApi.getMicrosoftAuthUrl();
      if (response.data.auth_url) {
        window.location.href = response.data.auth_url;
      }
    } catch (err) {
      setError('Failed to connect Microsoft Calendar');
    }
  };

  const handleSync = async (calendarId) => {
    try {
      setSyncing({ ...syncing, [calendarId]: true });
      await calendarApi.syncCalendar(calendarId);
      setSuccess('Calendar synced successfully');
      loadData();
    } catch (err) {
      setError('Failed to sync calendar');
    } finally {
      setSyncing({ ...syncing, [calendarId]: false });
    }
  };

  const handleDisconnect = async (calendarId) => {
    if (!window.confirm('Disconnect this calendar?')) return;
    try {
      await calendarApi.disconnectCalendar(calendarId);
      setSuccess('Calendar disconnected');
      loadData();
    } catch (err) {
      setError('Failed to disconnect calendar');
    }
  };

  const handleSaveSettings = async () => {
    try {
      setSaving(true);
      setError(null);
      await calendarApi.updateAvailabilitySettings(availabilitySettings);
      await calendarApi.updateWeeklyAvailability(weeklyAvailability);
      setSuccess('Settings saved successfully');
    } catch (err) {
      setError('Failed to save settings');
    } finally {
      setSaving(false);
    }
  };

  const toggleSlotDuration = (duration) => {
    const durations = availabilitySettings.slot_durations || [];
    if (durations.includes(duration)) {
      setAvailabilitySettings({
        ...availabilitySettings,
        slot_durations: durations.filter((d) => d !== duration),
      });
    } else {
      setAvailabilitySettings({
        ...availabilitySettings,
        slot_durations: [...durations, duration],
      });
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-sky-400"></div>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold text-sky-100 mb-6">Calendar Settings</h1>

      {error && (
        <div className="p-4 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 mb-6">
          {error}
        </div>
      )}

      {success && (
        <div className="p-4 bg-emerald-500/20 border border-emerald-500/30 rounded-lg text-emerald-300 mb-6 flex items-center gap-2">
          <CheckCircleIcon className="w-5 h-5" />
          {success}
        </div>
      )}

      {/* Connected Calendars */}
      <div className="card p-6 mb-6">
        <div className="flex items-center gap-3 mb-6">
          <LinkIcon className="w-6 h-6 text-gold-400" />
          <h2 className="text-xl font-semibold text-sky-100">Connected Calendars</h2>
        </div>

        {connectedCalendars.length > 0 && (
          <div className="space-y-3 mb-4">
            {connectedCalendars.map((cal) => (
              <div key={cal.id} className="flex items-center justify-between p-4 bg-navy-800 rounded-lg border border-navy-600">
                <div>
                  <div className="font-medium text-sky-100">{cal.provider === 'google' ? 'Google Calendar' : 'Microsoft Calendar'}</div>
                  <div className="text-sm text-navy-400">
                    Last synced: {cal.last_sync ? new Date(cal.last_sync).toLocaleString() : 'Never'}
                  </div>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => handleSync(cal.id)}
                    disabled={syncing[cal.id]}
                    className="btn btn-secondary text-sm py-1 px-3 flex items-center gap-1"
                  >
                    <ArrowPathIcon className={`w-4 h-4 ${syncing[cal.id] ? 'animate-spin' : ''}`} />
                    Sync
                  </button>
                  <button
                    onClick={() => handleDisconnect(cal.id)}
                    className="btn btn-secondary text-sm py-1 px-3"
                  >
                    Disconnect
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        <div className="flex gap-3">
          <button onClick={handleConnectGoogle} className="btn btn-primary">
            Connect Google Calendar
          </button>
          <button onClick={handleConnectMicrosoft} className="btn btn-secondary">
            Connect Microsoft Calendar
          </button>
        </div>
      </div>

      {/* Availability Settings */}
      <div className="card p-6 mb-6">
        <div className="flex items-center gap-3 mb-6">
          <ClockIcon className="w-6 h-6 text-gold-400" />
          <h2 className="text-xl font-semibold text-sky-100">Availability Settings</h2>
        </div>

        <div className="space-y-4">
          <div>
            <label htmlFor="calendar-settings-timezone" className="block text-sm font-medium text-sky-200 mb-2">Timezone</label>
            <select
              id="calendar-settings-timezone"
              value={availabilitySettings.timezone}
              onChange={(e) => setAvailabilitySettings({ ...availabilitySettings, timezone: e.target.value })}
              className="input"
            >
              {TIMEZONES.map((tz) => (
                <option key={tz} value={tz}>{tz}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-sky-200 mb-2">Slot Durations</label>
            <div className="flex gap-3">
              {[15, 30, 60].map((duration) => (
                <label key={duration} className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={(availabilitySettings.slot_durations || []).includes(duration)}
                    onChange={() => toggleSlotDuration(duration)}
                    className="w-4 h-4 text-sky-500 bg-navy-700 border-navy-600 rounded focus:ring-sky-500"
                  />
                  <span className="text-sky-100">{duration} min</span>
                </label>
              ))}
            </div>
          </div>

          <div>
            <label htmlFor="calendar-settings-minimum-notice" className="block text-sm font-medium text-sky-200 mb-2">Minimum Notice</label>
            <select
              id="calendar-settings-minimum-notice"
              value={availabilitySettings.min_notice_minutes}
              onChange={(e) => setAvailabilitySettings({ ...availabilitySettings, min_notice_minutes: parseInt(e.target.value) })}
              className="input"
            >
              <option value={60}>1 hour</option>
              <option value={120}>2 hours</option>
              <option value={240}>4 hours</option>
              <option value={480}>8 hours</option>
              <option value={1440}>24 hours</option>
              <option value={2880}>48 hours</option>
            </select>
          </div>

          <div>
            <label htmlFor="calendar-settings-max-future-booking" className="block text-sm font-medium text-sky-200 mb-2">Max Future Booking</label>
            <select
              id="calendar-settings-max-future-booking"
              value={availabilitySettings.max_future_days}
              onChange={(e) => setAvailabilitySettings({ ...availabilitySettings, max_future_days: parseInt(e.target.value) })}
              className="input"
            >
              <option value={7}>7 days</option>
              <option value={14}>14 days</option>
              <option value={30}>30 days</option>
              <option value={60}>60 days</option>
              <option value={90}>90 days</option>
            </select>
          </div>

          <div>
            <label htmlFor="calendar-settings-buffer-between-appointments" className="block text-sm font-medium text-sky-200 mb-2">Buffer Between Appointments</label>
            <select
              id="calendar-settings-buffer-between-appointments"
              value={availabilitySettings.buffer_minutes}
              onChange={(e) => setAvailabilitySettings({ ...availabilitySettings, buffer_minutes: parseInt(e.target.value) })}
              className="input"
            >
              <option value={0}>No buffer</option>
              <option value={5}>5 minutes</option>
              <option value={10}>10 minutes</option>
              <option value={15}>15 minutes</option>
              <option value={30}>30 minutes</option>
            </select>
          </div>
        </div>
      </div>

      {/* Weekly Availability */}
      <div className="card p-6 mb-6">
        <div className="flex items-center gap-3 mb-6">
          <CalendarIcon className="w-6 h-6 text-gold-400" />
          <h2 className="text-xl font-semibold text-sky-100">Weekly Availability</h2>
        </div>

        <div className="space-y-3">
          {DAYS_OF_WEEK.map((day) => (
            <div key={day.key} className="flex items-center gap-4">
              <label className="flex items-center gap-2 w-32 cursor-pointer">
                <input
                  type="checkbox"
                  checked={weeklyAvailability[day.key]?.enabled || false}
                  onChange={(e) => setWeeklyAvailability({
                    ...weeklyAvailability,
                    [day.key]: { ...weeklyAvailability[day.key], enabled: e.target.checked },
                  })}
                  className="w-4 h-4 text-sky-500 bg-navy-700 border-navy-600 rounded focus:ring-sky-500"
                />
                <span className="text-sky-100">{day.label}</span>
              </label>
              {weeklyAvailability[day.key]?.enabled && (
                <div className="flex items-center gap-2 flex-1">
                  <input
                    aria-label={`${day.label} start time`}
                    type="time"
                    value={weeklyAvailability[day.key]?.start_time || '09:00'}
                    onChange={(e) => setWeeklyAvailability({
                      ...weeklyAvailability,
                      [day.key]: { ...weeklyAvailability[day.key], start_time: e.target.value },
                    })}
                    className="input"
                  />
                  <span className="text-navy-400">to</span>
                  <input
                    aria-label={`${day.label} end time`}
                    type="time"
                    value={weeklyAvailability[day.key]?.end_time || '17:00'}
                    onChange={(e) => setWeeklyAvailability({
                      ...weeklyAvailability,
                      [day.key]: { ...weeklyAvailability[day.key], end_time: e.target.value },
                    })}
                    className="input"
                  />
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Visibility Settings */}
      <div className="card p-6 mb-6">
        <div className="flex items-center gap-3 mb-6">
          <GlobeAltIcon className="w-6 h-6 text-gold-400" />
          <h2 className="text-xl font-semibold text-sky-100">Visibility Settings</h2>
        </div>

        <div className="space-y-4">
          <div>
            <label htmlFor="calendar-settings-public-visibility" className="block text-sm font-medium text-sky-200 mb-2">Public Visibility</label>
            <select
              id="calendar-settings-public-visibility"
              value={availabilitySettings.visibility_public}
              onChange={(e) => setAvailabilitySettings({ ...availabilitySettings, visibility_public: e.target.value })}
              className="input"
            >
              {VISIBILITY_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="calendar-settings-registered-users" className="block text-sm font-medium text-sky-200 mb-2">Registered Users</label>
            <select
              id="calendar-settings-registered-users"
              value={availabilitySettings.visibility_registered}
              onChange={(e) => setAvailabilitySettings({ ...availabilitySettings, visibility_registered: e.target.value })}
              className="input"
            >
              {VISIBILITY_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="calendar-settings-community-members" className="block text-sm font-medium text-sky-200 mb-2">Community Members</label>
            <select
              id="calendar-settings-community-members"
              value={availabilitySettings.visibility_community}
              onChange={(e) => setAvailabilitySettings({ ...availabilitySettings, visibility_community: e.target.value })}
              className="input"
            >
              {VISIBILITY_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
        </div>
      </div>

      <div className="flex justify-end">
        <button onClick={handleSaveSettings} disabled={saving} className="btn btn-primary">
          {saving ? 'Saving...' : 'Save Settings'}
        </button>
      </div>
    </div>
  );
}

export default CalendarSettings;
