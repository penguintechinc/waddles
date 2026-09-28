import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { tenantApi } from '../../services/api';

/**
 * Tier-2 follow-up placeholder (three-tier app lifecycle, Justin's
 * 2026-09-27 ruling): once a global admin installs a bundle version into
 * the platform catalog (SuperAdminBundleApprovals.jsx, tier 1), a TENANT
 * admin should be able to mark it available/hidden in this tenant's
 * marketplace here -- tier 3 (community admin activate/deactivate) lives
 * in AdminModules.jsx. No such tenant-level availability endpoint exists
 * in hub-api yet (no `app_active_versions`/tenant-visibility read or
 * write route as of this commit) -- this section is deliberately a
 * placeholder, not a call against an invented endpoint, until that
 * backend lands.
 */
function BundleAppsAvailabilityFollowUp() {
  return (
    <div
      className="bg-navy-800 border border-navy-700 border-dashed rounded-xl p-6 text-center"
      data-testid="tenant-bundle-apps-followup"
    >
      <p className="text-gold-400 font-semibold mb-1">Bundle Apps (Coming Soon)</p>
      <p className="text-navy-400 text-sm">
        Making installed bundle-catalog apps available or hidden for this tenant&apos;s communities
        will appear here once the tenant-availability backend endpoint ships.
      </p>
    </div>
  );
}

function TenantModules() {
  const { tenantSlug } = useParams();
  const [allModules, setAllModules] = useState([]);
  const [allowedIds, setAllowedIds] = useState(null); // null = allow all
  const [allowAll, setAllowAll] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    loadModules();
  }, [tenantSlug]);

  const loadModules = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await tenantApi.getModules(tenantSlug);
      setAllModules(res.data.modules || []);
      const ids = res.data.allowedModuleIds;
      if (ids === null || ids === undefined) {
        setAllowAll(true);
        setAllowedIds(null);
      } else {
        setAllowAll(false);
        setAllowedIds(new Set(ids));
      }
      setDirty(false);
    } catch (err) {
      setError(err?.response?.data?.error || 'Failed to load modules.');
    }
    setLoading(false);
  };

  const handleAllowAllToggle = (checked) => {
    setAllowAll(checked);
    if (checked) {
      setAllowedIds(null);
    } else {
      // Start with all modules selected when switching to manual mode
      setAllowedIds(new Set(allModules.map((m) => m.id)));
    }
    setDirty(true);
    setSaveSuccess(false);
  };

  const handleModuleToggle = (moduleId, checked) => {
    setAllowedIds((prev) => {
      const next = new Set(prev || []);
      if (checked) {
        next.add(moduleId);
      } else {
        next.delete(moduleId);
      }
      return next;
    });
    setDirty(true);
    setSaveSuccess(false);
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);
    try {
      const ids = allowAll ? null : Array.from(allowedIds || []);
      await tenantApi.updateModules(tenantSlug, ids);
      setSaveSuccess(true);
      setDirty(false);
      setTimeout(() => setSaveSuccess(false), 3000);
    } catch (err) {
      setSaveError(err?.response?.data?.error || 'Failed to save module settings.');
    }
    setSaving(false);
  };

  const modulesByCategory = allModules.reduce((acc, mod) => {
    const cat = mod.category || 'General';
    if (!acc[cat]) acc[cat] = [];
    acc[cat].push(mod);
    return acc;
  }, {});

  const isModuleAllowed = (moduleId) => {
    if (allowAll) return true;
    return allowedIds ? allowedIds.has(moduleId) : false;
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-64">
        <div className="animate-spin rounded-full h-10 w-10 border-t-2 border-b-2 border-gold-400" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <div className="bg-red-900/30 border border-red-700 rounded-xl p-4 text-red-300">
          {error}
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-sky-100 text-2xl font-bold">Module Permissions</h1>
          <p className="text-sky-400 text-sm mt-1">
            Control which modules are available to communities in this tenant
          </p>
        </div>
        <button
          onClick={handleSave}
          disabled={saving || !dirty}
          className="px-4 py-2 bg-gold-500 text-navy-900 rounded-lg font-semibold hover:bg-gold-400 transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
      </div>

      {saveError && (
        <div className="bg-red-900/30 border border-red-700 rounded-xl p-4 text-red-300 text-sm">
          {saveError}
        </div>
      )}

      {saveSuccess && (
        <div className="bg-green-900/30 border border-green-700 rounded-xl p-4 text-green-300 text-sm">
          Module permissions saved successfully.
        </div>
      )}

      <BundleAppsAvailabilityFollowUp />

      {/* Allow All Toggle */}
      <div className="bg-navy-800 border border-navy-700 rounded-xl p-5">
        <label className="flex items-center gap-4 cursor-pointer">
          <div className="relative">
            <input
              type="checkbox"
              checked={allowAll}
              onChange={(e) => handleAllowAllToggle(e.target.checked)}
              className="sr-only"
            />
            <div
              className={`w-11 h-6 rounded-full transition-colors ${
                allowAll ? 'bg-gold-500' : 'bg-navy-600'
              }`}
            >
              <div
                className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
                  allowAll ? 'translate-x-5' : 'translate-x-0'
                }`}
              />
            </div>
          </div>
          <div>
            <p className="text-sky-100 font-semibold">Allow All Modules</p>
            <p className="text-sky-400 text-sm">
              {allowAll
                ? 'All current and future modules are available to this tenant.'
                : 'Only selected modules are available to this tenant.'}
            </p>
          </div>
        </label>
      </div>

      {/* Module List */}
      {!allowAll && (
        <div className="space-y-4">
          {allModules.length === 0 ? (
            <div className="bg-navy-800 border border-navy-700 rounded-xl p-8 text-center text-sky-400">
              No modules available.
            </div>
          ) : (
            Object.entries(modulesByCategory).map(([category, modules]) => (
              <div key={category} className="bg-navy-800 border border-navy-700 rounded-xl overflow-hidden">
                <div className="px-5 py-3 bg-navy-750 border-b border-navy-700">
                  <h3 className="text-gold-400 text-sm font-semibold uppercase tracking-wider">
                    {category}
                  </h3>
                </div>
                <div className="divide-y divide-navy-700">
                  {modules.map((mod) => (
                    <label
                      key={mod.id}
                      className="flex items-center gap-4 px-5 py-4 cursor-pointer hover:bg-navy-750 transition-colors"
                    >
                      <input
                        type="checkbox"
                        checked={isModuleAllowed(mod.id)}
                        onChange={(e) => handleModuleToggle(mod.id, e.target.checked)}
                        className="w-4 h-4 accent-gold-500 cursor-pointer"
                      />
                      <div className="flex-1 min-w-0">
                        <p className="text-sky-100 font-medium">{mod.displayName || mod.name}</p>
                        {mod.description && (
                          <p className="text-sky-400 text-sm mt-0.5 truncate">{mod.description}</p>
                        )}
                      </div>
                      {mod.isCore && (
                        <span className="flex-shrink-0 text-xs px-2 py-0.5 bg-gold-500/20 text-gold-400 border border-gold-500/30 rounded-full">
                          Core
                        </span>
                      )}
                    </label>
                  ))}
                </div>
              </div>
            ))
          )}

          {/* Selection Summary */}
          {allModules.length > 0 && (
            <p className="text-sky-400 text-sm text-right">
              {allowedIds ? allowedIds.size : 0} of {allModules.length} modules selected
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export default TenantModules;
