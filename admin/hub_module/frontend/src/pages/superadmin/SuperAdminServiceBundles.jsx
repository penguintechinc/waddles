import { useEffect, useState } from 'react';
import { bundleAdminApi } from '../../services/api';

/**
 * Super-admin SERVICE bundles page: lists installed service bundles
 * (`GET /api/v1/superadmin/bundles?kind=service`) with their global status and a per-row
 * Disable / Enable action. Thin presentation only -- hub-api owns the lifecycle and the
 * dependents check. A 409 `has_dependents` response lists the app bundles that request this
 * service's tools; the confirm dialog then offers a forced disable (`?force=true`).
 */
function SuperAdminServiceBundles() {
  const [bundles, setBundles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [pendingApp, setPendingApp] = useState(null);
  const [confirm, setConfirm] = useState(null);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function load() {
    setLoading(true);
    try {
      const response = await bundleAdminApi.listInstalled({ kind: 'service' });
      setBundles(response.data.bundles);
      setError(null);
    } catch (err) {
      console.error('[SuperAdminServiceBundles] Load failed', { statusCode: err.response?.status });
      setError(err.response?.data?.error?.message || 'Failed to load service bundles');
    } finally {
      setLoading(false);
    }
  }

  async function disable(appId, force) {
    setPendingApp(appId);
    try {
      await bundleAdminApi.disableService(appId, force);
      setConfirm(null);
      setError(null);
      await load();
    } catch (err) {
      const apiError = err.response?.data?.error;
      if (err.response?.status === 409 && apiError?.code === 'has_dependents') {
        setConfirm({ appId, dependents: apiError.details?.dependents ?? [] });
      } else {
        setError(apiError?.message || 'Failed to disable service bundle');
      }
    } finally {
      setPendingApp(null);
    }
  }

  async function enable(appId) {
    setPendingApp(appId);
    try {
      await bundleAdminApi.enableService(appId);
      setError(null);
      await load();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to enable service bundle');
    } finally {
      setPendingApp(null);
    }
  }

  if (loading && bundles.length === 0) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-white">Service Bundles</h1>
        <p className="text-navy-300 mt-1">
          Installed service bundles. Disabling one hides it from every tenant and community; app
          bundles that request its tools are listed before you confirm.
        </p>
      </div>

      {error && (
        <div
          className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg"
          data-testid="service-bundles-error"
        >
          {error}
        </div>
      )}

      {bundles.length > 0 ? (
        <div className="bg-navy-800 border border-navy-700 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-navy-900 text-navy-400 uppercase text-xs">
              <tr>
                <th className="text-left px-4 py-3">Name</th>
                <th className="text-left px-4 py-3">App ID</th>
                <th className="text-left px-4 py-3">Version</th>
                <th className="text-left px-4 py-3">Status</th>
                <th className="text-right px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {bundles.map((b) => (
                <tr key={b.appId} className="border-t border-navy-700" data-testid="service-bundle-row">
                  <td className="px-4 py-3 text-white">{b.name}</td>
                  <td className="px-4 py-3 text-white font-mono text-xs">{b.appId}</td>
                  <td className="px-4 py-3 text-navy-300">{b.version}</td>
                  <td className="px-4 py-3 text-navy-300" data-testid={`service-status-${b.appId}`}>
                    {b.status}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {b.status === 'disabled' ? (
                      <button
                        onClick={() => enable(b.appId)}
                        disabled={pendingApp === b.appId}
                        data-testid={`service-enable-${b.appId}`}
                        className="px-3 py-1 rounded bg-gold-500 text-navy-900 font-semibold disabled:opacity-50"
                      >
                        Enable
                      </button>
                    ) : (
                      <button
                        onClick={() => disable(b.appId, false)}
                        disabled={pendingApp === b.appId}
                        data-testid={`service-disable-${b.appId}`}
                        className="px-3 py-1 rounded bg-red-600 text-white hover:bg-red-500 disabled:opacity-50"
                      >
                        Disable
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
          <p className="text-navy-400">No installed service bundles</p>
        </div>
      )}

      {confirm && (
        <div
          className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4"
          data-testid="service-disable-confirm"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="service-disable-title"
            className="bg-navy-800 border border-navy-700 rounded-lg max-w-md w-full p-6"
          >
            <h2 id="service-disable-title" className="text-xl font-bold text-white mb-2">
              Disable {confirm.appId}?
            </h2>
            <p className="text-navy-300 text-sm mb-2">
              These installed bundles request tools from this service and will stop working:
            </p>
            <ul className="mb-4 text-sm text-white font-mono list-disc pl-5">
              {confirm.dependents.map((d) => (
                <li key={d.appId} data-testid="service-dependent">
                  {d.appId} ({d.version})
                </li>
              ))}
            </ul>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setConfirm(null)}
                className="px-4 py-2 rounded bg-navy-700 text-white"
              >
                Cancel
              </button>
              <button
                onClick={() => disable(confirm.appId, true)}
                data-testid="service-disable-force"
                className="px-4 py-2 rounded bg-red-600 text-white"
              >
                Disable anyway
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default SuperAdminServiceBundles;
