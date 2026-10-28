import { useState, useEffect, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { FormModalBuilder } from '@penguintechinc/react-libs';
import { getPlatformIcon, getPlatformColor } from '../../utils/platformConfig';
import { WADDLES_COLORS } from '../../theme/waddlebotTheme';

const PLATFORM_ICONS = new Proxy({}, { get: (_, key) => getPlatformIcon(key) });
const PLATFORM_COLORS = new Proxy({}, { get: (_, key) => getPlatformColor(key) });

const LIMIT_OPTIONS = [10, 25, 50, 100];

function formatCurrency(amount, symbol) {
  const value = Number(amount) || 0;
  return `${symbol || '💰'} ${value.toLocaleString()}`;
}

/**
 * Admin leaderboard + balance management for one community's MVP
 * core-currency loyalty system (gh-317) — binds to
 * `services.community_loyalty.LeaderboardEntryDTO`
 * (`platform`/`platform_user_id`/`balance`) and `StatsDTO`. Entries are
 * keyed by platform identity, not a hub user id — there is no username,
 * avatar, or hub-user join available from this endpoint.
 */
function LoyaltyLeaderboard() {
  const { communityId } = useParams();
  const [entries, setEntries] = useState([]);
  const [stats, setStats] = useState(null);
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [limit, setLimit] = useState(25);
  const [platformFilter, setPlatformFilter] = useState('');
  const [search, setSearch] = useState('');
  const [message, setMessage] = useState(null);
  const [actionLoading, setActionLoading] = useState(false);

  const [showAdjustModal, setShowAdjustModal] = useState(false);
  const [selectedEntry, setSelectedEntry] = useState(null);
  const [showWipeModal, setShowWipeModal] = useState(false);

  useEffect(() => {
    fetchData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [communityId, limit]);

  async function fetchData() {
    setLoading(true);
    try {
      const [leaderboardRes, statsRes, configRes] = await Promise.all([
        adminApi.getLoyaltyLeaderboard(communityId, { limit }),
        adminApi.getLoyaltyStats(communityId),
        adminApi.getLoyaltyConfig(communityId),
      ]);
      setEntries(leaderboardRes.data.data?.entries || []);
      setStats(statsRes.data.data || null);
      setConfig(configRes.data.data || null);
    } catch (err) {
      console.error('[LoyaltyLeaderboard] Load failed', { communityId, status: err.response?.status });
      setMessage({ type: 'error', text: 'Failed to load loyalty data' });
    } finally {
      setLoading(false);
    }
  }

  const visibleEntries = useMemo(() => {
    return entries.filter((entry) => {
      if (platformFilter && entry.platform !== platformFilter) return false;
      if (search && !entry.platform_user_id.toLowerCase().includes(search.toLowerCase())) return false;
      return true;
    });
  }, [entries, platformFilter, search]);

  function openAdjustModal(entry) {
    setSelectedEntry(entry);
    setShowAdjustModal(true);
  }

  function closeAdjustModal() {
    setShowAdjustModal(false);
    setSelectedEntry(null);
  }

  async function handleAdjustBalance(data) {
    const amount = parseInt(data.amount, 10);
    if (!Number.isInteger(amount) || amount <= 0) {
      setMessage({ type: 'error', text: 'Please enter a valid whole-number amount' });
      throw new Error('Please enter a valid amount');
    }

    const delta = data.action === 'remove' ? -amount : amount;
    setActionLoading(true);
    try {
      await adminApi.adjustLoyaltyBalance(communityId, selectedEntry.platform_user_id, {
        platform: selectedEntry.platform,
        delta,
        note: data.note?.trim() || undefined,
      });
      setMessage({ type: 'success', text: `Balance ${data.action === 'remove' ? 'decreased' : 'increased'} successfully` });
      closeAdjustModal();
      fetchData();
    } catch (err) {
      const errorMsg = err.response?.data?.error?.message || `Failed to ${data.action} balance`;
      setMessage({ type: 'error', text: errorMsg });
      throw err;
    } finally {
      setActionLoading(false);
    }
  }

  async function handleWipeAll() {
    setActionLoading(true);
    try {
      const response = await adminApi.wipeLoyaltyCurrency(communityId);
      const affected = response.data.data?.affected ?? 0;
      setMessage({ type: 'success', text: `Wiped ${affected} balance${affected === 1 ? '' : 's'} to zero` });
      setShowWipeModal(false);
      fetchData();
    } catch (err) {
      const errorMsg = err.response?.data?.error?.message || 'Failed to wipe balances';
      setMessage({ type: 'error', text: errorMsg });
      throw err;
    } finally {
      setActionLoading(false);
    }
  }

  const adjustBalanceFields = useMemo(() => [
    {
      name: 'action',
      type: 'select',
      label: 'Action',
      required: true,
      defaultValue: 'add',
      options: [
        { value: 'add', label: 'Add - Increase balance' },
        { value: 'remove', label: 'Remove - Decrease balance' },
      ],
    },
    {
      name: 'amount',
      type: 'number',
      label: 'Amount',
      required: true,
      placeholder: 'Enter amount...',
      min: 1,
      step: 1,
    },
    {
      name: 'note',
      type: 'textarea',
      label: 'Note (optional)',
      required: false,
      placeholder: 'Why are you adjusting this balance?',
      rows: 3,
    },
  ], []);

  const wipeConfirmFields = useMemo(() => [
    {
      name: 'confirmation',
      type: 'text',
      label: 'Type "WIPE ALL" to confirm',
      required: true,
      placeholder: 'WIPE ALL',
      helpText: `This will reset ALL user loyalty currency balances to zero. ${stats?.total_users || 0} users will lose a total of ${formatCurrency(stats?.total_currency || 0, config?.currency_symbol)} currency.`,
    },
  ], [stats, config]);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-sky-100">Loyalty Currency Leaderboard</h1>
        <button
          onClick={() => setShowWipeModal(true)}
          data-testid="loyalty-wipe-open"
          className="btn bg-red-500/20 text-red-300 border border-red-500/30 hover:bg-red-500/30"
        >
          Wipe All Currency
        </button>
      </div>

      {message && (
        <div
          data-testid="loyalty-leaderboard-feedback"
          className={`mb-4 p-4 rounded-lg border ${
            message.type === 'success'
              ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
              : 'bg-red-500/20 text-red-300 border-red-500/30'
          }`}
        >
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">×</button>
        </div>
      )}

      {stats && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
          <div className="card p-4">
            <div className="text-sm text-navy-400 mb-1">Total Currency in Circulation</div>
            <div className="text-2xl font-bold text-gold-400">
              {formatCurrency(stats.total_currency, config?.currency_symbol)}
            </div>
          </div>
          <div className="card p-4">
            <div className="text-sm text-navy-400 mb-1">Users with Balances</div>
            <div className="text-2xl font-bold text-sky-400">
              {(stats.total_users || 0).toLocaleString()}
            </div>
          </div>
          <div className="card p-4">
            <div className="text-sm text-navy-400 mb-1">Average Balance</div>
            <div className="text-2xl font-bold text-purple-400">
              {formatCurrency(stats.average_balance, config?.currency_symbol)}
            </div>
          </div>
        </div>
      )}

      <div className="flex flex-col md:flex-row gap-4 mb-6">
        <input
          aria-label="Filter by platform user id"
          type="search"
          placeholder="Filter by platform user id..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          data-testid="loyalty-search"
          className="input flex-1"
        />
        <select
          aria-label="Filter by platforms"
          value={platformFilter}
          onChange={(e) => setPlatformFilter(e.target.value)}
          data-testid="loyalty-platform-filter"
          className="input w-full md:w-48"
        >
          <option value="">All Platforms</option>
          <option value="discord">Discord</option>
          <option value="twitch">Twitch</option>
          <option value="slack">Slack</option>
          <option value="youtube">YouTube</option>
        </select>
        <select
          aria-label="Rows to show"
          value={limit}
          onChange={(e) => setLimit(parseInt(e.target.value, 10))}
          data-testid="loyalty-limit"
          className="input w-full md:w-32"
        >
          {LIMIT_OPTIONS.map((opt) => (
            <option key={opt} value={opt}>Top {opt}</option>
          ))}
        </select>
      </div>

      <div className="card overflow-hidden">
        <table>
          <thead>
            <tr>
              <th className="w-16">Rank</th>
              <th>Platform User</th>
              <th>Platform</th>
              <th className="text-right">Balance</th>
              <th className="text-center">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan="5" className="p-12 text-center">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400 mx-auto"></div>
                </td>
              </tr>
            ) : visibleEntries.length === 0 ? (
              <tr>
                <td colSpan="5" className="p-12 text-center text-navy-400">
                  No users found
                </td>
              </tr>
            ) : (
              visibleEntries.map((entry, index) => (
                <tr
                  key={`${entry.platform}-${entry.platform_user_id}`}
                  data-testid={`loyalty-row-${entry.platform}-${entry.platform_user_id}`}
                  className="hover:bg-navy-700/50"
                >
                  <td className="text-center">
                    <div className={`font-bold ${
                      index === 0 ? 'text-gold-400 text-xl' :
                      index === 1 ? 'text-silver-400 text-lg' :
                      index === 2 ? 'text-bronze-400 text-lg' :
                      'text-navy-400'
                    }`}>
                      #{index + 1}
                    </div>
                  </td>
                  <td>
                    <div className="font-medium text-sky-100">{entry.platform_user_id}</div>
                  </td>
                  <td>
                    <div className="flex items-center space-x-2">
                      <span className="text-xl">{PLATFORM_ICONS[entry.platform] || '🌐'}</span>
                      <span className={`text-xs px-2 py-0.5 rounded border ${PLATFORM_COLORS[entry.platform] || 'bg-navy-700 text-navy-300 border-navy-600'}`}>
                        {entry.platform}
                      </span>
                    </div>
                  </td>
                  <td className="text-right">
                    <div className="text-lg font-bold text-gold-400">
                      {formatCurrency(entry.balance, config?.currency_symbol)}
                    </div>
                  </td>
                  <td className="text-center">
                    <button
                      onClick={() => openAdjustModal(entry)}
                      data-testid={`loyalty-adjust-${entry.platform}-${entry.platform_user_id}`}
                      className="btn btn-secondary text-sm"
                    >
                      Adjust
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <FormModalBuilder
        title={`Adjust Balance - ${selectedEntry?.platform_user_id || ''}`}
        description={selectedEntry ? `Current Balance: ${formatCurrency(selectedEntry.balance, config?.currency_symbol)} | Platform: ${selectedEntry.platform}` : ''}
        fields={adjustBalanceFields}
        isOpen={showAdjustModal && !!selectedEntry}
        onClose={closeAdjustModal}
        onSubmit={handleAdjustBalance}
        submitButtonText={actionLoading ? 'Processing...' : 'Apply Adjustment'}
        cancelButtonText="Cancel"
        width="md"
        themeMode="dark"
        colors={WADDLES_COLORS}
      />

      <FormModalBuilder
        title="Wipe All Currency"
        description="This will permanently reset ALL user loyalty currency balances to zero. This action cannot be undone!"
        fields={wipeConfirmFields}
        isOpen={showWipeModal}
        onClose={() => setShowWipeModal(false)}
        onSubmit={(data) => {
          if (data.confirmation !== 'WIPE ALL') {
            setMessage({ type: 'error', text: 'Please type "WIPE ALL" to confirm' });
            throw new Error('Confirmation text does not match');
          }
          return handleWipeAll();
        }}
        submitButtonText={actionLoading ? 'Wiping...' : 'Confirm Wipe'}
        cancelButtonText="Cancel"
        width="md"
        themeMode="dark"
        colors={WADDLES_COLORS}
      />
    </div>
  );
}

export default LoyaltyLeaderboard;
