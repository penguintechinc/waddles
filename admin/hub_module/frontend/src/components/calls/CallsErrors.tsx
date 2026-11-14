import { Alert, EmptyState, ErrorState } from '../ui';
import { toCallsError } from '../../services/callsApi';
import { describeCallsError } from './describeCallsError';

export interface CallsErrorPanelProps {
  error: unknown;
  /** Re-runs the failed read; offered for every failure except "feature not enabled". */
  onRetry: () => void;
}

/**
 * Block-level state for a failed calls READ. Flag-off (HTTP 402) is a clear "not enabled for this
 * community" state with no retry (retrying cannot help); 503 and other failures show the server's
 * message with a retry button. An error is never rendered as an empty list.
 */
export function CallsErrorPanel({ error, onRetry }: CallsErrorPanelProps) {
  const err = toCallsError(error);
  const { title, message } = describeCallsError(err);
  if (err.kind === 'feature-disabled') {
    return (
      <div data-testid="calls-feature-disabled" role="status">
        <EmptyState
          title={title}
          description="Voice and video calls are a plan feature that is switched off for this community. Nothing is wrong with your account or the server."
          testId="calls-feature-disabled-body"
        />
      </div>
    );
  }
  return (
    <div data-testid={err.retryable ? 'calls-unavailable' : 'calls-error'}>
      <ErrorState title={title} message={message} onRetry={onRetry} />
    </div>
  );
}

export interface CallsActionErrorProps {
  error: unknown;
  onDismiss: () => void;
}

/** Inline banner for a failed calls ACTION (create, kick, raise hand, ...). */
export function CallsActionError({ error, onDismiss }: CallsActionErrorProps) {
  const err = toCallsError(error);
  const { title, message } = describeCallsError(err);
  return (
    <Alert tone="danger" title={title} onDismiss={onDismiss} className="mb-4">
      <span>{message}</span>
      {err.retryable ? <span> You can try again.</span> : null}
    </Alert>
  );
}
