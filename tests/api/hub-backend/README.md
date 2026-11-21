# Waddles Hub Backend API Tests

Comprehensive API integration tests for the Waddles Hub Backend.

## Test Coverage

- **auth.test.js** - Authentication endpoints (login, register, OAuth, password reset)
- **public.test.js** - Public endpoints (health, stats, communities list)
- **community.test.js** - Community management (CRUD, members, join/leave)
- **vendor.test.js** - Vendor submissions and dashboard
- More tests to be added for: admin, superadmin, polls, forms, streaming, music, workflow, calls (Waddles features)

## Running Tests

```bash
# Install dependencies
cd tests/api/hub-backend
npm install

# Run all tests
npm test

# Run specific test file
npm test auth.test.js

# Run with coverage
npm run test:coverage

# Watch mode
npm run test:watch
```

## Test Environment

Tests use the following environment variables:

- `API_BASE_URL` - API endpoint (default: http://localhost:8204)
- `SILENT_TESTS` - Suppress console output (default: false)

### Local Testing

```bash
# Ensure services are running
cd /path/to/waddlebot
docker compose up -d

# Run tests
cd tests/api/hub-backend
npm test
```

### Beta Testing

```bash
API_BASE_URL=https://waddlebot.penguintech.io npm test
```

## OAuth Mocking

OAuth platform responses (Twitch, Discord, YouTube, Slack) are mocked using `nock` to avoid requiring real OAuth tokens during testing. See `setup.js` for mock configurations.

## Test User Credentials

`ADMIN_USER` in `setup.js` reads `HUB_TEST_EMAIL`/`HUB_TEST_PASS`, falling
back to `INITIAL_ADMIN_EMAIL`/`INITIAL_ADMIN_PASSWORD` -- no admin
credential is hardcoded (CWE-798 fix). Set one of those to match whatever
the target `API_BASE_URL` instance was actually provisioned with.

- **Test User** (mocked, not a real login): test@test.com / Test123!

## Writing New Tests

1. Create a new test file: `<route-name>.test.js`
2. Follow the existing pattern:
   ```javascript
   const request = require('supertest');
   const API_BASE_URL = global.TEST_CONFIG.API_BASE_URL;

   describe('My API - /api/v1/my-route', () => {
     // Test cases...
   });
   ```
3. Add to `package.json` test coverage if needed

## Test Standards

- All tests must pass before committing
- Use descriptive test names
- Test both success and error cases
- Mock external services (OAuth, webhooks, etc.)
- Keep tests independent (no shared state)
- Clean up test data after tests
