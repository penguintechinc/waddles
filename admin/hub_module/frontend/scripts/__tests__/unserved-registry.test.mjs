/**
 * Guards `src/lib/unservedFeatures.ts` against rot, in both directions:
 *   - every endpoint a "not available yet" feature lists is STILL unserved by
 *     hub-api (the moment a blueprint ships one, this fails: wire the UI, delete the entry);
 *   - every registered feature is actually used by some page (an entry nothing gates is dead).
 * Counts are asserted > 0 so a scanner pointed at the wrong root cannot pass.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, backendRoutes, buildIsServed, norm, walk } from '../lib/backend-routes.mjs';
import { UNSERVED_FEATURES } from '../../src/lib/unservedFeatures';

const SRC = join(FRONTEND_ROOT, 'src');
const routes = backendRoutes();
const isServed = buildIsServed(routes);

/** Split `GET /api/v1/x/:id` into `{ method, path }` with the shared normalisation. */
function parse(endpoint) {
  const [method, path] = endpoint.split(' ');
  return { method, path: norm(path) };
}

describe('unserved feature registry', () => {
  const entries = Object.entries(UNSERVED_FEATURES);

  it('scans a real backend and a non-empty registry', () => {
    expect(routes.length).toBeGreaterThan(100);
    expect(entries.length).toBeGreaterThan(0);
  });

  it.each(entries)('%s lists only endpoints hub-api still does not serve', (_key, feature) => {
    expect(feature.endpoints.length).toBeGreaterThan(0);
    const nowServed = feature.endpoints.filter((e) => isServed(parse(e)));
    expect(nowServed, `hub-api now serves ${nowServed.join(', ')}: wire the UI and delete the registry entry`).toEqual([]);
  });

  it('every registered feature is referenced by at least one page', () => {
    const files = walk(SRC, /\.(jsx?|tsx?)$/).filter((f) => !/unservedFeatures\.ts$/.test(f));
    const source = files.map((f) => readFileSync(f, 'utf8')).join('\n');
    const unused = entries.map(([key]) => key).filter((key) => !source.includes(`'${key}'`) && !source.includes(`"${key}"`));
    expect(unused).toEqual([]);
  });
});
