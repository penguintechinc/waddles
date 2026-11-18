import { useState, type FormEvent } from 'react';
import Captcha from '../../components/Captcha';
import { Alert, Button, SelectField, TextField } from '../../components/ui';
import { publicPrivacyApi } from '../../services/publicPrivacyApi';
import { REQUEST_TYPE_LABEL } from '../../services/dsarConstants';
import { publicError } from './publicErrors';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

interface ResendFormProps {
  tenantSlug: string;
  defaultType: string;
}

/** Re-send form with its own captcha; uniform response, never confirms a request exists. */
export default function ResendForm({ tenantSlug, defaultType }: ResendFormProps) {
  const [email, setEmail] = useState('');
  const [reqType, setReqType] = useState(defaultType);
  const [token, setToken] = useState<string | null>(null);
  const [resetKey, setResetKey] = useState(0);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!token || !EMAIL_RE.test(email)) {
      setError('Enter a valid email address and complete the security check.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await publicPrivacyApi.resendVerification(tenantSlug, { email: email.trim(), requestType: Number(reqType), captchaToken: token });
      setDone(true);
    } catch (err) {
      setError(publicError(err));
    }
    setToken(null);
    setResetKey((k) => k + 1);
    setBusy(false);
  }

  if (done) {
    return (
      <p role="status" data-testid="resend-done">
        If a request of that type is awaiting verification for that address, a new verification email has been sent. The previous link no longer works.
      </p>
    );
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-4" aria-label="Resend verification email">
      <TextField label="Email address" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
      <SelectField label="Type of request to re-send" value={reqType} onChange={(e) => setReqType(e.target.value)}>
        {Object.entries(REQUEST_TYPE_LABEL).map(([v, l]) => (
          <option key={v} value={v}>{l}</option>
        ))}
      </SelectField>
      <Captcha onToken={setToken} resetKey={resetKey} label="Security check for resend" />
      {error && <Alert tone="danger">{error}</Alert>}
      <Button type="submit" loading={busy} disabled={!token || busy}>Resend verification email</Button>
    </form>
  );
}
