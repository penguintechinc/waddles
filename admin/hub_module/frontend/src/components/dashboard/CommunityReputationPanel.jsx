import { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { communityApi } from '../../services/api';
import { PanelErrorState } from '../ui';

/**
 * Presentation-only mirror of hub_api's shared `REPUTATION_TIERS`
 * (`hub_api/services/community_reputation_service.py`, gh-310) -- used
 * ONLY to draw the "distance to next tier" progress bar. The tier LABEL
 * shown in each chip always comes straight from the API response
 * (`community_tier`/`global_tier`/leaderboard entry `tier`), never
 * recomputed here -- this table exists so the bar can render without a
 * second round trip, not to second-guess the server.
 */
const REPUTATION_MIN = 300;
const REPUTATION_MAX = 850;
const TIER_BANDS = [
  { lower: REPUTATION_MIN, upper: 465, label: 'Newcomer' },
  { lower: 465, upper: 575, label: 'Regular' },
  { lower: 575, upper: 658, label: 'Trusted' },
  { lower: 658, upper: 740, label: 'Respected' },
  { lower: 740, upper: 795, label: 'Champion' },
  { lower: 795, upper: REPUTATION_MAX, label: 'Legend' },
];

/** `{ percent, nextLabel, nextThreshold }` -- how far through the current tier band `score` sits. */
function tierProgress(score) {
  const clamped = Math.min(REPUTATION_MAX, Math.max(REPUTATION_MIN, score ?? REPUTATION_MIN));
  const index = TIER_BANDS.findIndex((band) => clamped < band.upper);
  const band = index === -1 ? TIER_BANDS[TIER_BANDS.length - 1] : TIER_BANDS[index];
  const isTopTier = band.label === 'Legend';
  const percent = isTopTier
    ? 100
    : Math.round(((clamped - band.lower) / (band.upper - band.lower)) * 100);
  const next = isTopTier ? null : TIER_BANDS[TIER_BANDS.indexOf(band) + 1];
  return { percent, nextLabel: next?.label ?? null, nextThreshold: isTopTier ? null : band.upper };
}

const TIER_CHIP_COLOR = {
  Newcomer: 'bg-navy-600 text-navy-300',
  Regular: 'bg-sky-600 text-sky-100',
  Trusted: 'bg-sky-500 text-white',
  Respected: 'bg-purple-500 text-white',
  Champion: 'bg-amber-500 text-navy-900',
  Legend: 'bg-gold-500 text-navy-900',
};

function TierChip({ tier, testId }) {
  return (
    <span
      data-testid={testId}
      className={`text-xs px-2 py-0.5 rounded-full font-semibold ${
        TIER_CHIP_COLOR[tier] || 'bg-navy-600 text-navy-300'
      }`}
    >
      {tier}
    </span>
  );
}

TierChip.propTypes = {
  tier: PropTypes.string.isRequired,
  testId: PropTypes.string,
};

function ScoreBlock({ label, score, tier, testId }) {
  const { percent, nextLabel, nextThreshold } = tierProgress(score);
  return (
    <div data-testid={testId}>
      <div className="flex items-center justify-between mb-1">
        <span className="text-navy-400 text-xs uppercase tracking-wide">{label}</span>
        <TierChip tier={tier} testId={`${testId}-tier-chip`} />
      </div>
      <div className="flex items-baseline gap-2">
        <span className="text-xl font-bold text-sky-100">{score}</span>
      </div>
      <div className="mt-2 h-1.5 rounded-full bg-navy-800 overflow-hidden" aria-hidden="true">
        <div
          className="h-full rounded-full bg-sky-400 transition-all"
          style={{ width: `${percent}%` }}
        />
      </div>
      <div className="mt-1 text-xs text-navy-500">
        {nextLabel ? `${percent}% to ${nextLabel} (${nextThreshold})` : 'Top tier'}
      </div>
    </div>
  );
}

ScoreBlock.propTypes = {
  label: PropTypes.string.isRequired,
  score: PropTypes.number.isRequired,
  tier: PropTypes.string.isRequired,
  testId: PropTypes.string.isRequired,
};

/**
 * Dashboard sidebar widget (gh-310): the caller's community + global
 * reputation score/tier with a progress bar to the next tier, plus this
 * community's top-10 reputation leaderboard (display name, score, tier
 * chip -- no ids/emails, matches `communityApi.getReputationLeaderboard`'s
 * PII-free response). Sibling of `LeaderboardCard` (same card chrome,
 * same self-fetching-on-mount shape) -- mounted next to it in
 * `CommunityDashboard.jsx`'s sidebar.
 */
function CommunityReputationPanel({ communityId }) {
  const [me, setMe] = useState(null);
  const [leaderboard, setLeaderboard] = useState([]);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);
      setForbidden(false);
      try {
        const [meResponse, leaderboardResponse] = await Promise.all([
          communityApi.getMyReputation(communityId),
          communityApi.getReputationLeaderboard(communityId, { limit: 10 }),
        ]);
        if (cancelled) return;
        setMe(meResponse.data?.data ?? null);
        setLeaderboard(leaderboardResponse.data?.data?.entries ?? []);
      } catch (err) {
        if (cancelled) return;
        if (err.response?.status === 403) {
          setForbidden(true);
        } else {
          setError(err);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [communityId, reloadKey]);

  return (
    <div
      data-testid="community-reputation-panel"
      className="card p-4 bg-navy-900 rounded-xl border border-navy-700"
    >
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sky-100 font-semibold flex items-center gap-2">
          <svg
            className="w-5 h-5 text-gold-400"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
            />
          </svg>
          Reputation
        </h3>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-8" data-testid="reputation-loading">
          <div className="animate-spin rounded-full h-6 w-6 border-t-2 border-b-2 border-sky-400" />
        </div>
      ) : forbidden ? (
        <div className="text-center py-8 text-navy-400 text-sm" data-testid="reputation-forbidden">
          You don&apos;t have access to reputation here.
        </div>
      ) : error ? (
        <div data-testid="reputation-error">
          <PanelErrorState feature="Reputation" error={error} onRetry={() => setReloadKey((k) => k + 1)} className="py-6" />
        </div>
      ) : (
        <>
          {me && (
            <div className="grid grid-cols-2 gap-4 mb-4">
              <ScoreBlock
                label="This Community"
                score={me.community_score}
                tier={me.community_tier}
                testId="reputation-community-score"
              />
              <ScoreBlock
                label="Global"
                score={me.global_score}
                tier={me.global_tier}
                testId="reputation-global-score"
              />
            </div>
          )}

          <div className="border-t border-navy-700 pt-3">
            <div className="text-navy-400 text-xs uppercase tracking-wide mb-2">
              Top Reputation
            </div>
            {leaderboard.length === 0 ? (
              <div
                className="text-center py-6 text-navy-400 text-sm"
                data-testid="reputation-leaderboard-empty"
              >
                No reputation data yet for this community
              </div>
            ) : (
              <div className="space-y-1.5" data-testid="reputation-leaderboard-list">
                {leaderboard.map((entry, index) => (
                  <div
                    key={`${entry.display_name}-${index}`}
                    data-testid="reputation-leaderboard-entry"
                    className="flex items-center gap-3 p-2 rounded-lg bg-navy-800 bg-opacity-50"
                  >
                    <div className="w-6 h-6 flex items-center justify-center rounded-full text-xs font-bold bg-navy-700 text-navy-400">
                      {index + 1}
                    </div>
                    <div className="flex-1 min-w-0 text-sky-100 text-sm font-medium truncate">
                      {entry.display_name}
                    </div>
                    <div className="text-sm font-semibold text-gold-400">{entry.score}</div>
                    <TierChip tier={entry.tier} testId="reputation-leaderboard-entry-tier-chip" />
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

CommunityReputationPanel.propTypes = {
  communityId: PropTypes.oneOfType([PropTypes.string, PropTypes.number]).isRequired,
};

export default CommunityReputationPanel;
