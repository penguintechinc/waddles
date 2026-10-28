import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { TicketIcon, ChatBubbleLeftRightIcon } from '@heroicons/react/24/outline';
import { supportApi } from '../../services/api';

const STATUS_COLORS = {
  open: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  in_progress: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
  waiting: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  resolved: 'bg-green-500/20 text-green-300 border-green-500/30',
  closed: 'bg-navy-500/20 text-fg-secondary border-navy-500/30',
};

const PRIORITY_COLORS = {
  urgent: 'bg-red-500/20 text-red-300 border-red-500/30',
  high: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  medium: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  low: 'bg-navy-500/20 text-fg-secondary border-navy-500/30',
};

function SupportMyTickets() {
  const { communityId } = useParams();
  const [tickets, setTickets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedTicket, setSelectedTicket] = useState(null);
  const [ticketDetail, setTicketDetail] = useState(null);
  const [commentText, setCommentText] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    loadTickets();
  }, [communityId]);

  const loadTickets = async () => {
    try {
      setLoading(true);
      const res = await supportApi.getMyTickets(communityId);
      setTickets(res.data?.tickets || []);
    } catch (err) {
      console.error('Failed to load tickets:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleSelectTicket = async (ticketId) => {
    if (selectedTicket === ticketId) {
      setSelectedTicket(null);
      setTicketDetail(null);
      return;
    }
    try {
      setSelectedTicket(ticketId);
      const res = await supportApi.getMyTicket(communityId, ticketId);
      setTicketDetail(res.data?.ticket || null);
    } catch (err) {
      console.error('Failed to load ticket detail:', err);
    }
  };

  const handleAddComment = async (e) => {
    e.preventDefault();
    if (!commentText.trim() || !selectedTicket) return;
    try {
      setSubmitting(true);
      await supportApi.addComment(communityId, selectedTicket, commentText);
      setCommentText('');
      // Reload detail
      const res = await supportApi.getMyTicket(communityId, selectedTicket);
      setTicketDetail(res.data?.ticket || null);
    } catch (err) {
      console.error('Failed to add comment:', err);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <TicketIcon className="w-8 h-8 text-gold-400" />
        <h1 className="text-2xl font-bold text-sky-100">My Support Tickets</h1>
      </div>

      {loading ? (
        <div className="p-8 text-center text-navy-400">Loading tickets...</div>
      ) : tickets.length === 0 ? (
        <div className="p-8 text-center text-navy-400">You have no support tickets.</div>
      ) : (
        <div className="bg-navy-900 border border-navy-700 rounded-lg overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-navy-700 text-left">
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Ticket #</th>
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Subject</th>
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Status</th>
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Priority</th>
                <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Updated</th>
              </tr>
            </thead>
            <tbody>
              {tickets.map((ticket) => (
                <>
                  <tr
                    key={ticket.id}
                    data-click-delegate
                    onClick={() => handleSelectTicket(ticket.id)}
                    className={`border-b border-navy-800 hover:bg-navy-800/50 cursor-pointer transition-colors ${
                      selectedTicket === ticket.id ? 'bg-navy-800/70' : ''
                    }`}
                  >
                    <td className="px-4 py-3 text-sm text-gold-400 font-mono"><button type="button" aria-expanded={selectedTicket === ticket.id} className="font-mono text-gold-400 hover:underline focus-visible:underline">{ticket.ticket_number}</button></td>
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
                    <td className="px-4 py-3 text-sm text-navy-400">
                      {ticket.updated_at ? new Date(ticket.updated_at).toLocaleDateString() : '-'}
                    </td>
                  </tr>
                  {selectedTicket === ticket.id && ticketDetail && (
                    <tr key={`${ticket.id}-detail`}>
                      <td colSpan={5} className="px-4 py-4 bg-navy-800/30">
                        {/* Description */}
                        {ticketDetail.description && (
                          <div className="mb-4">
                            <h4 className="text-sm font-medium text-navy-400 mb-1">Description</h4>
                            <p className="text-sm text-sky-100 whitespace-pre-wrap">{ticketDetail.description}</p>
                          </div>
                        )}

                        {/* Comments */}
                        <div className="mb-4">
                          <div className="flex items-center gap-2 mb-2">
                            <ChatBubbleLeftRightIcon className="w-4 h-4 text-gold-400" />
                            <h4 className="text-sm font-medium text-sky-200">
                              Comments ({(ticketDetail.comments || []).length})
                            </h4>
                          </div>
                          {(ticketDetail.comments || []).length === 0 ? (
                            <p className="text-xs text-navy-400">No comments yet.</p>
                          ) : (
                            <div className="space-y-2">
                              {(ticketDetail.comments || []).map((c) => (
                                <div key={c.id} className="bg-navy-800 border border-navy-700 rounded p-3">
                                  <div className="flex items-center justify-between mb-1">
                                    <span className="text-xs font-medium text-sky-200">{c.author_name || 'Staff'}</span>
                                    <span className="text-xs text-navy-400">
                                      {c.created_at ? new Date(c.created_at).toLocaleString() : ''}
                                    </span>
                                  </div>
                                  <p className="text-sm text-sky-100 whitespace-pre-wrap">{c.content}</p>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>

                        {/* Add Comment */}
                        <form onSubmit={handleAddComment} className="flex gap-2">
                          <input
                            aria-label="Add a comment"
                            type="text"
                            value={commentText}
                            onChange={(e) => setCommentText(e.target.value)}
                            placeholder="Add a comment..."
                            className="flex-1 bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm placeholder-navy-500"
                          />
                          <button
                            type="submit"
                            disabled={submitting || !commentText.trim()}
                            className="px-4 py-2 bg-gold-500 text-navy-900 font-medium rounded-lg hover:bg-gold-400 disabled:opacity-50 transition-colors text-sm"
                          >
                            {submitting ? '...' : 'Send'}
                          </button>
                        </form>
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default SupportMyTickets;
