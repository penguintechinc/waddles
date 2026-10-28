/**
 * Admin Calendar Scanner Page
 * QR code scanning interface for event check-in with manual entry fallback
 */
import { useState, useEffect, useRef } from 'react';
import { useParams, Link } from 'react-router-dom';
import { adminApi } from '../../services/api';
import {
  QrCodeIcon,
  CheckCircleIcon,
  XCircleIcon,
  ExclamationTriangleIcon,
  ArrowPathIcon,
  TicketIcon,
  ClockIcon,
  UserIcon,
  KeyIcon,
} from '@heroicons/react/24/outline';

export default function AdminCalendarScanner() {
  const { communityId, eventId } = useParams();

  // State
  const [manualCode, setManualCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [lastResult, setLastResult] = useState(null);
  const [recentScans, setRecentScans] = useState([]);
  const [stats, setStats] = useState(null);
  const [scannerMode, setScannerMode] = useState('manual'); // 'manual' or 'camera'

  const inputRef = useRef(null);

  // Fetch initial stats
  useEffect(() => {
    fetchStats();
    // Focus input on load
    if (inputRef.current) {
      inputRef.current.focus();
    }
  }, [communityId, eventId]);

  const fetchStats = async () => {
    try {
      const response = await adminApi.getAttendanceStats(communityId, eventId);
      setStats(response.data.data || response.data);
    } catch (err) {
      console.error('Failed to fetch stats:', err);
    }
  };

  const handleVerify = async (code, performCheckin = true) => {
    if (!code || code.length !== 64) {
      setLastResult({
        success: false,
        result_code: 'invalid_format',
        message: 'Invalid ticket code format (must be 64 characters)',
      });
      return;
    }

    try {
      setLoading(true);
      const response = await adminApi.verifyTicket(code, performCheckin);
      const result = response.data.data || response.data;

      setLastResult(result);

      // Add to recent scans
      setRecentScans((prev) => [
        {
          ...result,
          timestamp: new Date().toISOString(),
          code: code.substring(0, 8) + '...',
        },
        ...prev.slice(0, 9),
      ]);

      // Refresh stats on successful check-in
      if (result.success && performCheckin) {
        fetchStats();
      }

      // Clear input
      setManualCode('');
    } catch (err) {
      setLastResult({
        success: false,
        result_code: 'error',
        message: err.response?.data?.error || 'Failed to verify ticket',
      });
    } finally {
      setLoading(false);
      // Refocus input for next scan
      if (inputRef.current) {
        inputRef.current.focus();
      }
    }
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    handleVerify(manualCode.trim());
  };

  // Handle paste (for QR scanners that type into input)
  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && manualCode.trim().length === 64) {
      handleVerify(manualCode.trim());
    }
  };

  // Result status styling
  const getResultStyle = (result) => {
    if (!result) return {};
    if (result.success) {
      return {
        bg: 'bg-green-500/20',
        border: 'border-green-500',
        icon: CheckCircleIcon,
        iconColor: 'text-green-400',
        text: 'text-green-400',
      };
    }
    if (result.result_code === 'already_checked_in') {
      return {
        bg: 'bg-yellow-500/20',
        border: 'border-yellow-500',
        icon: ExclamationTriangleIcon,
        iconColor: 'text-yellow-400',
        text: 'text-yellow-400',
      };
    }
    return {
      bg: 'bg-red-500/20',
      border: 'border-red-500',
      icon: XCircleIcon,
      iconColor: 'text-red-400',
      text: 'text-red-400',
    };
  };

  const resultStyle = getResultStyle(lastResult);

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm text-fg-muted mb-1">
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
            <span>Scanner</span>
          </div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-3">
            <QrCodeIcon className="h-7 w-7 text-purple-400" />
            Ticket Scanner
          </h1>
        </div>
        <Link
          to={`/admin/${communityId}/calendar/events/${eventId}/attendance`}
          className="px-4 py-2 bg-sky-600 hover:bg-sky-700 text-white rounded-lg transition-colors"
        >
          View Attendance
        </Link>
      </div>

      {/* Stats Bar */}
      {stats && (
        <div className="grid grid-cols-4 gap-4">
          <div className="bg-navy-800 rounded-lg p-4 border border-navy-700 text-center">
            <div className="text-3xl font-bold text-white">{stats.total_tickets || 0}</div>
            <div className="text-sm text-fg-muted">Total</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-navy-700 text-center">
            <div className="text-3xl font-bold text-green-400">{stats.checked_in || 0}</div>
            <div className="text-sm text-fg-muted">Checked In</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-navy-700 text-center">
            <div className="text-3xl font-bold text-sky-400">{stats.not_checked_in || 0}</div>
            <div className="text-sm text-fg-muted">Remaining</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-navy-700 text-center">
            <div className="text-3xl font-bold text-purple-400">
              {stats.check_in_rate ? `${stats.check_in_rate}%` : '0%'}
            </div>
            <div className="text-sm text-fg-muted">Rate</div>
          </div>
        </div>
      )}

      {/* Scanner Input */}
      <div className="bg-navy-800 rounded-lg p-6 border border-navy-700">
        <div className="flex items-center gap-4 mb-4">
          <button
            onClick={() => setScannerMode('manual')}
            className={`px-4 py-2 rounded-lg flex items-center gap-2 transition-colors ${
              scannerMode === 'manual'
                ? 'bg-purple-600 text-white'
                : 'bg-navy-700 text-fg-muted hover:text-white'
            }`}
          >
            <KeyIcon className="h-5 w-5" />
            Manual Entry
          </button>
          <button
            onClick={() => setScannerMode('camera')}
            className={`px-4 py-2 rounded-lg flex items-center gap-2 transition-colors ${
              scannerMode === 'camera'
                ? 'bg-purple-600 text-white'
                : 'bg-navy-700 text-fg-muted hover:text-white'
            }`}
          >
            <QrCodeIcon className="h-5 w-5" />
            Camera Scan
          </button>
        </div>

        {scannerMode === 'manual' ? (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label htmlFor="admin-calendar-scanner-ticket-code-64-characters" className="block text-sm font-medium text-fg-secondary mb-2">
                Ticket Code (64 characters)
              </label>
              <div className="flex gap-3">
                <input
                  id="admin-calendar-scanner-ticket-code-64-characters"
                  ref={inputRef}
                  type="text"
                  value={manualCode}
                  onChange={(e) => setManualCode(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder="Paste or scan the 64-character ticket code..."
                  maxLength={64}
                  className="flex-1 px-4 py-3 bg-navy-900 border border-navy-700 rounded-lg text-white font-mono text-sm placeholder-fg-muted focus:border-purple-500 focus:ring-1 focus:ring-purple-500"
                  autoComplete="off"
                  autoFocus
                />
                <button
                  type="submit"
                  disabled={loading || manualCode.length !== 64}
                  className="px-6 py-3 bg-purple-600 hover:bg-purple-700 disabled:bg-purple-600/50 disabled:cursor-not-allowed text-white rounded-lg font-medium transition-colors flex items-center gap-2"
                >
                  {loading ? (
                    <ArrowPathIcon className="h-5 w-5 animate-spin" />
                  ) : (
                    <CheckCircleIcon className="h-5 w-5" />
                  )}
                  Check In
                </button>
              </div>
              <div className="mt-2 text-sm text-fg-muted">
                {manualCode.length}/64 characters
                {manualCode.length === 64 && (
                  <span className="text-green-400 ml-2">Ready to scan</span>
                )}
              </div>
            </div>
          </form>
        ) : (
          <div className="text-center py-12">
            <QrCodeIcon className="h-16 w-16 text-fg-muted mx-auto mb-4" />
            <p className="text-fg-muted">
              Camera scanning requires a secure context (HTTPS).
              <br />
              For now, use a QR scanner app that types the code into the manual entry field.
            </p>
          </div>
        )}
      </div>

      {/* Last Result */}
      {lastResult && (
        <div
          className={`rounded-lg p-6 border-2 ${resultStyle.bg} ${resultStyle.border} transition-all`}
        >
          <div className="flex items-start gap-4">
            <resultStyle.icon className={`h-12 w-12 ${resultStyle.iconColor} flex-shrink-0`} />
            <div className="flex-1">
              <div className={`text-xl font-bold ${resultStyle.text}`}>
                {lastResult.success ? 'Check-in Successful!' : lastResult.message}
              </div>
              {lastResult.ticket && (
                <div className="mt-3 space-y-2">
                  <div className="flex items-center gap-2 text-white">
                    <TicketIcon className="h-5 w-5 text-fg-muted" />
                    <span>
                      Ticket #{String(lastResult.ticket.ticket_number).padStart(3, '0')}
                    </span>
                    {lastResult.ticket.ticket_type_name && (
                      <span className="px-2 py-0.5 bg-navy-700 rounded text-sm">
                        {lastResult.ticket.ticket_type_name}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2 text-fg-secondary">
                    <UserIcon className="h-5 w-5 text-fg-muted" />
                    <span>{lastResult.ticket.holder_name || lastResult.ticket.username}</span>
                  </div>
                  {lastResult.ticket.checked_in_at && (
                    <div className="flex items-center gap-2 text-fg-muted text-sm">
                      <ClockIcon className="h-4 w-4" />
                      <span>
                        Checked in at {new Date(lastResult.ticket.checked_in_at).toLocaleTimeString()}
                      </span>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Recent Scans */}
      {recentScans.length > 0 && (
        <div className="bg-navy-800 rounded-lg border border-navy-700">
          <div className="px-4 py-3 border-b border-navy-700">
            <h3 className="text-lg font-medium text-white">Recent Scans</h3>
          </div>
          <div className="divide-y divide-navy-700">
            {recentScans.map((scan, index) => {
              const style = getResultStyle(scan);
              return (
                <div
                  key={index}
                  className={`px-4 py-3 flex items-center justify-between ${
                    index === 0 ? 'bg-navy-700/30' : ''
                  }`}
                >
                  <div className="flex items-center gap-3">
                    <style.icon className={`h-5 w-5 ${style.iconColor}`} />
                    <div>
                      <div className="text-sm text-white">
                        {scan.ticket?.holder_name || scan.ticket?.username || 'Unknown'}
                      </div>
                      <div className="text-xs text-fg-muted">
                        {scan.code} • {new Date(scan.timestamp).toLocaleTimeString()}
                      </div>
                    </div>
                  </div>
                  <span className={`text-sm ${style.text}`}>
                    {scan.success
                      ? 'Checked In'
                      : scan.result_code === 'already_checked_in'
                        ? 'Already In'
                        : 'Failed'}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Instructions */}
      <div className="bg-navy-800/50 rounded-lg p-4 border border-navy-700">
        <h4 className="text-sm font-medium text-fg-secondary mb-2">How to use:</h4>
        <ul className="text-sm text-fg-muted space-y-1 list-disc list-inside">
          <li>Use a QR scanner app or handheld scanner that outputs text</li>
          <li>Point the scanner at the ticket QR code</li>
          <li>The 64-character code will be automatically entered</li>
          <li>Press Enter or click &quot;Check In&quot; to verify</li>
          <li>Green = success, Yellow = already checked in, Red = invalid</li>
        </ul>
      </div>
    </div>
  );
}
