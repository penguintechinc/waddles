import { useState, useEffect, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { superAdminApi } from '../../services/api';
import { TIER_BADGE_CLASS, TIER_LABEL, TIER_OPTIONS, REASON_CODES, REASON_LABEL, normalizeTier } from '../../services/communityTierConstants';
import { getPlatformIcon, getPlatformColor, getPlatformLabel, getAllPlatformOptions } from '../../utils/platformConfig';

function SuperAdminCommunities() {
  const [communities, setCommunities] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [platform, setPlatform] = useState('');
  const [isActive, setIsActive] = useState('');
  const [page, setPage] = useState(1);
  const [pagination, setPagination] = useState({ total: 0, totalPages: 0 });
  const [editingCommunity, setEditingCommunity] = useState(null);
  const [reassignModal, setReassignModal] = useState(null);
  const [deleteModal, setDeleteModal] = useState(null);

  const loadCommunities = useCallback(async () => {
    try {
      setLoading(true);
      const params = { page, limit: 25 };
      if (search) params.search = search;
      if (platform) params.platform = platform;
      if (isActive !== '') params.isActive = isActive;

      const response = await superAdminApi.getCommunities(params);
      if (response.data.success) {
        setCommunities(response.data.communities);
        setPagination(response.data.pagination);
      }
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load communities');
    } finally {
      setLoading(false);
    }
  }, [page, search, platform, isActive]);

  useEffect(() => {
    loadCommunities();
  }, [loadCommunities]);

  const handleSearch = (e) => {
    e.preventDefault();
    setPage(1);
    loadCommunities();
  };

  /** Save an edit; returns an error message to show inside the dialog, or null on success/no-op. */
  const handleUpdate = async (id, data) => {
    try {
      const response = await superAdminApi.updateCommunity(id, data);
      if (response.data.success) {
        setEditingCommunity(null);
        loadCommunities();
      }
      return null;
    } catch (err) {
      const status = err.response?.status;
      const message = err.response?.data?.error?.message;
      if (status === 403 && data.licenseTier !== undefined) {
        return message || "Changing a community's tier requires global admin access (users:admin).";
      }
      return message || 'Failed to update community';
    }
  };

  const handleReassign = async (id, newOwnerId, newOwnerName) => {
    try {
      const response = await superAdminApi.reassignOwner(id, { newOwnerId, newOwnerName });
      if (response.data.success) {
        setReassignModal(null);
        loadCommunities();
      }
    } catch (err) {
      alert(err.response?.data?.error?.message || 'Failed to reassign owner');
    }
  };

  const handleDelete = async (id) => {
    try {
      const response = await superAdminApi.deleteCommunity(id);
      if (response.data.success) {
        setDeleteModal(null);
        loadCommunities();
      }
    } catch (err) {
      alert(err.response?.data?.error?.message || 'Failed to delete community');
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold gradient-text">Manage Communities</h1>
        <Link to="/communities/create" className="btn btn-primary">
          + Create Community
        </Link>
      </div>

      {/* Filters */}
      <div className="card p-4 mb-6">
        <form onSubmit={handleSearch} className="flex flex-wrap gap-4">
          <input
            aria-label="Search communities"
            type="text"
            placeholder="Search communities..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input flex-1 min-w-[200px]"
          />
          <select
            aria-label="Filter by platforms"
            value={platform}
            onChange={(e) => { setPlatform(e.target.value); setPage(1); }}
            className="input w-40"
          >
            <option value="">All Platforms</option>
            {getAllPlatformOptions()
              .filter((p) => p.id !== 'hub')
              .map((p) => (
                <option key={p.id} value={p.id}>
                  {p.icon} {p.label}
                </option>
              ))}
          </select>
          <select
            aria-label="Filter by status"
            value={isActive}
            onChange={(e) => { setIsActive(e.target.value); setPage(1); }}
            className="input w-32"
          >
            <option value="">All Status</option>
            <option value="true">Active</option>
            <option value="false">Inactive</option>
          </select>
          <button type="submit" className="btn btn-secondary">Search</button>
        </form>
      </div>

      {error && (
        <div className="p-4 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 mb-6">
          {error}
        </div>
      )}

      {/* Communities Table */}
      <div className="card overflow-hidden">
        <table>
          <thead>
            <tr>
              <th>Community</th>
              <th>Platform</th>
              <th>Owner</th>
              <th>Members</th>
              <th>Plan</th>
              <th>Status</th>
              <th>Created</th>
              <th className="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={8} className="p-8 text-center">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400 mx-auto"></div>
                </td>
              </tr>
            ) : communities.length === 0 ? (
              <tr>
                <td colSpan={8} className="p-8 text-center text-navy-400">
                  No communities found
                </td>
              </tr>
            ) : (
              communities.map((community) => (
                <tr key={community.id}>
                  <td>
                    <div className="font-medium text-sky-100">{community.displayName}</div>
                    <div className="text-sm text-navy-400">{community.name}</div>
                  </td>
                  <td>
                    <span className={`badge border ${getPlatformColor(community.platform)}`}>
                      {getPlatformIcon(community.platform)} {getPlatformLabel(community.platform)}
                    </span>
                  </td>
                  <td>
                    {community.ownerName || <span className="text-navy-500">Unassigned</span>}
                  </td>
                  <td>{community.memberCount}</td>
                  <td>
                    <span
                      className={`badge border ${TIER_BADGE_CLASS[normalizeTier(community.licenseTier ?? (community.isPremium ? 2 : 1))]}`}
                    >
                      {TIER_LABEL[normalizeTier(community.licenseTier ?? (community.isPremium ? 2 : 1))]}
                    </span>
                  </td>
                  <td>
                    <span className={`badge ${
                      community.isActive ? 'badge-green' : 'badge-red'
                    }`}>
                      {community.isActive ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td className="text-sm text-navy-400">
                    {new Date(community.createdAt).toLocaleDateString()}
                  </td>
                  <td className="text-right">
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => setEditingCommunity(community)}
                        className="text-sky-400 hover:text-sky-300 text-sm"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => setReassignModal(community)}
                        className="text-gold-400 hover:text-gold-300 text-sm"
                      >
                        Reassign
                      </button>
                      <button
                        onClick={() => setDeleteModal(community)}
                        className="text-red-400 hover:text-red-300 text-sm"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>

        {/* Pagination */}
        {pagination.totalPages > 1 && (
          <div className="flex items-center justify-between p-4 border-t border-navy-700">
            <div className="text-sm text-navy-400">
              Showing {((page - 1) * 25) + 1} to {Math.min(page * 25, pagination.total)} of {pagination.total}
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page === 1}
                className="btn btn-secondary text-sm disabled:opacity-50"
              >
                Previous
              </button>
              <button
                onClick={() => setPage(p => Math.min(pagination.totalPages, p + 1))}
                disabled={page === pagination.totalPages}
                className="btn btn-secondary text-sm disabled:opacity-50"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Edit Modal */}
      {editingCommunity && (
        <EditCommunityModal
          community={editingCommunity}
          onClose={() => setEditingCommunity(null)}
          onSave={handleUpdate}
        />
      )}

      {/* Reassign Modal */}
      {reassignModal && (
        <ReassignOwnerModal
          community={reassignModal}
          onClose={() => setReassignModal(null)}
          onReassign={handleReassign}
        />
      )}

      {/* Delete Modal */}
      {deleteModal && (
        <DeleteCommunityModal
          community={deleteModal}
          onClose={() => setDeleteModal(null)}
          onDelete={handleDelete}
        />
      )}
    </div>
  );
}

/**
 * Parse the Seat Limit field. Blank means unlimited (null); otherwise a whole number >= 1.
 * Returns { value, error } so the dialog can show an inline field error.
 */
export function parseSeatLimit(raw) {
  const text = String(raw ?? '').trim();
  if (text === '') return { value: null, error: null };
  if (!/^\d+$/.test(text) || Number(text) < 1) {
    return { value: null, error: 'Seat limit must be a whole number of 1 or more (leave blank for unlimited).' };
  }
  return { value: Number(text), error: null };
}

/**
 * Builds the PUT body from the edit form. `seatLimit` and `licenseTier` are sent only when they differ
 * from the loaded value (absent = unchanged on the server, explicit null = unlimited); a cleared
 * description is sent as null; the tier always travels with its reason. `isPremium` is never sent (the server derives it).
 */
function buildUpdatePayload(community, form, seatValue, reasonCode) {
  const payload = {
    displayName: form.displayName,
    description: form.description === '' ? null : form.description,
    isActive: form.isActive,
    isPublic: form.isPublic,
  };
  const loadedSeat = community.seatLimit ?? null;
  if (seatValue !== loadedSeat) payload.seatLimit = seatValue;
  if (form.licenseTier !== loadedTierOf(community)) {
    payload.licenseTier = form.licenseTier;
    payload.reasonCode = reasonCode;
  }
  return payload;
}

/** The community's current tier code; falls back to the legacy premium flag for older payloads. */
function loadedTierOf(community) {
  return normalizeTier(community.licenseTier ?? (community.isPremium ? 2 : 1));
}

function EditCommunityModal({ community, onClose, onSave }) {
  const loadedTier = loadedTierOf(community);
  const [form, setForm] = useState({
    displayName: community.displayName || '',
    description: community.description || '',
    isActive: community.isActive,
    isPublic: community.isPublic,
    licenseTier: loadedTier,
    seatLimit: community.seatLimit ?? '',
  });
  const [reasonCode, setReasonCode] = useState('');
  const [saving, setSaving] = useState(false);
  const [seatError, setSeatError] = useState(null);
  const [reasonError, setReasonError] = useState(null);
  const [saveError, setSaveError] = useState(null);
  const tierChanged = form.licenseTier !== loadedTier;

  const handleSubmit = async (e) => {
    e.preventDefault();
    const seat = parseSeatLimit(form.seatLimit);
    setSeatError(seat.error);
    setSaveError(null);
    const missingReason = tierChanged && !reasonCode;
    setReasonError(missingReason ? 'Select a reason for the tier change.' : null);
    if (seat.error || missingReason) return;
    setSaving(true);
    const message = await onSave(community.id, buildUpdatePayload(community, form, seat.value, reasonCode));
    if (message) setSaveError(message);
    setSaving(false);
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
      <div role="dialog" aria-modal="true" aria-labelledby="super-admin-communities-dialog-1-title" className="bg-navy-900 rounded-xl shadow-xl max-w-md w-full mx-4 border border-navy-700">
        <div className="p-6 border-b border-navy-700">
          <h2 id="super-admin-communities-dialog-1-title" className="text-xl font-semibold text-sky-100">Edit Community</h2>
        </div>
        <form onSubmit={handleSubmit} noValidate>
          <div className="p-6 space-y-4">
            <div>
              <label htmlFor="super-admin-communities-display-name" className="block text-sm font-medium text-sky-200 mb-1">Display Name</label>
              <input
                id="super-admin-communities-display-name"
                type="text"
                value={form.displayName}
                onChange={(e) => setForm({ ...form, displayName: e.target.value })}
                className="input w-full"
              />
            </div>
            <div>
              <label htmlFor="super-admin-communities-description" className="block text-sm font-medium text-sky-200 mb-1">Description</label>
              <textarea
                id="super-admin-communities-description"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                className="input w-full"
                rows={3}
              />
            </div>
            <div className="flex gap-4 flex-wrap">
              <label className="flex items-center gap-2 text-sky-200">
                <input
                  type="checkbox"
                  checked={form.isActive}
                  onChange={(e) => setForm({ ...form, isActive: e.target.checked })}
                  className="w-4 h-4 rounded bg-navy-800 border-navy-600"
                />
                <span className="text-sm">Active</span>
              </label>
              <label className="flex items-center gap-2 text-sky-200">
                <input
                  type="checkbox"
                  checked={form.isPublic}
                  onChange={(e) => setForm({ ...form, isPublic: e.target.checked })}
                  className="w-4 h-4 rounded bg-navy-800 border-navy-600"
                />
                <span className="text-sm">Public</span>
              </label>
            </div>
            <div>
              <label htmlFor="super-admin-communities-license-tier" className="block text-sm font-medium text-sky-200 mb-1">License tier</label>
              <select
                id="super-admin-communities-license-tier"
                value={form.licenseTier}
                onChange={(e) => {
                  setForm({ ...form, licenseTier: Number(e.target.value) });
                  setReasonError(null);
                }}
                className="input w-full"
                aria-describedby="super-admin-communities-license-tier-help"
              >
                {TIER_OPTIONS.map((t) => (
                  <option key={t} value={t}>{TIER_LABEL[t]}</option>
                ))}
              </select>
              <p id="super-admin-communities-license-tier-help" className="text-xs mt-1 text-sky-400">
                {"Operator override: not blocked by the tenant's pool; counts against it and is audited."}
              </p>
            </div>
            {tierChanged && (
              <div>
                <label htmlFor="super-admin-communities-reason-code" className="block text-sm font-medium text-sky-200 mb-1">Reason for tier change</label>
                <select
                  id="super-admin-communities-reason-code"
                  value={reasonCode}
                  onChange={(e) => { setReasonCode(e.target.value); setReasonError(null); }}
                  className="input w-full"
                  aria-invalid={reasonError ? 'true' : undefined}
                  aria-describedby="super-admin-communities-reason-code-help"
                >
                  <option value="">Select a reason...</option>
                  {REASON_CODES.map((r) => (
                    <option key={r} value={r}>{REASON_LABEL[r]}</option>
                  ))}
                </select>
                <p
                  id="super-admin-communities-reason-code-help"
                  className={`text-xs mt-1 ${reasonError ? 'text-red-400' : 'text-sky-400'}`}
                  role={reasonError ? 'alert' : undefined}
                >
                  {reasonError || 'Required: recorded in the audit trail.'}
                </p>
              </div>
            )}
            <div>
              <label htmlFor="super-admin-communities-seat-limit" className="block text-sm font-medium text-sky-200 mb-1">Seat Limit</label>
              <input
                id="super-admin-communities-seat-limit"
                type="text"
                inputMode="numeric"
                value={form.seatLimit}
                onChange={(e) => setForm({ ...form, seatLimit: e.target.value })}
                className="input w-full"
                placeholder="Leave blank for unlimited"
                aria-invalid={seatError ? 'true' : undefined}
                aria-describedby="super-admin-communities-seat-limit-help"
              />
              <p
                id="super-admin-communities-seat-limit-help"
                className={`text-xs mt-1 ${seatError ? 'text-red-400' : 'text-sky-400'}`}
                role={seatError ? 'alert' : undefined}
              >
                {seatError || 'Blank = unlimited seats.'}
              </p>
            </div>
            {saveError && (
              <div role="alert" className="text-sm text-red-400 border border-red-500/40 rounded p-2">
                {saveError}
              </div>
            )}
          </div>
          <div className="p-6 border-t border-navy-700 flex justify-end gap-3">
            <button type="button" onClick={onClose} className="btn btn-secondary">
              Cancel
            </button>
            <button type="submit" disabled={saving} className="btn btn-primary">
              {saving ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function ReassignOwnerModal({ community, onClose, onReassign }) {
  const [newOwnerName, setNewOwnerName] = useState('');
  const [newOwnerId, setNewOwnerId] = useState('');
  const [saving, setSaving] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!newOwnerName.trim()) {
      alert('Owner name is required');
      return;
    }
    setSaving(true);
    await onReassign(community.id, newOwnerId || null, newOwnerName);
    setSaving(false);
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
      <div role="dialog" aria-modal="true" aria-labelledby="super-admin-communities-dialog-2-title" className="bg-navy-900 rounded-xl shadow-xl max-w-md w-full mx-4 border border-navy-700">
        <div className="p-6 border-b border-navy-700">
          <h2 id="super-admin-communities-dialog-2-title" className="text-xl font-semibold text-gold-400">Reassign Owner</h2>
          <p className="text-sm text-navy-400 mt-1">
            Reassigning ownership of: <strong className="text-sky-100">{community.displayName}</strong>
          </p>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="p-6 space-y-4">
            <div className="p-3 bg-gold-500/20 border border-gold-500/30 rounded-lg text-gold-300 text-sm">
              Current owner: {community.ownerName || 'Unassigned'}
            </div>
            <div>
              <label htmlFor="super-admin-communities-new-owner-name" className="block text-sm font-medium text-sky-200 mb-1">New Owner Name *</label>
              <input
                id="super-admin-communities-new-owner-name"
                type="text"
                value={newOwnerName}
                onChange={(e) => setNewOwnerName(e.target.value)}
                className="input w-full"
                placeholder="Enter new owner's name"
                required
              />
            </div>
            <div>
              <label htmlFor="super-admin-communities-new-owner-id-optional" className="block text-sm font-medium text-sky-200 mb-1">New Owner ID (optional)</label>
              <input
                id="super-admin-communities-new-owner-id-optional"
                type="text"
                value={newOwnerId}
                onChange={(e) => setNewOwnerId(e.target.value)}
                className="input w-full"
                placeholder="Platform user ID"
              />
            </div>
          </div>
          <div className="p-6 border-t border-navy-700 flex justify-end gap-3">
            <button type="button" onClick={onClose} className="btn btn-secondary">
              Cancel
            </button>
            <button type="submit" disabled={saving} className="btn btn-primary">
              {saving ? 'Reassigning...' : 'Reassign Owner'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function DeleteCommunityModal({ community, onClose, onDelete }) {
  const [confirmName, setConfirmName] = useState('');
  const [deleting, setDeleting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (confirmName !== community.name) {
      alert('Community name does not match');
      return;
    }
    setDeleting(true);
    await onDelete(community.id);
    setDeleting(false);
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
      <div role="dialog" aria-modal="true" aria-labelledby="super-admin-communities-dialog-3-title" className="bg-navy-900 rounded-xl shadow-xl max-w-md w-full mx-4 border border-navy-700">
        <div className="p-6 border-b border-navy-700">
          <h2 id="super-admin-communities-dialog-3-title" className="text-xl font-semibold text-red-400">Delete Community</h2>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="p-6 space-y-4">
            <div className="p-3 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 text-sm">
              This action will deactivate the community. This cannot be undone.
            </div>
            <p className="text-sky-100">
              To confirm deletion, please type the community name:{' '}
              <strong className="text-red-400">{community.name}</strong>
            </p>
            <input
              aria-label="Type community name to confirm"
              type="text"
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
              className="input w-full"
              placeholder="Type community name to confirm"
            />
          </div>
          <div className="p-6 border-t border-navy-700 flex justify-end gap-3">
            <button type="button" onClick={onClose} className="btn btn-secondary">
              Cancel
            </button>
            <button
              type="submit"
              disabled={deleting || confirmName !== community.name}
              className="btn btn-danger disabled:opacity-50"
            >
              {deleting ? 'Deleting...' : 'Delete Community'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default SuperAdminCommunities;
