# Waddles Integration Tests

Tests for multi-service workflows, database transactions, and inter-module communication.

## Test Coverage

- **database.test.js** - Database transactions, referential integrity, data consistency
- **websocket.test.js** - Real-time WebSocket communication
- More tests to be added: module-to-module communication, OAuth flows, webhook handling (Waddles features)

## Running Tests

```bash
# Install dependencies
cd tests/integration
npm install

# Run all tests
npm test

# Run specific test file
npm test database.test.js
```

## Environment Variables

```bash
# Database
DB_HOST=localhost
DB_PORT=5432
DB_NAME=waddlebot
DB_USER=postgres
DB_PASSWORD=postgres

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# WebSocket
WS_URL=ws://localhost:8204

# Test token for authenticated WS
TEST_TOKEN=your-test-token
```

## Prerequisites

Integration tests require running services:
- PostgreSQL database
- Redis cache
- Hub API (with WebSocket support)

```bash
# Start services
docker compose up -d

# Run tests
npm test
```

## Writing Integration Tests

Integration tests verify that multiple components work together correctly:

1. Database transactions across tables
2. WebSocket real-time communication
3. Module-to-module API calls
4. Cache consistency with database
5. OAuth flows with external providers (mocked)

Tests should:
- Set up test data in beforeAll/beforeEach
- Clean up test data in afterAll/afterEach
- Test actual service integration (not mocked)
- Verify data consistency across services
