/**
 * Vendor Execution Service — proxy command execution to vendor modules
 */
// Node >=18 (see package.json engines) ships a global fetch; node-fetch was never a dependency.
const fetch = (...args) => globalThis.fetch(...args);
import crypto from 'crypto';
import { query } from '../config/database.js';
import { logger } from '../utils/logger.js';
import { errors } from '../middleware/errorHandler.js';
import { assertSafeUrl } from '../utils/safeUrl.js';

export const MAX_TIMEOUT_MS = 10000;
export const DEFAULT_TIMEOUT_MS = 5000;
export const MAX_RESPONSE_BYTES = 1024 * 1024;

/** Clamps a vendor-supplied timeout into (0, MAX_TIMEOUT_MS], defaulting when invalid. */
export function clampTimeout(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_TIMEOUT_MS;
  return Math.min(n, MAX_TIMEOUT_MS);
}

export async function getModuleConfig(moduleId) {
  const { rows } = await query(
    `SELECT id, name, webhook_url, webhook_secret, webhook_timeout_ms,
            communication_model, auth_type, auth_config, api_base_url
     FROM marketplace_modules
     WHERE id=$1 AND status='approved' AND deleted_at IS NULL
     LIMIT 1`,
    [moduleId]
  );

  return rows[0] || null;
}

/** Reads a response body as a stream, aborting once it exceeds maxBytes. */
async function readBodyCapped(resp, maxBytes) {
  const declared = Number(resp.headers?.get?.('content-length'));
  if (Number.isFinite(declared) && declared > maxBytes) {
    throw new Error('Vendor response too large');
  }
  if (!resp.body || typeof resp.body.getReader !== 'function') {
    throw new Error('Vendor response has no readable body');
  }
  const reader = resp.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel().catch(() => {});
      throw new Error('Vendor response too large');
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString('utf8');
}

/** Drops prototype-pollution keys and rejects non-object vendor payloads. */
function sanitizeVendorData(data) {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('Vendor module returned an invalid response');
  }
  const clean = {};
  for (const [k, v] of Object.entries(data)) {
    if (k === '__proto__' || k === 'constructor' || k === 'prototype') continue;
    clean[k] = v;
  }
  return clean;
}

/**
 * SSRF-safe vendor call: re-validates the URL + DNS at fetch time, refuses redirects,
 * and keeps one timeout signal active through the (size-capped) body read.
 */
async function vendorFetch(rawUrl, init, timeoutMs, { requireOk }) {
  await assertSafeUrl(rawUrl, 'Vendor URL');
  const signal = AbortSignal.timeout(clampTimeout(timeoutMs));

  const resp = await fetch(rawUrl, { ...init, redirect: 'manual', signal });

  if (resp.status >= 300 && resp.status < 400) {
    throw new Error(`Vendor module attempted a redirect (${resp.status})`);
  }
  if (requireOk && !resp.ok) {
    throw new Error(`Vendor module returned ${resp.status}`);
  }

  const text = await readBodyCapped(resp, MAX_RESPONSE_BYTES);
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error('Vendor module returned invalid JSON');
  }
  return sanitizeVendorData(parsed);
}

export async function executeCommand(moduleId, payload) {
  const module = await getModuleConfig(moduleId);

  if (!module) {
    throw errors.notFound('Module not found or not available');
  }

  const start = Date.now();
  let data;

  if (!module.communication_model || module.communication_model === 'webhook_push') {
    const sig = crypto
      .createHmac('sha256', module.webhook_secret)
      .update(JSON.stringify(payload))
      .digest('hex');

    data = await vendorFetch(
      module.webhook_url,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-WaddleBot-Signature': `sha256=${sig}`,
          'X-WaddleBot-Module-Id': String(moduleId),
        },
        body: JSON.stringify(payload),
      },
      module.webhook_timeout_ms,
      { requireOk: true }
    );
  } else if (module.communication_model === 'rest_pull') {
    let authHeaders;

    if (module.auth_type === 'api_key') {
      authHeaders = { Authorization: 'Bearer ' + module.auth_config.api_key };
    } else if (module.auth_type === 'oauth2_client_credentials') {
      authHeaders = { Authorization: 'Bearer ' + module.auth_config.access_token };
    } else {
      const sig = crypto
        .createHmac('sha256', module.webhook_secret)
        .update(JSON.stringify(payload))
        .digest('hex');
      authHeaders = { 'X-WaddleBot-Signature': `sha256=${sig}` };
    }

    data = await vendorFetch(
      module.api_base_url + '/execute',
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...authHeaders,
        },
        body: JSON.stringify(payload),
      },
      module.webhook_timeout_ms,
      { requireOk: true }
    );
  }

  const durationMs = Date.now() - start;

  logger.debug('Vendor module executed', {
    moduleId,
    command: payload.command,
    durationMs,
  });

  query('UPDATE marketplace_modules SET total_requests=total_requests+1 WHERE id=$1', [
    moduleId,
  ]).catch(() => {});

  return data;
}

export function incrementRequestCount(moduleId) {
  query('UPDATE marketplace_modules SET total_requests=total_requests+1 WHERE id=$1', [
    moduleId,
  ]).catch(() => {});
}
