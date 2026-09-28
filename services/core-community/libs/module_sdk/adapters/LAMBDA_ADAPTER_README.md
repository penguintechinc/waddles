# LambdaAdapter - AWS Lambda Integration for WaddleBot

Complete AWS Lambda adapter implementation for WaddleBot's module system.

## Quick Start

```python
import asyncio
from module_sdk.adapters import LambdaAdapter
from module_sdk import ExecuteRequest

async def main():
    # Create adapter
    adapter = LambdaAdapter(
        function_identifier="my-lambda-function",
        region="us-east-1",
        module_name="my_module",
    )

    # Create request
    request = ExecuteRequest(
        command="process",
        args=["data"],
        user_id="user_123",
        entity_id="entity_456",
        community_id="community_789",
        session_id="session_abc123",
        platform="discord",
    )

    # Execute
    response = await adapter.execute_async(request)
    print(f"Success: {response.success}, Message: {response.message}")

asyncio.run(main())
```

## Files Included

### Core Implementation

- **`lambda_adapter.py`** (555 lines)
  - Main adapter implementation
  - Full docstrings and type hints
  - Async/await support
  - Exponential backoff retry logic
  - Health tracking
  - Response parsing

### Documentation

- **`LAMBDA_ADAPTER_USAGE.md`**
  - Comprehensive usage guide
  - Configuration reference
  - Code examples
  - Troubleshooting guide
  - IAM permissions required

- **`LAMBDA_ADAPTER_ARCHITECTURE.md`**
  - Design patterns and architecture
  - Data flow diagrams
  - Component descriptions
  - Error handling strategy
  - Performance characteristics

- **`lambda_adapter_example.py`** (428 lines)
  - 10 practical examples
  - Different use cases
  - Real-world workflows
  - Runnable demonstrations

## Features

### Core Features

- **AWS Lambda Invocation**: Invoke functions via boto3
- **Dual Invocation Modes**:
  - `RequestResponse`: Synchronous, waits for response
  - `Event`: Asynchronous, fire-and-forget
- **Function Identification**:
  - Function name: `"my-function"`
  - Function ARN: `"arn:aws:lambda:..."`
  - Prefix support: `"waddlebot-" + "my-function"`

### Advanced Features

- **Exponential Backoff Retry Logic**
  - Configurable retry attempts (default: 3)
  - Configurable backoff delays (default: 0.5s initial, 30s max)
  - Smart retry classification (retryable vs non-retryable errors)

- **Response Parsing**
  - Automatic JSON parsing
  - Payload extraction from boto3 responses
  - Error detection and reporting
  - Custom field preservation

- **Health Tracking**
  - Success/failure recording
  - Consecutive failure tracking
  - Error rate calculation
  - Last success/failure timestamps
  - Unhealthy state detection (3+ failures)

- **Request/Response Mapping**
  - ExecuteRequest → Lambda payload transformation
  - Lambda response → ExecuteResponse conversion
  - Metadata preservation
  - Target routing support

## Configuration

### Required Parameters

```python
LambdaAdapter(
    function_identifier="my-function",      # Function name or ARN
    region="us-east-1",                      # AWS region
    module_name="my_module",                 # Module name
)
```

### Optional Parameters

```python
LambdaAdapter(
    function_identifier="my-function",
    region="us-east-1",
    module_name="my_module",
    function_prefix="waddlebot-",           # Prefix for function name
    invocation_type="RequestResponse",       # or "Event"
    max_retries=3,                           # Retry attempts
    initial_retry_delay=0.5,                 # Seconds
    max_retry_delay=30.0,                    # Seconds
    connect_timeout=10.0,                    # Seconds
    read_timeout=30.0,                       # Seconds
    aws_access_key_id="...",                # Optional credentials
    aws_secret_access_key="...",            # Optional credentials
    module_version="1.0.0",                  # Module version
    required_scopes=["community.read"],      # Permission scopes
)
```

### AWS Credentials

Credentials can be provided via:

1. **Parameters**:
   ```python
   LambdaAdapter(
       ...,
       aws_access_key_id="YOUR_KEY",
       aws_secret_access_key="YOUR_SECRET"
   )
   ```

2. **Environment Variables**:
   ```bash
   export AWS_ACCESS_KEY_ID="YOUR_KEY"
   export AWS_SECRET_ACCESS_KEY="YOUR_SECRET"
   export AWS_DEFAULT_REGION="us-east-1"
   ```

3. **IAM Role** (EC2, Lambda, ECS):
   - Automatically detected, no configuration needed

## Request/Response Formats

### Lambda Invocation Payload

```json
{
  "community": {
    "id": "community_789",
    "name": "My Community",
    "is_subscribed": true,
    "subscription_order_id": "order_123",
    "seat_count": 50
  },
  "trigger": {
    "type": "command",
    "command": "my_command",
    "context_text": "arg1 arg2",
    "event_type": null,
    "event_data": null
  },
  "user": {
    "id": "user_123",
    "username": "MyUser",
    "platform": "discord",
    "platform_user_id": "discord_123"
  },
  "entity": {
    "id": "entity_456",
    "platform": "discord",
    "platform_entity_id": "channel_456"
  },
  "request_id": "session_abc123",
  "timestamp": "2024-01-15T10:30:45.123456Z"
}
```

### Expected Lambda Response

```json
{
  "success": true,
  "message": "Operation completed",
  "response_type": "text",
  "overlay_data": null,
  "browser_source_url": null,
  "targets": ["discord", "twitch"],
  "custom_field": "preserved_value"
}
```

## Health Monitoring

```python
# Get health status
health = adapter.get_health_status()
# {
#   "is_healthy": true,
#   "last_success": "2024-01-15T10:30:45.123456Z",
#   "last_failure": null,
#   "consecutive_failures": 0,
#   "total_requests": 42,
#   "total_failures": 0,
#   "error_rate": 0.0
# }

# Check if healthy (< 3 consecutive failures)
if adapter.is_healthy():
    print("Adapter is healthy")
else:
    print("Adapter is unhealthy")

# Get module info
info = adapter.get_module_info()
# Contains: name, version, type, function_name, region, etc.
```

## Usage Examples

### Example 1: Basic Sync Invocation

```python
adapter = LambdaAdapter(
    function_identifier="process-data",
    region="us-east-1",
    module_name="data_processor",
)

request = ExecuteRequest(
    command="analyze",
    args=["dataset.csv"],
    user_id="user_123",
    entity_id="entity_456",
    community_id="community_789",
    session_id="session_abc123",
    platform="discord",
)

response = await adapter.execute_async(request)
```

### Example 2: Async Invocation (Fire-and-Forget)

```python
adapter = LambdaAdapter(
    function_identifier="send-email",
    region="us-east-1",
    module_name="email_service",
    invocation_type="Event",  # Async mode
)

response = await adapter.execute_async(request)
# Returns immediately: {"success": True, "message": "Lambda function queued"}
```

### Example 3: With Retry Configuration

```python
adapter = LambdaAdapter(
    function_identifier="unreliable-service",
    region="us-east-1",
    module_name="resilient_module",
    max_retries=5,
    initial_retry_delay=1.0,
    max_retry_delay=60.0,
)
```

### Example 4: With Full Metadata

```python
request = ExecuteRequest(
    command="process",
    args=["arg1"],
    user_id="user_123",
    entity_id="entity_456",
    community_id="community_789",
    session_id="session_abc123",
    platform="discord",
    metadata={
        "community": {
            "name": "My Community",
            "is_subscribed": True,
            "subscription_order_id": "order_123",
            "seat_count": 50,
        },
        "user": {
            "username": "MyUser",
            "platform_user_id": "discord_123",
        },
        "entity": {
            "platform_entity_id": "channel_456",
        },
    },
    scopes=["community.read"],
)
```

### Example 5: Error Handling

```python
try:
    response = await adapter.execute_async(request)
    
    if response.success:
        print(f"Success: {response.message}")
    else:
        print(f"Failed: {response.error}")
        
except Exception as e:
    print(f"Error: {e}")
finally:
    health = adapter.get_health_status()
    if not adapter.is_healthy():
        print(f"Warning: Adapter unhealthy ({health['consecutive_failures']} failures)")
```

## Requirements

- Python 3.8+
- boto3 (for AWS Lambda client)

Install requirements:

```bash
pip install boto3
```

## Architecture Highlights

### Async/Await Support
- Full async implementation using `asyncio`
- Non-blocking execution
- Concurrent request handling

### Exponential Backoff Retry
- Automatically retries transient failures
- Smart error classification
- Configurable backoff strategy

### Health Tracking
- Tracks success/failure metrics
- Monitors error rates
- Detects unhealthy states (3+ failures)

### Response Normalization
- Parses AWS Lambda responses
- Extracts status from response body
- Preserves custom fields
- Handles errors gracefully

## Validation

The adapter validates all inputs:

- Function identifier (non-empty)
- AWS region (non-empty)
- Module name (alphanumeric + underscore/hyphen)
- Invocation type (RequestResponse or Event)
- Numeric parameters (positive values)
- Timeout values (positive, within limits)

Raises `ValueError` for invalid configuration.

## IAM Permissions Required

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["lambda:InvokeFunction"],
      "Resource": [
        "arn:aws:lambda:us-east-1:123456789:function:my-function",
        "arn:aws:lambda:us-east-1:123456789:function:waddlebot-*"
      ]
    }
  ]
}
```

## Error Categories

### Retryable (Auto-Retry with Exponential Backoff)
- ThrottlingException
- ServiceUnavailableException
- TooManyRequestsException
- Connection errors
- Timeout errors

### Non-Retryable (Immediate Failure)
- ResourceNotFoundException
- AccessDeniedException
- InvalidParameterValueException
- Function code errors

## Performance

### Latency

- **Sync Mode**: Lambda execution time + network latency + retries
- **Async Mode**: Network latency only (~50-100ms)

### Throughput

- Limited by Lambda concurrent execution limits
- Supports concurrent requests via asyncio
- Multiple adapters for parallelism

### Resource Usage

- CPU: Minimal (I/O bound)
- Memory: ~50MB per adapter instance
- Network: One connection per invocation

## Testing

See `lambda_adapter_example.py` for:
- 10 working examples
- Different configurations
- Error handling patterns
- Concurrent invocations
- Real-world workflows

Run examples:

```bash
python lambda_adapter_example.py
```

## Troubleshooting

### boto3 Not Installed
```bash
pip install boto3
```

### Function Not Found
- Verify function name or ARN
- Verify region is correct
- Check function is deployed to that region

### Access Denied
- Check AWS credentials
- Verify IAM permissions
- Check function ARN is accessible

### Timeout Issues
- Increase `read_timeout` parameter
- Check Lambda function execution time
- Review CloudWatch logs

See `LAMBDA_ADAPTER_USAGE.md` for detailed troubleshooting.

## Documentation

- **LAMBDA_ADAPTER_USAGE.md**: Complete usage guide with examples
- **LAMBDA_ADAPTER_ARCHITECTURE.md**: Technical architecture and design
- **lambda_adapter_example.py**: 10 practical examples

## Integration with WaddleBot

The adapter integrates seamlessly with WaddleBot:

1. Create adapter instance
2. Register as external module
3. Handle incoming ExecuteRequest
4. Process via Lambda
5. Return ExecuteResponse
6. Monitor health metrics

## Version History

- **1.0.0**: Initial release
  - AWS Lambda invocation
  - Sync and async modes
  - Exponential backoff retry
  - Health tracking
  - Response parsing

## License

Same as WaddleBot project

## Support

For issues or questions:
1. Check LAMBDA_ADAPTER_USAGE.md
2. Review lambda_adapter_example.py
3. Check LAMBDA_ADAPTER_ARCHITECTURE.md
4. Review CloudWatch logs
