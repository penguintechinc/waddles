# AI Interaction Module - Testing Guide

## Real-Ollama integration tests (env-gated)

`tests/test_ai_interaction_ollama_realpath.py` drives `OllamaProvider`, `WaddleAIProvider` (against
Ollama's OpenAI-compatible `/v1`) and `AIService` against a live Ollama - no mocked transport, one
query at a time. Set `WADDLE_TEST_OLLAMA_URL` to run them (unset = skipped). Offline tests always run.
Full instructions: `docs/testing/ollama-realpath.md`.

## Test Environment Setup

```bash
# Install test dependencies
pip install pytest pytest-asyncio pytest-cov httpx

# Set test environment variables
export AI_PROVIDER=ollama
export OLLAMA_HOST=localhost
export MODULE_PORT=8005
export DATABASE_URL=postgresql://waddlebot:password@localhost:5432/waddlebot_test
```

## Running Tests

### All Tests
```bash
pytest tests/
```

### Specific Test File
```bash
pytest tests/test_ai_service.py
```

### With Coverage
```bash
pytest --cov=. --cov-report=html tests/
```

## Unit Tests

### Test AI Service

```python
import pytest
from services.ai_service import AIService

@pytest.mark.asyncio
async def test_generate_response():
    service = AIService.create()
    response = await service.generate_response(
        message_content="Hello",
        message_type="chatMessage",
        user_id="test_user",
        platform="test",
        context={"trigger_type": "greeting"}
    )
    assert response is not None
    assert len(response) > 0

@pytest.mark.asyncio
async def test_health_check():
    service = AIService.create()
    health = await service.health_check()
    assert health is True
```

### Test Providers

```python
@pytest.mark.asyncio
async def test_ollama_provider():
    from services.ollama_provider import OllamaProvider
    provider = OllamaProvider(
        host="localhost",
        port="11434",
        model="llama3.2"
    )
    response = await provider.generate("Test message")
    assert response is not None

@pytest.mark.asyncio
async def test_waddleai_provider():
    from services.waddleai_provider import WaddleAIProvider
    provider = WaddleAIProvider(
        base_url="http://waddleai:8000",
        api_key="wa-test-key",
        model="auto"
    )
    response = await provider.generate("Test message")
    assert response is not None
```

## Integration Tests

### Test Interaction Endpoint

```python
import pytest
from httpx import AsyncClient
from app import app

@pytest.mark.asyncio
async def test_interaction_endpoint():
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post("/api/v1/ai/interaction", json={
            "session_id": "test_session",
            "message_type": "chatMessage",
            "message_content": "Hello!",
            "user_id": "test_user",
            "entity_id": "test:channel:123",
            "platform": "test",
            "username": "testuser"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
```

### Test Chat Completions

```python
@pytest.mark.asyncio
async def test_chat_completions():
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ai/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "messages": [{"role": "user", "content": "Test"}],
                "model": "auto"
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert "choices" in data["data"]
```

## Manual Testing

### Test Greetings

```bash
curl -X POST http://localhost:8005/api/v1/ai/interaction \
  -H "Content-Type: application/json" \
  -d '{"session_id":"s1","message_type":"chatMessage","message_content":"o7","user_id":"u1","entity_id":"e1","platform":"test","username":"tester"}'
```

### Test Questions

```bash
curl -X POST http://localhost:8005/api/v1/ai/interaction \
  -H "Content-Type: application/json" \
  -d '{"session_id":"s1","message_type":"chatMessage","message_content":"What time is it?","user_id":"u1","entity_id":"e1","platform":"test","username":"tester"}'
```

### Test Events

```bash
curl -X POST http://localhost:8005/api/v1/ai/interaction \
  -H "Content-Type: application/json" \
  -d '{"session_id":"s1","message_type":"subscription","message_content":"","user_id":"u1","entity_id":"e1","platform":"test","username":"subscriber"}'
```

## Load Testing

```python
import asyncio
import httpx

async def send_request(client, i):
    return await client.post("/api/v1/ai/interaction", json={
        "session_id": f"sess_{i}",
        "message_type": "chatMessage",
        "message_content": f"Test message {i}",
        "user_id": f"user_{i}",
        "entity_id": "test:123",
        "platform": "test",
        "username": f"user{i}"
    })

async def load_test(num_requests=100):
    async with httpx.AsyncClient(base_url="http://localhost:8005") as client:
        tasks = [send_request(client, i) for i in range(num_requests)]
        results = await asyncio.gather(*tasks)
        success_count = sum(1 for r in results if r.status_code == 200)
        print(f"Success: {success_count}/{num_requests}")

# Run: asyncio.run(load_test(100))
```

## Test Scenarios

### Scenario 1: New User Greeting
```
Input: "o7 first time here"
Expected: Friendly welcome message
```

### Scenario 2: Question
```
Input: "What game is this?"
Expected: Response about current game
```

### Scenario 3: Event Response
```
Input: Event type = "subscription"
Expected: Thank you message
```

### Scenario 4: No Trigger
```
Input: "just chatting"
Expected: No response (unless configured otherwise)
```

## Validation Tests

```python
def test_validation():
    # Missing required fields
    response = client.post("/api/v1/ai/interaction", json={})
    assert response.status_code == 400

    # Invalid message type
    response = client.post("/api/v1/ai/interaction", json={
        "session_id": "s1",
        "message_type": "invalid",
        "message_content": "test",
        "user_id": "u1",
        "entity_id": "e1",
        "platform": "test",
        "username": "test"
    })
    assert response.status_code == 400
```

## Performance Benchmarks

| Metric | Target | Actual |
|--------|--------|--------|
| Response Time (p50) | <500ms | TBD |
| Response Time (p95) | <2000ms | TBD |
| Concurrent Requests | 10 | TBD |
| Error Rate | <1% | TBD |
