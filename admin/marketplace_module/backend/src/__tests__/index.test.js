import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';
import http from 'node:http';
import request from 'supertest';

const flush = () => new Promise((r) => setTimeout(r, 0));

/**
 * Loads src/index.js fresh with the database and HTTP server replaced by doubles,
 * returning the Express app plus handles to drive startup/shutdown paths.
 */
async function loadIndex({ queryImpl = async () => ({ rows: [] }), healthy = true } = {}) {
  jest.resetModules();
  const db = {
    query: jest.fn(queryImpl),
    transaction: jest.fn(),
    checkConnection: jest.fn(async () => healthy),
    closePool: jest.fn(async () => {}),
    getPoolMetrics: jest.fn(() => ({ totalConnections: 1 })),
  };
  jest.unstable_mockModule('../config/database.js', () => ({ ...db, default: db }));
  // The app's own listen(port, host, cb) call is intercepted; supertest's ephemeral listens pass through.
  const server = { listen: jest.fn(), close: jest.fn(), on: jest.fn() };
  const realListen = http.Server.prototype.listen;
  const realClose = http.Server.prototype.close;
  const realOn = http.Server.prototype.on;
  jest.spyOn(http.Server.prototype, 'listen').mockImplementation(function listen(...args) {
    return typeof args[1] === 'string' ? server.listen(...args) : realListen.apply(this, args);
  });
  jest.spyOn(http.Server.prototype, 'close').mockImplementation(function close(...args) {
    return this.listening ? realClose.apply(this, args) : server.close(...args);
  });
  jest.spyOn(http.Server.prototype, 'on').mockImplementation(function on(evt, fn) {
    // Only the app's own 'Server error' handler is captured; every other listener is real.
    return evt === 'error' && fn.toString().includes('Server error')
      ? server.on(evt, fn)
      : realOn.call(this, evt, fn);
  });
  const handlers = {};
  jest.spyOn(process, 'on').mockImplementation((evt, fn) => { handlers[evt] = fn; return process; });
  const exit = jest.spyOn(process, 'exit').mockImplementation(() => {});
  const mod = await import('../index.js');
  await flush();
  return { app: mod.default, db, server, handlers, exit };
}

describe('index.js (application entry point)', () => {
  beforeEach(() => {
    jest.spyOn(console, 'log').mockImplementation(() => {});
    jest.spyOn(console, 'error').mockImplementation(() => {});
    jest.spyOn(console, 'warn').mockImplementation(() => {});
  });
  afterEach(() => jest.restoreAllMocks());

  it('verifies the schema and starts listening on the configured port', async () => {
    const { db, server } = await loadIndex();
    expect(db.query).toHaveBeenCalledTimes(3);
    expect(server.listen).toHaveBeenCalledTimes(1);
    const [port, host, cb] = server.listen.mock.calls[0];
    expect(typeof port).toBe('number');
    expect(typeof host).toBe('string');
    cb();
  });

  it('exits non-zero when schema verification fails', async () => {
    const { exit, server } = await loadIndex({ queryImpl: async () => { throw new Error('no db'); } });
    expect(exit).toHaveBeenCalledWith(1);
    expect(server.listen).not.toHaveBeenCalled();
  });

  it('exits non-zero on HTTP server errors', async () => {
    const { server, exit } = await loadIndex();
    const errorHandler = server.on.mock.calls.find((c) => c[0] === 'error')[1];
    errorHandler(new Error('EADDRINUSE'));
    expect(exit).toHaveBeenCalledWith(1);
  });

  it.each(['SIGTERM', 'SIGINT'])('shuts down gracefully on %s', async (signal) => {
    const { server, db, handlers, exit } = await loadIndex();
    await handlers[signal]();
    expect(server.close).toHaveBeenCalled();
    expect(db.closePool).toHaveBeenCalled();
    expect(exit).toHaveBeenCalledWith(0);
  });

  describe('HTTP surface', () => {
    it('reports healthy when the database is reachable', async () => {
      const { app } = await loadIndex();
      const res = await request(app).get('/health');
      expect(res.status).toBe(200);
      expect(res.body).toMatchObject({ status: 'healthy', database: 'connected', module: 'marketplace_module' });
    });

    it('reports degraded (503) when the database is down', async () => {
      const { app } = await loadIndex({ healthy: false });
      const res = await request(app).get('/health');
      expect(res.status).toBe(503);
      expect(res.body).toMatchObject({ status: 'degraded', database: 'disconnected' });
    });

    it('serves pool metrics', async () => {
      const { app } = await loadIndex();
      const res = await request(app).get('/metrics');
      expect(res.status).toBe(200);
      expect(res.body.database).toEqual({ pool: { totalConnections: 1 }, health: true });
      expect(typeof res.body.uptime).toBe('number');
    });

    it('sets security headers and an XSRF cookie', async () => {
      const { app } = await loadIndex();
      const res = await request(app).get('/health');
      expect(res.headers['strict-transport-security']).toContain('max-age=31536000');
      expect(res.headers['content-security-policy']).toContain("object-src 'none'");
      expect(res.headers['set-cookie'].join(';')).toContain('XSRF-TOKEN=');
    });

    it('returns the 404 envelope for unknown API routes', async () => {
      const { app } = await loadIndex();
      const res = await request(app).get('/api/unknown');
      expect(res.status).toBe(404);
      expect(res.body.error.code).toBe('NOT_FOUND');
    });

    it('rate limits API traffic with the standard envelope headers', async () => {
      const { app } = await loadIndex();
      const res = await request(app).get('/api/v1/premium/pricing');
      expect(res.headers).toHaveProperty('ratelimit-limit');
    });

    it('rejects cookie-authenticated state changes without a CSRF token', async () => {
      const { app } = await loadIndex();
      const res = await request(app).post('/api/v1/payments/checkout').set('Cookie', 'token=abc').send({});
      expect(res.status).toBe(403);
      expect(res.body.error.code).toBe('CSRF_ERROR');
    });

    it('routes errors through the global error handler', async () => {
      const { app } = await loadIndex();
      const res = await request(app).post('/api/v1/webhooks/stripe').set('Content-Type', 'application/json').send('{bad json');
      expect(res.status).toBe(400);
      expect(res.body.success).toBe(false);
    });
  });
});
