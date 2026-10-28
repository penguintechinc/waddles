import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import {
  PlusIcon,
  PencilIcon,
  TrashIcon,
  ShieldCheckIcon,
} from '@heroicons/react/24/outline';
import { rolesApi } from '../../services/api';

const AVAILABLE_SCOPES = [
  {
    group: 'Community',
    scopes: [
      'community:read',
      'community:manage_channels',
      'community:manage_members',
      'community:manage_roles',
    ],
  },
  {
    group: 'Channels',
    scopes: [
      'channels:read',
      'channels:send_chat',
      'channels:speak',
      'channels:share_video',
      'channels:screenshare',
      'channels:moderate',
      'channels:override_screenshare',
    ],
  },
  {
    group: 'Resources',
    scopes: [
      'resource:delete_any',
      'resource:pin',
      'resource:moderate',
    ],
  },
];

const ALL_SCOPES = AVAILABLE_SCOPES.flatMap((g) => g.scopes);

const EMPTY_FORM = {
  name: '',
  displayName: '',
  description: '',
  priority: 0,
  scopes: [],
};

function ScopesGrid({ selected, onChange, disabled }) {
  function toggle(scope) {
    if (disabled) return;
    if (selected.includes(scope)) {
      onChange(selected.filter((s) => s !== scope));
    } else {
      onChange([...selected, scope]);
    }
  }

  return (
    <div className="space-y-4">
      {AVAILABLE_SCOPES.map(({ group, scopes }) => (
        <div key={group}>
          <p className="text-xs font-semibold text-navy-400 uppercase tracking-wider mb-2">
            {group}
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
            {scopes.map((scope) => (
              <label
                key={scope}
                className={`flex items-center gap-2 cursor-pointer ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
              >
                <input
                  type="checkbox"
                  checked={selected.includes(scope)}
                  onChange={() => toggle(scope)}
                  disabled={disabled}
                  className="h-4 w-4 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
                />
                <span className="text-sm text-navy-300 font-mono">{scope}</span>
              </label>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function RoleForm({ initial, onSave, onCancel, saving }) {
  const [form, setForm] = useState(
    initial
      ? {
          name: initial.name ?? '',
          displayName: initial.displayName ?? initial.display_name ?? '',
          description: initial.description ?? '',
          priority: initial.priority ?? 0,
          scopes: initial.scopes ?? [],
        }
      : EMPTY_FORM
  );

  function set(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  function handleNameChange(e) {
    set('name', e.target.value.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_-]/g, ''));
  }

  function handleSubmit(e) {
    e.preventDefault();
    onSave(form);
  }

  const isEdit = Boolean(initial?.id);

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-navy-800 border border-navy-600 rounded-xl p-5 mb-6"
    >
      <h3 className="text-sky-100 font-semibold mb-4">
        {isEdit ? 'Edit Role' : 'Create Role'}
      </h3>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
        <div>
          <label htmlFor="admin-community-roles-name-lowercase" className="block text-sm font-medium text-navy-300 mb-1">
            Name <span className="text-navy-500 font-normal">(lowercase)</span>
          </label>
          <input
            id="admin-community-roles-name-lowercase"
            type="text"
            required
            value={form.name}
            onChange={handleNameChange}
            className="bg-navy-900 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 w-full focus:border-sky-500"
            placeholder="e.g. moderator"
          />
        </div>
        <div>
          <label htmlFor="admin-community-roles-display-name" className="block text-sm font-medium text-navy-300 mb-1">Display Name</label>
          <input
            id="admin-community-roles-display-name"
            type="text"
            required
            value={form.displayName}
            onChange={(e) => set('displayName', e.target.value)}
            className="bg-navy-900 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 w-full focus:border-sky-500"
            placeholder="e.g. Moderator"
          />
        </div>
        <div className="sm:col-span-2">
          <label htmlFor="admin-community-roles-description" className="block text-sm font-medium text-navy-300 mb-1">Description</label>
          <textarea
            id="admin-community-roles-description"
            value={form.description}
            onChange={(e) => set('description', e.target.value)}
            rows={2}
            className="bg-navy-900 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 w-full focus:border-sky-500 resize-none"
            placeholder="Optional description"
          />
        </div>
        <div>
          <label htmlFor="admin-community-roles-priority-0-49" className="block text-sm font-medium text-navy-300 mb-1">
            Priority <span className="text-navy-500 font-normal">(0–49)</span>
          </label>
          <input
            id="admin-community-roles-priority-0-49"
            type="number"
            min={0}
            max={49}
            value={form.priority}
            onChange={(e) => set('priority', Math.max(0, Math.min(49, parseInt(e.target.value, 10) || 0)))}
            className="bg-navy-900 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 w-full focus:border-sky-500"
          />
        </div>
      </div>

      <div className="mb-5">
        <label className="block text-sm font-medium text-navy-300 mb-3">Scopes</label>
        <div className="bg-navy-900 border border-navy-700 rounded-lg p-4">
          <ScopesGrid
            selected={form.scopes}
            onChange={(scopes) => set('scopes', scopes)}
            disabled={false}
          />
        </div>
      </div>

      <div className="flex gap-2">
        <button
          type="submit"
          disabled={saving}
          className="bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-4 py-2 font-medium disabled:opacity-50 transition-colors"
        >
          {saving ? 'Saving…' : isEdit ? 'Save Changes' : 'Create Role'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="bg-navy-700 text-navy-300 hover:text-sky-100 rounded-lg px-4 py-2 font-medium transition-colors"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

function DeleteConfirm({ role, onConfirm, onCancel, deleting }) {
  return (
    <div className="bg-navy-800 border border-red-500/40 rounded-xl p-4 mb-6">
      <p className="text-sky-100 mb-3">
        Delete role <span className="font-semibold text-red-400">{role.displayName ?? role.display_name ?? role.name}</span>?
        This cannot be undone.
      </p>
      <div className="flex gap-2">
        <button
          onClick={onConfirm}
          disabled={deleting}
          className="bg-red-600 hover:bg-red-500 text-white rounded-lg px-4 py-2 font-medium disabled:opacity-50 transition-colors"
        >
          {deleting ? 'Deleting…' : 'Delete'}
        </button>
        <button
          onClick={onCancel}
          className="bg-navy-700 text-navy-300 hover:text-sky-100 rounded-lg px-4 py-2 font-medium transition-colors"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function RoleRow({ role, onEdit, onDelete }) {
  const displayName = role.displayName ?? role.display_name ?? role.name;
  const scopeCount = role.scopes?.length ?? 0;
  const isSystem = role.is_system ?? false;

  return (
    <div className="bg-navy-800 border border-navy-700 rounded-xl px-4 py-3 flex items-center justify-between gap-4">
      <div className="flex items-center gap-3 min-w-0">
        <ShieldCheckIcon className={`h-5 w-5 flex-shrink-0 ${isSystem ? 'text-gold-400' : 'text-navy-400'}`} />
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sky-100 font-medium">{displayName}</span>
            {isSystem && (
              <span className="bg-gold-500/20 text-gold-400 text-xs px-2 py-0.5 rounded">
                System
              </span>
            )}
            <span className="text-xs text-navy-400 font-mono">{role.name}</span>
          </div>
          <div className="flex items-center gap-3 mt-0.5">
            <span className="text-xs text-navy-400">Priority: {role.priority ?? 0}</span>
            <span className="text-xs text-navy-400">
              {scopeCount} scope{scopeCount !== 1 ? 's' : ''}
            </span>
          </div>
          {role.description && (
            <p className="text-xs text-navy-400 mt-0.5 truncate">{role.description}</p>
          )}
        </div>
      </div>
      {!isSystem && (
        <div className="flex items-center gap-1 flex-shrink-0">
          <button
            onClick={() => onEdit(role)}
            className="p-1.5 text-navy-400 hover:text-sky-300 rounded transition-colors"
            title="Edit role"
          >
            <PencilIcon className="h-4 w-4" />
          </button>
          <button
            onClick={() => onDelete(role)}
            className="p-1.5 text-navy-400 hover:text-red-400 rounded transition-colors"
            title="Delete role"
          >
            <TrashIcon className="h-4 w-4" />
          </button>
        </div>
      )}
    </div>
  );
}

export default function AdminCommunityRoles() {
  const { communityId } = useParams();

  const [roles, setRoles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [showCreate, setShowCreate] = useState(false);
  const [editRole, setEditRole] = useState(null);
  const [deleteRole, setDeleteRole] = useState(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function loadRoles() {
    setLoading(true);
    setError(null);
    try {
      const res = await rolesApi.list(communityId);
      const raw = res.data?.roles ?? [];
      // Sort by priority descending (higher priority first)
      raw.sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0));
      setRoles(raw);
    } catch (err) {
      setError(err?.response?.data?.message ?? 'Failed to load roles.');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadRoles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [communityId]);

  async function handleCreate(formData) {
    setSaving(true);
    setError(null);
    try {
      await rolesApi.create(communityId, formData);
      setShowCreate(false);
      await loadRoles();
    } catch (err) {
      setError(err?.response?.data?.message ?? 'Failed to create role.');
    } finally {
      setSaving(false);
    }
  }

  async function handleUpdate(formData) {
    setSaving(true);
    setError(null);
    try {
      await rolesApi.update(communityId, editRole.id, formData);
      setEditRole(null);
      await loadRoles();
    } catch (err) {
      setError(err?.response?.data?.message ?? 'Failed to update role.');
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    setDeleting(true);
    setError(null);
    try {
      await rolesApi.delete(communityId, deleteRole.id);
      setDeleteRole(null);
      await loadRoles();
    } catch (err) {
      setError(err?.response?.data?.message ?? 'Failed to delete role.');
    } finally {
      setDeleting(false);
    }
  }

  function startEdit(role) {
    setShowCreate(false);
    setDeleteRole(null);
    setEditRole(role);
  }

  function startDelete(role) {
    setShowCreate(false);
    setEditRole(null);
    setDeleteRole(role);
  }

  function startCreate() {
    setEditRole(null);
    setDeleteRole(null);
    setShowCreate(true);
  }

  const systemRoles = roles.filter((r) => r.is_system);
  const customRoles = roles.filter((r) => !r.is_system);

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-sky-100">Community Roles</h1>
        <button
          onClick={startCreate}
          className="flex items-center gap-1.5 bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-4 py-2 font-medium transition-colors"
        >
          <PlusIcon className="h-4 w-4" />
          Create Role
        </button>
      </div>

      {/* Inline forms */}
      {showCreate && (
        <RoleForm
          initial={null}
          onSave={handleCreate}
          onCancel={() => setShowCreate(false)}
          saving={saving}
        />
      )}
      {editRole && (
        <RoleForm
          initial={editRole}
          onSave={handleUpdate}
          onCancel={() => setEditRole(null)}
          saving={saving}
        />
      )}
      {deleteRole && (
        <DeleteConfirm
          role={deleteRole}
          onConfirm={handleDelete}
          onCancel={() => setDeleteRole(null)}
          deleting={deleting}
        />
      )}

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 text-red-400 rounded-lg px-4 py-3 mb-4 text-sm">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-navy-400 text-sm">Loading roles…</div>
      ) : roles.length === 0 ? (
        <div className="text-navy-400 text-sm">
          No roles yet. Click &ldquo;Create Role&rdquo; to add one.
        </div>
      ) : (
        <div className="space-y-6">
          {systemRoles.length > 0 && (
            <div>
              <h2 className="text-sm font-semibold text-navy-400 uppercase tracking-wider mb-3">
                System Roles
              </h2>
              <div className="space-y-2">
                {systemRoles.map((role) => (
                  <RoleRow
                    key={role.id}
                    role={role}
                    onEdit={startEdit}
                    onDelete={startDelete}
                  />
                ))}
              </div>
            </div>
          )}
          {customRoles.length > 0 && (
            <div>
              <h2 className="text-sm font-semibold text-navy-400 uppercase tracking-wider mb-3">
                Custom Roles
              </h2>
              <div className="space-y-2">
                {customRoles.map((role) => (
                  <RoleRow
                    key={role.id}
                    role={role}
                    onEdit={startEdit}
                    onDelete={startDelete}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
