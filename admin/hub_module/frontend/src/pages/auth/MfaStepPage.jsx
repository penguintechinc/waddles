import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import MfaLoginStep from '../../components/security/MfaLoginStep';

/** Methods the MFA step understands, in preference order (authenticator app first). */
const KNOWN_METHODS = ['totp', 'email', 'recovery'];

/**
 * Parse the `#pending=...&methods=...&expiresIn=...` fragment hub-api's OAuth / SSO callback
 * redirects to. Returns `null` when no pending token is present. Unknown methods are dropped and
 * the rest re-ordered so TOTP is always offered first.
 */
export function parseMfaFragment(hash) {
  const params = new URLSearchParams((hash || '').replace(/^#/, ''));
  const token = params.get('pending');
  if (!token) return null;
  const offered = (params.get('methods') || '').split(',');
  const methods = KNOWN_METHODS.filter((m) => offered.includes(m));
  const expiresIn = Number.parseInt(params.get('expiresIn') || '', 10);
  return {
    mfaToken: token,
    methods: methods.length ? methods : ['recovery'],
    expiresIn: Number.isFinite(expiresIn) && expiresIn > 0 ? expiresIn : null,
  };
}

/**
 * Second factor after a federated (OAuth provider or tenant SSO) sign-in. hub-api holds the
 * sign-in at the MFA step and hands this page an opaque single-use pending token in the URL
 * fragment (never the query string, so it stays out of server logs and `Referer`). The page
 * scrubs the fragment from the address bar, reuses the password path's MFA step, and finishes
 * sign-in exactly like the password path (session cookie set by verify, refresh user, dashboard).
 */
export default function MfaStepPage() {
  const navigate = useNavigate();
  const { refreshUser } = useAuth();
  // Read once on mount: the fragment is scrubbed right after (see effect below).
  const challenge = useMemo(
    () => parseMfaFragment(typeof window !== 'undefined' ? window.location.hash : ''),
    [],
  );
  const [expired, setExpired] = useState(false);
  const [finishing, setFinishing] = useState(false);
  const [finishError, setFinishError] = useState(null);

  useEffect(() => {
    // Keep the pending token out of browser history / bookmarks / screenshots of the URL bar.
    if (window.location.hash) {
      window.history.replaceState(window.history.state, '', window.location.pathname);
    }
  }, []);

  useEffect(() => {
    if (!challenge?.expiresIn) return undefined;
    const timer = setTimeout(() => setExpired(true), challenge.expiresIn * 1000);
    return () => clearTimeout(timer);
  }, [challenge]);

  const handleSuccess = async (data) => {
    setFinishing(true);
    setFinishError(null);
    try {
      if (data?.token) {
        await refreshUser(true);
      }
      console.debug('[MfaStepPage] Federated sign-in completed');
      navigate('/dashboard');
    } catch (err) {
      console.warn('[MfaStepPage] Could not load the signed-in user', { status: err?.response?.status });
      setFinishing(false);
      setFinishError('Signed in, but loading your account failed. Please try again.');
    }
  };

  const restart = () => navigate('/login');

  let body;
  if (!challenge) {
    body = (
      <Notice
        testId="mfa-step-missing"
        title="Sign-in link incomplete"
        message="This verification link is missing its sign-in details. Please sign in again."
        onRestart={restart}
      />
    );
  } else if (expired) {
    body = (
      <Notice
        testId="mfa-step-expired"
        title="Verification expired"
        message="This sign-in took too long to verify. Please sign in again."
        onRestart={restart}
      />
    );
  } else if (finishing) {
    body = (
      <div className="text-center" role="status" data-testid="mfa-step-finishing">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-600 mx-auto" />
        <p className="mt-4 text-fg-muted">Completing sign in...</p>
      </div>
    );
  } else {
    body = (
      <>
        {finishError && (
          <div role="alert" className="mb-4 p-3 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 text-sm">
            {finishError}
          </div>
        )}
        <MfaLoginStep challenge={challenge} onSuccess={handleSuccess} onCancel={restart} />
      </>
    );
  }

  return (
    <div className="min-h-[80vh] flex items-center justify-center py-12 px-4">
      <div className="max-w-md w-full">{body}</div>
    </div>
  );
}

/** Terminal state card (missing or expired pending token) with a way back to sign-in. */
function Notice({ testId, title, message, onRestart }) {
  return (
    <div className="bg-navy-800 border border-navy-600 rounded-xl p-6 space-y-4" data-testid={testId}>
      <h2 className="text-xl font-semibold text-gold-400">{title}</h2>
      <p role="alert" className="text-sm text-navy-300">
        {message}
      </p>
      <button type="button" onClick={onRestart} className="btn btn-primary w-full">
        Back to sign in
      </button>
    </div>
  );
}
