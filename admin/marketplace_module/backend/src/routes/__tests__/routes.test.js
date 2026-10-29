import { beforeEach, describe, expect, it, jest } from '@jest/globals';
import express from 'express';
import jwt from 'jsonwebtoken';
import request from 'supertest';

const query = jest.fn();
const transaction = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, transaction, default: { query, transaction } }));

const redis = { on: jest.fn(), connect: jest.fn(), publish: jest.fn() };
jest.unstable_mockModule('redis', () => ({ createClient: () => redis }));

const { default: routes } = await import('../index.js');
const { errorHandler, notFoundHandler } = await import('../../middleware/errorHandler.js');
const { config } = await import('../../config/index.js');

const app = express();
app.use(express.json());
app.use('/api/v1', routes);
app.use('/api/', notFoundHandler);
app.use(errorHandler);

const token = (payload) => jwt.sign({ userId: 5, ...payload }, config.jwt.secret);
const userToken = token({ roles: [] });
const adminToken = token({ roles: ['super_admin'], isSuperAdmin: true });

/**
 * Default DB double: every token has an active session; community membership is
 * 'member' (not an admin role); everything else returns an empty result set.
 */
function installDb({ memberRole = 'member' } = {}) {
  query.mockImplementation(async (sql) => {
    if (sql.includes('hub_sessions')) return { rows: [{ id: 1 }] };
    if (sql.includes('community_members')) return { rows: [{ role: memberRole }] };
    if (sql.includes('COUNT(*)') && !sql.includes('GROUP BY')) return { rows: [{ count: '0', total: '0' }] };
    return { rows: [] };
  });
}

const as = (r, t) => (t ? r.set('Authorization', `Bearer ${t}`) : r);

describe('route auth boundaries', () => {
  beforeEach(() => {
    query.mockReset();
    transaction.mockReset();
    installDb();
    jest.spyOn(console, 'log').mockImplementation(() => {});
    jest.spyOn(console, 'error').mockImplementation(() => {});
  });

  const authRequired = [
    ['post', '/api/v1/payments/refunds'],
    ['get', '/api/v1/vendor/dashboard'],
    ['get', '/api/v1/vendor/profile'],
    ['post', '/api/v1/vendor/profile'],
    ['put', '/api/v1/vendor/profile'],
    ['get', '/api/v1/vendor/analytics/sales'],
    ['get', '/api/v1/vendor/analytics/installs'],
    ['get', '/api/v1/vendor/analytics/api-usage'],
    ['get', '/api/v1/vendor/analytics/discount-codes'],
    ['get', '/api/v1/vendor/analytics/communities'],
    ['get', '/api/v1/vendor/analytics/export'],
    ['get', '/api/v1/vendor/analytics/overview'],
    ['get', '/api/v1/vendor/modules'],
    ['post', '/api/v1/vendor/modules'],
    ['put', '/api/v1/vendor/modules/1'],
    ['post', '/api/v1/vendor/modules/1/submit'],
    ['get', '/api/v1/vendor/request'],
    ['post', '/api/v1/vendor/request'],
    ['post', '/api/v1/vendor/discount-codes/validate'],
    ['post', '/api/v1/vendor/discount-codes/redeem'],
    ['get', '/api/v1/vendor/discount-codes'],
    ['post', '/api/v1/vendor/discount-codes'],
    ['put', '/api/v1/vendor/discount-codes/1'],
    ['delete', '/api/v1/vendor/discount-codes/1'],
    ['get', '/api/v1/communities/1/installed'],
    ['post', '/api/v1/communities/1/install'],
    ['delete', '/api/v1/communities/1/install/1'],
    ['put', '/api/v1/communities/1/install/1'],
    ['get', '/api/v1/communities/1/subscriptions'],
    ['post', '/api/v1/communities/1/subscriptions'],
    ['put', '/api/v1/communities/1/subscriptions/1'],
    ['delete', '/api/v1/communities/1/subscriptions/1'],
    ['get', '/api/v1/premium/status/1'],
    ['post', '/api/v1/premium/subscribe'],
    ['post', '/api/v1/premium/cancel'],
    ['get', '/api/v1/admin/marketplace/vendor-requests'],
    ['post', '/api/v1/admin/marketplace/vendor-requests/1/approve'],
    ['post', '/api/v1/admin/marketplace/vendor-requests/1/reject'],
    ['get', '/api/v1/admin/marketplace/submissions'],
    ['post', '/api/v1/admin/marketplace/submissions/1/approve'],
    ['post', '/api/v1/admin/marketplace/submissions/1/reject'],
    ['get', '/api/v1/admin/marketplace/settings'],
    ['put', '/api/v1/admin/marketplace/settings'],
    ['post', '/api/v1/modules'],
    ['put', '/api/v1/modules/1'],
    ['delete', '/api/v1/modules/1'],
    ['get', '/api/v1/modules/1/subscriptions'],
  ];

  it.each(authRequired)('%s %s rejects anonymous callers with 401', async (method, path) => {
    const res = await request(app)[method](path).send({});
    expect(res.status).toBe(401);
  });

  it('rejects a forged token and a token without an active session', async () => {
    expect((await request(app).get('/api/v1/vendor/profile').set('Authorization', 'Bearer nope')).status).toBe(401);
    query.mockImplementation(async () => ({ rows: [] }));
    expect((await as(request(app).get('/api/v1/vendor/profile'), userToken)).status).toBe(401);
  });

  describe('super-admin only routes', () => {
    const adminOnly = [
      ['get', '/api/v1/admin/marketplace/vendor-requests'],
      ['post', '/api/v1/admin/marketplace/submissions/1/approve'],
      ['put', '/api/v1/admin/marketplace/settings'],
      ['delete', '/api/v1/modules/1'],
      ['get', '/api/v1/modules/1/subscriptions'],
    ];
    it.each(adminOnly)('%s %s is forbidden for ordinary users', async (method, path) => {
      const res = await as(request(app)[method](path), userToken).send({});
      expect(res.status).toBe(403);
    });

    it('allows a super admin through to the controller', async () => {
      const res = await as(request(app).get('/api/v1/admin/marketplace/settings'), adminToken);
      expect(res.status).toBe(200);
      expect(res.body).toEqual({ success: true, settings: {} });
    });

    it('validates module creation input before reaching the controller', async () => {
      const res = await as(request(app).post('/api/v1/modules'), adminToken).send({ name: 'x' });
      expect(res.status).toBe(400);
      const upd = await as(request(app).put('/api/v1/modules/1'), adminToken).send({ iconUrl: 'not a url' });
      expect(upd.status).toBe(400);
    });
  });

  describe('community admin routes', () => {
    it('forbid plain community members', async () => {
      for (const [method, path] of [
        ['get', '/api/v1/communities/1/installed'],
        ['post', '/api/v1/communities/1/install'],
        ['get', '/api/v1/communities/1/subscriptions'],
        ['delete', '/api/v1/communities/1/subscriptions/1'],
      ]) {
        const res = await as(request(app)[method](path), userToken).send({});
        expect(res.status).toBe(403);
      }
    });

    it('premium subscribe/cancel currently reject every non-super-admin caller (communityId is read from the body, the guard needs a path param)', async () => {
      installDb({ memberRole: 'community-owner' });
      for (const path of ['/api/v1/premium/subscribe', '/api/v1/premium/cancel']) {
        const res = await as(request(app).post(path), userToken).send({ communityId: 1 });
        expect(res.status).toBe(400);
      }
    });

    it('allow community admins', async () => {
      installDb({ memberRole: 'community-admin' });
      const res = await as(request(app).get('/api/v1/communities/1/installed'), userToken);
      expect(res.status).toBe(200);
      expect(res.body).toMatchObject({ success: true, installed: [], total: 0 });
      const subs = await as(request(app).get('/api/v1/communities/1/subscriptions'), userToken);
      expect(subs.status).toBe(200);
    });

    it('validates subscription payloads', async () => {
      installDb({ memberRole: 'community-owner' });
      const res = await as(request(app).post('/api/v1/communities/1/subscriptions'), userToken).send({ moduleId: 0 });
      expect(res.status).toBe(400);
      const upd = await as(request(app).put('/api/v1/communities/1/subscriptions/1'), userToken).send({ isEnabled: 'maybe' });
      expect(upd.status).toBe(400);
    });
  });

  describe('internal service routes', () => {
    it('require the service API key', async () => {
      expect((await request(app).get('/api/v1/internal/commands/1')).status).toBe(401);
      expect((await request(app).get('/api/v1/internal/commands/1').set('x-api-key', 'wrong')).status).toBe(401);
      expect((await request(app).post('/api/v1/internal/execute/1').send({})).status).toBe(401);
    });
    it('serve commands to a service caller', async () => {
      const res = await request(app).get('/api/v1/internal/commands/1').set('x-api-key', config.serviceApiKey);
      expect(res.status).toBe(200);
      expect(res.body).toEqual({ success: true, commands: [] });
    });
  });

  describe('public routes', () => {
    it('browse modules and catalog anonymously', async () => {
      expect((await request(app).get('/api/v1/modules')).status).toBe(200);
      expect((await request(app).get('/api/v1/catalog')).status).toBe(200);
      expect((await request(app).get('/api/v1/catalog/categories')).body).toEqual({ success: true, categories: [] });
      expect((await request(app).get('/api/v1/catalog/featured')).body).toEqual({ success: true, modules: [] });
      expect((await request(app).get('/api/v1/catalog/core/1')).status).toBe(404);
      expect((await request(app).get('/api/v1/modules/1')).status).toBe(404);
    });
    it('catalog accepts an authenticated caller too', async () => {
      expect((await as(request(app).get('/api/v1/catalog'), userToken)).status).toBe(200);
    });
    it('premium pricing is public', async () => {
      const res = await request(app).get('/api/v1/premium/pricing');
      expect(res.status).toBe(200);
      expect(res.body.pricing).toEqual({ basePriceCents: 0, baseSeatLimit: 0, overagePriceCents: 0 });
    });
    it('payment provider metadata is reachable', async () => {
      const res = await request(app).get('/api/v1/payments/providers');
      expect(res.body.providers).toEqual(['stripe', 'paypal']);
      expect((await request(app).get('/api/v1/payments/config/validate/stripe')).status).toBe(200);
    });
    it('webhook routes reject unverifiable payloads', async () => {
      expect((await request(app).post('/api/v1/webhooks/stripe').send({})).status).toBe(400);
      expect((await request(app).post('/api/v1/webhooks/paypal').send({})).status).toBe(400);
    });
    it('unknown API paths return the 404 envelope', async () => {
      const res = await request(app).get('/api/v1/nope');
      expect(res.status).toBe(404);
      expect(res.body.error.code).toBe('NOT_FOUND');
    });
  });

  describe('payment routes delegate to the controller', () => {
    it('checkout validates input', async () => {
      expect((await request(app).post('/api/v1/payments/checkout').send({})).status).toBe(400);
      expect((await request(app).post('/api/v1/payments/complete').send({})).status).toBe(400);
      expect((await request(app).post('/api/v1/payments/subscriptions').send({})).status).toBe(400);
      expect((await request(app).post('/api/v1/payments/customers').send({})).status).toBe(400);
    });
    it('refund requires provider and payment id for authenticated users', async () => {
      const res = await as(request(app).post('/api/v1/payments/refunds'), userToken).send({});
      expect(res.status).toBe(400);
    });
    it('provider lookups for unsupported providers surface as 500s', async () => {
      for (const path of [
        '/api/v1/payments/venmo/1',
        '/api/v1/payments/subscriptions/venmo/1',
        '/api/v1/payments/refunds/venmo/1',
        '/api/v1/payments/customers/venmo/1',
        '/api/v1/payments/customers/venmo/1/payment-methods',
      ]) {
        expect((await request(app).get(path)).status).toBe(500);
      }
      for (const path of ['/api/v1/payments/subscriptions/venmo/1/cancel', '/api/v1/payments/subscriptions/venmo/1/reactivate']) {
        expect((await request(app).post(path).send({})).status).toBe(500);
      }
    });
  });
});
