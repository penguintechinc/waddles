/**
 * Shared route parsing for the hub-webui audit scripts: maps every `<Route>` in
 * `src/App.jsx` to the page component file that renders it.
 */
import { readFileSync, existsSync, statSync } from 'node:fs';
import { join } from 'node:path';

const NON_PAGE = new Set(['Suspense', 'LoadingSpinner', 'ProtectedRoute', 'Navigate', 'PublicLayout', 'DashboardLayout', 'AdminLayout', 'Route', 'Routes', 'PageLoading', 'ServedFeature']);

/** Resolve an import specifier to a real file, trying .jsx/.tsx extensions. */
function resolveFile(src, rel) {
  const base = join(src, rel);
  return ['', '.jsx', '.tsx'].map((e) => base + e).find((p) => existsSync(p) && statSync(p).isFile());
}

/**
 * Parse App.jsx and return `[{ path, component, file, gate }]`, one entry per routed
 * page. Handles static imports, `lazy()` imports, and Suspense-wrapped elements. `gate` is
 * the `<ServedFeature feature="...">` key wrapping the page (a visible "not available yet"
 * state while hub-api lacks its routes), or `null`.
 */
export function routeSurvey(src) {
  const app = readFileSync(join(src, 'App.jsx'), 'utf8');
  const imports = new Map();
  for (const m of app.matchAll(/import (\w+) from '(\.\/[^']+)'/g)) imports.set(m[1], m[2]);
  for (const m of app.matchAll(/const (\w+) = lazy\(\(\) => import\('(\.\/[^']+)'\)\)/g)) imports.set(m[1], m[2]);
  const routes = [];
  const chunks = app.split(/<Route\b/).slice(1);
  for (const chunk of chunks) {
    const path = chunk.match(/path="([^"]+)"/)?.[1];
    if (!path) continue;
    const head = chunk.slice(0, chunk.search(/<\/Route>|\n\s*<Route\b|\n\s*\{\/\*/) > 0 ? chunk.search(/<\/Route>|\n\s*<Route\b|\n\s*\{\/\*/) : chunk.length);
    const gate = head.match(/<ServedFeature\s+feature="([^"]+)"/)?.[1] ?? null;
    for (const m of head.matchAll(/<([A-Z]\w+)/g)) {
      if (NON_PAGE.has(m[1])) continue;
      const rel = imports.get(m[1]);
      const file = rel && resolveFile(src, rel);
      if (file) {
        routes.push({ path, component: m[1], file, gate });
        break;
      }
    }
  }
  return routes;
}
