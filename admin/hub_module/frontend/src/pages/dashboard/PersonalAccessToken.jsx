import { useState, useEffect } from 'react';
import { KeyIcon, ShieldCheckIcon, TrashIcon, EyeIcon, ClipboardDocumentIcon } from '@heroicons/react/24/outline';
import { tokenApi } from '../../services/api';

export default function PersonalAccessToken() {
  const [token, setToken] = useState(null);
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

  const [revoking, setRevoking] = useState(false);

  useEffect(() => {
    loadData();
  }, []);

  async function loadData() {
    setLoading(true);
    setError('');
    try {
      const [patRes, scopesRes] = await Promise.all([
        tokenApi.getPAT(),
        tokenApi.getPATScopes(),
      ]);
      setToken(patRes.data?.pat || null);
      setScopes(scopesRes.data?.scopes || []);
    } catch (err) {
      setError(err.message || 'Failed to load token data.');
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
    setCreating(true);
    setCreateError('');
    try {
      const payload = {
        name: createName.trim(),
        scope_ceiling: selectedScopes.length ? selectedScopes : null,
      };
      const res = await tokenApi.createPAT(payload);
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

  async function handleRevoke() {
    const confirmed = window.confirm(
      'Revoke your PAT? Any scripts using it will stop working.'
    );
    if (!confirmed) return;
    setRevoking(true);
    setError('');
    try {
      await tokenApi.revokePAT();
      setToken(null);
    } catch (err) {
      setError(err.message || 'Failed to revoke token.');
    } finally {
      setRevoking(false);
    }
  }

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(newTokenValue);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // fallback: select text manually
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

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48 text-navy-400">
        Loading…
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto py-8 px-4">
      <div className="flex items-center gap-3 mb-6">
        <KeyIcon className="h-7 w-7 text-gold-400" />
        <h1 className="text-2xl font-semibold text-sky-100">Personal Access Token</h1>
      </div>

      <p className="text-navy-400 mb-6 text-sm">
        Your Personal Access Token (PAT) lets you authenticate with the WaddleBot API from scripts and
        tools. You may have one active PAT at a time.
      </p>

      {error && (
        <p className="text-red-400 text-sm mb-4">{error}</p>
      )}

      {token ? (
        /* Token metadata card */
        <div className="bg-navy-900 border border-navy-700 rounded-xl p-6">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-center gap-3">
              <ShieldCheckIcon className="h-6 w-6 text-gold-400 flex-shrink-0" />
              <div>
                <p className="text-sky-100 font-medium">{token.name}</p>
                <p className="text-navy-400 text-xs mt-0.5">
                  Created {new Date(token.created_at).toLocaleDateString()}
                </p>
              </div>
            </div>
            <button
              onClick={handleRevoke}
              disabled={revoking}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm text-red-400 border border-red-700 hover:bg-red-900/30 transition-colors disabled:opacity-50"
            >
              <TrashIcon className="h-4 w-4" />
              {revoking ? 'Revoking…' : 'Revoke'}
            </button>
          </div>

          <div className="mt-4 grid grid-cols-2 gap-4 text-sm">
            <div>
              <p className="text-navy-400 text-xs uppercase tracking-wide mb-1">Last Used</p>
              <p className="text-sky-100">
                {token.last_used_at
                  ? new Date(token.last_used_at).toLocaleDateString()
                  : 'Never'}
              </p>
            </div>
            <div>
              <p className="text-navy-400 text-xs uppercase tracking-wide mb-1">Scope Ceiling</p>
              {token.scope_ceiling && token.scope_ceiling.length > 0 ? (
                <div className="flex flex-wrap gap-1">
                  {token.scope_ceiling.map((s) => (
                    <span
                      key={s}
                      className="text-xs bg-navy-800 border border-navy-700 text-navy-300 rounded px-1.5 py-0.5"
                    >
                      {s}
                    </span>
                  ))}
                </div>
              ) : (
                <p className="text-sky-100">Full permissions</p>
              )}
            </div>
          </div>
        </div>
      ) : (
        /* Empty state card */
        <div className="bg-navy-900 border border-navy-700 rounded-xl p-10 text-center">
          <KeyIcon className="h-12 w-12 text-navy-400 mx-auto mb-4" />
          <p className="text-sky-100 font-medium mb-1">No active token</p>
          <p className="text-navy-400 text-sm mb-6">
            Create a Personal Access Token to authenticate API requests from your scripts and tools.
          </p>
          <button
            onClick={openCreateModal}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-gold-400 text-navy-900 font-semibold text-sm hover:bg-gold-300 transition-colors"
          >
            <KeyIcon className="h-4 w-4" />
            Create Token
          </button>
        </div>
      )}

      {/* Create Token Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-navy-900 border border-navy-700 rounded-xl p-6 w-full max-w-lg mx-4">
            <h2 className="text-lg font-semibold text-sky-100 mb-4">Create Personal Access Token</h2>

            <form onSubmit={handleCreate} className="space-y-5">
              <div>
                <label className="block text-sm text-navy-400 mb-1.5">Token Name <span className="text-red-400">*</span></label>
                <input
                  type="text"
                  value={createName}
                  onChange={(e) => setCreateName(e.target.value)}
                  placeholder="e.g. My deploy script"
                  className="w-full bg-navy-800 border border-navy-700 rounded-lg px-3 py-2 text-sky-100 placeholder-navy-400 text-sm focus:outline-none focus:border-gold-400"
                />
              </div>

              <div>
                <p className="text-sm text-navy-400 mb-1">Scope Ceiling</p>
                <p className="text-xs text-navy-400 mb-3">
                  Leave unchecked to grant your full permissions.
                </p>
                {Object.entries(scopesByCategory).map(([category, categoryScopes]) => (
                  <div key={category} className="mb-3">
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
              <ShieldCheckIcon className="h-5 w-5 text-green-400" />
              <h2 className="text-lg font-semibold text-sky-100">Token Created</h2>
            </div>
            <p className="text-red-400 text-sm mb-4 font-medium">
              This token will not be shown again. Store it securely.
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
