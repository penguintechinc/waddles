import type { ReactNode } from 'react';
import type { AnalyticsFailure } from './classifyAnalyticsError';

export interface AnalyticsEmptyProps {
  /** Headline, e.g. "No events yet" / "No data yet". */
  title?: string;
  description?: ReactNode;
  testId?: string;
}

/** Neutral "nothing to show yet" state for a successful response with no rows (`role="status"`). */
export function AnalyticsEmpty({
  title = 'No data yet',
  description,
  testId = 'analytics-empty',
}: AnalyticsEmptyProps) {
  return (
    <div role="status" data-testid={testId} className="py-6 text-center">
      <p className="text-sm font-medium text-sky-100">{title}</p>
      {description ? <p className="mt-1 text-sm text-navy-400">{description}</p> : null}
    </div>
  );
}

/** Neutral info state: the analytics backend is not deployed/reachable here (`role="status"`, not red). */
export function AnalyticsUnavailable({ message }: { message?: string }) {
  return (
    <div role="status" data-testid="analytics-unavailable" className="py-6 text-center">
      <p className="text-sm font-medium text-sky-100">
        {message ?? "Analytics aren't available on this deployment yet."}
      </p>
      <p className="mt-1 text-sm text-navy-400">
        Nothing is wrong on your side; this will appear once analytics are enabled.
      </p>
    </div>
  );
}

/** Neutral notice for a plan/flag-gated widget (HTTP 402). */
export function AnalyticsFeatureNotice({ message }: { message: string }) {
  return (
    <div role="status" data-testid="analytics-feature-notice" className="py-6 text-center">
      <p className="text-sm font-medium text-sky-100">Not included in your plan</p>
      <p className="mt-1 text-sm text-navy-400">{message}</p>
    </div>
  );
}

/** Real failure: the actual message plus a Retry (`role="alert"`). */
export function AnalyticsError({ message, onRetry }: { message: string; onRetry?: (() => void) | undefined }) {
  return (
    <div role="alert" data-testid="analytics-error" className="py-6 text-center">
      <p className="text-sm text-red-300">{message}</p>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded bg-navy-800 px-3 py-1 text-sm text-sky-200 hover:text-sky-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}

/** Picks the right presentation for a classified failure. */
export function AnalyticsFailureState({
  failure,
  onRetry,
  emptyTitle = 'No data yet',
}: {
  failure: AnalyticsFailure;
  onRetry?: (() => void) | undefined;
  /** Shown for `not-found` (a 404 from an endpoint that means "no rows"). */
  emptyTitle?: string;
}) {
  switch (failure.kind) {
    case 'unavailable':
      return <AnalyticsUnavailable message={failure.message} />;
    case 'feature-disabled':
      return <AnalyticsFeatureNotice message={failure.message} />;
    case 'not-found':
      return <AnalyticsEmpty title={emptyTitle} />;
    default:
      return <AnalyticsError message={failure.message} onRetry={onRetry} />;
  }
}

export interface AnalyticsPanelProps {
  title?: string;
  loading: boolean;
  failure: AnalyticsFailure | null;
  /** True when a successful response carries no rows / only zeros. */
  empty: boolean;
  emptyTitle?: string;
  onRetry?: () => void;
  /** Rendered only for a successful, non-empty response. */
  children: ReactNode;
  testId?: string;
  className?: string;
}

/**
 * One analytics widget with its own loading / failure / empty / ok state, so a single failed call
 * never blanks the page. Failure and empty states render inside a titled card matching the charts.
 */
export function AnalyticsPanel({
  title,
  loading,
  failure,
  empty,
  emptyTitle = 'No data yet',
  onRetry,
  children,
  testId,
  className = '',
}: AnalyticsPanelProps) {
  if (!loading && !failure && !empty) return <>{children}</>;
  return (
    <div className={`card p-6 ${className}`} data-testid={testId}>
      {title ? <h2 className="text-lg font-semibold mb-4 text-sky-100">{title}</h2> : null}
      {loading ? (
        <div role="status" aria-label="Loading" className="flex h-16 items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-b-2 border-sky-400" />
        </div>
      ) : failure ? (
        <AnalyticsFailureState failure={failure} onRetry={onRetry} emptyTitle={emptyTitle} />
      ) : (
        <AnalyticsEmpty title={emptyTitle} />
      )}
    </div>
  );
}
