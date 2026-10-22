import { useState, useEffect, useMemo } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  TicketIcon, PlusIcon, FunnelIcon, MagnifyingGlassIcon,
  TagIcon, Cog6ToothIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

const STATUS_OPTIONS = ['open', 'in_progress', 'waiting', 'resolved', 'closed'];
const PRIORITY_OPTIONS = ['low', 'medium', 'high', 'urgent'];

const STATUS_COLORS = {
  open: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  in_progress: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
  waiting: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  resolved: 'bg-green-500/20 text-green-300 border-green-500/30',
  closed: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
};

const PRIORITY_COLORS = {
  urgent: 'bg-red-500/20 text-red-300 border-red-500/30',
  high: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  medium: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  low: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
};

function AdminSupportDashboard() {
  const { communityId } = useParams();
  const navigate = useNavigate();
  const [tickets, setTickets] = useState([]);
  const [stats, setStats] = useState({ open: 0, in_progress: 0, waiting: 0, resolved_today: 0 });
  const [categories, setCategories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filterStatus, setFilterStatus] = useState('');
  const [filterPriority, setFilterPriority] = useState('');
  const [search, setSearch] = useState('');
  const [showCategoryModal, setShowCategoryModal] = useState(false);
  const [categoryForm, setCategoryForm] = useState({ name: '', description: '', sort_order: 0 });

  useEffect(() => {
    loadData();
  }, [communityId, filterStatus, filterPriority]);

  const loadData = async () => {
    try {
      setLoading(true);
      const params = {};
      if (filterStatus) params.status = filterStatus;
      if (filterPriority) params.priority = filterPriority;

      const [ticketsRes, statsRes, catsRes] = await Promise.all([
        adminApi.getSupportTickets(communityId, params),
        adminApi.getSupportStats(communityId),
        adminApi.getSupportCategories(communityId),
      ]);
      setTickets(ticketsRes.data?.tickets || []);
      setStats(statsRes.data?.stats || { open: 0, in_progress: 0, waiting: 0, resolved_today: 0 });
      setCategories(catsRes.data?.categories || []);
    } catch (err) {
      console.error('Failed to load support data:', err);
    } finally {
      setLoading(false);
    }
  };

  const filteredTickets = useMemo(() => {
    if (!search) return tickets;
    const q = search.toLowerCase();
    return tickets.filter(t =>
      t.subject?.toLowerCase().includes(q) ||
      t.ticket_number?.toLowerCase().includes(q) ||
      t.reporter_name?.toLowerCase().includes(q)
    );
  }, [tickets, search]);

  const handleCreateCategory = async () => {
    try {
      await adminApi.createSupportCategory(communityId, categoryForm);
      setCategoryForm({ name: '', description: '', sort_order: 0 });
      setShowCategoryModal(false);
      loadData();
    } catch (err) {
      console.error('Failed to create category:', err);
    }
  };

  const statCards = [
    { label: 'Open', value: stats.open, color: 'border-blue-500', textColor: 'text-blue-400', bgColor: 'bg-blue-500/10' },
    { label: 'In Progress', value: stats.in_progress, color: 'border-yellow-500', textColor: 'text-yellow-400', bgColor: 'bg-yellow-500/10' },
    { label: 'Waiting', value: stats.waiting, color: 'border-orange-500', textColor: 'text-orange-400', bgColor: 'bg-orange-500/10' },
    { label: 'Resolved Today', value: stats.resolved_today, color: 'border-green-500', textColor: 'text-green-400', bgColor: 'bg-green-500/10' },
  ];

  const categoryFields = [
    { name: 'name', label: 'Category Name', type: 'text', required: true },
    { name: 'description', label: 'Description', type: 'textarea' },
    { name: 'sort_order', label: 'Sort Order', type: 'number' },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <TicketIcon className="w-8 h-8 text-gold-400" />
          <h1 className="text-2xl font-bold text-sky-100">Support Tickets</h1>
        </div>
        <button
          onClick={() => setShowCategoryModal(true)}
          className="flex items-center gap-2 px-4 py-2 bg-navy-800 border border-navy-600 text-sky-200 rounded-lg hover:bg-navy-700 transition-colors"
        >
          <Cog6ToothIcon className="w-4 h-4" />
          Manage Categories
        </button>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {statCards.map((card) => (
          <div
            key={card.label}
            className={`p-4 rounded-lg border-l-4 ${card.color} ${card.bgColor} bg-navy-900`}
          >
            <p className="text-sm text-navy-400">{card.label}</p>
            <p className={`text-3xl font-bold ${card.textColor}`}>{card.value}</p>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3 bg-navy-900 border border-navy-700 rounded-lg p-4">
        <FunnelIcon className="w-5 h-5 text-navy-400" />
        <select
          aria-label="Filter by statuses"
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value)}
          className="bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm"
        >
          <option value="">All Statuses</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>{s.replace('_', ' ').replace(/\b\w/g, c => c.toUpperCase())}</option>
          ))}
        </select>
        <select
          aria-label="Filter by priorities"
          value={filterPriority}
          onChange={(e) => setFilterPriority(e.target.value)}
          className="bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm"
        >
          <option value="">All Priorities</option>
          {PRIORITY_OPTIONS.map((p) => (
            <option key={p} value={p}>{p.charAt(0).toUpperCase() + p.slice(1)}</option>
          ))}
        </select>
        <div className="flex items-center gap-2 flex-1 min-w-[200px]">
          <MagnifyingGlassIcon className="w-4 h-4 text-navy-400" />
          <input
            aria-label="Search tickets"
            type="text"
            placeholder="Search tickets..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm flex-1"
          />
        </div>
      </div>

      {/* Tickets Table */}
      <div className="bg-navy-900 border border-navy-700 rounded-lg overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-navy-400">Loading tickets...</div>
        ) : filteredTickets.length === 0 ? (
          <div className="p-8 text-center text-navy-400">No tickets found.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-navy-700 text-left">
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Ticket #</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Subject</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Status</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Priority</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Category</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Reporter</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Assignee</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Updated</th>
                </tr>
              </thead>
              <tbody>
                {filteredTickets.map((ticket) => (
                  <tr
                    key={ticket.id}
                    data-click-delegate
                    onClick={() => navigate(`/admin/${communityId}/support/tickets/${ticket.id}`)}
                    className="border-b border-navy-800 hover:bg-navy-800/50 cursor-pointer transition-colors"
                  >
                    <td className="px-4 py-3 text-sm text-gold-400 font-mono"><button type="button" className="font-mono text-gold-400 hover:underline focus-visible:underline">{ticket.ticket_number}</button></td>
                    <td className="px-4 py-3 text-sm text-sky-100">{ticket.subject}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-block px-2 py-1 text-xs rounded border ${STATUS_COLORS[ticket.status] || STATUS_COLORS.open}`}>
                        {(ticket.status || 'open').replace('_', ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-block px-2 py-1 text-xs rounded border ${PRIORITY_COLORS[ticket.priority] || PRIORITY_COLORS.medium}`}>
                        {ticket.priority || 'medium'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm text-navy-300">{ticket.category_name || '-'}</td>
                    <td className="px-4 py-3 text-sm text-navy-300">{ticket.reporter_name || '-'}</td>
                    <td className="px-4 py-3 text-sm text-navy-300">{ticket.assignee_name || 'Unassigned'}</td>
                    <td className="px-4 py-3 text-sm text-navy-400">
                      {ticket.updated_at ? new Date(ticket.updated_at).toLocaleDateString() : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Category Management Modal */}
      {showCategoryModal && (
        <FormModalBuilder
          title="Manage Support Categories"
          fields={categoryFields}
          values={categoryForm}
          onChange={(name, value) => setCategoryForm(prev => ({ ...prev, [name]: value }))}
          onSubmit={handleCreateCategory}
          onClose={() => setShowCategoryModal(false)}
          submitLabel="Create Category"
        >
          {/* Existing categories list */}
          {categories.length > 0 && (
            <div className="mb-4">
              <p className="text-sm font-medium text-sky-200 mb-2">Existing Categories</p>
              <div className="space-y-2">
                {categories.map((cat) => (
                  <div key={cat.id} className="flex items-center justify-between bg-navy-800 p-2 rounded">
                    <div>
                      <span className="text-sm text-sky-100">{cat.name}</span>
                      {cat.description && (
                        <p className="text-xs text-navy-400">{cat.description}</p>
                      )}
                    </div>
                    <button
                      onClick={() => adminApi.deleteSupportCategory(communityId, cat.id).then(loadData)}
                      className="text-xs text-red-400 hover:text-red-300"
                    >
                      Delete
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </FormModalBuilder>
      )}
    </div>
  );
}

export default AdminSupportDashboard;
