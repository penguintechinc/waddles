import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { tenantLicenseApi, normalizeLicenseKey } from '../../services/tenantLicenseApi';

const CONFIRM_WORD = 'REMOVE';

/** Maps an axios-style failure to a page state; never touches request bodies (they hold the key). */
function classify(err) {
  const status = err?.response?.status;
  const error = err?.response?.data?.error;
  const message = typeof error === 'string' ? error : error?.message;
  if (status === 403) return { kind: 'forbidden' };
  return { kind: 'error', message: message || 'Something went wrong. Please try again.' };
}

/**
 * Tenant-admin page for the tenant's license key. The key is write-only: the server only ever
 * returns whether one is configured plus a fingerprint, so the key lives in an uncontrolled-style
 * local field that is cleared after every submit (success or failure) and is never logged.
 */
function TenantLicense() {
  const { tenantSlug } = useParams();
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [keyInput, setKeyInput] = useState('');
  const [fieldError, setFieldError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [confirmText, setConfirmText] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const mine = ++seq.current;
    setLoading(true);
    setLoadError(null);
    try {
      const s = await tenantLicenseApi.get(tenantSlug);
      if (mine === seq.current) setStatus(s);
    } catch (err) {
      if (mine === seq.current) setLoadError(classify(err));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, [tenantSlug]);

  useEffect(() => {
    load();
  }, [load]);

  const onSubmit = async (e) => {
    e.preventDefault();
    setNotice(null);
    const key = normalizeLicenseKey(keyInput);
    if (!key) {
      setKeyInput('');
      setFieldError('The license key must be 16-256 characters of letters, digits and . _ : - (no spaces).');
      return;
    }
    setFieldError(null);
    // Clear the field before the request so no copy of the key outlives the call in component state.
    setKeyInput('');
    setSaving(true);
    try {
      const s = await tenantLicenseApi.set(tenantSlug, key);
      setStatus(s);
      setNotice({ type: 'success', text: 'License key saved.' });
    } catch (err) {
      const c = classify(err);
      setNotice({
        type: 'error',
        text: c.kind === 'forbidden' ? 'You do not have permission to change the license key.' : c.message,
      });
    } finally {
      setSaving(false);
    }
  };

  const onRemove = async () => {
    if (confirmText !== CONFIRM_WORD) return;
    setRemoving(true);
    setNotice(null);
    try {
      await tenantLicenseApi.remove(tenantSlug);
      setStatus({ configured: false, fingerprint: null, setAt: null });
      setNotice({ type: 'success', text: 'License key removed.' });
      setConfirmOpen(false);
      setConfirmText('');
    } catch (err) {
      const c = classify(err);
      setNotice({
        type: 'error',
        text: c.kind === 'forbidden' ? 'You do not have permission to remove the license key.' : c.message,
      });
    } finally {
      setRemoving(false);
    }
  };

  if (loading) {
    return (
      <div className="p-8 text-center" role="status" aria-label="Loading license">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gold-400 mx-auto" />
      </div>
    );
  }

  if (loadError?.kind === 'forbidden') {
    return (
      <div role="alert" data-testid="license-forbidden" className="p-6 text-red-300">
        You do not have permission to manage this tenant&apos;s license.
      </div>
    );
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold text-sky-100 mb-2">License</h1>
      <p className="text-sm text-navy-300 mb-6">
        Your license key unlocks the pool of Premium communities (one pool size per tenant). The key is
        write-only: once saved it is never shown again, only a short fingerprint.
      </p>

      {loadError && (
        <div role="alert" data-testid="license-error" className="p-4 mb-6 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300">
          {loadError.message}{' '}
          <button type="button" className="underline" onClick={load}>Retry</button>
        </div>
      )}

      {notice && (
        <div
          role={notice.type === 'error' ? 'alert' : 'status'}
          data-testid="license-notice"
          className={`p-3 mb-6 rounded-lg border text-sm ${notice.type === 'error' ? 'bg-red-500/20 border-red-500/30 text-red-300' : 'bg-green-500/20 border-green-500/30 text-green-300'}`}
        >
          {notice.text}
        </div>
      )}

      {!loadError && (
        <section className="card p-4 mb-6" aria-label="License status" data-testid="license-status">
          {status?.configured ? (
            <p className="text-sky-100">
              License key configured. Fingerprint{' '}
              <code className="font-mono" data-testid="license-fingerprint">{status.fingerprint}</code>
              {status.setAt ? <>, set {new Date(status.setAt).toLocaleString()}</> : null}.
            </p>
          ) : (
            <p className="text-sky-100" data-testid="license-not-configured">
              No license key: Free tier limits apply; team pools unavailable.
            </p>
          )}
        </section>
      )}

      <p className="text-sm text-navy-300 mb-6" data-testid="license-pending-note">
        Note: team-pool support in the license server is still pending. A configured key may still show
        &quot;pool unavailable&quot; on the Community tiers page until the license server returns team pool sizes.
      </p>

      <form onSubmit={onSubmit} className="card p-4 mb-6" aria-label="Set license key">
        <label htmlFor="license-key" className="block text-sm font-medium text-sky-100 mb-1">
          {status?.configured ? 'Replace license key' : 'License key'}
        </label>
        <input
          id="license-key"
          name="license-key"
          type="password"
          autoComplete="off"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          value={keyInput}
          onChange={(e) => setKeyInput(e.target.value)}
          className="input w-full font-mono"
          aria-describedby={fieldError ? 'license-key-error' : undefined}
          aria-invalid={fieldError ? 'true' : undefined}
          disabled={saving}
        />
        {fieldError && (
          <p id="license-key-error" role="alert" className="text-sm text-red-300 mt-2">{fieldError}</p>
        )}
        <div className="mt-4">
          <button type="submit" className="btn btn-primary" disabled={saving || keyInput.trim() === ''}>
            {saving ? 'Saving...' : status?.configured ? 'Replace key' : 'Save key'}
          </button>
        </div>
      </form>

      {status?.configured && (
        <section className="card p-4" aria-label="Remove license key">
          {!confirmOpen ? (
            <button type="button" className="btn btn-danger" onClick={() => setConfirmOpen(true)}>
              Remove license key
            </button>
          ) : (
            <div>
              <p className="text-sm text-sky-100 mb-2">
                Removing the key returns this tenant to Free tier limits. Type{' '}
                <strong className="text-red-400">{CONFIRM_WORD}</strong> to confirm.
              </p>
              <input
                type="text"
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                className="input w-full font-mono mb-3"
                aria-label="Type REMOVE to confirm"
                autoComplete="off"
              />
              <div className="flex gap-3">
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => {
                    setConfirmOpen(false);
                    setConfirmText('');
                  }}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn btn-danger disabled:opacity-50"
                  disabled={confirmText !== CONFIRM_WORD || removing}
                  onClick={onRemove}
                >
                  {removing ? 'Removing...' : 'Confirm removal'}
                </button>
              </div>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

export default TenantLicense;
