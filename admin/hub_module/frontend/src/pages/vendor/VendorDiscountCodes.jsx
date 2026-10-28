/**
 * Vendor Discount Codes
 * Full discount code management: create, edit, deactivate, delete
 */
import { useEffect, useState, useCallback } from 'react';
import {
  TicketIcon,
  PlusIcon,
  PencilSquareIcon,
  TrashIcon,
  XMarkIcon,
  CheckCircleIcon,
  XCircleIcon,
  ClockIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';

// ─── Toast ────────────────────────────────────────────────────────────────────

function Toast({ toasts, onDismiss }) {
  return (
    <div className="fixed top-4 right-4 z-50 flex flex-col space-y-2 pointer-events-none">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`flex items-center space-x-3 px-4 py-3 rounded-lg shadow-lg text-sm font-medium pointer-events-auto transition-all ${
            t.type === 'success'
              ? 'bg-emerald-800 border border-emerald-600 text-emerald-100'
              : 'bg-red-900 border border-red-600 text-red-100'
          }`}
        >
          {t.type === 'success' ? (
            <CheckCircleIcon className="w-5 h-5 text-emerald-400 flex-shrink-0" />
          ) : (
            <XCircleIcon className="w-5 h-5 text-red-400 flex-shrink-0" />
          )}
          <span>{t.message}</span>
          <button
            aria-label="Dismiss notification"
            onClick={() => onDismiss(t.id)}
            className="ml-2 opacity-60 hover:opacity-100 transition-opacity"
          >
            <XMarkIcon className="w-4 h-4" />
          </button>
        </div>
      ))}
    </div>
  );
}

// ─── Confirm Dialog ───────────────────────────────────────────────────────────

function ConfirmDialog({ open, title, message, confirmLabel = 'Confirm', onConfirm, onCancel, danger = false }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div role="dialog" aria-modal="true" aria-labelledby="vendor-discount-codes-dialog-1-title" className="bg-navy-800 border border-navy-700 rounded-xl p-6 w-full max-w-sm shadow-2xl">
        <div className="flex items-start space-x-3 mb-4">
          <ExclamationTriangleIcon className="w-6 h-6 text-orange-400 flex-shrink-0 mt-0.5" />
          <div>
            <h3 id="vendor-discount-codes-dialog-1-title" className="text-white font-semibold">{title}</h3>
            <p className="text-navy-300 text-sm mt-1">{message}</p>
          </div>
        </div>
        <div className="flex justify-end space-x-3">
          <button
            onClick={onCancel}
            className="px-4 py-2 text-sm text-navy-300 hover:text-white border border-navy-600 hover:border-navy-500 rounded-lg transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className={`px-4 py-2 text-sm text-white rounded-lg transition-colors ${
              danger
                ? 'bg-red-600 hover:bg-red-700'
                : 'bg-sky-600 hover:bg-sky-700'
            }`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Status Badge ─────────────────────────────────────────────────────────────

function StatusBadge({ status }) {
  const configs = {
    active: {
      label: 'Active',
      icon: <CheckCircleIcon className="w-3.5 h-3.5" />,
      classes: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
    },
    expired: {
      label: 'Expired',
      icon: <ClockIcon className="w-3.5 h-3.5" />,
      classes: 'text-orange-400 bg-orange-500/10 border-orange-500/20',
    },
    maxed: {
      label: 'Maxed Out',
      icon: <XCircleIcon className="w-3.5 h-3.5" />,
      classes: 'text-red-400 bg-red-500/10 border-red-500/20',
    },
    inactive: {
      label: 'Inactive',
      icon: <XCircleIcon className="w-3.5 h-3.5" />,
      classes: 'text-navy-400 bg-navy-700/50 border-navy-600',
    },
  };
  const cfg = configs[status] || configs.inactive;
  return (
    <span
      className={`inline-flex items-center space-x-1 px-2 py-0.5 rounded-full text-xs font-medium border ${cfg.classes}`}
    >
      {cfg.icon}
      <span>{cfg.label}</span>
    </span>
  );
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const today = () => new Date().toISOString().slice(0, 10);

const EMPTY_FORM = {
  code: '',
  module_id: '',
  discount_type: 'percentage',
  discount_value: '',
  max_uses: '',
  usage_window_days: '',
  application_months: '',
  valid_from: today(),
  valid_until: '',
  is_active: true,
};

function formatDiscountValue(type, value) {
  if (type === 'free') return 'Free';
  if (type === 'percentage') return `${value}%`;
  if (type === 'fixed') return `$${Number(value).toFixed(2)}`;
  return value;
}

function deriveStatus(code) {
  if (!code.is_active) return 'inactive';
  if (code.max_uses && code.use_count >= code.max_uses) return 'maxed';
  if (code.valid_until && new Date(code.valid_until) < new Date()) return 'expired';
  return 'active';
}

// ─── Create/Edit Modal ────────────────────────────────────────────────────────

function CodeModal({ open, onClose, onSaved, editCode, modules }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState(null);

  useEffect(() => {
    if (!open) return;
    if (editCode) {
      setForm({
        code: editCode.code || '',
        module_id: editCode.module_id || '',
        discount_type: editCode.discount_type || 'percentage',
        discount_value: editCode.discount_value != null ? String(editCode.discount_value) : '',
        max_uses: editCode.max_uses != null ? String(editCode.max_uses) : '',
        usage_window_days: editCode.usage_window_days != null ? String(editCode.usage_window_days) : '',
        application_months: editCode.application_months != null ? String(editCode.application_months) : '',
        valid_from: editCode.valid_from ? editCode.valid_from.slice(0, 10) : today(),
        valid_until: editCode.valid_until ? editCode.valid_until.slice(0, 10) : '',
        is_active: editCode.is_active !== false,
      });
    } else {
      setForm({ ...EMPTY_FORM, valid_from: today() });
    }
    setFormError(null);
  }, [open, editCode]);

  if (!open) return null;

  const set = (field, value) => setForm((f) => ({ ...f, [field]: value }));

  const handleCodeInput = (e) => {
    const val = e.target.value.replace(/[^A-Z0-9]/gi, '').toUpperCase().slice(0, 32);
    set('code', val);
  };

  const validate = () => {
    if (!form.code.trim()) return 'Code is required.';
    if (!/^[A-Z0-9]+$/.test(form.code)) return 'Code must be alphanumeric (A-Z, 0-9).';
    if (form.discount_type !== 'free') {
      const v = parseFloat(form.discount_value);
      if (!form.discount_value || isNaN(v) || v <= 0) return 'Discount value must be a positive number.';
      if (form.discount_type === 'percentage' && v > 100) return 'Percentage cannot exceed 100.';
    }
    if (form.max_uses && (isNaN(parseInt(form.max_uses)) || parseInt(form.max_uses) < 1))
      return 'Max uses must be a positive integer.';
    if (form.usage_window_days && (isNaN(parseInt(form.usage_window_days)) || parseInt(form.usage_window_days) < 1))
      return 'Usage window must be a positive integer.';
    if (form.application_months && (isNaN(parseInt(form.application_months)) || parseInt(form.application_months) < 1))
      return 'Application months must be a positive integer.';
    if (form.valid_until && form.valid_from && form.valid_until < form.valid_from)
      return 'Valid Until must be after Valid From.';
    return null;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    const err = validate();
    if (err) { setFormError(err); return; }
    setFormError(null);
    setSaving(true);

    const payload = {
      code: form.code,
      module_id: form.module_id || null,
      discount_type: form.discount_type,
      discount_value: form.discount_type === 'free' ? null : parseFloat(form.discount_value),
      max_uses: form.max_uses ? parseInt(form.max_uses) : null,
      usage_window_days: form.usage_window_days ? parseInt(form.usage_window_days) : null,
      application_months: form.application_months ? parseInt(form.application_months) : null,
      valid_from: form.valid_from || null,
      valid_until: form.valid_until || null,
      is_active: form.is_active,
    };

    try {
      if (editCode) {
        await api.put(`/vendor/discount-codes/${editCode.id}`, payload);
      } else {
        await api.post('/vendor/discount-codes', payload);
      }
      onSaved(editCode ? 'Code updated.' : 'Code created.');
    } catch (err) {
      setFormError(err.response?.data?.error?.message || 'Save failed. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  const isEdit = Boolean(editCode);

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4">
      <div role="dialog" aria-modal="true" aria-labelledby="vendor-discount-codes-dialog-2-title" className="bg-navy-800 border border-navy-700 rounded-xl w-full max-w-lg max-h-[90vh] overflow-y-auto shadow-2xl">
        {/* Modal header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-navy-700">
          <h2 id="vendor-discount-codes-dialog-2-title" className="text-xl font-bold text-white">
            {isEdit ? 'Edit Discount Code' : 'Create Discount Code'}
          </h2>
          <button
            aria-label="Close"
            onClick={onClose}
            className="text-navy-400 hover:text-white transition-colors"
          >
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-5 space-y-5">
          {/* Error */}
          {formError && (
            <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg text-sm">
              {formError}
            </div>
          )}

          {/* Code */}
          <div>
            <label htmlFor="vendor-discount-codes-input" className="block text-sm font-medium text-navy-200 mb-1.5">
              Code <span className="text-red-400">*</span>
            </label>
            <input
              id="vendor-discount-codes-input"
              type="text"
              value={form.code}
              onChange={handleCodeInput}
              disabled={isEdit}
              placeholder="e.g. LAUNCH50"
              maxLength={32}
              className="w-full bg-navy-900 border border-navy-600 text-white font-mono tracking-widest rounded-lg px-3 py-2 text-sm focus:border-sky-500 disabled:opacity-50 disabled:cursor-not-allowed placeholder-navy-500"
            />
            <p className="text-xs text-navy-400 mt-1">Alphanumeric only — auto-uppercased.</p>
          </div>

          {/* Module */}
          <div>
            <label htmlFor="vendor-discount-codes-module" className="block text-sm font-medium text-navy-200 mb-1.5">Module</label>
            <select
              id="vendor-discount-codes-module"
              value={form.module_id}
              onChange={(e) => set('module_id', e.target.value)}
              className="w-full bg-navy-900 border border-navy-600 text-white rounded-lg px-3 py-2 text-sm focus:border-sky-500"
            >
              <option value="">All My Modules</option>
              {modules.map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))}
            </select>
          </div>

          {/* Discount Type */}
          <div>
            <label className="block text-sm font-medium text-navy-200 mb-2">
              Discount Type <span className="text-red-400">*</span>
            </label>
            <div className="flex space-x-3">
              {[
                { value: 'percentage', label: 'Percentage' },
                { value: 'fixed', label: 'Fixed Amount' },
                { value: 'free', label: 'Free' },
              ].map((opt) => (
                <label
                  key={opt.value}
                  className={`flex-1 flex items-center justify-center space-x-2 border rounded-lg px-3 py-2 text-sm cursor-pointer transition-colors ${
                    form.discount_type === opt.value
                      ? 'border-sky-500 bg-sky-500/10 text-sky-300'
                      : 'border-navy-600 text-navy-300 hover:border-navy-500'
                  }`}
                >
                  <input
                    type="radio"
                    name="discount_type"
                    value={opt.value}
                    checked={form.discount_type === opt.value}
                    onChange={() => set('discount_type', opt.value)}
                    className="sr-only"
                  />
                  <span>{opt.label}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Discount Value */}
          {form.discount_type !== 'free' && (
            <div>
              <label className="block text-sm font-medium text-navy-200 mb-1.5">
                Discount Value <span className="text-red-400">*</span>
              </label>
              <div className="relative">
                {form.discount_type === 'fixed' && (
                  <span className="absolute left-3 top-1/2 -translate-y-1/2 text-navy-400 text-sm">$</span>
                )}
                <input
                  aria-label="Discount value"
                  type="number"
                  min="0"
                  max={form.discount_type === 'percentage' ? '100' : undefined}
                  step="0.01"
                  value={form.discount_value}
                  onChange={(e) => set('discount_value', e.target.value)}
                  placeholder={form.discount_type === 'percentage' ? '25' : '5.00'}
                  className={`w-full bg-navy-900 border border-navy-600 text-white rounded-lg px-3 py-2 text-sm focus:border-sky-500 ${
                    form.discount_type === 'fixed' ? 'pl-7' : ''
                  } ${form.discount_type === 'percentage' ? 'pr-7' : ''}`}
                />
                {form.discount_type === 'percentage' && (
                  <span className="absolute right-3 top-1/2 -translate-y-1/2 text-navy-400 text-sm">%</span>
                )}
              </div>
            </div>
          )}

          {/* Two-column: Max Uses + Window Days */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="vendor-discount-codes-max-uses" className="block text-sm font-medium text-navy-200 mb-1.5">Max Uses</label>
              <input
                id="vendor-discount-codes-max-uses"
                type="number"
                min="1"
                step="1"
                value={form.max_uses}
                onChange={(e) => set('max_uses', e.target.value)}
                placeholder="Unlimited"
                className="w-full bg-navy-900 border border-navy-600 text-white rounded-lg px-3 py-2 text-sm focus:border-sky-500 placeholder-navy-500"
              />
              <p className="text-xs text-navy-400 mt-1">Leave empty for unlimited.</p>
            </div>
            <div>
              <label htmlFor="vendor-discount-codes-window-days" className="block text-sm font-medium text-navy-200 mb-1.5">Window (days)</label>
              <input
                id="vendor-discount-codes-window-days"
                type="number"
                min="1"
                step="1"
                value={form.usage_window_days}
                onChange={(e) => set('usage_window_days', e.target.value)}
                placeholder="No expiry"
                className="w-full bg-navy-900 border border-navy-600 text-white rounded-lg px-3 py-2 text-sm focus:border-sky-500 placeholder-navy-500"
              />
              <p className="text-xs text-navy-400 mt-1">Expires N days after creation.</p>
            </div>
          </div>

          {/* Application Months */}
          <div>
            <label htmlFor="vendor-discount-codes-application-months" className="block text-sm font-medium text-navy-200 mb-1.5">Application Months</label>
            <input
              id="vendor-discount-codes-application-months"
              type="number"
              min="1"
              step="1"
              value={form.application_months}
              onChange={(e) => set('application_months', e.target.value)}
              placeholder="Forever"
              className="w-full bg-navy-900 border border-navy-600 text-white rounded-lg px-3 py-2 text-sm focus:border-sky-500 placeholder-navy-500"
            />
            <p className="text-xs text-navy-400 mt-1">Discount applies for first N months of subscription.</p>
          </div>

          {/* Valid From / Until */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="vendor-discount-codes-valid-from" className="block text-sm font-medium text-navy-200 mb-1.5">Valid From</label>
              <input
                id="vendor-discount-codes-valid-from"
                type="date"
                value={form.valid_from}
                onChange={(e) => set('valid_from', e.target.value)}
                className="w-full bg-navy-900 border border-navy-600 text-white rounded-lg px-3 py-2 text-sm focus:border-sky-500"
              />
            </div>
            <div>
              <label htmlFor="vendor-discount-codes-valid-until" className="block text-sm font-medium text-navy-200 mb-1.5">Valid Until</label>
              <input
                id="vendor-discount-codes-valid-until"
                type="date"
                value={form.valid_until}
                onChange={(e) => set('valid_until', e.target.value)}
                className="w-full bg-navy-900 border border-navy-600 text-white rounded-lg px-3 py-2 text-sm focus:border-sky-500"
              />
            </div>
          </div>

          {/* Active toggle */}
          <div className="flex items-center justify-between p-3 bg-navy-900 rounded-lg border border-navy-700">
            <div>
              <p className="text-sm font-medium text-white">Active</p>
              <p className="text-xs text-navy-400">Inactive codes cannot be redeemed.</p>
            </div>
            <button
              type="button"
              onClick={() => set('is_active', !form.is_active)}
              className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                form.is_active ? 'bg-emerald-600' : 'bg-navy-600'
              }`}
            >
              <span
                className={`inline-block h-4 w-4 rounded-full bg-white transform transition-transform ${
                  form.is_active ? 'translate-x-6' : 'translate-x-1'
                }`}
              />
            </button>
          </div>

          {/* Actions */}
          <div className="flex justify-end space-x-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-navy-300 hover:text-white border border-navy-600 hover:border-navy-500 rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="flex items-center space-x-2 px-5 py-2 text-sm bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg transition-colors"
            >
              {saving && (
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              )}
              <span>{saving ? 'Saving…' : isEdit ? 'Save Changes' : 'Create Code'}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────────

function VendorDiscountCodes() {
  const [codes, setCodes] = useState([]);
  const [modules, setModules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('all');
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [toasts, setToasts] = useState([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editCode, setEditCode] = useState(null);
  const [confirm, setConfirm] = useState(null); // { type: 'delete'|'toggle', code }
  const [actionLoading, setActionLoading] = useState(null);

  const LIMIT = 20;

  // ── Toast helpers ──────────────────────────────────────────────────────────

  const addToast = useCallback((message, type = 'success') => {
    const id = Date.now();
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 4000);
  }, []);

  const dismissToast = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  // ── Data loaders ───────────────────────────────────────────────────────────

  const loadCodes = useCallback(async (pageNum = 1, statusFilter = 'all') => {
    try {
      setLoading(true);
      const params = { page: pageNum, limit: LIMIT };
      if (statusFilter !== 'all') params.status = statusFilter;
      const response = await api.get('/vendor/discount-codes', { params });
      const data = response.data;
      setCodes(data?.codes || data?.items || []);
      const total = data?.total || data?.pagination?.total || 0;
      setTotalPages(Math.max(1, Math.ceil(total / LIMIT)));
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load discount codes.');
      setCodes([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadModules = useCallback(async () => {
    try {
      const response = await api.get('/vendor/modules');
      setModules(response.data?.modules || []);
    } catch {
      setModules([]);
    }
  }, []);

  useEffect(() => {
    loadModules();
  }, [loadModules]);

  useEffect(() => {
    loadCodes(page, filter);
  }, [loadCodes, page, filter]);

  // ── Filter tabs ────────────────────────────────────────────────────────────

  const handleFilterChange = (key) => {
    setFilter(key);
    setPage(1);
  };

  // ── Modal ──────────────────────────────────────────────────────────────────

  const openCreate = () => {
    setEditCode(null);
    setModalOpen(true);
  };

  const openEdit = (code) => {
    setEditCode(code);
    setModalOpen(true);
  };

  const handleModalSaved = (message) => {
    setModalOpen(false);
    addToast(message, 'success');
    loadCodes(page, filter);
  };

  // ── Delete ─────────────────────────────────────────────────────────────────

  const confirmDelete = (code) => {
    setConfirm({ type: 'delete', code });
  };

  const executeDelete = async () => {
    const { code } = confirm;
    setConfirm(null);
    setActionLoading(code.id + '-delete');
    try {
      await api.delete(`/vendor/discount-codes/${code.id}`);
      addToast(`Code "${code.code}" deleted.`, 'success');
      loadCodes(page, filter);
    } catch (err) {
      addToast(err.response?.data?.error?.message || 'Delete failed.', 'error');
    } finally {
      setActionLoading(null);
    }
  };

  // ── Activate / Deactivate ──────────────────────────────────────────────────

  const confirmToggle = (code) => {
    setConfirm({ type: 'toggle', code });
  };

  const executeToggle = async () => {
    const { code } = confirm;
    setConfirm(null);
    setActionLoading(code.id + '-toggle');
    try {
      await api.put(`/vendor/discount-codes/${code.id}`, { is_active: !code.is_active });
      addToast(
        `Code "${code.code}" ${code.is_active ? 'deactivated' : 'activated'}.`,
        'success'
      );
      loadCodes(page, filter);
    } catch (err) {
      addToast(err.response?.data?.error?.message || 'Update failed.', 'error');
    } finally {
      setActionLoading(null);
    }
  };

  // ── Helpers ────────────────────────────────────────────────────────────────

  const getModuleName = (module_id) => {
    if (!module_id) return 'All Modules';
    const mod = modules.find((m) => String(m.id) === String(module_id));
    return mod ? mod.name : 'Unknown Module';
  };

  const filterTabs = [
    { key: 'all', label: 'All' },
    { key: 'active', label: 'Active' },
    { key: 'expired', label: 'Expired' },
    { key: 'maxed', label: 'Maxed Out' },
  ];

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <>
      <Toast toasts={toasts} onDismiss={dismissToast} />

      <CodeModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onSaved={handleModalSaved}
        editCode={editCode}
        modules={modules}
      />

      <ConfirmDialog
        open={Boolean(confirm)}
        title={
          confirm?.type === 'delete'
            ? `Delete code "${confirm?.code?.code}"?`
            : confirm?.code?.is_active
            ? `Deactivate code "${confirm?.code?.code}"?`
            : `Activate code "${confirm?.code?.code}"?`
        }
        message={
          confirm?.type === 'delete'
            ? 'This action cannot be undone. The code will be permanently removed.'
            : confirm?.code?.is_active
            ? 'This code will no longer be redeemable until reactivated.'
            : 'This code will become redeemable again.'
        }
        confirmLabel={
          confirm?.type === 'delete'
            ? 'Delete'
            : confirm?.code?.is_active
            ? 'Deactivate'
            : 'Activate'
        }
        danger={confirm?.type === 'delete'}
        onConfirm={confirm?.type === 'delete' ? executeDelete : executeToggle}
        onCancel={() => setConfirm(null)}
      />

      <div className="space-y-6">
        {/* Header */}
        <div className="flex justify-between items-center">
          <div>
            <h1 className="text-3xl font-bold text-gold-400">Discount Codes</h1>
            <p className="text-navy-300 mt-1">Create and manage discount codes for your modules</p>
          </div>
          <button
            onClick={openCreate}
            className="flex items-center space-x-2 bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 rounded-lg transition-colors"
          >
            <PlusIcon className="w-5 h-5" />
            <span>Create New Code</span>
          </button>
        </div>

        {/* Error */}
        {error && (
          <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg text-sm">
            {error}
          </div>
        )}

        {/* Filter tabs */}
        <div className="flex space-x-2 border-b border-navy-700 pb-0">
          {filterTabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => handleFilterChange(tab.key)}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors -mb-px ${
                filter === tab.key
                  ? 'border-sky-400 text-sky-400'
                  : 'border-transparent text-navy-400 hover:text-navy-200'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Loading spinner */}
        {loading ? (
          <div className="flex items-center justify-center h-48">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400" />
          </div>
        ) : codes.length === 0 ? (
          /* Empty state */
          <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
            <TicketIcon className="w-14 h-14 text-navy-600 mx-auto mb-4" />
            <h2 className="text-white font-semibold mb-2">No discount codes yet</h2>
            <p className="text-navy-400 text-sm mb-6 max-w-xs mx-auto">
              {filter === 'all'
                ? 'Create your first code to offer promotional pricing on your modules.'
                : `No ${filter === 'maxed' ? 'maxed-out' : filter} codes found.`}
            </p>
            {filter === 'all' && (
              <button
                onClick={openCreate}
                className="inline-flex items-center space-x-2 bg-emerald-600 hover:bg-emerald-700 text-white px-5 py-2 rounded-lg text-sm transition-colors"
              >
                <PlusIcon className="w-4 h-4" />
                <span>Create First Code</span>
              </button>
            )}
          </div>
        ) : (
          /* Codes table */
          <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-navy-700 bg-navy-900/60">
                    <th className="text-left px-4 py-3 text-navy-400 font-medium">Code</th>
                    <th className="text-left px-4 py-3 text-navy-400 font-medium">Module</th>
                    <th className="text-left px-4 py-3 text-navy-400 font-medium">Discount</th>
                    <th className="text-left px-4 py-3 text-navy-400 font-medium">Uses</th>
                    <th className="text-left px-4 py-3 text-navy-400 font-medium">Window</th>
                    <th className="text-left px-4 py-3 text-navy-400 font-medium">Status</th>
                    <th className="text-left px-4 py-3 text-navy-400 font-medium">Created</th>
                    <th className="text-right px-4 py-3 text-navy-400 font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-navy-700">
                  {codes.map((code) => {
                    const status = deriveStatus(code);
                    const toggleLoading = actionLoading === code.id + '-toggle';
                    const deleteLoading = actionLoading === code.id + '-delete';
                    return (
                      <tr
                        key={code.id}
                        className="hover:bg-navy-900/50 transition-colors"
                      >
                        {/* Code */}
                        <td className="px-4 py-3">
                          <span className="font-mono text-sky-300 tracking-wider text-xs bg-sky-500/10 border border-sky-500/20 px-2 py-0.5 rounded">
                            {code.code}
                          </span>
                        </td>

                        {/* Module */}
                        <td className="px-4 py-3 text-navy-300">
                          {getModuleName(code.module_id)}
                        </td>

                        {/* Discount */}
                        <td className="px-4 py-3">
                          <span className="text-white font-medium">
                            {formatDiscountValue(code.discount_type, code.discount_value)}
                          </span>
                          <span className="text-navy-500 text-xs ml-1 capitalize">
                            {code.discount_type !== 'free' && `(${code.discount_type})`}
                          </span>
                        </td>

                        {/* Uses */}
                        <td className="px-4 py-3 text-navy-300">
                          {code.use_count ?? 0}
                          {code.max_uses ? (
                            <span className="text-navy-500"> / {code.max_uses}</span>
                          ) : (
                            <span className="text-navy-500"> / ∞</span>
                          )}
                        </td>

                        {/* Window */}
                        <td className="px-4 py-3 text-navy-300">
                          {code.usage_window_days ? `${code.usage_window_days}d` : '—'}
                        </td>

                        {/* Status */}
                        <td className="px-4 py-3">
                          <StatusBadge status={status} />
                        </td>

                        {/* Created */}
                        <td className="px-4 py-3 text-navy-400 whitespace-nowrap">
                          {code.created_at
                            ? new Date(code.created_at).toLocaleDateString()
                            : '—'}
                        </td>

                        {/* Actions */}
                        <td className="px-4 py-3">
                          <div className="flex items-center justify-end space-x-1">
                            {/* Edit */}
                            <button
                              onClick={() => openEdit(code)}
                              title="Edit"
                              className="p-1.5 text-navy-400 hover:text-sky-400 hover:bg-sky-500/10 rounded transition-colors"
                            >
                              <PencilSquareIcon className="w-4 h-4" />
                            </button>

                            {/* Activate / Deactivate */}
                            <button
                              onClick={() => confirmToggle(code)}
                              disabled={toggleLoading}
                              title={code.is_active ? 'Deactivate' : 'Activate'}
                              className={`p-1.5 rounded transition-colors ${
                                code.is_active
                                  ? 'text-navy-400 hover:text-orange-400 hover:bg-orange-500/10'
                                  : 'text-navy-400 hover:text-emerald-400 hover:bg-emerald-500/10'
                              } disabled:opacity-40 disabled:cursor-not-allowed`}
                            >
                              {toggleLoading ? (
                                <span className="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin inline-block" />
                              ) : code.is_active ? (
                                <XCircleIcon className="w-4 h-4" />
                              ) : (
                                <CheckCircleIcon className="w-4 h-4" />
                              )}
                            </button>

                            {/* Delete */}
                            <button
                              onClick={() => confirmDelete(code)}
                              disabled={deleteLoading}
                              title="Delete"
                              className="p-1.5 text-navy-400 hover:text-red-400 hover:bg-red-500/10 rounded transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                            >
                              {deleteLoading ? (
                                <span className="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin inline-block" />
                              ) : (
                                <TrashIcon className="w-4 h-4" />
                              )}
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between px-4 py-3 border-t border-navy-700 bg-navy-900/40">
                <span className="text-xs text-navy-400">
                  Page {page} of {totalPages}
                </span>
                <div className="flex space-x-2">
                  <button
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={page === 1}
                    className="px-3 py-1 text-xs border border-navy-600 text-navy-300 rounded hover:border-navy-500 hover:text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    Previous
                  </button>
                  <button
                    onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                    disabled={page === totalPages}
                    className="px-3 py-1 text-xs border border-navy-600 text-navy-300 rounded hover:border-navy-500 hover:text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  );
}

export default VendorDiscountCodes;
