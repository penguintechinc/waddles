# Video Proxy Module — Testing Guide

Complete guide for testing the video_proxy_module including mock video sources, test endpoints, and test procedures.

---

## Table of Contents

1. [Testing Overview](#testing-overview)
2. [Mock Video Sources](#mock-video-sources)
3. [Test Stream URLs](#test-stream-urls)
4. [Local Testing Setup](#local-testing-setup)
5. [Unit Tests](#unit-tests)
6. [Integration Tests](#integration-tests)
7. [Smoke Tests](#smoke-tests)
8. [Performance Testing](#performance-testing)
9. [Test Data Management](#test-data-management)
10. [CI/CD Testing](#cicd-testing)

---

## Testing Overview

**Test Categories**:
- **Unit Tests**: Individual functions and components (using pytest)
- **Integration Tests**: API endpoints with real database (pytest + fixtures)
- **Smoke Tests**: Critical path validation (build, run, health check)
- **Performance Tests**: Load testing with simulated multiple streams
- **E2E Tests**: Full workflow from stream creation to monitoring

**Test Tools**:
- `pytest` — Test framework
- `pytest-asyncio` — Async test support
- `curl` — HTTP testing
- `grpcurl` — gRPC testing
- `ffmpeg` — Video stream generation
- `docker-compose` — Test environment orchestration

---

## Mock Video Sources

### Option 1: FFmpeg Test Pattern (Recommended)

**Advantage**: Zero dependencies, instant startup, repeatable

**Generate test pattern**:
```bash
# Create test video file (5 seconds, pattern)
ffmpeg -f lavfi -i testsrc=size=1920x1080:duration=5 \
       -f lavfi -i sine=frequency=1000:duration=5 \
       -pix_fmt yuv420p \
       test_video.mp4

# Or infinite test pattern (for continuous streaming)
ffmpeg -f lavfi -i testsrc=size=1920x1080 \
       -f lavfi -i sine=frequency=1000 \
       -pix_fmt yuv420p \
       -c:v libx264 -preset ultrafast -b:v 2500k \
       -c:a aac -b:a 128k \
       -f flv rtmp://localhost:8092/live/test-stream-key
```

### Option 2: OBS Studio

**Advantage**: Realistic streaming conditions, adjustable settings

**Steps**:
1. Download OBS Studio (obsproject.com)
2. Settings > Stream > Custom RTMP
3. Server: `rtmp://localhost:8092/live`
4. Stream Key: (from test configuration)
5. Start Streaming

### Option 3: Docker-Based Test Stream

**Using testrtmp container**:

```bash
docker run -d \
  --name test-stream \
  -e RTMP_URL=rtmp://video-proxy:8092/live/test-key \
  testrtmp:latest
```

### Option 4: Hardware RTMP Camera/Encoder

**Example**: IP camera or hardware encoder configured with:
- RTMP Server: `rtmp://localhost:8092/live`
- Stream Key: (from test configuration)

---

## Test Stream URLs

### Ingest URL (from OBS/Encoder)

```
rtmp://[VIDEO_PROXY_HOST]:[VIDEO_PROXY_PORT]/live/[STREAM_KEY]
```

**Examples**:
```
# Local development
rtmp://localhost:8092/live/abc123def456

# Docker Compose
rtmp://video-proxy:8092/live/abc123def456

# Kubernetes
rtmp://video-proxy-service.default.svc.cluster.local:8092/live/abc123def456
```

### Destination URLs (Output Platforms)

**Twitch**:
```
rtmp://live.twitch.tv/app
[stream-key-from-twitch-dashboard]
```

**YouTube**:
```
rtmp://a.rtmp.youtube.com/live2
[stream-key-from-youtube-studio]
```

**Kick**:
```
rtmp://ingest.kick.com
[stream-key-from-kick-settings]
```

**Custom RTMP**:
```
rtmp://[your-rtmp-server]
[custom-stream-key]
```

---

## Local Testing Setup

### Step 1: Start Test Database

```bash
# PostgreSQL (Docker)
docker run -d \
  --name test-postgres \
  -e POSTGRES_USER=waddlebot \
  -e POSTGRES_PASSWORD=password \
  -e POSTGRES_DB=waddlebot_test \
  -p 5432:5432 \
  postgres:14

# Wait for startup
sleep 5
```

### Step 2: Start SeaweedFS (optional)

```bash
docker run -d \
  --name test-seaweedfs \
  -p 8333:8333 -p 9333:9333 \
  chrislusf/seaweedfs:4.48 server -dir=/data -s3 -s3.port=8333 -master.port=9333
```

No identity file means the S3 gateway runs in anonymous mode (fine for local
testing only) -- set `-s3.config=/path/to/s3-identities.json` for a scoped
access key/secret pair, matching `docker-compose.yml`'s `infra-seaweedfs`
service.

### Step 3: Configure Test Environment

```bash
# Copy test .env
cp .env.example .env.test

# Edit .env.test
cat > .env.test << 'EOF'
DATABASE_URL=postgresql://waddlebot:password@localhost:5432/waddlebot_test
MODULE_PORT=8092
MODULE_HOST=0.0.0.0
GRPC_PORT=50065
JWT_SECRET_KEY=test-secret-key
S3_ENDPOINT_URL=localhost:8333
S3_ACCESS_KEY_ID=waddlebot
S3_SECRET_ACCESS_KEY=waddlebot
LOG_LEVEL=DEBUG
RELEASE_MODE=false
EOF
```

### Step 4: Start Application

```bash
# Load test env and start
export $(cat .env.test | xargs)
python3 app.py
```

### Step 5: Verify Health Check

```bash
curl http://localhost:8092/health
```

Expected response:
```json
{
  "status": "healthy",
  "module": "video_proxy_module",
  "database": "connected"
}
```

---

## Unit Tests

### Test File Structure

```
tests/
├── __init__.py
├── conftest.py                      # pytest fixtures
├── test_config.py                   # Configuration tests
├── test_auth.py                     # JWT authentication
├── test_stream_config.py            # Stream config CRUD
├── test_destinations.py             # Destination management
└── test_status.py                   # Status monitoring
```

### Running Unit Tests

```bash
# All tests
pytest tests/

# Specific test file
pytest tests/test_auth.py

# Specific test function
pytest tests/test_auth.py::test_valid_jwt_token

# With verbose output
pytest -v tests/

# With coverage report
pytest --cov=. tests/
```

### Example Unit Test

**File**: `tests/test_config.py`

```python
import pytest
from app import app, db
from config import Config

class TestConfiguration:
    """Configuration validation tests."""

    def test_config_defaults(self):
        """Test default configuration values."""
        config = Config()
        assert config.MODULE_PORT == 8092
        assert config.GRPC_PORT == 50065
        assert config.FREE_MAX_DESTINATIONS == 3

    def test_config_env_override(self, monkeypatch):
        """Test environment variable override."""
        monkeypatch.setenv('MODULE_PORT', '9000')
        config = Config()
        assert config.MODULE_PORT == 9000

    def test_config_validation(self):
        """Test configuration validation."""
        config = Config()
        # Should not raise
        config.validate()

    def test_invalid_port(self, monkeypatch):
        """Test invalid port validation."""
        monkeypatch.setenv('MODULE_PORT', '99999')
        config = Config()
        with pytest.raises(ValueError, match="Invalid MODULE_PORT"):
            config.validate()
```

---

## Integration Tests

### Test Client Setup

**File**: `tests/conftest.py`

```python
import pytest
from app import app, db, init_database
from config import Config

@pytest.fixture
async def client():
    """Create test app client."""
    app.config['TESTING'] = True

    async with app.test_client() as test_client:
        # Initialize test database
        init_database()
        yield test_client

        # Cleanup
        db.rollback()

@pytest.fixture
def jwt_token():
    """Generate test JWT token."""
    from datetime import datetime, timedelta
    import jwt

    payload = {
        'sub': 'test_user',
        'exp': datetime.utcnow() + timedelta(hours=1)
    }
    return jwt.encode(
        payload,
        'jwt-secret-change-in-production',
        algorithm='HS256'
    )
```

### Integration Test Example

**File**: `tests/test_stream_config.py`

```python
@pytest.mark.asyncio
class TestStreamConfig:
    """Stream configuration endpoint tests."""

    async def test_create_stream_config(self, client, jwt_token):
        """Test creating a stream configuration."""
        response = await client.post(
            '/api/v1/stream/config',
            json={'community_id': 'test-community-1'},
            headers={'Authorization': f'Bearer {jwt_token}'}
        )

        assert response.status_code == 201
        data = await response.get_json()
        assert data['success'] is True
        assert data['config']['community_id'] == 'test-community-1'
        assert 'stream_key' in data['config']
        assert 'ingest_url' in data['config']

    async def test_create_duplicate_config(self, client, jwt_token):
        """Test duplicate community ID returns 409."""
        # Create first config
        await client.post(
            '/api/v1/stream/config',
            json={'community_id': 'duplicate'},
            headers={'Authorization': f'Bearer {jwt_token}'}
        )

        # Try to create duplicate
        response = await client.post(
            '/api/v1/stream/config',
            json={'community_id': 'duplicate'},
            headers={'Authorization': f'Bearer {jwt_token}'}
        )

        assert response.status_code == 409

    async def test_get_stream_config(self, client, jwt_token):
        """Test retrieving stream configuration."""
        # Create config
        create_response = await client.post(
            '/api/v1/stream/config',
            json={'community_id': 'retrieve-test'},
            headers={'Authorization': f'Bearer {jwt_token}'}
        )

        # Retrieve config
        response = await client.get(
            '/api/v1/stream/config/retrieve-test',
            headers={'Authorization': f'Bearer {jwt_token}'}
        )

        assert response.status_code == 200
        data = await response.get_json()
        assert data['config']['community_id'] == 'retrieve-test'
```

---

## Smoke Tests

**Objective**: Verify critical paths in < 2 minutes.

### Smoke Test Script

**File**: `tests/smoke.sh`

```bash
#!/bin/bash
set -e

echo "=== Video Proxy Module Smoke Tests ==="

# Test 1: Health Check
echo "Test 1: Health Check..."
HEALTH=$(curl -s http://localhost:8092/health)
if echo "$HEALTH" | grep -q '"status":"healthy"'; then
    echo "✓ Health check passed"
else
    echo "✗ Health check failed"
    exit 1
fi

# Test 2: Database Connectivity
echo "Test 2: Database Connectivity..."
python3 -c "from app import db; db.executesql('SELECT 1'); print('✓ Database connected')" || exit 1

# Test 3: Create Stream Configuration
echo "Test 3: Create Stream Configuration..."
TOKEN=$(python3 -c "import jwt; from datetime import datetime, timedelta; print(jwt.encode({'sub': 'test'}, 'jwt-secret-change-in-production', algorithm='HS256'))")

RESPONSE=$(curl -s -X POST http://localhost:8092/api/v1/stream/config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"community_id": "smoke-test-'$(date +%s)'"}')

if echo "$RESPONSE" | grep -q '"success":true'; then
    echo "✓ Stream creation passed"
else
    echo "✗ Stream creation failed"
    echo "$RESPONSE"
    exit 1
fi

# Test 4: gRPC Connectivity
echo "Test 4: gRPC Connectivity..."
grpcurl -plaintext localhost:50065 list > /dev/null 2>&1 && echo "✓ gRPC service available" || echo "⚠ gRPC not available (expected in some envs)"

echo ""
echo "=== All Smoke Tests Passed ==="
```

### Run Smoke Tests

```bash
# Make executable
chmod +x tests/smoke.sh

# Run
bash tests/smoke.sh
```

---

## Performance Testing

### Load Test Script

**File**: `tests/load_test.py`

```python
import asyncio
import aiohttp
import time
from statistics import mean, stdev

async def test_concurrent_config_creation(num_requests=100, concurrency=10):
    """Load test: concurrent stream config creation."""

    token = generate_test_token()
    url = 'http://localhost:8092/api/v1/stream/config'

    results = {
        'success': 0,
        'error': 0,
        'response_times': []
    }

    async def create_config(session, community_id):
        start = time.time()
        try:
            async with session.post(
                url,
                json={'community_id': f'load-test-{community_id}'},
                headers={'Authorization': f'Bearer {token}'}
            ) as resp:
                if resp.status == 201:
                    results['success'] += 1
                else:
                    results['error'] += 1
                elapsed = time.time() - start
                results['response_times'].append(elapsed)
        except Exception as e:
            results['error'] += 1

    async with aiohttp.ClientSession() as session:
        tasks = []
        for i in range(num_requests):
            tasks.append(create_config(session, i))
            if len(tasks) >= concurrency:
                await asyncio.gather(*tasks)
                tasks = []
        if tasks:
            await asyncio.gather(*tasks)

    # Print results
    avg_time = mean(results['response_times'])
    print(f"Requests: {num_requests}")
    print(f"Success: {results['success']}")
    print(f"Errors: {results['error']}")
    print(f"Avg Response Time: {avg_time:.3f}s")
    if len(results['response_times']) > 1:
        print(f"Std Dev: {stdev(results['response_times']):.3f}s")

# Run
if __name__ == '__main__':
    asyncio.run(test_concurrent_config_creation(num_requests=100, concurrency=10))
```

### Run Performance Tests

```bash
python3 tests/load_test.py
```

---

## Test Data Management

### Seed Mock Data

**File**: `tests/seed_test_data.py`

```python
from app import db, init_database
from datetime import datetime

def seed_test_data():
    """Populate test database with sample data."""
    init_database()

    # Create test stream configs
    for i in range(3):
        db.stream_configs.insert(
            community_id=f'test-community-{i}',
            stream_key=f'test-key-{i}' * 4,
            ingest_url=f'rtmp://localhost:8092/live/test-key-{i}',
            is_active=True
        )

    # Create test destinations
    for config_id in range(1, 4):
        db.stream_destinations.insert(
            config_id=config_id,
            platform='twitch',
            rtmp_url='rtmp://live.twitch.tv/app',
            stream_key='twitch-key-test',
            is_active=True,
            max_resolution='1080p'
        )

        db.stream_destinations.insert(
            config_id=config_id,
            platform='youtube',
            rtmp_url='rtmp://a.rtmp.youtube.com/live2',
            stream_key='youtube-key-test',
            is_active=True,
            max_resolution='1080p'
        )

    db.commit()
    print("Test data seeded successfully")

if __name__ == '__main__':
    seed_test_data()
```

### Run Seed

```bash
python3 tests/seed_test_data.py
```

---

## CI/CD Testing

### GitHub Actions Test Workflow

**File**: `.github/workflows/test-video-proxy.yml`

```yaml
name: Video Proxy Tests

on:
  push:
    branches: [main, develop]
    paths:
      - 'core/video_proxy_module/**'
  pull_request:
    paths:
      - 'core/video_proxy_module/**'

jobs:
  test:
    runs-on: ubuntu-latest

    services:
      postgres:
        image: postgres:14
        env:
          POSTGRES_USER: waddlebot
          POSTGRES_PASSWORD: password
          POSTGRES_DB: waddlebot_test
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 5432:5432

    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.13'

      - name: Install dependencies
        working-directory: core/video_proxy_module
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-asyncio pytest-cov

      - name: Run unit tests
        working-directory: core/video_proxy_module
        env:
          DATABASE_URL: postgresql://waddlebot:password@localhost:5432/waddlebot_test
        run: |
          pytest tests/ --cov=. --cov-report=xml

      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          files: ./core/video_proxy_module/coverage.xml
```

---

## Test Checklist

Before Committing:

- [ ] Unit tests pass: `pytest tests/`
- [ ] Smoke tests pass: `bash tests/smoke.sh`
- [ ] Code coverage > 80%: `pytest --cov=.`
- [ ] No linting errors: `flake8 app.py config.py`
- [ ] Configuration validates: `python3 -c "from config import Config; Config().validate()"`
- [ ] Health check responds: `curl http://localhost:8092/health`
- [ ] Mock streams created successfully
- [ ] Destinations added without error
- [ ] Stream status updated in real-time

---

**Last Updated**: 2026-02-16
**Repository**: github.com/penguintechinc/waddlebot
