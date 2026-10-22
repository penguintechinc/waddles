/**
 * Admin Calendar Attendance Page
 * View attendance stats, check-in history, and export attendance data
 */
import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { adminApi } from '../../services/api';
import {
  UserGroupIcon,
  ClockIcon,
  CheckCircleIcon,
  XCircleIcon,
  ArrowDownTrayIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  QrCodeIcon,
  TicketIcon,
  FunnelIcon,
} from '@heroicons/react/24/outline';

export default function AdminCalendarAttendance() {
  const { communityId, eventId } = useParams();

  // State
  const [stats, setStats] = useState(null);
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Pagination
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [totalLogs, setTotalLogs] = useState(0);

  // Filters
  const [successOnly, setSuccessOnly] = useState(false);

  // Export state
  const [exporting, setExporting] = useState(false);

  // Fetch data
  useEffect(() => {
    fetchData();
  }, [communityId, eventId, page, successOnly]);

  const fetchData = async () => {
    try {
      setLoading(true);
      setError(null);

      const [statsRes, logsRes] = await Promise.all([
        adminApi.getAttendanceStats(communityId, eventId),
        adminApi.getCheckInLog(communityId, eventId, {
          limit: 25,
          offset: (page - 1) * 25,
          success_only: successOnly,
        }),
      ]);

      setStats(statsRes.data.data || statsRes.data);
      const logData = logsRes.data.data || logsRes.data;
      setLogs(logData.logs || []);
      setTotalLogs(logData.total || 0);
      setTotalPages(Math.ceil((logData.total || 0) / 25));
    } catch (err) {
      console.error('Failed to load attendance data:', err);
      setError(err.response?.data?.error || 'Failed to load attendance data');
    } finally {
      setLoading(false);
    }
  };

  const handleExport = async (format) => {
    try {
      setExporting(true);
      const response = await adminApi.exportAttendance(communityId, eventId, format);

      if (format === 'csv') {
        // Download CSV file
        const blob = new Blob([response.data], { type: 'text/csv' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `attendance-event-${eventId}.csv`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
      } else {
        // Download JSON file
        const blob = new Blob([JSON.stringify(response.data, null, 2)], {
          type: 'application/json',
        });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `attendance-event-${eventId}.json`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
      }
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to export attendance');
    } finally {
      setExporting(false);
    }
  };

  // Result styling for log entries
  const getLogStyle = (log) => {
    if (log.success) {
      if (log.action === 'undo_check_in') {
        return {
          bg: 'bg-yellow-500/10',
          icon: ClockIcon,
          iconColor: 'text-yellow-400',
        };
      }
      return {
        bg: 'bg-green-500/10',
        icon: CheckCircleIcon,
        iconColor: 'text-green-400',
      };
    }
    return {
      bg: 'bg-red-500/10',
      icon: XCircleIcon,
      iconColor: 'text-red-400',
    };
  };

  if (loading && !stats) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin h-8 w-8 border-2 border-sky-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm text-gray-400 mb-1">
            <Link to={`/admin/${communityId}/calendar`} className="hover:text-sky-400">
              Calendar
            </Link>
            <span>/</span>
            <Link
              to={`/admin/${communityId}/calendar/events/${eventId}/tickets`}
              className="hover:text-sky-400"
            >
              Tickets
            </Link>
            <span>/</span>
            <span>Attendance</span>
          </div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-3">
            <UserGroupIcon className="h-7 w-7 text-green-400" />
            Attendance Report
          </h1>
        </div>
        <div className="flex items-center gap-3">
          <Link
            to={`/admin/${communityId}/calendar/events/${eventId}/scanner`}
            className="px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg flex items-center gap-2 transition-colors"
          >
            <QrCodeIcon className="h-5 w-5" />
            Scanner
          </Link>
          <div className="relative group">
            <button
              disabled={exporting}
              className="px-4 py-2 bg-sky-600 hover:bg-sky-700 text-white rounded-lg flex items-center gap-2 transition-colors disabled:opacity-50"
            >
              <ArrowDownTrayIcon className="h-5 w-5" />
              {exporting ? 'Exporting...' : 'Export'}
            </button>
            <div className="absolute right-0 mt-2 w-40 bg-navy-800 border border-navy-700 rounded-lg shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-10">
              <button
                onClick={() => handleExport('csv')}
                className="w-full px-4 py-2 text-left text-gray-300 hover:bg-navy-700 rounded-t-lg"
              >
                Export as CSV
              </button>
              <button
                onClick={() => handleExport('json')}
                className="w-full px-4 py-2 text-left text-gray-300 hover:bg-navy-700 rounded-b-lg"
              >
                Export as JSON
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Stats Overview */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-6 gap-4">
          <div className="bg-navy-800 rounded-lg p-4 border border-navy-700">
            <div className="flex items-center gap-2 text-gray-400 text-sm mb-1">
              <TicketIcon className="h-4 w-4" />
              Total Tickets
            </div>
            <div className="text-2xl font-bold text-white">{stats.total_tickets || 0}</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-green-500/30">
            <div className="flex items-center gap-2 text-green-400 text-sm mb-1">
              <CheckCircleIcon className="h-4 w-4" />
              Checked In
            </div>
            <div className="text-2xl font-bold text-green-400">{stats.checked_in || 0}</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-sky-500/30">
            <div className="flex items-center gap-2 text-sky-400 text-sm mb-1">
              <ClockIcon className="h-4 w-4" />
              Not Checked In
            </div>
            <div className="text-2xl font-bold text-sky-400">{stats.not_checked_in || 0}</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-red-500/30">
            <div className="flex items-center gap-2 text-red-400 text-sm mb-1">
              <XCircleIcon className="h-4 w-4" />
              Cancelled
            </div>
            <div className="text-2xl font-bold text-red-400">{stats.cancelled || 0}</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-yellow-500/30">
            <div className="flex items-center gap-2 text-yellow-400 text-sm mb-1">
              Refunded
            </div>
            <div className="text-2xl font-bold text-yellow-400">{stats.refunded || 0}</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-purple-500/30">
            <div className="flex items-center gap-2 text-purple-400 text-sm mb-1">
              Check-in Rate
            </div>
            <div className="text-2xl font-bold text-purple-400">
              {stats.check_in_rate ? `${stats.check_in_rate}%` : '0%'}
            </div>
          </div>
        </div>
      )}

      {/* Progress Bar */}
      {stats && stats.total_tickets > 0 && (
        <div className="bg-navy-800 rounded-lg p-4 border border-navy-700">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-gray-400">Check-in Progress</span>
            <span className="text-sm text-white">
              {stats.checked_in || 0} / {stats.total_tickets || 0}
            </span>
          </div>
          <div className="h-4 bg-navy-900 rounded-full overflow-hidden">
            <div
              className="h-full bg-gradient-to-r from-green-600 to-green-400 transition-all duration-500"
              style={{ width: `${stats.check_in_rate || 0}%` }}
            />
          </div>
        </div>
      )}

      {/* Error Display */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 text-red-400">
          {error}
          <button onClick={() => setError(null)} className="ml-2 underline">
            Dismiss
          </button>
        </div>
      )}

      {/* Check-in Log */}
      <div className="bg-navy-800 rounded-lg border border-navy-700">
        <div className="px-4 py-3 border-b border-navy-700 flex items-center justify-between">
          <h3 className="text-lg font-medium text-white">Check-in Activity Log</h3>
          <label className="flex items-center gap-2 text-sm text-gray-400 cursor-pointer">
            <input
              type="checkbox"
              checked={successOnly}
              onChange={(e) => {
                setSuccessOnly(e.target.checked);
                setPage(1);
              }}
              className="rounded border-navy-600 bg-navy-900 text-sky-500 focus:ring-sky-500"
            />
            <FunnelIcon className="h-4 w-4" />
            Success only
          </label>
        </div>

        <div className="divide-y divide-navy-700">
          {logs.length === 0 ? (
            <div className="px-4 py-8 text-center text-gray-400">No check-in activity yet</div>
          ) : (
            logs.map((log) => {
              const style = getLogStyle(log);
              return (
                <div key={log.id} className={`px-4 py-3 ${style.bg}`}>
                  <div className="flex items-start justify-between">
                    <div className="flex items-start gap-3">
                      <style.icon className={`h-5 w-5 ${style.iconColor} mt-0.5`} />
                      <div>
                        <div className="text-sm text-white">
                          {log.holder_username || 'Unknown user'}
                          {log.ticket_type_name && (
                            <span className="ml-2 px-2 py-0.5 bg-navy-700 rounded text-xs text-gray-300">
                              {log.ticket_type_name}
                            </span>
                          )}
                        </div>
                        <div className="text-xs text-gray-400 mt-1">
                          {log.action === 'check_in' && 'Checked in'}
                          {log.action === 'undo_check_in' && 'Check-in undone'}
                          {log.action === 'self_check_in' && 'Self check-in'}
                          {log.action === 'auto_check_in' && 'Auto check-in'}
                          {log.action === 'rejected' && `Rejected: ${log.failure_reason}`}
                          {' via '}
                          {log.scan_method === 'qr_scan' && 'QR scan'}
                          {log.scan_method === 'manual_entry' && 'manual entry'}
                          {log.scan_method === 'api' && 'API'}
                          {log.scan_method === 'self_checkin' && 'self service'}
                          {log.scan_method === 'auto_checkin' && 'auto'}
                        </div>
                        {log.operator_username && (
                          <div className="text-xs text-gray-500 mt-0.5">
                            by {log.operator_username}
                            {log.location && ` at ${log.location}`}
                          </div>
                        )}
                      </div>
                    </div>
                    <div className="text-xs text-gray-400">
                      {log.created_at ? new Date(log.created_at).toLocaleString() : '-'}
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-navy-700">
            <div className="text-sm text-gray-400">
              Showing {(page - 1) * 25 + 1} - {Math.min(page * 25, totalLogs)} of {totalLogs}
            </div>
            <div className="flex items-center gap-2">
              <button
                aria-label="Previous page"
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="p-2 text-gray-400 hover:text-white disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <ChevronLeftIcon className="h-5 w-5" />
              </button>
              <span className="text-sm text-gray-400">
                Page {page} of {totalPages}
              </span>
              <button
                aria-label="Next page"
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                className="p-2 text-gray-400 hover:text-white disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <ChevronRightIcon className="h-5 w-5" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
