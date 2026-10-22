import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import {
  ArchiveBoxIcon, PlusIcon, FunnelIcon, MagnifyingGlassIcon,
  PencilIcon, TrashIcon, PlusCircleIcon, MinusCircleIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { inventoryApi } from '../../services/api';

const ITEM_TYPES = ['general', 'equipment', 'resource', 'consumable'];

const CLAIM_STATUS_COLORS = {
  active: 'bg-green-500/20 text-green-300 border-green-500/30',
  returned: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
  overdue: 'bg-red-500/20 text-red-300 border-red-500/30',
};

const TABS = ['Items', 'Claims'];

const EMPTY_ITEM_FORM = { name: '', description: '', item_type: 'general', category: '', quantity: 1 };
const EMPTY_STOCK_FORM = { quantity: 1, notes: '' };

function AdminInventory() {
  const { communityId } = useParams();
  const [activeTab, setActiveTab] = useState('Items');

  // Items tab
  const [items, setItems] = useState([]);
  const [summary, setSummary] = useState({ total_items: 0, total_quantity: 0, available_quantity: 0, active_claims: 0 });
  const [itemSearch, setItemSearch] = useState('');
  const [itemsLoading, setItemsLoading] = useState(true);
  const [itemsError, setItemsError] = useState('');

  // Claims tab
  const [claims, setClaims] = useState([]);
  const [claimsLoading, setClaimsLoading] = useState(false);
  const [claimsError, setClaimsError] = useState('');
  const [claimStatusFilter, setClaimStatusFilter] = useState('');

  // Item create/edit modal
  const [showItemModal, setShowItemModal] = useState(false);
  const [editingItem, setEditingItem] = useState(null);
  const [itemForm, setItemForm] = useState(EMPTY_ITEM_FORM);
  const [itemFormError, setItemFormError] = useState('');
  const [itemFormLoading, setItemFormLoading] = useState(false);

  // Stock +/- modal
  const [stockModal, setStockModal] = useState(null); // { item, direction: 'add'|'remove' }
  const [stockForm, setStockForm] = useState(EMPTY_STOCK_FORM);
  const [stockError, setStockError] = useState('');
  const [stockLoading, setStockLoading] = useState(false);

  const loadItems = async () => {
    try {
      setItemsLoading(true);
      setItemsError('');
      const [itemsRes, summaryRes] = await Promise.all([
        inventoryApi.listItems(communityId),
        inventoryApi.getSummary(communityId),
      ]);
      setItems(itemsRes.data?.items || []);
      setSummary(summaryRes.data?.summary || { total_items: 0, total_quantity: 0, available_quantity: 0, active_claims: 0 });
    } catch (err) {
      console.error('Failed to load inventory items:', err);
      setItemsError('Failed to load inventory items.');
    } finally {
      setItemsLoading(false);
    }
  };

  const loadClaims = async () => {
    try {
      setClaimsLoading(true);
      setClaimsError('');
      const params = {};
      if (claimStatusFilter) params.status = claimStatusFilter;
      const res = await inventoryApi.listAllCheckouts(communityId, params);
      setClaims(res.data?.checkouts || []);
    } catch (err) {
      console.error('Failed to load claims:', err);
      setClaimsError('Failed to load claims.');
    } finally {
      setClaimsLoading(false);
    }
  };

  useEffect(() => { loadItems(); }, [communityId]);

  useEffect(() => {
    if (activeTab === 'Claims') loadClaims();
  }, [activeTab, communityId, claimStatusFilter]);

  const filteredItems = items.filter((item) => {
    if (!itemSearch) return true;
    const q = itemSearch.toLowerCase();
    return item.name?.toLowerCase().includes(q) || item.category?.toLowerCase().includes(q);
  });

  const openCreateModal = () => {
    setEditingItem(null);
    setItemForm(EMPTY_ITEM_FORM);
    setItemFormError('');
    setShowItemModal(true);
  };

  const openEditModal = (item) => {
    setEditingItem(item);
    setItemForm({ name: item.name || '', description: item.description || '', item_type: item.item_type || 'general', category: item.category || '', quantity: item.quantity || 1 });
    setItemFormError('');
    setShowItemModal(true);
  };

  const handleItemFormChange = (e) => {
    const { name, value } = e.target;
    setItemForm((prev) => ({ ...prev, [name]: value }));
  };

  const handleItemSubmit = async () => {
    if (!itemForm.name.trim()) { setItemFormError('Item name is required.'); return; }
    try {
      setItemFormLoading(true);
      setItemFormError('');
      if (editingItem) {
        const { quantity: _q, ...updateData } = itemForm;
        await inventoryApi.updateItem(communityId, editingItem.id, updateData);
      } else {
        await inventoryApi.createItem(communityId, { ...itemForm, quantity: Number(itemForm.quantity) || 1 });
      }
      setShowItemModal(false);
      loadItems();
    } catch (err) {
      setItemFormError(err?.response?.data?.error || 'Failed to save item.');
    } finally {
      setItemFormLoading(false);
    }
  };

  const handleDeleteItem = async (item) => {
    if (!window.confirm(`Delete "${item.name}"? This cannot be undone.`)) return;
    try {
      await inventoryApi.deleteItem(communityId, item.id);
      loadItems();
    } catch (err) {
      console.error('Failed to delete item:', err);
    }
  };

  const openStockModal = (item, direction) => {
    setStockModal({ item, direction });
    setStockForm(EMPTY_STOCK_FORM);
    setStockError('');
  };

  const handleStockSubmit = async () => {
    const qty = Number(stockForm.quantity);
    if (!qty || qty < 1) { setStockError('Quantity must be at least 1.'); return; }
    try {
      setStockLoading(true);
      setStockError('');
      const fn = stockModal.direction === 'add' ? inventoryApi.addStock : inventoryApi.removeStock;
      await fn(communityId, stockModal.item.id, { quantity: qty, notes: stockForm.notes });
      setStockModal(null);
      loadItems();
    } catch (err) {
      setStockError(err?.response?.data?.error || 'Failed to update stock.');
    } finally {
      setStockLoading(false);
    }
  };

  const statCards = [
    { label: 'Total Items',    value: summary.total_items,       color: 'border-sky-500',    textColor: 'text-sky-400',    bgColor: 'bg-sky-500/10' },
    { label: 'Total Quantity', value: summary.total_quantity,    color: 'border-gold-400',   textColor: 'text-gold-400',   bgColor: 'bg-yellow-500/10' },
    { label: 'Available Qty',  value: summary.available_quantity,color: 'border-green-500',  textColor: 'text-green-400',  bgColor: 'bg-green-500/10' },
    { label: 'Active Claims',  value: summary.active_claims,     color: 'border-orange-500', textColor: 'text-orange-400', bgColor: 'bg-orange-500/10' },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <ArchiveBoxIcon className="w-8 h-8 text-gold-400" />
          <h1 className="text-2xl font-bold text-sky-100">Inventory</h1>
        </div>
        {activeTab === 'Items' && (
          <button
            onClick={openCreateModal}
            className="flex items-center gap-2 px-4 py-2 bg-gold-400 text-navy-900 font-semibold rounded-lg hover:bg-gold-300 transition-colors"
          >
            <PlusIcon className="w-4 h-4" />
            Add Item
          </button>
        )}
      </div>

      {/* Summary Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {statCards.map((card) => (
          <div key={card.label} className={`p-4 rounded-lg border-l-4 ${card.color} ${card.bgColor} bg-navy-900`}>
            <p className="text-sm text-navy-400">{card.label}</p>
            <p className={`text-3xl font-bold ${card.textColor}`}>{card.value ?? 0}</p>
          </div>
        ))}
      </div>

      {/* Tabs */}
      <div className="flex border-b border-navy-700">
        {TABS.map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-5 py-2 text-sm font-medium transition-colors ${
              activeTab === tab ? 'border-b-2 border-gold-400 text-gold-400' : 'text-navy-400 hover:text-sky-200'
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* ── Items Tab ── */}
      {activeTab === 'Items' && (
        <div className="space-y-4">
          <div className="flex items-center gap-3 bg-navy-900 border border-navy-700 rounded-lg p-4">
            <MagnifyingGlassIcon className="w-5 h-5 text-navy-400" />
            <input
              aria-label="Search by name or category"
              type="text"
              placeholder="Search by name or category..."
              value={itemSearch}
              onChange={(e) => setItemSearch(e.target.value)}
              className="bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm flex-1"
            />
            <button onClick={loadItems} className="flex items-center gap-1 px-3 py-2 bg-navy-800 border border-navy-600 text-sky-200 rounded-lg hover:bg-navy-700 transition-colors text-sm">
              <ArrowPathIcon className="w-4 h-4" />Refresh
            </button>
          </div>

          {itemsError && <p className="text-red-400 text-sm">{itemsError}</p>}

          <div className="bg-navy-900 border border-navy-700 rounded-lg overflow-hidden">
            {itemsLoading ? (
              <div className="p-8 text-center text-navy-400">Loading...</div>
            ) : filteredItems.length === 0 ? (
              <div className="p-8 text-center text-navy-400">No items found.</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-navy-700 text-left">
                      {['Name', 'Category', 'Type', 'Total Qty', 'Available Qty', 'Actions'].map((h) => (
                        <th key={h} className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {filteredItems.map((item) => (
                      <tr key={item.id} className="border-b border-navy-800 hover:bg-navy-800/40 transition-colors">
                        <td className="px-4 py-3 text-sm text-sky-100 font-medium">{item.name}</td>
                        <td className="px-4 py-3 text-sm text-navy-300">{item.category || '—'}</td>
                        <td className="px-4 py-3 text-sm text-navy-300 capitalize">{item.item_type || 'general'}</td>
                        <td className="px-4 py-3 text-sm text-sky-200">{item.quantity ?? 0}</td>
                        <td className="px-4 py-3 text-sm text-green-400">{item.available_quantity ?? 0}</td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1">
                            <button onClick={() => openEditModal(item)} title="Edit" className="p-1.5 text-sky-400 hover:text-sky-200 hover:bg-navy-700 rounded transition-colors">
                              <PencilIcon className="w-4 h-4" />
                            </button>
                            <button onClick={() => openStockModal(item, 'add')} title="Add stock" className="p-1.5 text-green-400 hover:text-green-200 hover:bg-navy-700 rounded transition-colors">
                              <PlusCircleIcon className="w-4 h-4" />
                            </button>
                            <button onClick={() => openStockModal(item, 'remove')} title="Remove stock" className="p-1.5 text-orange-400 hover:text-orange-200 hover:bg-navy-700 rounded transition-colors">
                              <MinusCircleIcon className="w-4 h-4" />
                            </button>
                            <button onClick={() => handleDeleteItem(item)} title="Delete" className="p-1.5 text-red-400 hover:text-red-200 hover:bg-navy-700 rounded transition-colors">
                              <TrashIcon className="w-4 h-4" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Claims Tab ── */}
      {activeTab === 'Claims' && (
        <div className="space-y-4">
          <div className="flex items-center gap-3 bg-navy-900 border border-navy-700 rounded-lg p-4">
            <FunnelIcon className="w-5 h-5 text-navy-400" />
            <select
              aria-label="Filter by statuses"
              value={claimStatusFilter}
              onChange={(e) => setClaimStatusFilter(e.target.value)}
              className="bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm"
            >
              <option value="">All Statuses</option>
              <option value="active">Active</option>
              <option value="returned">Returned</option>
              <option value="overdue">Overdue</option>
            </select>
            <button onClick={loadClaims} className="flex items-center gap-1 px-3 py-2 bg-navy-800 border border-navy-600 text-sky-200 rounded-lg hover:bg-navy-700 transition-colors text-sm">
              <ArrowPathIcon className="w-4 h-4" />Refresh
            </button>
          </div>

          {claimsError && <p className="text-red-400 text-sm">{claimsError}</p>}

          <div className="bg-navy-900 border border-navy-700 rounded-lg overflow-hidden">
            {claimsLoading ? (
              <div className="p-8 text-center text-navy-400">Loading...</div>
            ) : claims.length === 0 ? (
              <div className="p-8 text-center text-navy-400">No claims found.</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-navy-700 text-left">
                      {['Item Name', 'Category', 'User', 'Qty', 'Due Date', 'Status', 'Claimed At'].map((h) => (
                        <th key={h} className="px-4 py-3 text-xs font-semibold text-navy-400 uppercase">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {claims.map((claim) => (
                      <tr key={claim.id} className="border-b border-navy-800 hover:bg-navy-800/40 transition-colors">
                        <td className="px-4 py-3 text-sm text-sky-100 font-medium">{claim.item_name || '—'}</td>
                        <td className="px-4 py-3 text-sm text-navy-300">{claim.item_category || '—'}</td>
                        <td className="px-4 py-3 text-sm text-navy-300">{claim.user_name || '—'}</td>
                        <td className="px-4 py-3 text-sm text-sky-200">{claim.quantity_checked_out ?? 1}</td>
                        <td className="px-4 py-3 text-sm text-navy-400">
                          {claim.due_date ? new Date(claim.due_date).toLocaleDateString() : '—'}
                        </td>
                        <td className="px-4 py-3">
                          <span className={`inline-block px-2 py-1 text-xs rounded border ${CLAIM_STATUS_COLORS[claim.status] || CLAIM_STATUS_COLORS.active}`}>
                            {claim.status || 'active'}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-sm text-navy-400">
                          {claim.checked_out_at ? new Date(claim.checked_out_at).toLocaleDateString() : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Create / Edit Item Modal */}
      {showItemModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-navy-900 border border-navy-700 rounded-xl p-6 w-full max-w-lg">
            <h2 className="text-lg font-bold text-sky-100 mb-4">{editingItem ? 'Edit Item' : 'Add Inventory Item'}</h2>
            <div className="space-y-4">
              <div>
                <label htmlFor="admin-inventory-input" className="block text-sm text-navy-400 mb-1">Name <span className="text-red-400">*</span></label>
                <input id="admin-inventory-input" type="text" name="name" value={itemForm.name} onChange={handleItemFormChange} placeholder="Item name" className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm" />
              </div>
              <div>
                <label htmlFor="admin-inventory-description" className="block text-sm text-navy-400 mb-1">Description</label>
                <textarea id="admin-inventory-description" name="description" value={itemForm.description} onChange={handleItemFormChange} placeholder="Optional description" rows={3} className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm resize-none" />
              </div>
              <div>
                <label htmlFor="admin-inventory-type" className="block text-sm text-navy-400 mb-1">Type</label>
                <select id="admin-inventory-type" name="item_type" value={itemForm.item_type} onChange={handleItemFormChange} className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm">
                  {ITEM_TYPES.map((t) => <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>)}
                </select>
              </div>
              <div>
                <label htmlFor="admin-inventory-category" className="block text-sm text-navy-400 mb-1">Category</label>
                <input id="admin-inventory-category" type="text" name="category" value={itemForm.category} onChange={handleItemFormChange} placeholder="e.g. Tools, Supplies" className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm" />
              </div>
              {!editingItem && (
                <div>
                  <label htmlFor="admin-inventory-initial-quantity" className="block text-sm text-navy-400 mb-1">Initial Quantity</label>
                  <input id="admin-inventory-initial-quantity" type="number" name="quantity" value={itemForm.quantity} onChange={handleItemFormChange} min={1} className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm" />
                </div>
              )}
              {itemFormError && <p className="text-red-400 text-sm">{itemFormError}</p>}
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <button onClick={() => setShowItemModal(false)} className="px-4 py-2 bg-navy-800 border border-navy-600 text-sky-200 rounded-lg hover:bg-navy-700 transition-colors text-sm">Cancel</button>
              <button onClick={handleItemSubmit} disabled={itemFormLoading} className="px-4 py-2 bg-gold-400 text-navy-900 font-semibold rounded-lg hover:bg-gold-300 transition-colors text-sm disabled:opacity-50">
                {itemFormLoading ? 'Saving...' : editingItem ? 'Save Changes' : 'Add Item'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Stock +/- Modal */}
      {stockModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-navy-900 border border-navy-700 rounded-xl p-6 w-full max-w-sm">
            <h2 className="text-lg font-bold text-sky-100 mb-1">{stockModal.direction === 'add' ? 'Add Stock' : 'Remove Stock'}</h2>
            <p className="text-sm text-navy-400 mb-4">{stockModal.item.name}</p>
            <div className="space-y-4">
              <div>
                <label htmlFor="admin-inventory-quantity" className="block text-sm text-navy-400 mb-1">Quantity</label>
                <input id="admin-inventory-quantity" type="number" value={stockForm.quantity} onChange={(e) => setStockForm((p) => ({ ...p, quantity: e.target.value }))} min={1} className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm" />
              </div>
              <div>
                <label htmlFor="admin-inventory-notes" className="block text-sm text-navy-400 mb-1">Notes</label>
                <input id="admin-inventory-notes" type="text" value={stockForm.notes} onChange={(e) => setStockForm((p) => ({ ...p, notes: e.target.value }))} placeholder="Optional notes" className="w-full bg-navy-800 border border-navy-600 text-sky-200 rounded-lg px-3 py-2 text-sm" />
              </div>
              {stockError && <p className="text-red-400 text-sm">{stockError}</p>}
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <button onClick={() => setStockModal(null)} className="px-4 py-2 bg-navy-800 border border-navy-600 text-sky-200 rounded-lg hover:bg-navy-700 transition-colors text-sm">Cancel</button>
              <button onClick={handleStockSubmit} disabled={stockLoading} className={`px-4 py-2 font-semibold rounded-lg transition-colors text-sm disabled:opacity-50 ${stockModal.direction === 'add' ? 'bg-green-600 text-white hover:bg-green-500' : 'bg-orange-600 text-white hover:bg-orange-500'}`}>
                {stockLoading ? 'Updating...' : stockModal.direction === 'add' ? 'Add Stock' : 'Remove Stock'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminInventory;
