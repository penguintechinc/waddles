/**
 * Runtime narrowing for hub-api JSON. `apiClient.get<unknown>` hands back `unknown`;
 * these helpers turn it into typed view-models and throw a `WireShapeError` when the
 * server's shape drifts from what the UI expects -- so contract drift surfaces as an
 * error state instead of a silently empty page (the failure mode that left the
 * post-login landing blank).
 */

/** Thrown when a response body does not match the shape the UI relies on. */
export class WireShapeError extends Error {
  constructor(what: string, detail: string) {
    super(`Unexpected response from the server (${what}: ${detail}).`);
    this.name = 'WireShapeError';
  }
}

/** Plain string-keyed JSON object. */
export type WireRecord = Record<string, unknown>;

/** Narrows to a non-null, non-array object. */
export function isRecord(value: unknown): value is WireRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Returns `value` as a record or throws a `WireShapeError` naming `what`. */
export function expectRecord(value: unknown, what: string): WireRecord {
  if (!isRecord(value)) throw new WireShapeError(what, 'expected an object');
  return value;
}

/** Returns `record[key]` as an array or throws. */
export function readArray(record: WireRecord, key: string, what: string): unknown[] {
  const value = record[key];
  if (!Array.isArray(value)) throw new WireShapeError(what, `"${key}" is not a list`);
  return value;
}

/** Required string field. */
export function readString(record: WireRecord, key: string, what: string): string {
  const value = record[key];
  if (typeof value !== 'string') throw new WireShapeError(what, `"${key}" is not a string`);
  return value;
}

/** Required finite number field. */
export function readNumber(record: WireRecord, key: string, what: string): number {
  const value = record[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new WireShapeError(what, `"${key}" is not a number`);
  }
  return value;
}

/** Optional string: `null` when absent, `null`, or not a string. */
export function readOptString(record: WireRecord, key: string): string | null {
  const value = record[key];
  return typeof value === 'string' ? value : null;
}

/** Optional finite number: `null` when absent or not a number. */
export function readOptNumber(record: WireRecord, key: string): number | null {
  const value = record[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Optional boolean with a default for absent/non-boolean values. */
export function readBool(record: WireRecord, key: string, fallback = false): boolean {
  const value = record[key];
  return typeof value === 'boolean' ? value : fallback;
}
