/**
 * Defence-in-depth guard: the public webui-server must never forward hub-api's service-to-service
 * routes (`/api/v{N}/internal`, `/api/v{N}/internal/...`) to hub-api.
 *
 * Those routes are protected by machine-JWT verification, but the public ingress sends `/` to
 * this server, which reverse-proxies all of `/api/*`; refusing them here means a bug in (or a
 * leaked credential for) the machine-auth layer is not reachable from the internet.
 */

/** Max rounds of percent-decoding, so `%252f`-style double encoding is flattened too. */
const MAX_DECODE_ROUNDS = 4;

/**
 * Return the request path (no query/fragment) fully percent-decoded, or `null` if it is
 * malformed or still carries encoding after the decode budget (treated as hostile).
 */
function decodePath(rawUrl) {
  let path = String(rawUrl).split('#')[0].split('?')[0];
  for (let round = 0; round < MAX_DECODE_ROUNDS; round += 1) {
    let decoded;
    try {
      decoded = decodeURIComponent(path);
    } catch {
      return null;
    }
    if (decoded === path) {
      return path;
    }
    path = decoded;
  }
  return /%[0-9a-f]{2}/i.test(path) ? null : path;
}

/**
 * True when `rawUrl` (the request's original URL) must be refused by the public proxy.
 *
 * Refused: `/api/v{N}/internal` and anything beneath it (case-insensitive, after percent-decoding
 * and slash/backslash collapsing), plus any path that is malformed, contains a NUL, a `.`/`..`
 * segment, or an encoded slash/backslash -- shapes whose upstream interpretation could differ
 * from ours and smuggle a request past the prefix check. Normal API paths never contain these.
 */
export function isBlockedApiPath(rawUrl) {
  const rawPath = String(rawUrl).split('#')[0].split('?')[0];
  if (/%(?:2f|5c)/i.test(rawPath)) {
    return true;
  }
  const decoded = decodePath(rawUrl);
  if (decoded === null || decoded.includes('\0')) {
    return true;
  }
  const segments = decoded.replace(/\\/g, '/').split('/').filter((s) => s !== '');
  if (segments.some((s) => s === '.' || s === '..')) {
    return true;
  }
  const [root, version, area] = segments.map((s) => s.toLowerCase());
  return root === 'api' && /^v\d+$/.test(version ?? '') && area === 'internal';
}
