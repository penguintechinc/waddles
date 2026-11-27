/**
 * Shared helpers for the MFA UI: turn a hub-api error envelope into a user-facing
 * message, and the method labels used by both the settings page and the login step.
 */

/** Human labels per MFA method key (TOTP first: it is the preferred method). */
export const METHOD_LABELS = {
  totp: 'Authenticator app',
  email: 'Email code',
  recovery: 'Recovery code',
};

/** Error code hub-api returns when the pending login can no longer be completed. */
export const CHALLENGE_INVALID = 'MFA_CHALLENGE_INVALID';

/**
 * Extracts `{ code, message }` from an axios error carrying hub-api's
 * `{ error: { code, message } }` envelope (or a legacy string error).
 */
export function mfaError(err, fallback = 'Something went wrong. Please try again.') {
  const apiErr = err?.response?.data?.error;
  if (typeof apiErr === 'string') return { code: undefined, message: apiErr };
  if (apiErr && typeof apiErr === 'object') {
    return { code: apiErr.code, message: apiErr.message || fallback };
  }
  return { code: undefined, message: fallback };
}
