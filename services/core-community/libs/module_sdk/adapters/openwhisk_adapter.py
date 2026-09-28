"""
Apache OpenWhisk adapter for serverless module invocation.

This module provides an OpenWhiskAdapter class that bridges Apache OpenWhisk
actions with Waddles's internal module system. It handles HTTP communication
to the OpenWhisk API, async action invocation, response parsing, retry logic
with exponential backoff, and health tracking.
"""

import base64
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse
import asyncio

import httpx

from ..base import ExecuteRequest, ExecuteResponse
from .base_adapter import BaseAdapter


logger = logging.getLogger(__name__)


class OpenWhiskAdapter(BaseAdapter):
    """
    Adapter for Apache OpenWhisk serverless actions.

    This adapter invokes OpenWhisk actions via HTTP API, supports async
    invocation, handles response parsing, and includes retry logic with
    exponential backoff.

    Attributes:
        api_host: OpenWhisk API host (e.g., https://openwhisk.ng.bluemix.net)
        auth_key: OpenWhisk authentication key (format: username:password)
        namespace: OpenWhisk namespace for the action
        action_name: Name of the action to invoke
        timeout: Request timeout in seconds (default 10s, max 60s)
        max_retries: Maximum number of retry attempts (default 3)
        retry_backoff_factor: Exponential backoff multiplier (default 2.0)
        initial_retry_delay: Initial delay in seconds before first retry (default 0.5s)
        MODULE_NAME: Name of the module (set during initialization)
        MODULE_VERSION: Version of the module (default "1.0.0")
        REQUIRED_SCOPES: List of required permission scopes
    """

    MODULE_VERSION: str = "1.0.0"
    REQUIRED_SCOPES: list = []

    def __init__(
        self,
        api_host: str,
        auth_key: str,
        namespace: str,
        action_name: str,
        module_name: str,
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_backoff_factor: float = 2.0,
        initial_retry_delay: float = 0.5,
        module_version: str = "1.0.0",
        required_scopes: Optional[list] = None,
    ):
        """
        Initialize the OpenWhisk adapter.

        Args:
            api_host: OpenWhisk API host URL (e.g., https://openwhisk.ng.bluemix.net)
            auth_key: OpenWhisk authentication key (format: username:password)
            namespace: OpenWhisk namespace for the action
            action_name: Name of the action to invoke
            module_name: Name of the module
            timeout: Request timeout in seconds (default 10.0, max 60.0)
            max_retries: Maximum number of retry attempts (default 3)
            retry_backoff_factor: Exponential backoff multiplier (default 2.0)
            initial_retry_delay: Initial delay in seconds before first retry (default 0.5)
            module_version: Version of the module (default "1.0.0")
            required_scopes: List of required permission scopes

        Raises:
            ValueError: If configuration is invalid
        """
        super().__init__()

        # Validate API host
        parsed_url = urlparse(api_host)
        if not parsed_url.scheme or not parsed_url.netloc:
            raise ValueError(f"Invalid OpenWhisk API host: {api_host}")

        if parsed_url.scheme not in ("http", "https"):
            raise ValueError(f"API host must use http or https scheme: {api_host}")

        # Validate auth_key format (should be username:password)
        if not auth_key or ':' not in auth_key:
            raise ValueError(
                "Invalid auth_key format. Expected 'username:password' format"
            )

        # Validate namespace and action_name
        if not namespace or not namespace.strip():
            raise ValueError("Namespace cannot be empty")

        if not action_name or not action_name.strip():
            raise ValueError("Action name cannot be empty")

        # Validate timeout
        if timeout <= 0:
            raise ValueError(f"Timeout must be positive, got {timeout}")
        if timeout > 60.0:
            raise ValueError(f"Timeout cannot exceed 60 seconds, got {timeout}")

        # Validate retry configuration
        if max_retries < 0:
            raise ValueError(f"max_retries must be non-negative, got {max_retries}")

        if retry_backoff_factor <= 0:
            raise ValueError(
                f"retry_backoff_factor must be positive, got {retry_backoff_factor}"
            )

        if initial_retry_delay <= 0:
            raise ValueError(
                f"initial_retry_delay must be positive, got {initial_retry_delay}"
            )

        self.api_host = api_host.rstrip('/')
        self.auth_key = auth_key
        self.namespace = namespace.strip()
        self.action_name = action_name.strip()
        self.MODULE_NAME = module_name
        self.MODULE_VERSION = module_version
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_factor = retry_backoff_factor
        self.initial_retry_delay = initial_retry_delay

        if required_scopes:
            self.REQUIRED_SCOPES = required_scopes

        logger.info(
            f"OpenWhiskAdapter initialized for module '{module_name}' "
            f"targeting {self.api_host}/api/v1/namespaces/{self.namespace}/actions/{self.action_name} "
            f"with {timeout}s timeout and {max_retries} max retries"
        )

    def _generate_auth_header(self) -> str:
        """
        Generate the Authorization header value for OpenWhisk API.

        The auth_key is base64 encoded and prefixed with 'Basic '.

        Returns:
            The Authorization header value
        """
        encoded = base64.b64encode(self.auth_key.encode('utf-8')).decode('utf-8')
        return f"Basic {encoded}"

    def _build_openwhisk_payload(self, request: ExecuteRequest) -> Dict[str, Any]:
        """
        Build the OpenWhisk action payload from an ExecuteRequest.

        Args:
            request: The ExecuteRequest to convert

        Returns:
            Dictionary containing the action payload
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

    def _parse_openwhisk_response(
        self, response_data: Dict[str, Any]
    ) -> ExecuteResponse:
        """
        Parse OpenWhisk action response and convert to ExecuteResponse.

        Expected OpenWhisk response format (from action.response):
        {
            "body": {
                "success": true,
                "response_type": "text",
                "message": "Response message",
                "overlay_data": null,
                "browser_source_url": null,
                "targets": ["platform"]
            }
        }

        Or simplified format:
        {
            "success": true,
            "response_type": "text",
            "message": "Response message"
        }

        Args:
            response_data: The OpenWhisk response dictionary

        Returns:
            ExecuteResponse object
        """
        # Handle nested body structure from OpenWhisk
        if 'body' in response_data and isinstance(response_data['body'], dict):
            action_result = response_data['body']
        else:
            action_result = response_data

        success = action_result.get('success', False)
        message = action_result.get('message')

        # Build data dictionary from action response
        data = {}
        if 'response_type' in action_result:
            data['response_type'] = action_result['response_type']
        if 'overlay_data' in action_result:
            data['overlay_data'] = action_result['overlay_data']
        if 'browser_source_url' in action_result:
            data['browser_source_url'] = action_result['browser_source_url']
        if 'targets' in action_result:
            data['targets'] = action_result['targets']

        # Include any additional fields
        for key, value in action_result.items():
            if key not in ('success', 'message', 'response_type', 'overlay_data',
                          'browser_source_url', 'targets', 'body'):
                data[key] = value

        # Convert targets to ExecuteResponse format if present
        targets = []
        if 'targets' in action_result:
            for target in action_result['targets']:
                if isinstance(target, str):
                    targets.append({'type': target})
                elif isinstance(target, dict):
                    targets.append(target)

        return ExecuteResponse(
            success=success,
            message=message,
            data=data if data else None,
            error=None if success else action_result.get('error', 'Unknown error'),
            targets=targets,
        )

    async def _invoke_action_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Invoke OpenWhisk action with exponential backoff retry logic.

        Args:
            client: httpx.AsyncClient for making requests
            url: The OpenWhisk API endpoint URL
            headers: HTTP headers for the request
            payload: The action payload

        Returns:
            The parsed JSON response from OpenWhisk

        Raises:
            httpx.TimeoutException: If all retries timeout
            httpx.RequestError: If all retries fail with request errors
            json.JSONDecodeError: If response cannot be parsed as JSON
            Exception: For other unexpected errors
        """
        last_exception = None
        delay = self.initial_retry_delay

        for attempt in range(self.max_retries + 1):
            try:
                logger.debug(
                    f"OpenWhisk action invocation attempt {attempt + 1}/{self.max_retries + 1}"
                )

                response = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )

                # Check HTTP status
                if response.status_code not in (200, 202):
                    error_msg = (
                        f"OpenWhisk returned HTTP {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                    logger.warning(f"Attempt {attempt + 1}: {error_msg}")

                    # If it's a client error (4xx), don't retry
                    if 400 <= response.status_code < 500:
                        raise ValueError(error_msg)

                    # For server errors (5xx), continue to retry logic
                    last_exception = ValueError(error_msg)

                    if attempt < self.max_retries:
                        await asyncio.sleep(delay)
                        delay *= self.retry_backoff_factor
                        continue

                    raise last_exception

                # Parse JSON response
                try:
                    return response.json()
                except json.JSONDecodeError as e:
                    error_msg = f"Failed to parse OpenWhisk response as JSON: {str(e)}"
                    logger.error(f"Attempt {attempt + 1}: {error_msg}")
                    raise

            except httpx.TimeoutException as e:
                logger.warning(
                    f"Attempt {attempt + 1}: OpenWhisk request timed out after {self.timeout}s"
                )
                last_exception = e

                if attempt < self.max_retries:
                    await asyncio.sleep(delay)
                    delay *= self.retry_backoff_factor
                    continue

                raise

            except httpx.RequestError as e:
                logger.warning(f"Attempt {attempt + 1}: OpenWhisk request failed: {str(e)}")
                last_exception = e

                if attempt < self.max_retries:
                    await asyncio.sleep(delay)
                    delay *= self.retry_backoff_factor
                    continue

                raise

        # Should not reach here, but handle just in case
        if last_exception:
            raise last_exception
        raise RuntimeError("OpenWhisk invocation failed with unknown error")

    async def execute_async(self, request: ExecuteRequest) -> ExecuteResponse:
        """
        Execute the OpenWhisk action asynchronously.

        This method:
        1. Builds the action payload from the ExecuteRequest
        2. Constructs the OpenWhisk API endpoint URL
        3. Invokes the action via HTTP API with retry logic
        4. Parses the response and converts to ExecuteResponse
        5. Tracks health status based on success/failure

        Args:
            request: ExecuteRequest containing command, args, and context

        Returns:
            ExecuteResponse with results from the OpenWhisk action

        Raises:
            Exception: If the action invocation fails (after recording the failure)
        """
        try:
            # Build payload
            payload_dict = self._build_openwhisk_payload(request)

            # Construct API URL
            url = (
                f"{self.api_host}/api/v1/namespaces/{self.namespace}/"
                f"actions/{self.action_name}?blocking=true&result=true"
            )

            # Prepare headers
            auth_header = self._generate_auth_header()
            headers = {
                "Authorization": auth_header,
                "Content-Type": "application/json",
                "User-Agent": f"WaddleBot-OpenWhiskAdapter/{self.MODULE_VERSION}",
            }

            # Log the request
            logger.info(
                f"Invoking OpenWhisk action {self.action_name} in namespace {self.namespace} "
                f"for module '{self.MODULE_NAME}' (session: {request.session_id})"
            )

            # Make HTTP request with retry logic
            async with httpx.AsyncClient() as client:
                response_data = await self._invoke_action_with_retry(
                    client, url, headers, payload_dict
                )

            # Parse response - OpenWhisk wraps results in a response object
            execute_response = self._parse_openwhisk_response(response_data)

            # Record success/failure
            if execute_response.success:
                self.health.record_success()
                logger.info(
                    f"OpenWhisk action successful for module '{self.MODULE_NAME}' "
                    f"(session: {request.session_id})"
                )
            else:
                self.health.record_failure()
                logger.warning(
                    f"OpenWhisk action returned success=false for module '{self.MODULE_NAME}' "
                    f"(session: {request.session_id}): {execute_response.error}"
                )

            return execute_response

        except httpx.TimeoutException as e:
            error_msg = (
                f"OpenWhisk action timed out after {self.timeout}s "
                f"(retried {self.max_retries} times): {str(e)}"
            )
            logger.error(error_msg)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

        except httpx.RequestError as e:
            error_msg = f"OpenWhisk request failed: {str(e)}"
            logger.error(error_msg)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

        except ValueError as e:
            error_msg = f"OpenWhisk invocation error: {str(e)}"
            logger.error(error_msg)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

        except json.JSONDecodeError as e:
            error_msg = f"Failed to parse OpenWhisk response as JSON: {str(e)}"
            logger.error(error_msg)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

        except Exception as e:
            error_msg = f"Unexpected error during OpenWhisk action execution: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

    def get_module_info(self) -> Dict[str, Any]:
        """
        Get information about this OpenWhisk module.

        Returns:
            Dictionary containing module metadata and health status
        """
        info = super().get_module_info()
        info.update({
            "type": "openwhisk_adapter",
            "api_host": self.api_host,
            "namespace": self.namespace,
            "action_name": self.action_name,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "retry_backoff_factor": self.retry_backoff_factor,
            "initial_retry_delay": self.initial_retry_delay,
            "health": self.health.get_status(),
        })
        return info
