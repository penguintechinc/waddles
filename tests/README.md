# Waddles Test Suite

Comprehensive testing framework for Waddles covering smoke tests, API integration, multi-service integration, and end-to-end workflows.

## Test Directory Structure

```
tests/
├── smoke/                   # Smoke tests (<2 min execution)
│   ├── smoke-api-comprehensive.sh   # All API endpoints
│   ├── smoke-pages.js              # All frontend pages
│   └── run-all.sh                  # Master test runner
├── api/                     # API integration tests
│   └── hub-backend/        # Hub backend API tests
│       ├── auth.test.js
│       ├── public.test.js
│       ├── community.test.js
│       └── vendor.test.js
├── integration/            # Multi-service integration tests
│   ├── database.test.js   # Database transactions
│   └── websocket.test.js  # Real-time communication
├── e2e/                    # End-to-end workflow tests
│   ├── auth-workflow.spec.js
│   ├── community-workflow.spec.js
│   └── vendor-workflow.spec.js
├── alpha-smoke-test.sh     # Container health checks
└── beta-smoke-test.sh      # Beta deployment verification
```

## Quick Start

```bash
# Run all smoke tests (local)
./tests/smoke/run-all.sh local

# Run all smoke tests (beta)
./tests/smoke/run-all.sh beta

# Run API tests
cd tests/api/hub-backend && npm install && npm test

# Run integration tests
cd tests/integration && npm install && npm test

# Run E2E tests
cd tests/e2e && npm install && npx playwright install && npm test
```

## Test Types

### 1. Smoke Tests (Mandatory)
**Purpose**: Verify basic functionality before deployment
**Execution Time**: <2 minutes
**Required**: Before every commit and deployment

**Coverage**:
- Container health (40 services)
- API endpoints (100+ endpoints across 18 route files)
- Page loads (79 pages including authenticated)
- JavaScript error detection

**Commands**:
```bash
# Alpha: Container health
./tests/alpha-smoke-test.sh

# Beta: API and pages
./tests/beta-smoke-test.sh

# Comprehensive: All endpoints
cd tests/smoke && ./smoke-api-comprehensive.sh

# Page loads: All pages
cd tests/smoke && npm install && node smoke-pages.js

# Master runner: All smoke tests
./tests/smoke/run-all.sh [local|beta]
```

### 2. API Integration Tests
**Purpose**: Test all API endpoints with authentication and error cases
**Execution Time**: 5-10 minutes
**Required**: Before committing API changes

**Coverage**:
- Authentication (login, register, OAuth, password reset)
- Public APIs (health, stats, communities)
- Community CRUD operations
- Vendor submissions
- Admin/Superadmin endpoints
- Input validation and error handling

**OAuth Mocking**: All OAuth platforms (Twitch, Discord, YouTube, Slack) are mocked - no real tokens needed

**Commands**:
```bash
cd tests/api/hub-backend
npm install
npm test                    # All tests
npm test auth.test.js       # Specific test
npm run test:coverage       # With coverage
```

### 3. Integration Tests
**Purpose**: Test multi-service workflows and data consistency
**Execution Time**: 5-10 minutes
**Required**: Before major releases

**Coverage**:
- Database transactions and referential integrity
- Redis cache consistency
- WebSocket real-time communication
- Module-to-module API calls
- Cross-service data flow

**Commands**:
```bash
cd tests/integration
npm install
npm test
```

### 4. End-to-End Tests
**Purpose**: Test complete user workflows in browser
**Execution Time**: 10-15 minutes
**Required**: Before major releases

**Coverage**:
- User registration → login → dashboard
- Community creation → configuration → module installation
- Vendor submission → review → approval
- Admin workflows

**Commands**:
```bash
cd tests/e2e
npm install
npx playwright install chromium
npm test                    # Headless
npm run test:headed         # Headed browser
npm run test:debug          # Debug mode
npm run test:ui             # Interactive UI
```

## Environment Variables

### Local Testing
```bash
# API tests
API_BASE_URL=http://localhost:8204

# Frontend tests
BASE_URL=http://localhost:3000

# Database
DB_HOST=localhost
DB_PORT=5432
DB_NAME=waddlebot
DB_USER=postgres
DB_PASSWORD=postgres

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
```

### Beta Testing
```bash
API_BASE_URL=https://waddlebot.penguintech.io
BASE_URL=https://waddlebot.penguintech.io
```

## Pre-Commit Requirements

**Per project standards (CLAUDE.md), before every commit:**

1. ✅ Run smoke tests (mandatory)
```bash
./tests/smoke/run-all.sh local
```

2. ✅ Run API tests for modified routes
```bash
cd tests/api/hub-backend && npm test <route>.test.js
```

3. ✅ Verify no errors in logs

## CI/CD Integration

Smoke tests run automatically on:
- Every commit (alpha smoke test)
- Pull requests (comprehensive smoke tests)
- Deployments (beta smoke tests)

Full test suite runs on:
- Pull requests to main
- Release branches
- Nightly builds

## Test Standards

All tests must:
- ✅ Pass before committing
- ✅ Be independent (no shared state)
- ✅ Clean up test data
- ✅ Have descriptive names
- ✅ Test both success and error cases
- ✅ Mock external services (OAuth, webhooks)
- ✅ Run in <30 seconds per test file (except E2E)

## Adding New Tests

### Smoke Tests
Add endpoints to `tests/smoke/smoke-api-comprehensive.sh` or pages to `tests/smoke/smoke-pages.js`

### API Tests
Create `tests/api/hub-backend/<route>.test.js` following existing patterns

### Integration Tests
Create `tests/integration/<feature>.test.js` for multi-service flows

### E2E Tests
Create `tests/e2e/<workflow>.spec.js` for complete user journeys

## Troubleshooting

### Smoke Tests Failing
- Ensure all services are running: `docker compose up -d`
- Check logs: `docker compose logs -f hub-api`
- Verify database is healthy: `docker compose ps`

### API Tests Failing
- Check API is accessible: `curl http://localhost:8204/healthz`
- Verify a test admin exists and `HUB_TEST_EMAIL`/`HUB_TEST_PASS` (or
  `INITIAL_ADMIN_EMAIL`/`INITIAL_ADMIN_PASSWORD`) match it -- no default
  admin credential is created automatically (CWE-798 fix)
- Check for port conflicts

### Integration Tests Failing
- Verify database connection
- Check Redis is running
- Ensure WebSocket server is enabled

### E2E Tests Failing
- Frontend must be built: `cd admin/hub_module/frontend && npm run build`
- Check browser installation: `npx playwright install chromium`
- View test report: `npm run report`

## Test Coverage Goals

| Component | Current | Goal |
|-----------|---------|------|
| Smoke Tests | 100% | 100% |
| API Routes | 40% | 100% |
| Frontend Pages | 50% | 100% |
| Critical Workflows | 30% | 100% |

## Resources

- [Jest Documentation](https://jestjs.io/)
- [Playwright Documentation](https://playwright.dev/)
- [Supertest Documentation](https://github.com/visionmedia/supertest)
- [Project Testing Standards](../docs/TESTING.md)
