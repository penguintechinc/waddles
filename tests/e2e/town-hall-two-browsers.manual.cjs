'use strict';
/**
 * Manual two-browser smoke for the Town Hall (real WHIP/WHEP media against a deployed stack).
 * NOT part of the Playwright suite (the name does not match *.spec.js) and NOT run in CI: it needs a deployed
 * svc-streaming + hub-api with the `waddles.streaming.rtc` flag ON. See docs/rtc-town-hall-validation.md.
 *
 * Two Chromium contexts with fake camera/microphone devices join the same community Town Hall; each side must see the
 * other's remote tile with decoded video and audio, then both leave and the lobby must come back.
 *
 * Env (credentials from the environment only, never the command line):
 *   BASE_URL, COMMUNITY_ID, MEMBER_A_EMAIL, MEMBER_A_PASSWORD, MEMBER_B_EMAIL, MEMBER_B_PASSWORD,
 *   SHOT_DIR (optional, screenshots of the connected state), PLAYWRIGHT_OUTPUT_DIR (default /tmp/playwright-waddles).
 * Exits non-zero on any failed assertion.
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('@playwright/test');

const BASE_URL = (process.env.BASE_URL || '').replace(/\/+$/, '');
const COMMUNITY_ID = process.env.COMMUNITY_ID || '';
const SHOT_DIR = process.env.SHOT_DIR || '';
const WORK_DIR = process.env.PLAYWRIGHT_OUTPUT_DIR || '/tmp/playwright-waddles';
const SETTLE_TIMEOUT_MS = 45000;

/** Fails fast when a required env var is missing. */
function need(name) {
  const value = process.env[name];
  if (!value) throw new Error(`missing env var ${name}`);
  return value;
}

/**
 * Logs in through the hub-webui API from within the page (same flow as tests/screenshots/capture.cjs).
 * @param {import('@playwright/test').Page} page
 * @param {string} email
 * @param {string} password
 */
async function login(page, email, password) {
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(800);
  const cookies = await page.context().cookies();
  const csrf = (cookies.find((c) => c.name === 'XSRF-TOKEN') || {}).value;
  if (!csrf) throw new Error('no XSRF-TOKEN cookie after loading /login');
  const result = await page.evaluate(async ({ e, p, token }) => {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-XSRF-TOKEN': token },
      credentials: 'include',
      body: JSON.stringify({ email: e, password: p }),
    });
    return { ok: res.ok, status: res.status };
  }, { e: email, p: password, token: csrf });
  if (!result.ok) throw new Error(`login failed: HTTP ${result.status}`);
}

/**
 * Opens a fresh context with fake media devices, logs in and opens the Town Hall lobby.
 * @param {import('@playwright/test').Browser} browser
 * @param {string} label
 * @param {string} email
 * @param {string} password
 */
async function openTownHall(browser, label, email, password) {
  const context = await browser.newContext({ permissions: ['microphone', 'camera'] });
  const page = await context.newPage();
  page.on('pageerror', (err) => console.error(`[${label}] page error: ${err.message}`));
  await login(page, email, password);
  await page.goto(`${BASE_URL}/community/${COMMUNITY_ID}/town-hall`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.getByTestId('call-lobby').waitFor({ timeout: SETTLE_TIMEOUT_MS });
  return { context, page };
}

/**
 * Joins with camera + microphone and waits until connected.
 * @param {import('@playwright/test').Page} page
 */
async function join(page) {
  await page.getByLabel('Share my camera').check();
  await page.getByRole('button', { name: 'Join with microphone' }).click();
  await page.getByTestId('call-connected').waitFor({ timeout: SETTLE_TIMEOUT_MS });
}

/**
 * Asserts the page shows a remote tile whose media element decodes video and audio.
 * @param {import('@playwright/test').Page} page
 * @param {string} label
 */
async function expectRemoteMedia(page, label) {
  await page.waitForFunction(() => {
    const video = document.querySelector('[data-testid="remote-video"]');
    return video instanceof HTMLVideoElement && video.videoWidth > 0 && video.readyState >= 2;
  }, null, { timeout: SETTLE_TIMEOUT_MS });
  const audioBytes = await page.evaluate(() => {
    const video = document.querySelector('[data-testid="remote-video"]');
    return video && 'webkitAudioDecodedByteCount' in video ? video.webkitAudioDecodedByteCount : -1;
  });
  if (audioBytes === 0) throw new Error(`[${label}] remote video decoded but no audio bytes were decoded`);
  console.log(`[${label}] remote video decoded; audio decoded bytes: ${audioBytes < 0 ? 'n/a (not Chromium)' : audioBytes}`);
}

/** Runs the smoke; throws on the first failed expectation. */
async function main() {
  if (!BASE_URL || !COMMUNITY_ID) throw new Error('BASE_URL and COMMUNITY_ID are required');
  fs.mkdirSync(WORK_DIR, { recursive: true });
  const browser = await chromium.launch({
    args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--autoplay-policy=no-user-gesture-required'],
  });
  try {
    const a = await openTownHall(browser, 'A', need('MEMBER_A_EMAIL'), need('MEMBER_A_PASSWORD'));
    const b = await openTownHall(browser, 'B', need('MEMBER_B_EMAIL'), need('MEMBER_B_PASSWORD'));
    await join(a.page);
    await join(b.page);
    await expectRemoteMedia(a.page, 'A sees B');
    await expectRemoteMedia(b.page, 'B sees A');
    if (SHOT_DIR) {
      fs.mkdirSync(SHOT_DIR, { recursive: true });
      await a.page.screenshot({ path: path.join(SHOT_DIR, 'town-hall-connected.png'), fullPage: true });
    }
    await a.page.getByRole('button', { name: 'Leave room' }).click();
    await b.page.getByRole('button', { name: 'Leave room' }).click();
    await a.page.getByTestId('call-lobby').waitFor({ timeout: SETTLE_TIMEOUT_MS });
    await b.page.getByTestId('call-lobby').waitFor({ timeout: SETTLE_TIMEOUT_MS });
    console.log('PASS: two browsers exchanged audio/video and left cleanly');
  } finally {
    await browser.close();
    fs.rmSync(WORK_DIR, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error(`FAIL: ${err.message}`);
  process.exit(1);
});
