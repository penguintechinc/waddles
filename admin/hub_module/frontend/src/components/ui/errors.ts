/** Narrows an unknown value to a plain string-keyed record. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/** Returns the first non-empty string among the candidates, or undefined. */
function firstString(...candidates: unknown[]): string | undefined {
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim() !== '') return candidate;
  }
  return undefined;
}

/**
 * Extracts a user-presentable message from anything a query/mutation can throw.
 * Handles Axios errors carrying hub-api's `{ error }` / `{ message }` body, plain
 * `Error`s and strings; never returns raw objects or stack traces.
 */
export function getErrorMessage(error: unknown, fallback = 'Something went wrong. Please try again.'): string {
  if (typeof error === 'string') return firstString(error) ?? fallback;
  if (!isRecord(error)) return fallback;

  const response = error['response'];
  if (isRecord(response)) {
    const data = response['data'];
    if (isRecord(data)) {
      const fromBody = firstString(data['error'], data['message'], data['detail']);
      if (fromBody) return fromBody;
    }
    if (response['status'] === 403) return 'You do not have permission to do that.';
    if (response['status'] === 404) return 'We could not find what you were looking for.';
  }
  return firstString(error['message']) ?? fallback;
}
