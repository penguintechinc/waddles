"""
Webhook adapter for external marketplace modules.

This module provides a WebhookAdapter class that bridges external webhook-based
modules with Waddles's internal module system. It handles HTTP communication,
HMAC signature generation, timeout management, and health tracking.
"""

import hmac
import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from ..base import ExecuteRequest, ExecuteResponse
from .base_adapter import BaseAdapter


logger = logging.getLogger(__name__)


class WebhookAdapter(BaseAdapter):
    """
    Adapter for webhook-based external modules.

    This adapter sends HTTP POST requests to external webhook endpoints,
    includes HMAC-SHA256 signatures for security, handles timeouts,
    and tracks health status based on recent failures.

    Attributes:
        webhook_url: The URL of the external webhook endpoint
        secret_key: Secret key for HMAC signature generation
        timeout: Request timeout in seconds (default 5s, max 30s)
        MODULE_NAME: Name of the module (set during initialization)
        MODULE_VERSION: Version of the module (default "1.0.0")
        REQUIRED_SCOPES: List of required permission scopes
    """

    MODULE_VERSION: str = "1.0.0"
    REQUIRED_SCOPES: list = []

    def __init__(
        self,
        webhook_url: str,
        secret_key: str,
        module_name: str,
        timeout: float = 5.0,
        module_version: str = "1.0.0",
        required_scopes: Optional[list] = None,
    ):
        """
        Initialize the webhook adapter.

        Args:
            webhook_url: The URL of the external webhook endpoint
            secret_key: Secret key for HMAC signature generation
            module_name: Name of the module
            timeout: Request timeout in seconds (default 5.0, max 30.0)
            module_version: Version of the module (default "1.0.0")
            required_scopes: List of required permission scopes

        Raises:
            ValueError: If webhook_url is invalid or timeout is out of range
        """
        super().__init__()

        # Validate webhook URL
        parsed_url = urlparse(webhook_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            raise ValueError(f"Invalid webhook URL: {webhook_url}")

        if parsed_url.scheme not in ("http", "https"):
            raise ValueError(f"Webhook URL must use http or https scheme: {webhook_url}")

        self.webhook_url = webhook_url
        self.secret_key = secret_key
        self.MODULE_NAME = module_name
        self.MODULE_VERSION = module_version

        if required_scopes:
            self.REQUIRED_SCOPES = required_scopes

        # Validate and set timeout (default 5s, max 30s)
        if timeout <= 0:
            raise ValueError(f"Timeout must be positive, got {timeout}")
        if timeout > 30.0:
            raise ValueError(f"Timeout cannot exceed 30 seconds, got {timeout}")

        self.timeout = timeout

        logger.info(
            f"WebhookAdapter initialized for module '{module_name}' "
            f"pointing to {webhook_url} with {timeout}s timeout"
        )

    def _generate_signature(self, payload: str) -> str:
        """
        Generate HMAC-SHA256 signature for the payload.

        Args:
            payload: The JSON payload string to sign

        Returns:
            The signature as a hex string prefixed with 'sha256='
        """
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        return f"sha256={signature}"

    def _build_webhook_payload(self, request: ExecuteRequest) -> Dict[str, Any]:
        """
        Build the webhook payload from an ExecuteRequest.

        The payload follows the format specified in .PLAN-v2:
        - community: id, name, is_subscribed, subscription_order_id, seat_count
        - trigger: type, command, context_text, event_type, event_data
        - user: id, username, platform, platform_user_id
        - entity: id, platform, platform_entity_id
        - request_id, timestamp

        Args:
            request: The ExecuteRequest to convert

        Returns:
            Dictionary containing the webhook payload
        """
        # Extract community information from metadata
        community_meta = request.metadata.get('community', {})

        # Determine trigger type and build trigger data
        is_event = request.metadata.get('is_event', False)
        trigger_type = "event" if is_event else "command"

        trigger = {
            "type": trigger_type,
            "command": request.command if not is_event else None,
            "context_text": ' '.join(request.args) if request.args and not is_event else None,
            "event_type": request.metadata.get('event_type') if is_event else None,
            "event_data": request.metadata.get('event_data') if is_event else None,
        }

        # Extract user information
        user_meta = request.metadata.get('user', {})
        user = {
            "id": request.user_id,
            "username": user_meta.get('username', 'unknown'),
            "platform": request.platform,
            "platform_user_id": user_meta.get('platform_user_id', request.user_id),
        }

        # Extract entity information
        entity_meta = request.metadata.get('entity', {})
        entity = {
            "id": request.entity_id,
            "platform": request.platform,
            "platform_entity_id": entity_meta.get('platform_entity_id', request.entity_id),
        }

        # Build community object
        community = {
            "id": request.community_id,
            "name": community_meta.get('name', 'Unknown Community'),
            "is_subscribed": community_meta.get('is_subscribed', True),
            "subscription_order_id": community_meta.get('subscription_order_id'),
            "seat_count": community_meta.get('seat_count', 0),
        }

        # Build complete payload
        payload = {
            "community": community,
            "trigger": trigger,
            "user": user,
            "entity": entity,
            "request_id": request.session_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

        return payload

    def _parse_webhook_response(self, response_data: Dict[str, Any]) -> ExecuteResponse:
        """
        Parse webhook response and convert to ExecuteResponse.

        Expected webhook response format:
        {
            "success": true,
            "response_type": "text",
            "message": "Response message",
            "overlay_data": null,
            "browser_source_url": null,
            "targets": ["platform"]
        }

        Args:
            response_data: The webhook response dictionary

        Returns:
            ExecuteResponse object
        """
        success = response_data.get('success', False)
        message = response_data.get('message')

        # Build data dictionary from webhook response
        data = {}
        if 'response_type' in response_data:
            data['response_type'] = response_data['response_type']
        if 'overlay_data' in response_data:
            data['overlay_data'] = response_data['overlay_data']
        if 'browser_source_url' in response_data:
            data['browser_source_url'] = response_data['browser_source_url']
        if 'targets' in response_data:
            data['targets'] = response_data['targets']

        # Include any additional fields
        for key, value in response_data.items():
            if key not in ('success', 'message', 'response_type', 'overlay_data',
                          'browser_source_url', 'targets'):
                data[key] = value

        # Convert targets to ExecuteResponse format if present
        targets = []
        if 'targets' in response_data:
            for target in response_data['targets']:
                if isinstance(target, str):
                    targets.append({'type': target})
                elif isinstance(target, dict):
                    targets.append(target)

        return ExecuteResponse(
            success=success,
            message=message,
            data=data if data else None,
            error=None if success else response_data.get('error', 'Unknown error'),
            targets=targets,
        )

    async def execute_async(self, request: ExecuteRequest) -> ExecuteResponse:
        """
        Execute the webhook module asynchronously.

        This method:
        1. Builds the webhook payload from the ExecuteRequest
        2. Generates an HMAC-SHA256 signature
        3. Sends an HTTP POST request to the webhook URL
        4. Parses the response and converts to ExecuteResponse
        5. Tracks health status based on success/failure

        Args:
            request: ExecuteRequest containing command, args, and context

        Returns:
            ExecuteResponse with results from the webhook

        Raises:
            Exception: If the webhook request fails (after recording the failure)
        """
        try:
            # Build payload
            payload_dict = self._build_webhook_payload(request)
            payload_json = json.dumps(payload_dict)

            # Generate signature
            signature = self._generate_signature(payload_json)

            # Prepare headers
            headers = {
                "Content-Type": "application/json",
                "X-WaddleBot-Signature": signature,
                "User-Agent": f"WaddleBot-WebhookAdapter/{self.MODULE_VERSION}",
            }

            # Log the request
            logger.info(
                f"Sending webhook request to {self.webhook_url} for module '{self.MODULE_NAME}' "
                f"(command: {request.command}, session: {request.session_id})"
            )

            # Make HTTP request
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.webhook_url,
                    content=payload_json,
                    headers=headers,
                    timeout=self.timeout,
                )

            # Check HTTP status
            if response.status_code != 200:
                error_msg = (
                    f"Webhook returned HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
                logger.error(error_msg)
                self.health.record_failure()

                return ExecuteResponse(
                    success=False,
                    error=error_msg,
                )

            # Parse JSON response
            try:
                response_data = response.json()
            except json.JSONDecodeError as e:
                error_msg = f"Failed to parse webhook response as JSON: {str(e)}"
                logger.error(error_msg)
                self.health.record_failure()

                return ExecuteResponse(
                    success=False,
                    error=error_msg,
                )

            # Convert to ExecuteResponse
            execute_response = self._parse_webhook_response(response_data)

            # Record success/failure
            if execute_response.success:
                self.health.record_success()
                logger.info(
                    f"Webhook request successful for module '{self.MODULE_NAME}' "
                    f"(session: {request.session_id})"
                )
            else:
                self.health.record_failure()
                logger.warning(
                    f"Webhook returned success=false for module '{self.MODULE_NAME}' "
                    f"(session: {request.session_id}): {execute_response.error}"
                )

            return execute_response

        except httpx.TimeoutException as e:
            error_msg = f"Webhook request timed out after {self.timeout}s: {str(e)}"
            logger.error(error_msg)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

        except httpx.RequestError as e:
            error_msg = f"Webhook request failed: {str(e)}"
            logger.error(error_msg)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

        except Exception as e:
            error_msg = f"Unexpected error during webhook execution: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

    def get_module_info(self) -> Dict[str, Any]:
        """
        Get information about this webhook module.

        Returns:
            Dictionary containing module metadata and health status
        """
        info = super().get_module_info()
        info.update({
            "type": "webhook_adapter",
            "webhook_url": self.webhook_url,
            "timeout": self.timeout,
            "health": self.health.get_status(),
        })
        return info
