/**
 * Jest Setup and Global Test Utilities
 * Configures test environment and provides mocking utilities
 */

const nock = require('nock');

// Test configuration
global.TEST_CONFIG = {
  API_BASE_URL: process.env.API_BASE_URL || 'http://localhost:8204',
  TEST_TIMEOUT: 10000,

  // Test user credentials
  TEST_USER: {
    email: 'test@test.com',
    password: 'Test123!',
    username: 'testuser'
  },

  // Admin user credentials. No hardcoded fallback (CWE-798) -- must match
  // whatever the target hub-api instance was actually provisioned with
  // (see config/postgres/migrations/081_seed_default_hub_admin.sql /
  // docker-compose.yml's INITIAL_ADMIN_EMAIL/PASSWORD).
  ADMIN_USER: {
    email: process.env.HUB_TEST_EMAIL || process.env.INITIAL_ADMIN_EMAIL || 'admin@localhost.local',
    password: process.env.HUB_TEST_PASS || process.env.INITIAL_ADMIN_PASSWORD || '',
    username: process.env.HUB_TEST_EMAIL || process.env.INITIAL_ADMIN_EMAIL || 'admin@localhost.local'
  }
};

// Mock OAuth platform responses (no real tokens needed)
global.mockOAuthPlatforms = () => {
  // Mock Twitch OAuth
  nock('https://id.twitch.tv')
    .persist()
    .post('/oauth2/token')
    .reply(200, {
      access_token: 'mock_twitch_access_token',
      refresh_token: 'mock_twitch_refresh_token',
      expires_in: 3600,
      scope: ['user:read:email'],
      token_type: 'bearer'
    });

  nock('https://api.twitch.tv')
    .persist()
    .get('/helix/users')
    .reply(200, {
      data: [{
        id: '12345',
        login: 'mockuser',
        display_name: 'Mock User',
        email: 'mock@twitch.tv',
        profile_image_url: 'https://example.com/avatar.jpg'
      }]
    });

  // Mock Discord OAuth
  nock('https://discord.com')
    .persist()
    .post('/api/oauth2/token')
    .reply(200, {
      access_token: 'mock_discord_access_token',
      refresh_token: 'mock_discord_refresh_token',
      expires_in: 604800,
      scope: 'identify email',
      token_type: 'Bearer'
    });

  nock('https://discord.com')
    .persist()
    .get('/api/users/@me')
    .reply(200, {
      id: '123456789',
      username: 'mockuser',
      discriminator: '1234',
      email: 'mock@discord.com',
      avatar: 'mock_avatar_hash'
    });

  // Mock YouTube/Google OAuth
  nock('https://oauth2.googleapis.com')
    .persist()
    .post('/token')
    .reply(200, {
      access_token: 'mock_google_access_token',
      refresh_token: 'mock_google_refresh_token',
      expires_in: 3599,
      scope: 'openid email profile',
      token_type: 'Bearer'
    });

  nock('https://www.googleapis.com')
    .persist()
    .get('/oauth2/v2/userinfo')
    .reply(200, {
      id: '1234567890',
      email: 'mock@gmail.com',
      verified_email: true,
      name: 'Mock User',
      picture: 'https://example.com/avatar.jpg'
    });

  // Mock Slack OAuth
  nock('https://slack.com')
    .persist()
    .post('/api/oauth.v2.access')
    .reply(200, {
      ok: true,
      access_token: 'mock_slack_access_token',
      token_type: 'bot',
      authed_user: {
        id: 'U12345',
        access_token: 'mock_slack_user_token'
      },
      team: {
        id: 'T12345',
        name: 'Mock Team'
      }
    });

  nock('https://slack.com')
    .persist()
    .post('/api/users.identity')
    .reply(200, {
      ok: true,
      user: {
        id: 'U12345',
        name: 'mockuser',
        email: 'mock@slack.com'
      }
    });
};

// Clean up all nock mocks after each test
afterEach(() => {
  nock.cleanAll();
});

// Set longer timeout for all tests
jest.setTimeout(global.TEST_CONFIG.TEST_TIMEOUT);

// Suppress console output during tests (optional)
if (process.env.SILENT_TESTS === 'true') {
  global.console = {
    ...console,
    log: jest.fn(),
    debug: jest.fn(),
    info: jest.fn(),
    warn: jest.fn(),
    error: jest.fn()
  };
}
