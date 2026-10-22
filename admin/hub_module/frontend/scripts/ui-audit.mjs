#!/usr/bin/env node
/**
 * Hub-webui static UI audit: route/page survey plus accessibility counters.
 *
 * Reproduces the release-audit accessibility numbers (unlabeled inputs, images
 * without alt, outline removal, clickable non-interactive elements, reduced
 * motion) so before/after counts are comparable, and classifies every routed
 * page as functional / stub so the functional-gap roadmap stays current.
 *
 * Usage: node scripts/ui-audit.mjs [--json] [--pages]
 * Exits non-zero when zero source files were examined (a gate that cannot
 * fail is not a gate).
 */
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { routeSurvey } from './lib/routes.mjs';
import { analyzeSource } from './lib/analyze.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const args = new Set(process.argv.slice(2));

/** Recursively list source files, skipping tests. */
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name === '__tests__' || name === 'test') continue;
      walk(p, out);
    } else if (/\.(jsx|tsx)$/.test(name) && !/\.test\./.test(name)) {
      out.push(p);
    }
  }
  return out;
}

/** Inspect one file for accessibility findings via the shared AST analyzer. */
function auditFile(file) {
  const f = analyzeSource(readFileSync(file, 'utf8'), file);
  return {
    unlabeled: f.unlabeled.map((x) => x.line),
    imgNoAlt: f.imgNoAlt.map((x) => x.line),
    iconButtons: f.iconButtons.map((x) => x.line),
    clickable: f.clickable.map((x) => x.line),
    outlineNone: f.outline.length,
    outlineNoReplacement: f.outline.filter((x) => !x.replaced).length,
    animate: f.animate,
  };
}

/** Classify a page file. */
function classify(file) {
  const src = readFileSync(file, 'utf8');
  const bytes = Buffer.byteLength(src);
  const stub = /coming soon|placeholder for|coming after|under construction|not available yet/i.test(src);
  const usesQuery = /useQuery|useMutation/.test(src);
  const usesApi = /from ['"][./]*(?:services|lib)\/|apiClient|axios/.test(src);
  const effectFetch = /useEffect/.test(src) && usesApi && !usesQuery;
  const hasLoading = /loading|isLoading|isPending|spinner|animate-spin|skeleton/i.test(src);
  const hasError = /error|isError/i.test(src) && /setError|isError|error\b/.test(src);
  const hasEmpty = /no \w+ (yet|found|available)|empty|nothing/i.test(src);
  let status = 'functional';
  if (stub && bytes < 3000) status = 'stub';
  else if (stub) status = 'partial';
  else if (bytes < 1200 && !usesApi) status = 'stub';
  else if (!usesApi && bytes < 4000) status = 'static';
  return { bytes, status, usesQuery, effectFetch, hasLoading, hasError, hasEmpty };
}

const files = walk(SRC);
if (files.length === 0) {
  console.error('FAIL: zero source files examined');
  process.exit(2);
}

const totals = { unlabeled: 0, imgNoAlt: 0, iconButtons: 0, outlineNone: 0, outlineNoReplacement: 0, clickable: 0, animate: 0 };
const perFile = [];
for (const f of files) {
  const r = auditFile(f);
  totals.unlabeled += r.unlabeled.length;
  totals.imgNoAlt += r.imgNoAlt.length;
  totals.iconButtons += r.iconButtons.length;
  totals.outlineNone += r.outlineNone;
  totals.outlineNoReplacement += r.outlineNoReplacement;
  totals.clickable += r.clickable.length;
  totals.animate += r.animate;
  perFile.push({ file: relative(ROOT, f), ...r });
}

const css = readFileSync(join(SRC, 'index.css'), 'utf8');
const reducedMotionCss = /prefers-reduced-motion/.test(css);
const reducedMotionTw = files.filter((f) => /motion-reduce:|motion-safe:/.test(readFileSync(f, 'utf8'))).length;

const routes = routeSurvey(SRC);
const pages = routes.map((r) => ({ ...r, file: relative(ROOT, r.file), ...classify(r.file) }));
const byStatus = pages.reduce((a, p) => ((a[p.status] = (a[p.status] || 0) + 1), a), {});
const pattern = {
  useQueryPages: pages.filter((p) => p.usesQuery).length,
  effectFetchPages: pages.filter((p) => p.effectFetch).length,
  noLoadingState: pages.filter((p) => p.status !== 'stub' && !p.hasLoading).length,
  noErrorState: pages.filter((p) => p.status !== 'stub' && !p.hasError).length,
  noEmptyState: pages.filter((p) => p.status !== 'stub' && !p.hasEmpty).length,
};

const summary = {
  filesExamined: files.length,
  routesExamined: routes.length,
  a11y: { ...totals, reducedMotionInCss: reducedMotionCss, filesWithMotionVariants: reducedMotionTw },
  routesByStatus: byStatus,
  pattern,
};

if (args.has('--json')) {
  console.log(JSON.stringify({ summary, pages, perFile: perFile.filter((f) => f.unlabeled.length || f.imgNoAlt.length || f.clickable.length || f.iconButtons.length) }, null, 2));
} else {
  console.log(JSON.stringify(summary, null, 2));
  if (args.has('--pages')) {
    for (const p of pages) console.log(`${p.status.padEnd(10)} ${p.path.padEnd(60)} ${p.file}`);
  }
}
