# LambdaAdapter Quick Reference

## Installation

```bash
pip install boto3
```

## Basic Usage

```python
import asyncio
from module_sdk.adapters import LambdaAdapter
from module_sdk import ExecuteRequest

async def main():
    adapter = LambdaAdapter(
        function_identifier="my-function",
        region="us-east-1",
        module_name="my_module",
    )
    
    request = ExecuteRequest(
        command="process",
        args=["data"],
        user_id="user_123",
        entity_id="entity_456",
        community_id="community_789",
        session_id="session_abc123",
        platform="discord",
    )
    
    response = await adapter.execute_async(request)
    print(response.success, response.message)

asyncio.run(main())
```

## Configuration

### Minimal (Sync)
```python
LambdaAdapter(
    function_identifier="my-function",
    region="us-east-1",
    module_name="my_module",
)
```

### Async Mode
```python
LambdaAdapter(
    function_identifier="my-function",
    region="us-east-1",
    module_name="my_module",
    invocation_type="Event",  # Fire-and-forget
)
```

### With Function Prefix
```python
LambdaAdapter(
    function_identifier="my-function",
    function_prefix="waddlebot-",  # Invokes "waddlebot-my-function"
    region="us-east-1",
    module_name="my_module",
)
```

### With Custom Retry
```python
LambdaAdapter(
    function_identifier="my-function",
    region="us-east-1",
    module_name="my_module",
    max_retries=5,
    initial_retry_delay=1.0,
    max_retry_delay=60.0,
)
```

### With Full Config
```python
LambdaAdapter(
    function_identifier="arn:aws:lambda:us-east-1:123456789:function:my-fn",
    region="us-east-1",
    module_name="my_module",
    function_prefix=None,
    invocation_type="RequestResponse",
    max_retries=3,
    initial_retry_delay=0.5,
    max_retry_delay=30.0,
    connect_timeout=10.0,
    read_timeout=30.0,
    aws_access_key_id="YOUR_KEY",
    aws_secret_access_key="YOUR_SECRET",
    module_version="1.0.0",
    required_scopes=["community.read"],
)
```

## Execute Async

```python
response = await adapter.execute_async(request)

# Response attributes:
response.success        # bool
response.message        # str or None
response.data          # dict or None
response.error         # str or None
response.targets       # list of dicts
```

## Health Monitoring

```python
# Get status
health = adapter.get_health_status()
# {
#   "is_healthy": true,
#   "last_success": "2024-01-15T10:30:45Z",
#   "last_failure": null,
#   "consecutive_failures": 0,
#   "total_requests": 42,
#   "total_failures": 0,
#   "error_rate": 0.0
# }

# Check if healthy
if adapter.is_healthy():
    print("OK")
else:
    print("UNHEALTHY")

# Get info
info = adapter.get_module_info()
```

## Lambda Response Format

```python
# Lambda should return this JSON:
{
    "success": true,
    "message": "Done",
    "response_type": "text",
    "overlay_data": null,
    "browser_source_url": null,
    "targets": ["discord", "twitch"],
    "custom_field": "value"  # Preserved in response.data
}
```

## Error Handling

```python
try:
    response = await adapter.execute_async(request)
    
    if response.success:
        # Process success
        print(response.message)
    else:
        # Handle failure
        print(response.error)
        
except Exception as e:
    # Handle unexpected errors
    print(f"Error: {e}")

finally:
    # Check health
    if not adapter.is_healthy():
        print("Warning: Adapter unhealthy")
```

## AWS Credentials

### Option 1: Parameters
```python
LambdaAdapter(
    ...,
    aws_access_key_id="YOUR_KEY",
    aws_secret_access_key="YOUR_SECRET"
)
```

### Option 2: Environment Variables
```bash
export AWS_ACCESS_KEY_ID="YOUR_KEY"
export AWS_SECRET_ACCESS_KEY="YOUR_SECRET"
export AWS_DEFAULT_REGION="us-east-1"
```

### Option 3: IAM Role (EC2, Lambda, ECS)
```python
# No credentials needed - auto-detected
LambdaAdapter(...)
```

## IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": ["lambda:InvokeFunction"],
  "Resource": "arn:aws:lambda:us-east-1:123456789:function:my-function"
}
```

## ExecuteRequest Format

```python
ExecuteRequest(
    command="my_command",           # str
    args=["arg1", "arg2"],          # List[str]
    user_id="user_123",             # str
    entity_id="entity_456",         # str
    community_id="community_789",   # str
    session_id="session_abc123",    # str
    platform="discord",             # str
    metadata={                      # Dict
        "community": {
            "name": "My Server",
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
        "is_event": False,          # Command vs Event
    },
    scopes=["community.read"],       # List[str]
)
```

## Lambda Payload Structure

```json
{
  "community": {
    "id": "community_789",
    "name": "My Server",
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

## Exponential Backoff

```
Attempt 1: Immediate
Attempt 2: Wait 0.5s
Attempt 3: Wait 1.0s
Attempt 4: Wait 2.0s
Attempt 5: Wait 4.0s (capped at max_retry_delay)
```

## Common Use Cases

### 1. Data Processing
```python
adapter = LambdaAdapter(
    function_identifier="process-data",
    region="us-east-1",
    module_name="data_processor",
)
response = await adapter.execute_async(request)
```

### 2. Fire-and-Forget Email
```python
adapter = LambdaAdapter(
    function_identifier="send-email",
    region="us-east-1",
    module_name="email_service",
    invocation_type="Event",
)
response = await adapter.execute_async(request)
# Returns immediately
```

### 3. API Integration
```python
adapter = LambdaAdapter(
    function_identifier="api-gateway",
    region="us-east-1",
    module_name="api_proxy",
    read_timeout=60.0,
)
response = await adapter.execute_async(request)
```

### 4. Monitoring
```python
response = await adapter.execute_async(request)
health = adapter.get_health_status()

if health['error_rate'] > 0.1:  # 10% errors
    alert("High error rate on Lambda adapter")
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError: boto3` | `pip install boto3` |
| `ResourceNotFoundException` | Check function name and region |
| `AccessDeniedException` | Check AWS credentials and IAM permissions |
| `ReadTimeoutError` | Increase `read_timeout` parameter |
| `ThrottlingException` | Adapter automatically retries (check CloudWatch) |
| Adapter unhealthy | 3+ consecutive failures - check logs |

## Performance

- Sync: ~100-500ms (depends on Lambda execution time)
- Async: ~50-100ms (immediate return)
- Concurrent requests: Use asyncio.gather()

## Files

- `lambda_adapter.py` - Main implementation (555 lines)
- `LAMBDA_ADAPTER_README.md` - Overview and quick start
- `LAMBDA_ADAPTER_USAGE.md` - Complete usage guide
- `LAMBDA_ADAPTER_ARCHITECTURE.md` - Technical details
- `lambda_adapter_example.py` - 10 working examples

## More Information

See `LAMBDA_ADAPTER_USAGE.md` for comprehensive documentation.
