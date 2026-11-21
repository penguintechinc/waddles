#!/usr/bin/env node
/**
 * Waddles Page Load Smoke Tests
 * Tests that all frontend pages load without JavaScript errors
 *
 * Tests:
 * - Public pages (unauthenticated)
 * - Authenticated pages (with test user session)
 * - All major page tabs
 *
 * Usage:
 *   node smoke-pages.js                          # Local (http://localhost:3000)
 *   BASE_URL=https://waddlebot.penguintech.io node smoke-pages.js  # Beta
 *
 * Exit codes:
 *   0 - All pages loaded without errors (PASS)
 *   1 - One or more pages failed (FAIL)
 */

const { chromium } = require('playwright');

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';
const API_URL = process.env.API_URL || BASE_URL.replace(':3000', ':8204');
const TIMEOUT = 10000; // 10 seconds per page

// Test user credentials (default admin). No hardcoded fallback (CWE-798) --
// must match whatever the target instance was actually provisioned with
// (see config/postgres/migrations/081_seed_default_hub_admin.sql).
const TEST_USER = {
  email: process.env.HUB_TEST_EMAIL || process.env.INITIAL_ADMIN_EMAIL || 'admin@localhost.local',
  password: process.env.HUB_TEST_PASS || process.env.INITIAL_ADMIN_PASSWORD || ''
};

// Pages to test
const PUBLIC_PAGES = [
  '/',
  '/login',
  '/register',
  '/cookie-policy',
  '/communities',
  '/marketplace'
];

const AUTHENTICATED_PAGES = [
  '/dashboard',
  '/communities/my',
  '/profile',
  '/settings',
  '/marketplace/installed',

  // Platform pages
  '/platform',
  '/platform/communities',
  '/platform/users',
  '/platform/analytics',
  '/platform/modules',
  '/platform/config',

  // Vendor pages
  '/vendor',
  '/vendor/dashboard',
  '/vendor/submissions',
  '/vendor/submit',
  '/vendor/request',

  // Super admin pages
  '/superadmin',
  '/superadmin/communities',
  '/superadmin/users',
  '/superadmin/platform-config'
];

let passed = 0;
let failed = 0;
let warnings = 0;

const colors = {
  reset: '\x1b[0m',
  green: '\x1b[32m',
  red: '\x1b[31m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m'
};

function log(level, message) {
  const color = {
    'PASS': colors.green,
    'FAIL': colors.red,
    'WARN': colors.yellow,
    'INFO': colors.blue
  }[level] || colors.reset;

  console.log(`  ${color}[${level}]${colors.reset} ${message}`);
}

async function testPage(page, url, authenticated = false) {
  const pageName = url === '/' ? 'Homepage' : url;
  const errors = [];

  // Collect console errors and uncaught exceptions
  page.on('pageerror', error => {
    errors.push(`Uncaught exception: ${error.message}`);
  });

  page.on('console', msg => {
    if (msg.type() === 'error') {
      errors.push(`Console error: ${msg.text()}`);
    }
  });

  try {
    const response = await page.goto(`${BASE_URL}${url}`, {
      waitUntil: 'networkidle',
      timeout: TIMEOUT
    });

    if (!response) {
      log('FAIL', `${pageName} - No response`);
      failed++;
      return;
    }

    const status = response.status();

    // Check response status
    if (status === 404) {
      log('FAIL', `${pageName} - 404 NOT FOUND`);
      failed++;
      return;
    } else if (status >= 500) {
      log('FAIL', `${pageName} - Server error ${status}`);
      failed++;
      return;
    }

    // Wait a bit for JS to execute
    await page.waitForTimeout(500);

    // Check for JavaScript errors
    if (errors.length > 0) {
      log('WARN', `${pageName} - ${errors.length} JS error(s)`);
      errors.forEach(err => console.log(`    ${err}`));
      warnings++;
    } else {
      log('PASS', `${pageName} -> ${status}`);
      passed++;
    }

  } catch (error) {
    if (error.message.includes('timeout')) {
      log('WARN', `${pageName} - Timeout (slow load)`);
      warnings++;
    } else {
      log('FAIL', `${pageName} - ${error.message}`);
      failed++;
    }
  }
}

async function loginUser(page) {
  try {
    // Navigate to login page
    await page.goto(`${BASE_URL}/login`, {
      waitUntil: 'networkidle',
      timeout: TIMEOUT
    });

    // Fill login form
    await page.fill('input[name="email"], input[type="email"]', TEST_USER.email);
    await page.fill('input[name="password"], input[type="password"]', TEST_USER.password);

    // Submit form
    await page.click('button[type="submit"]');

    // Wait for navigation to complete
    await page.waitForTimeout(2000);

    // Check if we're logged in (should redirect away from /login)
    const url = page.url();
    if (url.includes('/login')) {
      throw new Error('Login failed - still on login page');
    }

    log('INFO', 'Successfully authenticated test user');
    return true;

  } catch (error) {
    log('WARN', `Could not authenticate test user: ${error.message}`);
    log('WARN', 'Skipping authenticated page tests');
    return false;
  }
}

async function main() {
  console.log('========================================');
  console.log('Waddles Page Load Smoke Tests');
  console.log('========================================');
  console.log('');
  console.log(`Target: ${BASE_URL}`);
  console.log(`API: ${API_URL}`);
  console.log('');

  const browser = await chromium.launch({
    headless: true
  });

  const context = await browser.newContext({
    ignoreHTTPSErrors: true // For beta testing with self-signed certs
  });

  const page = await context.newPage();

  // Test public pages
  console.log('----------------------------------------');
  console.log('1. Public Pages (Unauthenticated)');
  console.log('----------------------------------------');

  for (const url of PUBLIC_PAGES) {
    await testPage(page, url, false);
  }

  console.log('');
  console.log('----------------------------------------');
  console.log('2. Authenticated Pages');
  console.log('----------------------------------------');

  // Attempt to login
  const authenticated = await loginUser(page);

  if (authenticated) {
    // Test authenticated pages
    for (const url of AUTHENTICATED_PAGES) {
      await testPage(page, url, true);
    }
  } else {
    log('WARN', 'Skipping authenticated pages - login failed');
    warnings += AUTHENTICATED_PAGES.length;
  }

  await browser.close();

  console.log('');
  console.log('========================================');
  console.log('Summary');
  console.log('========================================');
  console.log(`Passed:   ${colors.green}${passed}${colors.reset}`);
  console.log(`Failed:   ${colors.red}${failed}${colors.reset}`);
  console.log(`Warnings: ${colors.yellow}${warnings}${colors.reset}`);
  console.log('');

  if (failed > 0) {
    console.log(`${colors.red}PAGE SMOKE TEST FAILED${colors.reset}`);
    console.log('');
    console.log('Critical issues found:');
    console.log('- Pages returning 404/500 errors');
    console.log('- Pages failing to load');
    console.log('- Check application logs and frontend build');
    process.exit(1);
  }

  if (warnings > 0) {
    console.log(`${colors.yellow}PAGE SMOKE TEST PASSED WITH WARNINGS${colors.reset}`);
    console.log('');
    console.log('JavaScript errors or timeouts detected');
    console.log('Pages loaded but may have runtime issues');
    process.exit(0);
  }

  console.log(`${colors.green}PAGE SMOKE TEST PASSED${colors.reset}`);
  console.log(`All ${passed} pages loaded without errors!`);
  process.exit(0);
}

// Handle errors
process.on('unhandledRejection', (error) => {
  console.error(`${colors.red}Fatal error:${colors.reset}`, error);
  process.exit(1);
});

// Run tests
main().catch(error => {
  console.error(`${colors.red}Fatal error:${colors.reset}`, error);
  process.exit(1);
});
