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
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeftIcon } from '@heroicons/react/24/outline';

import api from '../services/api';
import { useCookieConsent } from '../hooks/useCookieConsent';

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
    <div className="min-h-screen bg-slate-900 text-slate-200">
      <div className="mx-auto max-w-3xl px-4 py-10">
        <Link
          to="/"
          className="mb-6 inline-flex items-center gap-2 text-amber-400 hover:text-amber-300 focus:outline-none focus:ring-2 focus:ring-sky-500 rounded"
        >
          <ArrowLeftIcon className="h-4 w-4" aria-hidden="true" />
          Back
        </Link>

        <h1 className="text-3xl font-bold text-amber-400">Your privacy rights</h1>
        <p className="mt-2 text-slate-400">
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

        <section className="mt-8 rounded-lg border border-slate-700 bg-slate-800 p-5">
          <h2 className="text-xl font-semibold text-amber-400">Get a copy of your data</h2>
          <p className="mt-1 text-sm text-slate-400">
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

        <section className="mt-6 rounded-lg border border-slate-700 bg-slate-800 p-5">
          <h2 className="text-xl font-semibold text-amber-400">
            Do Not Sell or Share My Personal Information
          </h2>
          <p className="mt-1 text-sm text-slate-400">
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
            className="mt-4 rounded border border-amber-500 px-4 py-2 font-medium text-amber-400 hover:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-sky-400 disabled:opacity-50"
          >
            {optedOut ? 'You are opted out — allow sharing again' : 'Opt out of sale or sharing'}
          </button>
        </section>

        <section className="mt-6 rounded-lg border border-red-800 bg-slate-800 p-5">
          <h2 className="text-xl font-semibold text-red-400">Delete my data</h2>
          <p className="mt-1 text-sm text-slate-400">
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
              <label htmlFor="confirm-password" className="block text-sm text-slate-300">
                Confirm your password
              </label>
              <input
                id="confirm-password"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="w-full rounded border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-400"
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
                  className="rounded border border-slate-600 px-4 py-2 text-slate-300 hover:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-sky-400"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </section>

        <p className="mt-8 text-sm text-slate-500">
          To correct your details, edit your{' '}
          <Link to="/dashboard/profile" className="text-amber-400 hover:text-amber-300">
            profile
          </Link>
          . See the{' '}
          <Link to="/cookie-policy" className="text-amber-400 hover:text-amber-300">
            cookie policy
          </Link>{' '}
          for what each category covers.
        </p>
      </div>
    </div>
  );
}
