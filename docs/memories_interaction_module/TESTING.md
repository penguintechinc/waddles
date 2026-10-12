# Memories Interaction Module - Testing Guide

## Test Strategy

The Memories Interaction Module uses a multi-level testing approach:

1. **Unit Tests**: Test individual service methods in isolation
2. **Integration Tests**: Test service layer with real database
3. **API Tests**: Test full HTTP endpoints with request/response validation
4. **Smoke Tests**: Quick sanity checks (health, basic operations)

## Test Framework

- **pytest**: Test runner
- **pytest-asyncio**: Async test support
- **pytest-cov**: Code coverage reporting

## Running Tests

### Run All Tests

```bash
cd action/interactive/memories_interaction_module
pytest -v
```

### Run Specific Test File

```bash
pytest tests/test_quote_service.py -v
```

### Run Single Test

```bash
pytest tests/test_quote_service.py::test_add_quote -v
```

### Run with Coverage

```bash
pytest --cov=. --cov-report=html
```

### Run Only Fast Tests

```bash
pytest -m "not slow" -v
```

## Sample Test Data

### Create Test Quotes

```python
# Sample quote for testing
test_quote = {
    "community_id": 1,
    "quote_text": "The only way to do great work is to love what you do.",
    "created_by_username": "alice",
    "created_by_user_id": 101,
    "author_username": "stevejobs",
    "author_user_id": 102,
    "category": "inspiration"
}

# Create via service
quote = await quote_service.add_quote(**test_quote)
assert quote['id'] > 0
assert quote['votes'] == 0
```

### Create Test Bookmarks

```python
test_bookmark = {
    "community_id": 1,
    "url": "https://www.python.org/",
    "created_by_username": "bob",
    "created_by_user_id": 103,
    "title": "Python Official Site",
    "description": "Official Python programming language site",
    "tags": ["programming", "python", "documentation"],
    "auto_fetch_metadata": False
}

bookmark = await bookmark_service.add_bookmark(**test_bookmark)
assert bookmark['id'] > 0
```

### Create Test Reminders

```python
from datetime import datetime, timedelta

test_reminder = {
    "community_id": 1,
    "user_id": 101,
    "username": "alice",
    "reminder_text": "Check the discussion forum",
    "remind_at": datetime.utcnow() + timedelta(hours=1),
    "channel": "discord",
    "platform_channel_id": "discord_123"
}

reminder = await reminder_service.create_reminder(**test_reminder)
assert reminder['id'] > 0
```

## Unit Test Examples

### Test Quote Service

```python
import pytest
from services.quote_service import QuoteService

@pytest.fixture
async def quote_service(dal):
    return QuoteService(dal)

@pytest.mark.asyncio
async def test_add_quote(quote_service):
    result = await quote_service.add_quote(
        community_id=1,
        quote_text="Test quote",
        created_by_username="testuser",
        created_by_user_id=1
    )
    assert result['id'] is not None
    assert result['quote_text'] == "Test quote"
    assert result['votes'] == 0

@pytest.mark.asyncio
async def test_search_quotes(quote_service):
    # Add test data
    await quote_service.add_quote(
        community_id=1,
        quote_text="Innovation",
        created_by_username="alice",
        created_by_user_id=1,
        category="tech"
    )
    
    # Search
    results = await quote_service.search_quotes(
        community_id=1,
        search_query="Innovation"
    )
    assert len(results) > 0

@pytest.mark.asyncio
async def test_vote_quote(quote_service):
    quote = await quote_service.add_quote(
        community_id=1,
        quote_text="Test",
        created_by_username="alice",
        created_by_user_id=1
    )
    
    result = await quote_service.vote_quote(
        community_id=1,
        quote_id=quote['id'],
        user_id=2,
        username="bob",
        vote_type="up"
    )
    assert result['votes'] == 1

@pytest.mark.asyncio
async def test_delete_quote_authorized(quote_service):
    quote = await quote_service.add_quote(
        community_id=1,
        quote_text="Test",
        created_by_username="alice",
        created_by_user_id=1
    )
    
    # Delete by creator
    success = await quote_service.delete_quote(
        community_id=1,
        quote_id=quote['id'],
        user_id=1  # Creator
    )
    assert success is True

@pytest.mark.asyncio
async def test_delete_quote_unauthorized(quote_service):
    quote = await quote_service.add_quote(
        community_id=1,
        quote_text="Test",
        created_by_username="alice",
        created_by_user_id=1
    )
    
    # Try to delete as non-creator
    success = await quote_service.delete_quote(
        community_id=1,
        quote_id=quote['id'],
        user_id=999  # Not creator
    )
    assert success is False
```

### Test Bookmark Service

```python
@pytest.mark.asyncio
async def test_fetch_url_metadata(bookmark_service):
    metadata = await bookmark_service._fetch_url_metadata(
        "https://www.example.com"
    )
    assert 'title' in metadata
    assert 'description' in metadata

@pytest.mark.asyncio
async def test_get_popular_bookmarks(bookmark_service):
    # Add test bookmarks with visits
    for i in range(3):
        bookmark = await bookmark_service.add_bookmark(
            community_id=1,
            url=f"https://example{i}.com",
            created_by_username="alice",
            created_by_user_id=1
        )
        # Simulate visits
        for _ in range(i + 1):
            await bookmark_service.increment_visits(1, bookmark['id'])
    
    # Get popular
    popular = await bookmark_service.get_popular_bookmarks(community_id=1)
    assert len(popular) > 0
    # Most visited should be first
    assert popular[0]['visits'] >= popular[-1]['visits']
```

### Test Reminder Service

```python
@pytest.mark.asyncio
async def test_parse_relative_time(reminder_service):
    base = datetime(2026, 2, 16, 10, 0, 0)
    
    result = await reminder_service.parse_relative_time("5m", base)
    assert result.minute == 5
    
    result = await reminder_service.parse_relative_time("2h", base)
    assert result.hour == 12
    
    result = await reminder_service.parse_relative_time("1d", base)
    assert result.day == 17

@pytest.mark.asyncio
async def test_create_recurring_reminder(reminder_service):
    result = await reminder_service.create_reminder(
        community_id=1,
        user_id=1,
        username="alice",
        reminder_text="Daily standup",
        remind_at=datetime.utcnow().replace(hour=10),
        recurring_rule="FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"
    )
    assert result['is_recurring'] is True
    assert result['recurring_rule'] is not None
```

## Integration Tests

### Test Full Workflow

```python
@pytest.mark.asyncio
async def test_full_quote_workflow(quote_service):
    # Create
    quote = await quote_service.add_quote(
        community_id=1,
        quote_text="Great quote",
        created_by_username="alice",
        created_by_user_id=1
    )
    quote_id = quote['id']
    
    # Retrieve
    retrieved = await quote_service.get_quote(community_id=1, quote_id=quote_id)
    assert retrieved['id'] == quote_id
    
    # Vote
    await quote_service.vote_quote(
        community_id=1,
        quote_id=quote_id,
        user_id=2,
        username="bob",
        vote_type="up"
    )
    
    # Search
    results = await quote_service.search_quotes(community_id=1)
    assert quote_id in [r['id'] for r in results]
    
    # Delete
    success = await quote_service.delete_quote(
        community_id=1,
        quote_id=quote_id,
        user_id=1
    )
    assert success is True
```

## API Integration Tests

### Test HTTP Endpoints

```bash
# Create quote
curl -X POST http://localhost:8031/api/v1/memories/quotes \
  -H "Content-Type: application/json" \
  -d '{
    "community_id": 1,
    "quote_text": "Test quote",
    "created_by_username": "alice",
    "created_by_user_id": 1
  }' | jq .

# Search quotes
curl http://localhost:8031/api/v1/memories/quotes/1?q=test | jq .

# Vote on quote (id=1)
curl -X POST http://localhost:8031/api/v1/memories/quotes/1/1/vote \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 2,
    "username": "bob",
    "vote_type": "up"
  }' | jq .

# Delete quote (requires auth)
curl -X DELETE http://localhost:8031/api/v1/memories/quotes/1/1 \
  -H "Content-Type: application/json" \
  -d '{"user_id": 1}' | jq .
```

## Smoke Tests

### Quick Health Check

```bash
#!/bin/bash
set -e

URL="http://localhost:8031"

echo "Health check..."
curl -f $URL/health

echo "Create quote..."
curl -X POST $URL/api/v1/memories/quotes \
  -H "Content-Type: application/json" \
  -d '{
    "community_id": 1,
    "quote_text": "Smoke test quote",
    "created_by_username": "tester",
    "created_by_user_id": 1
  }'

echo "Get stats..."
curl $URL/api/v1/memories/quotes/1/stats

echo "All smoke tests passed!"
```

Run with:
```bash
bash smoke-test.sh
```

## Performance Tests

### Load Testing with Apache Bench

```bash
# Single quote creation
ab -n 100 -c 10 -p quote.json \
  -T application/json \
  http://localhost:8031/api/v1/memories/quotes

# Search quotes
ab -n 1000 -c 50 \
  http://localhost:8031/api/v1/memories/quotes/1?q=test
```

### Load Testing with Locust

```python
from locust import HttpUser, task, between

class MemoriesUser(HttpUser):
    wait_time = between(1, 3)
    
    @task
    def search_quotes(self):
        self.client.get("/api/v1/memories/quotes/1?q=test")
    
    @task
    def get_stats(self):
        self.client.get("/api/v1/memories/quotes/1/stats")
```

Run:
```bash
locust -f locustfile.py -u 100 -r 10 -t 5m
```

## Test Database Setup

### Local PostgreSQL

```bash
# Create test database
createdb waddlebot_test

# Run migrations
psql waddlebot_test < schema.sql

# Run tests
pytest
```

### Docker Test Database

```bash
# Start PostgreSQL
docker run -d \
  -e POSTGRES_DB=waddlebot_test \
  -e POSTGRES_USER=test \
  -e POSTGRES_PASSWORD=test \
  -p 5432:5432 \
  postgres:15

# Set DATABASE_URL
export DATABASE_URL="postgresql://test:test@localhost:5432/waddlebot_test"

# Run tests
pytest
```

## CI/CD Testing

Tests run in GitHub Actions:

```yaml
- name: Run Tests
  run: pytest --cov=. --cov-report=xml

- name: Upload Coverage
  uses: codecov/codecov-action@v3
```

## Test Files

Test files located in: `action/interactive/memories_interaction_module/` (see `test-api.sh`)

Structure:
```
tests/
├── __init__.py
├── conftest.py              # Shared fixtures
├── test_quote_service.py
├── test_bookmark_service.py
├── test_reminder_service.py
├── test_validation.py
├── test_api_quotes.py
├── test_api_bookmarks.py
└── test_api_reminders.py
```

---

Last Updated: February 16, 2026
Module Version: 2.0.0
