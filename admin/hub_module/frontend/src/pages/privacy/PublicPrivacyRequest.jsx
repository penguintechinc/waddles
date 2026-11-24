import { useState } from 'react';
import { useParams } from 'react-router-dom';
import Captcha from '../../components/Captcha';
import { Alert, Button, SelectField, TextareaField, TextField } from '../../components/ui';
import { publicPrivacyApi } from '../../services/publicPrivacyApi';
import { REGIME_LABEL, REQUEST_TYPE_LABEL } from '../../services/dsarConstants';
import ResendForm from './ResendForm';
import { publicError } from './publicErrors';
import PublicPrivacyShell from './PublicPrivacyShell';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/**
 * Public privacy request form (/privacy-request/:tenantSlug): no login. Needs a captcha token to
 * submit (fail closed) and always shows the same acceptance message on a 202.
 */
export default function PublicPrivacyRequest() {
  const { tenantSlug } = useParams();
  const [email, setEmail] = useState('');
  const [requestType, setRequestType] = useState('1');
  const [regime, setRegime] = useState('1');
  const [message, setMessage] = useState('');
  const [token, setToken] = useState(null);
  const [resetKey, setResetKey] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [fieldError, setFieldError] = useState(null);
  const [accepted, setAccepted] = useState(false);
  const [showResend, setShowResend] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError(null);
    if (!EMAIL_RE.test(email.trim()) || email.length > 254) {
      setFieldError('Enter a valid email address.');
      return;
    }
    setFieldError(null);
    if (!token) return;
    setBusy(true);
    try {
      const body = { email: email.trim(), requestType: Number(requestType), regime: Number(regime), captchaToken: token };
      if (message.trim()) body.message = message.trim();
      await publicPrivacyApi.submit(tenantSlug, body);
      setAccepted(true);
    } catch (err) {
      setError(publicError(err));
    }
    setToken(null);
    setResetKey((k) => k + 1);
    setBusy(false);
  }

  return (
    <PublicPrivacyShell title="Submit a privacy request">
      {accepted ? (
        <Alert tone="success" title="Request received">
          <span data-testid="public-accepted">
            Thank you. If the details are valid, we will email you a verification link. Your request is not processed
            until you confirm your email address from that message.
          </span>
        </Alert>
      ) : (
        <form onSubmit={submit} noValidate className="space-y-4">
          <p className="text-sm text-fg-muted">
            Ask to access, erase, correct or stop the sale of your personal information. We email a link to confirm
            it is really you before we act.
          </p>
          <TextField label="Email address" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} error={fieldError || undefined} />
          <SelectField label="Request type" value={requestType} onChange={(e) => setRequestType(e.target.value)}>
            {Object.entries(REQUEST_TYPE_LABEL).map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </SelectField>
          <SelectField label="Privacy law that applies to you" value={regime} onChange={(e) => setRegime(e.target.value)}>
            {Object.entries(REGIME_LABEL).map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </SelectField>
          <TextareaField label="Message (optional)" maxLength={2000} value={message} onChange={(e) => setMessage(e.target.value)} />
          <Captcha onToken={setToken} resetKey={resetKey} />
          {error && <div data-testid="public-error"><Alert tone="danger">{error}</Alert></div>}
          <Button type="submit" loading={busy} disabled={!token || busy}>Submit request</Button>
        </form>
      )}
      <section aria-labelledby="resend-title" className="border-t border-line pt-4">
        <h2 id="resend-title" className="text-base font-semibold">Did not get the verification email?</h2>
        {showResend ? (
          <div className="mt-3"><ResendForm tenantSlug={tenantSlug} defaultType={requestType} /></div>
        ) : (
          <button type="button" onClick={() => setShowResend(true)} className="mt-2 text-gold-400 underline focus:outline-none focus:ring-2 focus:ring-sky-500 rounded">
            Resend verification email
          </button>
        )}
      </section>
    </PublicPrivacyShell>
  );
}
