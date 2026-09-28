# LambdaAdapter Architecture Documentation

This document describes the architecture, design patterns, and implementation details of the LambdaAdapter.

## Overview

The `LambdaAdapter` is a specialized adapter that extends `BaseAdapter` to enable seamless integration of AWS Lambda functions with WaddleBot's module system. It provides:

- **Async/Await Support**: Full async/await compatibility for non-blocking execution
- **Invocation Modes**: Both synchronous (RequestResponse) and asynchronous (Event) modes
- **Retry Logic**: Exponential backoff retry mechanism for transient failures
- **Response Parsing**: Automatic parsing and normalization of Lambda responses
- **Health Tracking**: Comprehensive health monitoring and metrics
- **Configuration Flexibility**: Extensive configuration options for different use cases

## Architecture

### Class Hierarchy

```
BaseModule (abstract)
    └── BaseAdapter (abstract)
            └── LambdaAdapter (concrete)
```

### Key Components

#### 1. Initialization (`__init__`)

The adapter requires:
- **AWS Configuration**: Lambda function identifier, region, and optional credentials
- **Module Configuration**: Module name, version, and required scopes
- **Invocation Configuration**: Sync/async mode selection
- **Retry Configuration**: Max retries, initial delay, max delay
- **Timeout Configuration**: Connection and read timeouts

```python
adapter = LambdaAdapter(
    function_identifier="my-function",
    region="us-east-1",
    module_name="my_module",
    # ... other params
)
```

#### 2. Boto3 Client Management

The adapter initializes a boto3 Lambda client with:
- Region configuration
- AWS credentials (from parameters or environment)
- Timeout settings

```python
self.lambda_client = boto3.client(
    'lambda',
    region_name=region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
    connect_timeout=int(connect_timeout),
    read_timeout=int(read_timeout),
)
```

#### 3. Request Payload Building

Transforms `ExecuteRequest` into Lambda-compatible payload:

```
ExecuteRequest
    ↓
_build_lambda_payload()
    ↓
Lambda Invocation Payload
{
  "community": {...},
  "trigger": {...},
  "user": {...},
  "entity": {...},
  "request_id": "...",
  "timestamp": "..."
}
```

#### 4. Lambda Invocation with Retry

Handles Lambda invocation with exponential backoff:

```
Invoke Attempt 1
    ↓ (if retryable error)
Wait: initial_retry_delay * 1
    ↓
Invoke Attempt 2
    ↓ (if retryable error)
Wait: initial_retry_delay * 2
    ↓
Invoke Attempt 3
    ↓ (if retryable error)
Wait: initial_retry_delay * 4 (capped at max_retry_delay)
    ↓
Invoke Attempt 4
    ↓ (if max_retries exceeded)
Fail
```

#### 5. Response Parsing

Normalizes Lambda responses:

```
Lambda Response (JSON)
    ↓
_parse_lambda_response()
    ↓
ExecuteResponse
{
  success: bool,
  message: str,
  data: dict,
  error: str,
  targets: list
}
```

#### 6. Health Tracking

Updates health metrics automatically:

```python
if success:
    self.health.record_success()
    # - Increments total_requests
    # - Sets last_success timestamp
    # - Clears consecutive_failures
    # - Updates error_rate
else:
    self.health.record_failure()
    # - Increments total_requests and total_failures
    # - Sets last_failure timestamp
    # - Increments consecutive_failures
    # - Marks unhealthy if failures >= 3
```

## Data Flow

### Execution Flow

```
1. User calls: adapter.execute_async(request)
                    ↓
2. execute_async() extracts and validates request
                    ↓
3. _build_lambda_payload() transforms ExecuteRequest
                    ↓
4. _invoke_lambda_with_retry() invokes Lambda with retries
                    ↓
5. _parse_lambda_response() normalizes response
                    ↓
6. health.record_success() or health.record_failure()
                    ↓
7. Returns ExecuteResponse to caller
```

### Request Payload Structure

```
ExecuteRequest
├── command: str (e.g., "process_data")
├── args: List[str] (e.g., ["arg1", "arg2"])
├── user_id: str
├── entity_id: str
├── community_id: str
├── session_id: str
├── platform: str (e.g., "discord")
├── metadata: Dict
│   ├── community: Dict (with name, is_subscribed, etc.)
│   ├── user: Dict (with username, platform_user_id, etc.)
│   ├── entity: Dict (with platform_entity_id, etc.)
│   ├── is_event: bool
│   ├── event_type: str (optional)
│   └── event_data: Dict (optional)
└── scopes: List[str] (permission scopes)
        ↓
        [_build_lambda_payload]
        ↓
Lambda Payload
{
  "community": {
    "id": "...",
    "name": "...",
    "is_subscribed": true,
    "subscription_order_id": "...",
    "seat_count": 0
  },
  "trigger": {
    "type": "command" | "event",
    "command": "...",
    "context_text": "...",
    "event_type": null,
    "event_data": null
  },
  "user": {
    "id": "...",
    "username": "...",
    "platform": "...",
    "platform_user_id": "..."
  },
  "entity": {
    "id": "...",
    "platform": "...",
    "platform_entity_id": "..."
  },
  "request_id": "...",
  "timestamp": "..."
}
```

### Response Payload Structure

```
Lambda Response (JSON)
{
  "success": true,
  "message": "Operation completed",
  "response_type": "text",
  "overlay_data": null,
  "browser_source_url": null,
  "targets": ["discord", "twitch"],
  "custom_field": "custom_value"
}
        ↓
        [_parse_lambda_response]
        ↓
ExecuteResponse
{
  success: True,
  message: "Operation completed",
  data: {
    "response_type": "text",
    "overlay_data": null,
    "browser_source_url": null,
    "targets": ["discord", "twitch"],
    "custom_field": "custom_value"
  },
  error: None,
  targets: [
    {"type": "discord"},
    {"type": "twitch"}
  ]
}
```

## Error Handling Strategy

### Retryable Errors

The adapter automatically retries on transient failures:

1. **AWS Service Errors**
   - `ThrottlingException`
   - `ServiceUnavailableException`
   - `TooManyRequestsException`

2. **Network Errors**
   - `ConnectionError`
   - `EndpointConnectionError`
   - `HTTPClientError`
   - `ReadTimeoutError`

3. **Exponential Backoff**
   ```python
   delay = min(
       initial_retry_delay * (2 ** attempt),
       max_retry_delay
   )
   ```

### Non-Retryable Errors

Fail immediately without retry:

1. **Configuration Errors**
   - Invalid function name/ARN
   - Invalid region

2. **Permission Errors**
   - Access denied (IAM)
   - Function not found

3. **Function Errors**
   - Code exceptions in Lambda
   - Invalid response format

4. **Resource Errors**
   - Memory exceeded
   - Timeout exceeded

## Health Tracking

### Health Metrics

```python
class HealthStatus:
    is_healthy: bool              # True if < 3 consecutive failures
    last_success: datetime        # Timestamp of last success
    last_failure: datetime        # Timestamp of last failure
    consecutive_failures: int     # Counter for consecutive failures
    total_requests: int           # Total requests processed
    total_failures: int           # Total failures
    error_rate: float             # total_failures / total_requests
```

### Health State Machine

```
Initial State: is_healthy = True

Success Path:
├── record_success() called
├── consecutive_failures = 0
├── last_success = now
└── is_healthy = True

Failure Path:
├── record_failure() called
├── consecutive_failures += 1
├── last_failure = now
└── if consecutive_failures >= 3:
    └── is_healthy = False
```

## Invocation Modes

### Synchronous Invocation (RequestResponse)

```
1. Invoke Lambda function
2. Wait for response
3. Parse response body
4. Return parsed response

Timeout: read_timeout seconds
```

### Asynchronous Invocation (Event)

```
1. Queue Lambda function
2. Return immediately
3. Don't wait for response

Timeout: None
Response: {"success": True, "message": "Lambda function queued"}
```

## Configuration Validation

The adapter validates all configuration parameters:

```python
- function_identifier: non-empty string
- region: non-empty string
- module_name: alphanumeric + underscore/hyphen
- invocation_type: "RequestResponse" or "Event"
- max_retries: >= 0
- initial_retry_delay: > 0
- max_retry_delay: > 0
- connect_timeout: > 0
- read_timeout: > 0
```

Raises `ValueError` on invalid configuration.

## Function Naming

The adapter supports three forms of function identification:

### 1. Function Name

```python
LambdaAdapter(
    function_identifier="my-function",
    region="us-east-1",
)
# Invokes: my-function
```

### 2. Function ARN

```python
LambdaAdapter(
    function_identifier="arn:aws:lambda:us-east-1:123456789:function:my-function",
    region="us-east-1",  # Can differ from ARN region
)
# Invokes: arn:aws:lambda:us-east-1:123456789:function:my-function
```

### 3. Function Name with Prefix

```python
LambdaAdapter(
    function_identifier="my-function",
    function_prefix="waddlebot-",
    region="us-east-1",
)
# Invokes: waddlebot-my-function
```

## Concurrency Considerations

The adapter is fully async and supports concurrent invocations:

```python
adapters = [
    LambdaAdapter(...),
    LambdaAdapter(...),
    LambdaAdapter(...)
]

responses = await asyncio.gather(
    adapter1.execute_async(request1),
    adapter2.execute_async(request2),
    adapter3.execute_async(request3)
)
```

Each adapter maintains independent:
- Boto3 client connection
- Health status
- Configuration

## Resource Management

### Boto3 Client

The adapter creates a single boto3 Lambda client on initialization that persists for the adapter's lifetime. The client handles:
- Connection pooling
- Credential management
- Timeout handling

### Memory Usage

- One client per adapter instance
- Minimal state tracking (HealthStatus object)
- Response objects are ephemeral

### Cleanup

The adapter doesn't require explicit cleanup. Python's garbage collection handles resource cleanup when the adapter is destroyed.

## Logging

The adapter uses Python's standard `logging` module:

```python
logger = logging.getLogger(__name__)

# Log levels used:
# - INFO: Initialization, successful invocations
# - DEBUG: Detailed invocation information
# - WARNING: Retries, failed operations
# - ERROR: Unrecoverable errors
```

### Log Examples

```
INFO: LambdaAdapter initialized for module 'my_module' with function 'my-function' in region 'us-east-1'
DEBUG: Invoking Lambda function 'my-function' (attempt 1/3, session: session_123)
WARNING: Lambda invocation failed (ThrottlingException): ... Retrying in 1.00s (attempt 1/3)
ERROR: Lambda invocation error: ... (attempt 3/3)
INFO: Lambda execution successful for module 'my_module' (session: session_123)
```

## Performance Characteristics

### Latency

- **Sync Mode**: Lambda timeout + network latency + retries
- **Async Mode**: Network latency only (instant return)

### Throughput

- Limited by Lambda concurrent execution limits
- Limited by AWS account throttling
- Use multiple adapter instances for parallel execution

### Resource Usage

- CPU: Minimal (mostly I/O waiting)
- Memory: ~50MB per adapter
- Network: One connection per invocation

## Security Considerations

### Authentication

- Credentials passed to `LambdaAdapter.__init__()`
- Or via environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
- Or via IAM role (when running on EC2, Lambda, ECS)

### Authorization

- Lambda function must be accessible by the credentials
- IAM role must have `lambda:InvokeFunction` permission

### Data Protection

- Request payloads contain sensitive data (user IDs, platform details)
- Ensure Lambda function handles data securely
- Use VPC endpoints for private invocation if needed
- Encrypt sensitive data in payload

## Testing

The adapter can be tested with:

1. **Unit Tests**: Mock boto3 client
2. **Integration Tests**: Use AWS Lambda test environment
3. **Local Testing**: Use LocalStack or moto

```python
from unittest.mock import Mock, patch

@patch('boto3.client')
def test_invoke(mock_boto3):
    adapter = LambdaAdapter(...)
    # ... test code ...
```

## Extensions and Customization

### Creating Specialized Adapters

```python
class CustomLambdaAdapter(LambdaAdapter):
    def _build_lambda_payload(self, request):
        # Custom payload building logic
        payload = super()._build_lambda_payload(request)
        payload['custom_field'] = 'custom_value'
        return payload

    def _parse_lambda_response(self, response_data):
        # Custom response parsing logic
        response = super()._parse_lambda_response(response_data)
        # ... custom processing ...
        return response
```

### Monitoring and Metrics

```python
# Export health metrics to monitoring system
adapter = LambdaAdapter(...)
response = await adapter.execute_async(request)

metrics = adapter.get_health_status()
# Export to CloudWatch, Prometheus, etc.
send_metrics({
    'error_rate': metrics['error_rate'],
    'total_requests': metrics['total_requests'],
    'is_healthy': metrics['is_healthy']
})
```

## Troubleshooting

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| `ModuleNotFoundError: No module named 'boto3'` | boto3 not installed | `pip install boto3` |
| `ResourceNotFoundException` | Function doesn't exist | Verify function name and region |
| `AccessDeniedException` | Insufficient IAM permissions | Grant `lambda:InvokeFunction` permission |
| `ReadTimeoutError` | Lambda execution timeout | Increase `read_timeout` parameter |
| `ThrottlingException` with max retries | Account throttling | Implement exponential backoff (built-in) |

## Future Enhancements

Potential improvements:

1. **Connection Pooling**: Optimize boto3 client reuse
2. **Metrics Integration**: Built-in CloudWatch metrics
3. **Circuit Breaker**: Automatic failure handling
4. **Caching**: Response caching for deterministic calls
5. **Rate Limiting**: Built-in rate limiting
6. **Dead Letter Queue**: Failed request handling
