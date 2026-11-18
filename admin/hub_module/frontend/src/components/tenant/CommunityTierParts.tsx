import { TIER_BADGE_CLASS, TIER_LABEL, tierLabel } from '../../services/communityTierConstants';
import { limitText } from '../../services/communityTierErrors';
import type { PoolTier } from '../../services/tenantCommunityTiersApi';

/** One tier's pool meter: used / limit, available, and an over-limit warning in text and color. */
export function PoolMeter({ row }: { row: PoolTier }) {
  const { tier, limit, used, available, overage } = row;
  const label = TIER_LABEL[tier] ?? `Tier ${tier}`;
  const notLicensed = limit === null;
  const unlimited = limit === -1;
  const over = overage > 0;
  const pct = !notLicensed && !unlimited && limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const barColor = over ? 'bg-red-500' : pct >= 100 ? 'bg-amber-500' : 'bg-sky-500';
  return (
    <div className="card p-4" data-testid={`pool-tier-${tier}`}>
      <div className="flex items-center justify-between mb-2">
        <h3 className="font-semibold text-sky-100">{label}</h3>
        <span className={`badge border ${TIER_BADGE_CLASS[tier] ?? ''}`}>{label}</span>
      </div>
      <p className="text-sm text-sky-200">
        {notLicensed ? <>Not licensed ({used} in use)</> : <>{used} of {limitText(limit)} used</>}
      </p>
      {!notLicensed && !unlimited && (
        <div
          className="mt-2 h-2 w-full rounded bg-navy-800"
          role="progressbar"
          aria-label={`${label} pool usage`}
          aria-valuemin={0}
          aria-valuemax={limit}
          aria-valuenow={Math.min(used, limit)}
        >
          <div className={`h-2 rounded ${barColor}`} style={{ width: `${pct}%` }} />
        </div>
      )}
      <p className="text-xs text-navy-300 mt-2">
        {unlimited ? 'Available: unlimited' : notLicensed ? 'Available: 0' : `Available: ${available ?? Math.max(0, limit - used)}`}
      </p>
      {over && (
        <p role="status" className="text-xs mt-2 text-red-300 font-medium">
          Over limit by {overage}: existing allocations stay, but no new {label} allocations are possible until you release
          slots or upgrade.
        </p>
      )}
    </div>
  );
}

export interface PendingChange {
  community: { name: string; publicId: string };
  from: number;
  to: number;
}

interface ConfirmProps {
  pending: PendingChange | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

/** Confirmation shown before a release or downgrade, since it removes access to higher-tier features. */
export function ConfirmDialog({ pending, busy, onCancel, onConfirm }: ConfirmProps) {
  if (!pending) return null;
  const { community, from, to } = pending;
  const releasing = to === 1;
  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="community-tiers-confirm-title"
        className="bg-navy-900 rounded-xl shadow-xl max-w-md w-full mx-4 border border-navy-700"
      >
        <div className="p-6 border-b border-navy-700">
          <h2 id="community-tiers-confirm-title" className="text-xl font-semibold text-gold-400">
            {releasing ? 'Release tier slot?' : 'Downgrade community?'}
          </h2>
        </div>
        <div className="p-6 text-sky-100 text-sm space-y-2">
          <p>
            Moving <strong>{community.name}</strong> from {tierLabel(from)} to {tierLabel(to)} removes access to{' '}
            {tierLabel(from)}-tier features for this community (for example workflows, bot detection, analytics and
            bring-your-own-key AI, depending on the tier).
          </p>
          {releasing && <p>The {tierLabel(from)} slot returns to your pool.</p>}
        </div>
        <div className="p-6 border-t border-navy-700 flex justify-end gap-3">
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="btn btn-danger" onClick={onConfirm} disabled={busy}>
            {releasing ? 'Release slot' : 'Downgrade'}
          </button>
        </div>
      </div>
    </div>
  );
}
