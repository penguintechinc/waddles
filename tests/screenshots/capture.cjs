'use strict';
/**
 * Marketing screenshot capture for Waddles (docs/screenshots/).
 *
 * Captures the logged-out pages in a fresh context, logs in via the hub-webui
 * API, then captures every authenticated page. Fails loudly (non-zero exit) on
 * login failure, redirects to /login, HTTP >= 400, rendered error text, or zero
 * captures; empty-state text is a warning unless STRICT_EMPTY=1.
 *
 * Env: BASE_URL, SCREENSHOT_EMAIL, SCREENSHOT_PASSWORD, COMMUNITY_ID,
 *      TENANT_SLUG, OUT_DIR, PLAYWRIGHT_OUTPUT_DIR, STRICT_EMPTY, SETTLE_MS.
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const { buildPages } = require('./pages.cjs');

const BASE_URL = (process.env.BASE_URL || 'http://localhost:8060').replace(/\/+$/, '');
const EMAIL = process.env.SCREENSHOT_EMAIL || process.env.ADMIN_EMAIL || '';
const PASSWORD = process.env.SCREENSHOT_PASSWORD || process.env.ADMIN_PASSWORD || '';
const COMMUNITY_ID = process.env.COMMUNITY_ID || '9001';
const TENANT_SLUG = process.env.TENANT_SLUG || 'default';
const OUT_DIR = path.resolve(process.env.OUT_DIR || path.join(__dirname, '..', '..', 'docs', 'screenshots'));
const WORK_DIR = process.env.PLAYWRIGHT_OUTPUT_DIR || '/tmp/playwright-waddles';
const STRICT_EMPTY = process.env.STRICT_EMPTY === '1';
const SETTLE_MS = parseInt(process.env.SETTLE_MS || '1500', 10);

const ERROR_RE = /(Traceback \(most recent call last\)|Internal Server Error|Something went wrong|Application error|404\s*[-–]?\s*Not Found|Page not found|Unexpected Application Error)/i;
const EMPTY_RE = /(no data|nothing here|no results|no .{1,40} (found|yet)|empty)/i;

/** Sleep helper. */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Log in through the hub-webui API from within the page so cookies and the
 * localStorage token are set exactly as the SPA expects.
 * @param {import('playwright').Page} page
 * @returns {Promise<void>} resolves on success, throws on failure
 */
async function login(page) {
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await sleep(800);
  const cookies = await page.context().cookies();
  const csrf = (cookies.find((c) => c.name === 'XSRF-TOKEN') || {}).value;
  if (!csrf) throw new Error('no XSRF-TOKEN cookie after loading /login');
  const result = await page.evaluate(async ({ email, password, token }) => {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-XSRF-TOKEN': token },
      credentials: 'include',
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
    const data = await res.json();
    if (!data.success || !data.token) return { ok: false, error: 'response lacked token' };
    localStorage.setItem('token', data.token);
    return { ok: true };
  }, { email: EMAIL, password: PASSWORD, token: csrf });
  if (!result.ok) throw new Error(`login failed: ${result.error}`);
}

/**
 * Capture one page; returns {status, note} where status is ok|warn|fail.
 * @param {import('playwright').Page} page
 * @param {{name: string, path: string}} entry
 * @param {boolean} authed whether a redirect to /login counts as failure
 */
async function capture(page, entry, authed) {
  const resp = await page.goto(`${BASE_URL}${entry.path}`, { waitUntil: 'networkidle', timeout: 60000 });
  await sleep(SETTLE_MS);
  if (resp && resp.status() >= 400) return { status: 'fail', note: `HTTP ${resp.status()}` };
  if (authed && page.url().includes('/login')) return { status: 'fail', note: 'redirected to /login' };
  const text = await page.evaluate(() => document.body.innerText || '');
  if (ERROR_RE.test(text)) return { status: 'fail', note: 'error text rendered' };
  await page.screenshot({ path: path.join(OUT_DIR, `${entry.name}.png`), fullPage: false });
  if (EMPTY_RE.test(text)) return { status: STRICT_EMPTY ? 'fail' : 'warn', note: 'possible empty state' };
  return { status: 'ok', note: '' };
}

/** Capture a list of pages in one context, tallying results. */
async function runSet(page, entries, authed, tally) {
  for (const entry of entries) {
    let r;
    try {
      r = await capture(page, entry, authed);
    } catch (err) {
      r = { status: 'fail', note: err.message.split('\n')[0] };
    }
    tally[r.status].push(`${entry.name}${r.note ? ` (${r.note})` : ''}`);
    console.log(`  [${r.status}] ${entry.name} ${r.note}`);
  }
}

/** Entry point: run both sets, print counts, exit non-zero on any failure. */
async function main() {
  if (!EMAIL || !PASSWORD) {
    throw new Error('SCREENSHOT_EMAIL/SCREENSHOT_PASSWORD (or ADMIN_EMAIL/ADMIN_PASSWORD) required');
  }
  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.mkdirSync(WORK_DIR, { recursive: true });
  const { publicPages, authPages } = buildPages({ communityId: COMMUNITY_ID, tenantSlug: TENANT_SLUG });
  const tally = { ok: [], warn: [], fail: [] };
  const browser = await chromium.launch({ headless: true });
  try {
    const viewport = { width: 1920, height: 1080 };
    const pubCtx = await browser.newContext({ viewport });
    console.log(`Unauthenticated pages (${publicPages.length}) against ${BASE_URL}`);
    await runSet(await pubCtx.newPage(), publicPages, false, tally);
    await pubCtx.close();

    const authCtx = await browser.newContext({ viewport });
    const page = await authCtx.newPage();
    await login(page);
    console.log(`Authenticated pages (${authPages.length}), community ${COMMUNITY_ID}`);
    await runSet(page, authPages, true, tally);
    await authCtx.close();
  } finally {
    await browser.close();
  }
  const total = publicPages.length + authPages.length;
  console.log(`\nExamined ${total}: ${tally.ok.length} ok, ${tally.warn.length} warn, ${tally.fail.length} fail -> ${OUT_DIR}`);
  if (tally.warn.length) console.log(`Warnings (review visually): ${tally.warn.join(', ')}`);
  if (tally.fail.length) console.log(`FAILED: ${tally.fail.join(', ')}`);
  if (tally.ok.length + tally.warn.length === 0 || tally.fail.length) process.exit(1);
}

main().catch((err) => {
  console.error(`screenshot capture aborted: ${err.message}`);
  process.exit(1);
});
