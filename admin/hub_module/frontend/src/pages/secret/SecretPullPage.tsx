import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';

import { useAuth } from '../../contexts/AuthContext';
import { useFeatureFlag } from '../../lib/useFeatureFlag';
import { oneTimeSecretApi, pullErrorStatus } from '../../services/oneTimeSecretApi';

/** `AuthContext` is untyped JS; narrow the one shape this page reads. */
interface AuthValue {
  user: unknown;
  loading: boolean;
}

/** Reads the token from the URL fragment (`#<token>` or `#token=<token>`). */
function tokenFromHash(hash: string): string | null {
  const raw = hash.replace(/^#/, '');
  const value = raw.startsWith('token=') ? raw.slice('token='.length) : raw;
  return value ? decodeURIComponent(value) : null;
}

/** Page chrome shared by every state. */
function Shell({ children }: { children: ReactNode }) {
  return (
    <main className="mx-auto max-w-xl p-6 text-slate-100" data-testid="secret-pull-page">
      <h1 className="mb-4 text-2xl font-semibold text-amber-400">Secret message</h1>
      {children}
    </main>
  );
}

const BTN = 'rounded bg-sky-600 px-4 py-2 focus:ring-2 focus:ring-sky-500 disabled:opacity-50';

/**
 * One-time secret pull page (#684). The DM link carries the token in the URL
 * fragment; the user must be logged in as the linked target and must click
 * "Reveal" explicitly because the pull is destructive. The secret lives only
 * in React state -- never storage, never logs.
 */
export default function SecretPullPage() {
  const enabled = useFeatureFlag('waddles.secret-messaging');
  const { user, loading } = useAuth() as unknown as AuthValue;
  const [token, setToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // Capture the token once logged in, then scrub it from the URL so it is not
  // left in history. Logged out: leave the fragment intact for the next visit.
  useEffect(() => {
    if (loading || !user || token) return;
    const found = tokenFromHash(window.location.hash);
    if (found) {
      setToken(found);
      window.history.replaceState(null, '', window.location.pathname);
    }
  }, [loading, user, token]);

  const pull = useMutation({
    mutationFn: (t: string) => oneTimeSecretApi.pull(t),
    gcTime: 0,
    retry: false,
  });

  const status = pullErrorStatus(pull.error);

  if (loading) return <p className="p-6 text-slate-300">Loading...</p>;

  if (!enabled) {
    return (
      <Shell>
        <p data-testid="secret-disabled">Secret messaging is not available.</p>
      </Shell>
    );
  }
  if (!user) {
    return (
      <Shell>
        <p data-testid="secret-login">
          Please{' '}
          <Link className="text-sky-400 underline" to="/login">
            log in
          </Link>{' '}
          with your linked account, then open your secret link again.
        </p>
      </Shell>
    );
  }

  if (pull.isSuccess) {
    return (
      <Shell>
        <pre className="whitespace-pre-wrap break-all rounded bg-slate-800 p-4" data-testid="secret-value">
          {pull.data}
        </pre>
        <p className="mt-3 text-sm text-amber-300">
          This secret was one-time and is now deleted. Copy it now; it cannot be shown again.
        </p>
        <button
          type="button"
          className={`mt-3 ${BTN}`}
          onClick={() => {
            void navigator.clipboard.writeText(pull.data).then(() => setCopied(true));
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </Shell>
    );
  }

  if (pull.isError) {
    const message =
      status === 403
        ? 'You are not the intended recipient of this secret.'
        : status === 410
          ? 'This secret was already viewed or has expired.'
          : 'Could not reach the server. Check your connection and try again.';
    return (
      <Shell>
        <div role="alert" data-testid="secret-error">
          <p className="text-red-400">{message}</p>
          {status === undefined && token && (
            <button type="button" className={`mt-3 ${BTN}`} onClick={() => pull.mutate(token)}>
              Retry
            </button>
          )}
        </div>
      </Shell>
    );
  }

  if (!token) {
    return (
      <Shell>
        <p data-testid="secret-no-token">No secret link detected. Open the link you were sent.</p>
      </Shell>
    );
  }

  return (
    <Shell>
      <p className="mb-3">
        Revealing shows the secret once and permanently deletes it. Make sure nobody is watching your screen.
      </p>
      <button type="button" className={BTN} disabled={pull.isPending} onClick={() => pull.mutate(token)}>
        Reveal secret
      </button>
    </Shell>
  );
}
