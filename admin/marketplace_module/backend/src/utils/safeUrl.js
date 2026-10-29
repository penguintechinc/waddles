/**
 * SSRF guard for vendor-controlled URLs: https-only, DNS-resolved, and rejects
 * private/loopback/link-local/CGNAT/metadata addresses. Used at module write
 * time and again immediately before every outbound vendor fetch.
 */
import dns from 'node:dns';
import net from 'node:net';
import { errors } from '../middleware/errorHandler.js';

/** Returns true when the IPv4 address is in a non-public range. */
function isBlockedV4(ip) {
  const [a, b] = ip.split('.').map(Number);
  return (
    a === 0 ||
    a === 10 ||
    a === 127 ||
    (a === 100 && b >= 64 && b <= 127) || // CGNAT 100.64/10
    (a === 169 && b === 254) || // link-local + cloud metadata
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    (a === 192 && b === 0) || // 192.0.0/24 + 192.0.2/24
    (a === 198 && (b === 18 || b === 19)) || // benchmarking
    a >= 224 // multicast + reserved + broadcast
  );
}

/** Returns true when the IPv6 address is in a non-public range. */
function isBlockedV6(ip) {
  const lower = ip.toLowerCase().split('%')[0];
  const mapped = lower.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
  if (mapped) return isBlockedV4(mapped[1]);
  const hexMapped = lower.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
  if (hexMapped) {
    const hi = parseInt(hexMapped[1], 16);
    const lo = parseInt(hexMapped[2], 16);
    return isBlockedV4(`${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`);
  }
  if (lower === '::' || lower === '::1') return true;
  const first = parseInt(lower.split(':')[0] || '0', 16);
  return (
    (first & 0xfe00) === 0xfc00 || // fc00::/7 unique-local (incl. fd00::/8)
    (first & 0xffc0) === 0xfe80 || // fe80::/10 link-local
    (first & 0xff00) === 0xff00 // multicast
  );
}

/** True when the literal IP (v4 or v6) must never be contacted. */
export function isBlockedIp(ip) {
  const family = net.isIP(ip);
  if (family === 4) return isBlockedV4(ip);
  if (family === 6) return isBlockedV6(ip);
  return true;
}

/** Synchronous shape check: https scheme, no credentials, has a hostname. Returns the URL. */
export function parseHttpsUrl(raw, label = 'URL') {
  let url;
  try {
    url = new URL(String(raw));
  } catch {
    throw errors.badRequest(`${label} is not a valid URL`);
  }
  if (url.protocol !== 'https:') {
    throw errors.badRequest(`${label} must use https`);
  }
  if (url.username || url.password) {
    throw errors.badRequest(`${label} must not contain credentials`);
  }
  return url;
}

/**
 * Full validation: shape + DNS resolution; every resolved address must be public.
 * Throws a 400 AppError on any violation. Returns the parsed URL and resolved addresses.
 */
export async function assertSafeUrl(raw, label = 'URL') {
  const url = parseHttpsUrl(raw, label);
  const host = url.hostname.replace(/^\[|\]$/g, '');

  let addresses;
  if (net.isIP(host)) {
    addresses = [host];
  } else {
    try {
      const results = await dns.promises.lookup(host, { all: true, verbatim: true });
      addresses = results.map((r) => r.address);
    } catch {
      throw errors.badRequest(`${label} host could not be resolved`);
    }
  }

  if (addresses.length === 0 || addresses.some(isBlockedIp)) {
    throw errors.badRequest(`${label} resolves to a disallowed address`);
  }
  return { url, addresses };
}

export default { assertSafeUrl, parseHttpsUrl, isBlockedIp };
