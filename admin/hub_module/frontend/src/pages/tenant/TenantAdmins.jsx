import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { tenantApi } from '../../services/api';

const ROLE_OPTIONS = [
  { value: 'tenant-admin', label: 'Tenant Admin' },
  { value: 'tenant-owner', label: 'Tenant Owner' },
];

const ROLE_BADGE = {
  'tenant-owner': 'bg-gold-500/20 text-gold-400 border-gold-500/30',
  'tenant-admin': 'bg-sky-500/20 text-sky-300 border-sky-500/30',
};

const ROLE_LABEL = {
  'tenant-owner': 'Owner',
  'tenant-admin': 'Admin',
};

function TenantAdmins() {
  const { tenantSlug } = useParams();
  const [admins, setAdmins] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Add form state
  const [addForm, setAddForm] = useState({ userId: '', role: 'tenant-admin' });
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState(null);
  const [addSuccess, setAddSuccess] = useState(false);

  // Remove confirmation state
  const [removingId, setRemovingId] = useState(null);
  const [confirmRemoveId, setConfirmRemoveId] = useState(null);
  const [removeError, setRemoveError] = useState(null);

  useEffect(() => {
    loadAdmins();
  }, [tenantSlug]);

  const loadAdmins = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await tenantApi.getAdmins(tenantSlug);
      setAdmins(res.data.admins || []);
    } catch (err) {
      setError(err?.response?.data?.error || 'Failed to load admins.');
    }
    setLoading(false);
  };

  const handleAdd = async (e) => {
    e.preventDefault();
    if (!addForm.userId.trim()) return;
    setAdding(true);
    setAddError(null);
    setAddSuccess(false);
    try {
      await tenantApi.addAdmin(tenantSlug, {
        userId: addForm.userId.trim(),
        role: addForm.role,
      });
      setAddForm({ userId: '', role: 'tenant-admin' });
      setAddSuccess(true);
      setTimeout(() => setAddSuccess(false), 3000);
      await loadAdmins();
    } catch (err) {
      setAddError(err?.response?.data?.error || 'Failed to add admin.');
    }
    setAdding(false);
  };

  const handleRemoveConfirm = (adminId) => {
    setConfirmRemoveId(adminId);
    setRemoveError(null);
  };

  const handleRemoveCancel = () => {
    setConfirmRemoveId(null);
    setRemoveError(null);
  };

  const handleRemove = async (adminId) => {
    setRemovingId(adminId);
    setRemoveError(null);
    try {
      await tenantApi.removeAdmin(tenantSlug, adminId);
      setConfirmRemoveId(null);
      await loadAdmins();
    } catch (err) {
      setRemoveError(err?.response?.data?.error || 'Failed to remove admin.');
    }
    setRemovingId(null);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-64">
        <div className="animate-spin rounded-full h-10 w-10 border-t-2 border-b-2 border-gold-400" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <div className="bg-red-900/30 border border-red-700 rounded-xl p-4 text-red-300">
          {error}
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-sky-100 text-2xl font-bold">Tenant Admins</h1>
        <p className="text-sky-400 text-sm mt-1">
          Manage administrators for this tenant
        </p>
      </div>

      {/* Add Admin Form */}
      <div className="bg-navy-800 border border-navy-700 rounded-xl p-6">
        <h2 className="text-sky-100 text-lg font-semibold mb-4">Add Administrator</h2>
        {addError && (
          <div className="mb-4 bg-red-900/30 border border-red-700 rounded-lg p-3 text-red-300 text-sm">
            {addError}
          </div>
        )}
        {addSuccess && (
          <div className="mb-4 bg-green-900/30 border border-green-700 rounded-lg p-3 text-green-300 text-sm">
            Admin added successfully.
          </div>
        )}
        <form onSubmit={handleAdd} className="flex flex-col sm:flex-row gap-3">
          <div className="flex-1">
            <label htmlFor="tenant-admins-user-id-or-username" className="block text-sky-300 text-xs font-medium mb-1">
              User ID or Username
            </label>
            <input
              id="tenant-admins-user-id-or-username"
              type="text"
              value={addForm.userId}
              onChange={(e) => setAddForm((f) => ({ ...f, userId: e.target.value }))}
              placeholder="Enter user ID or username"
              required
              className="w-full bg-navy-700 border border-navy-600 rounded-lg px-3 py-2 text-sky-100 placeholder-sky-600 focus:border-gold-500 text-sm"
            />
          </div>
          <div className="sm:w-44">
            <label htmlFor="tenant-admins-role" className="block text-sky-300 text-xs font-medium mb-1">
              Role
            </label>
            <select
              id="tenant-admins-role"
              value={addForm.role}
              onChange={(e) => setAddForm((f) => ({ ...f, role: e.target.value }))}
              className="w-full bg-navy-700 border border-navy-600 rounded-lg px-3 py-2 text-sky-100 focus:border-gold-500 text-sm"
            >
              {ROLE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-end">
            <button
              type="submit"
              disabled={adding || !addForm.userId.trim()}
              className="px-4 py-2 bg-gold-500 text-navy-900 rounded-lg font-semibold hover:bg-gold-400 transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
            >
              {adding ? 'Adding...' : 'Add Admin'}
            </button>
          </div>
        </form>
      </div>

      {/* Admins List */}
      <div className="bg-navy-800 border border-navy-700 rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-navy-700 flex items-center justify-between">
          <h2 className="text-sky-100 text-lg font-semibold">
            Current Admins
          </h2>
          <span className="text-sky-400 text-sm">{admins.length} total</span>
        </div>

        {removeError && (
          <div className="mx-5 mt-4 bg-red-900/30 border border-red-700 rounded-lg p-3 text-red-300 text-sm">
            {removeError}
          </div>
        )}

        {admins.length === 0 ? (
          <div className="p-8 text-center text-sky-400">
            No administrators found for this tenant.
          </div>
        ) : (
          <div className="divide-y divide-navy-700">
            {admins.map((admin) => (
              <div key={admin.id} className="px-5 py-4 flex items-center gap-4">
                {/* Avatar */}
                <div className="h-9 w-9 rounded-full bg-navy-700 border border-navy-600 flex items-center justify-center flex-shrink-0">
                  {admin.avatarUrl ? (
                    <img
                      src={admin.avatarUrl}
                      alt={admin.username || admin.displayName}
                      className="h-9 w-9 rounded-full object-cover"
                      onError={(e) => { e.target.style.display = 'none'; }}
                    />
                  ) : (
                    <span className="text-gold-400 text-sm font-bold">
                      {(admin.displayName || admin.username || '?')[0].toUpperCase()}
                    </span>
                  )}
                </div>

                {/* Info */}
                <div className="flex-1 min-w-0">
                  <p className="text-sky-100 font-medium truncate">
                    {admin.displayName || admin.username}
                  </p>
                  {admin.email && (
                    <p className="text-sky-400 text-sm truncate">{admin.email}</p>
                  )}
                </div>

                {/* Role Badge */}
                <span
                  className={`flex-shrink-0 text-xs px-2.5 py-0.5 border rounded-full font-medium ${
                    ROLE_BADGE[admin.role] || 'bg-navy-600 text-sky-300 border-navy-500'
                  }`}
                >
                  {ROLE_LABEL[admin.role] || admin.role}
                </span>

                {/* Remove / Confirm */}
                {confirmRemoveId === admin.id ? (
                  <div className="flex items-center gap-2 flex-shrink-0">
                    <span className="text-sky-400 text-xs hidden sm:block">Remove?</span>
                    <button
                      onClick={() => handleRemove(admin.id)}
                      disabled={removingId === admin.id}
                      className="px-3 py-1.5 bg-red-700 text-white rounded-lg text-xs font-semibold hover:bg-red-600 transition-colors disabled:opacity-50"
                    >
                      {removingId === admin.id ? '...' : 'Confirm'}
                    </button>
                    <button
                      onClick={handleRemoveCancel}
                      disabled={removingId === admin.id}
                      className="px-3 py-1.5 bg-navy-700 text-sky-300 rounded-lg text-xs font-medium hover:bg-navy-600 transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => handleRemoveConfirm(admin.id)}
                    className="flex-shrink-0 p-1.5 text-sky-500 hover:text-red-400 hover:bg-red-900/20 rounded-lg transition-colors"
                    title="Remove admin"
                  >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                        d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
                      />
                    </svg>
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default TenantAdmins;
