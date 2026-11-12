/**
 * Client-side mirror of the hub-api reputation policy rules (300-850 scale).
 * The server stays authoritative; these checks only give instant feedback.
 */

export const SCORE_MIN = 300;
export const SCORE_MAX = 850;

/** Returns an error string when `value` is not an integer in 300-850, else null. */
export function validateScore(value, label) {
  if (value === '' || value === null || value === undefined || !Number.isInteger(Number(value))) {
    return `${label} must be a whole number between ${SCORE_MIN} and ${SCORE_MAX}`;
  }
  const n = Number(value);
  if (n < SCORE_MIN || n > SCORE_MAX) {
    return `${label} must be between ${SCORE_MIN} and ${SCORE_MAX}`;
  }
  return null;
}

/** Validates tenant policy: both in range and floor strictly below the at-risk threshold. */
export function validateTenantPolicy({ floor, atRisk }) {
  const errors = {};
  const f = validateScore(floor, 'Auto-ban floor');
  const a = validateScore(atRisk, 'At-risk threshold');
  if (f) errors.floor = f;
  if (a) errors.atRisk = a;
  if (!f && !a && Number(floor) >= Number(atRisk)) {
    errors.floor = 'Auto-ban floor must be lower than the at-risk threshold';
  }
  return errors;
}

/** Validates community policy: 300 <= hard < soft < at-risk <= 850. */
export function validateCommunityPolicy({ hard, soft, atRisk }) {
  const errors = {};
  const h = validateScore(hard, 'Hard-ban floor');
  const s = validateScore(soft, 'Soft-ban floor');
  const a = validateScore(atRisk, 'At-risk threshold');
  if (h) errors.hard = h;
  if (s) errors.soft = s;
  if (a) errors.atRisk = a;
  if (!h && !s && Number(hard) >= Number(soft)) {
    errors.hard = 'Hard-ban floor must be lower than the soft-ban floor';
  }
  if (!s && !a && Number(soft) >= Number(atRisk)) {
    errors.soft = 'Soft-ban floor must be lower than the at-risk threshold';
  }
  return errors;
}

/** Extracts the server's error message verbatim from an axios-style error. */
export function serverErrorMessage(err, fallback = 'Request failed') {
  const data = err?.response?.data;
  const env = data?.error;
  if (typeof env === 'string') return data?.message || env;
  return env?.message || data?.message || err?.message || fallback;
}
