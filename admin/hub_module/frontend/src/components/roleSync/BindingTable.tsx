import { TrashIcon } from '@heroicons/react/24/outline';
import type { RoleSyncBinding } from '../../services/roleSyncApi';

const SCOPE_BADGES: Record<RoleSyncBinding['sync_scope'], { text: string; cls: string }> = {
  community_role: { text: 'Discord → Waddles', cls: 'bg-sky-500/20 text-sky-300' },
  subscriber_tier: { text: 'Twitch sub → Discord', cls: 'bg-purple-500/20 text-purple-300' },
  moderator: { text: 'Twitch mod → Discord', cls: 'bg-purple-500/20 text-purple-300' },
};

/** Human label for the Waddles/Twitch side of a binding. */
export function waddlesSide(binding: RoleSyncBinding): string {
  if (binding.sync_scope === 'subscriber_tier') return `Subscriber tier ${binding.subscriber_tier ?? '?'}`;
  if (binding.sync_scope === 'moderator') return 'Twitch moderator';
  return binding.community_role ?? '?';
}

interface BindingTableProps {
  bindings: RoleSyncBinding[];
  canManage: boolean;
  onDelete: (binding: RoleSyncBinding) => void;
}

/**
 * Read-first table of role bindings: Discord role id, arrow showing the single
 * lifelong write direction, the Waddles-side role, scope and created time.
 */
export default function BindingTable({ bindings, canManage, onDelete }: BindingTableProps) {
  const th = 'px-4 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-500';
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-700">
      <table className="w-full text-sm" data-testid="binding-table">
        <thead className="bg-slate-800">
          <tr>
            <th className={th}>Discord role</th>
            <th className={th}>Direction</th>
            <th className={th}>Waddles role</th>
            <th className={th}>Scope</th>
            {canManage && <th className={th}><span className="sr-only">Actions</span></th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-700 bg-slate-900/40">
          {bindings.map((b) => {
            const badge = SCOPE_BADGES[b.sync_scope];
            const toDiscord = b.sync_scope !== 'community_role';
            return (
              <tr key={b.id} data-testid={`binding-row-${b.id}`}>
                <td className="px-4 py-3 font-mono text-slate-100">{b.discord_role_id}</td>
                <td className="px-4 py-3 text-lg text-amber-400" aria-label={toDiscord ? 'to Discord' : 'from Discord'}>
                  {toDiscord ? '←' : '→'}
                </td>
                <td className="px-4 py-3 text-slate-100">{waddlesSide(b)}</td>
                <td className="px-4 py-3">
                  <span className={`rounded px-2 py-0.5 text-xs font-semibold ${badge.cls}`}>{badge.text}</span>
                </td>
                {canManage && (
                  <td className="px-4 py-3 text-right">
                    <button
                      type="button"
                      onClick={() => onDelete(b)}
                      className="rounded border border-red-500/60 p-1.5 text-red-400 hover:bg-red-500/10 focus:outline-none focus:ring-2 focus:ring-red-500"
                      aria-label={`Delete binding for Discord role ${b.discord_role_id}`}
                    >
                      <TrashIcon className="h-4 w-4" />
                    </button>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
