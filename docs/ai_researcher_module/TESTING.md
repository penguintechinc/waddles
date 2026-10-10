# AI Researcher Module — Testing Guide

## Testing Strategy

The AI Researcher Module uses a multi-tier testing approach:

1. **Unit Tests** — Individual service and component testing
2. **Integration Tests** — Service interactions with dependencies
3. **API Tests** — HTTP endpoint validation
4. **Mock Data** — Realistic test scenarios
5. **Performance Tests** — Latency and throughput validation

## Real-Ollama integration tests (env-gated)

`tests/test_ai_researcher_ollama_realpath.py` drives `AIProviderService`, `ResearchService` (+ the real
`SafetyLayer`) and `SummaryService` against a live Ollama - no mocked transport, one query at a time.
Set `WADDLE_TEST_OLLAMA_URL` to run them (unset = skipped). Offline contract/capability tests
(`test_ai_provider_capability.py`, `test_research_service_contract.py`,
`test_summary_service_capability.py`, `test_ai_provider_telemetry.py`) always run. Full instructions:
`docs/testing/ollama-realpath.md`.

## Test Setup

### Prerequisites

```bash
pip install pytest pytest-asyncio pytest-cov
pip install -r requirements.txt
```

### Test Configuration

Create `test_config.py` for test environment:

```python
import os

# Override settings for testing
os.environ['AI_PROVIDER'] = 'ollama'
os.environ['OLLAMA_HOST'] = 'localhost'
os.environ['OLLAMA_PORT'] = '11434'
os.environ['QDRANT_URL'] = 'http://localhost:6333'
os.environ['DATABASE_URL'] = 'postgresql://test:test@localhost/waddlebot_test'
os.environ['REDIS_URL'] = 'redis://localhost:6379/1'  # Use DB 1 for tests
os.environ['LOG_LEVEL'] = 'DEBUG'
```

### Test Database Setup

```bash
# Create test database
psql -U postgres -c "CREATE DATABASE waddlebot_test;"

# Run migrations
python scripts/migrate.py --db postgresql://test:test@localhost/waddlebot_test
```

## Mock Data & Fixtures

### Sample Research Queries

```python
# test_fixtures.py
import pytest

SAMPLE_QUERIES = [
    {
        "community_id": 123,
        "user_id": 456,
        "platform": "discord",
        "query": "What is semantic search?"
    },
    {
        "community_id": 123,
        "user_id": 456,
        "platform": "discord",
        "query": "Machine learning best practices"
    },
    {
        "community_id": 123,
        "user_id": 789,
        "platform": "twitch",
        "query": "Python performance optimization"
    }
]

SAMPLE_MESSAGES = [
    {
        "community_id": 123,
        "user_id": 456,
        "platform": "discord",
        "platform_user_id": "user123",
        "username": "john_dev",
        "message": "Just deployed the new feature!",
        "timestamp": "2026-02-16T14:30:00Z"
    },
    {
        "community_id": 123,
        "user_id": 789,
        "platform": "discord",
        "platform_user_id": "user789",
        "username": "jane_dev",
        "message": "Great work! Testing now",
        "timestamp": "2026-02-16T14:35:00Z"
    }
]

SAMPLE_MEMORIES = [
    {
        "text": "Q1 planning meeting scheduled for 2026-02-20",
        "metadata": {"type": "meeting", "attendees": 5}
    },
    {
        "text": "New API endpoint for user profiles released",
        "metadata": {"type": "release", "status": "production"}
    }
]

@pytest.fixture
def sample_research_queries():
    return SAMPLE_QUERIES

@pytest.fixture
def sample_messages():
    return SAMPLE_MESSAGES

@pytest.fixture
def sample_memories():
    return SAMPLE_MEMORIES
```

### Sample Mock AI Responses

```python
MOCK_RESPONSES = {
    "research": "Semantic search is a technique where search engines...",
    "ask": "Based on our earlier conversation, we discussed...",
    "summarize": "In the past hour, the main topics were..."
}

@pytest.fixture
def mock_ai_provider(mocker):
    """Mock AIProviderService for testing"""
    mock = mocker.AsyncMock()
    mock.generate.side_effect = lambda prompt: MOCK_RESPONSES.get("research", "")
    mock.count_tokens.return_value = 125
    return mock
```

## Unit Tests

### Test AIProviderService

```python
# tests/unit/test_ai_provider.py
import pytest
from services.ai_provider import AIProviderService
from config import Config

@pytest.mark.asyncio
async def test_ollama_initialization():
    """Test Ollama provider initialization"""
    provider = AIProviderService(Config)
    assert provider.provider_type == 'ollama'
    await provider.close()

@pytest.mark.asyncio
async def test_generate_with_ollama(mock_ai_provider):
    """Test LLM generation"""
    result = await mock_ai_provider.generate("Tell me about AI")
    assert result is not None
    assert len(result) > 0

@pytest.mark.asyncio
async def test_token_counting(mock_ai_provider):
    """Test token counting"""
    tokens = await mock_ai_provider.count_tokens("Hello world")
    assert isinstance(tokens, int)
    assert tokens > 0

@pytest.mark.asyncio
async def test_timeout_handling(mocker, mock_ai_provider):
    """Test request timeout"""
    mock_ai_provider.generate.side_effect = asyncio.TimeoutError()

    with pytest.raises(asyncio.TimeoutError):
        await mock_ai_provider.generate("test")
```

### Test RateLimiter

```python
# tests/unit/test_rate_limiter.py
import pytest
from services.rate_limiter import RateLimiter

@pytest.mark.asyncio
async def test_rate_limit_enforcement(redis_client, dal):
    """Test rate limiting"""
    limiter = RateLimiter(redis_client, dal)

    # Allow first request
    allowed, reason = await limiter.check_limit("user_123", "research")
    assert allowed is True
    await limiter.increment_counter("user_123", "research")

    # Simulate many requests
    for _ in range(30):
        await limiter.increment_counter("user_123", "research")

    # Next request should be blocked
    allowed, reason = await limiter.check_limit("user_123", "research")
    assert allowed is False
    assert reason == "rate_limit"

@pytest.mark.asyncio
async def test_redis_fallback(mocker, dal):
    """Test database fallback when Redis unavailable"""
    limiter = RateLimiter(None, dal)  # No Redis

    allowed, reason = await limiter.check_limit("user_456", "memory")
    assert allowed is True
```

### Test mem0Service

```python
# tests/unit/test_mem0_service.py
import pytest
from services.mem0_service import Mem0Service

@pytest.mark.asyncio
async def test_memory_add(sample_memories):
    """Test adding memories"""
    mem0 = Mem0Service(community_id=123)

    for memory in sample_memories:
        mem_id = await mem0.add(memory['text'], memory['metadata'])
        assert mem_id is not None
        assert isinstance(mem_id, str)

@pytest.mark.asyncio
async def test_memory_search(sample_memories):
    """Test semantic search"""
    mem0 = Mem0Service(community_id=123)

    # Add memories
    for memory in sample_memories:
        await mem0.add(memory['text'], memory['metadata'])

    # Search
    results = await mem0.search("meeting", limit=5)
    assert len(results) > 0
    assert any("meeting" in r['text'].lower() for r in results)

@pytest.mark.asyncio
async def test_memory_deduplication():
    """Test similar memory deduplication"""
    mem0 = Mem0Service(community_id=123)

    # Add similar memories
    await mem0.add("Python is a programming language")
    await mem0.add("Python is a great programming language")

    # Should deduplicate
    all_memories = await mem0.get_all()
    # Verify deduplication logic
```

## Integration Tests

### Test Research Service End-to-End

```python
# tests/integration/test_research_service.py
import pytest
from services.research_service import ResearchService

@pytest.mark.asyncio
async def test_research_workflow(mock_ai_provider, mock_mem0_service,
                                 mock_safety_layer, mock_rate_limiter,
                                 redis_client):
    """Test complete research workflow"""
    service = ResearchService(
        ai_provider=mock_ai_provider,
        mem0_service=mock_mem0_service,
        safety_layer=mock_safety_layer,
        rate_limiter=mock_rate_limiter,
        redis_client=redis_client
    )

    result = await service.research(
        community_id=123,
        user_id=456,
        topic="machine learning"
    )

    assert result.success is True
    assert len(result.content) > 0
    assert result.tokens_used > 0
    assert result.processing_time_ms > 0

@pytest.mark.asyncio
async def test_ask_with_context(mock_ai_provider, mock_mem0_service):
    """Test Q&A with community context"""
    service = ResearchService(
        ai_provider=mock_ai_provider,
        mem0_service=mock_mem0_service,
        safety_layer=mock_safety_layer,
        rate_limiter=mock_rate_limiter,
        redis_client=redis_client
    )

    # Add context
    await mock_mem0_service.add("Earlier we discussed Python decorators")

    result = await service.ask(
        community_id=123,
        user_id=456,
        question="What did we discuss about Python?"
    )

    assert result.success is True
    assert "python" in result.content.lower()

@pytest.mark.asyncio
async def test_rate_limit_blocking(mock_rate_limiter, redis_client):
    """Test that rate limiting blocks requests"""
    mock_rate_limiter.check_limit.return_value = (False, "rate_limit")

    service = ResearchService(
        ai_provider=mock_ai_provider,
        mem0_service=mock_mem0_service,
        safety_layer=mock_safety_layer,
        rate_limiter=mock_rate_limiter,
        redis_client=redis_client
    )

    result = await service.research(
        community_id=123,
        user_id=456,
        topic="test"
    )

    assert result.success is False
    assert result.blocked_reason == "rate_limit"
```

## API Tests

### Test HTTP Endpoints

```python
# tests/integration/test_api_endpoints.py
import pytest
from quart import Quart
from app import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
    return app.test_client()

@pytest.mark.asyncio
async def test_status_endpoint(client):
    """Test GET /api/v1/status"""
    response = await client.get('/api/v1/status')
    assert response.status_code == 200

    data = await response.json
    assert data['success'] is True
    assert data['module'] == 'ai_researcher_module'

@pytest.mark.asyncio
async def test_research_endpoint(client, sample_research_queries):
    """Test POST /api/v1/researcher/research"""
    query = sample_research_queries[0]

    response = await client.post(
        '/api/v1/researcher/research',
        json=query
    )

    assert response.status_code == 200
    data = await response.json
    assert data['success'] is True
    assert 'content' in data
    assert 'tokens_used' in data
    assert 'processing_time_ms' in data

@pytest.mark.asyncio
async def test_ask_endpoint(client, sample_research_queries):
    """Test POST /api/v1/researcher/ask"""
    query = sample_research_queries[0].copy()
    query['question'] = 'What did we discuss?'
    del query['query']

    response = await client.post(
        '/api/v1/researcher/ask',
        json=query
    )

    assert response.status_code == 200

@pytest.mark.asyncio
async def test_invalid_request(client):
    """Test error handling for invalid requests"""
    response = await client.post(
        '/api/v1/researcher/research',
        json={}  # Missing required fields
    )

    assert response.status_code == 400
    data = await response.json
    assert data['success'] is False
    assert 'error' in data

@pytest.mark.asyncio
async def test_firehose_endpoint(client, sample_messages):
    """Test POST /api/v1/researcher/messages/firehose"""
    response = await client.post(
        '/api/v1/researcher/messages/firehose',
        json={"messages": sample_messages},
        headers={'X-Service-Key': 'test-key'}
    )

    assert response.status_code == 200
    data = await response.json
    assert data['processed'] == len(sample_messages)

@pytest.mark.asyncio
async def test_unauthorized_firehose(client):
    """Test firehose without service key"""
    response = await client.post(
        '/api/v1/researcher/messages/firehose',
        json={"messages": []}
    )

    assert response.status_code == 401
```

## Performance Tests

### Latency Testing

```python
# tests/performance/test_latency.py
import pytest
import time

@pytest.mark.asyncio
async def test_research_latency(service):
    """Test research query latency"""
    start = time.time()

    result = await service.research(
        community_id=123,
        user_id=456,
        topic="test"
    )

    elapsed = time.time() - start
    assert elapsed < 5.0  # Should complete in < 5 seconds
    assert result.processing_time_ms < 5000

@pytest.mark.asyncio
async def test_memory_search_latency(mem0_service):
    """Test memory search latency"""
    # Add test memories
    for i in range(100):
        await mem0_service.add(f"Memory {i}")

    start = time.time()
    results = await mem0_service.search("Memory", limit=10)
    elapsed = time.time() - start

    assert elapsed < 1.0  # Should be < 1 second
    assert len(results) <= 10
```

### Throughput Testing

```python
# tests/performance/test_throughput.py
import asyncio
import pytest

@pytest.mark.asyncio
async def test_concurrent_requests(service):
    """Test concurrent research requests"""
    tasks = []

    for i in range(50):
        task = service.research(
            community_id=123,
            user_id=456 + i,
            topic=f"topic {i}"
        )
        tasks.append(task)

    results = await asyncio.gather(*tasks)

    # All should succeed
    assert all(r.success for r in results)

    # Check timing
    avg_time = sum(r.processing_time_ms for r in results) / len(results)
    assert avg_time < 2000  # Average < 2 seconds
```

## Running Tests

### Run All Tests

```bash
pytest tests/ -v
```

### Run Specific Test Category

```bash
# Unit tests
pytest tests/unit/ -v

# Integration tests
pytest tests/integration/ -v

# Performance tests
pytest tests/performance/ -v -s

# API tests
pytest tests/integration/test_api_endpoints.py -v
```

### Run with Coverage

```bash
pytest tests/ --cov=core/ai_researcher_module \
              --cov-report=html \
              --cov-report=term-missing
```

### Run with Markers

```bash
# Run only async tests
pytest -m asyncio

# Run only slow tests
pytest -m slow

# Skip slow tests
pytest -m "not slow"
```

## Mock Data for Testing

### Create Test Community

```sql
INSERT INTO communities (id, name, owner_id) VALUES
(123, 'Test Community', 1);

INSERT INTO ai_researcher_config (community_id, firehose_enabled, bot_detection_enabled) VALUES
(123, true, true);
```

### Create Test Users

```sql
INSERT INTO users (id, username, email) VALUES
(456, 'test_user_1', 'user1@test.com'),
(789, 'test_user_2', 'user2@test.com');
```

### Insert Test Messages

```sql
INSERT INTO ai_context_messages
(community_id, platform, platform_user_id, platform_username, message_content, created_at)
VALUES
(123, 'discord', 'user_123', 'john_dev', 'Hello everyone!', NOW()),
(123, 'discord', 'user_789', 'jane_dev', 'Hi there!', NOW());
```

## Test Execution Order

**Pre-commit test sequence** (2-3 minutes):

1. **Unit Tests** (30 seconds) — Fast, in-process
2. **API Tests** (60 seconds) — HTTP endpoint validation
3. **Integration Tests** (60 seconds) — Service interactions
4. **Smoke Tests** (30 seconds) — Basic health checks

## Debugging Failed Tests

### Verbose Output

```bash
pytest tests/ -vv  # Very verbose
pytest tests/ -s   # Show print statements
pytest tests/ --tb=long  # Full tracebacks
```

### Debug Specific Test

```bash
pytest tests/unit/test_ai_provider.py::test_ollama_initialization -vv
```

### Pause on Failure

```bash
pytest tests/ --pdb  # Drop to debugger on failure
```

### Generate Reports

```bash
pytest tests/ --html=report.html  # HTML report
pytest tests/ --junitxml=report.xml  # JUnit XML for CI/CD
```

## Common Test Patterns

### Mocking External Services

```python
@pytest.fixture
def mock_ollama(mocker):
    """Mock Ollama service"""
    mock = mocker.patch('services.ai_provider.httpx.AsyncClient')
    mock.return_value.post.return_value.json.return_value = {
        'response': 'Test response'
    }
    return mock

@pytest.fixture
def mock_qdrant(mocker):
    """Mock Qdrant service"""
    mock = mocker.patch('services.mem0_service.QdrantClient')
    mock.return_value.search.return_value = [
        {'id': 'mem_001', 'payload': {'text': 'Test'}}
    ]
    return mock
```

### Async Fixture Pattern

```python
@pytest.fixture
async def initialized_service():
    """Initialize service for testing"""
    service = ResearchService(...)
    await service.startup()
    yield service
    await service.shutdown()
```

### Parametrized Tests

```python
@pytest.mark.parametrize("query,expected_success", [
    ("machine learning", True),
    ("", False),  # Empty query
    ("x" * 10000, False),  # Too long
])
@pytest.mark.asyncio
async def test_research_queries(service, query, expected_success):
    result = await service.research(123, 456, query)
    assert result.success == expected_success
```
