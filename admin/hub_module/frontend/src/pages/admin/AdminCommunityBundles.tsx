import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FormModalBuilder, type FormField } from '@penguintechinc/react-libs';
import { useAuth } from '../../contexts/AuthContext';
import { useFeatureFlag } from '../../lib/useFeatureFlag';
import { bundleActivationApi } from '../../services/bundleActivationApi';

/**
 * Community Bundles ("app bundle modularity") admin page -- COMMUNITY tier
 * of the App Bundle 3-tier lifecycle (`hub_api/blueprints/v1/
 * bundle_activation.py`). The DB activation/bind (list/activate/deactivate
 * below) is the ONLY source of truth for whether a bundle runs in this
 * community; the PostHog `waddles.command-*` runtime flag shown beside each
 * row is a READ-ONLY status indicator and is never written from here.
 *
 * Gated behind `waddles.webui.community_bundles` (defaults OFF until
 * added to hub-api's `CLIENT_FLAG_KEYS` -- see PR description).
 */

const bundleActivationKeys = {
  list: (communityId: number) => ['community-bundle-activations', communityId] as const,
};

/** Read-only PostHog `waddles.command-<appId>` runtime status beside a bundle row. */
function CommandFlagBadge({ appId }: { appId: string }) {
  const enabled = useFeatureFlag(`waddles.command-${appId}`);
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${
        enabled ? 'bg-emerald-900/50 text-emerald-300' : 'bg-slate-700/60 text-slate-400'
      }`}
      title="Read-only PostHog runtime flag -- the activation state below is the source of truth"
    >
      command flag: {enabled ? 'on' : 'off'}
    </span>
  );
}

// `AuthContext.jsx` is plain JS (checkJs: false) -- its exported object-literal
// shape is not reliably inferred when consumed from a strict .tsx file, so
// `isCommunityAdmin` is typed explicitly here rather than trusting inference.
interface CommunityAuth {
  isCommunityAdmin: (communityId: string | number | undefined) => boolean;
}

export default function AdminCommunityBundles() {
  const pageEnabled = useFeatureFlag('waddles.webui.community_bundles');
  const { communityId: communityIdParam } = useParams<{ communityId: string }>();
  const { isCommunityAdmin } = useAuth() as CommunityAuth;
  const queryClient = useQueryClient();
  const [activateOpen, setActivateOpen] = useState(false);
  const [deactivateTarget, setDeactivateTarget] = useState<string | null>(null);

  const communityId = Number(communityIdParam);
  const hasValidCommunity = Number.isFinite(communityId);
  const canManage = Boolean(isCommunityAdmin(communityIdParam));

  const {
    data: bundles,
    isLoading,
    error: listError,
  } = useQuery({
    queryKey: bundleActivationKeys.list(communityId),
    queryFn: () => bundleActivationApi.list(communityId),
    enabled: pageEnabled && hasValidCommunity,
  });

  const activateMutation = useMutation({
    mutationFn: (appId: string) => bundleActivationApi.activate(communityId, appId),
    onSuccess: (_result, appId) => {
      console.debug('[CommunityBundles] Activate', { communityId, appId });
      void queryClient.invalidateQueries({ queryKey: bundleActivationKeys.list(communityId) });
      setActivateOpen(false);
    },
    onError: (err, appId) => {
      console.error('[CommunityBundles] ActivateFailed', { communityId, appId, error: String(err) });
    },
  });

  const deactivateMutation = useMutation({
    mutationFn: (appId: string) => bundleActivationApi.deactivate(communityId, appId),
    onSuccess: (_result, appId) => {
      console.debug('[CommunityBundles] Deactivate', { communityId, appId });
      void queryClient.invalidateQueries({ queryKey: bundleActivationKeys.list(communityId) });
      setDeactivateTarget(null);
    },
    onError: (err, appId) => {
      console.error('[CommunityBundles] DeactivateFailed', { communityId, appId, error: String(err) });
    },
  });

  if (!pageEnabled) {
    return (
      <div className="flex items-center justify-center min-h-[50vh] text-slate-400" role="status">
        Community bundles are not yet available for this community.
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

  const activateFields: FormField[] = [
    {
      name: 'appId',
      type: 'text',
      label: 'App ID',
      placeholder: 'waddles.core.example',
      required: true,
    },
  ];

  return (
    <div className="p-6 text-slate-300">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-amber-400">Community Bundles</h1>
        {canManage && (
          <button
            type="button"
            onClick={() => setActivateOpen(true)}
            className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500"
            aria-label="Activate a new bundle for this community"
          >
            Activate Bundle
          </button>
        )}
      </div>

      {isLoading && (
        <p role="status" className="text-slate-400">
          Loading bundles…
        </p>
      )}

      {listError && (
        <p role="alert" className="text-red-400">
          Failed to load bundles for this community.
        </p>
      )}

      {!isLoading && !listError && (bundles?.length ?? 0) === 0 && (
        <p className="text-slate-400">No bundles activated for this community yet.</p>
      )}

      <ul className="space-y-2" data-testid="bundle-activation-list">
        {bundles?.map((bundle) => (
          <li
            key={bundle.appId}
            className="flex items-center justify-between rounded border border-slate-700 bg-slate-800 px-4 py-3"
            data-testid={`bundle-row-${bundle.appId}`}
          >
            <div>
              <p className="font-medium text-slate-100">{bundle.appId}</p>
              <p className="text-xs text-slate-500">
                Activated{' '}
                {bundle.activatedAt ? new Date(bundle.activatedAt).toLocaleString() : 'unknown'}
              </p>
            </div>
            <div className="flex items-center gap-3">
              <CommandFlagBadge appId={bundle.appId} />
              {canManage && (
                <button
                  type="button"
                  onClick={() => setDeactivateTarget(bundle.appId)}
                  className="rounded border border-red-500/60 px-2 py-1 text-xs font-medium text-red-400 hover:bg-red-500/10 focus:outline-none focus:ring-2 focus:ring-red-500"
                  aria-label={`Deactivate ${bundle.appId}`}
                >
                  Deactivate
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>

      {activateMutation.isError && (
        <p role="alert" className="mt-3 text-sm text-red-400">
          Failed to activate bundle. Please try again.
        </p>
      )}
      {deactivateMutation.isError && (
        <p role="alert" className="mt-3 text-sm text-red-400">
          Failed to deactivate bundle. Please try again.
        </p>
      )}

      <FormModalBuilder
        title="Activate Bundle"
        fields={activateFields}
        isOpen={activateOpen}
        onClose={() => setActivateOpen(false)}
        onSubmit={async (data) => {
          const appId = String(data.appId ?? '').trim();
          if (!appId) return;
          await activateMutation.mutateAsync(appId);
        }}
        submitButtonText="Activate"
      />

      <FormModalBuilder
        title={deactivateTarget ? `Deactivate ${deactivateTarget}?` : 'Deactivate Bundle'}
        fields={[]}
        isOpen={deactivateTarget !== null}
        onClose={() => setDeactivateTarget(null)}
        onSubmit={async () => {
          if (deactivateTarget) {
            await deactivateMutation.mutateAsync(deactivateTarget);
          }
        }}
        submitButtonText="Deactivate"
      />
    </div>
  );
}
