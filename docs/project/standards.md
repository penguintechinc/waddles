# Waddles Microservices Standards and Patterns

## Overview

This document defines architectural patterns, code standards, and best practices for developing Waddles microservices. All module implementations must follow these standards to ensure consistency, reliability, and maintainability across the 24+ service architecture.

---

## Architecture Principles

### 1. Service Isolation

Each microservice is:
- **Independently deployable**: Docker containers for autonomous scaling
- **Loosely coupled**: Communication via REST APIs and Redis messaging
- **Autonomously managed**: Own database schemas (when applicable)
- **Fault tolerant**: Graceful degradation when dependencies fail

### 2. Communication Patterns

#### Synchronous Communication (REST/HTTP)
- Module-to-module direct API calls
- Router module orchestrates request flow
- Timeouts enforced at caller level
- Circuit breaker pattern for resilience

#### Asynchronous Communication (Redis)
- Event publishing via Redis pubsub
- Message queuing for background processing
- Decouples rapid request/response cycles
- Enables fan-out notifications

#### Service Discovery
- Router module maintains service registry
- Environment variables specify service endpoints
- Health checks via `/health` endpoints
- Automatic restart on failure

### 3. Data Management

#### Database Strategy
```
┌─ Module-Level Schemas
│  ├─ Each module owns its data tables
│  ├─ Separate locks for concurrency
│  └─ Module-specific indices for performance
│
├─ Shared Data (PostgreSQL)
│  ├─ servers: Platform channel/server configuration
│  ├─ users: Cross-platform user identity
│  ├─ roles: RBAC definition
│  └─ audit_log: Security auditing
│
└─ Transient Data (Redis)
   ├─ Session tokens
   ├─ Cache entries (TTL-based)
   ├─ Rate limit counters
   └─ Real-time state
```

#### Caching Strategy
- **Layer 1**: In-memory module cache (60s TTL)
- **Layer 2**: Redis distributed cache (300s TTL)
- **Layer 3**: PostgreSQL with read replicas
- Invalidate on writes, revalidate on reads

### 4. Module Lifecycle

```
Service State Machine:
STARTING → INITIALIZING → READY → RUNNING → DRAINING → STOPPED
   ↓          ↓             ↓        ↓         ↓         ↓
init()    health()      register  process  shutdown   cleanup()
```

**State Handlers**:

```python
class ModuleBase:
    async def on_startup(self):
        """Initialize module resources"""
        await self.db.connect()
        await self.register_with_router()

    async def on_ready(self):
        """Signal readiness for traffic"""
        self.ready = True

    async def on_request(self, request):
        """Process incoming request"""
        return await self.handle_request(request)

    async def on_shutdown(self):
        """Graceful shutdown"""
        await self.drain_in_flight()
        await self.db.disconnect()
```

---

## Code Structure Standards

### Python Module Layout

```
{module_name}/
├── Dockerfile                 # Container definition
├── requirements.txt          # Python dependencies
├── pyproject.toml           # Python project config (if using poetry)
├── main.py                  # Entry point (Quart application)
├── config.py                # Configuration from environment
├── models.py                # Dataclasses and ORM models
├── handlers/                # Request handlers by feature
│   ├── __init__.py
│   ├── auth.py
│   ├── commands.py
│   └── webhooks.py
├── services/                # Business logic
│   ├── __init__.py
│   ├── {feature}_service.py
│   └── external_api.py
├── utils/                   # Utility functions
│   ├── __init__.py
│   ├── logging.py
│   ├── validation.py
│   └── async_helpers.py
├── tests/                   # Unit and integration tests
│   ├── test_handlers.py
│   ├── test_services.py
│   └── conftest.py
└── README.md               # Module documentation
```

### Shared Flask Core Library

All modules import from `libs/flask_core`:

```python
from flask_core.auth import require_api_key, api_key_auth
from flask_core.datamodels import ModuleConfig, UserIdentity
from flask_core.database import AsyncDAL
from flask_core.logging import get_logger, log_audit_event
from flask_core.api import create_response, error_response
from flask_core.validation import validate_input, validate_discord_payload
```

---

## API Standards

### Endpoint Design

#### Module Registration
```
POST /api/v1/register
Authorization: X-API-Key: {router-key}
{
  "module_id": "ai-interaction",
  "module_version": "0.2.0",
  "endpoints": ["/api/v1/ai/chat"],
  "health_check": "/health",
  "port": 8005
}
```

#### Health Check
```
GET /health
Response (200):
{
  "status": "healthy",
  "module": "ai-interaction",
  "version": "0.2.0",
  "uptime_seconds": 3600,
  "dependencies": {
    "database": "connected",
    "redis": "connected"
  }
}
```

#### Action Endpoints
```
POST /api/v1/action/{action_id}
Authorization: X-API-Key: {api-key}
X-Community-ID: {community_id}
{
  "user_id": "discord:123456",
  "context": {
    "platform": "discord",
    "channel": "#general"
  },
  "params": { ... }
}

Response (200):
{
  "success": true,
  "action_id": "action_xyz",
  "result": { ... },
  "execution_time_ms": 145
}
```

### Error Responses

```json
{
  "error": "invalid_input",
  "message": "User ID must be in format 'platform:user_id'",
  "status": 400,
  "correlation_id": "req_abc123",
  "timestamp": "2025-12-11T10:30:00Z"
}
```

**Status Codes**:
- `200` OK - Request successful
- `400` Bad Request - Invalid input
- `401` Unauthorized - Missing/invalid API key
- `403` Forbidden - Insufficient permissions
- `404` Not Found - Resource doesn't exist
- `429` Too Many Requests - Rate limited
- `500` Internal Server Error - Unexpected failure
- `503` Service Unavailable - Dependency failure

---

## Security Standards

### Authentication & Authorization

#### API Key Management
```python
# Header: X-API-Key: {key}
# Roles: trigger, action, core, admin, user

def require_api_key(role='action'):
    def decorator(f):
        @wraps(f)
        async def wrapper(*args, **kwargs):
            key = request.headers.get('X-API-Key')
            if not key or not validate_key(key, role):
                return error_response(401, 'unauthorized')
            request.api_key = key
            return await f(*args, **kwargs)
        return wrapper
    return decorator

@app.route('/api/v1/admin/users', methods=['GET'])
@require_api_key(role='admin')
async def list_users():
    return {'users': [...]}
```

#### Community Isolation
```python
# Every request must include community context
X-Community-ID: {id}

# All queries filtered by community:
users = await db.query(
    User,
    User.community_id == request.community_id
)
```

### Input Validation

```python
from flask_core.validation import validate_input, ValidationError

@app.route('/api/v1/command', methods=['POST'])
async def execute_command():
    try:
        data = await validate_input(
            await request.json(),
            {
                'user_id': {'type': 'string', 'required': True, 'pattern': r'^[a-z]+:\d+$'},
                'command': {'type': 'string', 'required': True, 'max_length': 255},
                'args': {'type': 'array', 'items': {'type': 'string'}},
            }
        )
    except ValidationError as e:
        return error_response(400, str(e))

    return await process_command(data)
```

### Webhook Signature Verification

```python
import hmac
import hashlib

def verify_webhook(payload_bytes, signature, secret):
    """Verify HMAC-SHA256 webhook signature"""
    expected = hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)

@app.route('/webhook/twitch', methods=['POST'])
async def twitch_webhook():
    signature = request.headers.get('Twitch-Eventsub-Message-Signature')
    payload = await request.get_data()

    if not verify_webhook(payload, signature, TWITCH_SECRET):
        return error_response(401, 'invalid_signature')

    return await process_twitch_event(payload)
```

### Secrets Management

**NEVER hardcode credentials!**

```python
# ✓ CORRECT - Use environment variables
DB_PASSWORD = os.getenv('DB_PASSWORD')
TWITCH_SECRET = os.getenv('TWITCH_SECRET')

# ✗ WRONG - Hardcoded credentials
DB_PASSWORD = 'postgres123'
TWITCH_SECRET = 'abc123def456'

# ✓ CORRECT - Use Kubernetes secrets
# spec:
#   containers:
#   - env:
#     - name: DB_PASSWORD
#       valueFrom:
#         secretKeyRef:
#           name: db-credentials
#           key: password
```

---

## Logging Standards

### AAA Logging (Authentication, Authorization, Auditing)

All modules must implement comprehensive logging:

```
[timestamp] LEVEL module:version EVENT_TYPE community=X user=Y action=Z result=STATUS
```

#### Log Categories

**AUTH**: Authentication events
```
[2025-12-11T10:30:00Z] INFO ai_interaction:0.2.0 AUTH community=1001 user=discord:789 action=login result=success
```

**AUTHZ**: Authorization events
```
[2025-12-11T10:30:05Z] INFO ai_interaction:0.2.0 AUTHZ community=1001 user=discord:789 action=execute_admin_command result=denied reason=insufficient_role
```

**AUDIT**: System actions
```
[2025-12-11T10:30:10Z] INFO ai_interaction:0.2.0 AUDIT community=1001 user=admin:123 action=modify_settings module=ai result=success
```

**ERROR**: Failures and exceptions
```
[2025-12-11T10:30:15Z] ERROR ai_interaction:0.2.0 ERROR community=1001 action=process_request result=failure error=timeout_exceeded duration_ms=30000
```

**SYSTEM**: Operational events
```
[2025-12-11T10:30:20Z] INFO ai_interaction:0.2.0 SYSTEM action=startup status=success dependencies_ready=true
```

### Implementation

```python
import logging
import sys
from datetime import datetime

logger = logging.getLogger(__name__)

# Console logging
console = logging.StreamHandler(sys.stdout)
console.setFormatter(logging.Formatter(
    '[%(asctime)s] %(levelname)s %(name)s EVENT_TYPE community=%(community)s '
    'user=%(user)s action=%(action)s result=%(result)s'
))
logger.addHandler(console)

# File logging with rotation
file_handler = logging.handlers.RotatingFileHandler(
    '/var/log/waddlebotlog/ai_interaction.log',
    maxBytes=10_000_000,  # 10 MB
    backupCount=5
)
file_handler.setFormatter(logging.Formatter(
    '[%(asctime)s] %(levelname)s %(name)s %(message)s'
))
logger.addHandler(file_handler)

# Usage
def log_auth_event(community_id, user_id, action, result, **kwargs):
    logger.info(
        f'AUTH community={community_id} user={user_id} action={action} result={result}',
        extra={
            'community': community_id,
            'user': user_id,
            'action': action,
            'result': result,
        },
        **kwargs
    )

def log_error(community_id, action, error, **kwargs):
    logger.error(
        f'ERROR action={action} error={error}',
        extra={
            'community': community_id,
            'action': action,
            'result': 'failure',
        },
        **kwargs
    )
```

---

## Testing Standards

### Unit Tests

```python
# tests/test_handlers.py
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_ai_chat_endpoint_success():
    """Test successful chat endpoint"""
    client = create_test_client()

    response = await client.post(
        '/api/v1/ai/chat',
        json={
            'user_id': 'discord:123',
            'message': 'Hello AI',
            'context': {'platform': 'discord'}
        },
        headers={'X-API-Key': 'test-key'}
    )

    assert response.status_code == 200
    data = await response.json()
    assert 'response' in data
    assert 'execution_time_ms' in data

@pytest.mark.asyncio
async def test_ai_chat_invalid_input():
    """Test validation of invalid input"""
    client = create_test_client()

    response = await client.post(
        '/api/v1/ai/chat',
        json={'message': 'Missing required fields'},
        headers={'X-API-Key': 'test-key'}
    )

    assert response.status_code == 400
    data = await response.json()
    assert 'error' in data
```

### Integration Tests

```python
@pytest.mark.asyncio
async def test_ai_chat_with_database():
    """Test chat with actual database connection"""
    async with create_test_db() as db:
        # Create test community
        community = await db.create(Community, name='test')
        user = await db.create(User, community_id=community.id, name='testuser')

        response = await client.post(
            '/api/v1/ai/chat',
            json={
                'user_id': f'discord:{user.id}',
                'message': 'Hello AI',
                'context': {'platform': 'discord'}
            }
        )

        assert response.status_code == 200
        # Verify response stored in database
        message = await db.get(Message, id=response.json()['message_id'])
        assert message.content == 'Hello AI'
```

### Mocking External APIs

```python
@pytest.mark.asyncio
async def test_twitch_api_failure_handling():
    """Test graceful degradation when Twitch API fails"""
    with patch('handlers.twitch.TwitchAPI.get_user') as mock_api:
        mock_api.side_effect = TwitchAPIError('Service unavailable')

        response = await client.get('/api/v1/user/twitch:123')

        # Should return fallback data without crashing
        assert response.status_code == 200
        data = await response.json()
        assert data['cached'] == True  # Using cached data
```

---

## Performance Standards

### Concurrency Patterns

```python
from concurrent.futures import ThreadPoolExecutor
import asyncio

# Thread pool for blocking I/O
THREAD_POOL = ThreadPoolExecutor(max_workers=20)

# Thread-based operations
async def blocking_operation(data):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(THREAD_POOL, process_sync, data)
    return result

# Async/await for I/O
async def fetch_from_api(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=5) as resp:
            return await resp.json()

# Batch operations
async def process_bulk(items):
    tasks = [process_item(item) for item in items]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results
```

### Database Query Optimization

```python
# ✓ GOOD - Single query with join
users_with_stats = await db.select(
    User,
    User.id == Reputation.user_id,
    select=[User, Reputation.points]
)

# ✗ BAD - N+1 query problem
users = await db.select(User)
for user in users:
    reputation = await db.get(Reputation, user_id=user.id)  # SLOW!

# ✓ GOOD - Bulk query
user_ids = [u.id for u in users]
reputations = await db.select(
    Reputation,
    Reputation.user_id.belongs(user_ids)
)
```

### Caching Strategy

```python
CACHE_TTL = 300  # 5 minutes

async def get_user_with_cache(user_id: str):
    # Check cache
    cache_key = f"user:{user_id}"
    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    # Fetch from database
    user = await db.get(User, id=user_id)

    # Store in cache
    await redis_client.setex(
        cache_key,
        CACHE_TTL,
        json.dumps(user.to_dict())
    )

    return user
```

### Rate Limiting

```python
from flask_core.ratelimit import rate_limit

@app.route('/api/v1/ai/chat', methods=['POST'])
@rate_limit(max_requests=100, window=60)  # 100 requests per minute
async def ai_chat():
    return await process_chat()
```

---

## Deployment Standards

### Docker Standards

**Dockerfile Template**:
```dockerfile
FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8005/health')"

# Non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Run application
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005"]
```

**Build Command**:
```bash
docker build \
  --build-arg MODULE_NAME=ai-interaction \
  --build-arg MODULE_PORT=8005 \
  -t ghcr.io/penguintechinc/waddlebot/ai-interaction:v0.2.0 \
  .
```

### Environment Variables

Every module must support:
```bash
# Core configuration
MODULE_NAME=ai-interaction
MODULE_VERSION=0.2.0
MODULE_PORT=8005

# Database
DB_HOST=postgres.default.svc.cluster.local
DB_PORT=5432
DB_NAME=waddlebot
DB_USER=waddlebot
DB_PASSWORD=<from-secret>

# Redis
REDIS_HOST=redis.default.svc.cluster.local
REDIS_PORT=6379

# Router
ROUTER_URL=http://router:8000
ROUTER_API_KEY=<from-secret>

# Security
API_KEY_SECRET=<from-secret>
# (no JWT secret: platform JWTs are ES256, keys live in platform_vault)

# Features
ENABLE_AI=true
ENABLE_CACHE=true
LOG_LEVEL=INFO
```

### Kubernetes Deployment

**Pod Specification**:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-interaction
  labels:
    app: waddlebot
    module: ai-interaction
spec:
  replicas: 3
  selector:
    matchLabels:
      app: ai-interaction
  template:
    metadata:
      labels:
        app: ai-interaction
    spec:
      containers:
      - name: ai-interaction
        image: ghcr.io/penguintechinc/waddlebot/ai-interaction:latest
        ports:
        - containerPort: 8005
        env:
        - name: MODULE_PORT
          value: "8005"
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef:
              name: db-credentials
              key: password

        # Health checks
        livenessProbe:
          httpGet:
            path: /health
            port: 8005
          initialDelaySeconds: 15
          periodSeconds: 30

        readinessProbe:
          httpGet:
            path: /health
            port: 8005
          initialDelaySeconds: 5
          periodSeconds: 10

        # Resource limits
        resources:
          requests:
            memory: "256Mi"
            cpu: "100m"
          limits:
            memory: "512Mi"
            cpu: "500m"

        # Security context
        securityContext:
          readOnlyRootFilesystem: true
          runAsNonRoot: true
          runAsUser: 1000
          capabilities:
            drop:
            - ALL
```

---

## Module Checklist

Before deploying any module, verify:

- [ ] **Code Quality**
  - [ ] Passes black, flake8, mypy
  - [ ] All functions have docstrings
  - [ ] No hardcoded credentials or secrets
  - [ ] Comprehensive error handling

- [ ] **Testing**
  - [ ] Unit tests written (>80% coverage)
  - [ ] Integration tests pass
  - [ ] Edge cases tested
  - [ ] Performance benchmarked

- [ ] **Security**
  - [ ] API authentication required
  - [ ] Input validation on all endpoints
  - [ ] Webhook signatures verified
  - [ ] SQL injection prevention (parameterized queries)
  - [ ] XSS protection (if frontend)

- [ ] **Logging**
  - [ ] AAA logging implemented
  - [ ] No sensitive data in logs
  - [ ] Structured logging format
  - [ ] Log rotation configured

- [ ] **Documentation**
  - [ ] README.md with setup instructions
  - [ ] API endpoint documentation
  - [ ] Environment variables documented
  - [ ] Configuration examples provided

- [ ] **Deployment**
  - [ ] Dockerfile present and tested
  - [ ] Health check endpoint working
  - [ ] All dependencies in requirements.txt
  - [ ] Kubernetes YAML provided
  - [ ] Environment variables externalized

- [ ] **Monitoring**
  - [ ] Metrics exposed (Prometheus format)
  - [ ] Logging configured
  - [ ] Alerts configured
  - [ ] SLO defined

---

## Related Documentation

- **WORKFLOWS.md**: CI/CD automation and version management
- **CLAUDE.md**: Project context and development guidelines
- **docs/development-rules.md**: Detailed development standards
- **docs/api-reference.md**: Complete API documentation
- **docs/module-details-core.md**: Core module implementations

---

**Last Updated**: 2025-12-11
**Waddles Version**: 0.2.0
**Total Modules**: 24+
