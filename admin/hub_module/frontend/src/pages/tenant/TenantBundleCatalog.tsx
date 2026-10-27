import { useState } from 'react';
import type { FormEvent } from 'react';
import { useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { AxiosError } from 'axios';

import { useFeatureFlag } from '../../lib/useFeatureFlag';
import { useAuth } from '../../contexts/AuthContext';
import {
  bundleAvailabilityApi,
  type ApiErrorResponse,
  type BundleAvailability,
} from '../../services/bundleAvailabilityApi';

const availabilityKeys = {
  all: ['tenantBundleAvailability'] as const,
  list: (tenantSlug: string) => ['tenantBundleAvailability', tenantSlug] as const,
};

/** Pulls the standardized `flask_core` error envelope's message, falling back to a generic one. */
function apiErrorMessage(err: unknown, fallback: string): string {
  const axiosErr = err as AxiosError<ApiErrorResponse>;
  return axiosErr.response?.data?.error?.message ?? fallback;
}

/**
 * `AuthContext` is plain untyped JS (`createContext(null)`), so TS infers
 * its `useContext` return as the literal `null` and narrows the
 * post-null-check type to `never` -- not something this slice owns or may
 * edit (out of scope). Narrow the one shape this page actually reads.
 */
interface AuthContextValue {
  hasRole: (role: string) => boolean;
  isAdmin: boolean;
  isSuperAdmin: boolean;
}

/**
 * Tenant-tier bundle availability catalog (App Bundle lifecycle tier 2):
 * lists every app this tenant has an availability record for and lets a
 * tenant admin enable/disable which globally-installed bundles are
 * AVAILABLE in this tenant's marketplace -- the layer above per-community
 * activation (a separate module-allowlist page, not this one).
 *
 * Pre-registered in `App.jsx` as `/tenant/:tenantSlug/bundles` (S0
 * foundation) -- this file fills in the body only.
 */
export default function TenantBundleCatalog() {
  const enabled = useFeatureFlag('waddles.webui.tenant_bundle_catalog');
  const { tenantSlug } = useParams<{ tenantSlug: string }>();
  const { hasRole, isAdmin, isSuperAdmin } = useAuth() as unknown as AuthContextValue;
  const queryClient = useQueryClient();
  const [newAppId, setNewAppId] = useState('');
  const [formError, setFormError] = useState<string | null>(null);

  // UX-only gate -- `tenant:admin` scope is enforced server-side on the
  // mutating routes regardless of what this renders.
  const canManage = Boolean(hasRole('tenant-admin') || isAdmin || isSuperAdmin);

  const listQuery = useQuery({
    queryKey: availabilityKeys.list(tenantSlug ?? ''),
    queryFn: () => bundleAvailabilityApi.list(tenantSlug ?? ''),
    enabled: enabled && Boolean(tenantSlug),
  });

  const enableMutation = useMutation({
    mutationFn: (appId: string) => {
      console.debug('[TenantBundleCatalog] EnableAvailability', { appId });
      return bundleAvailabilityApi.enable(tenantSlug ?? '', { appId });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: availabilityKeys.list(tenantSlug ?? '') });
    },
  });

  const disableMutation = useMutation({
    mutationFn: (appId: string) => {
      console.debug('[TenantBundleCatalog] DisableAvailability', { appId });
      return bundleAvailabilityApi.disable(tenantSlug ?? '', appId);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: availabilityKeys.list(tenantSlug ?? '') });
    },
  });

  if (!enabled) {
    return (
      <div className="flex items-center justify-center min-h-[50vh] text-slate-400" role="status">
        Tenant bundle catalog is not yet available for this tenant.
      </div>
    );
  }

  if (!tenantSlug) {
    return (
      <div className="p-6 text-red-400" role="alert">
        Missing tenant in route.
      </div>
    );
  }

  const handleAddSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(null);
    const appId = newAppId.trim();
    if (!appId) {
      setFormError('App ID is required.');
      return;
    }
    enableMutation.mutate(appId, {
      onSuccess: () => setNewAppId(''),
      onError: (err) => setFormError(apiErrorMessage(err, 'Failed to enable bundle.')),
    });
  };

  const handleToggle = (bundle: BundleAvailability) => {
    if (bundle.available) {
      disableMutation.mutate(bundle.appId);
    } else {
      enableMutation.mutate(bundle.appId);
    }
  };

  return (
    <div className="p-6 text-slate-300">
      <h1 className="text-xl font-semibold text-amber-400">Bundle Catalog</h1>
      <p className="text-slate-400 mt-1">
        Control which globally-installed app bundles are available to this tenant&apos;s communities.
      </p>

      {canManage && (
        <form onSubmit={handleAddSubmit} className="mt-6 flex items-end gap-3" aria-label="Add bundle availability">
          <div className="flex flex-col">
            <label htmlFor="new-app-id" className="text-sm text-slate-400 mb-1">
              App ID
            </label>
            <input
              id="new-app-id"
              type="text"
              value={newAppId}
              onChange={(event) => setNewAppId(event.target.value)}
              placeholder="waddles.example.app"
              className="bg-slate-800 border border-slate-700 rounded-md px-3 py-2 text-slate-200 focus:outline-none focus:ring-2 focus:ring-sky-500"
            />
          </div>
          <button
            type="submit"
            disabled={enableMutation.isPending}
            aria-label="Make bundle available"
            className="bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white rounded-md px-4 py-2 transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500"
          >
            {enableMutation.isPending ? 'Adding...' : 'Make Available'}
          </button>
        </form>
      )}
      {formError && (
        <p className="text-red-400 text-sm mt-2" role="alert">
          {formError}
        </p>
      )}

      <div className="mt-6">
        {listQuery.isLoading && <p className="text-slate-400">Loading bundles...</p>}
        {listQuery.isError && (
          <p className="text-red-400" role="alert">
            {apiErrorMessage(listQuery.error, 'Failed to load bundle availability.')}
          </p>
        )}
        {listQuery.isSuccess && listQuery.data.length === 0 && (
          <p className="text-slate-400">No bundles have an availability record for this tenant yet.</p>
        )}
        {listQuery.isSuccess && listQuery.data.length > 0 && (
          <table className="w-full text-left border-collapse" data-testid="bundle-availability-table">
            <thead>
              <tr className="border-b border-slate-700 text-slate-400 text-sm">
                <th className="py-2 pr-4">App</th>
                <th className="py-2 pr-4">Status</th>
                <th className="py-2 pr-4">Pinned Version</th>
                <th className="py-2 pr-4">Updated</th>
                {canManage && <th className="py-2 pr-4">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {listQuery.data.map((bundle) => (
                <tr key={bundle.appId} className="border-b border-slate-800">
                  <td className="py-2 pr-4 text-slate-200">{bundle.appId}</td>
                  <td className="py-2 pr-4">
                    <span
                      className={
                        bundle.available
                          ? 'inline-block rounded-full bg-emerald-500/20 text-emerald-300 px-2 py-0.5 text-xs'
                          : 'inline-block rounded-full bg-slate-700 text-slate-400 px-2 py-0.5 text-xs'
                      }
                    >
                      {bundle.available ? 'Available' : 'Unavailable'}
                    </span>
                  </td>
                  <td className="py-2 pr-4 text-slate-400">{bundle.pinnedVersionId ?? '—'}</td>
                  <td className="py-2 pr-4 text-slate-400">
                    {bundle.updatedAt ? new Date(bundle.updatedAt).toLocaleString() : '—'}
                  </td>
                  {canManage && (
                    <td className="py-2 pr-4">
                      <button
                        type="button"
                        onClick={() => handleToggle(bundle)}
                        disabled={enableMutation.isPending || disableMutation.isPending}
                        aria-label={`${bundle.available ? 'Disable' : 'Enable'} ${bundle.appId}`}
                        className="text-sky-400 hover:text-sky-300 disabled:opacity-50 transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500 rounded"
                      >
                        {bundle.available ? 'Disable' : 'Enable'}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
