import type { GuildPairing, GuildPairingDirection } from '../../services/roleSyncApi';

const DIRECTION_LABELS: Record<GuildPairingDirection, string> = {
  discord_to_twitch: 'Discord → Waddles',
  twitch_to_discord: 'Waddles → Discord',
  bidirectional: 'Discord ↔ Waddles (bidirectional)',
};

/** Formats an ISO timestamp for display; returns an em dash for null/invalid input. */
export function formatSyncTime(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

interface PairingStatusCardProps {
  pairing: GuildPairing;
  bindingCount: number | null;
  canManage: boolean;
  toggling: boolean;
  onToggleSync: (enabled: boolean) => void;
}

/**
 * Status summary for one Discord guild pairing: direction, sync on/off, event
 * sync state, binding count, last-updated time and the guarded-demote policy.
 * Presentational only -- the parent page owns the queries and the toggle mutation.
 */
export default function PairingStatusCard({
  pairing,
  bindingCount,
  canManage,
  toggling,
  onToggleSync,
}: PairingStatusCardProps) {
  const stat = 'rounded border border-slate-700 bg-slate-900/60 p-3';
  const label = 'text-xs uppercase tracking-wide text-slate-500';
  return (
    <section
      aria-label="Role sync status"
      data-testid="pairing-status-card"
      className="mb-6 rounded-lg border border-slate-700 bg-slate-800 p-4"
    >
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">Guild {pairing.discord_guild_id}</h2>
          <p className="text-sm text-slate-400">{DIRECTION_LABELS[pairing.direction]}</p>
        </div>
        <label className="flex items-center gap-2 text-sm text-slate-200">
          <input
            type="checkbox"
            aria-label="Role sync enabled"
            checked={pairing.sync_enabled}
            disabled={!canManage || toggling}
            onChange={(e) => onToggleSync(e.target.checked)}
            className="h-4 w-4 focus:ring-2 focus:ring-sky-500"
          />
          <span
            data-testid="sync-state-badge"
            className={`rounded px-2 py-0.5 text-xs font-semibold ${
              pairing.sync_enabled ? 'bg-emerald-500/20 text-emerald-300' : 'bg-amber-500/20 text-amber-300'
            }`}
          >
            {pairing.sync_enabled ? 'Sync enabled' : 'Sync disabled'}
          </span>
        </label>
      </div>
      <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className={stat}>
          <dt className={label}>Mappings</dt>
          <dd className="text-slate-100">{bindingCount ?? '…'}</dd>
        </div>
        <div className={stat}>
          <dt className={label}>Event sync</dt>
          <dd className="text-slate-100">{pairing.event_sync_enabled ? 'On' : 'Off'}</dd>
        </div>
        <div className={stat}>
          <dt className={label}>Role name prefix</dt>
          <dd className="text-slate-100">{pairing.role_name_prefix || '—'}</dd>
        </div>
        <div className={stat}>
          <dt className={label}>Last updated</dt>
          <dd className="text-slate-100" data-testid="last-updated">{formatSyncTime(pairing.updated_at)}</dd>
        </div>
      </dl>
      <p className="mt-3 text-xs text-slate-400" data-testid="demote-policy">
        Guarded demotion: syncing is grant-only by default. Removing a mapped Discord role only
        reverts a member to the baseline role when demotion is opted in server-side for this
        pairing, and only for roles sync itself granted.
      </p>
    </section>
  );
}
