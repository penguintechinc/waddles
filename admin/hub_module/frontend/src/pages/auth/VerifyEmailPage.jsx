import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import { emailVerificationApi } from '../../services/api';
import ResendVerification from './ResendVerification';

/** Read `#token=...` from the URL fragment (never sent to any server by the browser). */
function tokenFromHash(hash) {
  const params = new URLSearchParams((hash || '').replace(/^#/, ''));
  return params.get('token') || '';
}

/**
 * Landing page for the emailed verification link. Posts the single-use token to hub-api exactly
 * once (guarded against StrictMode's double effect), scrubs it from the address bar, then signs
 * the user in. With no token it shows the "check your inbox / resend" state.
 */
function VerifyEmailPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { refreshUser } = useAuth();
  const [token] = useState(() => tokenFromHash(location.hash));
  const [state, setState] = useState(token ? 'verifying' : 'pending');
  const [error, setError] = useState('');
  const submitted = useRef(false);

  useEffect(() => {
    if (!token || submitted.current) return;
    submitted.current = true;
    if (window.history?.replaceState) {
      window.history.replaceState(null, '', location.pathname);
    }
    emailVerificationApi.verify(token)
      .then(async () => {
        setState('verified');
        try {
          await refreshUser(true);
        } catch {
          // Session refresh failure is non-fatal: the user can still sign in normally.
        }
      })
      .catch((err) => {
        const apiErr = err?.response?.data?.error;
        setError(
          (typeof apiErr === 'string' ? apiErr : apiErr?.message)
            || 'This verification link is invalid or has expired.',
        );
        setState('failed');
      });
  }, [token, location.pathname, refreshUser]);

  return (
    <div className="min-h-[60vh] flex items-center justify-center px-4">
      <div className="card max-w-md w-full p-6">
        <h1 className="text-2xl font-bold text-gold-400">Verify your email</h1>

        {state === 'verifying' && (
          <p className="mt-4 text-navy-300" role="status" data-testid="verify-loading">
            Verifying your email…
          </p>
        )}

        {state === 'verified' && (
          <div data-testid="verify-success">
            <p className="mt-4 p-3 rounded-lg bg-green-500/20 border border-green-500/30 text-green-300" role="status">
              Your email is verified. Welcome to Waddles!
            </p>
            <button
              type="button"
              className="btn btn-primary w-full mt-4"
              onClick={() => navigate('/dashboard')}
            >
              Continue
            </button>
          </div>
        )}

        {state === 'failed' && (
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
