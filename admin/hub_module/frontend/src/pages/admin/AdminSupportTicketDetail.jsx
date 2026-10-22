import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  TicketIcon, ChatBubbleLeftRightIcon, ArrowLeftIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';
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

function AdminSupportTicketDetail() {
  const { communityId, ticketId } = useParams();
  const [ticket, setTicket] = useState(null);
  const [loading, setLoading] = useState(true);
  const [commentText, setCommentText] = useState('');
  const [isInternal, setIsInternal] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    loadTicket();
  }, [communityId, ticketId]);

  const loadTicket = async () => {
    try {
      setLoading(true);
      const res = await adminApi.getSupportTicket(communityId, ticketId);
      setTicket(res.data?.ticket || null);
    } catch (err) {
      console.error('Failed to load ticket:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleStatusChange = async (status) => {
    try {
      await adminApi.updateTicketStatus(communityId, ticketId, status);
      loadTicket();
    } catch (err) {
      console.error('Failed to update status:', err);
    }
  };

  const handlePriorityChange = async (priority) => {
    try {
      await adminApi.updateSupportTicketPriority(communityId, ticketId, priority);
      loadTicket();
    } catch (err) {
      console.error('Failed to update priority:', err);
    }
  };

  const handleAddComment = async (e) => {
    e.preventDefault();
    if (!commentText.trim()) return;
    try {
      setSubmitting(true);
      await adminApi.addSupportTicketComment(communityId, ticketId, commentText, isInternal);
      setCommentText('');
      setIsInternal(false);
      loadTicket();
    } catch (err) {
      console.error('Failed to add comment:', err);
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return <div className="p-8 text-center text-navy-400">Loading ticket...</div>;
  }

  if (!ticket) {
    return <div className="p-8 text-center text-navy-400">Ticket not found.</div>;
  }

  const comments = ticket.comments || [];

  return (
    <div className="space-y-6">
      {/* Back link */}
      <Link
        to={`/admin/${communityId}/support`}
        className="inline-flex items-center gap-2 text-navy-400 hover:text-sky-300 transition-colors"
      >
        <ArrowLeftIcon className="w-4 h-4" />
        Back to Support Dashboard
      </Link>

      {/* Header */}
      <div className="bg-navy-900 border border-navy-700 rounded-lg p-6">
        <div className="flex items-start justify-between mb-4">
          <div className="flex items-center gap-3">
            <TicketIcon className="w-6 h-6 text-gold-400" />
            <div>
              <span className="text-sm text-gold-400 font-mono">{ticket.ticket_number}</span>
              <h1 className="text-xl font-bold text-sky-100">{ticket.subject}</h1>
            </div>
          </div>
        </div>

        {/* Controls row */}
        <div className="flex flex-wrap items-center gap-4">
          <div>
            <label htmlFor="admin-support-ticket-detail-status" className="block text-xs text-navy-400 mb-1">Status</label>
            <select
              id="admin-support-ticket-detail-status"
              value={ticket.status || 'open'}
              onChange={(e) => handleStatusChange(e.target.value)}
              className="bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-1.5 text-sm"
            >
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>{s.replace('_', ' ').replace(/\b\w/g, c => c.toUpperCase())}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="admin-support-ticket-detail-priority" className="block text-xs text-navy-400 mb-1">Priority</label>
            <select
              id="admin-support-ticket-detail-priority"
              value={ticket.priority || 'medium'}
              onChange={(e) => handlePriorityChange(e.target.value)}
              className="bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-1.5 text-sm"
            >
              {PRIORITY_OPTIONS.map((p) => (
                <option key={p} value={p}>{p.charAt(0).toUpperCase() + p.slice(1)}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs text-navy-400 mb-1">Assignee</label>
            <span className="text-sm text-sky-200">{ticket.assignee_name || 'Unassigned'}</span>
          </div>
        </div>
      </div>

      {/* Ticket Info */}
      <div className="bg-navy-900 border border-navy-700 rounded-lg p-6">
        <div className="grid grid-cols-2 gap-4 mb-4 text-sm">
          <div>
            <span className="text-navy-400">Reporter:</span>{' '}
            <span className="text-sky-200">{ticket.reporter_name || '-'}</span>
          </div>
          <div>
            <span className="text-navy-400">Category:</span>{' '}
            <span className="text-sky-200">{ticket.category_name || '-'}</span>
          </div>
          <div>
            <span className="text-navy-400">Created:</span>{' '}
            <span className="text-sky-200">
              {ticket.created_at ? new Date(ticket.created_at).toLocaleString() : '-'}
            </span>
          </div>
          <div>
            <span className="text-navy-400">Updated:</span>{' '}
            <span className="text-sky-200">
              {ticket.updated_at ? new Date(ticket.updated_at).toLocaleString() : '-'}
            </span>
          </div>
        </div>
        {ticket.description && (
          <div>
            <h3 className="text-sm font-medium text-navy-400 mb-2">Description</h3>
            <p className="text-sky-100 whitespace-pre-wrap">{ticket.description}</p>
          </div>
        )}
        {ticket.custom_fields && Object.keys(ticket.custom_fields).length > 0 && (
          <div className="mt-4">
            <h3 className="text-sm font-medium text-navy-400 mb-2">Custom Fields</h3>
            <div className="space-y-1">
              {Object.entries(ticket.custom_fields).map(([key, val]) => (
                <div key={key} className="text-sm">
                  <span className="text-navy-400">{key}:</span>{' '}
                  <span className="text-sky-200">{String(val)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Comments Thread */}
      <div className="bg-navy-900 border border-navy-700 rounded-lg p-6">
        <div className="flex items-center gap-2 mb-4">
          <ChatBubbleLeftRightIcon className="w-5 h-5 text-gold-400" />
          <h2 className="text-lg font-semibold text-sky-100">Comments ({comments.length})</h2>
        </div>

        {comments.length === 0 ? (
          <p className="text-navy-400 text-sm">No comments yet.</p>
        ) : (
          <div className="space-y-4">
            {comments.map((comment) => (
              <div
                key={comment.id}
                className={`p-4 rounded-lg border ${
                  comment.is_internal
                    ? 'bg-amber-500/10 border-amber-500/30'
                    : 'bg-navy-800 border-navy-700'
                }`}
              >
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-sky-200">{comment.author_name || 'Unknown'}</span>
                    {comment.is_internal && (
                      <span className="text-xs px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">
                        Internal
                      </span>
                    )}
                  </div>
                  <span className="text-xs text-navy-400">
                    {comment.created_at ? new Date(comment.created_at).toLocaleString() : ''}
                  </span>
                </div>
                <p className="text-sm text-sky-100 whitespace-pre-wrap">{comment.content}</p>
              </div>
            ))}
          </div>
        )}

        {/* Add Comment Form */}
        <form onSubmit={handleAddComment} className="mt-6 space-y-3">
          <textarea
            aria-label="Write a comment"
            value={commentText}
            onChange={(e) => setCommentText(e.target.value)}
            placeholder="Write a comment..."
            rows={3}
            className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-4 py-3 text-sm placeholder-navy-500 focus:border-gold-500"
          />
          <div className="flex items-center justify-between">
            <label className="flex items-center gap-2 text-sm text-sky-200">
              <input
                type="checkbox"
                checked={isInternal}
                onChange={(e) => setIsInternal(e.target.checked)}
                className="w-4 h-4 rounded bg-navy-800 border-navy-600"
              />
              Internal note (only visible to admins)
            </label>
            <button
              type="submit"
              disabled={submitting || !commentText.trim()}
              className="px-4 py-2 bg-gold-500 text-navy-900 font-medium rounded-lg hover:bg-gold-400 disabled:opacity-50 transition-colors text-sm"
            >
              {submitting ? 'Sending...' : 'Add Comment'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default AdminSupportTicketDetail;
