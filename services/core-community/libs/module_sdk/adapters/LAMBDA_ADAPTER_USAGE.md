# LambdaAdapter Usage Guide

The `LambdaAdapter` integrates AWS Lambda functions with WaddleBot's module system, enabling serverless module execution with automatic retry logic, health tracking, and response parsing.

## Installation

Make sure `boto3` is installed:

```bash
pip install boto3
```

## Basic Usage

### 1. Simple Synchronous Invocation

```python
import asyncio
from module_sdk.adapters import LambdaAdapter
from module_sdk import ExecuteRequest

async def main():
    # Create a Lambda adapter
    adapter = LambdaAdapter(
        function_identifier="my-lambda-function",
        region="us-east-1",
        module_name="my_module",
        invocation_type="RequestResponse",  # Synchronous
    )

    # Create a request
    request = ExecuteRequest(
        command="process_data",
        args=["arg1", "arg2"],
        user_id="user_123",
        entity_id="entity_456",
        community_id="community_789",
        session_id="session_abc123",
        platform="discord",
    )

    # Execute the Lambda function
    response = await adapter.execute_async(request)

    print(f"Success: {response.success}")
    print(f"Message: {response.message}")
    print(f"Error: {response.error}")

asyncio.run(main())
```

### 2. Asynchronous (Event) Invocation

For fire-and-forget invocations that don't wait for a response:

```python
adapter = LambdaAdapter(
    function_identifier="my-lambda-function",
    region="us-east-1",
    module_name="my_module",
    invocation_type="Event",  # Asynchronous
)

response = await adapter.execute_async(request)
# Response will be {"success": True, "message": "Lambda function queued"}
```

### 3. With Function Prefix

Prepend a prefix to the function name (useful for organizational schemes):

```python
adapter = LambdaAdapter(
    function_identifier="my-function",
    function_prefix="waddlebot-",  # Will invoke "waddlebot-my-function"
    region="us-east-1",
    module_name="my_module",
)
```

### 4. With Custom AWS Credentials

```python
adapter = LambdaAdapter(
    function_identifier="my-lambda-function",
    region="us-east-1",
    module_name="my_module",
    aws_access_key_id="YOUR_ACCESS_KEY",
    aws_secret_access_key="YOUR_SECRET_KEY",
)
```

Alternatively, set AWS credentials as environment variables:

```bash
export AWS_ACCESS_KEY_ID="your_access_key"
export AWS_SECRET_ACCESS_KEY="your_secret_key"
export AWS_DEFAULT_REGION="us-east-1"
```

### 5. With Retry Configuration

Configure exponential backoff retry logic:

```python
adapter = LambdaAdapter(
    function_identifier="my-lambda-function",
    region="us-east-1",
    module_name="my_module",
    max_retries=5,                    # Max retry attempts
    initial_retry_delay=1.0,          # Start with 1 second delay
    max_retry_delay=60.0,             # Cap at 60 seconds
)
```

### 6. With Custom Timeouts

```python
adapter = LambdaAdapter(
    function_identifier="my-lambda-function",
    region="us-east-1",
    module_name="my_module",
    connect_timeout=15.0,   # Connection timeout
    read_timeout=60.0,      # Read timeout (should be >= Lambda timeout)
)
```

## Configuration Parameters

### Required Parameters

- **function_identifier** (str): Lambda function name or ARN
  - Example: `"my-function"` or `"arn:aws:lambda:us-east-1:123456789:function:my-function"`

- **region** (str): AWS region
  - Example: `"us-east-1"`, `"eu-west-1"`, `"ap-southeast-1"`

- **module_name** (str): Name of the WaddleBot module
  - Example: `"weather_module"`, `"data_processor"`

### Optional Parameters

- **function_prefix** (str, optional): Prefix to prepend to function name
  - Default: `None`
  - Example: `"waddlebot-"` → invokes `"waddlebot-my-function"`

- **invocation_type** (str, default: `"RequestResponse"`):
  - `"RequestResponse"`: Synchronous invocation, waits for response
  - `"Event"`: Asynchronous invocation, returns immediately

- **max_retries** (int, default: `3`):
  - Number of retry attempts for transient failures
  - Retryable errors: Throttling, Service Unavailable, Connection errors

- **initial_retry_delay** (float, default: `0.5`):
  - Initial delay in seconds before first retry
  - Uses exponential backoff: delay * (2 ^ attempt)

- **max_retry_delay** (float, default: `30.0`):
  - Maximum delay cap for exponential backoff

- **connect_timeout** (float, default: `10.0`):
  - Connection establishment timeout in seconds

- **read_timeout** (float, default: `30.0`):
  - Response read timeout in seconds
  - Should be greater than or equal to Lambda function timeout

- **aws_access_key_id** (str, optional):
  - AWS access key ID
  - If not provided, uses environment variables or IAM role

- **aws_secret_access_key** (str, optional):
  - AWS secret access key
  - If not provided, uses environment variables or IAM role

- **module_version** (str, default: `"1.0.0"`):
  - Semantic version of the module

- **required_scopes** (list, optional):
  - List of permission scopes required by the module
  - Example: `["community.read", "user.write"]`

## Lambda Function Response Format

The Lambda function should return a JSON response with this structure:

```json
{
  "success": true,
  "message": "Operation completed successfully",
  "response_type": "text",
  "overlay_data": null,
  "browser_source_url": null,
  "targets": ["discord", "twitch"],
  "additional_field": "additional_value"
}
```

### Response Fields

- **success** (bool, required): Whether the operation succeeded
- **message** (str, optional): Message to display to the user
- **response_type** (str, optional): Type of response ("text", "embed", etc.)
- **overlay_data** (object, optional): Data for overlay rendering
- **browser_source_url** (str, optional): URL for browser source integration
- **targets** (list, optional): Platforms to send the response to
  - Can be strings: `["discord", "twitch"]`
  - Or objects: `[{"type": "discord", "channel_id": "123"}]`
- **error** (str, optional): Error message if success is false
- **Additional fields**: Any other fields are preserved in response.data

## Health Tracking

The adapter tracks health metrics automatically:

```python
# Get health status
health_status = adapter.get_health_status()
print(health_status)
# {
#   "is_healthy": true,
#   "last_success": "2024-01-15T10:30:45.123456Z",
#   "last_failure": "2024-01-15T10:25:30.654321Z",
#   "consecutive_failures": 0,
#   "total_requests": 42,
#   "total_failures": 2,
#   "error_rate": 0.0476
# }

# Check if healthy
if adapter.is_healthy():
    print("Lambda adapter is healthy")
else:
    print("Lambda adapter is unhealthy (3+ consecutive failures)")
```

## Module Information

Get detailed information about the adapter:

```python
info = adapter.get_module_info()
print(info)
# {
#   "name": "my_module",
#   "version": "1.0.0",
#   "scopes": ["community.read", "user.read"],
#   "type": "lambda_adapter",
#   "function_name": "my-function",
#   "region": "us-east-1",
#   "invocation_type": "RequestResponse",
#   "max_retries": 3,
#   "connect_timeout": 10.0,
#   "read_timeout": 30.0,
#   "health": { ... }
# }
```

## Request Payload Format

The adapter automatically builds a payload with this structure:

```json
{
  "community": {
    "id": "community_789",
    "name": "AwesomeStreamers",
    "is_subscribed": true,
    "subscription_order_id": "ord_abc123",
    "seat_count": 45
  },
  "trigger": {
    "type": "command",
    "command": "process_data",
    "context_text": "arg1 arg2",
    "event_type": null,
    "event_data": null
  },
  "user": {
    "id": "user_123",
    "username": "CoolUser",
    "platform": "discord",
    "platform_user_id": "discord_user_456"
  },
  "entity": {
    "id": "entity_456",
    "platform": "discord",
    "platform_entity_id": "discord_channel_789"
  },
  "request_id": "session_abc123",
  "timestamp": "2024-01-15T10:30:45.123456Z"
}
```

## Error Handling

The adapter handles various error scenarios:

### Retryable Errors (Automatic Retry)

- `ThrottlingException`: Too many requests
- `ServiceUnavailableException`: AWS service temporarily unavailable
- `TooManyRequestsException`: Rate limiting
- Connection errors and timeouts

### Non-Retryable Errors

- Invalid function name/ARN
- Access denied (IAM permissions)
- Function code errors (returned in response)
- Invalid JSON in Lambda response

```python
response = await adapter.execute_async(request)

if not response.success:
    print(f"Error: {response.error}")
    # Check health
    if adapter.health.consecutive_failures >= 3:
        print("Adapter is unhealthy!")
```

## Complete Example with Error Handling

```python
import asyncio
import logging
from module_sdk.adapters import LambdaAdapter
from module_sdk import ExecuteRequest

logging.basicConfig(level=logging.INFO)

async def main():
    # Create adapter with retry configuration
    adapter = LambdaAdapter(
        function_identifier="arn:aws:lambda:us-east-1:123456789:function:my-function",
        region="us-east-1",
        module_name="weather_module",
        invocation_type="RequestResponse",
        max_retries=3,
        initial_retry_delay=1.0,
        max_retry_delay=30.0,
        module_version="2.0.0",
        required_scopes=["community.read"],
    )

    # Create request with full metadata
    request = ExecuteRequest(
        command="get_weather",
        args=["London"],
        user_id="user_123",
        entity_id="entity_456",
        community_id="community_789",
        session_id="session_abc123",
        platform="discord",
        metadata={
            "community": {
                "name": "Weather Enthusiasts",
                "is_subscribed": True,
                "subscription_order_id": "order_123",
                "seat_count": 50,
            },
            "user": {
                "username": "WeatherBot",
                "platform_user_id": "discord_user_123",
            },
            "entity": {
                "platform_entity_id": "discord_channel_456",
            },
        },
        scopes=["community.read"],
    )

    try:
        response = await adapter.execute_async(request)

        if response.success:
            print(f"Success: {response.message}")
            print(f"Data: {response.data}")
        else:
            print(f"Failed: {response.error}")

    except Exception as e:
        print(f"Unexpected error: {e}")

    finally:
        # Check final health status
        health = adapter.get_health_status()
        print(f"\nFinal Health Status:")
        print(f"  Healthy: {health['is_healthy']}")
        print(f"  Error Rate: {health['error_rate']:.2%}")
        print(f"  Total Requests: {health['total_requests']}")

asyncio.run(main())
```

## AWS IAM Permissions Required

Your Lambda execution role needs these permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "lambda:InvokeFunction"
      ],
      "Resource": [
        "arn:aws:lambda:us-east-1:123456789:function:my-function",
        "arn:aws:lambda:us-east-1:123456789:function:waddlebot-*"
      ]
    }
  ]
}
```

## Troubleshooting

### ModuleNotFoundError: No module named 'boto3'

Install boto3:
```bash
pip install boto3
```

### botocore.errorfactory.ResourceNotFoundException

The Lambda function doesn't exist in the specified region. Verify:
- Function name is correct
- Region is correct
- Function is deployed to that region

### botocore.errorfactory.AccessDeniedException

IAM permissions are insufficient. Ensure the execution role has `lambda:InvokeFunction` permission.

### ReadTimeoutError

The Lambda function is taking too long. Increase `read_timeout`:
```python
adapter = LambdaAdapter(
    ...,
    read_timeout=120.0,  # Increase if Lambda needs more time
)
```

### Too many retries

If you see repeated retries, check:
- AWS service status
- Lambda throttling (concurrent execution limit)
- Network connectivity
- CloudWatch logs for Lambda errors
