/**
 * Community ids are opaque UUID strings (hub-api `public_id`); the numeric primary key never
 * crosses the API boundary. This module is the single place the webui validates one.
 */

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** True when `value` is a canonical UUID string, the only accepted community id shape. */
export function isCommunityId(value: unknown): value is string {
  return typeof value === 'string' && UUID_RE.test(value);
}

/**
 * Validates a route param / prop as a community UUID. Returns it, or null when absent or
 * malformed so the caller can render an explicit "unknown community" state (never a fallback).
 */
export function parseCommunityId(raw: unknown): string | null {
  return isCommunityId(raw) ? raw : null;
}

/** Like {@link parseCommunityId} but throws, for code paths where an absent id is a defect. */
export function requireCommunityId(raw: unknown, context: string): string {
  if (!isCommunityId(raw)) {
    throw new Error(`${context}: expected a community UUID, got ${JSON.stringify(raw)}`);
  }
  return raw;
}
