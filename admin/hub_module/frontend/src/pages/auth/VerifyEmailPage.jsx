import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { emailVerificationApi } from '../../services/api';
import ResendVerification from './ResendVerification';

/** Read `#token=...&action=...` from the URL fragment (never sent to any server by the browser). */
function linkFromHash(hash) {
  const params = new URLSearchParams((hash || '').replace(/^#/, ''));
  return { token: params.get('token') || '', cancel: params.get('action') === 'cancel' };
}

/** Pull a human-readable message out of an API error response. */
function apiMessage(err, fallback) {
  const apiErr = err?.response?.data?.error;
  return (typeof apiErr === 'string' ? apiErr : apiErr?.message) || fallback;
}

/**
 * Landing page for the emailed links. The "confirm" link posts the single-use token once
 * (guarded against StrictMode's double effect), scrubs it from the address bar and asks the
 * user to sign in -- verifying an address never signs anyone in. The "this wasn't me" link
 * (`&action=cancel`) cancels the request and deletes the pending account. With no token it
 * shows the "check your inbox / resend" state.
 */
function VerifyEmailPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const [{ token, cancel }] = useState(() => linkFromHash(location.hash));
  const [state, setState] = useState(token ? 'verifying' : 'pending');
  const [error, setError] = useState('');
  const submitted = useRef(false);

  useEffect(() => {
    if (!token || submitted.current) return;
    submitted.current = true;
    if (window.history?.replaceState) {
      window.history.replaceState(null, '', location.pathname);
    }
    const call = cancel ? emailVerificationApi.cancelSignup : emailVerificationApi.verify;
    call(token)
      .then(() => setState(cancel ? 'cancelled' : 'verified'))
      .catch((err) => {
        setError(apiMessage(err, 'This link is invalid or has expired.'));
        setState('failed');
      });
  }, [token, cancel, location.pathname]);

  return (
    <div className="min-h-[60vh] flex items-center justify-center px-4">
      <div className="card max-w-md w-full p-6">
        <h1 className="text-2xl font-bold text-gold-400">
          {cancel ? 'Cancel sign-up request' : 'Verify your email'}
        </h1>

        {state === 'verifying' && (
          <p className="mt-4 text-navy-300" role="status" data-testid="verify-loading">
            {cancel ? 'Cancelling the request…' : 'Verifying your email…'}
          </p>
        )}

        {state === 'verified' && (
          <div data-testid="verify-success">
            <p className="mt-4 p-3 rounded-lg bg-green-500/20 border border-green-500/30 text-green-300" role="status">
              Your email is verified. Sign in to continue.
            </p>
            <button
              type="button"
              className="btn btn-primary w-full mt-4"
              onClick={() => navigate('/login')}
            >
              Sign in
            </button>
          </div>
        )}

        {state === 'cancelled' && (
          <p
            className="mt-4 p-3 rounded-lg bg-green-500/20 border border-green-500/30 text-green-300"
            role="status"
            data-testid="cancel-success"
          >
            Thanks for letting us know. The request was cancelled and the pending account deleted.
          </p>
        )}

        {state === 'failed' && cancel && (
          <p className="mt-4 p-3 rounded-lg bg-red-500/20 border border-red-500/30 text-red-300" role="alert">
            {error}
          </p>
        )}

        {state === 'failed' && !cancel && (
          <ResendVerification
            title="That link didn't work"
            initialError={error}
            initialEmail={location.state?.email || ''}
          />
        )}

        {state === 'pending' && (
          <ResendVerification initialEmail={location.state?.email || ''} />
        )}

        <p className="mt-6 text-center text-navy-400">
          <Link to="/login" className="text-sky-400 hover:text-sky-300">Back to sign in</Link>
        </p>
      </div>
    </div>
  );
}

export default VerifyEmailPage;
