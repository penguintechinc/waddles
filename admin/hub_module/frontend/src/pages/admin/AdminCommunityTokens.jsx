import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { CpuChipIcon, PlusIcon, TrashIcon, ClipboardDocumentIcon, ShieldExclamationIcon } from '@heroicons/react/24/outline';
import { tokenApi } from '../../services/api';

function ScopeBadge({ scope }) {
  return (
    <span className="text-xs bg-navy-800 border border-navy-700 text-navy-300 rounded px-1.5 py-0.5">
      {scope}
    </span>
  );
}

function ScopeBadgeList({ scopes }) {
  const MAX_VISIBLE = 3;
  if (!scopes || scopes.length === 0) {
    return <span className="text-navy-400 text-xs">No scopes</span>;
  }
  const visible = scopes.slice(0, MAX_VISIBLE);
  const overflow = scopes.length - MAX_VISIBLE;
  return (
    <div className="flex flex-wrap gap-1 items-center">
      {visible.map((s) => (
        <ScopeBadge key={s} scope={s} />
      ))}
      {overflow > 0 && (
        <span className="text-xs text-navy-400">+{overflow} more</span>
      )}
    </div>
  );
}

export default function AdminCommunityTokens() {
  const { communityId } = useParams();

  const [tokens, setTokens] = useState([]);
  const [quota, setQuota] = useState(null);
  const [scopes, setScopes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createName, setCreateName] = useState('');
  const [selectedScopes, setSelectedScopes] = useState([]);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');

  const [newTokenValue, setNewTokenValue] = useState('');
  const [showSuccessModal, setShowSuccessModal] = useState(false);
  const [copied, setCopied] = useState(false);

  const [revokingId, setRevokingId] = useState(null);

  useEffect(() => {
    if (communityId) loadData();
  }, [communityId]);

  async function loadData() {
    setLoading(true);
    setError('');
    try {
      const [listRes, scopesRes] = await Promise.all([
        tokenApi.listCATs(communityId),
        tokenApi.getCATScopes(communityId),
      ]);
      setTokens(listRes.data?.tokens || []);
      setQuota(listRes.data?.quota ?? null);
      setScopes(scopesRes.data?.scopes || []);
    } catch (err) {
      setError(err.message || 'Failed to load community tokens.');
    } finally {
      setLoading(false);
    }
  }

  function openCreateModal() {
    setCreateName('');
    setSelectedScopes([]);
    setCreateError('');
    setShowCreateModal(true);
  }

  function closeCreateModal() {
    setShowCreateModal(false);
  }

  function toggleScope(scopeKey) {
    setSelectedScopes((prev) =>
      prev.includes(scopeKey) ? prev.filter((s) => s !== scopeKey) : [...prev, scopeKey]
    );
  }

  async function handleCreate(e) {
    e.preventDefault();
    if (!createName.trim()) {
      setCreateError('Token name is required.');
      return;
    }
    if (selectedScopes.length === 0) {
      setCreateError('At least one scope must be selected.');
      return;
    }
    setCreating(true);
    setCreateError('');
    try {
      const res = await tokenApi.createCAT(communityId, {
        name: createName.trim(),
        scopes: selectedScopes,
      });
      setNewTokenValue(res.data.token);
      setShowCreateModal(false);
      setCopied(false);
      setShowSuccessModal(true);
    } catch (err) {
      setCreateError(err.message || 'Failed to create token.');
    } finally {
      setCreating(false);
    }
  }

  async function handleRevoke(token) {
    const confirmed = window.confirm(`Revoke token "${token.name}"?`);
    if (!confirmed) return;
    setRevokingId(token.id);
    try {
      await tokenApi.revokeCAT(communityId, token.id);
      setTokens((prev) => prev.filter((t) => t.id !== token.id));
    } catch (err) {
      setError(err.message || 'Failed to revoke token.');
    } finally {
      setRevokingId(null);
    }
  }

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(newTokenValue);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // fallback: user can select manually
    }
  }

  function handleSuccessDismiss() {
    setShowSuccessModal(false);
    setNewTokenValue('');
    loadData();
  }

  // Group scopes by category
  const scopesByCategory = scopes.reduce((acc, scope) => {
    const cat = scope.category || 'General';
    if (!acc[cat]) acc[cat] = [];
    acc[cat].push(scope);
    return acc;
  }, {});

  const atQuota = quota !== null && tokens.length >= quota;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48 text-navy-400">
        Loading…
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto py-8 px-4">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <CpuChipIcon className="h-7 w-7 text-gold-400" />
          <h1 className="text-2xl font-semibold text-sky-100">Community Access Tokens</h1>
        </div>
        <button
          onClick={openCreateModal}
          disabled={atQuota}
          title={atQuota ? 'Token quota reached' : undefined}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-gold-400 text-navy-900 font-semibold text-sm hover:bg-gold-300 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <PlusIcon className="h-4 w-4" />
          New Token
        </button>
      </div>

      {/* Quota indicator */}
      {quota !== null && (
        <div className="mb-5">
          <p className="text-sm text-navy-400">
            <span className={tokens.length >= quota ? 'text-red-400' : 'text-sky-100'}>
              {tokens.length}
            </span>
            <span> / {quota} tokens used</span>
          </p>
        </div>
      )}

      {error && (
        <p className="text-red-400 text-sm mb-4">{error}</p>
      )}

      {tokens.length === 0 ? (
        /* Empty state */
        <div className="bg-navy-900 border border-navy-700 rounded-xl p-10 text-center">
          <CpuChipIcon className="h-12 w-12 text-navy-400 mx-auto mb-4" />
          <p className="text-sky-100 font-medium mb-1">No community tokens yet</p>
          <p className="text-navy-400 text-sm max-w-md mx-auto">
            Community Access Tokens (CATs) are used by service accounts, bots, and integrations to
            authenticate as this community rather than as an individual user.
          </p>
        </div>
      ) : (
        /* Token table */
        <div className="bg-navy-900 border border-navy-700 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-navy-700">
                <th className="text-left text-xs text-navy-400 uppercase tracking-wide px-5 py-3 font-medium">Name</th>
                <th className="text-left text-xs text-navy-400 uppercase tracking-wide px-5 py-3 font-medium">Scopes</th>
                <th className="text-left text-xs text-navy-400 uppercase tracking-wide px-5 py-3 font-medium">Created By</th>
                <th className="text-left text-xs text-navy-400 uppercase tracking-wide px-5 py-3 font-medium">Last Used</th>
                <th className="text-left text-xs text-navy-400 uppercase tracking-wide px-5 py-3 font-medium">Created</th>
                <th className="px-5 py-3" />
              </tr>
            </thead>
            <tbody>
              {tokens.map((token, idx) => (
                <tr
                  key={token.id}
                  className={idx < tokens.length - 1 ? 'border-b border-navy-700' : ''}
                >
                  <td className="px-5 py-4 text-sky-100 font-medium">{token.name}</td>
                  <td className="px-5 py-4">
                    <ScopeBadgeList scopes={token.scopes} />
                  </td>
                  <td className="px-5 py-4 text-navy-400">{token.created_by_name || '—'}</td>
                  <td className="px-5 py-4 text-navy-400">
                    {token.last_used_at
                      ? new Date(token.last_used_at).toLocaleDateString()
                      : 'Never'}
                  </td>
                  <td className="px-5 py-4 text-navy-400">
                    {token.created_at
                      ? new Date(token.created_at).toLocaleDateString()
                      : '—'}
                  </td>
                  <td className="px-5 py-4 text-right">
                    <button
                      onClick={() => handleRevoke(token)}
                      disabled={revokingId === token.id}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm text-red-400 border border-red-700 hover:bg-red-900/30 transition-colors disabled:opacity-50 ml-auto"
                    >
                      <TrashIcon className="h-4 w-4" />
                      {revokingId === token.id ? 'Revoking…' : 'Revoke'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Create Token Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-navy-900 border border-navy-700 rounded-xl p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">Create Community Access Token</h2>

            <form onSubmit={handleCreate} className="space-y-5">
              <div>
                <label className="block text-sm text-navy-400 mb-1.5">
                  Token Name <span className="text-red-400">*</span>
                </label>
                <input
                  type="text"
                  value={createName}
                  onChange={(e) => setCreateName(e.target.value)}
                  placeholder="e.g. Event bot, Welcome service"
                  className="w-full bg-navy-800 border border-navy-700 rounded-lg px-3 py-2 text-sky-100 placeholder-navy-400 text-sm focus:outline-none focus:border-gold-400"
                />
              </div>

              <div>
                <p className="text-sm text-navy-400 mb-1">
                  Scopes <span className="text-red-400">*</span>
                </p>
                <p className="text-xs text-navy-400 mb-3">
                  Select the permissions this token should have. At least one scope is required.
                </p>
                {Object.entries(scopesByCategory).map(([category, categoryScopes]) => (
                  <div key={category} className="mb-4">
                    <p className="text-xs text-gold-400 uppercase tracking-wide mb-2">{category}</p>
                    <div className="space-y-1.5">
                      {categoryScopes.map((scope) => (
                        <label key={scope.scope_key} className="flex items-start gap-2.5 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={selectedScopes.includes(scope.scope_key)}
                            onChange={() => toggleScope(scope.scope_key)}
                            className="mt-0.5 accent-gold-400"
                          />
                          <span className="text-sm text-sky-100">{scope.scope_key}</span>
                          {scope.description && (
                            <span className="text-xs text-navy-400 ml-1">{scope.description}</span>
                          )}
                        </label>
                      ))}
                    </div>
                  </div>
                ))}
              </div>

              {createError && (
                <p className="text-red-400 text-sm">{createError}</p>
              )}

              <div className="flex items-center justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={closeCreateModal}
                  className="px-4 py-2 rounded-lg text-sm text-navy-400 hover:text-sky-100 transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={creating}
                  className="px-4 py-2 rounded-lg bg-gold-400 text-navy-900 font-semibold text-sm hover:bg-gold-300 transition-colors disabled:opacity-50"
                >
                  {creating ? 'Creating…' : 'Create Token'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Success Modal */}
      {showSuccessModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-navy-900 border border-navy-700 rounded-xl p-6 w-full max-w-lg mx-4">
            <div className="flex items-center gap-2 mb-2">
              <ShieldExclamationIcon className="h-5 w-5 text-yellow-400" />
              <h2 className="text-lg font-semibold text-sky-100">Token Created</h2>
            </div>
            <p className="text-yellow-400 text-sm mb-4 font-medium">
              This token grants community access. Treat it like a password. It will not be shown again.
            </p>

            <div className="bg-navy-800 border border-navy-700 rounded-lg p-4 mb-4">
              <p className="font-mono text-sm text-green-300 break-all select-all leading-relaxed">
                {newTokenValue}
              </p>
            </div>

            <div className="flex items-center justify-between gap-3">
              <button
                onClick={handleCopy}
                className="flex items-center gap-2 px-4 py-2 rounded-lg border border-navy-700 text-sm text-sky-100 hover:bg-navy-800 transition-colors"
              >
                <ClipboardDocumentIcon className="h-4 w-4" />
                {copied ? 'Copied!' : 'Copy'}
              </button>
              <button
                onClick={handleSuccessDismiss}
                className="px-4 py-2 rounded-lg bg-gold-400 text-navy-900 font-semibold text-sm hover:bg-gold-300 transition-colors"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
