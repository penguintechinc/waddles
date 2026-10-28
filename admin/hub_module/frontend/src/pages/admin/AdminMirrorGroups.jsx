import { useState, useEffect, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { getPlatformIcon } from '../../utils/platformConfig';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

const PLATFORM_ICONS = new Proxy({}, { get: (_, key) => getPlatformIcon(key) });

const DIRECTION_LABELS = {
  bidirectional: '↔️ Bidirectional',
  send_only: '→ Send Only',
  receive_only: '← Receive Only',
};

const MESSAGE_TYPES = [
  { id: 'chat', label: 'Chat Messages' },
  { id: 'sub', label: 'Subscriptions' },
  { id: 'follow', label: 'Follows' },
  { id: 'raid', label: 'Raids' },
  { id: 'donation', label: 'Donations' },
  { id: 'cheer', label: 'Cheers/Bits' },
];

function AdminMirrorGroups() {
  const { communityId } = useParams();
  const [groups, setGroups] = useState([]);
  const [servers, setServers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedGroup, setSelectedGroup] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showAddMemberModal, setShowAddMemberModal] = useState(false);
  const [message, setMessage] = useState(null);
  const [actionLoading, setActionLoading] = useState(false);


  useEffect(() => {
    fetchData();
  }, [communityId]);

  async function fetchData() {
    setLoading(true);
    try {
      const [groupsRes, serversRes] = await Promise.all([
        adminApi.getMirrorGroups(communityId),
        adminApi.getServers(communityId, { status: 'approved' }),
      ]);
      setGroups(groupsRes.data.groups || []);
      setServers(serversRes.data.servers || []);
    } catch (err) {
      console.error('Failed to fetch data:', err);
      setMessage({ type: 'error', text: 'Failed to load mirror groups' });
    } finally {
      setLoading(false);
    }
  }

  async function loadGroupDetails(groupId) {
    try {
      const response = await adminApi.getMirrorGroup(communityId, groupId);
      setSelectedGroup(response.data.group);
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to load group details' });
    }
  }

  async function handleCreateGroup(data) {
    setActionLoading(true);
    try {
      await adminApi.createMirrorGroup(communityId, {
        name: data.name,
        description: data.description,
        config: { messageTypes: data.messageTypes || ['chat'] },
      });
      setMessage({ type: 'success', text: 'Mirror group created' });
      setShowCreateModal(false);
      fetchData();
    } catch (err) {
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to create group' });
      throw err;
    } finally {
      setActionLoading(false);
    }
  }

  async function handleDeleteGroup(groupId) {
    if (!confirm('Are you sure you want to delete this mirror group?')) return;
    setActionLoading(true);
    try {
      await adminApi.deleteMirrorGroup(communityId, groupId);
      setMessage({ type: 'success', text: 'Mirror group deleted' });
      setSelectedGroup(null);
      fetchData();
    } catch (err) {
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to delete group' });
    } finally {
      setActionLoading(false);
    }
  }

  async function handleToggleGroupActive(groupId, isActive) {
    setActionLoading(true);
    try {
      await adminApi.updateMirrorGroup(communityId, groupId, { isActive: !isActive });
      setMessage({ type: 'success', text: `Mirror group ${isActive ? 'disabled' : 'enabled'}` });
      fetchData();
      if (selectedGroup?.id === groupId) {
        loadGroupDetails(groupId);
      }
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to update group' });
    } finally {
      setActionLoading(false);
    }
  }

  async function handleAddMember(data) {
    if (!selectedGroup) return;
    setActionLoading(true);
    try {
      await adminApi.addMirrorGroupMember(communityId, selectedGroup.id, {
        communityServerId: parseInt(data.serverId, 10),
        direction: data.direction,
      });
      setMessage({ type: 'success', text: 'Server added to mirror group' });
      setShowAddMemberModal(false);
      loadGroupDetails(selectedGroup.id);
    } catch (err) {
      setMessage({ type: 'error', text: err.response?.data?.error?.message || 'Failed to add server' });
      throw err;
    } finally {
      setActionLoading(false);
    }
  }

  async function handleRemoveMember(memberId) {
    if (!selectedGroup) return;
    setActionLoading(true);
    try {
      await adminApi.removeMirrorGroupMember(communityId, selectedGroup.id, memberId);
      setMessage({ type: 'success', text: 'Server removed from mirror group' });
      loadGroupDetails(selectedGroup.id);
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to remove server' });
    } finally {
      setActionLoading(false);
    }
  }

  async function handleUpdateMemberDirection(memberId, direction) {
    if (!selectedGroup) return;
    setActionLoading(true);
    try {
      await adminApi.updateMirrorGroupMember(communityId, selectedGroup.id, memberId, { direction });
      loadGroupDetails(selectedGroup.id);
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to update direction' });
    } finally {
      setActionLoading(false);
    }
  }

  // Get servers not already in the selected group
  const availableServers = selectedGroup
    ? servers.filter(s => !selectedGroup.members?.some(m => m.server.id === s.id))
    : servers;

  // Field definitions for Create Mirror Group modal
  const createGroupFields = useMemo(() => [
    {
      name: 'name',
      type: 'text',
      label: 'Name',
      required: true,
      placeholder: 'Enter group name',
    },
    {
      name: 'description',
      type: 'textarea',
      label: 'Description',
      placeholder: 'Optional description',
      rows: 2,
    },
    {
      name: 'messageTypes',
      type: 'checkbox_multi',
      label: 'Message Types',
      defaultValue: ['chat'],
      options: MESSAGE_TYPES.map(t => ({ value: t.id, label: t.label })),
      helpText: 'Select which message types to mirror',
    },
  ], []);

  // Field definitions for Add Server modal
  const addServerFields = useMemo(() => [
    {
      name: 'serverId',
      type: 'select',
      label: 'Server',
      required: true,
      options: availableServers.map(server => ({
        value: String(server.id),
        label: `${PLATFORM_ICONS[server.platform] || ''} ${server.platformServerName} (${server.platform})`,
      })),
    },
    {
      name: 'direction',
      type: 'select',
      label: 'Direction',
      defaultValue: 'bidirectional',
      options: [
        { value: 'bidirectional', label: 'Bidirectional (send and receive)' },
        { value: 'send_only', label: 'Send Only (messages go out)' },
        { value: 'receive_only', label: 'Receive Only (messages come in)' },
      ],
    },
  ], [availableServers]);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-sky-100">Chat Mirroring</h1>
        <button onClick={() => setShowCreateModal(true)} className="btn btn-primary">
          + New Mirror Group
        </button>
      </div>

      {message && (
        <div className={`mb-4 p-4 rounded-lg border ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border-red-500/30'
        }`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">×</button>
        </div>
      )}

      {loading ? (
        <div className="flex justify-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
        </div>
      ) : (
        <div className="grid gap-6 lg:grid-cols-3">
          {/* Groups List */}
          <div className="lg:col-span-1">
            <div className="card p-4">
              <h2 className="text-lg font-medium text-sky-100 mb-4">Mirror Groups</h2>
              {groups.length === 0 ? (
                <div className="text-center py-8 text-navy-400">
                  <div className="text-3xl mb-2">🔄</div>
                  <p>No mirror groups yet</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {groups.map((group) => (
                    <button
                      key={group.id}
                      onClick={() => loadGroupDetails(group.id)}
                      className={`w-full text-left p-3 rounded-lg transition-colors ${
                        selectedGroup?.id === group.id
                          ? 'bg-sky-500/20 border border-sky-500/30'
                          : 'bg-navy-800 hover:bg-navy-700 border border-transparent'
                      }`}
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-medium text-sky-100">{group.name}</span>
                        <span className={`text-xs px-2 py-0.5 rounded ${
                          group.isActive
                            ? 'bg-emerald-500/20 text-emerald-300'
                            : 'bg-red-500/20 text-red-300'
                        }`}>
                          {group.isActive ? 'Active' : 'Disabled'}
                        </span>
                      </div>
                      <div className="text-xs text-navy-400 mt-1">
                        {group.memberCount} server{group.memberCount !== 1 ? 's' : ''}
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Group Details */}
          <div className="lg:col-span-2">
            {selectedGroup ? (
              <div className="card p-6">
                <div className="flex items-start justify-between mb-6">
                  <div>
                    <h2 className="text-xl font-bold text-sky-100">{selectedGroup.name}</h2>
                    {selectedGroup.description && (
                      <p className="text-navy-400 mt-1">{selectedGroup.description}</p>
                    )}
                    <div className="flex flex-wrap gap-2 mt-3">
                      {(selectedGroup.config?.messageTypes || ['chat']).map((type) => (
                        <span key={type} className="text-xs px-2 py-1 bg-navy-700 text-navy-300 rounded">
                          {MESSAGE_TYPES.find(t => t.id === type)?.label || type}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="flex space-x-2">
                    <button
                      onClick={() => handleToggleGroupActive(selectedGroup.id, selectedGroup.isActive)}
                      disabled={actionLoading}
                      className={`btn text-sm ${
                        selectedGroup.isActive
                          ? 'bg-red-500/20 text-red-300 hover:bg-red-500/30'
                          : 'btn-primary'
                      }`}
                    >
                      {selectedGroup.isActive ? 'Disable' : 'Enable'}
                    </button>
                    <button
                      onClick={() => handleDeleteGroup(selectedGroup.id)}
                      disabled={actionLoading}
                      className="btn bg-red-500/20 text-red-300 hover:bg-red-500/30 text-sm"
                    >
                      Delete
                    </button>
                  </div>
                </div>

                {/* Members */}
                <div className="mb-4 flex items-center justify-between">
                  <h3 className="text-lg font-medium text-sky-100">Connected Servers</h3>
                  <button
                    onClick={() => setShowAddMemberModal(true)}
                    disabled={availableServers.length === 0}
                    className="btn btn-secondary text-sm disabled:opacity-50"
                  >
                    + Add Server
                  </button>
                </div>

                {selectedGroup.members?.length === 0 ? (
                  <div className="text-center py-8 bg-navy-800 rounded-lg text-navy-400">
                    <p>No servers in this mirror group yet</p>
                    <p className="text-sm mt-1">Add servers to start mirroring messages</p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {selectedGroup.members?.map((member) => (
                      <div key={member.id} className="flex items-center justify-between p-4 bg-navy-800 rounded-lg">
                        <div className="flex items-center space-x-3">
                          <span className="text-2xl">{PLATFORM_ICONS[member.server.platform] || '🌐'}</span>
                          <div>
                            <div className="font-medium text-sky-100">{member.server.platformServerName}</div>
                            <div className="text-xs text-navy-500">
                              {member.server.platform} · {member.server.platformServerId}
                            </div>
                          </div>
                        </div>
                        <div className="flex items-center space-x-3">
                          <select
                            aria-label="Member sync direction"
                            value={member.direction}
                            onChange={(e) => handleUpdateMemberDirection(member.id, e.target.value)}
                            className="input text-sm py-1"
                          >
                            <option value="bidirectional">↔️ Bidirectional</option>
                            <option value="send_only">→ Send Only</option>
                            <option value="receive_only">← Receive Only</option>
                          </select>
                          <button
                            onClick={() => handleRemoveMember(member.id)}
                            className="text-red-400 hover:text-red-300"
                          >
                            ✕
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {/* Visual Diagram */}
                {selectedGroup.members?.length >= 2 && (
                  <div className="mt-6 p-4 bg-navy-800 rounded-lg">
                    <h4 className="text-sm font-medium text-navy-400 mb-3">Message Flow</h4>
                    <div className="flex flex-wrap items-center justify-center gap-4">
                      {selectedGroup.members?.map((member, idx) => (
                        <div key={member.id} className="flex items-center">
                          <div className="text-center p-3 bg-navy-700 rounded-lg">
                            <div className="text-lg">{PLATFORM_ICONS[member.server.platform]}</div>
                            <div className="text-xs text-sky-100 mt-1">{member.server.platformServerName}</div>
                          </div>
                          {idx < selectedGroup.members.length - 1 && (
                            <div className="mx-2 text-navy-500">
                              {member.direction === 'bidirectional' ? '↔' : member.direction === 'send_only' ? '→' : '←'}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="card p-12 text-center">
                <div className="text-4xl mb-4">🔄</div>
                <h3 className="text-lg font-medium text-sky-100 mb-2">Select a Mirror Group</h3>
                <p className="text-navy-400">
                  Choose a group from the list to view and manage its settings
                </p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Create Mirror Group Modal */}
      <FormModalBuilder
        title="Create Mirror Group"
        fields={createGroupFields}
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={handleCreateGroup}
        submitButtonText="Create"
        cancelButtonText="Cancel"
        width="md"
        themeMode="dark"
        colors={WADDLES_COLORS}
      />

      {/* Add Server Modal */}
      <FormModalBuilder
        title="Add Server to Mirror Group"
        fields={addServerFields}
        isOpen={showAddMemberModal && selectedGroup !== null}
        onClose={() => setShowAddMemberModal(false)}
        onSubmit={handleAddMember}
        submitButtonText="Add Server"
        cancelButtonText="Cancel"
        width="md"
        themeMode="dark"
        colors={WADDLES_COLORS}
      />
    </div>
  );
}

export default AdminMirrorGroups;
