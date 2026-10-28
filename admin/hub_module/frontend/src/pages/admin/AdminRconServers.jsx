import { useState, useEffect, useRef, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import {
  ServerStackIcon, PlusIcon, PencilIcon, TrashIcon,
  CommandLineIcon, UserGroupIcon, ShieldCheckIcon,
  ClipboardDocumentListIcon, ArrowPathIcon, SignalIcon,
  PlayIcon, XMarkIcon, ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';
import { rconApi } from '../../services/api';
import GameTypeBadge from '../../components/GameTypeBadge';

const TABS = ['Servers', 'Console', 'Players', 'Access Policy', 'Command Log'];

const TAB_ICONS = {
  Servers: ServerStackIcon,
  Console: CommandLineIcon,
  Players: UserGroupIcon,
  'Access Policy': ShieldCheckIcon,
  'Command Log': ClipboardDocumentListIcon,
};

const SERVER_TYPES = [
  { value: 'rcon', label: 'RCON (Game Server)' },
  { value: 'mumble', label: 'Mumble (Voice)' },
  { value: 'teamspeak', label: 'TeamSpeak (Voice)' },
];

const GAME_TYPES = [
  'rust', 'minecraft', 'cs2', 'ark', 'valheim', 'palworld',
  'factorio', 'conan_exiles', '7dtd', 'squad', 'unturned',
  'terraria', 'starbound', 'source', 'mumble', 'teamspeak', 'other',
];

const VISIBILITY_OPTIONS = [
  { value: 'admin_only', label: 'Admin Only' },
  { value: 'members', label: 'Community Members' },
  { value: 'registered', label: 'Registered Users' },
];

const EMPTY_SERVER_FORM = {
  display_name: '', server_type: 'rcon', host: '', game_port: '',
  rcon_port: '', password: '', game_type: 'other', visibility: 'admin_only',
  game_name: '', metadata: {},
};

const TYPE_BADGES = {
  rcon: { bg: 'bg-amber-500/20', text: 'text-amber-300', border: 'border-amber-500/30', label: 'RCON' },
  mumble: { bg: 'bg-indigo-500/20', text: 'text-indigo-300', border: 'border-indigo-500/30', label: 'Mumble' },
  teamspeak: { bg: 'bg-sky-500/20', text: 'text-sky-300', border: 'border-sky-500/30', label: 'TeamSpeak' },
  status_only: { bg: 'bg-navy-500/20', text: 'text-fg-secondary', border: 'border-navy-500/30', label: 'Status' },
};

const VIS_BADGES = {
  admin_only: { bg: 'bg-red-500/20', text: 'text-red-300', border: 'border-red-500/30' },
  members: { bg: 'bg-green-500/20', text: 'text-green-300', border: 'border-green-500/30' },
  registered: { bg: 'bg-blue-500/20', text: 'text-blue-300', border: 'border-blue-500/30' },
};

function AdminRconServers() {
  const { communityId } = useParams();
  const [activeTab, setActiveTab] = useState('Servers');

  // Servers tab state
  const [servers, setServers] = useState([]);
  const [serversLoading, setServersLoading] = useState(true);
  const [serversError, setServersError] = useState('');

  // Server modal
  const [showServerModal, setShowServerModal] = useState(false);
  const [editingServer, setEditingServer] = useState(null);
  const [serverForm, setServerForm] = useState(EMPTY_SERVER_FORM);
  const [serverFormError, setServerFormError] = useState('');
  const [serverFormLoading, setServerFormLoading] = useState(false);

  // Console tab state
  const [selectedServerId, setSelectedServerId] = useState('');
  const [commandInput, setCommandInput] = useState('');
  const [consoleHistory, setConsoleHistory] = useState([]);
  const [commandLoading, setCommandLoading] = useState(false);
  const consoleEndRef = useRef(null);

  // Players tab state
  const [playerServerId, setPlayerServerId] = useState('');
  const [players, setPlayers] = useState([]);
  const [playersLoading, setPlayersLoading] = useState(false);

  // Access Policy tab state
  const [policyServerId, setPolicyServerId] = useState('');
  const [policy, setPolicy] = useState(null);
  const [policyLoading, setPolicyLoading] = useState(false);
  const [policySaving, setPolicySaving] = useState(false);

  // Command Log tab state
  const [commandLog, setCommandLog] = useState([]);
  const [logLoading, setLogLoading] = useState(false);

  // ── Data Loading ──

  const loadServers = useCallback(async () => {
    try {
      setServersLoading(true);
      setServersError('');
      const res = await rconApi.listServers(communityId);
      setServers(res.data?.servers || []);
    } catch (err) {
      console.error('Failed to load servers:', err);
      setServersError('Failed to load servers.');
    } finally {
      setServersLoading(false);
    }
  }, [communityId]);

  const loadPlayers = useCallback(async () => {
    if (!playerServerId) return;
    try {
      setPlayersLoading(true);
      const res = await rconApi.getPlayerList(communityId, playerServerId);
      const data = res.data?.data || res.data;
      setPlayers(data?.clients || data?.users || []);
    } catch {
      setPlayers([]);
    } finally {
      setPlayersLoading(false);
    }
  }, [communityId, playerServerId]);

  const loadPolicy = useCallback(async () => {
    if (!policyServerId) return;
    try {
      setPolicyLoading(true);
      const res = await rconApi.getAccessPolicy(communityId, policyServerId);
      setPolicy(res.data?.data?.policy || res.data?.policy || null);
    } catch {
      setPolicy(null);
    } finally {
      setPolicyLoading(false);
    }
  }, [communityId, policyServerId]);

  const loadCommandLog = useCallback(async () => {
    try {
      setLogLoading(true);
      const res = await rconApi.getCommandLog(communityId, { limit: 100 });
      setCommandLog(res.data?.log || []);
    } catch {
      setCommandLog([]);
    } finally {
      setLogLoading(false);
    }
  }, [communityId]);

  useEffect(() => { loadServers(); }, [loadServers]);
  useEffect(() => { if (activeTab === 'Players') loadPlayers(); }, [activeTab, loadPlayers]);
  useEffect(() => { if (activeTab === 'Access Policy') loadPolicy(); }, [activeTab, loadPolicy]);
  useEffect(() => { if (activeTab === 'Command Log') loadCommandLog(); }, [activeTab, loadCommandLog]);

  // Auto-select first server for console/players/policy
  useEffect(() => {
    if (servers.length > 0) {
      if (!selectedServerId) setSelectedServerId(String(servers[0].id));
      if (!playerServerId) setPlayerServerId(String(servers[0].id));
      if (!policyServerId) setPolicyServerId(String(servers[0].id));
    }
  }, [servers, selectedServerId, playerServerId, policyServerId]);

  // ── Server CRUD ──

  const openCreateModal = () => {
    setEditingServer(null);
    setServerForm(EMPTY_SERVER_FORM);
    setServerFormError('');
    setShowServerModal(true);
  };

  const openEditModal = (srv) => {
    setEditingServer(srv);
    setServerForm({
      display_name: srv.display_name || '',
      server_type: srv.server_type || 'rcon',
      host: srv.host || '',
      game_port: srv.game_port || '',
      rcon_port: srv.rcon_port || '',
      password: '',
      game_type: srv.game_type || 'other',
      visibility: srv.visibility || 'admin_only',
      game_name: srv.game_name || '',
      metadata: srv.metadata || {},
    });
    setServerFormError('');
    setShowServerModal(true);
  };

  const handleServerSubmit = async () => {
    if (!serverForm.display_name.trim()) {
      setServerFormError('Display name is required.');
      return;
    }
    if (!serverForm.host.trim()) {
      setServerFormError('Host is required.');
      return;
    }
    try {
      setServerFormLoading(true);
      const payload = { ...serverForm };
      if (!payload.password) delete payload.password;
      if (editingServer) {
        await rconApi.updateServer(communityId, editingServer.id, payload);
      } else {
        await rconApi.createServer(communityId, payload);
      }
      setShowServerModal(false);
      loadServers();
    } catch (err) {
      setServerFormError(err.response?.data?.error || 'Failed to save server.');
    } finally {
      setServerFormLoading(false);
    }
  };

  const handleDelete = async (srv) => {
    if (!window.confirm(`Delete "${srv.display_name || srv.game_name}"?`)) return;
    try {
      await rconApi.deleteServer(communityId, srv.id);
      loadServers();
    } catch (err) {
      console.error('Delete failed:', err);
    }
  };

  // ── Console ──

  const handleCommand = async (e) => {
    e.preventDefault();
    if (!commandInput.trim() || !selectedServerId) return;
    const cmd = commandInput.trim();
    setConsoleHistory((prev) => [...prev, { type: 'input', text: cmd, time: new Date() }]);
    setCommandInput('');
    setCommandLoading(true);
    try {
      const res = await rconApi.executeCommand(communityId, selectedServerId, { command: cmd });
      const data = res.data?.data || res.data;
      setConsoleHistory((prev) => [
        ...prev,
        { type: data?.success ? 'output' : 'error', text: data?.response || data?.error || 'No response', time: new Date() },
      ]);
    } catch (err) {
      setConsoleHistory((prev) => [
        ...prev,
        { type: 'error', text: err.response?.data?.error || 'Command failed', time: new Date() },
      ]);
    } finally {
      setCommandLoading(false);
    }
  };

  useEffect(() => {
    consoleEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [consoleHistory]);

  // ── Policy save ──

  const handlePolicySave = async () => {
    if (!policyServerId) return;
    try {
      setPolicySaving(true);
      await rconApi.updateAccessPolicy(communityId, policyServerId, policy || {});
      loadPolicy();
    } catch (err) {
      console.error('Policy save failed:', err);
    } finally {
      setPolicySaving(false);
    }
  };

  const updatePolicy = (key, value) => {
    setPolicy((prev) => ({ ...(prev || {}), [key]: value }));
  };

  // ── Kick/Ban from Players tab ──

  const handleKick = async (player) => {
    const name = player.nickname || player.name || player.session || player.clid;
    if (!window.confirm(`Kick "${name}"?`)) return;
    try {
      await rconApi.kickPlayer(communityId, playerServerId, { player: String(player.clid || player.session || name) });
      loadPlayers();
    } catch (err) {
      console.error('Kick failed:', err);
    }
  };

  const handleBan = async (player) => {
    const name = player.nickname || player.name || player.session || player.clid;
    if (!window.confirm(`Ban "${name}"? This cannot be easily undone.`)) return;
    try {
      await rconApi.banPlayer(communityId, playerServerId, { player: String(player.clid || player.session || name) });
      loadPlayers();
    } catch (err) {
      console.error('Ban failed:', err);
    }
  };

  // ── Server selector helper ──
  const ServerSelector = ({ value, onChange, label = 'Server' }) => (
    <div className="flex items-center gap-3">
      <label htmlFor="admin-rcon-servers-select" className="text-sm text-navy-400">{label}:</label>
      <select
        id="admin-rcon-servers-select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-1.5 text-sm focus:border-gold-500"
      >
        {servers.map((s) => (
          <option key={s.id} value={s.id}>{s.display_name || s.game_name}</option>
        ))}
      </select>
    </div>
  );

  // ── Render ──

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Game Servers</h1>
          <p className="text-navy-400 text-sm mt-1">Manage RCON, Mumble, and TeamSpeak servers</p>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-navy-700">
        <nav className="flex gap-1 -mb-px">
          {TABS.map((tab) => {
            const Icon = TAB_ICONS[tab];
            return (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === tab
                    ? 'border-gold-400 text-gold-400'
                    : 'border-transparent text-navy-400 hover:text-sky-300 hover:border-navy-500'
                }`}
              >
                <Icon className="w-4 h-4" />
                {tab}
              </button>
            );
          })}
        </nav>
      </div>

      {/* ═══ TAB 1: Servers ═══ */}
      {activeTab === 'Servers' && (
        <div className="space-y-4">
          <div className="flex justify-end">
            <button
              onClick={openCreateModal}
              className="flex items-center gap-2 px-4 py-2 bg-gold-500 text-navy-900 rounded-lg font-medium text-sm hover:bg-gold-400 transition-colors"
            >
              <PlusIcon className="w-4 h-4" />
              Add Server
            </button>
          </div>

          {serversError && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-300 text-sm">{serversError}</div>
          )}

          {serversLoading ? (
            <div className="flex justify-center py-12">
              <ArrowPathIcon className="w-6 h-6 text-navy-400 animate-spin" />
            </div>
          ) : !servers.length ? (
            <div className="text-center py-16">
              <ServerStackIcon className="w-12 h-12 text-navy-600 mx-auto mb-3" />
              <p className="text-navy-400">No servers configured yet.</p>
              <p className="text-navy-500 text-sm mt-1">Add a game or voice server to get started.</p>
            </div>
          ) : (
            <div className="bg-navy-900 border border-navy-700 rounded-xl overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-navy-700">
                    <th className="text-left px-4 py-3 text-xs font-semibold text-navy-400 uppercase tracking-wider">Server</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-navy-400 uppercase tracking-wider">Type</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-navy-400 uppercase tracking-wider">Host</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-navy-400 uppercase tracking-wider">Visibility</th>
                    <th className="text-right px-4 py-3 text-xs font-semibold text-navy-400 uppercase tracking-wider">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-navy-800">
                  {servers.map((srv) => {
                    const tb = TYPE_BADGES[srv.server_type] || TYPE_BADGES.status_only;
                    const vb = VIS_BADGES[srv.visibility] || VIS_BADGES.admin_only;
                    return (
                      <tr key={srv.id} className="hover:bg-navy-800/50 transition-colors">
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-3">
                            <GameTypeBadge type={srv.game_type} size="sm" showLabel={false} />
                            <div>
                              <p className="text-sm font-medium text-sky-100">{srv.display_name || srv.game_name}</p>
                              <p className="text-xs text-navy-500">{srv.game_type}</p>
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex items-center text-xs px-2 py-0.5 rounded-full border ${tb.bg} ${tb.text} ${tb.border}`}>
                            {tb.label}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <span className="text-sm text-navy-300 font-mono">
                            {srv.host}:{srv.rcon_port || srv.game_port || '—'}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex items-center text-xs px-2 py-0.5 rounded-full border ${vb.bg} ${vb.text} ${vb.border}`}>
                            {srv.visibility?.replace('_', ' ')}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <button aria-label={`Edit ${srv.display_name || srv.game_name}`} onClick={() => openEditModal(srv)} className="p-1.5 text-navy-400 hover:text-sky-300 rounded-lg hover:bg-navy-700">
                              <PencilIcon className="w-4 h-4" />
                            </button>
                            <button aria-label={`Delete ${srv.display_name || srv.game_name}`} onClick={() => handleDelete(srv)} className="p-1.5 text-navy-400 hover:text-red-400 rounded-lg hover:bg-navy-700">
                              <TrashIcon className="w-4 h-4" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ═══ TAB 2: Console ═══ */}
      {activeTab === 'Console' && (
        <div className="space-y-4">
          <ServerSelector value={selectedServerId} onChange={setSelectedServerId} />

          <div className="bg-navy-950 border border-navy-700 rounded-xl overflow-hidden">
            {/* Terminal output */}
            <div className="h-96 overflow-y-auto p-4 font-mono text-sm space-y-1">
              {consoleHistory.length === 0 && (
                <p className="text-navy-600 italic">Type a command below and press Enter...</p>
              )}
              {consoleHistory.map((entry, i) => (
                <div key={i} className={entry.type === 'input' ? 'text-gold-400' : entry.type === 'error' ? 'text-red-400' : 'text-green-300'}>
                  {entry.type === 'input' && <span className="text-navy-500">{'> '}</span>}
                  <span className="whitespace-pre-wrap">{entry.text}</span>
                </div>
              ))}
              <div ref={consoleEndRef} />
            </div>

            {/* Command input */}
            <form onSubmit={handleCommand} className="border-t border-navy-700 flex">
              <span className="px-3 py-2.5 text-gold-400 font-mono text-sm">{'>'}</span>
              <input
                aria-label="Enter RCON command"
                type="text"
                value={commandInput}
                onChange={(e) => setCommandInput(e.target.value)}
                placeholder="Enter RCON command..."
                disabled={commandLoading || !selectedServerId}
                className="flex-1 bg-transparent text-sky-100 font-mono text-sm px-0 py-2.5 placeholder-navy-600"
              />
              <button
                aria-label="Start"
                type="submit"
                disabled={commandLoading || !commandInput.trim()}
                className="px-4 py-2.5 text-gold-400 hover:text-gold-300 disabled:text-navy-600"
              >
                <PlayIcon className="w-4 h-4" />
              </button>
            </form>
          </div>
        </div>
      )}

      {/* ═══ TAB 3: Players ═══ */}
      {activeTab === 'Players' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <ServerSelector value={playerServerId} onChange={setPlayerServerId} />
            <button onClick={loadPlayers} className="flex items-center gap-2 px-3 py-1.5 text-sm text-navy-300 hover:text-sky-300 bg-navy-800 rounded-lg border border-navy-600">
              <ArrowPathIcon className="w-4 h-4" />
              Refresh
            </button>
          </div>

          {playersLoading ? (
            <div className="flex justify-center py-12"><ArrowPathIcon className="w-6 h-6 text-navy-400 animate-spin" /></div>
          ) : !players.length ? (
            <div className="text-center py-12">
              <UserGroupIcon className="w-10 h-10 text-navy-600 mx-auto mb-2" />
              <p className="text-navy-400 text-sm">No players connected or unable to fetch player list.</p>
            </div>
          ) : (
            <div className="bg-navy-900 border border-navy-700 rounded-xl overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-navy-700">
                    <th className="text-left px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Player</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-navy-400 uppercase">ID</th>
                    <th className="text-right px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-navy-800">
                  {players.map((p, i) => (
                    <tr key={i} className="hover:bg-navy-800/50">
                      <td className="px-4 py-3 text-sm text-sky-100">{p.nickname || p.name || `Player ${i + 1}`}</td>
                      <td className="px-4 py-3 text-sm text-navy-400 font-mono">{p.clid || p.session || '—'}</td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center justify-end gap-1">
                          <button onClick={() => handleKick(p)} className="px-2 py-1 text-xs text-amber-400 hover:bg-amber-500/20 rounded border border-amber-500/30">
                            Kick
                          </button>
                          <button onClick={() => handleBan(p)} className="px-2 py-1 text-xs text-red-400 hover:bg-red-500/20 rounded border border-red-500/30">
                            Ban
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ═══ TAB 4: Access Policy ═══ */}
      {activeTab === 'Access Policy' && (
        <div className="space-y-6">
          <div className="flex items-center justify-between">
            <ServerSelector value={policyServerId} onChange={setPolicyServerId} />
            <button
              onClick={handlePolicySave}
              disabled={policySaving}
              className="flex items-center gap-2 px-4 py-2 bg-gold-500 text-navy-900 rounded-lg font-medium text-sm hover:bg-gold-400 disabled:opacity-50"
            >
              {policySaving ? 'Saving...' : 'Save Policy'}
            </button>
          </div>

          {policyLoading ? (
            <div className="flex justify-center py-12"><ArrowPathIcon className="w-6 h-6 text-navy-400 animate-spin" /></div>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {/* Membership gate */}
              <div className="bg-navy-900 border border-navy-700 rounded-xl p-5 space-y-4">
                <h3 className="text-sm font-semibold text-sky-100">Membership Requirement</h3>
                <label className="flex items-center gap-3">
                  <input
                    type="checkbox"
                    checked={policy?.require_community_member || false}
                    onChange={(e) => updatePolicy('require_community_member', e.target.checked)}
                    className="w-4 h-4 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
                  />
                  <span className="text-sm text-navy-300">Only allow community members to play (whitelist sync)</span>
                </label>
                <div>
                  <label htmlFor="admin-rcon-servers-min-reputation-to-join-300-850" className="text-xs text-navy-400 block mb-1">Min Reputation to Join (300–850)</label>
                  <input
                    id="admin-rcon-servers-min-reputation-to-join-300-850"
                    type="number"
                    min={300}
                    max={850}
                    value={policy?.min_reputation_to_join || ''}
                    onChange={(e) => updatePolicy('min_reputation_to_join', e.target.value ? parseInt(e.target.value) : null)}
                    placeholder="None"
                    className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-1.5 text-sm focus:border-gold-500"
                  />
                </div>
              </div>

              {/* Auto-kick */}
              <div className="bg-navy-900 border border-navy-700 rounded-xl p-5 space-y-4">
                <h3 className="text-sm font-semibold text-sky-100">Auto-Kick</h3>
                <label className="flex items-center gap-3">
                  <input
                    type="checkbox"
                    checked={policy?.auto_kick_enabled || false}
                    onChange={(e) => updatePolicy('auto_kick_enabled', e.target.checked)}
                    className="w-4 h-4 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
                  />
                  <span className="text-sm text-navy-300">Auto-kick players below reputation threshold</span>
                </label>
                <div>
                  <label htmlFor="admin-rcon-servers-input" className="text-xs text-navy-400 block mb-1">Kick Threshold: {policy?.auto_kick_threshold || 450}</label>
                  <input
                    id="admin-rcon-servers-input"
                    type="range"
                    min={300}
                    max={850}
                    value={policy?.auto_kick_threshold || 450}
                    onChange={(e) => updatePolicy('auto_kick_threshold', parseInt(e.target.value))}
                    className="w-full accent-gold-500"
                  />
                  <div className="flex justify-between text-xs text-navy-500 mt-1">
                    <span>300 (Poor)</span><span>575 (Fair)</span><span>850 (Excellent)</span>
                  </div>
                </div>
              </div>

              {/* Auto-ban */}
              <div className="bg-navy-900 border border-navy-700 rounded-xl p-5 space-y-4">
                <h3 className="text-sm font-semibold text-sky-100">Auto-Ban</h3>
                <label className="flex items-center gap-3">
                  <input
                    type="checkbox"
                    checked={policy?.auto_ban_enabled || false}
                    onChange={(e) => updatePolicy('auto_ban_enabled', e.target.checked)}
                    className="w-4 h-4 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
                  />
                  <span className="text-sm text-navy-300">Auto-ban players below reputation threshold</span>
                </label>
                <div>
                  <label htmlFor="admin-rcon-servers-input-2" className="text-xs text-navy-400 block mb-1">Ban Threshold: {policy?.auto_ban_threshold || 350}</label>
                  <input
                    id="admin-rcon-servers-input-2"
                    type="range"
                    min={300}
                    max={850}
                    value={policy?.auto_ban_threshold || 350}
                    onChange={(e) => updatePolicy('auto_ban_threshold', parseInt(e.target.value))}
                    className="w-full accent-gold-500"
                  />
                </div>
                <div>
                  <label htmlFor="admin-rcon-servers-ban-duration" className="text-xs text-navy-400 block mb-1">Ban Duration</label>
                  <select
                    id="admin-rcon-servers-ban-duration"
                    value={policy?.auto_ban_duration_hours ?? ''}
                    onChange={(e) => updatePolicy('auto_ban_duration_hours', e.target.value ? parseInt(e.target.value) : null)}
                    className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-1.5 text-sm focus:border-gold-500"
                  >
                    <option value="">Permanent</option>
                    <option value="1">1 hour</option>
                    <option value="24">24 hours</option>
                    <option value="168">7 days</option>
                    <option value="720">30 days</option>
                  </select>
                </div>
              </div>

              {/* Settings */}
              <div className="bg-navy-900 border border-navy-700 rounded-xl p-5 space-y-4">
                <h3 className="text-sm font-semibold text-sky-100">Settings</h3>
                <div>
                  <label htmlFor="admin-rcon-servers-sync-interval" className="text-xs text-navy-400 block mb-1">Sync Interval</label>
                  <select
                    id="admin-rcon-servers-sync-interval"
                    value={policy?.sync_interval_minutes || 5}
                    onChange={(e) => updatePolicy('sync_interval_minutes', parseInt(e.target.value))}
                    className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-1.5 text-sm focus:border-gold-500"
                  >
                    <option value="1">Every 1 minute</option>
                    <option value="5">Every 5 minutes</option>
                    <option value="15">Every 15 minutes</option>
                    <option value="30">Every 30 minutes</option>
                  </select>
                </div>
                <label className="flex items-center gap-3">
                  <input
                    type="checkbox"
                    checked={policy?.notify_on_action ?? true}
                    onChange={(e) => updatePolicy('notify_on_action', e.target.checked)}
                    className="w-4 h-4 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
                  />
                  <span className="text-sm text-navy-300">Notify admins when auto-action fires</span>
                </label>
                <label className="flex items-center gap-3">
                  <input
                    type="checkbox"
                    checked={policy?.sync_to_community || false}
                    onChange={(e) => updatePolicy('sync_to_community', e.target.checked)}
                    className="w-4 h-4 rounded border-navy-600 bg-navy-800 text-gold-500 focus:ring-gold-500"
                  />
                  <span className="text-sm text-navy-300">Sync auto-actions to community moderation (cross-platform)</span>
                </label>
                <button
                  onClick={async () => {
                    try { await rconApi.triggerEnforcement(communityId, policyServerId); alert('Enforcement cycle triggered.'); } catch { alert('Failed.'); }
                  }}
                  className="flex items-center gap-2 px-3 py-1.5 text-sm text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-lg hover:bg-amber-500/20"
                >
                  <PlayIcon className="w-4 h-4" />
                  Enforce Now
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ═══ TAB 5: Command Log ═══ */}
      {activeTab === 'Command Log' && (
        <div className="space-y-4">
          <div className="flex justify-end">
            <button onClick={loadCommandLog} className="flex items-center gap-2 px-3 py-1.5 text-sm text-navy-300 hover:text-sky-300 bg-navy-800 rounded-lg border border-navy-600">
              <ArrowPathIcon className="w-4 h-4" />
              Refresh
            </button>
          </div>

          {logLoading ? (
            <div className="flex justify-center py-12"><ArrowPathIcon className="w-6 h-6 text-navy-400 animate-spin" /></div>
          ) : !commandLog.length ? (
            <div className="text-center py-12">
              <ClipboardDocumentListIcon className="w-10 h-10 text-navy-600 mx-auto mb-2" />
              <p className="text-navy-400 text-sm">No commands have been executed yet.</p>
            </div>
          ) : (
            <div className="bg-navy-900 border border-navy-700 rounded-xl overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-navy-700">
                    <th className="text-left px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Time</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-navy-400 uppercase">User</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Server</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Command</th>
                    <th className="text-center px-4 py-3 text-xs font-semibold text-navy-400 uppercase">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-navy-800">
                  {commandLog.map((entry) => (
                    <tr key={entry.id} className="hover:bg-navy-800/50">
                      <td className="px-4 py-3 text-xs text-navy-400">
                        {entry.executed_at ? new Date(entry.executed_at).toLocaleString() : '—'}
                      </td>
                      <td className="px-4 py-3 text-sm text-navy-300">{entry.user_name || '—'}</td>
                      <td className="px-4 py-3 text-sm text-navy-300">{entry.server_name || '—'}</td>
                      <td className="px-4 py-3 text-sm text-sky-100 font-mono">{entry.command}</td>
                      <td className="px-4 py-3 text-center">
                        <span className={`inline-flex items-center text-xs px-2 py-0.5 rounded-full ${
                          entry.success ? 'bg-green-500/20 text-green-300' : 'bg-red-500/20 text-red-300'
                        }`}>
                          {entry.success ? 'OK' : 'FAIL'}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ═══ Server Create/Edit Modal ═══ */}
      {showServerModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
          <div role="dialog" aria-modal="true" aria-labelledby="admin-rcon-servers-dialog-1-title" className="bg-navy-900 border border-navy-700 rounded-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto shadow-2xl">
            <div className="flex items-center justify-between p-5 border-b border-navy-700">
              <h2 id="admin-rcon-servers-dialog-1-title" className="text-lg font-bold text-sky-100">
                {editingServer ? 'Edit Server' : 'Add Server'}
              </h2>
              <button aria-label="Close" onClick={() => setShowServerModal(false)} className="text-navy-400 hover:text-sky-300">
                <XMarkIcon className="w-5 h-5" />
              </button>
            </div>

            <div className="p-5 space-y-4">
              {serverFormError && (
                <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-300 text-sm flex items-center gap-2">
                  <ExclamationTriangleIcon className="w-4 h-4 flex-shrink-0" />
                  {serverFormError}
                </div>
              )}

              <div>
                <label htmlFor="admin-rcon-servers-display-name" className="text-xs text-navy-400 block mb-1">Display Name *</label>
                <input
                  id="admin-rcon-servers-display-name"
                  type="text"
                  value={serverForm.display_name}
                  onChange={(e) => setServerForm((p) => ({ ...p, display_name: e.target.value }))}
                  placeholder="My Rust Server"
                  className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm focus:border-gold-500"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label htmlFor="admin-rcon-servers-server-type" className="text-xs text-navy-400 block mb-1">Server Type</label>
                  <select
                    id="admin-rcon-servers-server-type"
                    value={serverForm.server_type}
                    onChange={(e) => setServerForm((p) => ({ ...p, server_type: e.target.value }))}
                    className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm focus:border-gold-500"
                  >
                    {SERVER_TYPES.map((t) => (
                      <option key={t.value} value={t.value}>{t.label}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor="admin-rcon-servers-game-type" className="text-xs text-navy-400 block mb-1">Game Type</label>
                  <select
                    id="admin-rcon-servers-game-type"
                    value={serverForm.game_type}
                    onChange={(e) => setServerForm((p) => ({ ...p, game_type: e.target.value }))}
                    className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm focus:border-gold-500"
                  >
                    {GAME_TYPES.map((g) => (
                      <option key={g} value={g}>{g.replace('_', ' ')}</option>
                    ))}
                  </select>
                </div>
              </div>

              <div>
                <label htmlFor="admin-rcon-servers-host" className="text-xs text-navy-400 block mb-1">Host *</label>
                <input
                  id="admin-rcon-servers-host"
                  type="text"
                  value={serverForm.host}
                  onChange={(e) => setServerForm((p) => ({ ...p, host: e.target.value }))}
                  placeholder="play.example.com"
                  className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm focus:border-gold-500"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label htmlFor="admin-rcon-servers-game-port" className="text-xs text-navy-400 block mb-1">Game Port</label>
                  <input
                    id="admin-rcon-servers-game-port"
                    type="number"
                    value={serverForm.game_port}
                    onChange={(e) => setServerForm((p) => ({ ...p, game_port: e.target.value }))}
                    placeholder="27015"
                    className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm focus:border-gold-500"
                  />
                </div>
                <div>
                  <label htmlFor="admin-rcon-servers-rcon-query-port" className="text-xs text-navy-400 block mb-1">RCON/Query Port</label>
                  <input
                    id="admin-rcon-servers-rcon-query-port"
                    type="number"
                    value={serverForm.rcon_port}
                    onChange={(e) => setServerForm((p) => ({ ...p, rcon_port: e.target.value }))}
                    placeholder="27015"
                    className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm focus:border-gold-500"
                  />
                </div>
              </div>

              <div>
                <label htmlFor="admin-rcon-servers-input-3" className="text-xs text-navy-400 block mb-1">
                  {editingServer ? 'Password (leave blank to keep)' : 'Password / Secret'}
                </label>
                <input
                  id="admin-rcon-servers-input-3"
                  type="password"
                  value={serverForm.password}
                  onChange={(e) => setServerForm((p) => ({ ...p, password: e.target.value }))}
                  placeholder="••••••••"
                  className="w-full bg-navy-800 border border-navy-600 text-sky-100 rounded-lg px-3 py-2 text-sm focus:border-gold-500"
                />
              </div>

              <div>
                <label className="text-xs text-navy-400 block mb-2">Visibility</label>
                <div className="flex gap-3">
                  {VISIBILITY_OPTIONS.map((opt) => (
                    <label key={opt.value} className="flex items-center gap-2">
                      <input
                        type="radio"
                        name="visibility"
                        value={opt.value}
                        checked={serverForm.visibility === opt.value}
                        onChange={(e) => setServerForm((p) => ({ ...p, visibility: e.target.value }))}
                        className="text-gold-500 focus:ring-gold-500"
                      />
                      <span className="text-sm text-navy-300">{opt.label}</span>
                    </label>
                  ))}
                </div>
              </div>
            </div>

            <div className="flex justify-end gap-3 p-5 border-t border-navy-700">
              <button
                onClick={() => setShowServerModal(false)}
                className="px-4 py-2 text-sm text-navy-300 hover:text-sky-300 bg-navy-800 rounded-lg border border-navy-600"
              >
                Cancel
              </button>
              <button
                onClick={handleServerSubmit}
                disabled={serverFormLoading}
                className="px-4 py-2 text-sm bg-gold-500 text-navy-900 rounded-lg font-medium hover:bg-gold-400 disabled:opacity-50"
              >
                {serverFormLoading ? 'Saving...' : editingServer ? 'Update' : 'Create'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminRconServers;
