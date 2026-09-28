# WebhookAdapter Integration Guide

This guide explains how to integrate the WebhookAdapter with WaddleBot's marketplace system.

## Overview

The `WebhookAdapter` enables external marketplace modules to be integrated into WaddleBot by bridging webhook-based endpoints with the internal module system.

## Architecture

```
WaddleBot Router
      |
      v
ExecuteRequest
      |
      v
WebhookAdapter
      |
      v-- HTTP POST with HMAC signature -->
                                          External Webhook
                                          (AWS Lambda, GCP, etc.)
      <-- JSON Response ------------------
      |
      v
ExecuteResponse
      |
      v
WaddleBot Response Handler
```

## Implementation Components

### 1. BaseAdapter (`base_adapter.py`)

Abstract base class providing:
- Health status tracking (`HealthStatus` dataclass)
- Async/sync execution methods
- Health monitoring utilities

**Key Features:**
- Tracks consecutive failures
- Calculates error rates
- Marks unhealthy after 3+ consecutive failures
- Auto-recovery on success

### 2. WebhookAdapter (`webhook_adapter.py`)

Concrete implementation for webhook-based modules:

**Core Functionality:**
- HTTP POST request handling via `httpx`
- HMAC-SHA256 signature generation
- Request/response transformation
- Timeout management (5s default, 30s max)
- Comprehensive error handling

**Security:**
- HMAC-SHA256 signatures in `X-WaddleBot-Signature` header
- Format: `sha256=<hex_digest>`
- Prevents request tampering

**Health Tracking:**
- Success/failure recording
- Error rate calculation
- Automatic health status updates

## Integration Steps

### Step 1: Module Registration

When a marketplace module is registered in WaddleBot:

```python
from module_sdk.adapters import WebhookAdapter

# Create adapter instance
adapter = WebhookAdapter(
    webhook_url=module_config['webhook_url'],
    secret_key=module_config['secret_key'],
    module_name=module_config['name'],
    timeout=module_config.get('timeout', 5.0),
    module_version=module_config.get('version', '1.0.0'),
    required_scopes=module_config.get('scopes', [])
)

# Register with module registry
module_registry.register(module_config['name'], adapter)
```

### Step 2: Request Routing

In the Router module (`processing/router_module`):

```python
async def route_marketplace_command(request: ExecuteRequest):
    """Route command to marketplace module via webhook adapter."""

    # Get the adapter for this module
    adapter = module_registry.get(module_name)

    # Validate scopes
    if not adapter.validate_scopes(request.scopes):
        return ExecuteResponse(
            success=False,
            error="Insufficient permissions"
        )

    # Check health before executing
    if not adapter.is_healthy():
        logger.warning(f"Module {module_name} is unhealthy")
        # Could fallback or return error

    # Execute the webhook
    response = await adapter.execute_async(request)

    return response
```

### Step 3: Response Handling

The adapter automatically converts webhook responses to `ExecuteResponse`:

```python
# Webhook returns:
{
    "success": true,
    "response_type": "text",
    "message": "Weather in London: 12°C",
    "targets": ["platform"]
}

# Adapter converts to ExecuteResponse:
ExecuteResponse(
    success=True,
    message="Weather in London: 12°C",
    data={
        "response_type": "text",
        "targets": ["platform"]
    },
    targets=[{"type": "platform"}]
)
```

## Metadata Requirements

The `ExecuteRequest` must include specific metadata for webhook payloads:

```python
request = ExecuteRequest(
    command="#weather",
    args=["London", "UK"],
    user_id="user_123",
    entity_id="entity_456",
    community_id="789",
    session_id="session_abc",
    platform="twitch",
    metadata={
        # Community information (required)
        "community": {
            "name": "StreamerCommunity",
            "is_subscribed": True,
            "subscription_order_id": "ord_123",
            "seat_count": 45
        },
        # User information (required)
        "user": {
            "username": "viewer123",
            "platform_user_id": "12345678"
        },
        # Entity information (required)
        "entity": {
            "platform_entity_id": "channel_id"
        },
        # For events (optional)
        "is_event": False,
        "event_type": None,
        "event_data": None
    },
    scopes=["community.read", "user.read"]
)
```

## Database Schema Integration

The marketplace modules table should include:

```sql
CREATE TABLE marketplace_modules (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    webhook_url VARCHAR(512) NOT NULL,
    secret_key VARCHAR(512) NOT NULL,  -- Encrypted
    timeout_seconds INTEGER DEFAULT 5,
    version VARCHAR(50),
    required_scopes JSONB,
    is_enabled BOOLEAN DEFAULT true,
    health_status JSONB,  -- Cached health info
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

Health status can be periodically updated:

```python
async def update_module_health(module_name: str, adapter: WebhookAdapter):
    """Update module health in database."""
    health = adapter.get_health_status()

    await db.execute(
        "UPDATE marketplace_modules SET health_status = $1, updated_at = NOW() "
        "WHERE name = $2",
        json.dumps(health),
        module_name
    )
```

## Health Monitoring

### Periodic Health Checks

```python
async def monitor_module_health():
    """Background task to monitor module health."""
    while True:
        for module_name, adapter in module_registry.items():
            health = adapter.get_health_status()

            if not health['is_healthy']:
                logger.warning(
                    f"Module {module_name} is unhealthy: "
                    f"{health['consecutive_failures']} consecutive failures, "
                    f"error rate: {health['error_rate']}"
                )

                # Send alert to admins
                await send_admin_alert(module_name, health)

        await asyncio.sleep(60)  # Check every minute
```

### Health API Endpoint

```python
@app.get("/api/marketplace/modules/{module_name}/health")
async def get_module_health(module_name: str):
    """Get health status of a marketplace module."""
    adapter = module_registry.get(module_name)

    if not adapter:
        raise HTTPException(404, "Module not found")

    return adapter.get_health_status()
```

## Error Handling

The adapter handles various error scenarios:

1. **Network Errors**: Connection failures, DNS errors
   - Returns `ExecuteResponse(success=False, error=...)`
   - Records failure in health tracker

2. **Timeout Errors**: Request exceeds timeout
   - Returns error response
   - Increases consecutive failure count

3. **HTTP Errors**: Non-200 status codes
   - Logs error with status code and response
   - Records failure

4. **JSON Errors**: Invalid response format
   - Logs parsing error
   - Returns error response

5. **Validation Errors**: Missing required fields
   - Handled during payload building

## Testing

### Unit Tests

Run the test suite:

```bash
cd /home/penguin/code/WaddleBot/libs/module_sdk/adapters
python -m pytest test_webhook_adapter.py -v
```

### Integration Tests

Test with a real webhook endpoint:

```python
# See example_usage.py
python -m module_sdk.adapters.example_usage
```

### Mock Webhook Server

For local testing, create a mock webhook server:

```python
from flask import Flask, request, jsonify
import hmac
import hashlib

app = Flask(__name__)
SECRET_KEY = "test-secret"

@app.route('/webhook', methods=['POST'])
def webhook():
    # Verify signature
    signature = request.headers.get('X-WaddleBot-Signature', '')
    payload = request.get_data(as_text=True)

    expected = hmac.new(
        SECRET_KEY.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    if not signature.endswith(expected):
        return jsonify({"success": False, "error": "Invalid signature"}), 401

    # Handle request
    data = request.json
    command = data['trigger']['command']

    return jsonify({
        "success": True,
        "response_type": "text",
        "message": f"Processed {command}",
        "targets": ["platform"]
    })

if __name__ == '__main__':
    app.run(port=8080)
```

## Performance Considerations

### Timeouts

- Default: 5 seconds (marketplace requirement)
- Maximum: 30 seconds
- Choose based on module complexity

### Connection Pooling

`httpx.AsyncClient` handles connection pooling automatically. For high-volume scenarios, consider using a persistent client:

```python
class WebhookAdapterPool:
    def __init__(self):
        self.client = httpx.AsyncClient()

    async def execute(self, adapter, request):
        # Use persistent client instead of creating new one
        response = await self.client.post(...)
        return response

    async def close(self):
        await self.client.aclose()
```

### Caching

Consider caching module health status to reduce database queries:

```python
from functools import lru_cache

@lru_cache(maxsize=100)
def get_cached_health(module_name: str) -> dict:
    adapter = module_registry.get(module_name)
    return adapter.get_health_status()
```

## Security Best Practices

1. **Secret Key Storage**: Store secret keys encrypted in database
2. **HTTPS Only**: Enforce HTTPS for webhook URLs in production
3. **Rate Limiting**: Implement rate limits per module
4. **Input Validation**: Validate webhook responses before processing
5. **Timeout Limits**: Enforce maximum timeout of 30s
6. **Audit Logging**: Log all webhook requests and responses

## Troubleshooting

### Module Always Showing Unhealthy

- Check webhook endpoint is responding with HTTP 200
- Verify JSON response format matches expected structure
- Check network connectivity to webhook URL
- Review error logs for specific failure reasons

### Signature Verification Failures

- Ensure secret key matches on both sides
- Verify payload is not modified between signing and sending
- Check character encoding (UTF-8)

### Timeout Issues

- Increase timeout if module legitimately needs more time
- Optimize webhook endpoint to respond faster
- Check for network latency issues

## Future Enhancements

Potential improvements for the adapter system:

1. **Retry Logic**: Automatic retries with exponential backoff
2. **Circuit Breaker**: Stop requests after repeated failures
3. **Metrics Collection**: Detailed performance metrics
4. **Response Caching**: Cache responses for identical requests
5. **Async Callbacks**: Support webhook callbacks for long-running tasks
6. **Multiple Endpoints**: Load balancing across multiple webhook URLs

## See Also

- [WebhookAdapter README](README.md)
- [BaseModule Documentation](../base/module.py)
- [Marketplace Module Guide](.PLAN-v2)
- [Security Best Practices](../security/README.md)
