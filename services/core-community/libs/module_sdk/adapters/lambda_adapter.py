"""
AWS Lambda adapter for external serverless module invocation.

This module provides a LambdaAdapter class that bridges AWS Lambda functions
with Waddles's internal module system. It handles Lambda invocation,
response parsing, error handling, and health tracking.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Union

try:
    import boto3
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        ConnectionError,
        EndpointConnectionError,
        HTTPClientError,
        ReadTimeoutError,
    )
except ImportError:
    boto3 = None
    BotoCoreError = None
    ClientError = None
    ConnectionError = None
    EndpointConnectionError = None
    HTTPClientError = None
    ReadTimeoutError = None

from ..base import ExecuteRequest, ExecuteResponse
from .base_adapter import BaseAdapter


logger = logging.getLogger(__name__)


class LambdaAdapter(BaseAdapter):
    """
    Adapter for AWS Lambda-based external modules.

    This adapter invokes AWS Lambda functions, handles both synchronous
    and asynchronous invocation modes, parses responses, and tracks
    health status based on invocation results.

    Attributes:
        function_identifier: The Lambda function name or ARN
        function_prefix: Prefix to prepend to function names
        region: AWS region where the Lambda function is located
        invocation_type: Type of invocation (RequestResponse or Event)
        max_retries: Maximum number of retry attempts
        initial_retry_delay: Initial delay for exponential backoff (seconds)
        max_retry_delay: Maximum delay for exponential backoff (seconds)
        connect_timeout: Connection timeout in seconds
        read_timeout: Read timeout in seconds
        MODULE_NAME: Name of the module (set during initialization)
        MODULE_VERSION: Version of the module (default "1.0.0")
        REQUIRED_SCOPES: List of required permission scopes
    """

    MODULE_VERSION: str = "1.0.0"
    REQUIRED_SCOPES: list = []

    # Retryable error codes for exponential backoff
    RETRYABLE_ERRORS = {
        'TooManyRequestsException',
        'ServiceUnavailableException',
        'ThrottlingException',
        'RequestLimitExceeded',
        'ConnectionError',
    }

    def __init__(
        self,
        function_identifier: str,
        region: str,
        module_name: str,
        function_prefix: Optional[str] = None,
        invocation_type: str = "RequestResponse",
        max_retries: int = 3,
        initial_retry_delay: float = 0.5,
        max_retry_delay: float = 30.0,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        module_version: str = "1.0.0",
        required_scopes: Optional[list] = None,
    ):
        """
        Initialize the Lambda adapter.

        Args:
            function_identifier: Lambda function name or ARN
            region: AWS region (e.g., 'us-east-1')
            module_name: Name of the module
            function_prefix: Optional prefix to prepend to function names
            invocation_type: 'RequestResponse' (sync) or 'Event' (async)
            max_retries: Maximum number of retry attempts (default 3)
            initial_retry_delay: Initial retry delay in seconds (default 0.5)
            max_retry_delay: Maximum retry delay in seconds (default 30.0)
            connect_timeout: Connection timeout in seconds (default 10.0)
            read_timeout: Read timeout in seconds (default 30.0)
            aws_access_key_id: AWS access key ID (uses env var if not provided)
            aws_secret_access_key: AWS secret access key (uses env var if not provided)
            module_version: Version of the module (default "1.0.0")
            required_scopes: List of required permission scopes

        Raises:
            ImportError: If boto3 is not installed
            ValueError: If function_identifier, region, or module_name is invalid
        """
        if boto3 is None:
            raise ImportError(
                "boto3 is required for LambdaAdapter. "
                "Install it with: pip install boto3"
            )

        super().__init__()

        # Validate inputs
        if not function_identifier or not function_identifier.strip():
            raise ValueError("function_identifier cannot be empty")

        if not region or not region.strip():
            raise ValueError("region cannot be empty")

        if not module_name or not module_name.strip():
            raise ValueError("module_name cannot be empty")

        if invocation_type not in ("RequestResponse", "Event"):
            raise ValueError(
                f"invocation_type must be 'RequestResponse' or 'Event', "
                f"got {invocation_type}"
            )

        if max_retries < 0:
            raise ValueError(f"max_retries must be non-negative, got {max_retries}")

        if initial_retry_delay <= 0:
            raise ValueError(f"initial_retry_delay must be positive, got {initial_retry_delay}")

        if max_retry_delay <= 0:
            raise ValueError(f"max_retry_delay must be positive, got {max_retry_delay}")

        if connect_timeout <= 0:
            raise ValueError(f"connect_timeout must be positive, got {connect_timeout}")

        if read_timeout <= 0:
            raise ValueError(f"read_timeout must be positive, got {read_timeout}")

        # Store configuration
        self.function_identifier = function_identifier
        self.function_prefix = function_prefix
        self.region = region
        self.invocation_type = invocation_type
        self.max_retries = max_retries
        self.initial_retry_delay = initial_retry_delay
        self.max_retry_delay = max_retry_delay
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.MODULE_NAME = module_name
        self.MODULE_VERSION = module_version

        if required_scopes:
            self.REQUIRED_SCOPES = required_scopes

        # Initialize boto3 client
        try:
            self.lambda_client = boto3.client(
                'lambda',
                region_name=region,
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                connect_timeout=int(connect_timeout),
                read_timeout=int(read_timeout),
            )
        except Exception as e:
            raise ValueError(f"Failed to initialize AWS Lambda client: {str(e)}")

        logger.info(
            f"LambdaAdapter initialized for module '{module_name}' "
            f"with function '{self._get_qualified_function_name()}' "
            f"in region '{region}' "
            f"(invocation_type: {invocation_type}, max_retries: {max_retries})"
        )

    def _get_qualified_function_name(self) -> str:
        """
        Get the fully qualified function name, prepending prefix if provided.

        Returns:
            The qualified function name or ARN
        """
        if self.function_prefix:
            return f"{self.function_prefix}{self.function_identifier}"
        return self.function_identifier

    def _build_lambda_payload(self, request: ExecuteRequest) -> Dict[str, Any]:
        """
        Build the Lambda invocation payload from an ExecuteRequest.

        The payload follows the format specified in .PLAN-v2:
        - community: id, name, is_subscribed, subscription_order_id, seat_count
        - trigger: type, command, context_text, event_type, event_data
        - user: id, username, platform, platform_user_id
        - entity: id, platform, platform_entity_id
        - request_id, timestamp

        Args:
            request: The ExecuteRequest to convert

        Returns:
            Dictionary containing the Lambda payload
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

    def _parse_lambda_response(
        self,
        response_data: Union[Dict[str, Any], str],
    ) -> ExecuteResponse:
        """
        Parse Lambda response and convert to ExecuteResponse.

        Expected Lambda response format:
        {
            "success": true,
            "response_type": "text",
            "message": "Response message",
            "overlay_data": null,
            "browser_source_url": null,
            "targets": ["platform"]
        }

        If the Lambda response is a string, it's parsed as JSON first.

        Args:
            response_data: The Lambda response (dict or JSON string)

        Returns:
            ExecuteResponse object
        """
        # Parse JSON string if needed
        if isinstance(response_data, str):
            try:
                response_data = json.loads(response_data)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Lambda response as JSON: {str(e)}")
                return ExecuteResponse(
                    success=False,
                    error=f"Failed to parse Lambda response as JSON: {str(e)}",
                )

        if not isinstance(response_data, dict):
            logger.error(f"Lambda response is not a dict: {type(response_data)}")
            return ExecuteResponse(
                success=False,
                error="Lambda response must be a dictionary",
            )

        success = response_data.get('success', False)
        message = response_data.get('message')

        # Build data dictionary from Lambda response
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
            if key not in (
                'success',
                'message',
                'response_type',
                'overlay_data',
                'browser_source_url',
                'targets',
            ):
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

    async def _invoke_lambda_with_retry(
        self,
        payload: Dict[str, Any],
        request: ExecuteRequest,
    ) -> Dict[str, Any]:
        """
        Invoke Lambda function with exponential backoff retry logic.

        Args:
            payload: The Lambda invocation payload
            request: The original ExecuteRequest

        Returns:
            The Lambda response data

        Raises:
            Exception: If all retry attempts fail
        """
        attempt = 0
        last_error = None

        while attempt <= self.max_retries:
            try:
                logger.debug(
                    f"Invoking Lambda function '{self._get_qualified_function_name()}' "
                    f"(attempt {attempt + 1}/{self.max_retries + 1}, "
                    f"session: {request.session_id})"
                )

                # Invoke Lambda function
                response = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.lambda_client.invoke(
                        FunctionName=self._get_qualified_function_name(),
                        InvocationType=self.invocation_type,
                        Payload=json.dumps(payload),
                    ),
                )

                # Parse response
                status_code = response.get('StatusCode', 500)

                if status_code not in (200, 202, 204):
                    error_msg = (
                        f"Lambda returned HTTP {status_code}: "
                        f"{response.get('FunctionError', 'Unknown error')}"
                    )
                    logger.error(error_msg)
                    raise Exception(error_msg)

                # For async invocations, return empty success
                if self.invocation_type == "Event":
                    logger.info(
                        f"Lambda invocation queued successfully "
                        f"(session: {request.session_id})"
                    )
                    return {"success": True, "message": "Lambda function queued"}

                # Parse response payload for sync invocations
                payload_stream = response.get('Payload')
                if payload_stream is None:
                    raise Exception("No payload in Lambda response")

                try:
                    response_data = json.loads(payload_stream.read().decode('utf-8'))
                except (json.JSONDecodeError, AttributeError) as e:
                    response_data = payload_stream

                # Check for Lambda function errors
                if response.get('FunctionError'):
                    error_details = response_data if isinstance(response_data, dict) else str(response_data)
                    raise Exception(f"Lambda function error: {error_details}")

                return response_data

            except Exception as e:
                last_error = e
                error_name = type(e).__name__

                # Check if error is retryable
                is_retryable = (
                    any(retryable in str(e) for retryable in self.RETRYABLE_ERRORS)
                    or isinstance(e, (
                        BotoCoreError,
                        ConnectionError,
                        EndpointConnectionError,
                        HTTPClientError,
                        ReadTimeoutError,
                    ))
                )

                if attempt < self.max_retries and is_retryable:
                    # Calculate exponential backoff delay
                    delay = min(
                        self.initial_retry_delay * (2 ** attempt),
                        self.max_retry_delay,
                    )

                    logger.warning(
                        f"Lambda invocation failed ({error_name}): {str(e)[:100]}. "
                        f"Retrying in {delay:.2f}s "
                        f"(attempt {attempt + 1}/{self.max_retries}, "
                        f"session: {request.session_id})"
                    )

                    await asyncio.sleep(delay)
                    attempt += 1
                else:
                    # Non-retryable error or max retries exceeded
                    logger.error(
                        f"Lambda invocation failed ({error_name}): {str(e)} "
                        f"(attempt {attempt + 1}/{self.max_retries + 1}, "
                        f"session: {request.session_id})"
                    )
                    raise

        # All retries exhausted
        raise last_error

    async def execute_async(self, request: ExecuteRequest) -> ExecuteResponse:
        """
        Execute the Lambda module asynchronously.

        This method:
        1. Builds the Lambda payload from the ExecuteRequest
        2. Invokes the Lambda function with exponential backoff retry logic
        3. Parses the response and converts to ExecuteResponse
        4. Tracks health status based on success/failure

        Args:
            request: ExecuteRequest containing command, args, and context

        Returns:
            ExecuteResponse with results from the Lambda function

        Raises:
            Exception: If the Lambda invocation fails (after recording the failure)
        """
        try:
            # Build payload
            payload = self._build_lambda_payload(request)

            logger.info(
                f"Executing Lambda module '{self.MODULE_NAME}' "
                f"(command: {request.command}, session: {request.session_id})"
            )

            # Invoke Lambda with retry logic
            response_data = await self._invoke_lambda_with_retry(payload, request)

            # Parse response
            execute_response = self._parse_lambda_response(response_data)

            # Record success/failure
            if execute_response.success:
                self.health.record_success()
                logger.info(
                    f"Lambda execution successful for module '{self.MODULE_NAME}' "
                    f"(session: {request.session_id})"
                )
            else:
                self.health.record_failure()
                logger.warning(
                    f"Lambda returned success=false for module '{self.MODULE_NAME}' "
                    f"(session: {request.session_id}): {execute_response.error}"
                )

            return execute_response

        except Exception as e:
            error_msg = f"Lambda invocation error: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.health.record_failure()

            return ExecuteResponse(
                success=False,
                error=error_msg,
            )

    def get_module_info(self) -> Dict[str, Any]:
        """
        Get information about this Lambda module.

        Returns:
            Dictionary containing module metadata and health status
        """
        info = super().get_module_info()
        info.update({
            "type": "lambda_adapter",
            "function_name": self._get_qualified_function_name(),
            "region": self.region,
            "invocation_type": self.invocation_type,
            "max_retries": self.max_retries,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "health": self.health.get_status(),
        })
        return info
