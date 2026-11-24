import { toCallsError } from '../../services/callsApi';

/** Title + message to show for a failed calls request; the message is the server's `error.message`. */
export function describeCallsError(error: unknown): { title: string; message: string } {
  const err = toCallsError(error);
  switch (err.kind) {
    case 'feature-disabled':
      return { title: 'Calls are not enabled for this community', message: err.message };
    case 'unavailable':
      return { title: 'The calls service is unavailable', message: err.message };
    case 'forbidden':
      return { title: 'You do not have permission to do that', message: err.message };
    case 'not-found':
      return { title: 'That room no longer exists', message: err.message };
    case 'rate-limited':
      return { title: 'Too many requests', message: err.message };
    default:
      return { title: 'The calls request failed', message: err.message };
  }
}
