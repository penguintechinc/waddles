"""
GCP Cloud Functions adapter for external module integration.

This module provides a GCPAdapter class that bridges GCP Cloud Functions
with Waddles's internal module system. It handles async invocation,
response parsing, retry logic with exponential backoff, and health tracking.
"""

import asyncio
import json
import logging
from typing import Any, Dict, Optional
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx
from google.cloud import functions_v1
from google.auth.credentials import Credentials

from ..base import ExecuteRequest, ExecuteResponse
from .base_adapter import BaseAdapter


logger = logging.getLogger(__name__)


class GCPAdapter(BaseAdapter):
    """
    Adapter for GCP Cloud Functions-based external modules.

    This adapter can invoke GCP Cloud Functions either via:
    1. HTTP Cloud Function URLs (using httpx)
    2. Google Cloud Functions client (for authenticated access)

    Features include:
    - Async invocation with httpx
    - Response parsing and validation
    - Retry logic with exponential backoff
    - Health tracking and status monitoring
    - Support for both function URLs and function names
    - Configurable timeouts and retry parameters

    Attributes:
        function_url: The HTTP endpoint of the Cloud Function (if using HTTP)
        function_name: The name of the Cloud Function (if using client)
        project_id: GCP project ID
        region: GCP region where the function is deployed
        credentials: GCP credentials for authentication
        timeout: Request timeout in seconds (default 5s, max 30s)
        max_retries: Maximum number of retry attempts (default 3)
        backoff_factor: Exponential backoff multiplier (default 1.5)
        MODULE_NAME: Name of the module (set during initialization)
        MODULE_VERSION: Version of the module (default "1.0.0")
        REQUIRED_SCOPES: List of required permission scopes
    """

    MODULE_VERSION: str = "1.0.0"
    REQUIRED_SCOPES: list = []

    def __init__(
        self,
        module_name: str,
        function_url: Optional[str] = None,
        function_name: Optional[str] = None,
        project_id: Optional[str] = None,
        region: str = "us-central1",
        credentials: Optional[Credentials] = None,
        timeout: float = 5.0,
        max_retries: int = 3,
        backoff_factor: float = 1.5,
        module_version: str = "1.0.0",
        required_scopes: Optional[list] = None,
    ):
        """
        Initialize the GCP adapter.

        Must provide either function_url (for HTTP) or function_name (for client).
        If function_name is provided without credentials, Application Default
        Credentials will be used.

        Args:
            module_name: Name of the module
            function_url: HTTP endpoint of the Cloud Function
            function_name: Name of the Cloud Function (e.g., 'my-function')
            project_id: GCP project ID (required if using function_name)
            region: GCP region where the function is deployed (default: 'us-central1')
            credentials: GCP credentials for authentication (optional)
            timeout: Request timeout in seconds (default 5.0, max 30.0)
            max_retries: Maximum number of retry attempts (default 3)
            backoff_factor: Exponential backoff multiplier (default 1.5)
            module_version: Version of the module (default "1.0.0")
            required_scopes: List of required permission scopes

        Raises:
            ValueError: If configuration is invalid
            TypeError: If credentials are invalid
        """
        super().__init__()

        # Validate that at least one invocation method is provided
        if not function_url and not function_name:
            raise ValueError(
                "Must provide either 'function_url' (for HTTP) or "
                "'function_name' (for client-based invocation)"
            )

        # Validate function_url if provided
        if function_url:
            parsed_url = urlparse(function_url)
            if not parsed_url.scheme or not parsed_url.netloc:
                raise ValueError(f"Invalid function URL: {function_url}")
            if parsed_url.scheme not in ("http", "https"):
                raise ValueError(
                    f"Function URL must use http or https scheme: {function_url}"
                )
            self.function_url = function_url
        else:
            self.function_url = None

        # Validate function_name if provided
        if function_name:
            if not project_id:
                raise ValueError(
                    "project_id is required when using function_name"
                )
            self.function_name = function_name
            self.project_id = project_id
            self.region = region
            self.credentials = credentials
        else:
            self.function_name = None
            self.project_id = None
            self.region = region
            self.credentials = None

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

        # Validate retry configuration
        if max_retries < 0:
            raise ValueError(f"max_retries must be non-negative, got {max_retries}")
        if backoff_factor <= 1.0:
            raise ValueError(
                f"backoff_factor must be > 1.0, got {backoff_factor}"
            )

        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

        # Initialize GCP client if using function_name
        self._gcp_client = None
        if self.function_name:
            try:
                self._gcp_client = functions_v1.CloudFunctionsServiceClient(
                    credentials=self.credentials
                )
                logger.info(
                    f"GCPAdapter initialized for module '{module_name}' "
                    f"using Cloud Functions client ({self.function_name} "
                    f"in {self.project_id}/{self.region})"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to initialize GCP client: {str(e)}. "
                    "Will fall back to HTTP invocation if URL is available."
                )
                self._gcp_client = None
        else:
            logger.info(
                f"GCPAdapter initialized for module '{module_name}' "
                f"using HTTP endpoint: {self.function_url} "
                f"with {timeout}s timeout and {max_retries} max retries"
            )

    def _build_gcp_request_payload(self, request: ExecuteRequest) -> Dict[str, Any]:
        """
        Build the request payload for GCP Cloud Function invocation.

        The payload follows the format specified in .PLAN-v2:
        - community: id, name, is_subscribed, subscription_order_id, seat_count
        - trigger: type, command, context_text, event_type, event_data
        - user: id, username, platform, platform_user_id
        - entity: id, platform, platform_entity_id
        - request_id, timestamp

        Args:
            request: The ExecuteRequest to convert

        Returns:
            Dictionary containing the function request payload
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

    def _parse_gcp_response(self, response_data: Dict[str, Any]) -> ExecuteResponse:
        """
        Parse GCP function response and convert to ExecuteResponse.

        Expected response format:
        {
            "success": true,
            "response_type": "text",
            "message": "Response message",
            "overlay_data": null,
            "browser_source_url": null,
            "targets": ["platform"]
        }

        Args:
            response_data: The GCP function response dictionary

        Returns:
            ExecuteResponse object
        """
        success = response_data.get('success', False)
        message = response_data.get('message')

        # Build data dictionary from response
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

    async def _invoke_http(
        self,
        request: ExecuteRequest,
        retry_count: int = 0,
    ) -> ExecuteResponse:
        """
        Invoke the Cloud Function via HTTP with retry logic.

        Uses exponential backoff for retries: wait = backoff_factor ^ retry_count

        Args:
            request: The ExecuteRequest to send
            retry_count: Current retry attempt (used internally)

        Returns:
            ExecuteResponse with results from the Cloud Function
        """
        try:
            # Build payload
            payload_dict = self._build_gcp_request_payload(request)
            payload_json = json.dumps(payload_dict)

            # Prepare headers
            headers = {
                "Content-Type": "application/json",
                "User-Agent": f"WaddleBot-GCPAdapter/{self.MODULE_VERSION}",
            }

            # Log the request
            logger.info(
                f"Sending HTTP request to GCP function {self.function_url} "
                f"for module '{self.MODULE_NAME}' "
                f"(command: {request.command}, session: {request.session_id})"
            )

            # Make HTTP request
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.function_url,
                    content=payload_json,
                    headers=headers,
                    timeout=self.timeout,
                )

            # Check HTTP status
            if response.status_code != 200:
                error_msg = (
                    f"GCP function returned HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
                logger.error(error_msg)

                # Retry on server errors (5xx)
                if 500 <= response.status_code < 600 and retry_count < self.max_retries:
                    wait_time = self.backoff_factor ** retry_count
                    logger.info(
                        f"Retrying after {wait_time}s "
                        f"(attempt {retry_count + 1}/{self.max_retries})"
                    )
                    await asyncio.sleep(wait_time)
                    return await self._invoke_http(request, retry_count + 1)

                self.health.record_failure()
                return ExecuteResponse(
                    success=False,
                    error=error_msg,
                )

            # Parse JSON response
            try:
                response_data = response.json()
            except json.JSONDecodeError as e:
                error_msg = f"Failed to parse GCP response as JSON: {str(e)}"
                logger.error(error_msg)
                self.health.record_failure()

                return ExecuteResponse(
                    success=False,
                    error=error_msg,
                )

            # Convert to ExecuteResponse
            execute_response = self._parse_gcp_response(response_data)

            # Record success/failure
            if execute_response.success:
                self.health.record_success()
                logger.info(
                    f"GCP function request successful for module '{self.MODULE_NAME}' "
                    f"(session: {request.session_id})"
                )
            else:
                self.health.record_failure()
                logger.warning(
                    f"GCP function returned success=false for module '{self.MODULE_NAME}' "
                    f"(session: {request.session_id}): {execute_response.error}"
                )

            return execute_response

        except httpx.TimeoutException as e:
            # Retry on timeout
            if retry_count < self.max_retries:
                wait_time = self.backoff_factor ** retry_count
                logger.info(
                    f"Request timed out, retrying after {wait_time}s "
                    f"(attempt {retry_count + 1}/{self.max_retries})"
                )
                await asyncio.sleep(wait_time)
                return await self._invoke_http(request, retry_count + 1)

            error_msg = f"GCP function request timed out after {self.timeout}s: {str(e)}"
            logger.error(error_msg)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

        except httpx.RequestError as e:
            # Retry on connection errors
            if retry_count < self.max_retries:
                wait_time = self.backoff_factor ** retry_count
                logger.info(
                    f"Request failed, retrying after {wait_time}s "
                    f"(attempt {retry_count + 1}/{self.max_retries})"
                )
                await asyncio.sleep(wait_time)
                return await self._invoke_http(request, retry_count + 1)

            error_msg = f"GCP function request failed: {str(e)}"
            logger.error(error_msg)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

        except Exception as e:
            error_msg = f"Unexpected error during GCP HTTP invocation: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

    async def _invoke_client(
        self,
        request: ExecuteRequest,
        retry_count: int = 0,
    ) -> ExecuteResponse:
        """
        Invoke the Cloud Function via Google Cloud Functions client.

        Uses exponential backoff for retries: wait = backoff_factor ^ retry_count

        Args:
            request: The ExecuteRequest to send
            retry_count: Current retry attempt (used internally)

        Returns:
            ExecuteResponse with results from the Cloud Function
        """
        if not self._gcp_client:
            error_msg = "GCP client not initialized. Use HTTP invocation instead."
            logger.error(error_msg)
            self.health.record_failure()
            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

        try:
            # Build payload
            payload_dict = self._build_gcp_request_payload(request)
            payload_json = json.dumps(payload_dict)

            # Build function resource name
            function_resource = (
                f"projects/{self.project_id}/locations/{self.region}/"
                f"functions/{self.function_name}"
            )

            logger.info(
                f"Invoking GCP function {function_resource} "
                f"for module '{self.MODULE_NAME}' "
                f"(command: {request.command}, session: {request.session_id})"
            )

            # Make async call to GCP (wrapped in executor to avoid blocking)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gcp_client.call_function(
                    name=function_resource,
                    data=payload_json,
                ),
            )

            # Parse response
            try:
                response_data = json.loads(response.result)
            except (json.JSONDecodeError, AttributeError) as e:
                error_msg = f"Failed to parse GCP response as JSON: {str(e)}"
                logger.error(error_msg)
                self.health.record_failure()

                return ExecuteResponse(
                    success=False,
                    error=error_msg,
                )

            # Convert to ExecuteResponse
            execute_response = self._parse_gcp_response(response_data)

            # Record success/failure
            if execute_response.success:
                self.health.record_success()
                logger.info(
                    f"GCP function invocation successful for module '{self.MODULE_NAME}' "
                    f"(session: {request.session_id})"
                )
            else:
                self.health.record_failure()
                logger.warning(
                    f"GCP function returned success=false for module '{self.MODULE_NAME}' "
                    f"(session: {request.session_id}): {execute_response.error}"
                )

            return execute_response

        except Exception as e:
            # Retry on any error with exponential backoff
            if retry_count < self.max_retries:
                wait_time = self.backoff_factor ** retry_count
                logger.info(
                    f"GCP invocation failed, retrying after {wait_time}s "
                    f"(attempt {retry_count + 1}/{self.max_retries}): {str(e)}"
                )
                await asyncio.sleep(wait_time)
                return await self._invoke_client(request, retry_count + 1)

            error_msg = f"GCP function invocation failed: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

    async def execute_async(self, request: ExecuteRequest) -> ExecuteResponse:
        """
        Execute the GCP Cloud Function asynchronously.

        This method:
        1. Builds the request payload from the ExecuteRequest
        2. Invokes the Cloud Function (via HTTP or client)
        3. Parses the response and converts to ExecuteResponse
        4. Tracks health status based on success/failure
        5. Implements retry logic with exponential backoff

        Uses HTTP invocation if function_url is available, otherwise
        falls back to client invocation if function_name is configured.

        Args:
            request: ExecuteRequest containing command, args, and context

        Returns:
            ExecuteResponse with results from the Cloud Function

        Raises:
            Exception: If invocation fails (after recording the failure)
        """
        # Use HTTP invocation if available, otherwise use client
        if self.function_url:
            return await self._invoke_http(request)
        elif self.function_name:
            return await self._invoke_client(request)
        else:
            error_msg = (
                "No invocation method available. "
                "Provide either function_url or function_name."
            )
            logger.error(error_msg)
            self.health.record_failure()
            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

    def get_module_info(self) -> Dict[str, Any]:
        """
        Get information about this GCP adapter module.

        Returns:
            Dictionary containing module metadata and health status
        """
        info = super().get_module_info()

        invocation_info = {}
        if self.function_url:
            invocation_info["type"] = "http"
            invocation_info["function_url"] = self.function_url
        elif self.function_name:
            invocation_info["type"] = "client"
            invocation_info["function_name"] = self.function_name
            invocation_info["project_id"] = self.project_id
            invocation_info["region"] = self.region

        info.update({
            "type": "gcp_adapter",
            "invocation": invocation_info,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "backoff_factor": self.backoff_factor,
            "health": self.health.get_status(),
        })
        return info
