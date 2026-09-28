/**
 * Express static-file server for the WaddleBot Hub WebUI.
 *
 * Serves the Vite-built React SPA (./dist) and reverse-proxies /api
 * requests to hub-api. This container exists solely for static serving
 * and API proxying -- no business logic, no database access -- and runs
 * on Express instead of nginx per house convention.
 */
import express from 'express';
import { createProxyMiddleware } from 'http-proxy-middleware';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));

const PORT = Number(process.env.PORT ?? 8080);
const NODE_ENV = process.env.NODE_ENV ?? 'development';

// In-cluster deployments (Helm) always set HUB_API_URL explicitly to the
// hub-api Service DNS name -- localhost is never correct once this runs in
// a pod. Fail fast in production rather than silently proxying to nothing.
if (!process.env.HUB_API_URL && NODE_ENV === 'production') {
  console.error(
    '[hub-webui] FATAL: HUB_API_URL is required when NODE_ENV=production ' +
      '-- refusing to start with the localhost:8060 dev default',
  );
  process.exit(1);
}
const HUB_API_URL = process.env.HUB_API_URL ?? 'http://localhost:8060';
const DIST_DIR = join(__dirname, 'dist');

const app = express();

// Health check -- LOCAL only (process-up check). Must never call out to
// hub-api: liveness uses this same endpoint, and a slow/down upstream must
// not cause kubelet to kill this pod. Registered before the SPA catch-all.
app.get('/health', (_req, res) => {
  res.json({ status: 'ok' });
});

// Reverse proxy /api/* to hub-api. Express strips the '/api' mount prefix
// from req.url before this middleware ever sees it, so pathRewrite adds it
// back -- without this, hub-api (routes mounted at /api/v1) 404s on every
// proxied request (e.g. /api/v1/auth/login arrives as /v1/auth/login).
app.use(
  '/api',
  createProxyMiddleware({
    target: HUB_API_URL,
    changeOrigin: true,
    pathRewrite: { '^/': '/api/' },
  }),
);

app.use(express.static(DIST_DIR));

// SPA fallback -- client-side routing (react-router) owns everything else.
app.get('*', (_req, res) => {
  res.sendFile(join(DIST_DIR, 'index.html'));
});

app.listen(PORT, () => {
  console.log(`[hub-webui] Listening { port: ${PORT}, hubApiUrl: "${HUB_API_URL}" }`);
});
