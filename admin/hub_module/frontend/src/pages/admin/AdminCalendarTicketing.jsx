/**
 * Admin Calendar Ticketing Page
 * Manage tickets for a specific event - view, create, cancel, transfer
 */
import { useState, useEffect, useMemo } from 'react';
import { useParams, useSearchParams, Link } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import {
  TicketIcon,
  PlusIcon,
  XMarkIcon,
  ArrowPathRoundedSquareIcon,
  MagnifyingGlassIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  QrCodeIcon,
  UserGroupIcon,
  CheckCircleIcon,
} from '@heroicons/react/24/outline';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

export default function AdminCalendarTicketing() {
  const { communityId, eventId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();

  // State
  const [tickets, setTickets] = useState([]);
  const [ticketTypes, setTicketTypes] = useState([]);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [eventInfo, setEventInfo] = useState(null);

  // Pagination & Filters
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [totalTickets, setTotalTickets] = useState(0);
  const [search, setSearch] = useState(searchParams.get('search') || '');
  const [statusFilter, setStatusFilter] = useState(searchParams.get('status') || '');
  const [checkedInFilter, setCheckedInFilter] = useState(searchParams.get('checked_in') || '');

  // Modal state
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showTransferModal, setShowTransferModal] = useState(false);
  const [showCancelModal, setShowCancelModal] = useState(false);
  const [selectedTicket, setSelectedTicket] = useState(null);
  const [actionLoading, setActionLoading] = useState(false);

  // Status colors
  const statusColors = {
    valid: { bg: 'bg-green-500/20', text: 'text-green-400', label: 'Valid' },
    checked_in: { bg: 'bg-sky-500/20', text: 'text-sky-400', label: 'Checked In' },
    cancelled: { bg: 'bg-red-500/20', text: 'text-red-400', label: 'Cancelled' },
    expired: { bg: 'bg-gray-500/20', text: 'text-gray-400', label: 'Expired' },
    refunded: { bg: 'bg-yellow-500/20', text: 'text-yellow-400', label: 'Refunded' },
    transferred: { bg: 'bg-purple-500/20', text: 'text-purple-400', label: 'Transferred' },
  };

  // Platform options for select fields
  const platformOptions = [
    { value: 'hub', label: 'Hub' },
    { value: 'discord', label: 'Discord' },
    { value: 'twitch', label: 'Twitch' },
    { value: 'slack', label: 'Slack' },
  ];

  // Fields for Create Ticket Modal
  const createTicketFields = useMemo(() => [
    {
      name: 'username',
      type: 'text',
      label: 'Username',
      required: true,
      placeholder: 'Enter username',
    },
    {
      name: 'platform',
      type: 'select',
      label: 'Platform',
      defaultValue: 'hub',
      options: platformOptions,
    },
    {
      name: 'platform_user_id',
      type: 'text',
      label: 'Platform User ID',
      required: true,
      placeholder: 'Enter platform user ID',
    },
    ...(ticketTypes.length > 0 ? [{
      name: 'ticket_type_id',
      type: 'select',
      label: 'Ticket Type',
      defaultValue: ticketTypes[0]?.id?.toString() || '',
      options: ticketTypes.map((type) => ({
        value: type.id.toString(),
        label: type.name,
      })),
    }] : []),
    {
      name: 'holder_name',
      type: 'text',
      label: 'Holder Name (optional)',
      placeholder: 'Enter holder name',
    },
    {
      name: 'holder_email',
      type: 'email',
      label: 'Holder Email (optional)',
      placeholder: 'Enter holder email',
    },
  ], [ticketTypes]);

  // Fields for Transfer Ticket Modal
  const transferTicketFields = useMemo(() => [
    {
      name: 'username',
      type: 'text',
      label: 'New Holder Username',
      required: true,
      placeholder: 'Enter username',
    },
    {
      name: 'platform',
      type: 'select',
      label: 'Platform',
      defaultValue: 'hub',
      options: platformOptions,
    },
    {
      name: 'platform_user_id',
      type: 'text',
      label: 'Platform User ID',
      required: true,
      placeholder: 'Enter platform user ID',
    },
    {
      name: 'holder_name',
      type: 'text',
      label: 'Holder Name (optional)',
      placeholder: 'Enter holder name',
    },
    {
      name: 'holder_email',
      type: 'email',
      label: 'Holder Email (optional)',
      placeholder: 'Enter holder email',
    },
    {
      name: 'notes',
      type: 'textarea',
      label: 'Transfer Notes (optional)',
      placeholder: 'Enter transfer notes',
      rows: 2,
    },
  ], []);

  // Fields for Cancel Ticket Modal
  const cancelTicketFields = useMemo(() => [
    {
      name: 'reason',
      type: 'textarea',
      label: 'Reason (optional)',
      placeholder: 'Enter cancellation reason...',
      rows: 3,
    },
  ], []);

  // Fetch data
  useEffect(() => {
    fetchData();
  }, [communityId, eventId, page, search, statusFilter, checkedInFilter]);

  const fetchData = async () => {
    try {
      setLoading(true);
      setError(null);

      const params = {
        limit: 25,
        offset: (page - 1) * 25,
      };
      if (search) params.search = search;
      if (statusFilter) params.status = statusFilter;
      if (checkedInFilter !== '') params.is_checked_in = checkedInFilter === 'true';

      const [ticketsRes, typesRes, statsRes] = await Promise.all([
        adminApi.getTickets(communityId, eventId, params),
        adminApi.getTicketTypes(communityId, eventId),
        adminApi.getAttendanceStats(communityId, eventId),
      ]);

      setTickets(ticketsRes.data.data?.tickets || ticketsRes.data.tickets || []);
      setTotalTickets(ticketsRes.data.data?.total || ticketsRes.data.total || 0);
      setTotalPages(Math.ceil((ticketsRes.data.data?.total || ticketsRes.data.total || 0) / 25));
      setTicketTypes(typesRes.data.data?.ticket_types || typesRes.data.ticket_types || []);
      setStats(statsRes.data.data || statsRes.data);
    } catch (err) {
      console.error('Failed to load ticket data:', err);
      setError(err.response?.data?.error || 'Failed to load tickets');
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = (e) => {
    e.preventDefault();
    setPage(1);
    setSearchParams({
      ...(search && { search }),
      ...(statusFilter && { status: statusFilter }),
      ...(checkedInFilter && { checked_in: checkedInFilter }),
    });
  };

  const handleCancelTicket = async (data) => {
    if (!selectedTicket) return;
    try {
      setActionLoading(true);
      await adminApi.cancelTicket(communityId, eventId, selectedTicket.id, { reason: data.reason || '' });
      setShowCancelModal(false);
      setSelectedTicket(null);
      fetchData();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to cancel ticket');
      throw err;
    } finally {
      setActionLoading(false);
    }
  };

  const handleTransferTicket = async (data) => {
    if (!selectedTicket) return;
    try {
      setActionLoading(true);
      await adminApi.transferTicket(communityId, eventId, selectedTicket.id, data);
      setShowTransferModal(false);
      setSelectedTicket(null);
      fetchData();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to transfer ticket');
      throw err;
    } finally {
      setActionLoading(false);
    }
  };

  const handleCreateTicket = async (data) => {
    try {
      setActionLoading(true);
      const ticketData = {
        ...data,
        ticket_type_id: data.ticket_type_id ? parseInt(data.ticket_type_id) : null,
      };
      await adminApi.createTicket(communityId, eventId, ticketData);
      setShowCreateModal(false);
      fetchData();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create ticket');
      throw err;
    } finally {
      setActionLoading(false);
    }
  };

  if (loading && !tickets.length) {
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
            <span>Event Tickets</span>
          </div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-3">
            <TicketIcon className="h-7 w-7 text-sky-400" />
            Ticket Management
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
          <Link
            to={`/admin/${communityId}/calendar/events/${eventId}/attendance`}
            className="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg flex items-center gap-2 transition-colors"
          >
            <UserGroupIcon className="h-5 w-5" />
            Attendance
          </Link>
          <button
            onClick={() => setShowCreateModal(true)}
            className="px-4 py-2 bg-sky-600 hover:bg-sky-700 text-white rounded-lg flex items-center gap-2 transition-colors"
          >
            <PlusIcon className="h-5 w-5" />
            Create Ticket
          </button>
        </div>
      </div>

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <div className="bg-navy-800 rounded-lg p-4 border border-navy-700">
            <div className="text-2xl font-bold text-white">{stats.total_tickets || 0}</div>
            <div className="text-sm text-gray-400">Total Tickets</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-navy-700">
            <div className="text-2xl font-bold text-green-400">{stats.checked_in || 0}</div>
            <div className="text-sm text-gray-400">Checked In</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-navy-700">
            <div className="text-2xl font-bold text-sky-400">{stats.not_checked_in || 0}</div>
            <div className="text-sm text-gray-400">Not Checked In</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-navy-700">
            <div className="text-2xl font-bold text-red-400">{stats.cancelled || 0}</div>
            <div className="text-sm text-gray-400">Cancelled</div>
          </div>
          <div className="bg-navy-800 rounded-lg p-4 border border-navy-700">
            <div className="text-2xl font-bold text-purple-400">
              {stats.check_in_rate ? `${stats.check_in_rate}%` : '0%'}
            </div>
            <div className="text-sm text-gray-400">Check-in Rate</div>
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

      {/* Search & Filters */}
      <form onSubmit={handleSearch} className="flex flex-wrap gap-4">
        <div className="flex-1 min-w-[200px]">
          <div className="relative">
            <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-5 w-5 text-gray-400" />
            <input
              aria-label="Search by name, username, or email"
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name, username, or email..."
              className="w-full pl-10 pr-4 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white placeholder-gray-400 focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
            />
          </div>
        </div>
        <select
          aria-label="Filter by statuses"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white"
        >
          <option value="">All Statuses</option>
          <option value="valid">Valid</option>
          <option value="checked_in">Checked In</option>
          <option value="cancelled">Cancelled</option>
          <option value="expired">Expired</option>
          <option value="refunded">Refunded</option>
        </select>
        <select
          aria-label="Filter by check-in status"
          value={checkedInFilter}
          onChange={(e) => setCheckedInFilter(e.target.value)}
          className="px-4 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white"
        >
          <option value="">All Check-in Status</option>
          <option value="true">Checked In</option>
          <option value="false">Not Checked In</option>
        </select>
        <button
          type="submit"
          className="px-4 py-2 bg-sky-600 hover:bg-sky-700 text-white rounded-lg transition-colors"
        >
          Search
        </button>
      </form>

      {/* Tickets Table */}
      <div className="bg-navy-800 rounded-lg border border-navy-700 overflow-hidden">
        <table className="w-full">
          <thead className="bg-navy-900">
            <tr>
              <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">#</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">Holder</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">Type</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">Status</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">Check-in</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">Created</th>
              <th className="px-4 py-3 text-right text-sm font-medium text-gray-400">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-navy-700">
            {tickets.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-gray-400">
                  No tickets found
                </td>
              </tr>
            ) : (
              tickets.map((ticket) => {
                const status = statusColors[ticket.status] || statusColors.valid;
                return (
                  <tr key={ticket.id} className="hover:bg-navy-700/50">
                    <td className="px-4 py-3 text-sm font-mono text-gray-300">
                      #{String(ticket.ticket_number).padStart(3, '0')}
                    </td>
                    <td className="px-4 py-3">
                      <div className="text-sm text-white">{ticket.holder_name || ticket.username}</div>
                      <div className="text-xs text-gray-400">{ticket.holder_email || `@${ticket.username}`}</div>
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-300">
                      {ticket.ticket_type_name || 'General'}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-1 rounded-full text-xs font-medium ${status.bg} ${status.text}`}>
                        {status.label}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {ticket.is_checked_in ? (
                        <div className="flex items-center gap-1 text-green-400">
                          <CheckCircleIcon className="h-4 w-4" />
                          <span className="text-xs">
                            {ticket.checked_in_at ? new Date(ticket.checked_in_at).toLocaleTimeString() : 'Yes'}
                          </span>
                        </div>
                      ) : (
                        <span className="text-gray-400 text-sm">-</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-400">
                      {ticket.created_at ? new Date(ticket.created_at).toLocaleDateString() : '-'}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        {ticket.status === 'valid' && !ticket.is_checked_in && (
                          <>
                            <button
                              onClick={() => {
                                setSelectedTicket(ticket);
                                setShowTransferModal(true);
                              }}
                              className="p-1.5 text-gray-400 hover:text-purple-400 hover:bg-purple-500/10 rounded transition-colors"
                              title="Transfer Ticket"
                            >
                              <ArrowPathRoundedSquareIcon className="h-5 w-5" />
                            </button>
                            <button
                              onClick={() => {
                                setSelectedTicket(ticket);
                                setShowCancelModal(true);
                              }}
                              className="p-1.5 text-gray-400 hover:text-red-400 hover:bg-red-500/10 rounded transition-colors"
                              title="Cancel Ticket"
                            >
                              <XMarkIcon className="h-5 w-5" />
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-navy-700">
            <div className="text-sm text-gray-400">
              Showing {(page - 1) * 25 + 1} - {Math.min(page * 25, totalTickets)} of {totalTickets}
            </div>
            <div className="flex items-center gap-2">
              <button
                aria-label="Previous page"
                onClick={() => setPage(p => Math.max(1, p - 1))}
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
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                className="p-2 text-gray-400 hover:text-white disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <ChevronRightIcon className="h-5 w-5" />
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Create Ticket Modal */}
      <FormModalBuilder
        title="Create Ticket"
        fields={createTicketFields}
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={handleCreateTicket}
        submitButtonText={actionLoading ? 'Creating...' : 'Create Ticket'}
        cancelButtonText="Cancel"
        width="md"
        themeMode="dark"
        colors={WADDLES_COLORS}
      />

      {/* Transfer Modal */}
      <FormModalBuilder
        title="Transfer Ticket"
        description={selectedTicket ? `Transfer ticket #${String(selectedTicket.ticket_number).padStart(3, '0')} from ${selectedTicket.holder_name || selectedTicket.username}` : ''}
        fields={transferTicketFields}
        isOpen={showTransferModal && !!selectedTicket}
        onClose={() => {
          setShowTransferModal(false);
          setSelectedTicket(null);
        }}
        onSubmit={handleTransferTicket}
        submitButtonText={actionLoading ? 'Transferring...' : 'Transfer'}
        cancelButtonText="Cancel"
        width="md"
        themeMode="dark"
        colors={{
          ...WADDLES_COLORS,
          primaryButton: 'bg-purple-600',
          primaryButtonHover: 'hover:bg-purple-700',
        }}
      />

      {/* Cancel Modal */}
      <FormModalBuilder
        title="Cancel Ticket"
        description={selectedTicket ? `Are you sure you want to cancel ticket #${String(selectedTicket.ticket_number).padStart(3, '0')} for ${selectedTicket.holder_name || selectedTicket.username}? This action cannot be undone.` : ''}
        fields={cancelTicketFields}
        isOpen={showCancelModal && !!selectedTicket}
        onClose={() => {
          setShowCancelModal(false);
          setSelectedTicket(null);
        }}
        onSubmit={handleCancelTicket}
        submitButtonText={actionLoading ? 'Cancelling...' : 'Cancel Ticket'}
        cancelButtonText="Keep Ticket"
        width="md"
        themeMode="dark"
        colors={{
          ...WADDLES_COLORS,
          primaryButton: 'bg-red-600',
          primaryButtonHover: 'hover:bg-red-700',
        }}
      />
    </div>
  );
}

