import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { ArchiveBoxIcon, MagnifyingGlassIcon, ShoppingCartIcon } from '@heroicons/react/24/outline';
import { inventoryApi } from '../../services/api';

function InventoryBrowse() {
  const { communityId } = useParams();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [successMessage, setSuccessMessage] = useState('');

  const [showModal, setShowModal] = useState(false);
  const [selectedItem, setSelectedItem] = useState(null);
  const [claimQty, setClaimQty] = useState(1);
  const [claimDueDate, setClaimDueDate] = useState('');
  const [claimNotes, setClaimNotes] = useState('');
  const [claimError, setClaimError] = useState('');
  const [claimSubmitting, setClaimSubmitting] = useState(false);

  useEffect(() => {
    loadItems();
  }, [communityId]);

  useEffect(() => {
    const delay = setTimeout(() => loadItems(), 300);
    return () => clearTimeout(delay);
  }, [search]);

  const loadItems = async () => {
    try {
      setLoading(true);
      setError('');
      const res = await inventoryApi.listAvailable(communityId, { search });
      setItems(res.data?.items || []);
    } catch (err) {
      console.error('Failed to load inventory:', err);
      setError('Failed to load inventory items.');
    } finally {
      setLoading(false);
    }
  };

  const openClaimModal = (item) => {
    setSelectedItem(item);
    setClaimQty(1);
    setClaimDueDate('');
    setClaimNotes('');
    setClaimError('');
    setShowModal(true);
  };

  const closeModal = () => {
    setShowModal(false);
    setSelectedItem(null);
    setClaimError('');
  };

  const handleClaim = async (e) => {
    e.preventDefault();
    if (!selectedItem) return;
    try {
      setClaimSubmitting(true);
      setClaimError('');
      await inventoryApi.checkoutItem(communityId, {
        item_id: selectedItem.id,
        quantity: claimQty,
        due_date: claimDueDate || undefined,
        notes: claimNotes || undefined,
      });
      closeModal();
      setSuccessMessage(`Claimed "${selectedItem.name}" successfully!`);
      setTimeout(() => setSuccessMessage(''), 4000);
      await loadItems();
    } catch (err) {
      console.error('Failed to claim item:', err);
      setClaimError(err.response?.data?.error || 'Failed to claim item. Please try again.');
    } finally {
      setClaimSubmitting(false);
    }
  };

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <ArchiveBoxIcon className="w-8 h-8 text-gold-400" />
        <h1 className="text-2xl font-bold text-sky-100">Community Inventory</h1>
      </div>

      {successMessage && (
        <div className="bg-green-500/10 border border-green-500/30 text-green-300 rounded-lg px-4 py-3 text-sm">
          {successMessage}
        </div>
      )}

      <div className="relative">
        <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-navy-400" />
        <input
          aria-label="Search items"
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search items..."
          className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg pl-10 pr-4 py-2 placeholder-navy-500 focus:border-gold-500"
        />
      </div>

      {error && <div className="text-red-400 text-sm">{error}</div>}

      {loading ? (
        <div className="p-8 text-center text-navy-400">Loading inventory...</div>
      ) : items.length === 0 ? (
        <div className="p-8 text-center text-navy-400">No items available right now.</div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {items.map((item) => (
            <div key={item.id} className="bg-navy-900 border border-navy-700 rounded-lg p-4 flex flex-col gap-3">
              <div className="flex items-start justify-between gap-2">
                <span className="text-sky-100 font-semibold leading-snug">{item.name}</span>
                {item.category && (
                  <span className="shrink-0 text-xs bg-navy-800 text-navy-400 rounded px-2 py-0.5 border border-navy-700">
                    {item.category}
                  </span>
                )}
              </div>
              {item.description && (
                <p className="text-navy-400 text-sm leading-relaxed">{item.description}</p>
              )}
              <div className="text-sm text-navy-400">
                Available:{' '}
                <span className={item.available_quantity > 0 ? 'text-green-300' : 'text-red-400'}>
                  {item.available_quantity}
                </span>
                {' '}/ Total: <span className="text-sky-200">{item.quantity}</span>
              </div>
              {item.available_quantity === 0 ? (
                <span className="self-start text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded px-2 py-1">
                  Out of Stock
                </span>
              ) : (
                <button
                  onClick={() => openClaimModal(item)}
                  className="flex items-center gap-2 self-start px-4 py-2 bg-gold-500 hover:bg-gold-400 text-navy-950 font-semibold rounded-lg text-sm transition-colors"
                >
                  <ShoppingCartIcon className="w-4 h-4" />
                  Claim
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {showModal && selectedItem && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div role="dialog" aria-modal="true" aria-labelledby="inventory-browse-dialog-1-title" className="bg-navy-900 border border-navy-700 rounded-xl p-6 w-full max-w-md mx-4">
            <h2 id="inventory-browse-dialog-1-title" className="text-lg font-semibold text-sky-100 mb-4">Claim: {selectedItem.name}</h2>
            <form onSubmit={handleClaim} className="space-y-4">
              <div>
                <label htmlFor="inventory-browse-quantity" className="block text-sm text-navy-400 mb-1">Quantity</label>
                <input
                  id="inventory-browse-quantity"
                  type="number"
                  min={1}
                  max={selectedItem.available_quantity}
                  value={claimQty}
                  onChange={(e) => setClaimQty(Number(e.target.value))}
                  className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 focus:border-gold-500"
                  required
                />
              </div>
              <div>
                <label htmlFor="inventory-browse-due-date-optional" className="block text-sm text-navy-400 mb-1">
                  Due Date <span className="text-navy-500">(optional)</span>
                </label>
                <input
                  id="inventory-browse-due-date-optional"
                  type="date"
                  value={claimDueDate}
                  onChange={(e) => setClaimDueDate(e.target.value)}
                  className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 focus:border-gold-500"
                />
              </div>
              <div>
                <label htmlFor="inventory-browse-notes-optional" className="block text-sm text-navy-400 mb-1">
                  Notes <span className="text-navy-500">(optional)</span>
                </label>
                <textarea
                  id="inventory-browse-notes-optional"
                  value={claimNotes}
                  onChange={(e) => setClaimNotes(e.target.value)}
                  rows={3}
                  placeholder="Any notes about this claim..."
                  className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 placeholder-navy-500 focus:border-gold-500 resize-none"
                />
              </div>
              {claimError && <div className="text-red-400 text-sm">{claimError}</div>}
              <div className="flex gap-3 pt-2">
                <button
                  type="submit"
                  disabled={claimSubmitting}
                  className="flex-1 px-4 py-2 bg-gold-500 hover:bg-gold-400 text-navy-950 font-semibold rounded-lg transition-colors disabled:opacity-50"
                >
                  {claimSubmitting ? 'Claiming...' : 'Confirm Claim'}
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

export default InventoryBrowse;
