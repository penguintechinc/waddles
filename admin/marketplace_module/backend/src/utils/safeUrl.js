/**
 * SSRF guard for vendor-controlled URLs: https-only, DNS-resolved, and rejects
 * non-public addresses (classified by the maintained ipaddr.js range tables). Used at module
 * write time; outbound vendor fetches additionally pin the socket to validated IPs via pinnedAgent.
 */
import dns from 'node:dns';
import net from 'node:net';
import ipaddr from 'ipaddr.js';
import { Agent } from 'undici';
import { errors } from '../middleware/errorHandler.js';

/**
 * True when the literal IP (v4 or v6) must never be contacted. Only ipaddr.js `unicast` is
 * allowed. IPv4-mapped IPv6 is unwrapped and judged as IPv4; every other v4-embedding transition
 * form (NAT64 64:ff9b::/96, 6to4 2002::/16, Teredo 2001::/32, ::/96, 100::/64 discard, ...) has a
 * non-unicast range name and is refused.
 */
export function isBlockedIp(ip) {
  if (typeof ip !== 'string' || !ipaddr.isValid(ip)) return true;
  let addr = ipaddr.parse(ip);
  if (addr.kind() === 'ipv6' && addr.isIPv4MappedAddress()) addr = addr.toIPv4Address();
  // Deprecated IPv4-compatible ::/96 (e.g. ::7f00:1) is reported as unicast by ipaddr.js; refuse it.
  if (addr.kind() === 'ipv6' && addr.parts.slice(0, 6).every((p) => p === 0)) return true;
  return addr.range() !== 'unicast';
}

/**
 * dns.lookup-compatible resolver for undici's `connect.lookup`. Every resolved address is
 * validated and ONLY validated addresses are handed to the socket, so the connection is pinned to
 * the IP that was checked (no resolve-then-reconnect rebinding window). TLS SNI/Host still use
 * the original hostname because undici passes the hostname, not the address, to TLS.
 */
export function guardedLookup(hostname, options, callback) {
  const opts = typeof options === 'function' ? {} : options || {};
  const cb = typeof options === 'function' ? options : callback;
  dns.lookup(hostname, { ...opts, all: true, verbatim: true }, (err, results) => {
    if (err) return cb(err);
    const list = Array.isArray(results) ? results : [];
    if (list.length === 0 || list.some((r) => isBlockedIp(r.address))) {
      const e = new Error(`Blocked address for ${hostname}`);
      e.code = 'ESSRFBLOCKED';
      return cb(e);
    }
    if (opts.all) return cb(null, list);
    return cb(null, list[0].address, list[0].family);
  });
}

/** Shared undici Agent whose sockets can only connect to addresses accepted by guardedLookup. */
export const pinnedAgent = new Agent({ connect: { lookup: guardedLookup } });

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
 * Literal-IP hosts bypass DNS entirely, so this check is the guard for them.
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

export default { assertSafeUrl, parseHttpsUrl, isBlockedIp, guardedLookup, pinnedAgent };
