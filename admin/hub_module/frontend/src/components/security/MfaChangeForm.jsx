import { useState } from 'react';
import { mfaApi } from '../../services/api';
import CodeInput from './CodeInput';
import { mfaError } from './mfaErrors';

/**
 * Re-authentication form for sensitive MFA changes (disable a method,
 * regenerate recovery codes): current password (when the account has one)
 * plus a valid code from any active factor. `submit({ password, code })` does
 * the call; `canEmail` offers to email an account code.
 */
export default function MfaChangeForm({ title, submitLabel, hasPassword, canEmail, submit, onCancel, danger = false }) {
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  const sendCode = async () => {
    setError('');
    try {
      await mfaApi.sendEmailCode();
      setNotice('We emailed you a 6-digit code.');
    } catch (err) {
      setError(mfaError(err, 'Could not send the code.').message);
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await submit({ password, code: code.trim() });
    } catch (err) {
      setError(mfaError(err, 'Verification failed.').message);
      setCode('');
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="p-4 bg-navy-900 rounded-lg border border-navy-700 space-y-4" data-testid="mfa-change-form">
      <h3 className="font-semibold text-sky-100">{title}</h3>
      {error && <div role="alert" className="text-sm text-red-300">{error}</div>}
      {notice && <div className="text-sm text-emerald-300">{notice}</div>}
      {hasPassword && (
        <div>
          <label htmlFor="mfa-reauth-password" className="block text-sm font-medium text-sky-200 mb-1">
            Current password
          </label>
          <input
            id="mfa-reauth-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full px-3 py-2 bg-navy-900 border border-navy-600 rounded-lg text-sky-100"
          />
        </div>
      )}
      <CodeInput
        id="mfa-reauth-code"
        label="Authenticator, email or recovery code"
        value={code}
        onChange={setCode}
        recovery
        disabled={busy}
      />
      <div className="flex flex-wrap gap-3">
        <button
          type="submit"
          disabled={busy || !code.trim() || (hasPassword && !password)}
          className={`btn text-sm ${danger ? 'btn-danger' : 'btn-primary'}`}
        >
          {busy ? 'Working...' : submitLabel}
        </button>
        {canEmail && (
          <button type="button" onClick={sendCode} className="btn btn-secondary text-sm">Email me a code</button>
        )}
        <button type="button" onClick={onCancel} className="btn btn-secondary text-sm">Cancel</button>
      </div>
    </form>
  );
}
