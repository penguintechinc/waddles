/**
 * Privacy Rights Page
 *
 * One place for the data subject rights that were previously either
 * unreachable or unimplemented: access and portability (GDPR Art. 15/20),
 * erasure (Art. 17 — the endpoint existed with no UI), and the CCPA/CPRA
 * opt-out from the sale or sharing of personal information.
 *
 * Rectification (Art. 16) is not duplicated here; it lives on the profile
 * editor, and a second place to change the same fields would drift from it.
 */
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeftIcon } from '@heroicons/react/24/outline';

import api from '../services/api';
import { useCookieConsent } from '../hooks/useCookieConsent';
import { userPrivacyRequestsApi } from '../services/userPrivacyRequestsApi';
import {
  REGIME_LABEL,
  REQUEST_TYPE_LABEL,
  STATUS_LABEL,
  dsarErrorCode,
  dsarErrorMessage,
} from '../services/dsarConstants';

const formatDate = (iso) => (iso ? new Date(iso).toLocaleDateString() : 'Not set');

/**
 * "Submit a privacy request" section: files a formal request through the tenant's request inbox
 * and lists the caller's own requests with status and due date.
 */
function PrivacyRequestsSection() {
  const [requestType, setRequestType] = useState('1');
  const [regime, setRegime] = useState('1');
  const [items, setItems] = useState(null);
  const [loadState, setLoadState] = useState('loading'); // loading | ok | disabled | error
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState(null);
  const [problem, setProblem] = useState(null);

  const load = useCallback(async () => {
    try {
      setItems(await userPrivacyRequestsApi.list());
      setLoadState('ok');
    } catch (err) {
      setLoadState(dsarErrorCode(err) === 'DSAR_INBOX_DISABLED' ? 'disabled' : 'error');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const submit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setMessage(null);
    setProblem(null);
    try {
      await userPrivacyRequestsApi.create({ requestType: Number(requestType), regime: Number(regime) });
      setMessage('Your request has been submitted.');
      await load();
    } catch (err) {
      setProblem(dsarErrorMessage(err, 'Your request could not be submitted. Please try again.'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="mt-6 rounded-lg border border-line bg-surface-raised p-5" aria-labelledby="privacy-requests-title">
      <h2 id="privacy-requests-title" className="text-xl font-semibold text-gold-400">Submit a privacy request</h2>
      <p className="mt-1 text-sm text-fg-muted">
        File a formal request and track it here. We respond within the legal deadline for your region.
      </p>
      {loadState === 'disabled' ? (
        <p className="mt-4 text-sm" data-testid="requests-disabled">Formal privacy requests are not enabled for your community yet.</p>
      ) : (
        <>
          <form onSubmit={submit} className="mt-4 grid gap-3 sm:grid-cols-2">
            <label className="block text-sm text-fg-secondary">
              Request type
              <select value={requestType} onChange={(e) => setRequestType(e.target.value)} className="mt-1 w-full rounded border border-line-strong bg-surface px-3 py-2 text-fg">
                {Object.entries(REQUEST_TYPE_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <label className="block text-sm text-fg-secondary">
              Privacy law
              <select value={regime} onChange={(e) => setRegime(e.target.value)} className="mt-1 w-full rounded border border-line-strong bg-surface px-3 py-2 text-fg">
                {Object.entries(REGIME_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <div className="sm:col-span-2">
              <button type="submit" disabled={submitting} data-testid="submit-privacy-request" className="rounded bg-sky-600 px-4 py-2 font-medium text-white hover:bg-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-400 disabled:opacity-50">
                {submitting ? 'Submitting…' : 'Submit request'}
              </button>
            </div>
          </form>
          {message && <p role="status" className="mt-3 text-sm text-emerald-300">{message}</p>}
          {problem && <p role="alert" className="mt-3 text-sm text-red-300">{problem}</p>}
          <h3 className="mt-5 font-medium">Your requests</h3>
          {loadState === 'loading' && <p role="status" className="text-sm text-fg-muted">Loading…</p>}
          {loadState === 'error' && <p role="alert" className="text-sm text-red-300">Your requests could not be loaded.</p>}
          {loadState === 'ok' && items.length === 0 && <p className="text-sm text-fg-muted" data-testid="requests-empty">You have not submitted any requests.</p>}
          {loadState === 'ok' && items.length > 0 && (
            <ul className="mt-2 space-y-2" data-testid="requests-list">
              {items.map((r) => (
                <li key={r.id} className="rounded border border-line p-3 text-sm">
                  <span className="font-medium">{REQUEST_TYPE_LABEL[r.requestType] ?? 'Request'}</span>
                  {' · '}{STATUS_LABEL[r.status] ?? 'Unknown'}
                  {' · '}Submitted {formatDate(r.requestedAt)}
                  {' · '}Due {formatDate(r.dueAt)}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

export default function PrivacyRights() {
  const { consent, setDoNotSell, loading: consentLoading } = useCookieConsent();
  const [busy, setBusy] = useState(null);
  const [notice, setNotice] = useState(null);
  const [problem, setProblem] = useState(null);
  const [confirmPassword, setConfirmPassword] = useState('');
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const optedOut = Boolean(consent?.do_not_sell);

  /** Download the export as a file rather than rendering it, so it is portable. */
  const handleExport = async () => {
    setBusy('export');
    setProblem(null);
    setNotice(null);
    try {
      const response = await api.get('/api/v1/user/me/data');
      const blob = new Blob([JSON.stringify(response.data, null, 2)], {
        type: 'application/json',
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `waddles-my-data-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);

      // A partial export is still delivered, but the subject is told it is partial
      // rather than being left to assume it is everything held about them.
      setNotice(
        response.data?.incomplete
          ? 'Your data was downloaded, but some sections could not be read. Please contact support.'
          : 'Your data has been downloaded.',
      );
    } catch (err) {
      console.error('[PrivacyRights] Export failed', { reason: err.message });
      setProblem('Your data could not be exported. Please try again.');
    } finally {
      setBusy(null);
    }
  };

  const handleOptOut = async (next) => {
    setBusy('optout');
    setProblem(null);
    setNotice(null);
    try {
      await setDoNotSell(next);
      setNotice(
        next
          ? 'You have opted out of the sale or sharing of your personal information.'
          : 'Sharing preferences updated.',
      );
    } catch (err) {
      console.error('[PrivacyRights] Opt-out failed', { reason: err.message });
      setProblem('Your preference could not be saved. Please try again.');
    } finally {
      setBusy(null);
    }
  };

  const handleDelete = async () => {
    setBusy('delete');
    setProblem(null);
    setNotice(null);
    try {
      await api.delete('/api/v1/user/me/data', {
        data: confirmPassword ? { password: confirmPassword } : {},
      });
      setNotice('Your personal data has been deleted.');
      setConfirmingDelete(false);
      setConfirmPassword('');
    } catch (err) {
      console.error('[PrivacyRights] Deletion failed', { reason: err.message });
      setProblem(
        err.response?.status === 401
          ? 'Password confirmation failed.'
          : 'Your data could not be deleted. Please try again.',
      );
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="min-h-screen bg-surface text-fg">
      <div className="mx-auto max-w-3xl px-4 py-10">
        <Link
          to="/"
          className="mb-6 inline-flex items-center gap-2 text-gold-400 hover:text-gold-300 focus:outline-none focus:ring-2 focus:ring-sky-500 rounded"
        >
          <ArrowLeftIcon className="h-4 w-4" aria-hidden="true" />
          Back
        </Link>

        <h1 className="text-3xl font-bold text-gold-400">Your privacy rights</h1>
        <p className="mt-2 text-fg-muted">
          Access, download, correct or delete the personal information we hold about you, and
          control whether it is shared.
        </p>

        {notice && (
          <p role="status" className="mt-6 rounded border border-emerald-700 bg-emerald-950 p-3 text-emerald-300">
            {notice}
          </p>
        )}
        {problem && (
          <p role="alert" className="mt-6 rounded border border-red-700 bg-red-950 p-3 text-red-300">
            {problem}
          </p>
        )}

        <section className="mt-8 rounded-lg border border-line bg-surface-raised p-5">
          <h2 className="text-xl font-semibold text-gold-400">Get a copy of your data</h2>
          <p className="mt-1 text-sm text-fg-muted">
            Downloads everything we hold about you as a JSON file you can keep or move to
            another service. Passwords, session tokens and security keys are never included.
          </p>
          <button
            type="button"
            onClick={handleExport}
            disabled={busy === 'export'}
            data-testid="export-data"
            className="mt-4 rounded bg-sky-600 px-4 py-2 font-medium text-white hover:bg-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-400 disabled:opacity-50"
          >
            {busy === 'export' ? 'Preparing…' : 'Download my data'}
          </button>
        </section>

        <section className="mt-6 rounded-lg border border-line bg-surface-raised p-5">
          <h2 className="text-xl font-semibold text-gold-400">
            Do Not Sell or Share My Personal Information
          </h2>
          <p className="mt-1 text-sm text-fg-muted">
            Opting out stops your personal information being sold or shared, and turns off
            marketing cookies. If your browser sends a Global Privacy Control signal we honour
            it automatically.
          </p>
          <button
            type="button"
            onClick={() => handleOptOut(!optedOut)}
            disabled={busy === 'optout' || consentLoading}
            aria-pressed={optedOut}
            data-testid="toggle-do-not-sell"
            className="mt-4 rounded border border-gold-500 px-4 py-2 font-medium text-gold-400 hover:bg-navy-700 focus:outline-none focus:ring-2 focus:ring-sky-400 disabled:opacity-50"
          >
            {optedOut ? 'You are opted out — allow sharing again' : 'Opt out of sale or sharing'}
          </button>
        </section>

        <PrivacyRequestsSection />

        <section className="mt-6 rounded-lg border border-red-800 bg-surface-raised p-5">
          <h2 className="text-xl font-semibold text-red-400">Delete my data</h2>
          <p className="mt-1 text-sm text-fg-muted">
            Permanently removes your profile, activity, sessions, community memberships, linked
            platform accounts and reputation. The one exception: if you are under an active ban
            in a community, a one-way coded record of that ban (never your platform ID) is kept
            until it expires or a moderator lifts it, so the ban cannot be evaded. You can object
            to this — see the privacy policy.
          </p>

          {!confirmingDelete ? (
            <button
              type="button"
              onClick={() => setConfirmingDelete(true)}
              data-testid="start-delete"
              className="mt-4 rounded border border-red-600 px-4 py-2 font-medium text-red-400 hover:bg-red-950 focus:outline-none focus:ring-2 focus:ring-red-400"
            >
              Delete my data
            </button>
          ) : (
            <div className="mt-4 space-y-3">
              <label htmlFor="confirm-password" className="block text-sm text-fg-secondary">
                Confirm your password
              </label>
              <input
                id="confirm-password"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="w-full rounded border border-line-strong bg-surface px-3 py-2 text-fg focus:outline-none focus:ring-2 focus:ring-sky-400"
              />
              <div className="flex gap-3">
                <button
                  type="button"
                  onClick={handleDelete}
                  disabled={busy === 'delete'}
                  data-testid="confirm-delete"
                  className="rounded bg-red-600 px-4 py-2 font-medium text-white hover:bg-red-500 focus:outline-none focus:ring-2 focus:ring-red-400 disabled:opacity-50"
                >
                  {busy === 'delete' ? 'Deleting…' : 'Permanently delete'}
                </button>
                <button
                  type="button"
                  onClick={() => { setConfirmingDelete(false); setConfirmPassword(''); }}
                  className="rounded border border-line-strong px-4 py-2 text-fg-secondary hover:bg-navy-700 focus:outline-none focus:ring-2 focus:ring-sky-400"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </section>

        <p className="mt-8 text-sm text-fg-muted">
          To correct your details, edit your{' '}
          <Link to="/dashboard/profile" className="text-gold-400 hover:text-gold-300">
            profile
          </Link>
          . See the{' '}
          <Link to="/cookie-policy" className="text-gold-400 hover:text-gold-300">
            cookie policy
          </Link>{' '}
          for what each category covers.
        </p>
      </div>
    </div>
  );
}
