import { useCallback, useEffect, useState } from 'react';
import { DevicePhoneMobileIcon, EnvelopeIcon, LockClosedIcon } from '@heroicons/react/24/outline';
import { mfaApi } from '../../services/api';
import EmailSetup from './EmailSetup';
import MfaChangeForm from './MfaChangeForm';
import RecoveryCodesPanel from './RecoveryCodesPanel';
import TotpSetup from './TotpSetup';
import { mfaError } from './mfaErrors';

const ACTIVE = 2;

/**
 * "Two-step verification" card on Account Settings. The authenticator app
 * (TOTP) is listed first and marked Recommended; email codes are the second
 * option and show as unavailable when the server has no SMTP transport or the
 * address is unverified. Handles loading, error, enroll, disable and
 * recovery-code flows; fresh recovery codes are shown exactly once.
 */
export default function MfaSettings() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [panel, setPanel] = useState(null); // 'totp' | 'email' | 'disable-totp' | 'disable-email' | 'regen'
  const [codes, setCodes] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const { data } = await mfaApi.status();
      setStatus(data);
    } catch (err) {
      setError(mfaError(err, 'Could not load two-step verification settings.').message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const enrolled = async (recoveryCodes) => {
    setPanel(null);
    if (recoveryCodes.length) setCodes(recoveryCodes);
    await load();
  };

  const finished = async () => {
    setPanel(null);
    await load();
  };

  if (loading && !status) {
    return <div className="text-sm text-navy-400" role="status">Loading two-step verification...</div>;
  }
  if (error && !status) {
    return (
      <div role="alert" className="p-3 bg-red-500/20 border border-red-500/30 rounded-lg text-red-300 text-sm flex justify-between">
        <span>{error}</span>
        <button type="button" onClick={load} className="underline">Retry</button>
      </div>
    );
  }

  const totpActive = status.totp.status === ACTIVE;
  const emailActive = status.email.status === ACTIVE;
  const emailBlocked = !status.email.available
    ? 'Unavailable: email delivery is not configured on this server.'
    : !status.emailVerified
      ? 'Verify your email address first.'
      : '';

  return (
    <div className="space-y-4" data-testid="mfa-settings">
      {codes && <RecoveryCodesPanel codes={codes} onDone={() => setCodes(null)} />}
      {!status.enabled && (
        <div className="p-3 bg-amber-500/10 border border-amber-500/30 rounded-lg text-amber-300 text-sm">
          Two-step verification is turned off on this server.
        </div>
      )}

      <section className="p-4 bg-navy-800 rounded-lg border border-navy-700 space-y-3" data-testid="mfa-method-totp">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <DevicePhoneMobileIcon className="w-5 h-5 text-gold-400" />
            <div>
              <div className="font-medium text-sky-100 flex items-center gap-2">
                Authenticator app
                <span className="text-xs px-2 py-0.5 rounded-full bg-gold-500/20 text-gold-400 border border-gold-500/40">Recommended</span>
              </div>
              <div className="text-sm text-navy-400">Google Authenticator or any TOTP app. Works offline.</div>
            </div>
          </div>
          {totpActive ? (
            <button type="button" onClick={() => setPanel('disable-totp')} className="btn btn-secondary text-sm">Turn off</button>
          ) : null}
        </div>
        {totpActive && <div className="text-sm text-emerald-400">On</div>}
        {!totpActive && status.enabled && panel !== 'totp' && (
          <button type="button" onClick={() => setPanel('totp')} className="btn btn-primary text-sm">Set up</button>
        )}
        {panel === 'totp' && <TotpSetup onEnrolled={enrolled} onCancel={() => setPanel(null)} />}
        {panel === 'disable-totp' && (
          <MfaChangeForm
            title="Turn off authenticator app"
            submitLabel="Turn off"
            danger
            hasPassword={status.hasPassword}
            canEmail={emailActive}
            submit={async (body) => { await mfaApi.disable({ method: 'totp', ...body }); await finished(); }}
            onCancel={() => setPanel(null)}
          />
        )}
      </section>

      <section className="p-4 bg-navy-800 rounded-lg border border-navy-700 space-y-3" data-testid="mfa-method-email">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <EnvelopeIcon className="w-5 h-5 text-navy-300" />
            <div>
              <div className="font-medium text-sky-100">Email code</div>
              <div className="text-sm text-navy-400">A 6-digit code sent to your verified email address.</div>
            </div>
          </div>
          {emailActive && (
            <button type="button" onClick={() => setPanel('disable-email')} className="btn btn-secondary text-sm">Turn off</button>
          )}
        </div>
        {emailActive && <div className="text-sm text-emerald-400">On</div>}
        {!emailActive && emailBlocked && <div className="text-sm text-navy-400" data-testid="email-unavailable">{emailBlocked}</div>}
        {!emailActive && !emailBlocked && status.enabled && panel !== 'email' && (
          <button type="button" onClick={() => setPanel('email')} className="btn btn-secondary text-sm">Set up</button>
        )}
        {panel === 'email' && <EmailSetup onEnrolled={enrolled} onCancel={() => setPanel(null)} />}
        {panel === 'disable-email' && (
          <MfaChangeForm
            title="Turn off email codes"
            submitLabel="Turn off"
            danger
            hasPassword={status.hasPassword}
            canEmail
            submit={async (body) => { await mfaApi.disable({ method: 'email', ...body }); await finished(); }}
            onCancel={() => setPanel(null)}
          />
        )}
      </section>

      {(totpActive || emailActive) && (
        <section className="p-4 bg-navy-800 rounded-lg border border-navy-700 space-y-3" data-testid="mfa-recovery">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <LockClosedIcon className="w-5 h-5 text-navy-300" />
              <div>
                <div className="font-medium text-sky-100">Recovery codes</div>
                <div className="text-sm text-navy-400">{status.recoveryCodesRemaining} unused</div>
              </div>
            </div>
            <button type="button" onClick={() => setPanel('regen')} className="btn btn-secondary text-sm">Generate new codes</button>
          </div>
          {panel === 'regen' && (
            <MfaChangeForm
              title="Generate new recovery codes"
              submitLabel="Generate"
              hasPassword={status.hasPassword}
              canEmail={emailActive}
              submit={async (body) => {
                const { data } = await mfaApi.regenerateRecoveryCodes(body);
                setCodes(data.recoveryCodes);
                await finished();
              }}
              onCancel={() => setPanel(null)}
            />
          )}
        </section>
      )}
    </div>
  );
}
