/**
 * Pins the hub-api route discovery the audit and the unserved-registry guard rely on: path
 * normalisation, blueprint-prefix resolution, the `_register(bp)` factory style (which once hid
 * served stream routes as "gaps"), and that the real blueprints still yield a sane route set.
 */
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';
import { backendRoutes, buildIsServed, norm } from '../lib/backend-routes.mjs';

describe('norm', () => {
  it('collapses every param style to <p> and drops query/trailing slash', () => {
    expect(norm('/api/v1/a/${id}/b/<int:x>/c/:name/')).toBe('/api/v1/a/<p>/b/<p>/c/<p>');
    expect(norm('/api/v1/x?y=1')).toBe('/api/v1/x');
    expect(norm('/api/v1/a:b')).toBe('/api/v1/a:b');
  });
});

describe('backendRoutes (synthetic blueprints)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'webui-routes-'));
  afterAll(() => rmSync(dir, { recursive: true, force: true }));

  writeFileSync(
    join(dir, 'plain.py'),
    [
      'plain_bp = Blueprint("v1_plain", __name__, url_prefix="/api/v1/plain")',
      '@plain_bp.route("/<int:thing_id>", methods=["GET", "DELETE"])',
      'async def thing(): ...',
      '@plain_bp.route("/list")',
      'async def lst(): ...',
    ].join('\n'),
  );
  writeFileSync(
    join(dir, 'factory.py'),
    [
      'one_bp = Blueprint("v1_one", __name__, url_prefix="/api/v1/one")',
      'two_bp = Blueprint("v1_two", __name__, url_prefix="/api/v1/two")',
      'def _register(bp):',
      '    @bp.route("/<int:cid>/streams", methods=["GET"])',
      '    async def streams(): ...',
      '_register(one_bp)',
      '_register(two_bp)',
    ].join('\n'),
  );

  const routes = backendRoutes(dir);
  const served = buildIsServed(routes);

  it('resolves prefixes, methods and the default GET', () => {
    expect(served({ method: 'GET', path: '/api/v1/plain/<p>' })).toBe(true);
    expect(served({ method: 'DELETE', path: '/api/v1/plain/<p>' })).toBe(true);
    expect(served({ method: 'POST', path: '/api/v1/plain/<p>' })).toBe(false);
    expect(served({ method: 'GET', path: '/api/v1/plain/list' })).toBe(true);
  });

  it('attributes factory-registered routes to every blueprint in the file', () => {
    expect(served({ method: 'GET', path: '/api/v1/one/<p>/streams' })).toBe(true);
    expect(served({ method: 'GET', path: '/api/v1/two/<p>/streams' })).toBe(true);
  });

  it('never reports unrelated paths as served', () => {
    expect(served({ method: 'GET', path: '/api/v1/three/<p>/streams' })).toBe(false);
  });
});

describe('backendRoutes (real hub-api blueprints)', () => {
  const routes = backendRoutes();
  const served = buildIsServed(routes);

  it('examines a plausible number of routes', () => {
    expect(routes.length).toBeGreaterThan(300);
  });

  it('knows the routes the typed services call', () => {
    for (const [method, path] of [
      ['GET', '/api/v1/auth/me'],
      ['GET', '/api/v1/public/communities/<p>'],
      ['GET', '/api/v1/community/<p>/leaderboard/watch-time'],
      ['GET', '/api/v1/community/<p>/activity/my-stats'],
      ['GET', '/api/v1/community/<p>/live-activity'],
      ['GET', '/api/v1/community/<p>/reputation/leaderboard'],
      ['GET', '/api/v1/communities/<p>/streams'],
      ['GET', '/api/v1/admin/<p>/members'],
      ['POST', '/api/v1/<p>/join-requests'],
      ['GET', '/api/v1/<p>/join-requests/mine'],
      ['PUT', '/api/v1/admin/<p>/join-requests/<p>/approve'],
      ['POST', '/api/v1/superadmin/communities'],
    ]) {
      expect(served({ method, path }), `${method} ${path}`).toBe(true);
    }
  });

  it('does not serve the legacy paths the UI used to call', () => {
    for (const [method, path] of [
      ['GET', '/api/v1/communities/my'],
      ['GET', '/api/v1/communities/<p>/dashboard'],
      ['GET', '/api/v1/communities/<p>/leaderboard/watch-time'],
      ['POST', '/api/v1/communities/<p>/join'],
      ['POST', '/api/v1/admin/<p>/join-requests/<p>/approve'],
      ['GET', '/api/v1/admin/<p>/reputation/leaderboard'],
    ]) {
      expect(served({ method, path }), `${method} ${path}`).toBe(false);
    }
  });
});
