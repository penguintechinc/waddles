import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Alert, Button } from '../../components/ui';
import { publicPrivacyApi } from '../../services/publicPrivacyApi';
import { REGIME_LABEL, REQUEST_TYPE_LABEL, dsarErrorStatus } from '../../services/dsarConstants';
import PublicPrivacyShell from './PublicPrivacyShell';

/** Reads a param from the URL fragment (`#token=..`, `#..`) or query string. */
function readParam(name, search) {
  const frag = window.location.hash.replace(/^#/, '');
  if (frag) {
    const hp = new URLSearchParams(frag);
    if (hp.get(name)) return hp.get(name);
    if (name === 'token' && !frag.includes('=')) return frag;
  }
  return search.get(name);
}

const SLUG_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/;

/**
 * Public email-verification landing page. On load it only PREVIEWS the token (non-consuming) so the
 * person sees WHICH request they are confirming; it never verifies on load: mail scanners prefetch
 * links and the token is single use. Only an explicit click POSTs the verification.
 */
export default function PublicPrivacyVerify() {
  const [search] = useSearchParams();
  const [token] = useState(() => readParam('token', search));
  const [tenant] = useState(() => {
    const t = readParam('tenant', search);
    return t && SLUG_RE.test(t) ? t : null;
  });
  const [state, setState] = useState(token ? 'loading' : 'idle'); // loading | idle | busy | ok | invalid | error
  const [preview, setPreview] = useState(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!token) return undefined;
    let live = true;
    publicPrivacyApi
      .previewVerification(token)
      .then((p) => {
        if (!live) return;
        setPreview(p);
        setState('idle');
      })
      .catch((err) => {
        if (live) setState(dsarErrorStatus(err) === 400 ? 'invalid' : 'error');
      });
    return () => {
      live = false;
    };
  }, [token, attempt]);

  async function confirm() {
    setState('busy');
    try {
      await publicPrivacyApi.verify(token);
      setState('ok');
    } catch (err) {
      setState(dsarErrorStatus(err) === 400 ? 'invalid' : 'error');
    }
  }

  return (
    <PublicPrivacyShell title="Confirm your email">
      {!token && (
        <Alert tone="danger">This link is missing its verification token. Open the link from your email again.</Alert>
      )}
      {token && state === 'loading' && <p role="status">Checking your link...</p>}
      {token && (state === 'idle' || state === 'busy' || (state === 'error' && preview)) && (
        <>
          <p data-testid="verify-preview">
            Confirm that you made this privacy request:{' '}
            <strong>{REQUEST_TYPE_LABEL[preview?.requestType] ?? 'Privacy request'}</strong>
            {REGIME_LABEL[preview?.regime] ? ` (${REGIME_LABEL[preview.regime]})` : ''}. We only start processing it
            after you confirm. If you did not make this request, close this page and nothing will happen.
          </p>
          {state === 'error' && <Alert tone="danger">We could not confirm right now. Please try again.</Alert>}
          <Button onClick={confirm} loading={state === 'busy'} disabled={state === 'busy'}>Confirm your email</Button>
        </>
      )}
      {token && state === 'error' && !preview && (
        <>
          <Alert tone="danger">We could not check your link right now. Please try again.</Alert>
          <Button onClick={() => { setState('loading'); setAttempt((a) => a + 1); }}>Try again</Button>
        </>
      )}
      {state === 'ok' && (
        <Alert tone="success" title="Email confirmed">
          <span data-testid="verify-ok">Thank you. Your request has been verified and will now be processed.</span>
        </Alert>
      )}
      {state === 'invalid' && (
        <div data-testid="verify-invalid" className="space-y-3">
          <Alert tone="danger" title="This link cannot be used">
            It may have expired, or it may already have been used. If you have not confirmed yet, you can request a new link.
          </Alert>
          {tenant ? (
            <Link to={`/privacy-request/${encodeURIComponent(tenant)}`} className="text-gold-400 underline">Request a new verification email</Link>
          ) : (
            <p className="text-sm text-fg-muted">Go back to the privacy request page you used and choose &quot;Resend verification email&quot;.</p>
          )}
        </div>
      )}
    </PublicPrivacyShell>
  );
}
