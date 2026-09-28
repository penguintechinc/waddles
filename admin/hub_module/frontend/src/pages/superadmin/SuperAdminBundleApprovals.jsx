import { useEffect, useState } from 'react';
import { CheckCircleIcon, XCircleIcon, ClockIcon, EyeIcon } from '@heroicons/react/24/outline';
import { bundleAdminApi, bundleApi } from '../../services/api';

/**
 * Global-admin bundle-version INSTALL queue (three-tier app lifecycle,
 * tier 1 of 3 -- Justin's 2026-09-27 ruling): lists `app_version_uploads`
 * rows awaiting a platform:admin install/deny decision
 * (`GET /api/v1/admin/bundle-versions`, hub_api/blueprints/v1/bundle_admin.py)
 * across every vendor/first-party `app_id`, and drives the per-version
 * approve/deny actions (hub_api/blueprints/v1/bundle_approvals.py).
 * "Review" fetches the permission summary + hash on demand so Install
 * always sends the hash it just displayed (fails closed, 409, if the
 * manifest changed meanwhile).
 *
 * Tier boundary: this page installs a version into the PLATFORM CATALOG
 * only -- it deliberately does NOT ask for a target tenant/community.
 * Tier 2 (tenant admin: make an installed app available/hidden in the
 * tenant marketplace) and tier 3 (community admin: activate/deactivate an
 * available app for their community) are separate, not-yet-built backend
 * endpoints (see TenantModules.jsx / AdminModules.jsx follow-up notes) --
 * this page must never invent a communityId/tenantId field to bridge that
 * gap itself. Today's `bundle_approvals.py::approve_version` still accepts
 * an optional `communityId` for backward compatibility with its pre-tier
 * behavior; this page always omits it (server default: tenant-wide), and
 * should switch to a dedicated install-only endpoint once tier 2/3 land.
 */
function SuperAdminBundleApprovals() {
  const [versions, setVersions] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [status, setStatus] = useState('pending');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [reviewing, setReviewing] = useState(null);
  const [summary, setSummary] = useState(null);
  const [permissionHash, setPermissionHash] = useState(null);
  const [denyReason, setDenyReason] = useState('');
  const [panelError, setPanelError] = useState(null);
  const [panelLoading, setPanelLoading] = useState(false);
  const [actionPending, setActionPending] = useState(false);

  useEffect(() => {
    loadVersions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, page]);

  async function loadVersions() {
    setLoading(true);
    try {
      const response = await bundleAdminApi.listPendingVersions({ status, page, limit: 25 });
      setVersions(response.data.versions);
      setPagination(response.data.pagination);
      setError(null);
    } catch (err) {
      console.error('[SuperAdminBundleApprovals] Load failed', {
        status,
        page,
        statusCode: err.response?.status,
      });
      setError(err.response?.data?.error?.message || 'Failed to load bundle versions');
    } finally {
      setLoading(false);
    }
  }

  async function openReview(row) {
    setReviewing(row);
    setSummary(null);
    setPermissionHash(null);
    setDenyReason('');
    setPanelError(null);
    setPanelLoading(true);
    try {
      const response = await bundleApi.getPermissions(row.appId, row.version);
      setSummary(response.data.summary);
      setPermissionHash(response.data.permissionHash);
    } catch (err) {
      setPanelError(err.response?.data?.error?.message || 'Failed to load permission summary');
    } finally {
      setPanelLoading(false);
    }
  }

  function closePanel() {
    setReviewing(null);
    setSummary(null);
    setPermissionHash(null);
    setPanelError(null);
  }

  async function handleInstall() {
    if (!reviewing) return;
    setActionPending(true);
    setPanelError(null);
    try {
      // Tier 1 only -- platform-catalog install, no communityId/tenantId.
      // See this file's module docstring for the tier-2/3 boundary.
      await bundleApi.approveVersion(reviewing.appId, reviewing.version, { permissionHash });
      console.debug('[SuperAdminBundleApprovals] Install', {
        appId: reviewing.appId,
        version: reviewing.version,
      });
      closePanel();
      await loadVersions();
    } catch (err) {
      setPanelError(err.response?.data?.error?.message || 'Failed to install version');
    } finally {
      setActionPending(false);
    }
  }

  async function handleDeny() {
    if (!reviewing) return;
    if (!denyReason.trim()) {
      setPanelError('A denial reason is required');
      return;
    }
    setActionPending(true);
    setPanelError(null);
    try {
      await bundleApi.denyVersion(reviewing.appId, reviewing.version, { reason: denyReason.trim() });
      console.debug('[SuperAdminBundleApprovals] Deny', {
        appId: reviewing.appId,
        version: reviewing.version,
      });
      closePanel();
      await loadVersions();
    } catch (err) {
      setPanelError(err.response?.data?.error?.message || 'Failed to deny version');
    } finally {
      setActionPending(false);
    }
  }

  const statusIcon = (s) => {
    if (s === 'REJECTED') return <XCircleIcon className="w-5 h-5 text-red-400" />;
    if (s === 'PUBLISHED') return <ClockIcon className="w-5 h-5 text-orange-400" />;
    return <CheckCircleIcon className="w-5 h-5 text-emerald-400" />;
  };

  if (loading && versions.length === 0) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-white">Bundle Version Install Queue</h1>
        <p className="text-navy-300 mt-1">
          Review vendor/first-party bundle-version uploads and install them into the platform
          catalog, or deny them. Making an installed app available to tenants/communities is a
          separate, later step (tenant + community admin pages).
        </p>
      </div>

      <div className="bg-navy-800 border border-navy-700 rounded-lg p-4">
        <label className="block text-sm font-medium text-navy-300 mb-2">Filter by Status</label>
        <select
          data-testid="bundle-approvals-status-filter"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setPage(1);
          }}
          className="w-full md:w-64 bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white focus:outline-none focus:border-gold-400"
        >
          <option value="pending">Pending Approval</option>
          <option value="rejected">Rejected</option>
        </select>
      </div>

      {error && (
        <div
          className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg"
          data-testid="bundle-approvals-error"
        >
          {error}
        </div>
      )}

      {versions.length > 0 ? (
        <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-navy-900 text-navy-400 uppercase text-xs">
              <tr>
                <th className="text-left px-4 py-3">App ID</th>
                <th className="text-left px-4 py-3">Version</th>
                <th className="text-left px-4 py-3">Status</th>
                <th className="text-left px-4 py-3">Requested By</th>
                <th className="text-left px-4 py-3">Created</th>
                <th className="text-right px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((v) => (
                <tr key={v.versionId} className="border-t border-navy-700" data-testid="bundle-approvals-row">
                  <td className="px-4 py-3 text-white font-mono text-xs">{v.appId}</td>
                  <td className="px-4 py-3 text-navy-300">{v.version}</td>
                  <td className="px-4 py-3">
                    <span className="inline-flex items-center gap-1 text-navy-300">
                      {statusIcon(v.status)} {v.status}
                    </span>
                    {v.status === 'REJECTED' && v.rejectReason && (
                      <p className="text-xs text-red-300 mt-1">{v.rejectReason}</p>
                    )}
                  </td>
                  <td className="px-4 py-3 text-navy-300">{v.requestedBy ?? 'N/A'}</td>
                  <td className="px-4 py-3 text-navy-300">
                    {v.createdAt ? new Date(v.createdAt).toLocaleString() : 'N/A'}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {v.status === 'PUBLISHED' && (
                      <button
                        onClick={() => openReview(v)}
                        className="flex items-center gap-1 text-gold-400 hover:text-gold-300 ml-auto"
                        data-testid={`bundle-approvals-review-${v.versionId}`}
                      >
                        <EyeIcon className="w-4 h-4" /> Review
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="bg-navy-800 border border-navy-700 rounded-lg p-12 text-center">
          <p className="text-navy-400">No {status === 'pending' ? 'pending' : status} bundle versions</p>
        </div>
      )}

      {pagination && pagination.totalPages > 1 && (
        <div className="flex justify-center gap-2">
          <button
            disabled={page <= 1}
            onClick={() => setPage((p) => p - 1)}
            className="px-3 py-1 rounded bg-navy-800 border border-navy-700 text-navy-300 disabled:opacity-40"
          >
            Prev
          </button>
          <span className="text-navy-300 px-2 py-1">
            Page {pagination.page} of {pagination.totalPages}
          </span>
          <button
            disabled={page >= pagination.totalPages}
            onClick={() => setPage((p) => p + 1)}
            className="px-3 py-1 rounded bg-navy-800 border border-navy-700 text-navy-300 disabled:opacity-40"
          >
            Next
          </button>
        </div>
      )}

      {/* Review + Approve/Deny panel */}
      {reviewing && (
        <div
          className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4"
          data-testid="bundle-approvals-review-panel"
        >
          <div className="bg-navy-800 border border-navy-700 rounded-lg max-w-2xl w-full max-h-[85vh] overflow-y-auto p-6">
            <h2 className="text-xl font-bold text-white mb-1">{reviewing.appId}</h2>
            <p className="text-navy-400 text-sm mb-4">Version {reviewing.version}</p>

            {panelLoading && <p className="text-navy-300">Loading permission summary...</p>}
            {panelError && (
              <div
                className="bg-red-500/10 border border-red-500/20 text-red-400 px-3 py-2 rounded mb-4"
                data-testid="bundle-approvals-panel-error"
              >
                {panelError}
              </div>
            )}
            {summary && (
              <>
                <pre
                  className="bg-navy-900 border border-navy-700 rounded p-3 text-xs text-navy-300 overflow-x-auto mb-4"
                  data-testid="bundle-approvals-summary"
                >
                  {JSON.stringify(summary, null, 2)}
                </pre>

                <div className="mb-4">
                  <label htmlFor="denyReason" className="block text-sm font-medium text-navy-300 mb-1">
                    Denial Reason (required only to deny)
                  </label>
                  <textarea
                    id="denyReason"
                    data-testid="bundle-approvals-deny-reason"
                    value={denyReason}
                    onChange={(e) => setDenyReason(e.target.value)}
                    rows={3}
                    placeholder="Explain why this version is denied..."
                    className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-gold-400"
                  />
                </div>
              </>
            )}

            <div className="flex justify-end gap-3">
              <button
                onClick={closePanel}
                className="px-4 py-2 rounded bg-navy-700 text-white hover:bg-navy-600"
              >
                Close
              </button>
              <button
                onClick={handleDeny}
                disabled={actionPending || !summary}
                data-testid="bundle-approvals-deny-button"
                className="px-4 py-2 rounded bg-red-600 text-white hover:bg-red-500 disabled:opacity-50"
              >
                Deny
              </button>
              <button
                onClick={handleInstall}
                disabled={actionPending || !summary}
                data-testid="bundle-approvals-install-button"
                className="px-4 py-2 rounded bg-gold-500 text-navy-900 font-semibold hover:bg-gold-400 disabled:opacity-50"
              >
                Install
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default SuperAdminBundleApprovals;
