/**
 * Vendor Execution Service — proxy command execution to vendor modules
 */
// Node >=18 (see package.json engines) ships a global fetch; node-fetch was never a dependency.
const fetch = (...args) => globalThis.fetch(...args);
import crypto from 'crypto';
import { query } from '../config/database.js';
import { logger } from '../utils/logger.js';
import { errors } from '../middleware/errorHandler.js';

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

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), module.webhook_timeout_ms || 5000);

    const resp = await fetch(module.webhook_url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-WaddleBot-Signature': `sha256=${sig}`,
        'X-WaddleBot-Module-Id': String(moduleId),
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    clearTimeout(timeout);

    if (!resp.ok) {
      throw new Error(`Vendor module returned ${resp.status}`);
    }

    data = await resp.json();
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

    const resp = await fetch(module.api_base_url + '/execute', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders,
      },
      body: JSON.stringify(payload),
    });

    data = await resp.json();
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
