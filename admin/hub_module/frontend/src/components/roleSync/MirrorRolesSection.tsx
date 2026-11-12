import { useMemo, useState } from 'react';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { TrashIcon } from '@heroicons/react/24/outline';
import {
  roleSyncApi,
  extractErrorMessage,
  type DiscordRole,
  type GuildPairing,
  type RoleMirrorMapping,
} from '../../services/roleSyncApi';

interface MirrorRolesSectionProps {
  communityId: number;
  pairings: GuildPairing[];
  canManage: boolean;
}

const mirrorKeys = {
  list: (communityId: number) => ['role-mirrors', communityId] as const,
  roles: (communityId: number, guildId: string) =>
    ['role-mirror-roles', communityId, guildId] as const,
};

const selectCls =
  'rounded border border-line-strong bg-surface-raised px-3 py-2 text-fg focus:outline-none focus:ring-2 focus:ring-sky-500';

/**
 * "Mirror roles between Discord servers": pick a source server+role and a target server+role
 * (both from this community's paired guilds), list/delete one-way mappings, and show the
 * last sync's counters. Thin: all validation (pairing, tenant, cycles, role existence) is
 * server-side; this only renders API data and relays the server's error message.
 */
export default function MirrorRolesSection({
  communityId,
  pairings,
  canManage,
}: MirrorRolesSectionProps) {
  const queryClient = useQueryClient();
  const guildIds = useMemo(() => pairings.map((p) => p.discord_guild_id), [pairings]);
  const [sourceGuild, setSourceGuild] = useState('');
  const [sourceRole, setSourceRole] = useState('');
  const [targetGuild, setTargetGuild] = useState('');
  const [targetRole, setTargetRole] = useState('');

  const {
    data: mappings,
    isLoading,
    error,
  } = useQuery({
    queryKey: mirrorKeys.list(communityId),
    queryFn: () => roleSyncApi.listMirrors(communityId),
  });

  const roleQueries = useQueries({
    queries: guildIds.map((guildId) => ({
      queryKey: mirrorKeys.roles(communityId, guildId),
      queryFn: () => roleSyncApi.listGuildRoles(communityId, guildId),
    })),
  });
  const rolesByGuild: Record<string, DiscordRole[]> = {};
  guildIds.forEach((guildId, i) => {
    rolesByGuild[guildId] = roleQueries[i]?.data ?? [];
  });
  const rolesError = roleQueries.find((q) => q.error)?.error;

  const roleName = (guildId: string, roleId: string) =>
    rolesByGuild[guildId]?.find((r) => r.id === roleId)?.name ?? roleId;

  const createMutation = useMutation({
    mutationFn: () =>
      roleSyncApi.createMirror(communityId, {
        source_guild_id: sourceGuild,
        source_role_id: sourceRole,
        target_guild_id: targetGuild,
        target_role_id: targetRole,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: mirrorKeys.list(communityId) });
      setSourceRole('');
      setTargetRole('');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (mapping: RoleMirrorMapping) => roleSyncApi.deleteMirror(communityId, mapping.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: mirrorKeys.list(communityId) });
    },
  });

  const canSubmit =
    canManage &&
    sourceGuild !== '' &&
    sourceRole !== '' &&
    targetGuild !== '' &&
    targetRole !== '' &&
    sourceGuild !== targetGuild &&
    !createMutation.isPending;

  const th = 'px-4 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-500';

  return (
    <section
      className="mt-8"
      aria-labelledby="mirror-roles-heading"
      data-testid="mirror-roles-section"
    >
      <h2 id="mirror-roles-heading" className="mb-2 text-lg font-semibold text-amber-400">
        Mirror roles between Discord servers
      </h2>
      <p className="mb-4 text-sm text-fg-muted">
        Members who hold the source role in one server get the target role in the other server
        (matched by Discord account; one-way). Roles the mirror did not grant are never removed.
        Both servers must be connected to this community.
      </p>

      {pairings.length < 2 && (
        <p className="text-fg-muted" data-testid="mirror-needs-two-guilds">
          Connect at least two Discord servers to this community to mirror roles between them.
        </p>
      )}

      {pairings.length >= 2 && canManage && (
        <form
          className="mb-4 grid gap-3 sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (canSubmit) createMutation.mutate();
          }}
        >
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium text-fg-secondary">Source</legend>
            <select
              aria-label="Source server"
              className={selectCls}
              value={sourceGuild}
              onChange={(e) => {
                setSourceGuild(e.target.value);
                setSourceRole('');
              }}
            >
              <option value="">Select server…</option>
              {guildIds.map((g) => (
                <option key={g} value={g}>
                  Server {g}
                </option>
              ))}
            </select>
            <select
              aria-label="Source role"
              className={selectCls}
              value={sourceRole}
              disabled={sourceGuild === ''}
              onChange={(e) => setSourceRole(e.target.value)}
            >
              <option value="">Select role…</option>
              {(rolesByGuild[sourceGuild] ?? []).map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </fieldset>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium text-fg-secondary">Target</legend>
            <select
              aria-label="Target server"
              className={selectCls}
              value={targetGuild}
              onChange={(e) => {
                setTargetGuild(e.target.value);
                setTargetRole('');
              }}
            >
              <option value="">Select server…</option>
              {guildIds.map((g) => (
                <option key={g} value={g}>
                  Server {g}
                </option>
              ))}
            </select>
            <select
              aria-label="Target role"
              className={selectCls}
              value={targetRole}
              disabled={targetGuild === ''}
              onChange={(e) => setTargetRole(e.target.value)}
            >
              <option value="">Select role…</option>
              {(rolesByGuild[targetGuild] ?? []).map((r) => (
                <option
                  key={r.id}
                  value={r.id}
                  disabled={r.privileged}
                  title={
                    r.privileged
                      ? 'Privileged or managed roles cannot be a mirror target'
                      : undefined
                  }
                >
                  {r.privileged ? `${r.name} (privileged)` : r.name}
                </option>
              ))}
            </select>
          </fieldset>
          <div className="sm:col-span-2">
            <button
              type="submit"
              disabled={!canSubmit}
              className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50"
            >
              Add mirror
            </button>
          </div>
        </form>
      )}

      {rolesError && (
        <p role="alert" className="mb-2 text-sm text-red-400">
          {extractErrorMessage(rolesError, 'Failed to load a server’s roles.')}
        </p>
      )}
      {createMutation.isError && (
        <p role="alert" className="mb-2 text-sm text-red-400">
          {extractErrorMessage(createMutation.error, 'Failed to create mirror mapping.')}
        </p>
      )}
      {deleteMutation.isError && (
        <p role="alert" className="mb-2 text-sm text-red-400">
          {extractErrorMessage(deleteMutation.error, 'Failed to delete mirror mapping.')}
        </p>
      )}

      {isLoading && (
        <p role="status" className="text-fg-muted">
          Loading mirrors…
        </p>
      )}
      {error && (
        <p role="alert" className="text-red-400">
          {extractErrorMessage(error, 'Failed to load mirror mappings.')}
        </p>
      )}
      {!isLoading && !error && (mappings?.length ?? 0) === 0 && (
        <p className="text-fg-muted">No mirror mappings yet.</p>
      )}

      {mappings && mappings.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-700">
          <table className="w-full text-sm" data-testid="mirror-table">
            <thead className="bg-slate-800">
              <tr>
                <th className={th}>Source</th>
                <th className={th}>Target</th>
                <th className={th}>Last sync</th>
                {canManage && (
                  <th className={th}>
                    <span className="sr-only">Actions</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-700 bg-slate-900/40">
              {mappings.map((m) => (
                <tr key={m.id} data-testid={`mirror-row-${m.id}`}>
                  <td className="px-4 py-3 text-slate-100">
                    {roleName(m.source_guild_id, m.source_role_id)}
                    <span className="block font-mono text-xs text-slate-500">
                      {m.source_guild_id}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-100">
                    → {roleName(m.target_guild_id, m.target_role_id)}
                    <span className="block font-mono text-xs text-slate-500">
                      {m.target_guild_id}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-300" data-testid={`mirror-result-${m.id}`}>
                    {m.last_synced_at === null ? (
                      'Not synced yet'
                    ) : (
                      <>
                        +{m.last_roles_added} added, −{m.last_roles_removed} removed,{' '}
                        {m.last_skipped_not_member} skipped (not in server)
                        {m.last_error && (
                          <span className="block text-red-400">Error: {m.last_error}</span>
                        )}
                        <span className="block text-xs text-slate-500">
                          {new Date(m.last_synced_at).toLocaleString()}
                        </span>
                      </>
                    )}
                  </td>
                  {canManage && (
                    <td className="px-4 py-3 text-right">
                      <button
                        type="button"
                        aria-label={`Delete mirror ${m.id}`}
                        onClick={() => deleteMutation.mutate(m)}
                        className="rounded border border-red-500/60 p-1.5 text-red-400 hover:bg-red-500/10 focus:outline-none focus:ring-2 focus:ring-red-500"
                      >
                        <TrashIcon className="h-4 w-4" aria-hidden="true" />
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
