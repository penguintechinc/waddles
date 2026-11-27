import { useState } from 'react';
import { mfaApi } from '../../services/api';
import CodeInput from './CodeInput';
import { CHALLENGE_INVALID, METHOD_LABELS, mfaError } from './mfaErrors';

/**
 * Second step of a password login when the account has MFA enrolled. Receives
 * the 202 `{ mfaToken, methods }` answer, lets the user pick a method (the first
 * one -- the authenticator app when enrolled -- is preselected), and completes
 * the login via `POST /auth/mfa/login/verify`, which sets the session cookie.
 */
export default function MfaLoginStep({ challenge, onSuccess, onCancel }) {
  const methods = challenge.methods?.length ? challenge.methods : ['recovery'];
  const [method, setMethod] = useState(methods[0]);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState(false);
  const [emailSent, setEmailSent] = useState(false);
  const [error, setError] = useState(null);
  const [expired, setExpired] = useState(false);

  const choose = (next) => {
    setMethod(next);
    setCode('');
    setError(null);
  };

  const handleSendEmail = async () => {
    setSending(true);
    setError(null);
    try {
      await mfaApi.loginSendEmail(challenge.mfaToken);
      setEmailSent(true);
      console.debug('[MfaLoginStep] Email code requested');
    } catch (err) {
      const { code: errCode, message } = mfaError(err, 'Could not send the code.');
      setExpired(errCode === CHALLENGE_INVALID);
      setError(message);
    } finally {
      setSending(false);
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { data } = await mfaApi.loginVerify({ mfaToken: challenge.mfaToken, method, code: code.trim() });
      console.debug('[MfaLoginStep] Verified', { method });
      await onSuccess(data);
    } catch (err) {
      const { code: errCode, message } = mfaError(err, 'Verification failed.');
      console.warn('[MfaLoginStep] Verification failed', { method, errCode });
      setExpired(errCode === CHALLENGE_INVALID);
      setError(message);
      setCode('');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-navy-800 border border-navy-600 rounded-xl p-6 space-y-4" data-testid="mfa-login-step">
      <div>
        <h2 className="text-xl font-semibold text-gold-400">Two-step verification</h2>
        <p className="text-sm text-navy-300 mt-1">Confirm it&apos;s you to finish signing in.</p>
      </div>

      {methods.length > 1 && (
        <div role="tablist" aria-label="Verification method" className="flex gap-2">
          {methods.map((m) => (
            <button
              key={m}
              type="button"
              role="tab"
              aria-selected={method === m}
              onClick={() => choose(m)}
              className={`px-3 py-1.5 rounded-lg text-sm border ${
                method === m ? 'border-gold-500 text-gold-400 bg-navy-900' : 'border-navy-600 text-sky-200'
              }`}
            >
              {METHOD_LABELS[m] || m}
            </button>
          ))}
        </div>
      )}

      {error && (
        <div role="alert" className="p-3 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 text-sm">
          {error}
        </div>
      )}

      {expired ? (
        <button type="button" onClick={onCancel} className="btn btn-primary w-full">
          Start over
        </button>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-4">
          {method === 'totp' && (
            <p className="text-sm text-navy-300">Enter the 6-digit code from your authenticator app.</p>
          )}
          {method === 'email' && (
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm text-navy-300">
                {emailSent ? 'We emailed you a 6-digit code (valid 10 minutes).' : 'We will email a 6-digit code to you.'}
              </p>
              <button type="button" onClick={handleSendEmail} disabled={sending} className="btn btn-secondary text-sm">
                {sending ? 'Sending...' : emailSent ? 'Resend' : 'Send code'}
              </button>
            </div>
          )}
          {method === 'recovery' && (
            <p className="text-sm text-navy-300">Enter one of your saved recovery codes. Each works once.</p>
          )}
          <CodeInput
            id="mfa-login-code"
            label={METHOD_LABELS[method] || 'Code'}
            value={code}
            onChange={setCode}
            recovery={method === 'recovery'}
            disabled={busy}
          />
          <div className="flex gap-3">
            <button type="submit" disabled={busy || !code.trim()} className="btn btn-primary flex-1">
              {busy ? 'Verifying...' : 'Verify'}
            </button>
            <button type="button" onClick={onCancel} className="btn btn-secondary">
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
