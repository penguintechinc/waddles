import { useFeatureFlag } from '../../lib/useFeatureFlag';

/**
 * Route stub for the super-communities (1:N community model) slice
 * (Wave-1, post-S0). Pre-registered in `App.jsx` as
 * `/superadmin/super-communities` -- see `TenantBundleCatalog.tsx` for the
 * rationale.
 */
export default function SuperAdminSuperCommunities() {
  const enabled = useFeatureFlag('waddles.hubwebui-super-communities');

  if (!enabled) {
    return (
      <div className="flex items-center justify-center min-h-[50vh] text-slate-400" role="status">
        Super communities are not yet available.
      </div>
    );
  }

  // TODO(wave-1): super communities (1:N model) implementation.
  return (
    <div className="p-6 text-slate-300">
      <h1 className="text-xl font-semibold text-amber-400">Super Communities</h1>
      <p>Coming soon.</p>
    </div>
  );
}
