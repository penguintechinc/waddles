"""
Example usage of WebhookAdapter for external marketplace modules.

This file demonstrates how to create and use a WebhookAdapter to integrate
external webhook-based modules with Waddles.
"""

import asyncio
from webhook_adapter import WebhookAdapter
from ..base import ExecuteRequest


async def main():
    """Example usage of WebhookAdapter."""

    # Create a webhook adapter for an external module
    adapter = WebhookAdapter(
        webhook_url="https://your-webhook-endpoint.com/webhook",
        secret_key="your-secret-key-here",
        module_name="weather_module",
        timeout=5.0,  # 5 second timeout
        module_version="1.0.0",
        required_scopes=["community.read", "user.read"],
    )

    # Create a sample request
    request = ExecuteRequest(
        command="#weather",
        args=["London", "UK"],
        user_id="user_123",
        entity_id="entity_456",
        community_id="789",
        session_id="session_abc123",
        platform="twitch",
        metadata={
            "community": {
                "name": "AwesomeStreamers",
                "is_subscribed": True,
                "subscription_order_id": "ord_abc123",
                "seat_count": 45,
            },
            "user": {
                "username": "CoolViewer",
                "platform_user_id": "12345678",
            },
            "entity": {
                "platform_entity_id": "channel123",
            },
        },
        scopes=["community.read", "user.read"],
    )

    # Execute the webhook
    response = await adapter.execute_async(request)

    # Check the response
    print(f"Success: {response.success}")
    print(f"Message: {response.message}")
    print(f"Data: {response.data}")
    print(f"Error: {response.error}")

    # Check health status
    health = adapter.get_health_status()
    print(f"\nHealth Status: {health}")

    # Get module info
    info = adapter.get_module_info()
    print(f"\nModule Info: {info}")


if __name__ == "__main__":
    asyncio.run(main())
