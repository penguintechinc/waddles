/**
 * In-cluster TLS policy for the hub-webui Express server.
 *
 * The Helm chart (global.internalTransitTls) sets INTERNAL_CA_PEM (or INTERNAL_CA_FILE; internal CA that signed
 * hub-api's cert), INTERNAL_TLS_REQUIRED, and TLS_CERT_FILE / TLS_KEY_FILE (this server's own
 * cert for the Gateway BackendTLSPolicy hop). Every misconfiguration throws; nothing falls back
 * to plaintext or to the public root store.
 */
import fs from 'node:fs';

/** Cipher suites: TLS1.3 AES-128-GCM / ChaCha20 plus the matching TLS1.2 ECDSA/ECDHE suites. */
export const CIPHERS = [
  'TLS_AES_128_GCM_SHA256',
  'TLS_CHACHA20_POLY1305_SHA256',
  'ECDHE-ECDSA-AES128-GCM-SHA256',
  'ECDHE-ECDSA-CHACHA20-POLY1305',
].join(':');

/** Key exchange groups: X25519 or P-256 only. */
export const ECDH_CURVE = 'X25519:prime256v1';

/** True for the truthy spellings the chart/other services use. */
function truthy(value) {
  return ['1', 'true', 'yes', 'on'].includes(String(value ?? '').trim().toLowerCase());
}

/** Read a PEM file, throwing a descriptive error when unreadable or not PEM. */
function readPem(path, label, marker) {
  let data;
  try {
    data = fs.readFileSync(path);
  } catch (err) {
    throw new Error(`${label} ${JSON.stringify(path)} is not readable: ${err.code ?? err.message}`);
  }
  if (!data.toString('utf8').includes(marker)) {
    throw new Error(`${label} ${JSON.stringify(path)} contains no PEM block (${marker})`);
  }
  return data;
}

/**
 * Resolve the internal TLS policy from an env map.
 *
 * Returns `{ required, ca }` where `ca` is the pinned CA Buffer (or null). `required` without
 * a CA is an error: refusing to guess a trust root.
 */
export function resolveInternalTls(env = process.env) {
  const required = truthy(env.INTERNAL_TLS_REQUIRED);
  const pem = (env.INTERNAL_CA_PEM ?? '').trim();
  if (pem) {
    if (!pem.includes('-----BEGIN CERTIFICATE-----')) {
      throw new Error('INTERNAL_CA_PEM contains no PEM block (-----BEGIN CERTIFICATE-----)');
    }
    return { required, ca: Buffer.from(pem) };
  }
  const caFile = (env.INTERNAL_CA_FILE ?? '').trim();
  if (!caFile) {
    if (required) {
      throw new Error('INTERNAL_TLS_REQUIRED is set but neither INTERNAL_CA_PEM nor INTERNAL_CA_FILE is set');
    }
    return { required, ca: null };
  }
  return { required, ca: readPem(caFile, 'INTERNAL_CA_FILE', '-----BEGIN CERTIFICATE-----') };
}

/**
 * Throw when the policy requires HTTPS and `url` is not an https:// URL.
 */
export function assertUpstreamUrl(url, policy) {
  if (policy.required && new URL(url).protocol !== 'https:') {
    throw new Error(`plaintext upstream ${url} refused: INTERNAL_TLS_REQUIRED is set`);
  }
}

/**
 * Build node:https server options from TLS_CERT_FILE / TLS_KEY_FILE, or null when neither is set.
 * Exactly one of the two set is an error.
 */
export function resolveServerTls(env = process.env) {
  const certFile = (env.TLS_CERT_FILE ?? '').trim();
  const keyFile = (env.TLS_KEY_FILE ?? '').trim();
  if (!certFile && !keyFile) {
    return null;
  }
  if (!certFile || !keyFile) {
    throw new Error('TLS_CERT_FILE and TLS_KEY_FILE must be set together');
  }
  return {
    cert: readPem(certFile, 'TLS_CERT_FILE', '-----BEGIN CERTIFICATE-----'),
    key: readPem(keyFile, 'TLS_KEY_FILE', 'PRIVATE KEY-----'),
    minVersion: 'TLSv1.2',
    ciphers: CIPHERS,
    ecdhCurve: ECDH_CURVE,
    honorCipherOrder: true,
  };
}
