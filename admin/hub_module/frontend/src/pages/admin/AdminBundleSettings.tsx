import { useQueries, useQuery } from '@tanstack/react-query';
import { PuzzlePieceIcon } from '@heroicons/react/24/outline';
import { useNavigate, useParams } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import { useFeatureFlag } from '../../lib/useFeatureFlag';
import { bundleActivationApi } from '../../services/bundleActivationApi';
import { BundleAdminError, bundleAdminApi } from '../../services/bundleAdminApi';
import BundleSettingsPanel from '../../components/bundles/BundleSettingsPanel';
import { bundleName, describeError, settingsKeys } from '../../components/bundles/settingsHelpers';

// AuthContext.jsx is plain JS; type the slice this page reads explicitly.
interface CommunityAuth {
  user?: { communities?: { id: number; role: string }[] } | null;
}

const EDIT_ROLES = ['community-owner', 'community-admin'];
const VIEW_ROLES = [...EDIT_ROLES, 'moderator'];

/**
 * Pluggable app-bundle settings (Nextcloud-apps style): one card per ACTIVATED
 * bundle, each rendered generically from the bundle's declared config schema.
 * Gated by `waddles.webui.community_bundles` (same feature as Bundle Apps).
 * Moderators view; community admins edit. hub-api enforces both.
 */
export default function AdminBundleSettings() {
  const enabled = useFeatureFlag('waddles.webui.community_bundles');
  const { communityId: cidParam, appId } = useParams<{ communityId: string; appId?: string }>();
  const navigate = useNavigate();
  const { user } = useAuth() as CommunityAuth;
  const communityId = Number(cidParam);
  const role = user?.communities?.find((c) => c.id === communityId)?.role ?? '';
  const canEdit = EDIT_ROLES.includes(role);
  const canView = VIEW_ROLES.includes(role);

  const list = useQuery({
    queryKey: ['community-bundle-activations', communityId],
    queryFn: () => bundleActivationApi.list(communityId),
    enabled: enabled && canView && Number.isFinite(communityId),
  });
  const bundles = list.data ?? [];
  const descriptors = useQueries({
    queries: bundles.map((b) => ({
      queryKey: settingsKeys.descriptor(communityId, b.appId),
      queryFn: () => bundleAdminApi.descriptor(communityId, b.appId),
    })),
  });

  if (!enabled) return null;
  const base = `/admin/${cidParam}/settings`;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="page-title">App Settings</h1>
        <p className="page-subtitle">Configure each app activated in this community.</p>
      </header>

      {!canView && (
        <p role="alert" className="text-danger-fg">
          You need to be a moderator or admin of this community to view app settings.
        </p>
      )}
      {list.isLoading && <p className="text-fg-muted">Loading apps…</p>}
      {list.error && <p role="alert" className="text-danger-fg">{describeError(list.error)}</p>}
      {canView && list.isSuccess && bundles.length === 0 && (
        <p className="text-fg-muted">No apps are activated yet. Activate one under Bundle Apps.</p>
      )}

      {appId && (
        <BundleSettingsPanel
          key={appId}
          communityId={communityId}
          appId={appId}
          canEdit={canEdit}
          onClose={() => navigate(base)}
        />
      )}

      <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-label="Activated apps">
        {bundles.map((b, i) => {
          const d = descriptors[i];
          const noConfig =
            d?.data?.configSchema === null ||
            (d?.error instanceof BundleAdminError && d.error.status === 404);
          const schemaDesc = d?.data?.configSchema?.description;
          const summary = typeof schemaDesc === 'string' ? schemaDesc : null;
          const status = d?.isLoading
            ? 'Loading…'
            : noConfig
              ? 'No settings'
              : d?.error
                ? 'Unavailable'
                : 'Configurable';
          return (
            <li key={b.appId}>
              <button
                type="button"
                data-testid={`bundle-card-${b.appId}`}
                onClick={() => navigate(`${base}/${encodeURIComponent(b.appId)}`)}
                aria-current={appId === b.appId ? 'true' : undefined}
                className="card card-interactive flex h-full w-full items-start gap-3 p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
              >
                <PuzzlePieceIcon className="h-8 w-8 shrink-0 text-gold-400" aria-hidden="true" />
                <span className="min-w-0 flex-1">
                  <span className="section-title block truncate">{bundleName(b.appId)}</span>
                  <span className="block truncate text-xs text-fg-muted">
                    {b.appId} · v{b.version ?? d?.data?.version ?? '?'}
                  </span>
                  <span className="mt-1 inline-block rounded bg-surface-raised px-2 py-0.5 text-xs text-fg-secondary">
                    {status}
                  </span>
                  {summary && <span className="mt-2 block text-sm text-fg-secondary">{summary}</span>}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
