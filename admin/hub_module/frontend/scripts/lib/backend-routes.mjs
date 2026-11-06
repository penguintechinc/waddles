/**
 * Shared hub-api route discovery for the webui audit scripts and tests: reads the
 * Quart blueprints on disk (never a running server) and answers "does hub-api
 * serve this method + path?".
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

/** Frontend root (`admin/hub_module/frontend`). */
export const FRONTEND_ROOT = resolve(HERE, '../..');

/** Directory holding hub-api's v1 blueprints. */
export const BLUEPRINTS_DIR = resolve(FRONTEND_ROOT, '../../../hub_api/blueprints/v1');

/** Recursively collect files matching `re`, skipping tests. */
export function walk(dir, re, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name !== '__tests__') walk(p, re, out);
    } else if (re.test(name) && !/\.test\./.test(name)) out.push(p);
  }
  return out;
}

/** Normalise a path so `${id}`, `:id`, `<int:id>` and `<id>` all compare equal. */
export function norm(p) {
  return p
    .replace(/\$\{[^}]+\}/g, '<p>')
    .replace(/<[^>]+>/g, '<p>')
    .replace(/(^|\/):[A-Za-z_]\w*/g, '$1<p>')
    .replace(/\?.*$/, '')
    .replace(/\/+$/, '');
}

/**
 * Backend: blueprint `url_prefix` + `@bp.route()` decorators.
 *
 * Two registration styles can't be resolved to a single literal prefix and are
 * therefore attributed to EVERY prefix declared in the same file (conservative:
 * it can only hide a gap, never invent one):
 *   - `ProxyRoute("name", "/path", "METHOD", ...)` tables
 *   - `@bp.route(...)` inside a `_register(bp)`-style factory that mounts the same
 *     handlers on several blueprints (e.g. `stream.py`)
 */
export function backendRoutes(dir = BLUEPRINTS_DIR) {
  const routes = [];
  for (const f of walk(dir, /\.py$/)) {
    const src = readFileSync(f, 'utf8');
    const prefixes = new Map();
    for (const m of src.matchAll(/(\w+)\s*=\s*Blueprint\(\s*"[^"]+"\s*,\s*__name__\s*,\s*url_prefix\s*=\s*"([^"]*)"/g)) prefixes.set(m[1], m[2]);
    for (const m of src.matchAll(/@(\w+)\.route\(\s*"([^"]*)"(?:\s*,\s*methods\s*=\s*\[([^\]]*)\])?/g)) {
      const own = prefixes.get(m[1]);
      const targets = own !== undefined ? [own] : [...prefixes.values()];
      const methods = (m[3] ?? '"GET"').match(/[A-Z]+/g) ?? ['GET'];
      for (const prefix of targets) {
        for (const method of methods) routes.push({ method, path: norm(prefix + m[2]), file: relative(dir, f) });
      }
    }
    for (const m of src.matchAll(/ProxyRoute\(\s*"[^"]+"\s*,\s*"([^"]*)"\s*,\s*"([A-Z]+)"/g)) {
      for (const prefix of prefixes.values()) {
        routes.push({ method: m[2], path: norm(prefix + m[1]), file: relative(dir, f) });
      }
    }
  }
  return routes;
}

/** True when a backend route segment is a `(a|b)` alternation, e.g. `(discord|twitch)`. */
function alternation(segment) {
  const m = segment.match(/^\(([^()]+\|[^()]+)\)$/);
  return m ? m[1].split('|') : null;
}

/**
 * Does one frontend call segment match one backend route segment?
 * A backend `<p>` matches any non-empty call segment. A call segment containing `<p>` (a
 * `${var}` interpolation, alone or mixed with literal text like `v-<p>`) is a wildcard ONLY
 * against a route alternation `(a|b)` whose option it can stand for -- never against a bare
 * literal, so `/admin/${id}/domains` is not "served" by `/admin/domains/<id>`.
 */
function segmentMatches(callSeg, routeSeg) {
  if (routeSeg === '<p>') return callSeg !== '';
  const options = alternation(routeSeg);
  if (options) {
    if (!callSeg.includes('<p>')) return options.includes(callSeg);
    const pattern = new RegExp(`^${callSeg.split('<p>').map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('[^/]+')}$`);
    return options.some((o) => pattern.test(o));
  }
  return callSeg === routeSeg;
}

/**
 * Build `isServed(call)` for `{ method, path }` where `path` is already `norm`-ed.
 * Matching is segment-wise: a call `<p>` matches a backend `<p>` or a route alternation, and a
 * literal call segment matches a backend `<p>` (`/auth/oauth/discord` vs `/auth/oauth/<provider>`).
 */
export function buildIsServed(routes) {
  const compiled = routes.map((r) => ({ method: r.method, segments: r.path.split('/') }));
  return (c) => {
    const callSegments = c.path.split('/');
    return compiled.some(
      (r) =>
        (!c.method || r.method === c.method) &&
        r.segments.length === callSegments.length &&
        r.segments.every((seg, i) => segmentMatches(callSegments[i], seg)),
    );
  };
}
