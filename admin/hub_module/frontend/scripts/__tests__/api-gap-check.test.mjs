/**
 * Pins the api-gap-check gate: it must FAIL (exit 1) when any HTTP call path is not under
 * `/api/v1/` -- including `fetch`, services/*, and the unprefixed `/vendor`, `/admin/vendor`,
 * `/ai-knowledge` shapes -- while health/static paths and SPA route literals stay allowed, and
 * route alternations / mixed `${var}` segments behave as wildcards. It also fails (exit 2) when
 * it examined nothing.
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, describe, expect, it } from 'vitest';
import { buildIsServed } from '../lib/backend-routes.mjs';

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), '..', 'api-gap-check.mjs');
const roots = [];
afterAll(() => roots.forEach((r) => rmSync(r, { recursive: true, force: true })));

/** Builds a throwaway `src/` tree from `{ relPath: contents }` and runs the gate against it. */
function run(files) {
  const root = mkdtempSync(join(tmpdir(), 'api-gap-'));
  roots.push(root);
  mkdirSync(join(root, 'lib'), { recursive: true });
  writeFileSync(join(root, 'lib/unservedFeatures.json'), '{}');
  for (const [rel, body] of Object.entries(files)) {
    mkdirSync(dirname(join(root, rel)), { recursive: true });
    writeFileSync(join(root, rel), body);
  }
  return spawnSync(process.execPath, [SCRIPT, '--list'], { env: { ...process.env, API_GAP_SRC: root }, encoding: 'utf8' });
}

const GOOD = "api.get('/api/v1/auth/me');\n";

describe('api-gap-check gate', () => {
  it('passes when every call is under /api/v1/ (health/static and SPA routes allowed)', () => {
    const r = run({
      'pages/Ok.jsx': `${GOOD}fetch('/health');\napi.get('/assets/x.json');\nnavigate('/vendor/request');\n`,
    });
    expect(r.status, r.stderr).toBe(0);
    expect(JSON.parse(r.stdout.slice(0, r.stdout.indexOf('\n}') + 2)).frontendCallsMissingApiPrefix).toBe(0);
  });

  it.each([
    ["api.get('/vendor/modules')", '/vendor/modules'],
    ["api.post(`/admin/vendor/requests/${id}/approve`, body)", '/admin/vendor/requests/${id}/approve'],
    ["api.get('/ai-knowledge/sources')", '/ai-knowledge/sources'],
    ["fetch('/api/v2/things')", '/api/v2/things'],
    ["apiClient.delete<void>('/legacy/x')", '/legacy/x'],
  ])('FAILS on %s', (call, path) => {
    const r = run({ 'pages/Ok.jsx': GOOD, 'pages/Bad.jsx': `${call};\n` });
    expect(r.status).toBe(1);
    expect(r.stderr).toMatch(/FAIL: 1 call\(s\) do not start with \/api\/v1\//);
    expect(r.stdout).toContain(path);
  });

  it('scans services/* too', () => {
    const r = run({ 'pages/Ok.jsx': GOOD, 'services/thing.js': "export const t = { x: () => api.get('/vendor/dashboard') };\n" });
    expect(r.status).toBe(1);
    expect(r.stdout).toContain('services/thing.js');
  });

  it('exits 2 when no frontend calls were examined', () => {
    const r = run({ 'pages/Empty.jsx': 'export default 1;\n' });
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/examined 0 frontend calls/);
  });
});

describe('buildIsServed wildcards', () => {
  const served = buildIsServed([
    { method: 'GET', path: '/api/v1/auth/(discord|twitch)/link', file: 'a.py' },
    { method: 'GET', path: '/api/v1/x/<p>/y', file: 'b.py' },
    { method: 'GET', path: '/api/v1/domains/<p>', file: 'c.py' },
  ]);

  it('treats a ${var} segment as a wildcard against a route alternation', () => {
    expect(served({ method: 'GET', path: '/api/v1/auth/<p>/link' })).toBe(true);
    expect(served({ method: 'GET', path: '/api/v1/auth/discord/link' })).toBe(true);
    expect(served({ method: 'GET', path: '/api/v1/auth/slack/link' })).toBe(false);
    expect(served({ method: 'GET', path: '/api/v1/auth/dis<p>/link' })).toBe(true);
    expect(served({ method: 'GET', path: '/api/v1/auth/zzz<p>/link' })).toBe(false);
  });

  it('matches params segment-wise and never lets a ${var} stand in for a literal', () => {
    expect(served({ method: 'GET', path: '/api/v1/x/<p>/y' })).toBe(true);
    expect(served({ method: 'GET', path: '/api/v1/x/<p>/y/extra' })).toBe(false);
    expect(served({ method: 'POST', path: '/api/v1/x/<p>/y' })).toBe(false);
    expect(served({ method: 'GET', path: '/api/v1/<p>/domains' })).toBe(false);
  });
});
