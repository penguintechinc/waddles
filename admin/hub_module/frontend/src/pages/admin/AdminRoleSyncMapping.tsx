import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FormModalBuilder, type FormField } from '@penguintechinc/react-libs';
import { TrashIcon } from '@heroicons/react/24/outline';
import { useAuth } from '../../contexts/AuthContext';
import { useFeatureFlag } from '../../lib/useFeatureFlag';
import {
  roleSyncApi,
  extractErrorMessage,
  COMMUNITY_ROLES,
  SUBSCRIBER_TIERS,
  type CommunityRole,
  type CreateRoleSyncBindingPayload,
  type RoleSyncBinding,
  type RoleSyncScope,
  type SubscriberTier,
} from '../../services/roleSyncApi';

/**
 * Discord guild role <-> community role/scope mapping admin UI -- the
 * deferred follow-up from role-sync PR #639. Lets a community admin pick a
 * paired Discord guild, then create/list/delete `community_role_sync_
 * bindings` rows against `hub_api/blueprints/v1/guild_pairing.py`'s existing
 * role-bindings CRUD (no new route added for this slice).
 *
 * Gated behind `waddles.webui.role_sync_mapping` (defaults OFF until added
 * to hub-api's `CLIENT_FLAG_KEYS` -- see PR description).
 *
 * KNOWN GAP: there is no backend endpoint that lists a Discord guild's
 * live role names/IDs (`hub_api/services/guild_pairing.py` only persists a
 * `discord_role_id` the caller supplies). Admins enter the numeric Discord
 * role ID by hand until that lookup endpoint exists -- see the help text
 * below and the PR description's "Missing backend endpoint" note.
 */

// `AuthContext.jsx` is plain JS (checkJs: false) -- see `AdminCommunityBundles.tsx`.
interface CommunityAuth {
  isCommunityAdmin: (communityId: string | number | undefined) => boolean;
}

const roleSyncKeys = {
  pairings: (communityId: number) => ['role-sync-pairings', communityId] as const,
  bindings: (communityId: number, pairingId: number) =>
    ['role-sync-bindings', communityId, pairingId] as const,
};

const SYNC_SCOPE_LABELS: Record<RoleSyncScope, string> = {
  subscriber_tier: 'Twitch subscriber tier → Discord role',
  moderator: 'Twitch moderator → Discord role',
  community_role: 'Discord role → community role',
};

function scopeSummary(binding: RoleSyncBinding): string {
  if (binding.sync_scope === 'subscriber_tier') {
    return `Tier ${binding.subscriber_tier ?? '?'} subscribers → Discord role ${binding.discord_role_id}`;
  }
  if (binding.sync_scope === 'moderator') {
    return `Twitch moderators → Discord role ${binding.discord_role_id}`;
  }
  return `Discord role ${binding.discord_role_id} → ${binding.community_role ?? '?'}`;
}

function BindingRow({
  binding,
  canManage,
  onDelete,
}: {
  binding: RoleSyncBinding;
  canManage: boolean;
  onDelete: (binding: RoleSyncBinding) => void;
}) {
  return (
    <li
      className="flex items-center justify-between rounded border border-slate-700 bg-slate-800 px-4 py-3"
      data-testid={`binding-row-${binding.id}`}
    >
      <div>
        <p className="font-medium text-slate-100">{scopeSummary(binding)}</p>
        <p className="text-xs text-slate-500">{SYNC_SCOPE_LABELS[binding.sync_scope]}</p>
      </div>
      {canManage && (
        <button
          type="button"
          onClick={() => onDelete(binding)}
          className="rounded border border-red-500/60 p-1.5 text-red-400 hover:bg-red-500/10 focus:outline-none focus:ring-2 focus:ring-red-500"
          aria-label={`Delete binding for Discord role ${binding.discord_role_id}`}
        >
          <TrashIcon className="h-4 w-4" />
        </button>
      )}
    </li>
  );
}

export default function AdminRoleSyncMapping() {
  const pageEnabled = useFeatureFlag('waddles.webui.role_sync_mapping');
  const { communityId: communityIdParam } = useParams<{ communityId: string }>();
  const { isCommunityAdmin } = useAuth() as CommunityAuth;
  const queryClient = useQueryClient();

  const communityId = Number(communityIdParam);
  const hasValidCommunity = Number.isFinite(communityId);
  const canManage = Boolean(isCommunityAdmin(communityIdParam));

  const [selectedPairingId, setSelectedPairingId] = useState<number | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<RoleSyncBinding | null>(null);

  const {
    data: pairings,
    isLoading: pairingsLoading,
    error: pairingsError,
  } = useQuery({
    queryKey: roleSyncKeys.pairings(communityId),
    queryFn: () => roleSyncApi.listPairings(communityId),
    enabled: pageEnabled && hasValidCommunity,
  });

  const activePairingId = selectedPairingId ?? pairings?.[0]?.id ?? null;
  const activePairing = useMemo(
    () => pairings?.find((p) => p.id === activePairingId) ?? null,
    [pairings, activePairingId],
  );

  const {
    data: bindings,
    isLoading: bindingsLoading,
    error: bindingsError,
  } = useQuery({
    queryKey: roleSyncKeys.bindings(communityId, activePairingId ?? -1),
    queryFn: () => roleSyncApi.listBindings(communityId, activePairingId as number),
    enabled: pageEnabled && hasValidCommunity && activePairingId !== null,
  });

  const createMutation = useMutation({
    mutationFn: (payload: CreateRoleSyncBindingPayload) =>
      roleSyncApi.createBinding(communityId, activePairingId as number, payload),
    onSuccess: (binding) => {
      console.debug('[RoleSyncMapping] CreateBinding', {
        communityId,
        pairingId: activePairingId,
        syncScope: binding.sync_scope,
      });
      void queryClient.invalidateQueries({
        queryKey: roleSyncKeys.bindings(communityId, activePairingId as number),
      });
      setCreateOpen(false);
    },
    onError: (err) => {
      console.error('[RoleSyncMapping] CreateBindingFailed', {
        communityId,
        pairingId: activePairingId,
        error: extractErrorMessage(err, 'unknown error'),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (binding: RoleSyncBinding) =>
      roleSyncApi.deleteBinding(communityId, activePairingId as number, binding.id),
    onSuccess: (_result, binding) => {
      console.debug('[RoleSyncMapping] DeleteBinding', {
        communityId,
        pairingId: activePairingId,
        bindingId: binding.id,
      });
      void queryClient.invalidateQueries({
        queryKey: roleSyncKeys.bindings(communityId, activePairingId as number),
      });
      setDeleteTarget(null);
    },
    onError: (err, binding) => {
      console.error('[RoleSyncMapping] DeleteBindingFailed', {
        communityId,
        pairingId: activePairingId,
        bindingId: binding.id,
        error: extractErrorMessage(err, 'unknown error'),
      });
    },
  });

  if (!pageEnabled) {
    return (
      <div className="flex items-center justify-center min-h-[50vh] text-slate-400" role="status">
        Discord role sync mapping is not yet available for this community.
      </div>
    );
  }

  if (!hasValidCommunity) {
    return (
      <div className="flex items-center justify-center min-h-[50vh] text-red-400" role="alert">
        Invalid community.
      </div>
    );
  }

  const createFields: FormField[] = [
    {
      name: 'sync_scope',
      type: 'select',
      label: 'Sync direction',
      required: true,
      defaultValue: 'subscriber_tier',
      options: [
        { value: 'subscriber_tier', label: SYNC_SCOPE_LABELS.subscriber_tier },
        { value: 'moderator', label: SYNC_SCOPE_LABELS.moderator },
        { value: 'community_role', label: SYNC_SCOPE_LABELS.community_role },
      ],
      helpText:
        'subscriber_tier/moderator push this platform’s Twitch state TO the Discord role below. community_role pulls FROM that Discord role, granting the mapped community role on this platform.',
    },
    {
      name: 'discord_role_id',
      type: 'text',
      label: 'Discord role ID',
      required: true,
      pattern: '^[0-9]+$',
      placeholder: 'e.g. 901234567890123456',
      helpText:
        'Numeric Discord role snowflake (enable Developer Mode in Discord, right-click the role → Copy Role ID). No backend endpoint yet lists a guild’s roles directly, so this is entered by hand.',
    },
    {
      name: 'subscriber_tier',
      type: 'select',
      label: 'Subscriber tier',
      required: true,
      defaultValue: 1,
      options: SUBSCRIBER_TIERS.map((tier) => ({ value: tier, label: `Tier ${tier}` })),
      triggerField: 'sync_scope',
      showWhen: (values) => values.sync_scope === 'subscriber_tier',
    },
    {
      name: 'community_role',
      type: 'select',
      label: 'Community role',
      required: true,
      defaultValue: 'community-admin',
      options: COMMUNITY_ROLES.map((role) => ({ value: role, label: role })),
      helpText:
        'If a member holds multiple mapped Discord roles, the highest-priority community role wins. Losing a mapped Discord role never auto-demotes — demotion is an explicit admin action.',
      triggerField: 'sync_scope',
      showWhen: (values) => values.sync_scope === 'community_role',
    },
  ];

  return (
    <div className="p-6 text-slate-300">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-amber-400">Discord Role Sync</h1>
        {canManage && activePairingId !== null && (
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500"
            aria-label="Create a new role sync binding"
          >
            Add Mapping
          </button>
        )}
      </div>

      <p className="mb-4 text-sm text-slate-400">
        Map Discord guild roles to this community&apos;s Twitch subscriber tiers/moderators
        (platform → Discord), or to a platform community role (Discord → platform).
        Conflict rule: when a member holds multiple mapped Discord roles, the
        highest-<code>priority</code> community role wins, and syncing is grant-only
        (never auto-demotes).
      </p>

      {pairingsLoading && (
        <p role="status" className="text-slate-400">
          Loading guild pairings…
        </p>
      )}

      {pairingsError && (
        <p role="alert" className="text-red-400">
          {extractErrorMessage(pairingsError, 'Failed to load guild pairings for this community.')}
        </p>
      )}

      {!pairingsLoading && !pairingsError && (pairings?.length ?? 0) === 0 && (
        <p className="text-slate-400">
          No Discord guild is paired with this community yet. Pair a Discord server under{' '}
          <strong>Connections</strong> before mapping roles.
        </p>
      )}

      {!pairingsLoading && (pairings?.length ?? 0) > 0 && (
        <div className="mb-4">
          <label htmlFor="pairing-select" className="mb-1 block text-sm font-medium text-slate-300">
            Paired Discord guild
          </label>
          <select
            id="pairing-select"
            value={activePairingId ?? ''}
            onChange={(e) => setSelectedPairingId(Number(e.target.value))}
            className="rounded border border-slate-600 bg-slate-800 px-3 py-2 text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
          >
            {pairings?.map((pairing) => (
              <option key={pairing.id} value={pairing.id}>
                Guild {pairing.discord_guild_id} ({pairing.direction}
                {pairing.sync_enabled ? '' : ', disabled'})
              </option>
            ))}
          </select>
        </div>
      )}

      {activePairing && !activePairing.sync_enabled && (
        <p role="alert" className="mb-4 text-sm text-amber-400">
          Role sync is currently disabled for this pairing; bindings below will not reconcile
          until it&apos;s re-enabled.
        </p>
      )}

      {activePairingId !== null && (
        <>
          {bindingsLoading && (
            <p role="status" className="text-slate-400">
              Loading bindings…
            </p>
          )}

          {bindingsError && (
            <p role="alert" className="text-red-400">
              {extractErrorMessage(bindingsError, 'Failed to load role bindings for this pairing.')}
            </p>
          )}

          {!bindingsLoading && !bindingsError && (bindings?.length ?? 0) === 0 && (
            <p className="text-slate-400">No role bindings yet for this guild pairing.</p>
          )}

          <ul className="space-y-2" data-testid="binding-list">
            {bindings?.map((binding) => (
              <BindingRow
                key={binding.id}
                binding={binding}
                canManage={canManage}
                onDelete={setDeleteTarget}
              />
            ))}
          </ul>
        </>
      )}

      {createMutation.isError && (
        <p role="alert" className="mt-3 text-sm text-red-400">
          {extractErrorMessage(createMutation.error, 'Failed to create role binding. Please try again.')}
        </p>
      )}
      {deleteMutation.isError && (
        <p role="alert" className="mt-3 text-sm text-red-400">
          {extractErrorMessage(deleteMutation.error, 'Failed to delete role binding. Please try again.')}
        </p>
      )}

      <FormModalBuilder
        title="Add Role Sync Mapping"
        fields={createFields}
        isOpen={createOpen}
        onClose={() => setCreateOpen(false)}
        onSubmit={async (data) => {
          const syncScope = data.sync_scope as RoleSyncScope;
          const payload: CreateRoleSyncBindingPayload = {
            sync_scope: syncScope,
            discord_role_id: String(data.discord_role_id ?? '').trim(),
          };
          if (syncScope === 'subscriber_tier') {
            payload.subscriber_tier = Number(data.subscriber_tier) as SubscriberTier;
          }
          if (syncScope === 'community_role') {
            payload.community_role = data.community_role as CommunityRole;
          }
          await createMutation.mutateAsync(payload);
        }}
        submitButtonText="Add Mapping"
      />

      <FormModalBuilder
        title={
          deleteTarget
            ? `Delete mapping for Discord role ${deleteTarget.discord_role_id}?`
            : 'Delete Mapping'
        }
        fields={[]}
        isOpen={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        onSubmit={async () => {
          if (deleteTarget) {
            await deleteMutation.mutateAsync(deleteTarget);
          }
        }}
        submitButtonText="Delete"
      />
    </div>
  );
}
