import registry from './unservedFeatures.json';

/**
 * UI features whose hub-api route does not exist yet (the Node -> Python port is
 * incomplete). Each entry in `unservedFeatures.json` is rendered as a visible "not
 * available yet" state via `<ServedFeature>` / `<NotAvailableYet>` instead of calling a dead
 * endpoint and failing with a 404 or a silently empty page.
 *
 * Rules:
 *  - Only list a feature when NO served route can back it. Never invent a route.
 *  - `endpoints` use `METHOD /api/v1/path/:param` and must all be unserved: the guard test
 *    `scripts/__tests__/unserved-registry.test.mjs` fails the moment hub-api starts serving
 *    one, telling you to wire the UI and delete the entry.
 *  - For gated legacy pages/components (`<ServedFeature>`), deleting the entry switches them
 *    back on with no other change. Where the UI had to be re-pointed to a different served
 *    route, the guard test is the prompt to wire the real endpoint before deleting the entry.
 *  - The registry is JSON so the audit scripts (`api-gap-check`) read the same source of truth.
 */
export interface UnservedFeature {
  /** Human name used in the "not available yet" heading. */
  label: string;
  /** hub-api calls the UI needs that no blueprint serves. */
  endpoints: readonly string[];
}

export const UNSERVED_FEATURES: Readonly<Record<string, UnservedFeature>> = registry;

/** Looks up an unserved feature by key; `undefined` once the entry is deleted. */
export function findUnservedFeature(key: string): UnservedFeature | undefined {
  return Object.prototype.hasOwnProperty.call(UNSERVED_FEATURES, key) ? UNSERVED_FEATURES[key] : undefined;
}

/**
 * True unless `key` is registered as unserved. Keys are plain strings on purpose:
 * deleting a registry entry (the signal that hub-api now serves the feature) must
 * switch the gated UI on without touching, or breaking the type-check of, its call sites.
 */
export function isFeatureServed(key: string): boolean {
  return findUnservedFeature(key) === undefined;
}
