import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import {
  CommandLineIcon,
  MagnifyingGlassIcon,
  ExclamationTriangleIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { adminApi } from '../../services/api';

const PLATFORM_BADGE = {
  discord: 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30',
  slack: 'bg-green-500/20 text-green-300 border-green-500/30',
  twitch: 'bg-purple-500/20 text-purple-300 border-purple-500/30',
  both: 'bg-navy-600/60 text-navy-300 border-navy-500/30',
};

function getPlatformFromCommand(command, platform) {
  if (platform) return platform.toLowerCase();
  if (command?.startsWith('!')) return 'twitch';
  if (command?.startsWith('/')) return 'discord';
  return 'both';
}

function PlatformBadge({ command, platform }) {
  const resolved = getPlatformFromCommand(command, platform);
  const label = resolved.charAt(0).toUpperCase() + resolved.slice(1);
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-medium border ${PLATFORM_BADGE[resolved] || PLATFORM_BADGE.both}`}>
      {label}
    </span>
  );
}

function AdminCommands() {
  const { communityId } = useParams();
  const [commands, setCommands] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Filters
  const [search, setSearch] = useState('');
  const [platformFilter, setPlatformFilter] = useState('all');
  const [moduleFilter, setModuleFilter] = useState('all');

  useEffect(() => {
    loadCommands();
  }, [communityId]);

  const loadCommands = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await adminApi.getCommands(communityId);
      setCommands(response.data.commands || []);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load commands');
    } finally {
      setLoading(false);
    }
  };

  const modules = [...new Set(commands.map((c) => c.module).filter(Boolean))];

  const filtered = commands.filter((cmd) => {
    const matchSearch = !search || cmd.command?.toLowerCase().includes(search.toLowerCase());
    const resolvedPlatform = getPlatformFromCommand(cmd.command, cmd.platform);
    const matchPlatform =
      platformFilter === 'all' || resolvedPlatform === platformFilter;
    const matchModule = moduleFilter === 'all' || cmd.module === moduleFilter;
    return matchSearch && matchPlatform && matchModule;
  });

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-sky-100">Bot Commands</h1>
        <p className="text-navy-400 mt-1">
          All registered bot commands for this community (read-only)
        </p>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <ExclamationTriangleIcon className="w-5 h-5 text-red-400" />
            <span className="text-red-400">{error}</span>
          </div>
          <button aria-label="Close" onClick={() => setError(null)} className="text-red-400 hover:text-red-300">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-wrap gap-3">
        <div className="relative flex-1 min-w-48">
          <MagnifyingGlassIcon className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-navy-400" />
          <input
            aria-label="Search commands"
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search commands..."
            className="w-full pl-9 pr-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-sky-100 placeholder-navy-500 focus:border-gold-500 text-sm"
          />
        </div>

        <select
          aria-label="Filter by platforms"
          value={platformFilter}
          onChange={(e) => setPlatformFilter(e.target.value)}
          className="px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-sky-100 text-sm focus:border-gold-500"
        >
          <option value="all">All Platforms</option>
          <option value="discord">Discord</option>
          <option value="slack">Slack</option>
          <option value="twitch">Twitch</option>
          <option value="both">Multi-platform</option>
        </select>

        <select
          aria-label="Filter by modules"
          value={moduleFilter}
          onChange={(e) => setModuleFilter(e.target.value)}
          className="px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-sky-100 text-sm focus:border-gold-500"
        >
          <option value="all">All Modules</option>
          {modules.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      </div>

      {/* Table */}
      {filtered.length === 0 ? (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
          <CommandLineIcon className="w-12 h-12 text-navy-500 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-sky-100 mb-2">No Commands Found</h3>
          <p className="text-navy-400">
            {commands.length === 0
              ? 'No bot commands are registered for this community.'
              : 'No commands match your current filters.'}
          </p>
        </div>
      ) : (
        <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
          <table className="w-full">
            <thead className="bg-navy-900">
              <tr>
                <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Command</th>
                <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Platform</th>
                <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Module</th>
                <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Category</th>
                <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Permission</th>
                <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-navy-700">
              {filtered.map((cmd, idx) => (
                <tr
                  key={cmd.id || idx}
                  className={`hover:bg-navy-700/50 ${!cmd.isEnabled ? 'opacity-60' : ''}`}
                >
                  <td className="py-3 px-4">
                    <span className="font-mono text-sm text-gold-400">{cmd.command}</span>
                    {cmd.description && (
                      <p className="text-xs text-navy-400 mt-0.5 max-w-xs truncate">{cmd.description}</p>
                    )}
                  </td>
                  <td className="py-3 px-4">
                    <PlatformBadge command={cmd.command} platform={cmd.platform} />
                  </td>
                  <td className="py-3 px-4 text-sm text-navy-300">{cmd.module || '—'}</td>
                  <td className="py-3 px-4 text-sm text-navy-300">{cmd.category || '—'}</td>
                  <td className="py-3 px-4 text-sm text-navy-300">{cmd.permissionLevel || 'Everyone'}</td>
                  <td className="py-3 px-4">
                    {cmd.isEnabled ? (
                      <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-green-500/20 text-green-400 border border-green-500/30">
                        Enabled
                      </span>
                    ) : (
                      <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-red-500/20 text-red-400 border border-red-500/30">
                        Disabled
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="px-4 py-3 border-t border-navy-700 text-xs text-navy-500">
            Showing {filtered.length} of {commands.length} commands
          </div>
        </div>
      )}
    </div>
  );
}

export default AdminCommands;
