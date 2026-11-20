/**
 * Tests for the public proxy's refusal of hub-api `/api/v1/internal/*`, against a real Express
 * app and a stub upstream that records every request it receives (the guard must mean the
 * upstream sees NOTHING for a refused path). Run with `npm test` (node:test).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import express from 'express';
import { createProxy } from './proxy.js';
import { isBlockedApiPath } from './internal-guard.js';

const BLOCKED = [
  '/api/v1/internal',
  '/api/v1/internal/',
  '/api/v1/internal/communities/7/tokens/debit',
  '/api/v1/internal/provider_connections/1',
  '/api/v1/internal?x=1',
  '/API/V1/INTERNAL/communities/7/tokens/debit',
  '/api/v1/Internal/x',
  '/api/v2/internal/x',
  '/api/v1/%69nternal/x',
  '/api/v1/%49nternal/x',
  '/api/%76%31/internal/x',
  '/api/v1/internal%2fcommunities',
  '/api/v1/internal%2Fcommunities',
  '/api/v1%2finternal/x',
  '/api/v1/internal%5ccommunities',
  '/api/v1/%252e%252e/internal/x',
  '/api/v1/%2e%2e/internal/x',
  '/api/v1/foo/../internal/x',
  '/api/v1/./internal/x',
  '/api/v1/foo/%2e%2e/internal/x',
  '/api//v1//internal/x',
  '/api/v1\\internal\\x',
  '/api/v1/internal/%00',
  '/api/v1/%zz',
  '/api/v1/internal#frag',
];

const ALLOWED = [
  '/api/v1/auth/login',
  '/api/v1/communities/abc/tokens',
  '/api/v1/internals',
  '/api/v1/internal-docs',
  '/api/v1/communities/internal',
  '/api/v1/auth/login?next=/api/v1/internal',
  '/api/v1/search?q=internal%2Fx',
];

test('isBlockedApiPath refuses every internal-route variant', () => {
  for (const path of BLOCKED) {
    assert.equal(isBlockedApiPath(path), true, `expected blocked: ${path}`);
  }
});

test('isBlockedApiPath allows normal /api/v1 paths', () => {
  for (const path of ALLOWED) {
    assert.equal(isBlockedApiPath(path), false, `expected allowed: ${path}`);
  }
});

/** Start the proxy app in front of a recording stub upstream. */
async function setup() {
  const seen = [];
  const upstream = http.createServer((req, res) => {
    seen.push(req.url);
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end('upstream');
  });
  await new Promise((r) => upstream.listen(0, '127.0.0.1', r));
  const app = express();
  app.use('/api', createProxy(`http://127.0.0.1:${upstream.address().port}`));
  const server = http.createServer(app);
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { seen, upstream, server, port: server.address().port };
}

/** Send a raw request line so the path reaches Express byte-for-byte (no client normalisation). */
function rawGet(port, path) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(port, '127.0.0.1', () => {
      socket.write(`GET ${path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n`);
    });
    let data = '';
    socket.on('data', (c) => {
      data += c;
    });
    socket.on('end', () => {
      const status = Number(/^HTTP\/1\.1 (\d{3})/.exec(data)?.[1]);
      resolve({ status, body: data.split('\r\n\r\n').slice(1).join('\r\n\r\n') });
    });
    socket.on('error', reject);
  });
}

test('refused internal paths return 404 and are never forwarded upstream', async () => {
  const { seen, upstream, server, port } = await setup();
  try {
    for (const path of BLOCKED.filter((p) => !p.includes('#') && !p.includes('\\'))) {
      const res = await rawGet(port, path);
      assert.equal(res.status, 404, `status for ${path}`);
      assert.ok(!res.body.includes('upstream'), `body for ${path}`);
    }
    assert.deepEqual(seen, [], 'upstream must receive nothing for refused paths');
  } finally {
    server.close();
    upstream.close();
  }
});

test('normal /api/v1 paths still proxy through', async () => {
  const { seen, upstream, server, port } = await setup();
  try {
    for (const path of ALLOWED) {
      const res = await rawGet(port, path);
      assert.equal(res.status, 200, `status for ${path}`);
      assert.ok(res.body.includes('upstream'), `body for ${path}`);
    }
    assert.equal(seen.length, ALLOWED.length);
    assert.ok(seen.includes('/api/v1/auth/login'));
  } finally {
    server.close();
    upstream.close();
  }
});
