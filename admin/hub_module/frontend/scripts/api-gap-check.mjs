#!/usr/bin/env node
/**
 * Cross-checks every `/api/v1/...` call the hub-webui makes against the routes
 * hub-api actually registers, to find UI calls with no backend route (dead
 * buttons) and, conversely, which backend blueprints have no UI at all.
 *
 * Usage: node scripts/api-gap-check.mjs [--list] [--pages]
 *   --list   print every UI call with no hub-api route and every UI-less blueprint
 *   --pages  per routed page: how many of its endpoints hub-api does not serve
 * Exits non-zero when zero frontend calls or zero backend routes were found
 * (a scanner pointed at the wrong root must never report clean).
 */
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { routeSurvey } from './lib/routes.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const BLUEPRINTS = resolve(ROOT, '../../../hub_api/blueprints/v1');
const list = process.argv.includes('--list');
const byPage = process.argv.includes('--pages');

/** Recursively collect files matching `re`, skipping tests. */
function walk(dir, re, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name !== '__tests__') walk(p, re, out);
    } else if (re.test(name) && !/\.test\./.test(name)) out.push(p);
  }
  return out;
}

/** Normalise a path so `${id}`, `<int:id>` and `<id>` all compare equal. */
function norm(p) {
  return p
    .replace(/\$\{[^}]+\}/g, '<p>')
    .replace(/<[^>]+>/g, '<p>')
    .replace(/\?.*$/, '')
    .replace(/\/+$/, '');
}

/** Frontend: every quoted/templated /api/v1 string with its HTTP method when visible. */
function frontendCalls() {
  const calls = [];
  for (const f of walk(SRC, /\.(jsx?|tsx?)$/)) {
    const lines = readFileSync(f, 'utf8').split('\n');
    lines.forEach((text, i) => {
      for (const m of text.matchAll(/(?:\b(?:api|apiClient|axios)\.(get|post|put|patch|delete)\(|\b(get|post|put|patch|delete)<[^>]*>\()?\s*[`'"](\/api\/v1\/[^`'"]*)[`'"]/g)) {
        calls.push({ method: (m[1] ?? m[2] ?? '').toUpperCase(), path: norm(m[3]), raw: m[3], file: relative(ROOT, f), line: i + 1 });
      }
    });
  }
  return calls;
}

/** Backend: blueprint url_prefix + route() decorators. */
function backendRoutes() {
  const routes = [];
  for (const f of walk(BLUEPRINTS, /\.py$/)) {
    const src = readFileSync(f, 'utf8');
    const prefixes = new Map();
    for (const m of src.matchAll(/(\w+)\s*=\s*Blueprint\(\s*"[^"]+"\s*,\s*__name__\s*,\s*url_prefix\s*=\s*"([^"]*)"/g)) prefixes.set(m[1], m[2]);
    for (const m of src.matchAll(/@(\w+)\.route\(\s*"([^"]*)"(?:\s*,\s*methods\s*=\s*\[([^\]]*)\])?/g)) {
      const prefix = prefixes.get(m[1]);
      if (prefix === undefined) continue;
      const methods = (m[3] ?? '"GET"').match(/[A-Z]+/g) ?? ['GET'];
      for (const method of methods) routes.push({ method, path: norm(prefix + m[2]), file: relative(BLUEPRINTS, f) });
    }
    // ProxyRoute("name", "/path", "METHOD", ...) tables register onto a blueprint in the
    // same file; attribute each to every prefix in the file (conservative: can only hide a
    // gap, never invent one).
    for (const m of src.matchAll(/ProxyRoute\(\s*"[^"]+"\s*,\s*"([^"]*)"\s*,\s*"([A-Z]+)"/g)) {
      for (const prefix of prefixes.values()) {
        routes.push({ method: m[2], path: norm(prefix + m[1]), file: relative(BLUEPRINTS, f) });
      }
    }
  }
  return routes;
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
    rows.push({ path: r.path, file: relative(ROOT, r.file), endpoints: used.size, unserved: unserved.length, sample: unserved.slice(0, 2).map((w) => `${w.method} ${w.raw}`) });
  }
  return rows;
}

const calls = frontendCalls();
const routes = backendRoutes();
if (calls.length === 0 || routes.length === 0) {
  console.error(`FAIL: scanner examined ${calls.length} frontend calls and ${routes.length} backend routes`);
  process.exit(2);
}

/** Compile a normalised backend path into a regex where `<p>` matches any one segment. */
function toRegex(path) {
  const esc = path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/<p>/g, '[^/]+');
  return new RegExp(`^${esc}$`);
}
const compiled = routes.map((r) => ({ method: r.method, re: toRegex(r.path) }));
// A frontend path matches when some backend route (same method when known) accepts it,
// treating a frontend `<p>` as a placeholder segment too -- lenient on purpose so literal-vs-
// param differences (`/auth/oauth/discord` vs `/auth/oauth/<provider>`) never invent a gap.
const isServed = (c) => compiled.some((r) => (!c.method || r.method === c.method) && (r.re.test(c.path) || r.re.test(c.path.replace(/<p>/g, 'x'))));
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
  backendBlueprintFiles: allFiles.size,
  backendBlueprintFilesWithNoUiCall: noUi.length,
}, null, 2));
if (list) {
  console.log('\n# UI calls with no matching backend route');
  for (const c of uniqueMissing) console.log(`${(c.method || '?').padEnd(6)} ${c.raw}  (${c.file}:${c.line})`);
  console.log('\n# Backend blueprints with no UI call');
  for (const f of noUi) console.log(f);
}
if (byPage) {
  const rows = pageCoverage(isServed);
  const broken = rows.filter((r) => r.unserved > 0);
  console.log(`\n# Pages with endpoints hub-api does not serve (${broken.length} of ${rows.length} routed pages examined)`);
  for (const r of broken.sort((a, b) => b.unserved - a.unserved)) {
    console.log(`${String(r.unserved).padStart(2)}/${String(r.endpoints).padEnd(2)} ${r.path.padEnd(58)} ${r.sample.join(' | ')}`);
  }
}
