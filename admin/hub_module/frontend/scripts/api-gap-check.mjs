#!/usr/bin/env node
/**
 * Cross-checks every `/api/v1/...` call the hub-webui makes against the routes
 * hub-api actually registers, to find UI calls with no backend route (dead
 * buttons) and, conversely, which backend blueprints have no UI at all.
 *
 * Usage: node scripts/api-gap-check.mjs [--list] [--pages]
 *   --list   print every UI call with no hub-api route, every call that omits the
 *            `/api/v1` prefix (so can never reach hub-api), and every UI-less blueprint
 *   --pages  per routed page: how many of its endpoints hub-api does not serve
 *   --full   with --pages, list every unserved endpoint instead of a two-endpoint sample
 * Exits non-zero when zero frontend calls or zero backend routes were found
 * (a scanner pointed at the wrong root must never report clean).
 */
import { readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { routeSurvey } from './lib/routes.mjs';
import { FRONTEND_ROOT as ROOT, backendRoutes, buildIsServed, norm, walk } from './lib/backend-routes.mjs';

const SRC = join(ROOT, 'src');
const REGISTRY_FILE = /unservedFeatures\.(ts|json)$/;
const REGISTRY = JSON.parse(readFileSync(join(SRC, 'lib/unservedFeatures.json'), 'utf8'));

/** `{ "METHOD norm-path" }` for every endpoint of the given registered features. */
function gatedEndpointSet(keys) {
  const set = new Set();
  for (const key of keys) {
    for (const endpoint of REGISTRY[key]?.endpoints ?? []) {
      const [method, path] = endpoint.split(' ');
      set.add(`${method} ${norm(path)}`);
    }
  }
  return set;
}

const list = process.argv.includes('--list');
const byPage = process.argv.includes('--pages');
const full = process.argv.includes('--full');

/** Frontend: every quoted/templated /api/v1 string with its HTTP method when visible. */
function frontendCalls() {
  const calls = [];
  for (const f of walk(SRC, /\.(jsx?|tsx?)$/)) {
    if (REGISTRY_FILE.test(f)) continue; // documents missing endpoints; not a call site
    const lines = readFileSync(f, 'utf8').split('\n');
    lines.forEach((text, i) => {
      for (const m of text.matchAll(/(?:\b(?:api|apiClient|axios)\.(get|post|put|patch|delete)\(|\b(get|post|put|patch|delete)<[^>]*>\()?\s*[`'"](\/api\/v1\/[^`'"]*)[`'"]/g)) {
        calls.push({ method: (m[1] ?? m[2] ?? '').toUpperCase(), path: norm(m[3]), raw: m[3], file: relative(ROOT, f), line: i + 1 });
      }
    });
  }
  return calls;
}

/**
 * Calls whose path does not start with `/api/` (e.g. `api.get('/vendor/modules')`). The
 * shared axios instance has an empty baseURL, so these hit the SPA host itself and are
 * answered by the index.html fallback -- they can never reach hub-api, and the
 * `/api/v1`-only scan above is blind to them.
 */
function unprefixedCalls() {
  const calls = [];
  for (const f of walk(SRC, /\.(jsx?|tsx?)$/)) {
    const lines = readFileSync(f, 'utf8').split('\n');
    lines.forEach((text, i) => {
      for (const m of text.matchAll(/\b(?:api|apiClient|axios)\.(get|post|put|patch|delete)(?:<[^>]*>)?\(\s*[`'"](\/(?!api\/)[^`'"]*)[`'"]/g)) {
        calls.push({ method: m[1].toUpperCase(), raw: m[2], file: relative(ROOT, f), line: i + 1 });
      }
    });
  }
  return calls;
}

/** Map `exportName.key` wrapper calls (services/*.js|ts) to their normalised endpoint. */
function wrapperMap() {
  const map = new Map();
  for (const f of walk(join(SRC, 'services'), /\.(js|ts)$/)) {
    const lines = readFileSync(f, 'utf8').split('\n');
    let owner = null;
    lines.forEach((text, i) => {
      const o = text.match(/^export const (\w+)\s*=\s*\{/);
      if (o) owner = o[1];
      const e = text.match(/^\s+(\w+):/);
      if (!owner || !e) return;
      const window3 = lines.slice(i, i + 3).join(' ');
      const m = window3.match(/(?:api|apiClient)\.(get|post|put|patch|delete)(?:<[^>]*>)?\(\s*[`'"](\/api\/v1\/[^`'"]*)[`'"]/);
      if (m) map.set(`${owner}.${e[1]}`, { method: m[1].toUpperCase(), path: norm(m[2]), raw: m[2] });
    });
  }
  return map;
}

/** Per routed page: endpoints it touches and how many have no hub-api route. */
function pageCoverage(isServed) {
  const wrappers = wrapperMap();
  const rows = [];
  for (const r of routeSurvey(SRC)) {
    const src = readFileSync(r.file, 'utf8');
    const used = new Map();
    for (const m of src.matchAll(/\b(\w+Api|api|adminApi|publicApi)\.(\w+)\(/g)) {
      const w = wrappers.get(`${m[1]}.${m[2]}`);
      if (w) used.set(`${w.method} ${w.path}`, w);
    }
    for (const m of src.matchAll(/\b(?:api|apiClient)\.(get|post|put|patch|delete)(?:<[^>]*>)?\(\s*[`'"](\/api\/v1\/[^`'"]*)[`'"]/g)) {
      used.set(`${m[1].toUpperCase()} ${norm(m[2])}`, { method: m[1].toUpperCase(), path: norm(m[2]), raw: m[2] });
    }
    const unserved = [...used.values()].filter((w) => !isServed(w));
    // Gated when the route wraps the page in <ServedFeature>, or when the page itself names a
    // registered feature (e.g. `isFeatureServed('reputation-config')`).
    const keys = Object.keys(REGISTRY).filter((key) => r.gate === key || src.includes(`'${key}'`));
    const gatedSet = gatedEndpointSet(keys);
    // A route-level <ServedFeature> shields the whole page (none of its calls run), whatever
    // feature key it names; a page-level gate shields only that feature's endpoints.
    const broken = r.gate ? [] : unserved.filter((w) => !gatedSet.has(`${w.method} ${w.path}`));
    rows.push({
      path: r.path,
      file: relative(ROOT, r.file),
      gate: keys[0] ?? null,
      endpoints: used.size,
      unserved: unserved.length,
      broken: broken.length,
      sample: broken.slice(0, full ? broken.length : 2).map((w) => `${w.method} ${w.raw}`),
    });
  }
  return rows;
}

const calls = frontendCalls();
const unprefixed = unprefixedCalls();
const routes = backendRoutes();
if (calls.length === 0 || routes.length === 0) {
  console.error(`FAIL: scanner examined ${calls.length} frontend calls and ${routes.length} backend routes`);
  process.exit(2);
}

const isServed = buildIsServed(routes);
const missing = calls.filter((c) => !isServed(c));
const uniqueMissing = [...new Map(missing.map((c) => [`${c.method} ${c.path}`, c])).values()];

const touchedFiles = new Set(routes.filter((r) => calls.some((c) => c.path === r.path)).map((r) => r.file));
const allFiles = new Set(routes.map((r) => r.file));
const noUi = [...allFiles].filter((f) => !touchedFiles.has(f)).sort();

console.log(JSON.stringify({
  frontendCallsExamined: calls.length,
  uniqueFrontendEndpoints: new Set(calls.map((c) => `${c.method} ${c.path}`)).size,
  backendRoutesExamined: routes.length,
  frontendCallsWithoutBackendRoute: uniqueMissing.length,
  frontendCallsMissingApiPrefix: unprefixed.length,
  backendBlueprintFiles: allFiles.size,
  backendBlueprintFilesWithNoUiCall: noUi.length,
}, null, 2));
if (list) {
  console.log('\n# UI calls with no matching backend route');
  for (const c of uniqueMissing) console.log(`${(c.method || '?').padEnd(6)} ${c.raw}  (${c.file}:${c.line})`);
  console.log('\n# UI calls missing the /api/v1 prefix (never reach hub-api)');
  for (const c of unprefixed) console.log(`${c.method.padEnd(6)} ${c.raw}  (${c.file}:${c.line})`);
  console.log('\n# Backend blueprints with no UI call');
  for (const f of noUi) console.log(f);
}
if (byPage) {
  const rows = pageCoverage(isServed);
  const broken = rows.filter((r) => r.broken > 0);
  const gated = rows.filter((r) => r.broken === 0 && r.unserved > 0);
  console.log(`\n# Pages calling endpoints hub-api does not serve and nothing gates (${broken.length} of ${rows.length} routed pages examined)`);
  for (const r of broken.sort((a, b) => b.broken - a.broken)) {
    console.log(`${String(r.broken).padStart(2)}/${String(r.endpoints).padEnd(2)} ${r.path.padEnd(58)} ${r.sample.join(' | ')}`);
  }
  console.log(`\n# Pages gated behind a visible "not available yet" state (${gated.length}; no dead calls are made)`);
  for (const r of gated.sort((a, b) => a.path.localeCompare(b.path))) {
    console.log(`${String(r.unserved).padStart(2)}/${String(r.endpoints).padEnd(2)} ${r.path.padEnd(58)} feature=${r.gate}`);
  }
}
