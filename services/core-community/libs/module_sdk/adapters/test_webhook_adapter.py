"""
Unit tests for WebhookAdapter.

This module contains tests for the WebhookAdapter class to ensure
proper functionality of webhook communication, signature generation,
health tracking, and error handling.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, Mock
from datetime import datetime

from webhook_adapter import WebhookAdapter
from ..base import ExecuteRequest, ExecuteResponse


class TestWebhookAdapter:
    """Test suite for WebhookAdapter."""

    def test_init_valid_url(self):
        """Test initialization with valid webhook URL."""
        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
        )

        assert adapter.webhook_url == "https://example.com/webhook"
        assert adapter.secret_key == "test-secret"
        assert adapter.MODULE_NAME == "test_module"
        assert adapter.timeout == 5.0
        assert adapter.is_healthy()

    def test_init_invalid_url(self):
        """Test initialization with invalid webhook URL."""
        with pytest.raises(ValueError, match="Invalid webhook URL"):
            WebhookAdapter(
                webhook_url="not-a-url",
                secret_key="test-secret",
                module_name="test_module",
            )

    def test_init_invalid_scheme(self):
        """Test initialization with invalid URL scheme."""
        with pytest.raises(ValueError, match="must use http or https"):
            WebhookAdapter(
                webhook_url="ftp://example.com/webhook",
                secret_key="test-secret",
                module_name="test_module",
            )

    def test_init_timeout_validation(self):
        """Test timeout validation."""
        # Test negative timeout
        with pytest.raises(ValueError, match="Timeout must be positive"):
            WebhookAdapter(
                webhook_url="https://example.com/webhook",
                secret_key="test-secret",
                module_name="test_module",
                timeout=-1.0,
            )

        # Test timeout exceeding max
        with pytest.raises(ValueError, match="cannot exceed 30 seconds"):
            WebhookAdapter(
                webhook_url="https://example.com/webhook",
                secret_key="test-secret",
                module_name="test_module",
                timeout=31.0,
            )

    def test_generate_signature(self):
        """Test HMAC signature generation."""
        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
        )

        payload = '{"test": "data"}'
        signature = adapter._generate_signature(payload)

        assert signature.startswith("sha256=")
        assert len(signature) > 7  # More than just the prefix

        # Same payload should generate same signature
        signature2 = adapter._generate_signature(payload)
        assert signature == signature2

    def test_build_webhook_payload_command(self):
        """Test building webhook payload for command trigger."""
        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
        )

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
                    "name": "TestCommunity",
                    "is_subscribed": True,
                    "subscription_order_id": "ord_123",
                    "seat_count": 10,
                },
                "user": {
                    "username": "testuser",
                    "platform_user_id": "12345",
                },
                "entity": {
                    "platform_entity_id": "channel_123",
                },
            },
        )

        payload = adapter._build_webhook_payload(request)

        assert payload["community"]["id"] == "789"
        assert payload["community"]["name"] == "TestCommunity"
        assert payload["community"]["is_subscribed"] is True
        assert payload["trigger"]["type"] == "command"
        assert payload["trigger"]["command"] == "#weather"
        assert payload["trigger"]["context_text"] == "London UK"
        assert payload["user"]["id"] == "user_123"
        assert payload["user"]["username"] == "testuser"
        assert payload["entity"]["id"] == "entity_456"
        assert payload["request_id"] == "session_abc"

    def test_build_webhook_payload_event(self):
        """Test building webhook payload for event trigger."""
        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
        )

        request = ExecuteRequest(
            command="",
            args=[],
            user_id="user_123",
            entity_id="entity_456",
            community_id="789",
            session_id="session_abc",
            platform="twitch",
            metadata={
                "is_event": True,
                "event_type": "twitch.subscription",
                "event_data": {"tier": "1000"},
                "community": {"name": "TestCommunity"},
                "user": {"username": "testuser"},
                "entity": {},
            },
        )

        payload = adapter._build_webhook_payload(request)

        assert payload["trigger"]["type"] == "event"
        assert payload["trigger"]["command"] is None
        assert payload["trigger"]["context_text"] is None
        assert payload["trigger"]["event_type"] == "twitch.subscription"
        assert payload["trigger"]["event_data"]["tier"] == "1000"

    def test_parse_webhook_response_success(self):
        """Test parsing successful webhook response."""
        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
        )

        webhook_response = {
            "success": True,
            "response_type": "text",
            "message": "Command executed successfully",
            "targets": ["platform"],
        }

        response = adapter._parse_webhook_response(webhook_response)

        assert response.success is True
        assert response.message == "Command executed successfully"
        assert response.data["response_type"] == "text"
        assert len(response.targets) == 1
        assert response.targets[0]["type"] == "platform"

    def test_parse_webhook_response_failure(self):
        """Test parsing failed webhook response."""
        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
        )

        webhook_response = {
            "success": False,
            "message": "Command failed",
            "error": "Invalid parameters",
        }

        response = adapter._parse_webhook_response(webhook_response)

        assert response.success is False
        assert response.message == "Command failed"
        assert response.error == "Invalid parameters"

    @pytest.mark.asyncio
    async def test_execute_async_success(self):
        """Test successful webhook execution."""
        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
        )

        request = ExecuteRequest(
            command="#test",
            args=["arg1"],
            user_id="user_123",
            entity_id="entity_456",
            community_id="789",
            session_id="session_abc",
            platform="twitch",
            metadata={"community": {}, "user": {}, "entity": {}},
        )

        # Mock httpx client
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "message": "Test successful",
            "targets": ["platform"],
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            response = await adapter.execute_async(request)

            assert response.success is True
            assert response.message == "Test successful"
            assert adapter.is_healthy()
            assert adapter.health.total_requests == 1
            assert adapter.health.total_failures == 0

    @pytest.mark.asyncio
    async def test_execute_async_http_error(self):
        """Test webhook execution with HTTP error."""
        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
        )

        request = ExecuteRequest(
            command="#test",
            args=[],
            user_id="user_123",
            entity_id="entity_456",
            community_id="789",
            session_id="session_abc",
            platform="twitch",
            metadata={"community": {}, "user": {}, "entity": {}},
        )

        # Mock httpx client with error response
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            response = await adapter.execute_async(request)

            assert response.success is False
            assert "HTTP 500" in response.error
            assert adapter.health.total_failures == 1

    @pytest.mark.asyncio
    async def test_execute_async_timeout(self):
        """Test webhook execution with timeout."""
        import httpx

        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
            timeout=1.0,
        )

        request = ExecuteRequest(
            command="#test",
            args=[],
            user_id="user_123",
            entity_id="entity_456",
            community_id="789",
            session_id="session_abc",
            platform="twitch",
            metadata={"community": {}, "user": {}, "entity": {}},
        )

        # Mock timeout exception
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post.side_effect = httpx.TimeoutException("Timeout")
            mock_client_class.return_value = mock_client

            response = await adapter.execute_async(request)

            assert response.success is False
            assert "timed out" in response.error
            assert adapter.health.total_failures == 1

    def test_health_tracking(self):
        """Test health status tracking across multiple requests."""
        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
        )

        # Start healthy
        assert adapter.is_healthy()

        # Record one failure - should still be healthy
        adapter.health.record_failure()
        assert adapter.is_healthy()
        assert adapter.health.consecutive_failures == 1

        # Record two more failures - should be unhealthy
        adapter.health.record_failure()
        adapter.health.record_failure()
        assert not adapter.is_healthy()
        assert adapter.health.consecutive_failures == 3

        # Record success - should be healthy again
        adapter.health.record_success()
        assert adapter.is_healthy()
        assert adapter.health.consecutive_failures == 0

    def test_get_module_info(self):
        """Test getting module information."""
        adapter = WebhookAdapter(
            webhook_url="https://example.com/webhook",
            secret_key="test-secret",
            module_name="test_module",
            module_version="2.0.0",
            timeout=10.0,
        )

        info = adapter.get_module_info()

        assert info["name"] == "test_module"
        assert info["version"] == "2.0.0"
        assert info["type"] == "webhook_adapter"
        assert info["webhook_url"] == "https://example.com/webhook"
        assert info["timeout"] == 10.0
        assert "health" in info
