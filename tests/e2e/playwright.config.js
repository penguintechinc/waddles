/**
 * Playwright Test Configuration
 * @see https://playwright.dev/docs/test-configuration
 *
 * --- Usage ---
 *
 * Local dev server (default):
 *   cd tests/e2e && npx playwright test
 *   Requires: hub frontend running on http://localhost:3000
 *
 * Against beta via kubectl port-forward (bypasses Cloudflare WAF):
 *   # Forward hub service to local port 3001
 *   kubectl port-forward svc/waddlebot-hub-webui-v3 3001:8080 -n waddlebot --context dal2-beta &
 *   BASE_URL=http://localhost:3001 npx playwright test
 *
 * NOTE: Playwright cannot bypass Cloudflare using the ALB Host-header trick
 * that curl uses — browsers block overriding the Host header (forbidden header).
 * Use kubectl port-forward or run against a non-Cloudflare-proxied endpoint.
 *
 * Environment variables:
 *   BASE_URL          - Target URL (default: http://localhost:3000)
 *   HUB_TEST_EMAIL    - Login email (default: admin@localhost.local)
 *   HUB_TEST_PASS     - Login password (default: admin123)
 */

const { defineConfig, devices } = require('@playwright/test');
const path = require('path');

const AUTH_STATE_PATH = path.join(__dirname, '.auth-state.json');

// Inter-test cooldown (ms) — prevents rate-limit cascades on beta where each
// page navigation fires 4-5 parallel API requests.  Default 2s; override via
// env var for local runs where the server has no rate limiter.
const TEST_COOLDOWN_MS = parseInt(process.env.TEST_COOLDOWN_MS || '2000', 10);

module.exports = defineConfig({
  testDir: './',
  fullyParallel: false,  // Allow parallel execution within projects
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1, // Force serial execution to avoid 429 rate-limit cascades on beta
  reporter: 'html',

  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:3000',
    extraHTTPHeaders: process.env.PLAYWRIGHT_HOST_HEADER
      ? { host: process.env.PLAYWRIGHT_HOST_HEADER }
      : undefined,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    ignoreHTTPSErrors: true, // For self-signed / internal certs
  },

  // Seed test data if missing — checks for test user, creates admin + community
  globalSetup: require.resolve('./global-setup.js'),

  timeout: 90000, // 90 seconds per test (allows for rate-limit retries)

  projects: [
    // Setup project: logs in once and saves auth state for reuse
    {
      name: 'setup',
      testMatch: 'auth.setup.js',
      use: { ...devices['Desktop Chrome'] },
    },
    // Authenticated tests run FIRST: reuse storageState (no login API calls)
    // This keeps the rate limit counter low for the auth tests that follow.
    {
      name: 'authenticated-tests',
      testMatch: [
        'community-creation.spec.js', 'community-workflow.spec.js', 'dashboard.spec.js',
        'vendor-workflow.spec.js', 'superadmin.spec.js',
        'platform-dashboard.spec.js', 'calendar-events.spec.js', 'calendar-settings.spec.js',
        'calendar-booking.spec.js', 'support-workflow.spec.js', 'form-results-visibility.spec.js',
        'module-management.spec.js', 'admin-sidebar.spec.js', 'modules-marketplace.spec.js',
        'module-config-pages.spec.js', 'community-interaction.spec.js',
        'interaction-channels.spec.js',
      ],
      use: {
        ...devices['Desktop Chrome'],
        storageState: AUTH_STATE_PATH,
      },
      dependencies: ['setup'],
    },
    // Auth tests: test login/logout flows with fresh (unauthenticated) contexts.
    // Depends on setup completing, runs after authenticated-tests (which use
    // storageState and don't make login calls) to minimize rate limit pressure.
    {
      name: 'auth-tests',
      testMatch: ['auth.spec.js', 'auth-workflow.spec.js', 'marketing-text.spec.js', 'spotlighted-communities.spec.js'],
      use: { ...devices['Desktop Chrome'] },
      dependencies: ['setup'],
    },
  ],

  // Auto-start local dev server when running locally (uncomment to enable):
  // webServer: {
  //   command: 'npm run dev --prefix admin/hub_module/frontend',
  //   port: 3000,
  //   reuseExistingServer: !process.env.CI,
  // },
});
