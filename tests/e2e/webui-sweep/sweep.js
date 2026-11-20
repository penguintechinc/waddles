#!/usr/bin/env node
/**
 * Hub web UI page sweep: logs in through the real form, visits every route
 * enumerated from the react-router source and reports console errors,
 * pageerrors, HTTP >= 400 responses and requestfailed per page.
 *
 * Env (credentials from env only, never args/files):
 *   BASE_URL            target origin (default https://waddles-alpha.penguintech.cloud:8443)
 *   HOST_HEADER         optional Host to present when BASE_URL is an IP/LB: Chromium is told to
 *                       resolve HOST_HEADER's hostname to BASE_URL's IP (Host + SNI preserved)
 *   WEBUI_UPSTREAM / API_UPSTREAM
 *                       optional https://localhost:PORT port-forward targets; starts an in-process
 *                       proxy ("/" -> webui, "/api" + "/socket.io" -> hub-api) and uses it as base
 *   WEBUI_ADMIN_EMAIL / WEBUI_ADMIN_PASSWORD   login credentials
 *   COMMUNITY_ID        community UUID for :communityId/:id params
 *   OUT_DIR             screenshot dir (default /tmp/playwright-waddles)
 *   RESULTS_DIR         results dir (default tests/e2e/webui-sweep/results)
 *   ROUTE_FILTER        optional regex limiting routes visited
 *   ROUTES_FILE         router source (default admin/hub_module/frontend/src/App.jsx)
 */
const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');
const { chromium } = require('@playwright/test');

const ROOT = path.resolve(__dirname, '../../..');
const ROUTES_FILE = process.env.ROUTES_FILE || path.join(ROOT, 'admin/hub_module/frontend/src/App.jsx');
const OUT_DIR = process.env.OUT_DIR || '/tmp/playwright-waddles';
const RESULTS_DIR = process.env.RESULTS_DIR || path.join(__dirname, 'results');
const COMMUNITY_ID = process.env.COMMUNITY_ID || '15959481-f9f1-47ce-be48-d975a6832d08';
// --user-role=user (or SWEEP_ROLE=user) sweeps with WEBUI_USER_EMAIL/WEBUI_USER_PASSWORD (a non-admin account that must already exist).
const ROLE = (process.argv.find((a) => a.startsWith('--user-role=')) || '').split('=')[1] || process.env.SWEEP_ROLE || 'admin';
const EMAIL = ROLE === 'user' ? process.env.WEBUI_USER_EMAIL : process.env.WEBUI_ADMIN_EMAIL;
const PASSWORD = ROLE === 'user' ? process.env.WEBUI_USER_PASSWORD : process.env.WEBUI_ADMIN_PASSWORD;
const NAV_CAP_MS = 20000;
const DELAY_MS = parseInt(process.env.SWEEP_DELAY_MS || '500', 10);

// Findings already tracked by PR #1462 so new failures stand out.
const KNOWN_PENDING = [
  { re: /\/(announcements|polls|forms|streams|watch-time)(\/|$)/, status: 402, tag: 'KNOWN-PENDING-#1462' },
  { re: /\/(leaderboard|messages)(\/|$)/, status: 500, tag: 'KNOWN-PENDING-#1462' },
  { re: /\/api\/v1\/flags/, status: 401, tag: 'KNOWN-PENDING-#1462' },
];
const ERROR_TEXT = /(Something went wrong|Failed to load|Unexpected|\bError\b)/i;

/** Launches the bundled Chromium; falls back to CHROMIUM_PATH or the newest cached chromium build. */
async function launchBrowser(args) {
  if (!process.env.CHROMIUM_PATH) {
    try { return await chromium.launch({ args }); } catch (e) { /* bundled build missing; try cache */ }
  }
  let exe = process.env.CHROMIUM_PATH;
  if (!exe) {
    const dir = path.join(process.env.HOME || '', '.cache/ms-playwright');
    const cands = fs.existsSync(dir) ? fs.readdirSync(dir).filter((d) => /^chromium-\d+$/.test(d)).sort() : [];
    if (cands.length) exe = path.join(dir, cands[cands.length - 1], 'chrome-linux64/chrome');
  }
  return chromium.launch({ args, executablePath: exe });
}

/** Extracts every route path from the react-router source, resolving nested relative tenant paths. */
function enumerateRoutes(file) {
  const src = fs.readFileSync(file, 'utf8');
  const routes = new Set();
  let tenantPrefix = null;
  for (const line of src.split('\n')) {
    const m = line.match(/^\s*<Route\s+(?:index\s+)?path="([^"]+)"/) || line.match(/^\s*path="([^"]+)"/);
    if (!m) continue;
    let p = m[1];
    if (p === '*') continue;
    if (p === '/tenant/:tenantSlug') { tenantPrefix = p; routes.add(p); continue; }
    if (!p.startsWith('/')) { if (tenantPrefix) p = `${tenantPrefix}/${p}`; else continue; }
    else tenantPrefix = null;
    routes.add(p);
  }
  return [...routes];
}

/** Starts a local reverse proxy fronting the two port-forwards (webui has no /api proxy of its own). */
function startProxy(webui, api) {
  const targets = { webui: new URL(webui), api: new URL(api) };
  const server = http.createServer((req, res) => {
    const isApi = /^\/(api|socket\.io)(\/|\?|$)/.test(req.url);
    const t = isApi ? targets.api : targets.webui;
    const up = https.request({
      hostname: t.hostname, port: t.port, path: req.url, method: req.method, rejectUnauthorized: false,
      headers: { ...req.headers, host: `localhost:${t.port}` },
    }, (r) => { res.writeHead(r.statusCode, r.headers); r.pipe(res); });
    up.on('error', (e) => { res.writeHead(502); res.end(String(e)); });
    req.pipe(up);
  });
  server.on('upgrade', (req, socket) => socket.destroy());
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port })));
}

const CONSENT_INIT = () => {
  const ts = new Date().toISOString();
  localStorage.setItem('gdpr_consent', JSON.stringify({ accepted: true, essential: true, functional: true, analytics: true, marketing: true, timestamp: ts, policyVersion: '1.0' }));
  localStorage.setItem('cookieConsent', JSON.stringify({ essential: true, analytics: true, marketing: true, preferences: true, timestamp: ts, policyVersion: '1.0' }));
  localStorage.setItem('cookie_consent', JSON.stringify({ essential_cookies: true, functional_cookies: true, analytics_cookies: true, marketing_cookies: true, consent_version: '1.0' }));
  localStorage.setItem('vendor-request-dismissed', 'true');
};

/** Pulls the first array of objects out of a typical hub-api list envelope. */
function firstItems(body) {
  if (Array.isArray(body)) return body;
  if (!body || typeof body !== 'object') return [];
  for (const k of ['data', 'items', 'results', 'communities', 'tenants', 'users', 'members', 'events', 'tickets', 'channels', 'apps', 'submissions', 'pages']) {
    if (Array.isArray(body[k])) return body[k];
    if (body[k] && typeof body[k] === 'object') { const r = firstItems(body[k]); if (r.length) return r; }
  }
  return [];
}

/** Resolves route params from the listing endpoints the webui itself calls; logs every attempt (url/status/count). */
async function discoverParams(page, base) {
  const log = [];
  const get = async (p) => {
    try {
      const r = await page.request.get(base + p);
      const body = r.ok() ? await r.json().catch(() => null) : null;
      return { status: r.status(), body };
    } catch (e) { console.log('discovery request error:', String(e.message).slice(0, 100)); return { status: 0, body: null }; }
  };
  /** First candidate listing that yields an item with one of `keys`; records why it failed otherwise. */
  const first = async (param, cands, keys) => {
    for (const u of cands) {
      const { status, body } = await get(u);
      const items = firstItems(body);
      const it = items[0];
      let val = null;
      if (it) for (const k of keys) if (it[k] != null) { val = String(it[k]); break; }
      log.push({ param, url: u, status, count: items.length, value: val ? 'found' : null });
      if (val) return val;
    }
    return null;
  };
  const c = COMMUNITY_ID;
  const params = { communityId: c, id: c };
  params.eventId = await first('eventId', [`/api/v1/admin/${c}/calendar/events`], ['id', 'uuid', 'event_id']);
  params.channelId = await first('channelId', [`/api/v1/admin/${c}/interaction/channels`, `/api/v1/communities/${c}/interaction/channels`], ['id', 'uuid', 'channel_id']);
  params.ticketId = await first('ticketId', [`/api/v1/admin/${c}/support/tickets`], ['id', 'uuid', 'ticket_id']);
  params.userId = await first('userId', [`/api/v1/admin/${c}/members`], ['user_id', 'userId', 'public_id', 'id', 'uuid']);
  // tenantSlug is not listed by any page: AuthContext takes it from /auth/me (the admin's own tenant).
  const me = await get('/api/v1/auth/me');
  const meStr = JSON.stringify(me.body || {});
  const mm = meStr.match(/"(?:tenantSlug|tenant_slug)":"([^"]+)"/);
  params.tenantSlug = mm ? mm[1] : null;
  log.push({ param: 'tenantSlug', url: '/api/v1/auth/me', status: me.status, count: mm ? 1 : 0, value: mm ? 'found' : null });
  if (!params.tenantSlug) params.tenantSlug = await first('tenantSlug', ['/api/v1/superadmin/tenants'], ['slug', 'tenant_slug']);
  params.slug = await first('slug', ['/api/v1/calendar/booking-pages'], ['slug']);
  params.appId = await first('appId', [`/api/v1/apps/community/${c}/activation`], ['app_id', 'appId', 'id']);
  params.submissionId = await first('submissionId', ['/api/v1/marketplace/admin/marketplace/submissions', '/api/v1/marketplace/admin/marketplace/vendor-requests'], ['id', 'uuid', 'submission_id']);
  params.source = null; // /admin/:communityId/marketplace/:source/:id only redirects to bundles; no listing exists
  log.push({ param: 'source', url: '-', status: 0, count: 0, value: null, reason: 'redirect-only route, no listing endpoint' });
  params.__log = log;
  return params;
}

/** Returns a concrete URL for a route pattern, or the list of params that could not be resolved. */
function fillRoute(route, params) {
  const missing = [];
  const url = route.replace(/:([A-Za-z]+)/g, (_, n) => { if (!params[n]) { missing.push(n); return ''; } return encodeURIComponent(params[n]); });
  return missing.length ? { missing } : { url };
}

/** Tags a failed-request record as KNOWN-PENDING when it matches a tracked finding. */
function classify(f) {
  for (const k of KNOWN_PENDING) if (f.status === k.status && k.re.test(f.path)) return k.tag;
  return null;
}

/** Visits one URL, collecting every error signal during load; screenshots only non-clean pages. */
async function visitOnce(context, base, route, url, shared) {
  const page = shared && shared.page ? shared.page : await context.newPage();
  const spa = !!(shared && shared.page);
  const handlers = [];
  const on = (ev, fn) => { page.on(ev, fn); handlers.push([ev, fn]); };
  const rec = { route, url, status: null, consoleErrors: [], pageErrors: [], failedRequests: [], errorText: [], navError: null };
  on('console', (m) => { if (m.type() === 'error') rec.consoleErrors.push(m.text().slice(0, 300)); });
  on('pageerror', (e) => rec.pageErrors.push(String(e.message || e).slice(0, 300)));
  on('response', (r) => {
    if (r.status() >= 400) rec.failedRequests.push({ method: r.request().method(), path: new URL(r.url()).pathname, status: r.status() });
  });
  on('requestfailed', (r) => {
    const err = r.failure()?.errorText || 'failed';
    if (/ERR_ABORTED/.test(err)) return; // SPA-cancelled requests are not failures
    rec.failedRequests.push({ method: r.method(), path: new URL(r.url()).pathname, status: 0, error: err });
  });
  try {
    if (spa) {
      // Client-side navigation (history API) like a real user: avoids re-running the auth bootstrap
      // (/auth/me, /auth/providers, /auth/sso/options) which hub-api rate-limits to 10/min per IP.
      await page.evaluate((u) => { window.history.pushState({}, '', u); window.dispatchEvent(new PopStateEvent('popstate')); }, url);
      await page.waitForTimeout(800);
      rec.status = 200;
    } else {
      const resp = await page.goto(base + url, { waitUntil: 'load', timeout: NAV_CAP_MS });
      rec.status = resp ? resp.status() : null;
    }
    await page.waitForLoadState('networkidle', { timeout: NAV_CAP_MS }).catch(() => { rec.navError = 'networkidle timeout (20s)'; });
    rec.finalPath = new URL(page.url()).pathname;
    const body = await page.evaluate(() => document.body?.innerText || '').catch(() => '');
    for (const line of body.split('\n')) if (ERROR_TEXT.test(line) && line.length < 200) rec.errorText.push(line.trim());
    rec.errorText = [...new Set(rec.errorText)].slice(0, 5);
  } catch (e) { rec.navError = String(e.message).split('\n')[0]; }
  for (const f of rec.failedRequests) f.known = classify(f);
  rec.newFailures = rec.failedRequests.filter((f) => !f.known);
  // Console "Failed to load resource" lines duplicate failed requests; keep them out of the unexplained set.
  rec.consoleResidual = rec.consoleErrors.filter((c) => !/Failed to load resource/.test(c));
  rec.fail = rec.newFailures.length > 0 || rec.pageErrors.length > 0 || rec.consoleResidual.length > 0 || !!rec.navError || (rec.status !== null && rec.status >= 400);
  rec.knownOnly = !rec.fail && rec.failedRequests.length > 0;
  if (rec.fail || rec.knownOnly) {
    const f = path.join(OUT_DIR, `${route.replace(/[^A-Za-z0-9]+/g, '_')}.png`);
    await page.screenshot({ path: f, fullPage: true }).catch(() => {});
    rec.screenshot = f;
  }
  for (const [ev, fn] of handlers) page.off(ev, fn);
  if (!spa) await page.close();
  await new Promise((r) => setTimeout(r, DELAY_MS)); // avoid tripping hub-api rate limits (429)
  return rec;
}

/** Visits a URL, retrying after backoff when hub-api rate-limits (429) so limiter noise is not reported as page failure. */
async function visit(context, base, route, url, shared) {
  let rec;
  for (let attempt = 0; attempt < 3; attempt++) {
    rec = await visitOnce(context, base, route, url, shared);
    if (!rec.failedRequests.some((f) => f.status === 429)) break;
    rec.retries = attempt + 1;
    await new Promise((r) => setTimeout(r, 20000 * (attempt + 1)));
  }
  return rec;
}

/** Real login-form flow (CSRF header injected as tests/e2e/auth.setup.js does). */
async function login(context, base) {
  const page = await context.newPage();
  await page.goto(base + '/login', { waitUntil: 'networkidle' });
  let csrf = (await context.cookies()).find((c) => c.name === 'XSRF-TOKEN')?.value;
  if (!csrf) {
    const r = await page.request.get('/api/v1/health').catch(() => null);
    const sc = r ? (r.headers()['set-cookie'] || '') : '';
    const m = sc.match(/XSRF-TOKEN=([^;]+)/);
    if (m) csrf = m[1];
  }
  if (csrf) {
    await context.addCookies([{ name: 'XSRF-TOKEN', value: decodeURIComponent(csrf), url: base, httpOnly: false, secure: false, sameSite: 'Lax' }]);
    await page.route('**/api/v1/auth/login', (route) => (route.request().method() === 'POST'
      ? route.continue({ headers: { ...route.request().headers(), 'x-xsrf-token': csrf } }) : route.continue()));
  }
  await page.waitForSelector('input[type="email"]:not([disabled])', { timeout: 15000 });
  await page.fill('[data-testid="email-input"], input[type="email"]', EMAIL);
  await page.fill('[data-testid="password-input"], input[type="password"]', PASSWORD);
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/api/v1/auth/login') && r.request().method() === 'POST', { timeout: 15000 }),
    page.click('[data-testid="auth-submit"], button[type="submit"]'),
  ]);
  if (resp.status() >= 400) throw new Error(`login failed: HTTP ${resp.status()}`);
  await page.waitForURL((u) => !u.toString().includes('/login'), { timeout: 15000 });
  await page.close();
}

/** Renders the results markdown table plus skipped routes and heuristic matches. */
function toMarkdown(results, skipped, meta) {
  const e = (s) => String(s).replace(/\|/g, '\\|');
  const rows = results.map((r) => {
    const st = r.fail ? 'FAIL' : r.knownOnly ? 'KNOWN-PENDING' : 'OK';
    const ce = [...r.consoleResidual, ...r.pageErrors.map((p) => `pageerror: ${p}`)].slice(0, 3).map((c) => c.slice(0, 80)).join('; ') || '-';
    const fr = r.failedRequests.map((f) => `${f.method} ${f.path} ${f.status || f.error}${f.known ? ` (${f.known})` : ''}`).join('<br>') || '-';
    return `| ${e(r.url)} | ${st}${r.navError ? ` (${e(r.navError)})` : ''} | ${e(ce)} | ${e(fr)} |`;
  });
  const heur = results.filter((r) => r.errorText.length).map((r) => `- ${r.url}: ${r.errorText.map((t) => `"${t}"`).join(' / ')}`);
  return [`# Web UI sweep ${meta.when}`, '',
    `Base: ${meta.base} | visited ${results.length}, clean ${results.filter((r) => !r.fail && !r.knownOnly).length}, known-pending ${results.filter((r) => r.knownOnly).length}, failing ${results.filter((r) => r.fail).length}, skipped ${skipped.length}`,
    '', '| route | status | console errors | failed requests |', '|---|---|---|---|', ...rows,
    '', '## Skipped (unresolved params)', '', ...(skipped.length ? skipped.map((s) => `- ${s.route} (needs: ${s.missing.join(', ')})`) : ['- none']),
    '', '## Error-text heuristic matches (reported separately)', '', ...(heur.length ? heur : ['- none']), ''].join('\n');
}

(async () => {
  if (!EMAIL || !PASSWORD) { console.error(`credentials for role '${ROLE}' must be set in env (WEBUI_${ROLE === 'user' ? 'USER' : 'ADMIN'}_EMAIL/_PASSWORD)`); process.exit(2); }
  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.mkdirSync(RESULTS_DIR, { recursive: true });
  let base = (process.env.BASE_URL || 'https://waddles-alpha.penguintech.cloud:8443').replace(/\/$/, '');
  const args = [];
  let proxy = null;
  if (process.env.WEBUI_UPSTREAM && process.env.API_UPSTREAM) {
    proxy = await startProxy(process.env.WEBUI_UPSTREAM, process.env.API_UPSTREAM);
    base = `http://localhost:${proxy.port}`;
  } else if (process.env.HOST_HEADER) {
    const u = new URL(base);
    args.push(`--host-resolver-rules=MAP ${process.env.HOST_HEADER} ${u.hostname}`);
    base = `${u.protocol}//${process.env.HOST_HEADER}${u.port ? ':' + u.port : ''}`;
  }
  const browser = await launchBrowser(args);
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  await context.addInitScript(CONSENT_INIT);
  const filter = process.env.ROUTE_FILTER ? new RegExp(process.env.ROUTE_FILTER) : null;
  let routes = enumerateRoutes(ROUTES_FILE);
  if (filter) routes = routes.filter((r) => filter.test(r));
  const results = [];
  const skipped = [];
  try {
    // Public pages first, unauthenticated.
    const anon = await browser.newContext({ ignoreHTTPSErrors: true });
    await anon.addInitScript(CONSENT_INIT);
    const publicRoutes = ['/', '/communities', '/live', '/login', '/register', '/forgot-password', '/cookie-policy', '/privacy-rights', '/vendor/submit', '/vendor/submission-status', '/privacy-request/verify', '/design-system'];
    const anonShared = { page: null };
    for (const r of publicRoutes.filter((x) => routes.includes(x))) { const x = await visit(anon, base, r, r, anonShared); if (!anonShared.page && process.env.FULL_RELOAD !== '1') { anonShared.page = await anon.newPage(); await anonShared.page.goto(base + r, { waitUntil: 'networkidle' }).catch(() => {}); } x.session = 'anon'; results.push(x); process.stdout.write(`${x.fail ? 'FAIL' : x.knownOnly ? 'KNOWN' : 'ok  '} anon ${r}\n`); }
    if (routes.includes('/communities/:id')) {
      const x = await visit(anon, base, '/communities/:id', `/communities/${COMMUNITY_ID}`, anonShared); x.session = 'anon'; results.push(x);
    }
    await anon.close();

    for (let attempt = 1; ; attempt++) {
      try { await login(context, base); break; } catch (e) {
        if (attempt >= 4 || !/429/.test(String(e.message))) throw e;
        console.log(`login rate-limited (429), waiting ${attempt * 30}s`);
        await new Promise((r) => setTimeout(r, attempt * 30000));
      }
    }
    const probe = await context.newPage();
    await probe.goto(base + '/dashboard', { waitUntil: 'networkidle' }).catch(() => {});
    const params = await discoverParams(probe, base);
    await probe.close();
    fs.writeFileSync(path.join(RESULTS_DIR, 'params.json'), JSON.stringify(params, null, 2));
    for (const l of params.__log) console.log(`param ${l.param}: ${l.url} -> ${l.status} items=${l.count} ${l.value ? 'RESOLVED' : 'UNRESOLVED' + (l.reason ? ' (' + l.reason + ')' : '')}`);
    // One persistent authed page: first route is a real full load, the rest navigate client-side.
    const shared = { page: null };
    const done = new Set(results.map((r) => r.route));
    for (const route of routes) {
      if (done.has(route)) continue;
      if (route === '/auth/callback') { skipped.push({ route, missing: ['oauth code/state (cannot be synthesised)'] }); continue; }
      const f = fillRoute(route, params);
      if (f.missing) { skipped.push({ route, missing: f.missing }); continue; }
      const x = await visit(context, base, route, f.url, shared); x.session = ROLE; results.push(x);
      if (!shared.page && process.env.FULL_RELOAD !== '1') { shared.page = await context.newPage(); await shared.page.goto(base + f.url, { waitUntil: 'networkidle' }).catch(() => {}); }
      process.stdout.write(`${x.fail ? 'FAIL' : x.knownOnly ? 'KNOWN' : 'ok  '} ${f.url}\n`);
    }
  } finally {
    await browser.close();
    if (proxy) proxy.server.close();
  }
  const meta = { when: new Date().toISOString(), base: process.env.BASE_URL || base };
  fs.writeFileSync(path.join(RESULTS_DIR, 'results.json'), JSON.stringify({ meta, results, skipped }, null, 2));
  fs.writeFileSync(path.join(RESULTS_DIR, 'results.md'), toMarkdown(results, skipped, meta));
  const clean = results.filter((r) => !r.fail && !r.knownOnly).length;
  console.log(`\nvisited ${results.length}, clean ${clean}, known-pending ${results.filter((r) => r.knownOnly).length}, failing ${results.filter((r) => r.fail).length}, skipped ${skipped.length}`);
  process.exit(results.length === 0 ? 2 : results.some((r) => r.fail) ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
