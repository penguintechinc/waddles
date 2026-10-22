import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArchiveBoxArrowDownIcon, ArrowUturnLeftIcon } from '@heroicons/react/24/outline';
import { inventoryApi } from '../../services/api';

const STATUS_COLORS = {
  active: 'bg-green-500/20 text-green-300 border-green-500/30',
  overdue: 'bg-red-500/20 text-red-300 border-red-500/30',
};

function InventoryMyItems() {
  const { communityId } = useParams();
  const [checkouts, setCheckouts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [successMessage, setSuccessMessage] = useState('');

  const [showModal, setShowModal] = useState(false);
  const [selectedCheckout, setSelectedCheckout] = useState(null);
  const [returnQty, setReturnQty] = useState(1);
  const [returnNotes, setReturnNotes] = useState('');
  const [returnError, setReturnError] = useState('');
  const [returnSubmitting, setReturnSubmitting] = useState(false);

  useEffect(() => {
    loadCheckouts();
  }, [communityId]);

  const loadCheckouts = async () => {
    try {
      setLoading(true);
      setError('');
      const res = await inventoryApi.getMyCheckouts(communityId);
      setCheckouts(res.data?.checkouts || []);
    } catch (err) {
      console.error('Failed to load checkouts:', err);
      setError('Failed to load your claimed items.');
    } finally {
      setLoading(false);
    }
  };

  const openReturnModal = (checkout) => {
    setSelectedCheckout(checkout);
    setReturnQty(checkout.quantity_checked_out);
    setReturnNotes('');
    setReturnError('');
    setShowModal(true);
  };

  const closeModal = () => {
    setShowModal(false);
    setSelectedCheckout(null);
    setReturnError('');
  };

  const handleReturn = async (e) => {
    e.preventDefault();
    if (!selectedCheckout) return;
    try {
      setReturnSubmitting(true);
      setReturnError('');
      await inventoryApi.checkinItem(communityId, {
        checkout_id: selectedCheckout.id,
        quantity_returned: returnQty,
        notes: returnNotes || undefined,
      });
      closeModal();
      setSuccessMessage(`Returned "${selectedCheckout.item_name}" successfully!`);
      setTimeout(() => setSuccessMessage(''), 4000);
      setCheckouts((prev) => prev.filter((c) => c.id !== selectedCheckout.id));
    } catch (err) {
      console.error('Failed to return item:', err);
      setReturnError(err.response?.data?.error || 'Failed to return item. Please try again.');
    } finally {
      setReturnSubmitting(false);
    }
  };

  const getStatus = (checkout) => {
    if (!checkout.due_date) return 'active';
    return new Date(checkout.due_date) < new Date() ? 'overdue' : 'active';
  };

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <ArchiveBoxArrowDownIcon className="w-8 h-8 text-gold-400" />
        <h1 className="text-2xl font-bold text-sky-100">My Claimed Items</h1>
      </div>

      {successMessage && (
        <div className="bg-green-500/10 border border-green-500/30 text-green-300 rounded-lg px-4 py-3 text-sm">
          {successMessage}
        </div>
      )}

      {error && <div className="text-red-400 text-sm">{error}</div>}

      {loading ? (
        <div className="p-8 text-center text-navy-400">Loading your claimed items...</div>
      ) : checkouts.length === 0 ? (
        <div className="p-8 text-center space-y-3">
          <p className="text-navy-400">You have no active claims.</p>
          <Link
            to={`/community/${communityId}/inventory`}
            className="inline-block text-sm text-gold-400 hover:text-gold-300 underline underline-offset-2 transition-colors"
          >
            Browse available inventory
          </Link>
        </div>
      ) : (
        <div className="bg-navy-900 border border-navy-700 rounded-lg overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-navy-700 text-left">
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Item</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Category</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Qty</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Due Date</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Claimed On</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Status</th>
                  <th className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody>
                {checkouts.map((checkout) => {
                  const status = getStatus(checkout);
                  return (
                    <tr key={checkout.id} className="border-b border-navy-800 hover:bg-navy-800/30 transition-colors">
                      <td className="px-4 py-3 text-sm text-sky-100 font-medium">{checkout.item_name}</td>
                      <td className="px-4 py-3">
                        {checkout.item_category ? (
                          <span className="text-xs bg-navy-800 text-navy-400 rounded px-2 py-0.5 border border-navy-700">
                            {checkout.item_category}
                          </span>
                        ) : (
                          <span className="text-navy-500 text-xs">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-sky-200">{checkout.quantity_checked_out}</td>
                      <td className="px-4 py-3 text-sm text-navy-400">
                        {checkout.due_date ? new Date(checkout.due_date).toLocaleDateString() : '—'}
                      </td>
                      <td className="px-4 py-3 text-sm text-navy-400">
                        {checkout.checked_out_at ? new Date(checkout.checked_out_at).toLocaleDateString() : '—'}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-block px-2 py-1 text-xs rounded border ${STATUS_COLORS[status]}`}>
                          {status}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <button
                          onClick={() => openReturnModal(checkout)}
                          className="flex items-center gap-1.5 text-sm px-3 py-1.5 bg-navy-800 border border-navy-600 hover:bg-navy-700 text-sky-200 rounded-lg transition-colors"
                        >
                          <ArrowUturnLeftIcon className="w-4 h-4" />
                          Return
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {showModal && selectedCheckout && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-navy-900 border border-navy-700 rounded-xl p-6 w-full max-w-md mx-4">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">Return: {selectedCheckout.item_name}</h2>
            <form onSubmit={handleReturn} className="space-y-4">
              <div>
                <label htmlFor="inventory-my-items-quantity-to-return" className="block text-sm text-navy-400 mb-1">Quantity to Return</label>
                <input
                  id="inventory-my-items-quantity-to-return"
                  type="number"
                  min={1}
                  max={selectedCheckout.quantity_checked_out}
                  value={returnQty}
                  onChange={(e) => setReturnQty(Number(e.target.value))}
                  className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 focus:border-gold-500"
                  required
                />
              </div>
              <div>
                <label htmlFor="inventory-my-items-notes-optional" className="block text-sm text-navy-400 mb-1">
                  Notes <span className="text-navy-500">(optional)</span>
                </label>
                <textarea
                  id="inventory-my-items-notes-optional"
                  value={returnNotes}
                  onChange={(e) => setReturnNotes(e.target.value)}
                  rows={3}
                  placeholder="Any notes about the return..."
                  className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 placeholder-navy-500 focus:border-gold-500 resize-none"
                />
              </div>
              {returnError && <div className="text-red-400 text-sm">{returnError}</div>}
              <div className="flex gap-3 pt-2">
                <button
                  type="submit"
                  disabled={returnSubmitting}
                  className="flex-1 px-4 py-2 bg-gold-500 hover:bg-gold-400 text-navy-950 font-semibold rounded-lg transition-colors disabled:opacity-50"
                >
                  {returnSubmitting ? 'Returning...' : 'Confirm Return'}
                </button>
                <button
                  type="button"
                  onClick={closeModal}
                  className="flex-1 px-4 py-2 bg-navy-800 border border-navy-600 text-sky-200 rounded-lg hover:bg-navy-700 transition-colors"
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default InventoryMyItems;
