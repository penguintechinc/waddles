import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { TrophyIcon } from '@heroicons/react/24/outline';

// FICO-style tier definitions
const REPUTATION_TIERS = [
  { min: 800, max: 850, label: 'Exceptional', color: 'text-emerald-400', bg: 'bg-emerald-500/20' },
  { min: 740, max: 799, label: 'Very Good', color: 'text-sky-400', bg: 'bg-sky-500/20' },
  { min: 670, max: 739, label: 'Good', color: 'text-blue-400', bg: 'bg-blue-500/20' },
  { min: 580, max: 669, label: 'Fair', color: 'text-yellow-400', bg: 'bg-yellow-500/20' },
  { min: 300, max: 579, label: 'Poor', color: 'text-red-400', bg: 'bg-red-500/20' },
];

// Weight categories for display
const ACTIVITY_WEIGHTS = [
  { key: 'chat_message', label: 'Chat Message', icon: '💬', description: 'Points per chat message' },
  { key: 'command_usage', label: 'Command Usage', icon: '⌨️', description: 'Points per command used' },
  { key: 'giveaway_entry', label: 'Giveaway Entry', icon: '🎁', description: 'Points per giveaway entry' },
  { key: 'follow', label: 'Follow', icon: '👤', description: 'Points for following' },
  { key: 'subscription', label: 'Subscription (T1)', icon: '⭐', description: 'Points for Tier 1 sub' },
  { key: 'subscription_tier2', label: 'Subscription (T2)', icon: '⭐⭐', description: 'Points for Tier 2 sub' },
  { key: 'subscription_tier3', label: 'Subscription (T3)', icon: '⭐⭐⭐', description: 'Points for Tier 3 sub' },
  { key: 'gift_subscription', label: 'Gift Sub', icon: '🎁', description: 'Points per gift sub' },
  { key: 'donation_per_dollar', label: 'Donation (per $)', icon: '💵', description: 'Points per dollar donated' },
  { key: 'cheer_per_100bits', label: 'Cheer (per 100 bits)', icon: '💎', description: 'Points per 100 bits' },
  { key: 'raid', label: 'Raid', icon: '🚀', description: 'Points for incoming raid' },
  { key: 'boost', label: 'Server Boost', icon: '🚀', description: 'Points for server boost' },
];

const MODERATION_WEIGHTS = [
  { key: 'warn', label: 'Warning', icon: '⚠️', description: 'Points for warning' },
  { key: 'timeout', label: 'Timeout', icon: '⏱️', description: 'Points for timeout' },
  { key: 'kick', label: 'Kick', icon: '👢', description: 'Points for kick' },
  { key: 'ban', label: 'Ban', icon: '🔨', description: 'Points for ban' },
];

function ReputationSettings() {
  const { communityId } = useParams();
  const [activeTab, setActiveTab] = useState('settings');
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [atRiskUsers, setAtRiskUsers] = useState([]);
  const [showAtRisk, setShowAtRisk] = useState(false);

  // Leaderboard state
  const [leaderboard, setLeaderboard] = useState([]);
  const [leaderboardLoading, setLeaderboardLoading] = useState(false);

  useEffect(() => {
    fetchConfig();
  }, [communityId]);

  useEffect(() => {
    if (activeTab === 'leaderboard') {
      fetchLeaderboard();
    }
  }, [activeTab, communityId]);

  async function fetchConfig() {
    setLoading(true);
    try {
      const response = await adminApi.getReputationConfig(communityId);
      if (response.data.success) {
        const raw = response.data.config;
        setConfig({
          is_premium: raw.isPremium,
          can_customize: raw.canCustomize,
          // weights
          chat_message:         raw.weights?.chatMessage,
          command_usage:        raw.weights?.commandUsage,
          giveaway_entry:       raw.weights?.giveawayEntry,
          follow:               raw.weights?.follow,
          subscription:         raw.weights?.subscription,
          subscription_tier2:   raw.weights?.subscriptionTier2,
          subscription_tier3:   raw.weights?.subscriptionTier3,
          gift_subscription:    raw.weights?.giftSubscription,
          donation_per_dollar:  raw.weights?.donationPerDollar,
          cheer_per_100bits:    raw.weights?.cheerPer100Bits,
          raid:                 raw.weights?.raid,
          boost:                raw.weights?.boost,
          warn:                 raw.weights?.warn,
          timeout:              raw.weights?.timeout,
          kick:                 raw.weights?.kick,
          ban:                  raw.weights?.ban,
          // policy
          auto_ban_enabled:     raw.policy?.autoBanEnabled,
          auto_ban_threshold:   raw.policy?.autoBanThreshold,
          starting_score:       raw.policy?.startingScore,
          min_score:            raw.policy?.minScore,
          max_score:            raw.policy?.maxScore,
        });
      }
    } catch (err) {
      console.error('Failed to fetch config:', err);
      setMessage({ type: 'error', text: 'Failed to load reputation configuration' });
    } finally {
      setLoading(false);
    }
  }

  async function fetchAtRiskUsers() {
    try {
      const response = await adminApi.getAtRiskUsers(communityId);
      if (response.data.success) {
        setAtRiskUsers(response.data.users || []);
        setShowAtRisk(true);
      }
    } catch (err) {
      console.error('Failed to fetch at-risk users:', err);
      setMessage({ type: 'error', text: 'Failed to load at-risk users' });
    }
  }

  async function fetchLeaderboard() {
    setLeaderboardLoading(true);
    try {
      const response = await adminApi.getReputationLeaderboard(communityId, { limit: 50 });
      if (response.data.success) {
        setLeaderboard(response.data.users || []);
      }
    } catch (err) {
      console.error('Failed to fetch leaderboard:', err);
    } finally {
      setLeaderboardLoading(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setMessage(null);
    try {
      await adminApi.updateReputationConfig(communityId, config);
      setMessage({ type: 'success', text: 'Reputation configuration saved' });
    } catch (err) {
      console.error('Failed to save config:', err);
      const errorMsg = err.response?.data?.error?.message || 'Failed to save configuration';
      if (err.response?.status === 403) {
        setMessage({ type: 'error', text: 'Premium feature: Upgrade to customize weights' });
      } else {
        setMessage({ type: 'error', text: errorMsg });
      }
    } finally {
      setSaving(false);
    }
  }

  function updateWeight(key, value) {
    const numValue = parseFloat(value) || 0;
    setConfig({ ...config, [key]: numValue });
  }

  function updateConfig(key, value) {
    setConfig({ ...config, [key]: value });
  }

  function getTierForScore(score) {
    return REPUTATION_TIERS.find(t => score >= t.min && score <= t.max) || REPUTATION_TIERS[4];
  }

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
      </div>
    );
  }

  if (!config) {
    return (
      <div className="text-center py-12 text-red-400">
        Failed to load configuration
      </div>
    );
  }

  const isPremium = config.is_premium;

  const tabs = [
    { id: 'settings', label: 'Settings' },
    { id: 'leaderboard', label: 'Leaderboard' },
  ];

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-sky-100">Reputation</h1>
          <p className="text-navy-400 mt-1">
            FICO-style reputation scoring (300-850 range)
          </p>
        </div>
        {activeTab === 'settings' && (
          <div className="flex gap-3">
            <button
              onClick={fetchAtRiskUsers}
              className="btn btn-secondary"
            >
              View At-Risk Users
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="btn btn-primary disabled:opacity-50"
            >
              {saving ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        )}
      </div>

      {/* Tab Navigation */}
      <div className="border-b border-navy-700 mb-6">
        <nav className="flex space-x-8">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`py-3 px-1 border-b-2 font-medium text-sm transition-colors ${
                activeTab === tab.id
                  ? 'border-gold-400 text-gold-400'
                  : 'border-transparent text-navy-400 hover:text-sky-300 hover:border-navy-500'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {message && (
        <div className={`mb-6 p-4 rounded-lg border ${
          message.type === 'success'
            ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
            : 'bg-red-500/20 text-red-300 border-red-500/30'
        }`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="float-right">x</button>
        </div>
      )}

      {/* Settings Tab */}
      {activeTab === 'settings' && (
        <>
          {!isPremium && (
            <div className="mb-6 p-4 rounded-lg border bg-gold-500/10 border-gold-500/30">
              <div className="flex items-center gap-3">
                <span className="text-2xl">👑</span>
                <div>
                  <div className="font-medium text-gold-300">Premium Feature</div>
                  <div className="text-sm text-gold-400/80">
                    Weight customization is a premium feature. Upgrade to modify scoring weights.
                  </div>
                </div>
              </div>
            </div>
          )}

          <div className="space-y-6">
        {/* FICO Tier Reference */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Reputation Tiers</h2>
          <div className="grid grid-cols-5 gap-2">
            {REPUTATION_TIERS.map((tier) => (
              <div
                key={tier.label}
                className={`p-3 rounded-lg text-center ${tier.bg} border border-white/10`}
              >
                <div className={`font-bold ${tier.color}`}>{tier.label}</div>
                <div className="text-xs text-navy-400 mt-1">
                  {tier.min}-{tier.max}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Policy Settings */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Policy Settings</h2>
          <div className="space-y-4">
            {/* Starting Score */}
            <div>
              <label htmlFor="reputation-settings-starting-score-for-new-members" className="block text-sm font-medium text-navy-300 mb-2">
                Starting Score for New Members
              </label>
              <div className="flex items-center gap-4">
                <input
                  id="reputation-settings-starting-score-for-new-members"
                  type="range"
                  min="300"
                  max="850"
                  value={config.starting_score || 600}
                  onChange={(e) => updateConfig('starting_score', parseInt(e.target.value))}
                  disabled={!isPremium}
                  className="flex-1 h-2 bg-navy-700 rounded-lg appearance-none cursor-pointer disabled:opacity-50"
                />
                <div className="w-20 text-center">
                  <span className={`font-bold ${getTierForScore(config.starting_score || 600).color}`}>
                    {config.starting_score || 600}
                  </span>
                </div>
              </div>
              <p className="text-xs text-navy-500 mt-1">
                Default: 600 (Fair). New members start with this score.
              </p>
            </div>

            {/* Auto-Ban Toggle */}
            <label className="flex items-center justify-between p-4 bg-navy-800 rounded-lg cursor-pointer">
              <div className="flex items-center gap-3">
                <svg className="w-6 h-6 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
                </svg>
                <div>
                  <div className="font-medium text-sky-100">Auto-Ban Low Reputation</div>
                  <div className="text-sm text-navy-400">
                    Automatically ban users when reputation drops below threshold
                  </div>
                </div>
              </div>
              <input
                type="checkbox"
                checked={config.auto_ban_enabled || false}
                onChange={(e) => updateConfig('auto_ban_enabled', e.target.checked)}
                className="w-5 h-5 rounded border-navy-600 text-red-500 focus:ring-red-500"
              />
            </label>

            {/* Auto-Ban Threshold */}
            {config.auto_ban_enabled && (
              <div className="ml-9">
                <label htmlFor="reputation-settings-auto-ban-threshold" className="block text-sm font-medium text-navy-300 mb-2">
                  Auto-Ban Threshold
                </label>
                <div className="flex items-center gap-4">
                  <input
                    id="reputation-settings-auto-ban-threshold"
                    type="range"
                    min="300"
                    max="580"
                    value={config.auto_ban_threshold || 450}
                    onChange={(e) => updateConfig('auto_ban_threshold', parseInt(e.target.value))}
                    className="flex-1 h-2 bg-navy-700 rounded-lg appearance-none cursor-pointer"
                  />
                  <div className="w-20 text-center">
                    <span className="font-bold text-red-400">
                      {config.auto_ban_threshold || 450}
                    </span>
                  </div>
                </div>
                <p className="text-xs text-navy-500 mt-1">
                  Users dropping below this score will be automatically banned
                </p>
              </div>
            )}
          </div>
        </div>

        {/* Activity Weights */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-2">Activity Weights</h2>
          <p className="text-sm text-navy-400 mb-4">
            Points awarded for positive activities. Higher values = bigger reputation impact.
          </p>
          <div className="grid md:grid-cols-2 gap-4">
            {ACTIVITY_WEIGHTS.map((weight) => (
              <div key={weight.key} className="p-4 bg-navy-800 rounded-lg">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-lg">{weight.icon}</span>
                  <span className="font-medium text-sky-100">{weight.label}</span>
                </div>
                <input
                  aria-label={weight.label}
                  type="number"
                  step="0.01"
                  value={config[weight.key] ?? 0}
                  onChange={(e) => updateWeight(weight.key, e.target.value)}
                  disabled={!isPremium}
                  className="w-full px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                    text-sky-100 focus:border-sky-500 focus:ring-1 focus:ring-sky-500
                    disabled:opacity-50 disabled:cursor-not-allowed"
                />
                <p className="text-xs text-navy-500 mt-1">{weight.description}</p>
              </div>
            ))}
          </div>
        </div>

        {/* Moderation Weights */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-2">Moderation Penalties</h2>
          <p className="text-sm text-navy-400 mb-4">
            Points deducted for moderation actions. Use negative values.
          </p>
          <div className="grid md:grid-cols-2 gap-4">
            {MODERATION_WEIGHTS.map((weight) => (
              <div key={weight.key} className="p-4 bg-navy-800 rounded-lg border border-red-500/20">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-lg">{weight.icon}</span>
                  <span className="font-medium text-red-300">{weight.label}</span>
                </div>
                <input
                  aria-label={weight.label}
                  type="number"
                  step="0.5"
                  value={config[weight.key] ?? 0}
                  onChange={(e) => updateWeight(weight.key, e.target.value)}
                  disabled={!isPremium}
                  className="w-full px-3 py-2 bg-navy-700 border border-navy-600 rounded-lg
                    text-sky-100 focus:border-red-500 focus:ring-1 focus:ring-red-500
                    disabled:opacity-50 disabled:cursor-not-allowed"
                />
                <p className="text-xs text-navy-500 mt-1">{weight.description}</p>
              </div>
            ))}
          </div>
        </div>

        {/* Score Preview */}
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-sky-100 mb-4">Score Impact Preview</h2>
          <p className="text-sm text-navy-400 mb-4">
            See how many actions it takes to change reputation tiers
          </p>
          <div className="grid md:grid-cols-3 gap-4">
            <div className="p-4 bg-navy-800 rounded-lg text-center">
              <div className="text-3xl mb-2">💬</div>
              <div className="text-sm text-navy-400">Chat messages to gain 10 points</div>
              <div className="text-2xl font-bold text-sky-100">
                {config.chat_message > 0 ? Math.ceil(10 / config.chat_message) : '∞'}
              </div>
            </div>
            <div className="p-4 bg-navy-800 rounded-lg text-center">
              <div className="text-3xl mb-2">⭐</div>
              <div className="text-sm text-navy-400">Subs to reach Exceptional</div>
              <div className="text-2xl font-bold text-emerald-400">
                {config.subscription > 0
                  ? Math.ceil((800 - (config.starting_score || 600)) / config.subscription)
                  : '∞'}
              </div>
            </div>
            <div className="p-4 bg-navy-800 rounded-lg text-center">
              <div className="text-3xl mb-2">⚠️</div>
              <div className="text-sm text-navy-400">Warnings to reach auto-ban</div>
              <div className="text-2xl font-bold text-red-400">
                {config.warn < 0
                  ? Math.ceil(((config.starting_score || 600) - (config.auto_ban_threshold || 450)) / Math.abs(config.warn))
                  : '∞'}
              </div>
            </div>
          </div>
        </div>
          </div>
        </>
      )}

      {/* Leaderboard Tab */}
      {activeTab === 'leaderboard' && (
        <div className="space-y-6">
          {leaderboardLoading ? (
            <div className="flex justify-center py-12">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400"></div>
            </div>
          ) : leaderboard.length === 0 ? (
            <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
              <TrophyIcon className="w-12 h-12 text-navy-500 mx-auto mb-4" />
              <h3 className="text-lg font-semibold text-sky-100 mb-2">No Reputation Data</h3>
              <p className="text-navy-400">
                No members with reputation scores yet.
              </p>
            </div>
          ) : (
            <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
              <table className="w-full">
                <thead className="bg-navy-900">
                  <tr>
                    <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm w-16">Rank</th>
                    <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Member</th>
                    <th className="text-left py-3 px-4 text-navy-400 font-medium text-sm">Tier</th>
                    <th className="text-right py-3 px-4 text-navy-400 font-medium text-sm">Score</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-navy-700">
                  {leaderboard.map((user, index) => {
                    const tier = getTierForScore(user.reputation?.score || 0);
                    return (
                      <tr key={user.userId} className="hover:bg-navy-700/50">
                        <td className="py-3 px-4">
                          <span className={`font-bold ${index < 3 ? 'text-gold-400' : 'text-navy-400'}`}>
                            #{user.rank || index + 1}
                          </span>
                        </td>
                        <td className="py-3 px-4">
                          <div className="flex items-center space-x-3">
                            <div className="w-8 h-8 rounded-full bg-navy-700 flex items-center justify-center overflow-hidden">
                              {user.avatarUrl ? (
                                <img src={user.avatarUrl} alt="" className="w-full h-full object-cover" />
                              ) : (
                                <span className="text-navy-400 text-sm">
                                  {user.username?.[0]?.toUpperCase()}
                                </span>
                              )}
                            </div>
                            <span className="font-medium text-sky-100">{user.username}</span>
                          </div>
                        </td>
                        <td className="py-3 px-4">
                          <span className={`px-2 py-1 rounded-full text-xs font-medium ${tier.bg} ${tier.color} border border-white/10`}>
                            {user.reputation?.label || tier.label}
                          </span>
                        </td>
                        <td className="py-3 px-4 text-right">
                          <div className="flex items-center justify-end space-x-3">
                            <div className="w-24 h-2 bg-navy-700 rounded-full overflow-hidden">
                              <div
                                className={`h-full ${tier.bg.replace('/20', '')}`}
                                style={{ width: `${((user.reputation?.score || 0) - 300) / 5.5}%` }}
                              ></div>
                            </div>
                            <span className={`font-bold ${tier.color} w-12 text-right`}>
                              {user.reputation?.score || 0}
                            </span>
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

      {/* At-Risk Users Modal */}
      {showAtRisk && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-navy-900 rounded-xl p-6 max-w-2xl w-full mx-4 max-h-[80vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-xl font-bold text-sky-100">At-Risk Users</h3>
              <button aria-label="Close at-risk users"
                onClick={() => setShowAtRisk(false)}
                className="text-navy-400 hover:text-sky-100"
              >
                <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            {atRiskUsers.length === 0 ? (
              <div className="text-center py-8 text-navy-400">
                No users near the auto-ban threshold
              </div>
            ) : (
              <div className="space-y-2">
                {atRiskUsers.map((user) => {
                  const tier = getTierForScore(user.reputation);
                  return (
                    <div
                      key={user.id}
                      className="flex items-center justify-between p-3 bg-navy-800 rounded-lg"
                    >
                      <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-full bg-navy-700 flex items-center justify-center">
                          {user.avatar_url ? (
                            <img src={user.avatar_url} alt="" className="w-full h-full rounded-full" />
                          ) : (
                            <span className="text-navy-400">{user.username?.[0]?.toUpperCase()}</span>
                          )}
                        </div>
                        <div>
                          <div className="font-medium text-sky-100">{user.username}</div>
                          <div className="text-xs text-navy-400">{user.platform}</div>
                        </div>
                      </div>
                      <div className="text-right">
                        <div className={`font-bold ${tier.color}`}>{user.reputation}</div>
                        <div className="text-xs text-navy-400">{tier.label}</div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default ReputationSettings;
