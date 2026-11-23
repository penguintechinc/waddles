/**
 * Local proxy for E2E tests against beta via kubectl port-forward.
 *
 * Routes requests on a single origin so the React SPA and API are reachable
 * without modifying /etc/hosts or test files.
 *
 * Usage:
 *   # Start two port-forwards first:
 *   kubectl --context dal2-beta port-forward -n waddlebot svc/waddlebot-hub-webui 3001:80 &
 *   kubectl --context dal2-beta port-forward -n waddlebot svc/waddlebot-hub-api-v3 8204:8204 &
 *
 *   # Then run tests via this proxy:
 *   BASE_URL=http://localhost:3000 node tests/e2e/local-proxy.js &
 *   BASE_URL=http://localhost:3000 npx playwright test
 *
 * Environment variables:
 *   PROXY_PORT   - Port this proxy listens on (default: 3000)
 *   WEBUI_PORT   - Port hub-webui is port-forwarded to (default: 3001)
 *   API_PORT     - Port hub-api is port-forwarded to (default: 8204)
 */

'use strict';

const http = require('http');

const PROXY_PORT = parseInt(process.env.PROXY_PORT || '3000', 10);
const WEBUI_PORT = parseInt(process.env.WEBUI_PORT || '3001', 10);
const API_PORT   = parseInt(process.env.API_PORT   || '8204', 10);

function isApiRequest(url) {
  return url.startsWith('/api/') || url.startsWith('/socket.io');
}

const server = http.createServer((req, res) => {
  const targetPort = isApiRequest(req.url) ? API_PORT : WEBUI_PORT;

  const options = {
    hostname: '127.0.0.1',
    port: targetPort,
    path: req.url,
    method: req.method,
    headers: { ...req.headers, host: `localhost:${targetPort}` },
  };

  const proxyReq = http.request(options, (proxyRes) => {
    res.writeHead(proxyRes.statusCode, proxyRes.headers);
    proxyRes.pipe(res, { end: true });
  });

  proxyReq.on('error', (err) => {
    res.writeHead(502);
    res.end(`Proxy error: ${err.message}`);
  });

  req.pipe(proxyReq, { end: true });
});

// WebSocket upgrade passthrough
server.on('upgrade', (req, socket, head) => {
  const targetPort = isApiRequest(req.url) ? API_PORT : WEBUI_PORT;
  const net = require('net');
  const target = net.connect(targetPort, '127.0.0.1', () => {
    target.write(
      `${req.method} ${req.url} HTTP/1.1\r\n` +
      Object.entries(req.headers).map(([k, v]) => `${k}: ${v}`).join('\r\n') +
      '\r\n\r\n'
    );
    target.write(head);
    socket.pipe(target);
    target.pipe(socket);
  });
  target.on('error', () => socket.destroy());
  socket.on('error', () => target.destroy());
});

server.listen(PROXY_PORT, '127.0.0.1', () => {
  console.log(`Local proxy listening on http://localhost:${PROXY_PORT}`);
  console.log(`  /           → http://localhost:${WEBUI_PORT} (hub-webui)`);
  console.log(`  /api/*      → http://localhost:${API_PORT} (hub-api)`);
  console.log(`  /socket.io  → http://localhost:${API_PORT} (hub-api)`);
});
