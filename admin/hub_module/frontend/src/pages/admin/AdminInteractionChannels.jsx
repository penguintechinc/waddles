import { useState, useEffect, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import {
  PlusIcon,
  PencilIcon,
  TrashIcon,
  HashtagIcon,
  ChatBubbleLeftRightIcon,
  SpeakerWaveIcon,
  ShieldCheckIcon,
  ChevronDownIcon,
  ChevronUpIcon,
} from '@heroicons/react/24/outline';
import { interactionApi, channelPermissionsApi, rolesApi, adminApi } from '../../services/api';

const CHANNEL_TYPES = [
  { value: 'chat', label: 'Chat' },
  { value: 'forum', label: 'Forum' },
  { value: 'voice', label: 'Voice' },
];

const TYPE_BADGE = {
  chat: 'bg-sky-500/20 text-sky-400',
  forum: 'bg-purple-500/20 text-purple-400',
  voice: 'bg-green-500/20 text-green-400',
};

const TYPE_ICON = {
  chat: HashtagIcon,
  forum: ChatBubbleLeftRightIcon,
  voice: SpeakerWaveIcon,
};

const EMPTY_FORM = {
  name: '',
  type: 'chat',
  description: '',
  sort_order: 0,
  allow_ad_hoc_voice: false,
  has_chat: true,
  has_voice: false,
  has_video: false,
  is_broadcast: false,
  is_temporary: false,
  temp_duration_minutes: 60,
};

const PERMISSION_SCOPES = [
  'channels:read',
  'channels:send_chat',
  'channels:speak',
  'channels:share_video',
  'channels:screenshare',
  'channels:moderate',
];

const PERMISSION_LABELS = {
  'channels:read': 'Read',
  'channels:send_chat': 'Send Chat',
  'channels:speak': 'Speak',
  'channels:share_video': 'Video',
  'channels:screenshare': 'Screen',
  'channels:moderate': 'Moderate',
};

function ChannelTypeBadge({ type }) {
  const label = CHANNEL_TYPES.find((t) => t.value === type)?.label ?? type;
  const className = TYPE_BADGE[type] ?? 'bg-navy-600/20 text-navy-300';
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${className}`}>
      {label.trim()}
    </span>
  );
}

function ChannelPermissions({ communityId, channelId }) {
  const [roles, setRoles] = useState([]);
  const [overrides, setOverrides] = useState({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [rolesRes, permsRes] = await Promise.all([
        rolesApi.list(communityId),
        channelPermissionsApi.getOverrides(communityId, channelId),
      ]);
      const rawRoles = rolesRes.data?.roles ?? [];
      rawRoles.sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0));
      setRoles(rawRoles);
      // overrides: { roleId: { scope: 'grant'|'deny'|null } }
      const raw = permsRes.data?.overrides ?? {};
      setOverrides(raw);
    } catch (err) {
      setError(err?.response?.data?.message ?? 'Failed to load permissions.');
    } finally {
      setLoading(false);
    }
  }, [communityId, channelId]);

  useEffect(() => {
    load();
  }, [load]);

  function cycleOverride(roleId, scope) {
    setOverrides((prev) => {
      const roleOverrides = { ...(prev[roleId] ?? {}) };
      const current = roleOverrides[scope] ?? null;
      // cycle: null → grant → deny → null
      if (current === null) roleOverrides[scope] = 'grant';
      else if (current === 'grant') roleOverrides[scope] = 'deny';
      else roleOverrides[scope] = null;
      return { ...prev, [roleId]: roleOverrides };
    });
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      await channelPermissionsApi.updateOverrides(communityId, channelId, overrides);
    } catch (err) {
      setError(err?.response?.data?.message ?? 'Failed to save permissions.');
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return <div className="text-navy-400 text-xs py-2">Loading permissions…</div>;
  }

  return (
    <div className="mt-3 pt-3 border-t border-navy-700">
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs font-semibold text-navy-400 uppercase tracking-wider flex items-center gap-1">
          <ShieldCheckIcon className="h-3.5 w-3.5" />
          Role Permissions
        </p>
        <button
          onClick={handleSave}
          disabled={saving}
          className="text-xs bg-gold-500 text-navy-900 hover:bg-gold-400 rounded px-2.5 py-1 font-medium disabled:opacity-50 transition-colors"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
      {error && (
        <div className="text-red-400 text-xs mb-2">{error}</div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr>
              <th className="text-left text-navy-400 font-medium pb-1.5 pr-3 min-w-[100px]">Role</th>
              {PERMISSION_SCOPES.map((scope) => (
                <th key={scope} className="text-center text-navy-400 font-medium pb-1.5 px-1 whitespace-nowrap">
                  {PERMISSION_LABELS[scope]}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {roles.map((role) => (
              <tr key={role.id} className="border-t border-navy-700/50">
                <td className="py-1.5 pr-3">
                  <span className="text-sky-100">{role.displayName ?? role.display_name ?? role.name}</span>
                  {role.is_system && (
                    <span className="ml-1.5 bg-gold-500/20 text-gold-400 text-xs px-1.5 py-0.5 rounded">sys</span>
                  )}
                </td>
                {PERMISSION_SCOPES.map((scope) => {
                  const val = overrides[role.id]?.[scope] ?? null;
                  return (
                    <td key={scope} className="py-1.5 px-1 text-center">
                      <button
                        onClick={() => cycleOverride(role.id, scope)}
                        title={`${role.name} — ${scope}: ${val ?? 'inherit'} (click to cycle)`}
                        className={`w-7 h-7 rounded text-xs font-bold transition-colors ${
                          val === 'grant'
                            ? 'bg-green-500/20 text-green-400 hover:bg-green-500/30'
                            : val === 'deny'
                            ? 'bg-red-500/20 text-red-400 hover:bg-red-500/30'
                            : 'bg-navy-700 text-navy-500 hover:bg-navy-600'
                        }`}
                      >
                        {val === 'grant' ? '✓' : val === 'deny' ? '✗' : '—'}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-navy-500 text-xs mt-2">
        Click a cell to cycle: inherit (—) → grant (✓) → deny (✗)
      </p>
    </div>
  );
}

function ChannelCard({ channel, onEdit, onDelete, communityId }) {
  const Icon = TYPE_ICON[channel.type] ?? HashtagIcon;
  const [showPerms, setShowPerms] = useState(false);

  return (
    <div className="bg-navy-800 border border-navy-700 rounded-lg p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-start gap-3 min-w-0">
          <div className="mt-0.5 flex-shrink-0">
            <Icon className="h-5 w-5 text-navy-400" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sky-100 font-medium">{channel.name}</span>
              <ChannelTypeBadge type={channel.type} />
              {channel.sort_order !== undefined && channel.sort_order !== null && (
                <span className="text-xs text-navy-400">order: {channel.sort_order}</span>
              )}
              {channel.is_broadcast && (
                <span className="text-xs bg-purple-500/20 text-purple-400 px-1.5 py-0.5 rounded">Broadcast</span>
              )}
              {channel.is_temporary && (
                <span className="text-xs bg-sky-500/20 text-sky-400 px-1.5 py-0.5 rounded">
                  Temp {channel.temp_duration_minutes ? `${channel.temp_duration_minutes}m` : ''}
                </span>
              )}
              {channel.mirror_groups && channel.mirror_groups.length > 0 && (
                <span className="text-xs text-gold-400">
                  {channel.mirror_groups.length} mirror group{channel.mirror_groups.length !== 1 ? 's' : ''}
                </span>
              )}
            </div>
            {channel.description && (
              <p className="text-sm text-navy-300 mt-1">{channel.description}</p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          <button
            onClick={() => setShowPerms((v) => !v)}
            className="p-1.5 text-navy-400 hover:text-gold-400 rounded transition-colors"
            title="Toggle permissions"
          >
            {showPerms ? <ChevronUpIcon className="h-4 w-4" /> : <ChevronDownIcon className="h-4 w-4" />}
          </button>
          <button
            onClick={() => onEdit(channel)}
            className="p-1.5 text-navy-400 hover:text-sky-300 rounded transition-colors"
            title="Edit channel"
          >
            <PencilIcon className="h-4 w-4" />
          </button>
          <button
            onClick={() => onDelete(channel)}
            className="p-1.5 text-navy-400 hover:text-red-400 rounded transition-colors"
            title="Delete channel"
          >
            <TrashIcon className="h-4 w-4" />
          </button>
        </div>
      </div>
      {showPerms && (
        <ChannelPermissions communityId={communityId} channelId={channel.id} />
      )}
    </div>
  );
}

function ChannelForm({ initial, onSave, onCancel, saving }) {
  const [form, setForm] = useState(initial ?? EMPTY_FORM);

  function set(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  // Reset allow_ad_hoc_voice when switching away from voice type
  useEffect(() => {
    if (form.type !== 'voice' && form.allow_ad_hoc_voice) {
      setForm((prev) => ({ ...prev, allow_ad_hoc_voice: false }));
    }
  }, [form.type]); // eslint-disable-line react-hooks/exhaustive-deps

  function handleSubmit(e) {
    e.preventDefault();
    onSave(form);
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-navy-800 border border-navy-600 rounded-lg p-4 mb-6"
    >
      <h3 className="text-sky-100 font-semibold mb-4">
        {initial?.id ? 'Edit Channel' : 'Create Channel'}
      </h3>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label htmlFor="admin-interaction-channels-name" className="block text-sm font-medium text-navy-300 mb-1">Name</label>
          <input
            id="admin-interaction-channels-name"
            type="text"
            required
            value={form.name}
            onChange={(e) => set('name', e.target.value)}
            className="bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 w-full focus:border-sky-500"
            placeholder="Channel name"
          />
        </div>
        <div>
          <label htmlFor="admin-interaction-channels-type" className="block text-sm font-medium text-navy-300 mb-1">Type</label>
          <select
            id="admin-interaction-channels-type"
            data-testid="channel-type-select"
            value={form.type}
            onChange={(e) => set('type', e.target.value)}
            className="bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 w-full focus:border-sky-500"
          >
            {CHANNEL_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </div>
        <div className="sm:col-span-2">
          <label htmlFor="admin-interaction-channels-description" className="block text-sm font-medium text-navy-300 mb-1">Description</label>
          <textarea
            id="admin-interaction-channels-description"
            value={form.description}
            onChange={(e) => set('description', e.target.value)}
            rows={2}
            className="bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 w-full focus:border-sky-500 resize-none"
            placeholder="Optional description"
          />
        </div>
        <div>
          <label htmlFor="admin-interaction-channels-sort-order" className="block text-sm font-medium text-navy-300 mb-1">Sort Order</label>
          <input
            id="admin-interaction-channels-sort-order"
            type="number"
            value={form.sort_order}
            onChange={(e) => set('sort_order', parseInt(e.target.value, 10) || 0)}
            className="bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 w-full focus:border-sky-500"
          />
        </div>
        {form.type === 'voice' && (
          <div className="flex items-center gap-2 self-end pb-2">
            <input
              id="allow_ad_hoc_voice"
              type="checkbox"
              checked={form.allow_ad_hoc_voice}
              onChange={(e) => set('allow_ad_hoc_voice', e.target.checked)}
              className="h-4 w-4 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
            />
            <label htmlFor="allow_ad_hoc_voice" className="text-sm font-medium text-navy-300">
              Allow ad-hoc voice
            </label>
          </div>
        )}
      </div>

      {/* Capability toggles */}
      <div className="mt-4">
        <p className="text-sm font-medium text-navy-300 mb-2">Capabilities</p>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
          {[
            { id: 'has_chat', label: 'Chat' },
            { id: 'has_voice', label: 'Voice' },
            { id: 'has_video', label: 'Video' },
            { id: 'is_broadcast', label: 'Broadcast' },
            { id: 'is_temporary', label: 'Temporary' },
          ].map(({ id, label }) => (
            <label key={id} className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                id={id}
                checked={form[id]}
                onChange={(e) => set(id, e.target.checked)}
                className="h-4 w-4 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
              />
              <span className="text-sm text-navy-300">{label}</span>
            </label>
          ))}
        </div>
        {form.is_temporary && (
          <div className="mt-3 max-w-xs">
            <label htmlFor="admin-interaction-channels-duration-minutes" className="block text-sm font-medium text-navy-300 mb-1">
              Duration (minutes)
            </label>
            <input
              id="admin-interaction-channels-duration-minutes"
              type="number"
              min={1}
              value={form.temp_duration_minutes}
              onChange={(e) => set('temp_duration_minutes', Math.max(1, parseInt(e.target.value, 10) || 60))}
              className="bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 w-full focus:border-sky-500"
            />
          </div>
        )}
      </div>

      <div className="flex gap-2 mt-4">
        <button
          type="submit"
          disabled={saving}
          className="bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-4 py-2 font-medium disabled:opacity-50 transition-colors"
        >
          {saving ? 'Saving…' : initial?.id ? 'Save Changes' : 'Create Channel'}
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

function DeleteConfirm({ channel, onConfirm, onCancel, deleting }) {
  return (
    <div className="bg-navy-800 border border-red-500/40 rounded-lg p-4 mb-6">
      <p className="text-sky-100 mb-3">
        Delete channel <span className="font-semibold text-red-400">{channel.name}</span>? This
        cannot be undone.
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

const POLICY_OPTIONS = [
  { value: 'admin_only', label: 'Admin Only', desc: 'Only moderators and admins can create channels (default).' },
  { value: 'communicator', label: 'Communicator Role+', desc: 'Members with the Communicator role or higher can create channels.' },
  { value: 'all_members', label: 'All Members', desc: 'Any community member can create channels.' },
];

function ChannelCreationPolicy({ communityId }) {
  const [policy, setPolicy] = useState('admin_only');
  const [loadingPolicy, setLoadingPolicy] = useState(true);
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [policyError, setPolicyError] = useState(null);
  const [policySaved, setPolicySaved] = useState(false);

  useEffect(() => {
    async function load() {
      try {
        const res = await adminApi.getSettings(communityId);
        setPolicy(res.data?.settings?.channelCreationPolicy || 'admin_only');
      } catch {
        setPolicyError('Failed to load policy setting.');
      } finally {
        setLoadingPolicy(false);
      }
    }
    load();
  }, [communityId]);

  async function handleSave() {
    setSavingPolicy(true);
    setPolicyError(null);
    setPolicySaved(false);
    try {
      await adminApi.updateSettings(communityId, { channelCreationPolicy: policy });
      setPolicySaved(true);
      setTimeout(() => setPolicySaved(false), 3000);
    } catch (err) {
      setPolicyError(err?.response?.data?.message ?? 'Failed to save policy.');
    } finally {
      setSavingPolicy(false);
    }
  }

  const selectedOption = POLICY_OPTIONS.find((o) => o.value === policy);

  return (
    <div className="bg-navy-800 border border-navy-700 rounded-lg p-4 mb-6">
      <h2 className="text-sm font-semibold text-navy-400 uppercase tracking-wider mb-3">
        Channel Creation Policy
      </h2>
      {loadingPolicy ? (
        <div className="text-navy-400 text-sm">Loading…</div>
      ) : (
        <div className="flex items-start gap-4">
          <div className="flex-1 max-w-xs">
            <select
              aria-label="Channel creation policy"
              value={policy}
              onChange={(e) => setPolicy(e.target.value)}
              className="w-full bg-navy-900 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm focus:border-sky-500"
            >
              {POLICY_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
            {selectedOption && (
              <p className="text-navy-400 text-xs mt-1.5">{selectedOption.desc}</p>
            )}
          </div>
          <button
            onClick={handleSave}
            disabled={savingPolicy}
            className="bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50 transition-colors"
          >
            {savingPolicy ? 'Saving…' : 'Save'}
          </button>
        </div>
      )}
      {policyError && (
        <div className="text-red-400 text-xs mt-2">{policyError}</div>
      )}
      {policySaved && (
        <div className="text-green-400 text-xs mt-2">Policy saved.</div>
      )}
    </div>
  );
}

export default function AdminInteractionChannels() {
  const { communityId } = useParams();

  const [channels, setChannels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [showCreate, setShowCreate] = useState(false);
  const [editChannel, setEditChannel] = useState(null);
  const [deleteChannel, setDeleteChannel] = useState(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function loadChannels() {
    setLoading(true);
    setError(null);
    try {
      const res = await interactionApi.getChannels(communityId);
      // Normalize API camelCase fields to the snake_case the component expects
      const raw = res.data?.channels ?? [];
      setChannels(raw.map((c) => ({
        ...c,
        type: c.channelType ?? c.type,
        sort_order: c.sortOrder ?? c.sort_order ?? 0,
        allow_ad_hoc_voice: c.allowAdHocVoice ?? c.allow_ad_hoc_voice ?? false,
        has_chat: c.hasChat ?? c.has_chat ?? true,
        has_voice: c.hasVoice ?? c.has_voice ?? false,
        has_video: c.hasVideo ?? c.has_video ?? false,
        is_broadcast: c.isBroadcast ?? c.is_broadcast ?? false,
        is_temporary: c.isTemporary ?? c.is_temporary ?? false,
        temp_duration_minutes: c.tempDurationMinutes ?? c.temp_duration_minutes ?? 60,
      })));
    } catch (err) {
      setError(err?.response?.data?.message ?? 'Failed to load channels.');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadChannels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [communityId]);

  async function handleCreate(formData) {
    setSaving(true);
    try {
      // Backend expects channel_type (snake_case), form state uses type
      const payload = { ...formData, channel_type: formData.type };
      delete payload.type;
      await interactionApi.createChannel(communityId, payload);
      setShowCreate(false);
      await loadChannels();
    } catch (err) {
      setError(err?.response?.data?.message ?? 'Failed to create channel.');
    } finally {
      setSaving(false);
    }
  }

  async function handleUpdate(formData) {
    setSaving(true);
    try {
      // Backend expects channel_type (snake_case), form state uses type
      const payload = { ...formData, channel_type: formData.type };
      delete payload.type;
      await interactionApi.updateChannel(communityId, editChannel.id, payload);
      setEditChannel(null);
      await loadChannels();
    } catch (err) {
      setError(err?.response?.data?.message ?? 'Failed to update channel.');
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    setDeleting(true);
    try {
      await interactionApi.deleteChannel(communityId, deleteChannel.id);
      setDeleteChannel(null);
      await loadChannels();
    } catch (err) {
      setError(err?.response?.data?.message ?? 'Failed to delete channel.');
    } finally {
      setDeleting(false);
    }
  }

  function startEdit(channel) {
    setShowCreate(false);
    setDeleteChannel(null);
    setEditChannel(channel);
  }

  function startDelete(channel) {
    setShowCreate(false);
    setEditChannel(null);
    setDeleteChannel(channel);
  }

  function startCreate() {
    setEditChannel(null);
    setDeleteChannel(null);
    setShowCreate(true);
  }

  const grouped = CHANNEL_TYPES.reduce((acc, t) => {
    acc[t.value] = channels.filter((c) => c.type === t.value);
    return acc;
  }, {});

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-sky-100">Hub Channels</h1>
        <button
          onClick={startCreate}
          className="flex items-center gap-1.5 bg-gold-500 text-navy-900 hover:bg-gold-400 rounded-lg px-4 py-2 font-medium transition-colors"
        >
          <PlusIcon className="h-4 w-4" />
          Create Channel
        </button>
      </div>

      {/* Channel creation policy */}
      <ChannelCreationPolicy communityId={communityId} />

      {/* Inline form */}
      {showCreate && (
        <ChannelForm
          initial={null}
          onSave={handleCreate}
          onCancel={() => setShowCreate(false)}
          saving={saving}
        />
      )}
      {editChannel && (
        <ChannelForm
          initial={editChannel}
          onSave={handleUpdate}
          onCancel={() => setEditChannel(null)}
          saving={saving}
        />
      )}
      {deleteChannel && (
        <DeleteConfirm
          channel={deleteChannel}
          onConfirm={handleDelete}
          onCancel={() => setDeleteChannel(null)}
          deleting={deleting}
        />
      )}

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 text-red-400 rounded-lg px-4 py-3 mb-4 text-sm">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-navy-400 text-sm">Loading channels…</div>
      ) : channels.length === 0 ? (
        <div className="text-navy-400 text-sm">
          No channels yet. Click &ldquo;Create Channel&rdquo; to add one.
        </div>
      ) : (
        <div className="space-y-6">
          {CHANNEL_TYPES.map(({ value, label }) => {
            const group = grouped[value];
            if (!group || group.length === 0) return null;
            return (
              <div key={value}>
                <h2 className="text-sm font-semibold text-navy-400 uppercase tracking-wider mb-3">
                  {label}
                </h2>
                <div className="space-y-2">
                  {group.map((channel) => (
                    <ChannelCard
                      key={channel.id}
                      channel={channel}
                      onEdit={startEdit}
                      onDelete={startDelete}
                      communityId={communityId}
                    />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
