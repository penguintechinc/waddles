# WaddleBot Module SDK - Adapters

This package provides adapter classes for integrating external modules with WaddleBot's module system.

## Overview

Adapters bridge external modules (webhook endpoints, serverless functions, etc.) with WaddleBot's internal module system. They handle:

- Communication protocol conversion
- Security (HMAC signatures)
- Error handling and retries
- Health status tracking
- Request/response transformation

## Available Adapters

### WebhookAdapter

The `WebhookAdapter` class enables integration with external webhook-based modules (AWS Lambda, Google Cloud Run, OpenWhisk, or any HTTP endpoint).

**Features:**
- Sends HTTP POST requests to configured webhook URLs
- Includes HMAC-SHA256 signatures in `X-WaddleBot-Signature` header
- Configurable timeouts (default 5s, max 30s)
- Automatic health tracking based on success/failure rates
- Converts between `ExecuteRequest`/`ExecuteResponse` and webhook payloads

## Usage

### Basic Example

```python
from module_sdk.adapters import WebhookAdapter
from module_sdk import ExecuteRequest

# Create a webhook adapter
adapter = WebhookAdapter(
    webhook_url="https://your-module.com/webhook",
    secret_key="your-secret-key",
    module_name="my_weather_module",
    timeout=5.0,
    module_version="1.0.0",
    required_scopes=["community.read"]
)

# Create a request
request = ExecuteRequest(
    command="#weather",
    args=["London", "UK"],
    user_id="user_123",
    entity_id="entity_456",
    community_id="789",
    session_id="session_abc",
    platform="twitch",
    metadata={
        "community": {
            "name": "AwesomeStreamers",
            "is_subscribed": True,
            "subscription_order_id": "ord_abc123",
            "seat_count": 45
        }
    }
)

# Execute the webhook
response = await adapter.execute_async(request)

print(response.success)  # True/False
print(response.message)  # Response message
```

### Health Monitoring

```python
# Check if adapter is healthy
if adapter.is_healthy():
    print("Module is healthy")

# Get detailed health status
health = adapter.get_health_status()
print(f"Error rate: {health['error_rate']}")
print(f"Consecutive failures: {health['consecutive_failures']}")
```

## Webhook Payload Format

### Request Payload (sent TO webhook)

```json
{
  "community": {
    "id": 123,
    "name": "AwesomeStreamers",
    "is_subscribed": true,
    "subscription_order_id": "ord_abc123",
    "seat_count": 45
  },
  "trigger": {
    "type": "command",
    "command": "#weather",
    "context_text": "London UK",
    "event_type": null,
    "event_data": null
  },
  "user": {
    "id": "user_456",
    "username": "CoolViewer",
    "platform": "twitch",
    "platform_user_id": "12345678"
  },
  "entity": {
    "id": "entity_789",
    "platform": "twitch",
    "platform_entity_id": "channel123"
  },
  "request_id": "req_xyz789",
  "timestamp": "2025-12-15T10:30:00Z"
}
```

### Response Payload (received FROM webhook)

```json
{
  "success": true,
  "response_type": "text",
  "message": "Weather in London: 12°C, Cloudy",
  "overlay_data": null,
  "browser_source_url": null,
  "targets": ["platform"]
}
```

## Security

### HMAC Signature Verification

The adapter automatically generates HMAC-SHA256 signatures for all requests. The signature is included in the `X-WaddleBot-Signature` header as `sha256=<hex_signature>`.

**Webhook endpoints should verify signatures:**

```python
import hmac
import hashlib

def verify_signature(payload: str, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    # Remove 'sha256=' prefix if present
    signature = signature.replace('sha256=', '')

    return hmac.compare_digest(expected, signature)
```

## Health Status

The adapter tracks health status automatically:

- **Healthy**: Recent requests are succeeding
- **Unhealthy**: 3+ consecutive failures

**Health Metrics:**
- `is_healthy`: Current health status (bool)
- `last_success`: Timestamp of last successful request
- `last_failure`: Timestamp of last failed request
- `consecutive_failures`: Number of consecutive failures
- `total_requests`: Total requests made
- `total_failures`: Total failed requests
- `error_rate`: Current error rate (0.0 to 1.0)

## Timeout Configuration

Timeouts are configurable per adapter instance:

```python
adapter = WebhookAdapter(
    webhook_url="https://slow-endpoint.com/webhook",
    secret_key="secret",
    module_name="slow_module",
    timeout=15.0  # 15 second timeout (max 30s)
)
```

**Timeout Guidelines:**
- Default: 5 seconds
- Maximum: 30 seconds
- Marketplace requirement: Modules should respond within 5 seconds

## Error Handling

The adapter handles various error scenarios:

1. **Network errors**: Connection failures, DNS errors
2. **Timeout errors**: Request exceeds configured timeout
3. **HTTP errors**: Non-200 status codes
4. **JSON errors**: Invalid JSON response
5. **Validation errors**: Missing required fields

All errors are logged and recorded in health metrics. Failed requests return `ExecuteResponse` with `success=False` and an error message.

## Creating Custom Adapters

To create a custom adapter, extend `BaseAdapter`:

```python
from module_sdk.adapters import BaseAdapter
from module_sdk import ExecuteRequest, ExecuteResponse

class MyCustomAdapter(BaseAdapter):
    MODULE_NAME = "my_custom_adapter"
    MODULE_VERSION = "1.0.0"

    async def execute_async(self, request: ExecuteRequest) -> ExecuteResponse:
        # Your custom logic here
        try:
            # ... do work ...
            self.health.record_success()
            return ExecuteResponse(success=True, message="Done")
        except Exception as e:
            self.health.record_failure()
            return ExecuteResponse(success=False, error=str(e))
```

## API Reference

### WebhookAdapter

#### Constructor Parameters

- `webhook_url` (str): The URL of the external webhook endpoint
- `secret_key` (str): Secret key for HMAC signature generation
- `module_name` (str): Name of the module
- `timeout` (float, optional): Request timeout in seconds (default: 5.0, max: 30.0)
- `module_version` (str, optional): Version of the module (default: "1.0.0")
- `required_scopes` (list, optional): List of required permission scopes

#### Methods

- `execute_async(request: ExecuteRequest) -> ExecuteResponse`: Execute webhook request asynchronously
- `execute(request: ExecuteRequest) -> ExecuteResponse`: Synchronous wrapper (for compatibility)
- `get_health_status() -> Dict[str, Any]`: Get current health status
- `is_healthy() -> bool`: Check if adapter is healthy
- `get_module_info() -> Dict[str, Any]`: Get module metadata

### BaseAdapter

Abstract base class for all adapters.

#### Methods

- `execute_async(request: ExecuteRequest) -> ExecuteResponse`: Abstract method to implement
- `execute(request: ExecuteRequest) -> ExecuteResponse`: Synchronous wrapper
- `get_health_status() -> Dict[str, Any]`: Get health status
- `is_healthy() -> bool`: Check health status

## Testing

See `example_usage.py` for a complete working example.

To test your webhook adapter:

```bash
# Run the example
python -m module_sdk.adapters.example_usage
```

## Requirements

- Python 3.8+
- httpx >= 0.27.0
- Other module_sdk dependencies

## See Also

- [Module SDK Documentation](../README.md)
- [BaseModule Documentation](../base/README.md)
- [Marketplace Module Guide](.PLAN-v2)
