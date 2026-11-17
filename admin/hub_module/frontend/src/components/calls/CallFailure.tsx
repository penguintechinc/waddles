import { Button, ErrorState } from '../ui';
import { CallsErrorPanel } from './CallsErrors';
import type { CallFailure } from '../../services/media/callSession';

export interface CallFailurePanelProps {
  failure: CallFailure;
  onRetry: () => void;
  onListenOnly: () => void;
}

/**
 * Full-panel state for a call that is not running: device permission problems (with listen-only), a failed join
 * (flag off = 402, retryable 503, ...), media negotiation errors, and a lost connection. Always shows the reason.
 */
export function CallFailurePanel({ failure, onRetry, onListenOnly }: CallFailurePanelProps) {
  if (failure.kind === 'join') {
    return <CallsErrorPanel error={failure.cause} onRetry={onRetry} />;
  }
  if (failure.kind === 'device') {
    return (
      <div data-testid="call-device-error">
        <ErrorState
          title={failure.deviceKind === 'permission-denied' ? 'Microphone access is blocked' : 'Could not open your microphone'}
          message={failure.message}
          onRetry={onRetry}
        />
        <div className="flex justify-center pb-6">
          <Button onClick={onListenOnly}>Join listen-only</Button>
        </div>
      </div>
    );
  }
  const title = failure.kind === 'connection-lost' ? 'Connection lost' : failure.kind === 'leave' ? 'Left, but not confirmed' : 'Could not connect audio and video';
  return (
    <div data-testid={failure.kind === 'connection-lost' ? 'call-connection-lost' : 'call-media-error'}>
      <ErrorState title={title} message={failure.message} {...(failure.retryable ? { onRetry } : {})} />
    </div>
  );
}
