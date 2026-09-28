import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import { bundleApi } from '../../services/api';

/**
 * Vendor bundle-version onboarding form -- `POST /api/v1/apps/{app_id}/versions`
 * (hub_api/blueprints/v1/bundle_versions.py). Replaces the old marketplace
 * intake form (webhook/pricing/payment fields, `/api/v1/vendor/submit`) --
 * that pipeline is a separate, legacy system; this page is the bundle-
 * onboarding entrypoint for the WASI-component pipeline.
 *
 * Vendors may only upload a pre-built `.wasm` component into their own
 * `waddles.integrations.vendor-{id}.*` namespace -- source-tarball uploads
 * are 400-refused server-side (services/vendor_bundle_authz.py), so this
 * form never offers a source-upload field. `version` is not a form field:
 * the server derives it entirely from the manifest's own `version:` key.
 */
function VendorSubmissionForm() {
  const navigate = useNavigate();
  const { user, loading: authLoading, isVendor, hasRole } = useAuth();

  const [appId, setAppId] = useState('');
  const [manifestFile, setManifestFile] = useState(null);
  const [componentFile, setComponentFile] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  const isPlatformAdmin =
    hasRole('admin') || hasRole('super_admin') || hasRole('platform-admin');
  const namespaceHint = user
    ? `waddles.integrations.vendor-${user.id}.<bundle-name>`
    : 'waddles.integrations.vendor-<your-id>.<bundle-name>';

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setResult(null);

    // Manual checks below, not a native `required` on the file inputs --
    // browser (and jsdom) file-input validity messages are terse and
    // inconsistent across engines; `vendor-submit-error` gives one
    // consistent, styled message matching every other error path here.
    if (!manifestFile) {
      setError('A bundle manifest (YAML) is required.');
      return;
    }
    if (!componentFile) {
      setError(
        'A pre-built .wasm component is required -- vendor onboarding does not accept source uploads.',
      );
      return;
    }

    const formData = new FormData();
    formData.append('manifest', manifestFile);
    formData.append('component', componentFile);

    setSubmitting(true);
    console.debug('[VendorSubmissionForm] Submit', { appId });
    try {
      const response = await bundleApi.createVersion(appId.trim(), formData);
      const body = response.data;
      console.debug('[VendorSubmissionForm] Submit succeeded', { appId, status: body.status });
      setResult(body);
    } catch (err) {
      const apiError = err.response?.data?.error;
      console.error('[VendorSubmissionForm] Submit failed', {
        appId,
        status: err.response?.status,
        code: apiError?.code,
      });
      setError(apiError?.message || 'Failed to submit bundle version');
    } finally {
      setSubmitting(false);
    }
  }

  if (authLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-gold-400" />
      </div>
    );
  }

  if (!user) {
    return (
      <div className="max-w-2xl mx-auto p-6 text-center" data-testid="vendor-submit-login-required">
        <h1 className="text-2xl font-bold text-white mb-2">Sign In Required</h1>
        <p className="text-navy-300">
          You must be signed in with a vendor account to submit a bundle version.
        </p>
        <a
          href="/login"
          className="inline-block mt-4 px-4 py-2 rounded bg-gold-500 text-navy-900 font-semibold focus:outline-none focus:ring-2 focus:ring-gold-400"
        >
          Sign In
        </a>
      </div>
    );
  }

  if (!isVendor && !isPlatformAdmin) {
    return (
      <div className="max-w-2xl mx-auto p-6 text-center" data-testid="vendor-submit-not-vendor">
        <h1 className="text-2xl font-bold text-white mb-2">Vendor Status Required</h1>
        <p className="text-navy-300">
          Only approved vendors can submit bundle versions. Request vendor status from{' '}
          <a href="/vendor/request" className="text-gold-400 underline">
            your account settings
          </a>
          .
        </p>
      </div>
    );
  }

  if (result) {
    const isRejected = result.status === 'REJECTED';
    const isStaged = result.status === 'PUBLISHED';
    return (
      <div className="max-w-2xl mx-auto p-6" data-testid="vendor-submit-result">
        <div
          className={`rounded-lg border p-6 ${
            isRejected ? 'bg-red-500/10 border-red-500/20' : 'bg-emerald-500/10 border-emerald-500/20'
          }`}
        >
          <h2 className="text-xl font-bold text-white mb-2">
            {isRejected ? 'Version Rejected' : 'Version Uploaded'}
          </h2>
          <p className="text-navy-300 text-sm">
            Version ID: <span className="text-white">{result.versionId}</span>
          </p>
          <p className="text-navy-300 text-sm">
            Status: <span className="text-white" data-testid="vendor-submit-status">{result.status}</span>
          </p>
          {isRejected && result.rejectReason && (
            <p className="text-red-300 text-sm mt-3" data-testid="vendor-submit-reject-reason">
              Reason: {result.rejectReason}
            </p>
          )}
          {isStaged && (
            <p className="text-emerald-300 text-sm mt-3">
              Your component passed WIT-conformance validation and is staged. It now awaits a
              global-admin approve/deny decision before any tenant can install it.
            </p>
          )}
          <div className="flex gap-3 mt-4">
            <button
              type="button"
              onClick={() => {
                setResult(null);
                setAppId('');
                setManifestFile(null);
                setComponentFile(null);
              }}
              className="px-4 py-2 rounded bg-navy-700 text-white hover:bg-navy-600 focus:outline-none focus:ring-2 focus:ring-gold-400"
              data-testid="vendor-submit-another"
            >
              Submit Another Version
            </button>
            <button
              type="button"
              onClick={() => navigate('/vendor/submissions')}
              className="px-4 py-2 rounded bg-navy-800 border border-navy-700 text-navy-300 hover:text-white focus:outline-none focus:ring-2 focus:ring-gold-400"
            >
              View My Submissions
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto p-6 space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-white">Submit a Bundle Version</h1>
        <p className="text-navy-300 mt-1">
          Upload a pre-built WASM component and its manifest. Your app id must fall within your
          own vendor namespace: <code className="text-gold-400">{namespaceHint}</code>.
        </p>
      </div>

      <div
        className="bg-navy-800/60 border border-navy-700 rounded-lg p-4 text-sm text-navy-300"
        data-testid="vendor-submit-source-notice"
      >
        Only pre-built components (<code>.wasm</code>) are accepted for vendor onboarding today
        -- source-tarball uploads (the compile + SAST pipeline) are not yet supported and are
        rejected by the server with a 400 response.
      </div>

      {error && (
        <div
          className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg"
          data-testid="vendor-submit-error"
        >
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-5">
        <div>
          <label htmlFor="appId" className="block text-sm font-medium text-navy-300 mb-1">
            App ID *
          </label>
          <input
            id="appId"
            data-testid="vendor-submit-app-id"
            type="text"
            value={appId}
            onChange={(e) => setAppId(e.target.value)}
            placeholder={`waddles.integrations.vendor-${user?.id ?? '1'}.my-bundle`}
            required
            className="w-full bg-navy-900 border border-navy-600 rounded px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-gold-400"
          />
          <p className="text-xs text-navy-400 mt-1">
            Must exactly match the <code>app_id</code> field inside your manifest.
          </p>
        </div>

        <div>
          <label htmlFor="manifestFile" className="block text-sm font-medium text-navy-300 mb-1">
            Bundle Manifest (YAML) *
          </label>
          <input
            id="manifestFile"
            data-testid="vendor-submit-manifest-file"
            type="file"
            accept=".yaml,.yml"
            onChange={(e) => setManifestFile(e.target.files?.[0] ?? null)}
            aria-label="Bundle manifest YAML file"
            className="w-full text-navy-300 file:mr-3 file:px-3 file:py-2 file:rounded file:border-0 file:bg-navy-700 file:text-white"
          />
        </div>

        <div>
          <label htmlFor="componentFile" className="block text-sm font-medium text-navy-300 mb-1">
            Pre-built Component (.wasm) *
          </label>
          <input
            id="componentFile"
            data-testid="vendor-submit-component-file"
            type="file"
            accept=".wasm"
            onChange={(e) => setComponentFile(e.target.files?.[0] ?? null)}
            aria-label="Pre-built WASM component file"
            className="w-full text-navy-300 file:mr-3 file:px-3 file:py-2 file:rounded file:border-0 file:bg-navy-700 file:text-white"
          />
        </div>

        <button
          type="submit"
          disabled={submitting}
          data-testid="vendor-submit-button"
          className="w-full px-4 py-3 rounded bg-gold-500 text-navy-900 font-semibold hover:bg-gold-400 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-gold-400"
        >
          {submitting ? 'Uploading...' : 'Submit Version'}
        </button>
      </form>
    </div>
  );
}

export default VendorSubmissionForm;
