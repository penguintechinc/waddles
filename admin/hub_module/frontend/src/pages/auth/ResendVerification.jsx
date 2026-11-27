import { useState } from 'react';
import { emailVerificationApi } from '../../services/api';

/**
 * "Check your inbox" panel with a resend button. Shown after signup, after a failed send, and on
 * the verify page when a link is bad. hub-api answers resend uniformly (no account enumeration),
 * rate-limits it (429 + retryAfterSeconds) and fails loud when email is not configured (503).
 */
function ResendVerification({ initialEmail = '', initialError = '', title = 'Check your inbox' }) {
  const [email, setEmail] = useState(initialEmail);
  const [status, setStatus] = useState('idle'); // idle | sending | sent
  const [error, setError] = useState(initialError);

  const handleResend = async (event) => {
    event.preventDefault();
    setError('');
    setStatus('sending');
    try {
      await emailVerificationApi.resend(email);
      setStatus('sent');
    } catch (err) {
      const apiErr = err?.response?.data?.error;
      const wait = apiErr?.details?.retryAfterSeconds;
      if (err?.response?.status === 429 && wait) {
        setError(`Please wait ${wait} seconds before requesting another email.`);
      } else {
        setError(
          (typeof apiErr === 'string' ? apiErr : apiErr?.message)
            || "We couldn't send the verification email. Please try again.",
        );
      }
      setStatus('idle');
    }
  };

  return (
    <div className="mt-4" data-testid="verification-pending">
      <h2 className="text-xl font-semibold text-gold-400">{title}</h2>
      <p className="mt-2 text-navy-300">
        We sent a verification link to your email address. Open it to finish setting up your
        account. You can&apos;t sign in until your email is verified.
      </p>
      {error && (
        <div className="mt-3 p-3 rounded-lg bg-red-500/20 border border-red-500/30 text-red-300" role="alert">
          {error}
        </div>
      )}
      {status === 'sent' && (
        <div className="mt-3 p-3 rounded-lg bg-green-500/20 border border-green-500/30 text-green-300" role="status">
          If this address has an account waiting for verification, a new link is on its way.
        </div>
      )}
      <form onSubmit={handleResend} className="mt-4 space-y-3">
        <label className="block text-sky-200">
          Email
          <input
            className="input mt-1 w-full"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
            data-testid="resend-email-input"
          />
        </label>
        <button
          type="submit"
          disabled={status === 'sending' || !email}
          className="btn btn-secondary w-full disabled:opacity-50"
          data-testid="resend-verification"
        >
          {status === 'sending' ? 'Sending…' : 'Resend verification email'}
        </button>
      </form>
    </div>
  );
}

export default ResendVerification;
