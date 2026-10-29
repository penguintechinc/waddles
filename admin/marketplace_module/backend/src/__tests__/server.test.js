import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';
import request from 'supertest';

/**
 * Loads src/server.js fresh with app.listen intercepted so no port is bound,
 * and process-level handlers captured for direct invocation.
 */
async function loadServer() {
  jest.resetModules();
  const fakeServer = { close: jest.fn((cb) => cb && cb()) };
  let onListen;
  const { default: freshExpress } = await import('express');
  jest.spyOn(freshExpress.application, 'listen').mockImplementation((_port, cb) => { onListen = cb; return fakeServer; });
  const handlers = {};
  jest.spyOn(process, 'on').mockImplementation((evt, fn) => { handlers[evt] = fn; return process; });
  const exit = jest.spyOn(process, 'exit').mockImplementation(() => {});
  const mod = await import('../server.js');
  return { app: mod.default, fakeServer, handlers, exit, onListen: () => onListen() };
}

describe('server.js (standalone payments server)', () => {
  beforeEach(() => {
    jest.spyOn(console, 'log').mockImplementation(() => {});
    jest.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
    delete process.env.ALLOWED_ORIGINS;
  });

  it('serves a health check', async () => {
    const { app } = await loadServer();
    const res = await request(app).get('/health');
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('ok');
  });

  it('returns a JSON 404 for unknown paths', async () => {
    const { app } = await loadServer();
    const res = await request(app).get('/nope');
    expect(res.status).toBe(404);
    expect(res.body).toMatchObject({ success: false, error: 'Not found', path: '/nope' });
  });

  it('mounts payment and webhook routes', async () => {
    const { app } = await loadServer();
    expect((await request(app).get('/api/payments/providers')).body.providers).toEqual(['stripe', 'paypal']);
    expect((await request(app).post('/api/webhooks/paypal').send({})).status).toBe(400);
  });

  it('does not leak error details outside development', async () => {
    const { app } = await loadServer();
    const res = await request(app).post('/api/payments/checkout').set('Content-Type', 'application/json').send('{bad');
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ success: false, error: 'Internal server error' });
  });

  it('includes error details and stack in development', async () => {
    const prev = process.env.NODE_ENV;
    process.env.NODE_ENV = 'development';
    try {
      const { app } = await loadServer();
      const res = await request(app).post('/api/payments/checkout').set('Content-Type', 'application/json').send('{bad');
      expect(res.body.error).toContain('JSON');
      expect(res.body.stack).toBeDefined();
    } finally {
      process.env.NODE_ENV = prev;
    }
  });

  it('limits CORS to configured origins', async () => {
    process.env.ALLOWED_ORIGINS = 'https://a.example,https://b.example';
    const { app } = await loadServer();
    const ok = await request(app).get('/health').set('Origin', 'https://a.example');
    expect(ok.headers['access-control-allow-origin']).toBe('https://a.example');
    const bad = await request(app).get('/health').set('Origin', 'https://evil.example');
    expect(bad.headers['access-control-allow-origin']).toBeUndefined();
  });

  it('reports provider configuration when it starts listening', async () => {
    const { onListen } = await loadServer();
    onListen();
    const logged = console.log.mock.calls.map((c) => String(c[0])).join('\n');
    expect(logged).toContain('STRIPE');
    expect(logged).toContain('PAYPAL');
  });

  it('reports a correctly configured provider as OK', async () => {
    const keys = { STRIPE_SECRET_KEY: 'k', STRIPE_WEBHOOK_SECRET: 'w' };
    Object.assign(process.env, keys);
    try {
      const { onListen } = await loadServer();
      onListen();
      expect(console.log.mock.calls.some((c) => String(c[0]).includes('STRIPE: Configured correctly'))).toBe(true);
    } finally {
      Object.keys(keys).forEach((k) => delete process.env[k]);
    }
  });

  it.each(['SIGTERM', 'SIGINT'])('closes the server and exits 0 on %s', async (signal) => {
    jest.useFakeTimers();
    const { handlers, fakeServer, exit } = await loadServer();
    handlers[signal]();
    expect(fakeServer.close).toHaveBeenCalled();
    expect(exit).toHaveBeenCalledWith(0);
  });

  it('force-exits 1 when connections do not drain within 10s', async () => {
    jest.useFakeTimers();
    const { handlers, fakeServer, exit } = await loadServer();
    fakeServer.close.mockImplementation(() => {});
    handlers.SIGTERM();
    expect(exit).not.toHaveBeenCalled();
    jest.advanceTimersByTime(10000);
    expect(exit).toHaveBeenCalledWith(1);
  });

  it('shuts down on uncaught exceptions and only logs unhandled rejections', async () => {
    jest.useFakeTimers();
    const { handlers, exit } = await loadServer();
    handlers.unhandledRejection(new Error('r'), Promise.resolve());
    expect(exit).not.toHaveBeenCalled();
    handlers.uncaughtException(new Error('boom'));
    expect(exit).toHaveBeenCalledWith(0);
  });
});
