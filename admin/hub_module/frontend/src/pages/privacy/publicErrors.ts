import { dsarErrorCode, dsarErrorMessage, dsarErrorStatus } from '../../services/dsarConstants';

/** Maps a failed public call to a message that never reveals whether a tenant or email exists. */
export function publicError(err: unknown): string {
  const status = dsarErrorStatus(err);
  const code = dsarErrorCode(err);
  if (status === 429) return 'Too many requests. Please wait a while and try again.';
  if (code === 'DSAR_CAPTCHA_FAILED') return 'The security check failed. Please try again.';
  if (status === 503) return dsarErrorMessage(err, 'This service is temporarily unavailable. Please try again later.');
  if (status === 400) return 'Please check the details you entered and try again.';
  return 'Your request could not be submitted. Please try again later.';
}
