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

/** Compile a normalised backend path into a regex where `<p>` matches any one segment. */
function toRegex(path) {
  const esc = path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/<p>/g, '[^/]+');
  return new RegExp(`^${esc}$`);
}

/**
 * Build `isServed(call)` for `{ method, path }` where `path` is already `norm`-ed.
 * A frontend `<p>` placeholder is treated as one segment too -- lenient on purpose so
 * literal-vs-param differences (`/auth/oauth/discord` vs `/auth/oauth/<provider>`)
 * never invent a gap.
 */
export function buildIsServed(routes) {
  const compiled = routes.map((r) => ({ method: r.method, re: toRegex(r.path) }));
  return (c) =>
    compiled.some((r) => (!c.method || r.method === c.method) && (r.re.test(c.path) || r.re.test(c.path.replace(/<p>/g, 'x'))));
}
