import { useFeatureFlag } from '../../lib/useFeatureFlag';

/**
 * Route stub for the community bundles slice (Wave-1, post-S0).
 * Pre-registered in `App.jsx` as `/admin/:communityId/bundles` -- see
 * `TenantBundleCatalog.tsx` for the rationale.
 */
export default function AdminCommunityBundles() {
  const enabled = useFeatureFlag('waddles.hubwebui-community-bundles');

  if (!enabled) {
    return (
      <div className="flex items-center justify-center min-h-[50vh] text-slate-400" role="status">
        Community bundles are not yet available for this community.
      </div>
    );
  }

  // TODO(wave-1): community bundles implementation.
  return (
    <div className="p-6 text-slate-300">
      <h1 className="text-xl font-semibold text-amber-400">Community Bundles</h1>
      <p>Coming soon.</p>
    </div>
  );
}
