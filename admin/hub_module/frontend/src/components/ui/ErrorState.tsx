import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { Button } from './Button';
import { cx } from './cx';
import { getErrorMessage } from './errors';

export interface ErrorStateProps {
  title?: string;
  /** Anything thrown by a query/mutation; reduced to a safe, readable message. */
  error?: unknown;
  /** Overrides the message derived from `error`. */
  message?: string;
  onRetry?: () => void;
  className?: string;
}

/**
 * Block-level failure state (`role="alert"`) with an optional retry.
 * Never renders raw error objects or stack traces; see `getErrorMessage`.
 */
export function ErrorState({ title = 'We could not load this', error, message, onRetry, className }: ErrorStateProps) {
  return (
    <div
      role="alert"
      data-testid="error-state"
      className={cx('flex flex-col items-center justify-center px-6 py-14 text-center', className)}
    >
      <div
        aria-hidden="true"
        className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-danger/15 text-danger-fg"
      >
        <ExclamationTriangleIcon className="h-6 w-6" />
      </div>
      <h3 className="text-base font-semibold text-fg">{title}</h3>
      <p className="mt-1.5 max-w-md text-sm text-fg-secondary">{message ?? getErrorMessage(error)}</p>
      {onRetry ? (
        <Button className="mt-5" variant="secondary" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}
