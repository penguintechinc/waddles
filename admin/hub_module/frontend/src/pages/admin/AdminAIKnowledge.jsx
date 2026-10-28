/**
 * Admin AI Knowledge
 * Community-level AI support assistant knowledge source management
 */
import { useEffect, useState, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import {
  AcademicCapIcon,
  PlusIcon,
  PencilSquareIcon,
  TrashIcon,
  XMarkIcon,
  CheckCircleIcon,
  XCircleIcon,
  ExclamationTriangleIcon,
  ArrowPathIcon,
  BookOpenIcon,
  GlobeAltIcon,
  CodeBracketIcon,
  DocumentTextIcon,
  SparklesIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';

const SOURCE_TYPES = [
  { value: 'github_wiki', label: 'GitHub Wiki', icon: CodeBracketIcon },
  { value: 'github_repo_markdown', label: 'GitHub Repo Markdown', icon: CodeBracketIcon },
  { value: 'mkdocs', label: 'MkDocs', icon: BookOpenIcon },
  { value: 'docusaurus', label: 'Docusaurus', icon: BookOpenIcon },
  { value: 'generic_url', label: 'Generic URL', icon: GlobeAltIcon },
  { value: 'manual_kb', label: 'Manual Knowledge Base', icon: DocumentTextIcon },
];

const REFRESH_INTERVALS = [
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'on_demand', label: 'On Demand' },
];

const GITHUB_TYPES = ['github_wiki', 'github_repo_markdown'];

const EMPTY_FORM = {
  name: '',
  sourceType: 'github_wiki',
  url: '',
  branch: '',
  docsPath: '',
  refreshInterval: 'weekly',
  token: '',
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
          <button aria-label="Dismiss notification" onClick={() => onDismiss(t.id)} className="ml-2 opacity-60 hover:opacity-100">
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
      <div role="dialog" aria-modal="true" aria-labelledby="admin-aiknowledge-dialog-1-title" className="bg-navy-800 border border-navy-700 rounded-xl p-6 w-full max-w-sm shadow-2xl">
        <div className="flex items-start space-x-3 mb-4">
          <ExclamationTriangleIcon className="w-6 h-6 text-orange-400 flex-shrink-0 mt-0.5" />
          <div>
            <h3 id="admin-aiknowledge-dialog-1-title" className="text-white font-semibold">{title}</h3>
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
    indexed: { label: 'Indexed', classes: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20' },
    indexing: { label: 'Indexing', classes: 'text-sky-400 bg-sky-500/10 border-sky-500/20' },
    error: { label: 'Error', classes: 'text-red-400 bg-red-500/10 border-red-500/20' },
    pending: { label: 'Pending', classes: 'text-orange-400 bg-orange-500/10 border-orange-500/20' },
  };
  const cfg = configs[status] || configs.pending;
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${cfg.classes}`}>
      {cfg.label}
    </span>
  );
}

// ─── Source Type Icon ─────────────────────────────────────────────────────────

function SourceTypeIcon({ type }) {
  const match = SOURCE_TYPES.find((s) => s.value === type);
  const Icon = match?.icon || DocumentTextIcon;
  return <Icon className="w-5 h-5 text-sky-400" />;
}

// ─── Source Modal ─────────────────────────────────────────────────────────────

function SourceModal({ open, editTarget, communityId, onClose, onSaved, showToast }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      setForm(editTarget ? { ...EMPTY_FORM, ...editTarget, token: '' } : { ...EMPTY_FORM });
    }
  }, [open, editTarget]);

  if (!open) return null;

  const setField = (field, value) => setForm((prev) => ({ ...prev, [field]: value }));

  const isGithub = GITHUB_TYPES.includes(form.sourceType);
  const isGithubRepoMd = form.sourceType === 'github_repo_markdown';
  const isManual = form.sourceType === 'manual_kb';

  const urlLabel = isGithub
    ? 'Repository URL'
    : isManual
    ? 'Knowledge Base Title'
    : 'Documentation URL';

  const urlPlaceholder = isGithub
    ? 'https://github.com/owner/repo'
    : isManual
    ? 'e.g. Bot Setup Guide'
    : 'https://docs.example.com';

  const handleSubmit = async (e) => {
    e.preventDefault();
    try {
      setSaving(true);
      if (editTarget?.id) {
        await api.put(`/ai-knowledge/sources/${editTarget.id}`, { ...form, community_id: communityId });
        showToast('success', 'Source updated');
      } else {
        await api.post('/ai-knowledge/sources', { ...form, community_id: communityId });
        showToast('success', 'Knowledge source added');
      }
      onSaved();
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Failed to save source');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div role="dialog" aria-modal="true" aria-labelledby="admin-aiknowledge-dialog-2-title" className="bg-navy-800 border border-navy-700 rounded-xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-navy-700">
          <h2 id="admin-aiknowledge-dialog-2-title" className="text-lg font-bold text-white">
            {editTarget ? 'Edit Knowledge Source' : 'Add Knowledge Source'}
          </h2>
          <button aria-label="Close" onClick={onClose} className="text-navy-400 hover:text-white transition-colors">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-5">
          {/* Source Name */}
          <div>
            <label htmlFor="admin-aiknowledge-input" className="block text-sm font-medium text-navy-300 mb-1.5">
              Source Name <span className="text-red-400">*</span>
            </label>
            <input
              id="admin-aiknowledge-input"
              type="text"
              value={form.name}
              onChange={(e) => setField('name', e.target.value)}
              required
              placeholder="e.g. Bot Documentation"
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400 text-sm"
            />
          </div>

          {/* Source Type */}
          <div>
            <label htmlFor="admin-aiknowledge-source-type" className="block text-sm font-medium text-navy-300 mb-1.5">Source Type</label>
            <select
              id="admin-aiknowledge-source-type"
              value={form.sourceType}
              onChange={(e) => setField('sourceType', e.target.value)}
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white focus:border-gold-400 text-sm"
            >
              {SOURCE_TYPES.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </div>

          {/* URL / Repo */}
          {!isManual && (
            <div>
              <label htmlFor="admin-aiknowledge-input-2" className="block text-sm font-medium text-navy-300 mb-1.5">
                {urlLabel} <span className="text-red-400">*</span>
              </label>
              <input
                id="admin-aiknowledge-input-2"
                type={isManual ? 'text' : 'url'}
                value={form.url}
                onChange={(e) => setField('url', e.target.value)}
                required
                placeholder={urlPlaceholder}
                className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400 text-sm"
              />
            </div>
          )}

          {/* Branch (github types only) */}
          {isGithub && (
            <div>
              <label htmlFor="admin-aiknowledge-branch" className="block text-sm font-medium text-navy-300 mb-1.5">Branch</label>
              <input
                id="admin-aiknowledge-branch"
                type="text"
                value={form.branch}
                onChange={(e) => setField('branch', e.target.value)}
                placeholder="main"
                className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400 text-sm"
              />
            </div>
          )}

          {/* Docs Path (github_repo_markdown only) */}
          {isGithubRepoMd && (
            <div>
              <label htmlFor="admin-aiknowledge-docs-path" className="block text-sm font-medium text-navy-300 mb-1.5">Docs Path</label>
              <input
                id="admin-aiknowledge-docs-path"
                type="text"
                value={form.docsPath}
                onChange={(e) => setField('docsPath', e.target.value)}
                placeholder="docs/"
                className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400 text-sm"
              />
              <p className="text-xs text-navy-500 mt-1">Relative path within the repo to scan for Markdown files</p>
            </div>
          )}

          {/* Refresh Interval */}
          <div>
            <label htmlFor="admin-aiknowledge-refresh-interval" className="block text-sm font-medium text-navy-300 mb-1.5">Refresh Interval</label>
            <select
              id="admin-aiknowledge-refresh-interval"
              value={form.refreshInterval}
              onChange={(e) => setField('refreshInterval', e.target.value)}
              className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white focus:border-gold-400 text-sm"
            >
              {REFRESH_INTERVALS.map((r) => (
                <option key={r.value} value={r.value}>{r.label}</option>
              ))}
            </select>
          </div>

          {/* Token (optional, for private repos) */}
          {!isManual && (
            <div>
              <label htmlFor="admin-aiknowledge-access-token-optional-required-for-priva" className="block text-sm font-medium text-navy-300 mb-1.5">
                Access Token{' '}
                <span className="text-navy-500 font-normal">(optional — required for private repos)</span>
              </label>
              <input
                id="admin-aiknowledge-access-token-optional-required-for-priva"
                type="password"
                value={form.token}
                onChange={(e) => setField('token', e.target.value)}
                placeholder={editTarget ? '••••••••' : 'ghp_... or leave blank for public'}
                className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white placeholder-navy-500 focus:border-gold-400 text-sm"
              />
            </div>
          )}

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
              {saving ? 'Saving…' : editTarget ? 'Save Changes' : 'Add Source'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Score Bar ────────────────────────────────────────────────────────────────

function ScoreBar({ label, value, color = 'sky' }) {
  const percent = Math.round((value || 0) * 100);
  const colorMap = {
    sky: 'bg-sky-500',
    emerald: 'bg-emerald-500',
    gold: 'bg-gold-400',
    orange: 'bg-orange-500',
  };
  return (
    <div>
      <div className="flex justify-between items-center mb-1">
        <span className="text-sm text-navy-300">{label}</span>
        <span className="text-sm font-medium text-white">{percent}%</span>
      </div>
      <div className="w-full bg-navy-700 rounded-full h-2">
        <div
          className={`h-2 rounded-full transition-all ${colorMap[color] || colorMap.sky}`}
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

function AdminAIKnowledge() {
  const { communityId } = useParams();
  const [sources, setSources] = useState([]);
  const [settings, setSettings] = useState({
    confidenceThreshold: 0.7,
    autoReply: false,
    notifyOnNoMatch: true,
  });
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [savingSettings, setSavingSettings] = useState(false);
  const [toasts, setToasts] = useState([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editTarget, setEditTarget] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [reindexingId, setReindexingId] = useState(null);

  const showToast = useCallback((type, message) => {
    const id = Date.now();
    setToasts((prev) => [...prev, { id, type, message }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 4000);
  }, []);

  const dismissToast = (id) => setToasts((prev) => prev.filter((t) => t.id !== id));

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const [sourcesRes, settingsRes, healthRes] = await Promise.all([
        api.get('/ai-knowledge/sources', { params: { community_id: communityId } }),
        api.get(`/ai-knowledge/settings/${communityId}`).catch(() => ({ data: {} })),
        api.get(`/ai-knowledge/health/${communityId}`).catch(() => ({ data: null })),
      ]);
      setSources(sourcesRes.data?.sources || []);
      if (settingsRes.data?.settings) {
        setSettings((prev) => ({ ...prev, ...settingsRes.data.settings }));
      }
      setHealth(healthRes.data?.health || null);
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Failed to load AI knowledge data');
    } finally {
      setLoading(false);
    }
  }, [communityId, showToast]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleOpenAdd = () => {
    setEditTarget(null);
    setModalOpen(true);
  };

  const handleOpenEdit = (source) => {
    setEditTarget(source);
    setModalOpen(true);
  };

  const handleModalSaved = () => {
    setModalOpen(false);
    loadData();
  };

  const handleDelete = async () => {
    if (!confirmDelete) return;
    try {
      await api.delete(`/ai-knowledge/sources/${confirmDelete.id}`);
      showToast('success', `Deleted "${confirmDelete.name}"`);
      loadData();
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Failed to delete source');
    } finally {
      setConfirmDelete(null);
    }
  };

  const handleReindex = async (source) => {
    try {
      setReindexingId(source.id);
      await api.post(`/ai-knowledge/sources/${source.id}/reindex`);
      showToast('success', `Reindex triggered for "${source.name}"`);
      loadData();
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Failed to trigger reindex');
    } finally {
      setReindexingId(null);
    }
  };

  const handleSaveSettings = async () => {
    try {
      setSavingSettings(true);
      await api.put(`/ai-knowledge/settings/${communityId}`, settings);
      showToast('success', 'Settings saved');
    } catch (err) {
      showToast('error', err.response?.data?.error?.message || 'Failed to save settings');
    } finally {
      setSavingSettings(false);
    }
  };

  const setSettingsField = (field, value) =>
    setSettings((prev) => ({ ...prev, [field]: value }));

  const sourceTypeLabel = (val) => SOURCE_TYPES.find((s) => s.value === val)?.label || val;
  const refreshLabel = (val) => REFRESH_INTERVALS.find((r) => r.value === val)?.label || val;

  return (
    <div className="space-y-8 max-w-4xl">
      <Toast toasts={toasts} onDismiss={dismissToast} />

      <ConfirmDialog
        open={!!confirmDelete}
        title="Delete Knowledge Source"
        message={confirmDelete ? `Remove "${confirmDelete.name}"? This cannot be undone.` : ''}
        confirmLabel="Delete"
        danger
        onConfirm={handleDelete}
        onCancel={() => setConfirmDelete(null)}
      />

      <SourceModal
        open={modalOpen}
        editTarget={editTarget}
        communityId={communityId}
        onClose={() => setModalOpen(false)}
        onSaved={handleModalSaved}
        showToast={showToast}
      />

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <AcademicCapIcon className="w-8 h-8 text-gold-400" />
          <div>
            <div className="flex items-center space-x-3">
              <h1 className="text-3xl font-bold text-white">AI Support Assistant</h1>
              <span className="text-[11px] px-2 py-0.5 rounded bg-gold-500 text-navy-900 font-bold uppercase tracking-wider">
                Premium
              </span>
            </div>
            <p className="text-navy-300 mt-1">Manage knowledge sources for AI-powered ticket responses</p>
          </div>
        </div>
        <button
          onClick={handleOpenAdd}
          className="flex items-center space-x-2 bg-sky-600 hover:bg-sky-700 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
        >
          <PlusIcon className="w-4 h-4" />
          <span>Add Source</span>
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-32">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400" />
        </div>
      ) : (
        <>
          {/* Knowledge Sources */}
          <div>
            <h2 className="text-lg font-bold text-white mb-4">Knowledge Sources</h2>
            {sources.length === 0 ? (
              <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 flex flex-col items-center justify-center text-center">
                <BookOpenIcon className="w-12 h-12 text-navy-600 mb-3" />
                <p className="text-navy-300 font-medium">No knowledge sources configured</p>
                <p className="text-navy-500 text-sm mt-1">
                  Add documentation sources to train the AI support assistant
                </p>
                <button
                  onClick={handleOpenAdd}
                  className="mt-4 flex items-center space-x-2 bg-sky-600 hover:bg-sky-700 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
                >
                  <PlusIcon className="w-4 h-4" />
                  <span>Add Source</span>
                </button>
              </div>
            ) : (
              <div className="space-y-3">
                {sources.map((source) => (
                  <div
                    key={source.id}
                    className="bg-navy-800 border border-navy-700 rounded-lg p-5 flex items-center justify-between gap-4"
                  >
                    <div className="flex items-start space-x-4 min-w-0">
                      <div className="w-10 h-10 rounded-lg bg-navy-700 border border-navy-600 flex items-center justify-center flex-shrink-0">
                        <SourceTypeIcon type={source.sourceType} />
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center space-x-2 flex-wrap gap-y-1">
                          <p className="text-white font-semibold">{source.name}</p>
                          <StatusBadge status={source.status || 'pending'} />
                        </div>
                        <p className="text-xs text-navy-400 mt-0.5 truncate max-w-xs">
                          {sourceTypeLabel(source.sourceType)}
                          {source.url && ` · ${source.url}`}
                        </p>
                        <div className="flex items-center space-x-3 mt-1 text-xs text-navy-500 flex-wrap gap-y-0.5">
                          <span>{refreshLabel(source.refreshInterval)}</span>
                          {source.pageCount != null && (
                            <span>{source.pageCount} pages indexed</span>
                          )}
                          {source.lastIndexed && (
                            <span>Last indexed {new Date(source.lastIndexed).toLocaleDateString()}</span>
                          )}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center space-x-2 flex-shrink-0">
                      <button
                        onClick={() => handleReindex(source)}
                        disabled={reindexingId === source.id}
                        title="Reindex"
                        className="p-2 text-navy-400 hover:text-sky-300 hover:bg-navy-700 rounded-lg transition-colors disabled:opacity-50"
                      >
                        <ArrowPathIcon
                          className={`w-4 h-4 ${reindexingId === source.id ? 'animate-spin' : ''}`}
                        />
                      </button>
                      <button
                        onClick={() => handleOpenEdit(source)}
                        title="Edit"
                        className="p-2 text-navy-400 hover:text-sky-300 hover:bg-navy-700 rounded-lg transition-colors"
                      >
                        <PencilSquareIcon className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => setConfirmDelete(source)}
                        title="Delete"
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

          {/* Settings */}
          <div className="bg-navy-800 border border-navy-700 rounded-lg p-6 space-y-6">
            <h2 className="text-lg font-bold text-white border-b border-navy-700 pb-3">
              Assistant Settings
            </h2>

            {/* Confidence Threshold */}
            <div>
              <div className="flex justify-between items-center mb-2">
                <label className="block text-sm font-medium text-navy-300">
                  Confidence Threshold
                </label>
                <span className="text-sm font-mono text-white">
                  {settings.confidenceThreshold.toFixed(2)}
                </span>
              </div>
              <input
                aria-label="Confidence threshold"
                type="range"
                min="0.5"
                max="1.0"
                step="0.05"
                value={settings.confidenceThreshold}
                onChange={(e) => setSettingsField('confidenceThreshold', parseFloat(e.target.value))}
                className="w-full accent-sky-500"
              />
              <div className="flex justify-between text-xs text-navy-500 mt-1">
                <span>0.5 — More answers, less accurate</span>
                <span>1.0 — Fewer answers, highly confident</span>
              </div>
            </div>

            {/* Toggles */}
            <div className="space-y-3">
              {[
                {
                  key: 'autoReply',
                  label: 'Auto-reply on new tickets',
                  description: 'AI automatically replies when a match is found above the confidence threshold',
                },
                {
                  key: 'notifyOnNoMatch',
                  label: 'Notify admin on no match',
                  description: 'Send an alert when AI cannot find a suitable answer',
                },
              ].map(({ key, label, description }) => (
                <label key={key} className="flex items-start space-x-3 cursor-pointer group">
                  <div className="pt-0.5">
                    <input
                      type="checkbox"
                      checked={settings[key] ?? false}
                      onChange={(e) => setSettingsField(key, e.target.checked)}
                      className="w-4 h-4 rounded border-navy-600 bg-navy-900 text-sky-500 focus:ring-sky-500"
                    />
                  </div>
                  <div>
                    <p className="text-white text-sm font-medium group-hover:text-sky-300 transition-colors">
                      {label}
                    </p>
                    <p className="text-xs text-navy-400">{description}</p>
                  </div>
                </label>
              ))}
            </div>

            <div className="flex justify-end">
              <button
                onClick={handleSaveSettings}
                disabled={savingSettings}
                className="bg-sky-600 hover:bg-sky-700 disabled:opacity-50 disabled:cursor-not-allowed text-white px-5 py-2 rounded-lg text-sm font-medium transition-colors"
              >
                {savingSettings ? 'Saving…' : 'Save Settings'}
              </button>
            </div>
          </div>

          {/* Health Dashboard */}
          <div className="bg-navy-800 border border-navy-700 rounded-lg p-6 space-y-4">
            <div className="flex items-center space-x-2 border-b border-navy-700 pb-3">
              <SparklesIcon className="w-5 h-5 text-gold-400" />
              <h2 className="text-lg font-bold text-white">Health Dashboard</h2>
            </div>

            {health ? (
              <div className="space-y-4">
                <ScoreBar
                  label="Index Freshness"
                  value={health.indexFreshness ?? 0}
                  color="sky"
                />
                <ScoreBar
                  label="Helpfulness Score"
                  value={health.helpfulnessScore ?? 0}
                  color="emerald"
                />
                <ScoreBar
                  label="Match Rate (last 30 days)"
                  value={health.matchRate ?? 0}
                  color="gold"
                />
                {health.noMatchCount != null && (
                  <p className="text-xs text-navy-400">
                    {health.noMatchCount} tickets with no match in the last 30 days
                  </p>
                )}
              </div>
            ) : (
              <p className="text-navy-400 text-sm">
                Health data will appear after the AI assistant processes its first ticket.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export default AdminAIKnowledge;
