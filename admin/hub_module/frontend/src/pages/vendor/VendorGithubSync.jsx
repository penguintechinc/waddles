/**
 * Vendor GitHub Sync
 * Configure GitHub Issues / Discussions sync connections for support ticket integration
 */
import { useEffect, useState, useCallback } from 'react';
import {
  CodeBracketIcon,
  PlusIcon,
  PencilSquareIcon,
  TrashIcon,
  XMarkIcon,
  CheckCircleIcon,
  XCircleIcon,
  ExclamationTriangleIcon,
  ClipboardDocumentIcon,
  ArrowPathIcon,
  SignalIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';

const SYNC_MODES = [
  { value: 'tickets_only', label: 'Tickets Only' },
  { value: 'tickets_and_discussions', label: 'Tickets + Discussions' },
  { value: 'off', label: 'Off' },
];

const AUTH_TYPES = [
  { value: 'github_app', label: 'GitHub App' },
  { value: 'pat', label: 'Personal Access Token' },
];

const EMPTY_FORM = {
  repoOwner: '',
  repoName: '',
  authType: 'pat',
  token: '',
  syncMode: 'tickets_only',
  defaultLabels: [],
  autoClose: false,
};

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
      <div role="dialog" aria-modal="true" aria-labelledby="vendor-github-sync-dialog-1-title" className="bg-navy-800 border border-navy-700 rounded-xl p-6 w-full max-w-sm shadow-2xl">
        <div className="flex items-start space-x-3 mb-4">
          <ExclamationTriangleIcon className="w-6 h-6 text-orange-400 flex-shrink-0 mt-0.5" />
          <div>
            <h3 id="vendor-github-sync-dialog-1-title" className="text-white font-semibold">{title}</h3>
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
              danger ? 'bg-red-600 hover:bg-red-700' : 'bg-sky-600 hover:bg-sky-700'
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
    active: { label: 'Active', classes: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20' },
    error: { label: 'Error', classes: 'text-red-400 bg-red-500/10 border-red-500/20' },
    pending: { label: 'Pending', classes: 'text-orange-400 bg-orange-500/10 border-orange-500/20' },
    off: { label: 'Off', classes: 'text-navy-400 bg-navy-700/50 border-navy-600' },
  };
  const cfg = configs[status] || configs.pending;
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${cfg.classes}`}>
      {cfg.label}
    </span>
  );
}

// ─── Tag Input ────────────────────────────────────────────────────────────────

function TagInput({ tags, onChange }) {
  const [input, setInput] = useState('');

  const addTag = (e) => {
    if ((e.key === 'Enter' || e.key === ',') && input.trim()) {
      e.preventDefault();
      const tag = input.trim().replace(/,$/, '');
      if (tag && !tags.includes(tag)) {
        onChange([...tags, tag]);
      }
      setInput('');
    }
  };

  const removeTag = (tag) => onChange(tags.filter((t) => t !== tag));

  return (
    <div className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 flex flex-wrap gap-1.5 focus-within:border-gold-400">
      {tags.map((tag) => (
        <span
          key={tag}
          className="inline-flex items-center space-x-1 bg-sky-600/20 text-sky-300 border border-sky-500/30 rounded px-2 py-0.5 text-xs"
        >
          <span>{tag}</span>
          <button
            aria-label={`Remove label ${tag}`}
            type="button"
            onClick={() => removeTag(tag)}
            className="opacity-60 hover:opacity-100"
          >
            <XMarkIcon className="w-3 h-3" />
          </button>
        </span>
      ))}
      <input
        aria-label="Add label"
        type="text"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={addTag}
        placeholder={tags.length === 0 ? 'Type label and press Enter…' : ''}
        className="bg-transparent text-white text-sm placeholder-navy-500 min-w-24 flex-1"
      />
    </div>
  );
}

// ─── Connection Modal ─────────────────────────────────────────────────────────

function ConnectionModal({ open, editTarget, onClose, onSaved, showToast }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      setForm(editTarget ? { ...EMPTY_FORM, ...editTarget, token: '' } : { ...EMPTY_FORM });
    }
  }, [open, editTarget]);

  if (!open) return null;

  const setField = (field, value) => setForm((prev) => ({ ...prev, [field]: value }));

  const handleSubmit = async (e) => {
    e.preventDefault();
    try {
      setSaving(true);
      if (editTarget?.id) {
        await api.put(`/vendor/github-sync/connections/${editTarget.id}`, form);
        showToast('success', 'Connection updated');
      } else {
        await api.post('/vendor/github-sync/connections', form);
        showToast('success', 'Repository connected');
      }
      onSaved();
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Failed to save connection');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div role="dialog" aria-modal="true" aria-labelledby="vendor-github-sync-dialog-2-title" className="bg-navy-800 border border-navy-700 rounded-xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-navy-700">
          <h2 id="vendor-github-sync-dialog-2-title" className="text-lg font-bold text-white">
            {editTarget ? 'Edit Connection' : 'Connect Repository'}
          </h2>
          <button
            aria-label="Close"
            onClick={onClose}
            className="text-navy-400 hover:text-white transition-colors"
          >
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-5">
          {/* Repo Owner + Name */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="vendor-github-sync-input" className="block text-sm font-medium text-navy-300 mb-1.5">
                Repo Owner <span className="text-red-400">*</span>
              </label>
              <input
                id="vendor-github-sync-input"
                type="text"
                value={form.repoOwner}
                onChange={(e) => setField('repoOwner', e.target.value)}
                required
                placeholder="owner"
                className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400 text-sm"
              />
            </div>
            <div>
              <label htmlFor="vendor-github-sync-input-2" className="block text-sm font-medium text-navy-300 mb-1.5">
                Repo Name <span className="text-red-400">*</span>
              </label>
              <input
                id="vendor-github-sync-input-2"
                type="text"
                value={form.repoName}
                onChange={(e) => setField('repoName', e.target.value)}
                required
                placeholder="repository"
                className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400 text-sm"
              />
            </div>
          </div>

          {/* Auth Type */}
          <div>
            <label className="block text-sm font-medium text-navy-300 mb-2">Auth Type</label>
            <div className="flex space-x-4">
              {AUTH_TYPES.map((a) => (
                <label key={a.value} className="flex items-center space-x-2 cursor-pointer">
                  <input
                    type="radio"
                    name="authType"
                    value={a.value}
                    checked={form.authType === a.value}
                    onChange={() => setField('authType', a.value)}
                    className="text-sky-500 border-navy-600 bg-navy-900 focus:ring-sky-500"
                  />
                  <span className="text-sm text-white">{a.label}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Token */}
          <div>
            <label htmlFor="vendor-github-sync-input-3" className="block text-sm font-medium text-navy-300 mb-1.5">
              {form.authType === 'github_app' ? 'Installation Token' : 'Personal Access Token'}{' '}
              {!editTarget && <span className="text-red-400">*</span>}
              {editTarget && <span className="text-navy-500 font-normal">(leave blank to keep existing)</span>}
            </label>
            <input
              id="vendor-github-sync-input-3"
              type="password"
              value={form.token}
              onChange={(e) => setField('token', e.target.value)}
              required={!editTarget}
              placeholder={editTarget ? '••••••••' : 'ghp_...'}
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400 text-sm"
            />
          </div>

          {/* Sync Mode */}
          <div>
            <label htmlFor="vendor-github-sync-sync-mode" className="block text-sm font-medium text-navy-300 mb-1.5">Sync Mode</label>
            <select
              id="vendor-github-sync-sync-mode"
              value={form.syncMode}
              onChange={(e) => setField('syncMode', e.target.value)}
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white focus:border-gold-400 text-sm"
            >
              {SYNC_MODES.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>

          {/* Default Labels */}
          <div>
            <label className="block text-sm font-medium text-navy-300 mb-1.5">Default Labels</label>
            <TagInput tags={form.defaultLabels} onChange={(tags) => setField('defaultLabels', tags)} />
            <p className="text-xs text-navy-500 mt-1">Labels auto-applied to issues created from tickets</p>
          </div>

          {/* Auto-close */}
          <label className="flex items-center space-x-3 cursor-pointer">
            <input
              type="checkbox"
              checked={form.autoClose}
              onChange={(e) => setField('autoClose', e.target.checked)}
              className="w-4 h-4 rounded border-navy-600 bg-navy-900 text-sky-500 focus:ring-sky-500"
            />
            <div>
              <p className="text-white text-sm font-medium">Auto-close tickets</p>
              <p className="text-xs text-navy-400">Close Waddles ticket when GitHub issue is closed</p>
            </div>
          </label>

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
              className="bg-sky-600 hover:bg-sky-700 disabled:opacity-50 disabled:cursor-not-allowed text-white px-5 py-2 rounded-lg text-sm font-medium transition-colors"
            >
              {saving ? 'Saving…' : editTarget ? 'Save Changes' : 'Connect'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

function VendorGithubSync() {
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [toasts, setToasts] = useState([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editTarget, setEditTarget] = useState(null);
  const [confirmDisconnect, setConfirmDisconnect] = useState(null);
  const [testingId, setTestingId] = useState(null);
  const [copied, setCopied] = useState(false);

  const webhookUrl = `${window.location.origin}/api/v1/github-sync/webhook`;

  const showToast = useCallback((type, message) => {
    const id = Date.now();
    setToasts((prev) => [...prev, { id, type, message }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 4000);
  }, []);

  const dismissToast = (id) => setToasts((prev) => prev.filter((t) => t.id !== id));

  const loadConnections = useCallback(async () => {
    try {
      setLoading(true);
      const res = await api.get('/vendor/github-sync/connections');
      setConnections(res.data?.connections || []);
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Failed to load connections');
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    loadConnections();
  }, [loadConnections]);

  const handleOpenAdd = () => {
    setEditTarget(null);
    setModalOpen(true);
  };

  const handleOpenEdit = (conn) => {
    setEditTarget(conn);
    setModalOpen(true);
  };

  const handleModalSaved = () => {
    setModalOpen(false);
    loadConnections();
  };

  const handleDisconnect = async () => {
    if (!confirmDisconnect) return;
    try {
      await api.delete(`/vendor/github-sync/connections/${confirmDisconnect.id}`);
      showToast('success', `Disconnected ${confirmDisconnect.repoOwner}/${confirmDisconnect.repoName}`);
      loadConnections();
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Failed to disconnect');
    } finally {
      setConfirmDisconnect(null);
    }
  };

  const handleTestConnection = async (conn) => {
    try {
      setTestingId(conn.id);
      await api.post(`/vendor/github-sync/connections/${conn.id}/test`);
      showToast('success', 'Connection test passed');
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Connection test failed');
    } finally {
      setTestingId(null);
    }
  };

  const handleCopyWebhook = () => {
    navigator.clipboard.writeText(webhookUrl).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  const syncModeLabel = (val) => SYNC_MODES.find((m) => m.value === val)?.label || val;

  return (
    <div className="space-y-8 max-w-4xl">
      <Toast toasts={toasts} onDismiss={dismissToast} />

      <ConfirmDialog
        open={!!confirmDisconnect}
        title="Disconnect Repository"
        message={
          confirmDisconnect
            ? `Remove the sync connection for ${confirmDisconnect.repoOwner}/${confirmDisconnect.repoName}? Existing tickets will not be affected.`
            : ''
        }
        confirmLabel="Disconnect"
        danger
        onConfirm={handleDisconnect}
        onCancel={() => setConfirmDisconnect(null)}
      />

      <ConnectionModal
        open={modalOpen}
        editTarget={editTarget}
        onClose={() => setModalOpen(false)}
        onSaved={handleModalSaved}
        showToast={showToast}
      />

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <CodeBracketIcon className="w-8 h-8 text-gold-400" />
          <div>
            <h1 className="text-3xl font-bold text-white">GitHub Integration</h1>
            <p className="text-navy-300 mt-1">Sync support tickets with GitHub Issues and Discussions</p>
          </div>
        </div>
        <button
          onClick={handleOpenAdd}
          className="flex items-center space-x-2 bg-sky-600 hover:bg-sky-700 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
        >
          <PlusIcon className="w-4 h-4" />
          <span>Connect Repository</span>
        </button>
      </div>

      {/* Webhook URL */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-5">
        <h2 className="text-sm font-semibold text-navy-300 uppercase tracking-wider mb-3">
          Webhook URL
        </h2>
        <div className="flex items-center space-x-3">
          <code className="flex-1 bg-navy-900 border border-navy-600 rounded px-3 py-2 text-sky-300 text-sm font-mono truncate">
            {webhookUrl}
          </code>
          <button
            onClick={handleCopyWebhook}
            className={`flex items-center space-x-2 px-3 py-2 rounded-lg text-sm font-medium border transition-colors ${
              copied
                ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'
                : 'bg-navy-700 border-navy-600 text-navy-300 hover:text-white hover:border-navy-500'
            }`}
          >
            <ClipboardDocumentIcon className="w-4 h-4" />
            <span>{copied ? 'Copied!' : 'Copy'}</span>
          </button>
        </div>
        <p className="text-xs text-navy-500 mt-2">
          Add this URL to your GitHub repository webhook settings (Content type: application/json)
        </p>
      </div>

      {/* Connected Repositories */}
      <div>
        <h2 className="text-lg font-bold text-white mb-4">Connected Repositories</h2>

        {loading ? (
          <div className="flex items-center justify-center h-32">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400" />
          </div>
        ) : connections.length === 0 ? (
          <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 flex flex-col items-center justify-center text-center">
            <CodeBracketIcon className="w-12 h-12 text-navy-600 mb-3" />
            <p className="text-navy-300 font-medium">No repositories connected</p>
            <p className="text-navy-500 text-sm mt-1">
              Connect a repository to sync issues with support tickets
            </p>
            <button
              onClick={handleOpenAdd}
              className="mt-4 flex items-center space-x-2 bg-sky-600 hover:bg-sky-700 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            >
              <PlusIcon className="w-4 h-4" />
              <span>Connect Repository</span>
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            {connections.map((conn) => (
              <div
                key={conn.id}
                className="bg-navy-800 border border-navy-700 rounded-lg p-5 flex items-center justify-between gap-4"
              >
                <div className="flex items-start space-x-4 min-w-0">
                  <div className="w-10 h-10 rounded-lg bg-navy-700 border border-navy-600 flex items-center justify-center flex-shrink-0">
                    <CodeBracketIcon className="w-5 h-5 text-sky-400" />
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center space-x-2 flex-wrap gap-y-1">
                      <p className="text-white font-semibold">
                        {conn.repoOwner}/{conn.repoName}
                      </p>
                      <StatusBadge status={conn.status || 'pending'} />
                    </div>
                    <div className="flex items-center space-x-3 mt-1 text-xs text-navy-400 flex-wrap gap-y-0.5">
                      <span className="flex items-center space-x-1">
                        <ArrowPathIcon className="w-3.5 h-3.5" />
                        <span>{syncModeLabel(conn.syncMode)}</span>
                      </span>
                      {conn.lastSynced && (
                        <span>Last synced {new Date(conn.lastSynced).toLocaleDateString()}</span>
                      )}
                      {conn.autoClose && (
                        <span className="text-emerald-400">Auto-close on</span>
                      )}
                    </div>
                    {conn.defaultLabels?.length > 0 && (
                      <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                        {conn.defaultLabels.map((lbl) => (
                          <span
                            key={lbl}
                            className="text-[10px] px-1.5 py-0.5 rounded bg-sky-600/20 text-sky-300 border border-sky-500/20"
                          >
                            {lbl}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>

                <div className="flex items-center space-x-2 flex-shrink-0">
                  <button
                    onClick={() => handleTestConnection(conn)}
                    disabled={testingId === conn.id}
                    title="Test connection"
                    className="p-2 text-navy-400 hover:text-sky-300 hover:bg-navy-700 rounded-lg transition-colors disabled:opacity-50"
                  >
                    {testingId === conn.id ? (
                      <ArrowPathIcon className="w-4 h-4 animate-spin" />
                    ) : (
                      <SignalIcon className="w-4 h-4" />
                    )}
                  </button>
                  <button
                    onClick={() => handleOpenEdit(conn)}
                    title="Edit connection"
                    className="p-2 text-navy-400 hover:text-sky-300 hover:bg-navy-700 rounded-lg transition-colors"
                  >
                    <PencilSquareIcon className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => setConfirmDisconnect(conn)}
                    title="Disconnect"
                    className="p-2 text-navy-400 hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-colors"
                  >
                    <TrashIcon className="w-4 h-4" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default VendorGithubSync;
