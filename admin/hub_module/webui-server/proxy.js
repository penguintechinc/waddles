/**
 * Dependency-free reverse proxy for the WaddleBot Hub WebUI server.
 *
 * Forwards requests to a single fixed upstream (hub-api) using only Node's
 * built-in http/https modules, so there is no third-party proxy dependency
 * (http-proxy-middleware -> micromatch -> braces) to go stale or carry
 * advisories.
 */
import http from 'node:http';
import https from 'node:https';

/** Hop-by-hop headers (RFC 9110 7.6.1) that must not be forwarded by a proxy. */
const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'proxy-connection',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);

/**
 * Copy headers, dropping hop-by-hop ones plus anything named in Connection.
 * Why: these describe a single transport hop and are invalid to relay.
 */
function filterHeaders(headers) {
  const dropped = new Set(HOP_BY_HOP);
  const connection = headers.connection;
  if (typeof connection === 'string') {
    for (const token of connection.split(',')) {
      dropped.add(token.trim().toLowerCase());
    }
  }
  const out = {};
  for (const [name, value] of Object.entries(headers)) {
    if (!dropped.has(name.toLowerCase())) {
      out[name] = value;
    }
  }
  return out;
}

/**
 * Build an Express-compatible middleware proxying every request to `target`.
 *
 * Mount with `app.use('/api', createProxy(url))`: the full original URL
 * (including the '/api' prefix Express strips from req.url) is forwarded, the
 * method, headers (incl. Authorization) and body are streamed through
 * unchanged, the Host header is set to the upstream host, and an unreachable
 * upstream yields a 502.
 */
export function createProxy(target) {
  const upstream = new URL(target);
  const transport = upstream.protocol === 'https:' ? https : http;
  const basePath = upstream.pathname.replace(/\/+$/, '');
  const agent = new transport.Agent({ keepAlive: true });

  return function proxy(req, res) {
    const upstreamReq = transport.request(
      {
        protocol: upstream.protocol,
        hostname: upstream.hostname,
        port: upstream.port || undefined,
        method: req.method,
        path: basePath + req.originalUrl,
        headers: { ...filterHeaders(req.headers), host: upstream.host },
        agent,
      },
      (upstreamRes) => {
        res.writeHead(
          upstreamRes.statusCode ?? 502,
          upstreamRes.statusMessage,
          filterHeaders(upstreamRes.headers),
        );
        upstreamRes.pipe(res);
        upstreamRes.on('error', () => res.destroy());
      },
    );

    upstreamReq.on('error', (err) => {
      console.error(`[hub-webui] Proxy error { code: "${err.code ?? 'UNKNOWN'}" }`);
      if (res.headersSent) {
        res.destroy();
        return;
      }
      res.writeHead(502, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Bad Gateway', message: 'hub-api is unreachable' }));
    });

    // Client went away mid-request: stop the upstream call too.
    res.on('close', () => {
      if (!res.writableFinished) {
        upstreamReq.destroy();
      }
    });

    req.pipe(upstreamReq);
  };
}
