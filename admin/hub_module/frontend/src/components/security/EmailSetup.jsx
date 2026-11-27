import { useState } from 'react';
import { mfaApi } from '../../services/api';
import CodeInput from './CodeInput';
import { mfaError } from './mfaErrors';

/**
 * Email one-time-code enrollment: sends a code to the account's verified
 * address, then activates the method with it. Calls `onEnrolled(recoveryCodes)`
 * (an empty list when the user already holds recovery codes).
 */
export default function EmailSetup({ onEnrolled, onCancel }) {
  const [sent, setSent] = useState(false);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const send = async () => {
    setBusy(true);
    setError('');
    try {
      await mfaApi.enrollEmail();
      setSent(true);
      console.debug('[EmailSetup] Code sent');
    } catch (err) {
      setError(mfaError(err, 'Could not send the code.').message);
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const { data } = await mfaApi.confirmEmail(code.trim());
      console.debug('[EmailSetup] Enrollment confirmed');
      onEnrolled(data.recoveryCodes || []);
    } catch (err) {
      setError(mfaError(err, 'That code did not match.').message);
      setCode('');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="p-4 bg-navy-900 rounded-lg border border-navy-700 space-y-4" data-testid="email-setup">
      {error && <div role="alert" className="text-sm text-red-300">{error}</div>}
      {!sent ? (
        <div className="flex gap-3">
          <button type="button" onClick={send} disabled={busy} className="btn btn-primary text-sm">
            {busy ? 'Sending...' : 'Email me a code'}
          </button>
          <button type="button" onClick={onCancel} className="btn btn-secondary text-sm">Cancel</button>
        </div>
      ) : (
        <form onSubmit={confirm} className="space-y-4">
          <p className="text-sm text-navy-300">Enter the 6-digit code we emailed you (valid 10 minutes).</p>
          <CodeInput id="email-confirm-code" label="Email code" value={code} onChange={setCode} disabled={busy} />
          <div className="flex gap-3">
            <button type="submit" disabled={busy || code.trim().length !== 6} className="btn btn-primary text-sm">
              {busy ? 'Verifying...' : 'Verify and enable'}
            </button>
            <button type="button" onClick={send} disabled={busy} className="btn btn-secondary text-sm">Resend</button>
          </div>
        </form>
      )}
    </div>
  );
}
