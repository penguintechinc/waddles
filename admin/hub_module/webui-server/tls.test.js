/**
 * Real-path TLS tests for the hub-webui in-cluster HTTPS policy (tls.js + proxy.js).
 *
 * A live https upstream (ECDSA P-256 / SHA-256 cert from a throwaway CA, generated with the
 * openssl CLI) is driven through the production proxy. Proves: pinned-CA verification, a wrong
 * CA fails loud as 502 (no plaintext/system-root fallback), plaintext is refused under the
 * policy, TLS >= 1.2 (1.3 here), and ONE pooled keep-alive agent reuses one TCP/TLS connection
 * across requests. Run with `npm test`.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import express from 'express';
import { createProxy } from './proxy.js';
import { assertUpstreamUrl, resolveInternalTls, resolveServerTls } from './tls.js';

/** Run openssl (must exist: a missing binary fails the suite rather than skipping it). */
function openssl(cwd, ...args) {
  execFileSync('openssl', args, { cwd, stdio: 'pipe' });
}

/** Make `<tag>-ca.crt`, `<tag>.crt`, `<tag>.key` (ECDSA P-256, SHA-256, SAN=localhost). */
function makePki(dir, tag) {
  openssl(dir, 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', `${tag}-ca.key`);
  openssl(dir, 'req', '-x509', '-new', '-key', `${tag}-ca.key`, '-sha256', '-days', '1', '-subj', '/CN=test-ca', '-out', `${tag}-ca.crt`);
  openssl(dir, 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', `${tag}.sec1`);
  openssl(dir, 'pkcs8', '-topk8', '-nocrypt', '-in', `${tag}.sec1`, '-out', `${tag}.key`);
  openssl(dir, 'req', '-new', '-key', `${tag}.key`, '-subj', '/CN=localhost', '-out', `${tag}.csr`);
  fs.writeFileSync(path.join(dir, `${tag}.ext`), 'subjectAltName=DNS:localhost\n');
  openssl(dir, 'x509', '-req', '-in', `${tag}.csr`, '-CA', `${tag}-ca.crt`, '-CAkey', `${tag}-ca.key`, '-CAcreateserial', '-sha256', '-days', '1', '-extfile', `${tag}.ext`, '-out', `${tag}.crt`);
  return {
    ca: path.join(dir, `${tag}-ca.crt`),
    cert: path.join(dir, `${tag}.crt`),
    key: path.join(dir, `${tag}.key`),
  };
}

/** GET through the proxy app and return `{status, body}`. */
function get(port, p) {
  return new Promise((resolve, reject) => {
    http
      .get({ host: '127.0.0.1', port, path: p, agent: false }, (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString() }));
      })
      .on('error', reject);
  });
}

/** Start an express app mounting the proxy; returns the http server + port. */
async function startProxy(target, opts) {
  const app = express();
  app.use('/api', createProxy(target, opts));
  const server = http.createServer(app);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { server, port: server.address().port };
}

test('real https upstream: pinned CA ok, TLS>=1.2, wrong CA fails loud, one pooled connection', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'webui-tls-'));
  try {
    const good = makePki(dir, 'good');
    const other = makePki(dir, 'other');
    const serverTls = (() => {
      process.env.TLS_CERT_FILE = good.cert;
      process.env.TLS_KEY_FILE = good.key;
      const opts = resolveServerTls();
      delete process.env.TLS_CERT_FILE;
      delete process.env.TLS_KEY_FILE;
      return opts;
    })();
    assert.equal(serverTls.minVersion, 'TLSv1.2');

    let connections = 0;
    let protocol = '';
    const upstream = https.createServer(serverTls, (req, res) => res.end('ok'));
    upstream.on('secureConnection', (sock) => {
      connections += 1;
      protocol = sock.getProtocol();
    });
    await new Promise((resolve) => upstream.listen(0, '127.0.0.1', resolve));
    const upPort = upstream.address().port;
    const target = `https://localhost:${upPort}`;

    const trusted = resolveInternalTls({ INTERNAL_CA_FILE: good.ca, INTERNAL_TLS_REQUIRED: 'true' });
    const ok = await startProxy(target, trusted);
    for (let i = 0; i < 4; i += 1) {
      const res = await get(ok.port, '/api/v1/x');
      assert.equal(res.status, 200);
      assert.equal(res.body, 'ok');
    }
    assert.equal(connections, 1, '4 proxied requests must reuse ONE pooled TLS connection');
    assert.match(protocol, /^TLSv1\.[23]$/);

    // Wrong CA: handshake must fail -> 502, never a plaintext or system-root fallback.
    const untrusted = resolveInternalTls({ INTERNAL_CA_FILE: other.ca, INTERNAL_TLS_REQUIRED: 'true' });
    const bad = await startProxy(target, untrusted);
    const failed = await get(bad.port, '/api/v1/x');
    assert.equal(failed.status, 502);

    ok.server.close();
    bad.server.close();
    upstream.close();
    upstream.closeAllConnections();
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('policy refuses plaintext upstream and bad configuration (fail loud)', () => {
  assert.throws(() => createProxy('http://hub-api:8204', { required: true }), /plaintext upstream/);
  assert.throws(() => assertUpstreamUrl('http://hub-api:8204', { required: true }), /refused/);
  assert.doesNotThrow(() => assertUpstreamUrl('https://hub-api:8204', { required: true }));
  assert.throws(() => resolveInternalTls({ INTERNAL_TLS_REQUIRED: 'true' }), /neither INTERNAL_CA_PEM nor INTERNAL_CA_FILE/);
  assert.throws(() => resolveInternalTls({ INTERNAL_CA_FILE: '/nonexistent/ca.crt' }), /not readable/);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'webui-tls-bad-'));
  try {
    const junk = path.join(dir, 'junk.crt');
    fs.writeFileSync(junk, 'not a certificate');
    assert.throws(() => resolveInternalTls({ INTERNAL_CA_FILE: junk }), /no PEM block/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  assert.throws(() => resolveInternalTls({ INTERNAL_CA_PEM: 'garbage' }), /no PEM block/);
  assert.ok(resolveInternalTls({ INTERNAL_CA_PEM: '-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----' }).ca);
  assert.throws(() => resolveServerTls({ TLS_CERT_FILE: '/x.crt' }), /must be set together/);
  assert.equal(resolveServerTls({}), null);
});
