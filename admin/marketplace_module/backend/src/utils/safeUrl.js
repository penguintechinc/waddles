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

const V4_DENY = [
  '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
  '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
  '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4', '255.255.255.255/32',
].map((c) => ipaddr.parseCIDR(c));

const V4_MAPPED = ipaddr.parseCIDR('::ffff:0:0/96');
const NAT64 = ipaddr.parseCIDR('64:ff9b::/96');
const SIX_TO_FOUR = ipaddr.parseCIDR('2002::/16');

const V6_DENY = [
  '::1/128', '::/128', '::/96', '::ffff:0:0/96', '64:ff9b::/96', '64:ff9b:1::/48', '2002::/16',
  '2001::/32', '2001:db8::/32', '100::/64', 'fc00::/7', 'fe80::/10', 'fec0::/10', 'ff00::/8',
].map((c) => ipaddr.parseCIDR(c));

const matchesAny = (addr, list) => list.some((cidr) => addr.match(cidr));

/** Packs two 16-bit IPv6 parts into an IPv4 address. */
const v4From = (hi, lo) => ipaddr.IPv4.parse(`${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`);

/**
 * Extracts the IPv4 embedded in a v4-mapped (::ffff:0:0/96), NAT64 (64:ff9b::/96) or 6to4
 * (2002::/16) IPv6 address, or null when the address embeds none.
 */
function embeddedV4(addr) {
  const p = addr.parts;
  if (addr.match(V4_MAPPED) || addr.match(NAT64)) return v4From(p[6], p[7]);
  if (addr.match(SIX_TO_FOUR)) return v4From(p[1], p[2]);
  return null;
}

/**
 * True when the literal IP (v4 or v6) must never be contacted. Denied when EITHER an explicit
 * reserved-range CIDR deny-list matches OR ipaddr.js does not classify it `unicast` (backstop).
 * Embedded-IPv4 forms (v4-mapped, NAT64, 6to4) are unwrapped and the inner IPv4 is judged too,
 * so an embedded private address is refused even if the wrapper range were ever allowed.
 */
export function isBlockedIp(ip) {
  if (typeof ip !== 'string' || !ipaddr.isValid(ip)) return true;
  const addr = ipaddr.parse(ip);
  if (addr.kind() === 'ipv4') return matchesAny(addr, V4_DENY) || addr.range() !== 'unicast';
  // A v4-mapped address is exactly as reachable as its embedded IPv4: judge it as that IPv4.
  if (addr.match(V4_MAPPED)) return isBlockedIp(embeddedV4(addr).toString());
  const inner = embeddedV4(addr);
  if (inner && isBlockedIp(inner.toString())) return true;
  return matchesAny(addr, V6_DENY) || addr.range() !== 'unicast';
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
