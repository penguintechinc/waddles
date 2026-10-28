/**
 * Accessibility ratchet for hub-webui sources. Budgets are zero: every category
 * below was driven to 0 and must stay there. The denominator checks make the
 * gate fail loudly if the scanner ever points at the wrong root.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { analyzeSource } from '../lib/analyze.mjs';
import { routeSurvey } from '../lib/routes.mjs';

const SRC = join(process.cwd(), 'src');

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name !== '__tests__' && name !== 'test') walk(p, out);
    } else if (/\.(jsx|tsx)$/.test(name) && !/\.test\./.test(name)) out.push(p);
  }
  return out;
}

const files = walk(SRC);
const totals = { unlabeled: [], iconButtons: [], clickable: [], overlays: [], outlineNoReplacement: [], imgNoAlt: [] };
for (const file of files) {
  const f = analyzeSource(readFileSync(file, 'utf8'), file);
  const rel = file.slice(SRC.length + 1);
  f.unlabeled.forEach((x) => totals.unlabeled.push(`${rel}:${x.line}`));
  f.iconButtons.forEach((x) => totals.iconButtons.push(`${rel}:${x.line}`));
  f.clickable.forEach((x) => totals.clickable.push(`${rel}:${x.line}`));
  f.overlays.forEach((x) => totals.overlays.push(`${rel}:${x.line}`));
  f.imgNoAlt.forEach((x) => totals.imgNoAlt.push(`${rel}:${x.line}`));
  f.outline.filter((o) => !o.replaced).forEach((o) => totals.outlineNoReplacement.push(`${rel}:${o.line}`));
}

describe('accessibility budget (all zero)', () => {
  it('examined a meaningful number of files and routes (denominator)', () => {
    expect(files.length).toBeGreaterThan(150);
    expect(routeSurvey(SRC).length).toBeGreaterThan(100);
  });

  it.each(Object.keys(totals))('has no %s findings', (key) => {
    expect(totals[key]).toEqual([]);
  });

  it('keeps the global reduced-motion and focus-visible rules and scans the shared lib', () => {
    const css = readFileSync(join(SRC, 'index.css'), 'utf8');
    expect(css).toMatch(/prefers-reduced-motion:\s*reduce/);
    expect(css).toMatch(/:focus-visible\s*\{/);
    const tailwind = readFileSync(join(process.cwd(), 'tailwind.config.js'), 'utf8');
    expect(tailwind).toContain('@penguintechinc/react-libs/dist');
  });
});
