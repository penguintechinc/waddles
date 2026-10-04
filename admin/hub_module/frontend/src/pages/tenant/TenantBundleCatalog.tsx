import { useFeatureFlag } from '../../lib/useFeatureFlag';

/**
 * Route stub for the tenant bundle catalog slice (Wave-1, post-S0).
 * Pre-registered in `App.jsx` as `/tenant/:tenantSlug/bundles` so the
 * slice that implements this only needs to fill in this file's body --
 * the route wiring and lazy import already exist, avoiding an `App.jsx`
 * merge conflict between parallel slices.
 */
export default function TenantBundleCatalog() {
  const enabled = useFeatureFlag('waddles.hubwebui-tenant-bundle-catalog');

  if (!enabled) {
    return (
      <div className="flex items-center justify-center min-h-[50vh] text-slate-400" role="status">
        Tenant bundle catalog is not yet available for this tenant.
      </div>
    );
  }

  // TODO(wave-1): tenant bundle catalog implementation.
  return (
    <div className="p-6 text-slate-300">
      <h1 className="text-xl font-semibold text-amber-400">Bundle Catalog</h1>
      <p>Coming soon.</p>
    </div>
  );
}
