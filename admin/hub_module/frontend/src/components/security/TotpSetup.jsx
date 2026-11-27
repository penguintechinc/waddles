import { useState } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import { mfaApi } from '../../services/api';
import CodeInput from './CodeInput';
import MfaChangeForm from './MfaChangeForm';
import { mfaError } from './mfaErrors';

/**
 * Authenticator-app enrollment: asks hub-api for a pending secret, renders its
 * otpauth URI as a QR code locally (qrcode.react, no network -- the secret never
 * leaves this browser for a third-party service), then confirms with a first code.
 * When another method is already on (`reauth`), the user first re-authenticates
 * (password when the account has one + a code from an existing factor).
 * Calls `onEnrolled(recoveryCodes)` once the server activates TOTP.
 */
export default function TotpSetup({ onEnrolled, onCancel, reauth = false, hasPassword = true, canEmail = false }) {
  const [enrollment, setEnrollment] = useState(null);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const begin = async (body) => {
    const { data } = await mfaApi.enrollTotp(body);
    setEnrollment({ uri: data.otpauthUri, secret: data.secret });
    console.debug('[TotpSetup] Enrollment started');
  };

  const start = async () => {
    setBusy(true);
    setError('');
    try {
      await begin();
    } catch (err) {
      setError(mfaError(err, 'Could not start authenticator setup.').message);
    } finally {
      setBusy(false);
    }
  };

  if (!enrollment && reauth) {
    return (
      <MfaChangeForm
        title="Confirm it's you to add an authenticator app"
        submitLabel="Continue"
        hasPassword={hasPassword}
        canEmail={canEmail}
        submit={begin}
        onCancel={onCancel}
      />
    );
  }

  const confirm = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const { data } = await mfaApi.confirmTotp(code.trim());
      console.debug('[TotpSetup] Enrollment confirmed');
      onEnrolled(data.recoveryCodes || []);
    } catch (err) {
      setError(mfaError(err, 'That code did not match. Try the newest code.').message);
      setCode('');
    } finally {
      setBusy(false);
    }
  };

  if (!enrollment) {
    return (
      <div className="space-y-3">
        {error && <div role="alert" className="text-sm text-red-300">{error}</div>}
        <button type="button" onClick={start} disabled={busy} className="btn btn-primary text-sm">
          {busy ? 'Preparing...' : 'Set up authenticator app'}
        </button>
      </div>
    );
  }

  return (
    <form onSubmit={confirm} className="p-4 bg-navy-900 rounded-lg border border-navy-700 space-y-4" data-testid="totp-setup">
      <ol className="list-decimal list-inside text-sm text-navy-300 space-y-1">
        <li>Open Google Authenticator (or any authenticator app).</li>
        <li>Scan this QR code, or enter the key manually.</li>
        <li>Type the 6-digit code it shows.</li>
      </ol>
      <div className="flex flex-wrap items-center gap-4">
        <div className="bg-white p-3 rounded-lg">
          <QRCodeSVG value={enrollment.uri} size={176} level="M" title="Authenticator QR code" />
        </div>
        <div className="text-sm">
          <div className="text-navy-400">Setup key</div>
          <code className="font-mono text-sky-100 break-all" data-testid="totp-secret">{enrollment.secret}</code>
        </div>
      </div>
      {error && <div role="alert" className="text-sm text-red-300">{error}</div>}
      <CodeInput id="totp-confirm-code" label="Code from your app" value={code} onChange={setCode} disabled={busy} />
      <div className="flex gap-3">
        <button type="submit" disabled={busy || code.trim().length !== 6} className="btn btn-primary text-sm">
          {busy ? 'Verifying...' : 'Verify and enable'}
        </button>
        <button type="button" onClick={onCancel} className="btn btn-secondary text-sm">Cancel</button>
      </div>
    </form>
  );
}
