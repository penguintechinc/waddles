import { useState, useEffect, useMemo } from 'react';
import {
  MagnifyingGlassIcon,
  PlusIcon,
  TrashIcon,
  PencilIcon,
  ShieldCheckIcon,
  ShoppingCartIcon,
  CheckBadgeIcon,
  ChartBarIcon,
} from '@heroicons/react/24/outline';
import api, { superAdminApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { WADDLES_GOLD_COLORS } from '../../theme/waddlebotTheme';

function SuperAdminUsers() {
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [activeFilter, setActiveFilter] = useState('');
  const [page, setPage] = useState(1);
  const [limit] = useState(25);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);

  // Modal states
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [showVerifyModal, setShowVerifyModal] = useState(false);
  const [showRoleModal, setShowRoleModal] = useState(false);
  const [selectedUser, setSelectedUser] = useState(null);
  const [roleName, setRoleName] = useState('');

  // Form states (kept for role modal only)
  const [roleFormData, setRoleFormData] = useState({
    grant: true,
  });

  // Edit modal initial values (for FormModalBuilder)
  const [editInitialValues, setEditInitialValues] = useState({
    email: '',
    isActive: true,
  });

  // Fetch users
  useEffect(() => {
    loadUsers();
  }, [searchTerm, roleFilter, activeFilter, page]);

  const loadUsers = async () => {
    try {
      setLoading(true);
      setError(null);
      const params = {
        page,
        limit,
        search: searchTerm,
        role: roleFilter || undefined,
        isActive: activeFilter || undefined,
      };
      const response = await superAdminApi.listUsers(params);
      setUsers(response.data.users);
      setTotal(response.data.pagination.total);
      setTotalPages(response.data.pagination.totalPages);
    } catch (err) {
      setError('Failed to load users: ' + (err.response?.data?.error || err.message));
    } finally {
      setLoading(false);
    }
  };

  const handleCreateUser = async (formData) => {
    try {
      await superAdminApi.createUser(formData);
      await loadUsers();
    } catch (err) {
      const errorMsg = err.response?.data?.error?.message ||
                      err.response?.data?.error ||
                      err.message ||
                      'Unknown error';
      setError('Failed to create user: ' + errorMsg);
      throw err; // Re-throw to prevent modal from closing
    }
  };

  const handleEditUser = async (formData) => {
    if (!selectedUser) return;

    try {
      await superAdminApi.updateUser(selectedUser.id, formData);
      await loadUsers();
    } catch (err) {
      const errorMsg = err.response?.data?.error?.message ||
                      err.response?.data?.error ||
                      err.message ||
                      'Unknown error';
      setError('Failed to update user: ' + errorMsg);
      throw err; // Re-throw to prevent modal from closing
    }
  };

  const handleDeleteUser = async () => {
    if (!selectedUser) return;

    try {
      await superAdminApi.deleteUser(selectedUser.id);
      setShowDeleteModal(false);
      await loadUsers();
    } catch (err) {
      const errorMsg = err.response?.data?.error?.message ||
                      err.response?.data?.error ||
                      err.message ||
                      'Unknown error';
      setError('Failed to delete user: ' + errorMsg);
    }
  };

  const handleAssignRole = async (e) => {
    e.preventDefault();
    if (!selectedUser) return;

    try {
      if (roleName === 'super_admin') {
        await superAdminApi.assignSuperAdminRole(selectedUser.id, roleFormData.grant);
      } else if (roleName === 'vendor') {
        await superAdminApi.assignVendorRole(selectedUser.id, roleFormData.grant);
      } else if (roleName === 'analytics_consumer') {
        await api.post(`/api/v1/superadmin/users/${selectedUser.id}/analytics-consumer-role`, { grant: roleFormData.grant });
      }
      setShowRoleModal(false);
      await loadUsers();
    } catch (err) {
      const errorMsg = err.response?.data?.error?.message ||
                      err.response?.data?.error ||
                      err.message ||
                      'Unknown error';
      setError('Failed to assign role: ' + errorMsg);
    }
  };

  const openEditModal = (user) => {
    setSelectedUser(user);
    setEditInitialValues({
      email: user.email,
      isActive: user.isActive,
    });
    setShowEditModal(true);
  };

  const openRoleModal = (user, role) => {
    setSelectedUser(user);
    setRoleName(role);
    let grant;
    if (role === 'super_admin') grant = !user.isSuperAdmin;
    else if (role === 'vendor') grant = !user.isVendor;
    else if (role === 'analytics_consumer') grant = !user.isAnalyticsConsumer;
    else grant = true;
    setRoleFormData({ grant });
    setShowRoleModal(true);
  };

  const openVerifyModal = (user) => {
    setSelectedUser(user);
    setShowVerifyModal(true);
  };

  const handleSetEmailVerification = async () => {
    if (!selectedUser) return;

    try {
      await superAdminApi.setEmailVerification(selectedUser.id, !selectedUser.emailVerified);
      setShowVerifyModal(false);
      await loadUsers();
    } catch (err) {
      const errorMsg = err.response?.data?.error?.message ||
                      err.response?.data?.error ||
                      err.message ||
                      'Unknown error';
      setError('Failed to update verification: ' + errorMsg);
    }
  };

  const openDeleteModal = (user) => {
    setSelectedUser(user);
    setShowDeleteModal(true);
  };

  // FormModalBuilder field definitions
  const createUserFields = useMemo(() => [
    {
      name: 'email',
      type: 'email',
      label: 'Email',
      placeholder: 'user@example.com',
      required: true,
      defaultValue: '',
    },
    {
      name: 'password',
      type: 'password',
      label: 'Password',
      placeholder: 'Enter password',
      required: true,
      defaultValue: '',
    },
  ], []);

  const editUserFields = useMemo(() => [
    {
      name: 'email',
      type: 'email',
      label: 'Email',
      placeholder: 'user@example.com',
      required: true,
      defaultValue: editInitialValues.email,
    },
    {
      name: 'isActive',
      type: 'checkbox',
      label: 'Active',
      defaultValue: editInitialValues.isActive,
    },
  ], [editInitialValues]);


  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex justify-between items-center">
        <h1 className="text-3xl font-bold text-gold-400">User Management</h1>
        <button
          onClick={() => setShowCreateModal(true)}
          className="flex items-center space-x-2 px-4 py-2 bg-gold-500 text-navy-950 rounded-lg hover:bg-gold-600 transition-colors font-medium"
        >
          <PlusIcon className="w-5 h-5" />
          <span>New User</span>
        </button>
      </div>

      {error && (
        <div className="p-4 bg-red-900/30 border border-red-700 rounded-lg text-red-200">
          {error}
        </div>
      )}

      {/* Filters */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-4 space-y-4">
        <div className="flex items-center space-x-2 bg-navy-900 rounded px-3 py-2 border border-navy-700">
          <MagnifyingGlassIcon className="w-5 h-5 text-navy-400" />
          <input
            aria-label="Search by email or username"
            type="text"
            placeholder="Search by email or username..."
            value={searchTerm}
            onChange={(e) => {
              setSearchTerm(e.target.value);
              setPage(1);
            }}
            className="bg-transparent text-sky-100 flex-1"
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label htmlFor="super-admin-users-role" className="block text-sm font-medium text-navy-300 mb-2">Role</label>
            <select
              id="super-admin-users-role"
              value={roleFilter}
              onChange={(e) => {
                setRoleFilter(e.target.value);
                setPage(1);
              }}
              className="w-full bg-navy-900 border border-navy-700 rounded px-3 py-2 text-sky-100 text-sm"
            >
              <option value="">All Roles</option>
              <option value="super_admin">Super Admin</option>
              <option value="vendor">Vendor</option>
              <option value="">Regular Users</option>
            </select>
          </div>

          <div>
            <label htmlFor="super-admin-users-status" className="block text-sm font-medium text-navy-300 mb-2">Status</label>
            <select
              id="super-admin-users-status"
              value={activeFilter}
              onChange={(e) => {
                setActiveFilter(e.target.value);
                setPage(1);
              }}
              className="w-full bg-navy-900 border border-navy-700 rounded px-3 py-2 text-sky-100 text-sm"
            >
              <option value="">All Statuses</option>
              <option value="true">Active</option>
              <option value="false">Inactive</option>
            </select>
          </div>
        </div>
      </div>

      {/* Users Table */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-64">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400"></div>
          </div>
        ) : users.length === 0 ? (
          <div className="p-8 text-center text-navy-400">No users found</div>
        ) : (
          <table className="w-full">
            <thead className="bg-navy-900 border-b border-navy-700">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-semibold text-gold-400 uppercase">
                  Email
                </th>
                <th className="px-6 py-3 text-left text-xs font-semibold text-gold-400 uppercase">
                  Username
                </th>
                <th className="px-6 py-3 text-left text-xs font-semibold text-gold-400 uppercase">
                  Status
                </th>
                <th className="px-6 py-3 text-left text-xs font-semibold text-gold-400 uppercase">
                  Roles
                </th>
                <th className="px-6 py-3 text-left text-xs font-semibold text-gold-400 uppercase">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-navy-700">
              {users.map((user) => (
                <tr key={user.id} className="hover:bg-navy-700/50 transition-colors">
                  <td className="px-6 py-4 text-sm text-sky-100">{user.email}</td>
                  <td className="px-6 py-4 text-sm text-sky-100">{user.username}</td>
                  <td className="px-6 py-4 text-sm space-x-1">
                    <span
                      className={`inline-block px-2 py-1 rounded text-xs font-medium ${
                        user.isActive
                          ? 'bg-emerald-900/30 text-emerald-300 border border-emerald-500/30'
                          : 'bg-red-900/30 text-red-300 border border-red-500/30'
                      }`}
                    >
                      {user.isActive ? 'Active' : 'Inactive'}
                    </span>
                    <span
                      className={`inline-block px-2 py-1 rounded text-xs font-medium ${
                        user.emailVerified
                          ? 'bg-emerald-900/30 text-emerald-300 border border-emerald-500/30'
                          : 'bg-amber-900/30 text-amber-300 border border-amber-500/30'
                      }`}
                    >
                      {user.emailVerified ? 'Verified' : 'Unverified'}
                    </span>
                  </td>
                  <td className="px-6 py-4 text-sm space-y-1">
                    {user.isSuperAdmin && (
                      <div className="inline-block mr-2 px-2 py-1 bg-gold-500/20 text-gold-300 rounded text-xs border border-gold-500/30">
                        Super Admin
                      </div>
                    )}
                    {user.isVendor && (
                      <div className="inline-block mr-2 px-2 py-1 bg-emerald-500/20 text-emerald-300 rounded text-xs border border-emerald-500/30">
                        Vendor
                      </div>
                    )}
                    {user.isAnalyticsConsumer && (
                      <div className="inline-block px-2 py-1 bg-sky-500/20 text-sky-300 rounded text-xs border border-sky-500/30">
                        Analytics Consumer
                      </div>
                    )}
                    {!user.isSuperAdmin && !user.isVendor && !user.isAnalyticsConsumer && (
                      <span className="text-navy-400">—</span>
                    )}
                  </td>
                  <td className="px-6 py-4 text-sm space-x-2">
                    <button
                      onClick={() => openEditModal(user)}
                      className="inline-flex items-center px-2 py-1 text-sky-400 hover:text-sky-300 hover:bg-navy-700 rounded transition-colors"
                      title="Edit user"
                    >
                      <PencilIcon className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => openRoleModal(user, 'super_admin')}
                      className={`inline-flex items-center px-2 py-1 rounded transition-colors ${
                        user.isSuperAdmin
                          ? 'text-gold-400 hover:text-gold-300 hover:bg-navy-700'
                          : 'text-navy-400 hover:text-navy-300 hover:bg-navy-700'
                      }`}
                      title={
                        user.isSuperAdmin ? 'Revoke super admin' : 'Grant super admin'
                      }
                    >
                      <ShieldCheckIcon className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => openRoleModal(user, 'vendor')}
                      className={`inline-flex items-center px-2 py-1 rounded transition-colors ${
                        user.isVendor
                          ? 'text-emerald-400 hover:text-emerald-300 hover:bg-navy-700'
                          : 'text-navy-400 hover:text-navy-300 hover:bg-navy-700'
                      }`}
                      title={user.isVendor ? 'Revoke vendor' : 'Grant vendor'}
                    >
                      <ShoppingCartIcon className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => openRoleModal(user, 'analytics_consumer')}
                      className={`inline-flex items-center px-2 py-1 rounded transition-colors ${
                        user.isAnalyticsConsumer
                          ? 'text-sky-400 hover:text-sky-300 hover:bg-navy-700'
                          : 'text-navy-400 hover:text-navy-300 hover:bg-navy-700'
                      }`}
                      title={user.isAnalyticsConsumer ? 'Revoke analytics consumer' : 'Grant analytics consumer'}
                    >
                      <ChartBarIcon className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => openVerifyModal(user)}
                      className={`inline-flex items-center px-2 py-1 rounded transition-colors ${
                        user.emailVerified
                          ? 'text-emerald-400 hover:text-emerald-300 hover:bg-navy-700'
                          : 'text-navy-400 hover:text-navy-300 hover:bg-navy-700'
                      }`}
                      title={user.emailVerified ? 'Remove email verification' : 'Verify email'}
                    >
                      <CheckBadgeIcon className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => openDeleteModal(user)}
                      className="inline-flex items-center px-2 py-1 text-red-400 hover:text-red-300 hover:bg-navy-700 rounded transition-colors"
                      title="Delete user"
                    >
                      <TrashIcon className="w-4 h-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex justify-center items-center space-x-2">
          <button
            onClick={() => setPage(Math.max(1, page - 1))}
            disabled={page === 1}
            className="px-4 py-2 bg-navy-800 border border-navy-700 rounded text-sky-100 disabled:opacity-50 hover:bg-navy-700 transition-colors"
          >
            Previous
          </button>
          <span className="text-sky-100">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => setPage(Math.min(totalPages, page + 1))}
            disabled={page === totalPages}
            className="px-4 py-2 bg-navy-800 border border-navy-700 rounded text-sky-100 disabled:opacity-50 hover:bg-navy-700 transition-colors"
          >
            Next
          </button>
        </div>
      )}

      {/* Create User Modal */}
      <FormModalBuilder
        title="Create New User"
        fields={createUserFields}
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={handleCreateUser}
        submitButtonText="Create"
        cancelButtonText="Cancel"
        themeMode="dark"
        colors={WADDLES_GOLD_COLORS}
      />

      {/* Edit User Modal */}
      <FormModalBuilder
        title="Edit User"
        fields={editUserFields}
        isOpen={showEditModal && selectedUser !== null}
        onClose={() => setShowEditModal(false)}
        onSubmit={handleEditUser}
        submitButtonText="Update"
        cancelButtonText="Cancel"
        themeMode="dark"
        colors={WADDLES_GOLD_COLORS}
      />

      {/* Role Assignment Modal */}
      {showRoleModal && selectedUser && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div role="dialog" aria-modal="true" aria-labelledby="super-admin-users-dialog-1-title" className="bg-navy-900 border border-navy-700 rounded-lg p-6 w-full max-w-md">
            <h2 id="super-admin-users-dialog-1-title" className="text-xl font-bold text-gold-400 mb-4">
              {roleName === 'super_admin'
                ? 'Super Admin Role'
                : roleName === 'vendor'
                ? 'Vendor Role'
                : 'Analytics Consumer Role'}
            </h2>
            <p className="text-navy-300 mb-4">
              {roleName === 'super_admin'
                ? `${roleFormData.grant ? 'Grant' : 'Revoke'} super admin access to ${selectedUser.username}?`
                : roleName === 'vendor'
                ? `${roleFormData.grant ? 'Grant' : 'Revoke'} vendor access to ${selectedUser.username}?`
                : `${roleFormData.grant ? 'Grant' : 'Revoke'} analytics consumer access to ${selectedUser.username}?`}
            </p>
            <form onSubmit={handleAssignRole} className="space-y-4">
              <div className="flex space-x-3">
                <button
                  type="button"
                  onClick={() => setShowRoleModal(false)}
                  className="flex-1 px-4 py-2 bg-navy-700 text-sky-100 rounded hover:bg-navy-600 transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="flex-1 px-4 py-2 bg-gold-500 text-navy-950 rounded hover:bg-gold-600 transition-colors font-medium"
                >
                  {roleFormData.grant ? 'Grant' : 'Revoke'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Verify Email Modal */}
      {showVerifyModal && selectedUser && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div role="dialog" aria-modal="true" aria-labelledby="super-admin-users-dialog-2-title" className="bg-navy-900 border border-navy-700 rounded-lg p-6 w-full max-w-md">
            <h2 id="super-admin-users-dialog-2-title" className="text-xl font-bold text-gold-400 mb-4">Email Verification</h2>
            <p className="text-navy-300 mb-4">
              {selectedUser.emailVerified
                ? <>Remove email verification from <span className="font-medium text-sky-100">{selectedUser.username}</span>?</>
                : <>Mark <span className="font-medium text-sky-100">{selectedUser.username}</span> as email verified?</>}
            </p>
            <div className="flex space-x-3">
              <button
                onClick={() => setShowVerifyModal(false)}
                className="flex-1 px-4 py-2 bg-navy-700 text-sky-100 rounded hover:bg-navy-600 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleSetEmailVerification}
                className="flex-1 px-4 py-2 bg-gold-500 text-navy-950 rounded hover:bg-gold-600 transition-colors font-medium"
              >
                {selectedUser.emailVerified ? 'Unverify' : 'Verify'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete User Modal */}
      {showDeleteModal && selectedUser && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div role="dialog" aria-modal="true" aria-labelledby="super-admin-users-dialog-3-title" className="bg-navy-900 border border-navy-700 rounded-lg p-6 w-full max-w-md">
            <h2 id="super-admin-users-dialog-3-title" className="text-xl font-bold text-red-400 mb-4">Delete User</h2>
            <p className="text-navy-300 mb-4">
              Are you sure you want to delete{' '}
              <span className="font-medium text-sky-100">{selectedUser.username}</span>?
              This action deactivates the account.
            </p>
            <div className="flex space-x-3">
              <button
                onClick={() => setShowDeleteModal(false)}
                className="flex-1 px-4 py-2 bg-navy-700 text-sky-100 rounded hover:bg-navy-600 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleDeleteUser}
                className="flex-1 px-4 py-2 bg-red-600 text-white rounded hover:bg-red-700 transition-colors font-medium"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default SuperAdminUsers;
