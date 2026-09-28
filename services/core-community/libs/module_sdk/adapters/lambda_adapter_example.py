"""
Practical examples of using the LambdaAdapter for AWS Lambda integration.

This module demonstrates various use cases and configurations for the LambdaAdapter.
"""

import asyncio
import logging
from lambda_adapter import LambdaAdapter
from ..base import ExecuteRequest


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def example_1_basic_sync_invocation():
    """Example 1: Basic synchronous Lambda invocation."""
    print("\n" + "=" * 60)
    print("Example 1: Basic Synchronous Invocation")
    print("=" * 60)

    # Create adapter for synchronous invocation
    adapter = LambdaAdapter(
        function_identifier="process-command",
        region="us-east-1",
        module_name="command_processor",
    )

    # Create a request
    request = ExecuteRequest(
        command="analyze",
        args=["user_data.json"],
        user_id="user_123",
        entity_id="entity_456",
        community_id="community_789",
        session_id="session_abc123",
        platform="discord",
    )

    # Execute
    response = await adapter.execute_async(request)

    print(f"\nResponse Success: {response.success}")
    print(f"Message: {response.message}")
    print(f"Error: {response.error}")
    print(f"Data: {response.data}")


async def example_2_async_invocation():
    """Example 2: Asynchronous (fire-and-forget) Lambda invocation."""
    print("\n" + "=" * 60)
    print("Example 2: Asynchronous Invocation (Event)")
    print("=" * 60)

    # Create adapter for asynchronous invocation
    adapter = LambdaAdapter(
        function_identifier="process-video",
        region="us-east-1",
        module_name="video_processor",
        invocation_type="Event",  # Async mode
    )

    request = ExecuteRequest(
        command="encode_video",
        args=["video.mp4"],
        user_id="user_123",
        entity_id="entity_456",
        community_id="community_789",
        session_id="session_abc123",
        platform="twitch",
    )

    response = await adapter.execute_async(request)
    print(f"\nAsync Response: {response.message}")
    print("Function queued successfully")


async def example_3_with_function_prefix():
    """Example 3: Using function prefix for organizational schemes."""
    print("\n" + "=" * 60)
    print("Example 3: Using Function Prefix")
    print("=" * 60)

    # Adapter that prepends 'waddlebot-' to function names
    adapter = LambdaAdapter(
        function_identifier="data-enricher",
        function_prefix="waddlebot-",  # Will invoke "waddlebot-data-enricher"
        region="eu-west-1",
        module_name="data_module",
    )

    print(f"\nFunction to invoke: {adapter._get_qualified_function_name()}")


async def example_4_with_retry_configuration():
    """Example 4: Configure custom retry behavior."""
    print("\n" + "=" * 60)
    print("Example 4: Custom Retry Configuration")
    print("=" * 60)

    # Adapter with aggressive retry strategy
    adapter = LambdaAdapter(
        function_identifier="unreliable-service",
        region="us-east-1",
        module_name="retry_example",
        max_retries=5,                    # More retries
        initial_retry_delay=0.5,          # Start slow
        max_retry_delay=60.0,             # But cap at 1 minute
    )

    print(f"\nRetry Configuration:")
    print(f"  Max retries: {adapter.max_retries}")
    print(f"  Initial delay: {adapter.initial_retry_delay}s")
    print(f"  Max delay: {adapter.max_retry_delay}s")

    # Exponential backoff progression:
    # Attempt 1: 0.5s
    # Attempt 2: 1.0s
    # Attempt 3: 2.0s
    # Attempt 4: 4.0s
    # Attempt 5: 8.0s
    # Attempt 6: 16.0s (would be 32s, but capped at max)


async def example_5_with_metadata():
    """Example 5: Include full metadata in request."""
    print("\n" + "=" * 60)
    print("Example 5: Request with Full Metadata")
    print("=" * 60)

    adapter = LambdaAdapter(
        function_identifier="weather-service",
        region="us-east-1",
        module_name="weather",
        required_scopes=["community.read", "user.read"],
    )

    # Create a detailed request with all metadata
    request = ExecuteRequest(
        command="get_weather",
        args=["London", "UK"],
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
        scopes=["community.read", "user.read"],
    )

    print(f"\nRequest Details:")
    print(f"  Command: {request.command}")
    print(f"  Args: {request.args}")
    print(f"  Platform: {request.platform}")
    print(f"  Community: {request.metadata['community']['name']}")
    print(f"  User: {request.metadata['user']['username']}")


async def example_6_health_tracking():
    """Example 6: Monitor adapter health status."""
    print("\n" + "=" * 60)
    print("Example 6: Health Status Tracking")
    print("=" * 60)

    adapter = LambdaAdapter(
        function_identifier="monitored-service",
        region="us-east-1",
        module_name="health_monitor",
    )

    # Check initial health
    health = adapter.get_health_status()
    print(f"\nInitial Health Status:")
    for key, value in health.items():
        print(f"  {key}: {value}")

    # Check if healthy
    is_healthy = adapter.is_healthy()
    print(f"\nAdapter is healthy: {is_healthy}")


async def example_7_module_info():
    """Example 7: Get detailed module information."""
    print("\n" + "=" * 60)
    print("Example 7: Module Information")
    print("=" * 60)

    adapter = LambdaAdapter(
        function_identifier="arn:aws:lambda:us-east-1:123456789:function:my-function",
        region="us-east-1",
        module_name="my_module",
        invocation_type="RequestResponse",
        max_retries=3,
        connect_timeout=15.0,
        read_timeout=45.0,
        module_version="2.0.1",
        required_scopes=["community.read"],
    )

    info = adapter.get_module_info()
    print(f"\nModule Information:")
    for key, value in info.items():
        if key != "health":
            print(f"  {key}: {value}")

    health = info.get("health", {})
    print(f"\nHealth Information:")
    for key, value in health.items():
        print(f"    {key}: {value}")


async def example_8_error_handling():
    """Example 8: Proper error handling and recovery."""
    print("\n" + "=" * 60)
    print("Example 8: Error Handling and Recovery")
    print("=" * 60)

    adapter = LambdaAdapter(
        function_identifier="error-prone-service",
        region="us-east-1",
        module_name="error_handler",
        max_retries=3,
    )

    request = ExecuteRequest(
        command="risky_operation",
        args=[],
        user_id="user_123",
        entity_id="entity_456",
        community_id="community_789",
        session_id="session_abc123",
        platform="discord",
    )

    try:
        response = await adapter.execute_async(request)

        if response.success:
            print(f"\nSuccess: {response.message}")
            print(f"Data: {response.data}")
        else:
            print(f"\nOperation failed:")
            print(f"  Error: {response.error}")

        # Check health after execution
        health = adapter.get_health_status()
        if not adapter.is_healthy():
            print(f"\nAdapter is unhealthy!")
            print(f"  Consecutive failures: {health['consecutive_failures']}")
            print(f"  Error rate: {health['error_rate']:.1%}")

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)


async def example_9_concurrent_invocations():
    """Example 9: Invoke multiple Lambda functions concurrently."""
    print("\n" + "=" * 60)
    print("Example 9: Concurrent Lambda Invocations")
    print("=" * 60)

    # Create multiple adapters
    adapters = [
        LambdaAdapter(
            function_identifier=f"service-{i}",
            region="us-east-1",
            module_name=f"service_{i}",
        )
        for i in range(3)
    ]

    # Create requests for each adapter
    requests = [
        ExecuteRequest(
            command=f"process_{i}",
            args=[f"data_{i}"],
            user_id="user_123",
            entity_id="entity_456",
            community_id="community_789",
            session_id=f"session_{i}",
            platform="discord",
        )
        for i in range(3)
    ]

    # Execute all concurrently
    print("\nExecuting 3 Lambda functions concurrently...")
    tasks = [
        adapter.execute_async(request)
        for adapter, request in zip(adapters, requests)
    ]

    # results = await asyncio.gather(*tasks)
    # print(f"Results: {len(results)} responses received")


async def example_10_complete_workflow():
    """Example 10: Complete real-world workflow."""
    print("\n" + "=" * 60)
    print("Example 10: Complete Real-World Workflow")
    print("=" * 60)

    # Setup adapter with production configuration
    adapter = LambdaAdapter(
        function_identifier="arn:aws:lambda:us-east-1:123456789:function:content-analyzer",
        region="us-east-1",
        module_name="content_analyzer",
        invocation_type="RequestResponse",
        max_retries=3,
        initial_retry_delay=1.0,
        max_retry_delay=30.0,
        connect_timeout=15.0,
        read_timeout=60.0,
        module_version="1.2.3",
        required_scopes=["community.read", "user.read"],
    )

    # Create a production request
    request = ExecuteRequest(
        command="analyze_content",
        args=["detect_sentiment", "--language=en"],
        user_id="user_123",
        entity_id="entity_456",
        community_id="community_789",
        session_id="session_abc123",
        platform="discord",
        metadata={
            "community": {
                "name": "Content Moderators",
                "is_subscribed": True,
                "subscription_order_id": "order_123",
                "seat_count": 100,
            },
            "user": {
                "username": "ContentBot",
                "platform_user_id": "discord_user_123",
            },
            "entity": {
                "platform_entity_id": "discord_channel_456",
            },
            "is_event": False,
        },
        scopes=["community.read", "user.read"],
    )

    logger.info(f"Starting analysis request: {request.session_id}")

    try:
        # Execute with timeout protection
        response = await asyncio.wait_for(
            adapter.execute_async(request),
            timeout=75.0  # Overall timeout
        )

        # Process response
        if response.success:
            logger.info(f"Analysis completed successfully")
            logger.debug(f"Response data: {response.data}")

            # Deliver results to targets
            if response.targets:
                logger.info(f"Delivering to {len(response.targets)} targets")

        else:
            logger.error(f"Analysis failed: {response.error}")

    except asyncio.TimeoutError:
        logger.error("Analysis request timed out")

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)

    finally:
        # Log final health status
        info = adapter.get_module_info()
        health = info["health"]
        logger.info(
            f"Final health - "
            f"Healthy: {health['is_healthy']}, "
            f"Requests: {health['total_requests']}, "
            f"Error rate: {health['error_rate']:.1%}"
        )


async def main():
    """Run all examples."""
    print("\n" + "=" * 60)
    print("LambdaAdapter Examples")
    print("=" * 60)

    # Run examples
    await example_1_basic_sync_invocation()
    await example_2_async_invocation()
    await example_3_with_function_prefix()
    await example_4_with_retry_configuration()
    await example_5_with_metadata()
    await example_6_health_tracking()
    await example_7_module_info()
    await example_8_error_handling()
    await example_9_concurrent_invocations()
    await example_10_complete_workflow()

    print("\n" + "=" * 60)
    print("Examples completed!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
