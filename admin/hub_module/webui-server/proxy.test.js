/**
 * Tests for the dependency-free /api reverse proxy, run against a real
 * Express app and a stubbed hub-api upstream (no mocks of the http layer).
 * Run with `npm test` (node:test, no extra dependencies).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import express from 'express';
import { createProxy } from './proxy.js';

/** Start an http server on an ephemeral port and return it with its port. */
function listen(server) {
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve(server.address().port));
  });
}

/** Perform a request and collect the full response body as a string. */
function request(port, { method = 'GET', path = '/', headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path, headers }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () =>
        resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString() }),
      );
    });
    req.on('error', reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

/** Start a proxy app pointing at the given upstream URL. */
async function startProxy(target) {
  const app = express();
  app.use('/api', createProxy(target));
  const server = http.createServer(app);
  const port = await listen(server);
  return { server, port };
}

test('forwards method, full /api path, query, headers, body and status; sets upstream Host', async () => {
  let seen;
  const upstream = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      seen = { method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString() };
      res.writeHead(201, { 'Content-Type': 'application/json', 'X-Upstream': 'yes' });
      res.end(JSON.stringify({ ok: true }));
    });
  });
  const upPort = await listen(upstream);
  const { server, port } = await startProxy(`http://127.0.0.1:${upPort}`);
  try {
    const res = await request(port, {
      method: 'POST',
      path: '/api/v1/auth/login?x=1',
      headers: { Authorization: 'Bearer abc', 'Content-Type': 'application/json' },
      body: '{"u":"a"}',
    });
    assert.equal(res.status, 201);
    assert.equal(res.headers['x-upstream'], 'yes');
    assert.equal(res.body, '{"ok":true}');
    assert.equal(seen.method, 'POST');
    assert.equal(seen.url, '/api/v1/auth/login?x=1');
    assert.equal(seen.headers.authorization, 'Bearer abc');
    assert.equal(seen.headers.host, `127.0.0.1:${upPort}`);
    assert.equal(seen.body, '{"u":"a"}');
  } finally {
    server.close();
    upstream.close();
  }
});

test('passes through upstream error statuses unchanged', async () => {
  const upstream = http.createServer((_req, res) => {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('nope');
  });
  const upPort = await listen(upstream);
  const { server, port } = await startProxy(`http://127.0.0.1:${upPort}`);
  try {
    const res = await request(port, { path: '/api/v1/missing' });
    assert.equal(res.status, 404);
    assert.equal(res.body, 'nope');
  } finally {
    server.close();
    upstream.close();
  }
});

test('streams the response incrementally (chunked)', async () => {
  const upstream = http.createServer((_req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write('one\n');
    setTimeout(() => res.end('two\n'), 50);
  });
  const upPort = await listen(upstream);
  const { server, port } = await startProxy(`http://127.0.0.1:${upPort}`);
  try {
    const first = await new Promise((resolve, reject) => {
      http
        .get({ host: '127.0.0.1', port, path: '/api/v1/stream' }, (res) => {
          const chunks = [];
          let firstChunkAt = null;
          res.on('data', (c) => {
            if (firstChunkAt === null) firstChunkAt = Date.now();
            chunks.push(c);
          });
          res.on('end', () => resolve({ body: Buffer.concat(chunks).toString(), firstChunkAt, endAt: Date.now() }));
        })
        .on('error', reject);
    });
    assert.equal(first.body, 'one\ntwo\n');
    assert.ok(first.endAt - first.firstChunkAt >= 30, 'first chunk must arrive before the stream ends');
  } finally {
    server.close();
    upstream.close();
  }
});

test('returns 502 when the upstream is down', async () => {
  // Grab a free port, then close it so nothing is listening there.
  const probe = http.createServer();
  const deadPort = await listen(probe);
  await new Promise((resolve) => probe.close(resolve));
  const { server, port } = await startProxy(`http://127.0.0.1:${deadPort}`);
  try {
    const res = await request(port, { path: '/api/v1/anything' });
    assert.equal(res.status, 502);
    assert.equal(JSON.parse(res.body).error, 'Bad Gateway');
  } finally {
    server.close();
  }
});

test('strips hop-by-hop headers and honours a base path on the target', async () => {
  let seen;
  const upstream = http.createServer((req, res) => {
    seen = req;
    res.end('ok');
  });
  const upPort = await listen(upstream);
  const { server, port } = await startProxy(`http://127.0.0.1:${upPort}/base/`);
  try {
    await request(port, { path: '/api/v1/x', headers: { 'Proxy-Authorization': 'secret', 'X-Custom': 'kept' } });
    assert.equal(seen.url, '/base/api/v1/x');
    assert.equal(seen.headers['proxy-authorization'], undefined);
    assert.equal(seen.headers['x-custom'], 'kept');
  } finally {
    server.close();
    upstream.close();
  }
});
