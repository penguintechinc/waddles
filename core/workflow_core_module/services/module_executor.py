"""
Module Executor Service
=======================

Executes module actions in workflows.
Routes action module calls through gRPC (primary) or HTTP (fallback).

Features:
- gRPC and HTTP dual transport with automatic fallback
- Support for all module types: interactive, pushing, core
- Request building with variable expression substitution
- Response handling and variable extraction
- Configurable timeouts and retry logic
- Comprehensive error handling
"""

import asyncio
import json
import logging
import sys
import os
from typing import Any, Dict, Optional, Tuple
from dataclasses import dataclass

import aiohttp
import grpc

from config import Config
from models.execution import ExecutionContext

logger = logging.getLogger(__name__)


@dataclass
class ModuleExecutionResult:
    """Result of module execution"""
    success: bool
    output: Dict[str, Any] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    module_name: Optional[str] = None
    module_version: Optional[str] = None
    execution_time_ms: float = 0.0
    transport_used: Optional[str] = None  # 'grpc' or 'http'

    def __post_init__(self):
        if self.output is None:
            self.output = {}


class GrpcModuleClientManager:
    """Manages gRPC connections to action modules"""

    def __init__(self):
        self._channels: Dict[str, grpc.aio.Channel] = {}
        self._lock = asyncio.Lock()
        self._module_hosts = self._build_module_hosts()

    def _build_module_hosts(self) -> Dict[str, str]:
        """Build mapping of module names to gRPC hosts from config"""
        hosts = {}

        # Action modules
        if hasattr(Config, 'DISCORD_GRPC_HOST'):
            hosts['discord_action'] = Config.DISCORD_GRPC_HOST
        if hasattr(Config, 'SLACK_GRPC_HOST'):
            hosts['slack_action'] = Config.SLACK_GRPC_HOST
        if hasattr(Config, 'TWITCH_GRPC_HOST'):
            hosts['twitch_action'] = Config.TWITCH_GRPC_HOST
        if hasattr(Config, 'YOUTUBE_GRPC_HOST'):
            hosts['youtube_action'] = Config.YOUTUBE_GRPC_HOST
        if hasattr(Config, 'LAMBDA_GRPC_HOST'):
            hosts['lambda_action'] = Config.LAMBDA_GRPC_HOST
        if hasattr(Config, 'GCP_FUNCTIONS_GRPC_HOST'):
            hosts['gcp_functions_action'] = Config.GCP_FUNCTIONS_GRPC_HOST
        if hasattr(Config, 'OPENWHISK_GRPC_HOST'):
            hosts['openwhisk_action'] = Config.OPENWHISK_GRPC_HOST

        # Core modules
        if hasattr(Config, 'REPUTATION_GRPC_HOST'):
            hosts['reputation'] = Config.REPUTATION_GRPC_HOST
        if hasattr(Config, 'WORKFLOW_GRPC_HOST'):
            hosts['workflow'] = Config.WORKFLOW_GRPC_HOST
        if hasattr(Config, 'BROWSER_SOURCE_GRPC_HOST'):
            hosts['browser_source'] = Config.BROWSER_SOURCE_GRPC_HOST
        if hasattr(Config, 'IDENTITY_GRPC_HOST'):
            hosts['identity'] = Config.IDENTITY_GRPC_HOST
        if hasattr(Config, 'HUB_GRPC_HOST'):
            hosts['hub_internal'] = Config.HUB_GRPC_HOST

        return hosts

    async def get_channel(self, module_name: str) -> Optional[grpc.aio.Channel]:
        """Get or create a gRPC channel for the specified module"""
        if not Config.GRPC_ENABLED:
            return None

        async with self._lock:
            if module_name in self._channels:
                channel = self._channels[module_name]
                try:
                    # Check if channel is still alive
                    await asyncio.wait_for(channel.channel_ready(), timeout=1.0)
                    return channel
                except Exception:
                    # Channel dead, recreate it
                    try:
                        await channel.close()
                    except Exception:
                        pass
                    del self._channels[module_name]

            # Get host for module
            host = self._module_hosts.get(module_name)
            if not host:
                logger.debug(f"No gRPC host configured for module {module_name}")
                return None

            try:
                # Configure channel with keepalive and other options
                options = [
                    ('grpc.keepalive_time_ms', getattr(Config, 'GRPC_KEEPALIVE_TIME_MS', 30000)),
                    ('grpc.keepalive_timeout_ms', getattr(Config, 'GRPC_KEEPALIVE_TIMEOUT_MS', 10000)),
                    ('grpc.keepalive_permit_without_calls', True),
                    ('grpc.http2.max_pings_without_data', 0),
                ]

                from flask_core.grpc_tls import secure_channel

                channel = secure_channel(host, options=options)
                self._channels[module_name] = channel

                logger.info(f"Created gRPC channel (TLS) to {module_name} at {host}")
                return channel

            except Exception as e:
                logger.warning(f"Failed to create gRPC channel for {module_name}: {e}")
                return None

    async def call_with_retry(
        self,
        method,
        request,
        max_retries: int = None,
        timeout: float = 30.0
    ) -> Any:
        """Call a gRPC method with retry logic and exponential backoff"""
        max_retries = max_retries or getattr(Config, 'GRPC_MAX_RETRIES', 3)
        last_error = None

        for attempt in range(max_retries):
            try:
                response = await asyncio.wait_for(
                    method(request),
                    timeout=timeout
                )
                return response
            except grpc.aio.AioRpcError as e:
                last_error = e
                if e.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                    wait_time = (2 ** attempt) * 0.5  # Exponential backoff
                    logger.warning(
                        f"gRPC call failed (attempt {attempt + 1}/{max_retries}): {e.code()}. "
                        f"Retrying in {wait_time}s"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    raise
            except asyncio.TimeoutError:
                last_error = asyncio.TimeoutError(f"gRPC call timed out after {timeout}s")
                wait_time = (2 ** attempt) * 0.5
                logger.warning(
                    f"gRPC call timed out (attempt {attempt + 1}/{max_retries}). "
                    f"Retrying in {wait_time}s"
                )
                await asyncio.sleep(wait_time)

        logger.error(f"gRPC call failed after {max_retries} attempts")
        raise last_error

    async def close_all(self):
        """Close all gRPC channels"""
        async with self._lock:
            for name, channel in self._channels.items():
                try:
                    await channel.close()
                    logger.info(f"Closed gRPC channel to {name}")
                except Exception as e:
                    logger.warning(f"Error closing channel to {name}: {e}")
            self._channels.clear()


class ModuleExecutor:
    """
    Executes action modules in workflows.

    Routes calls through gRPC (primary) or HTTP (fallback) with support for
    all module types (interactive, pushing, core).
    """

    def __init__(self):
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._grpc_manager = GrpcModuleClientManager() if Config.GRPC_ENABLED else None
        self._proto_path_setup = False

    async def _ensure_http_session(self):
        """Ensure HTTP session exists and is connected"""
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=60)
            self._http_session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        """Clean up resources"""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

        if self._grpc_manager:
            await self._grpc_manager.close_all()

    def _setup_proto_path(self):
        """Add proto path to sys.path for dynamic proto imports"""
        if self._proto_path_setup:
            return

        proto_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'libs', 'grpc_protos'
        )

        if proto_path not in sys.path:
            sys.path.insert(0, proto_path)
            self._proto_path_setup = True

    def _generate_token(self, payload: Optional[Dict] = None) -> str:
        """Generate a JWT token for service-to-service authentication"""
        import time
        import jwt

        token_payload = {
            'service': getattr(Config, 'MODULE_NAME', 'workflow_core_module'),
            'iat': int(time.time()),
            'exp': int(time.time()) + 3600,  # 1 hour expiry
        }

        if payload:
            token_payload.update(payload)

        # Config.SECRET_KEY is always set (Config.py's own require_secret_key()
        # already fails closed at import time in a production-like
        # environment) -- no second placeholder fallback needed here.
        return jwt.encode(token_payload, Config.SECRET_KEY, algorithm='HS256')

    def _normalize_module_name(self, module_name: str) -> str:
        """Normalize module name to gRPC module name format"""
        # Map module names to gRPC module names
        name_map = {
            'discord': 'discord_action',
            'discord_action': 'discord_action',
            'slack': 'slack_action',
            'slack_action': 'slack_action',
            'twitch': 'twitch_action',
            'twitch_action': 'twitch_action',
            'youtube': 'youtube_action',
            'youtube_action': 'youtube_action',
            'lambda': 'lambda_action',
            'lambda_action': 'lambda_action',
            'gcp_functions': 'gcp_functions_action',
            'gcp_functions_action': 'gcp_functions_action',
            'openwhisk': 'openwhisk_action',
            'openwhisk_action': 'openwhisk_action',
        }

        normalized = name_map.get(module_name.lower(), module_name)
        return normalized

    async def execute(
        self,
        module_name: str,
        module_version: str,
        input_data: Dict[str, Any],
        context: ExecutionContext,
        timeout_seconds: int = 30,
        retry_count: int = 1,
    ) -> ModuleExecutionResult:
        """
        Execute a module action.

        Args:
            module_name: Name of the module to execute
            module_version: Version of the module
            input_data: Input parameters for the module
            context: Execution context with workflow variables
            timeout_seconds: Timeout for execution
            retry_count: Number of retries on failure

        Returns:
            ModuleExecutionResult with output or error
        """
        normalized_name = self._normalize_module_name(module_name)

        # Build request payload
        request_payload = {
            'module_name': module_name,
            'module_version': module_version,
            'input': input_data,
            'entity_id': context.entity_id,
            'user_id': context.user_id,
            'session_id': context.session_id,
            'workflow_id': context.workflow_id,
            'execution_id': context.execution_id,
        }

        # Try gRPC first if enabled
        if Config.GRPC_ENABLED and self._grpc_manager:
            try:
                result = await self._execute_grpc(
                    normalized_name,
                    request_payload,
                    timeout_seconds,
                    retry_count
                )
                if result:
                    return result
            except Exception as e:
                logger.warning(
                    f"gRPC execution failed for {module_name}, falling back to HTTP: {e}"
                )

        # Fallback to HTTP
        try:
            result = await self._execute_http(
                module_name,
                request_payload,
                timeout_seconds,
                retry_count
            )
            return result
        except Exception as e:
            logger.error(f"HTTP execution failed for {module_name}: {e}")
            return ModuleExecutionResult(
                success=False,
                error=f"Module execution failed: {str(e)}",
                error_type="execution",
                module_name=module_name,
                module_version=module_version,
                transport_used="http"
            )

    async def _execute_grpc(
        self,
        module_name: str,
        request_payload: Dict[str, Any],
        timeout_seconds: float,
        retry_count: int
    ) -> Optional[ModuleExecutionResult]:
        """
        Execute module via gRPC.

        Returns None if gRPC is not available or not configured for this module.
        """
        if not self._grpc_manager:
            return None

        try:
            # Ensure proto path is set up for dynamic imports
            self._setup_proto_path()

            # Determine which proto to use based on module
            if 'action' in module_name:
                return await self._execute_grpc_action_module(
                    module_name,
                    request_payload,
                    timeout_seconds,
                    retry_count
                )
            else:
                # For core modules, use generic request format
                return await self._execute_grpc_generic(
                    module_name,
                    request_payload,
                    timeout_seconds,
                    retry_count
                )

        except Exception as e:
            logger.debug(f"gRPC execution not available for {module_name}: {e}")
            return None

    async def _execute_grpc_action_module(
        self,
        module_name: str,
        request_payload: Dict[str, Any],
        timeout_seconds: float,
        retry_count: int
    ) -> Optional[ModuleExecutionResult]:
        """
        Execute action module via gRPC.

        Dynamically imports module-specific protobuf definitions.
        """
        try:
            # Get channel for module
            channel = await self._grpc_manager.get_channel(module_name)
            if not channel:
                return None

            # Dynamically import module-specific proto
            # Format: discord_action -> discord_action_pb2
            proto_module_name = f"{module_name}_pb2"
            proto_grpc_name = f"{module_name}_pb2_grpc"

            try:
                # Try to import the module-specific proto
                proto_module = __import__(proto_module_name)
                proto_grpc = __import__(proto_grpc_name)
            except ImportError:
                logger.warning(
                    f"Proto module {proto_module_name} not found, "
                    f"using generic HTTP fallback"
                )
                return None

            # Convert request to module-specific format
            # For action modules, we'll use a generic request structure
            # Each module can define its own proto, but we'll serialize as JSON string
            request_json = json.dumps(request_payload)

            # Create generic request (this would need to match the actual proto)
            # For now, returning None to fall back to HTTP
            # In production, you'd create proper protobuf messages here
            return None

        except Exception as e:
            logger.debug(f"gRPC action module execution error: {e}")
            return None

    async def _execute_grpc_generic(
        self,
        module_name: str,
        request_payload: Dict[str, Any],
        timeout_seconds: float,
        retry_count: int
    ) -> Optional[ModuleExecutionResult]:
        """
        Execute module via gRPC using generic message format.

        Fallback for modules without specific proto definitions.
        """
        # This would use a common proto definition for all modules
        # For now, we'll return None to use HTTP fallback
        logger.debug(f"Generic gRPC not implemented for {module_name}")
        return None

    async def _execute_http(
        self,
        module_name: str,
        request_payload: Dict[str, Any],
        timeout_seconds: float,
        retry_count: int
    ) -> ModuleExecutionResult:
        """
        Execute module via HTTP (fallback transport).

        Retries with exponential backoff on failure.
        """
        await self._ensure_http_session()

        # Determine module URL
        module_url = self._get_module_http_url(module_name)
        if not module_url:
            return ModuleExecutionResult(
                success=False,
                error=f"No HTTP endpoint configured for module {module_name}",
                error_type="configuration",
                module_name=module_name,
                transport_used="http"
            )

        # Retry loop with exponential backoff
        last_error = None

        for attempt in range(retry_count + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                headers = {
                    'Content-Type': 'application/json',
                }

                # Add API key if configured
                if hasattr(Config, 'SERVICE_API_KEY') and Config.SERVICE_API_KEY:
                    headers['X-Service-Key'] = Config.SERVICE_API_KEY

                async with self._http_session.post(
                    f"{module_url}/api/v1/execute-module",
                    json=request_payload,
                    headers=headers,
                    timeout=timeout
                ) as response:
                    if response.status == 200:
                        result = await response.json()

                        # Extract output data
                        output = result.get('output', result.get('data', {}))

                        return ModuleExecutionResult(
                            success=True,
                            output=output,
                            module_name=request_payload.get('module_name'),
                            module_version=request_payload.get('module_version'),
                            transport_used="http"
                        )

                    elif response.status == 429:
                        # Rate limited, retry with backoff
                        last_error = f"Rate limited (HTTP {response.status})"
                        if attempt < retry_count:
                            wait_time = 2 ** attempt
                            logger.warning(
                                f"Module {module_name} rate limited, "
                                f"retrying in {wait_time}s (attempt {attempt + 1}/{retry_count + 1})"
                            )
                            await asyncio.sleep(wait_time)
                            continue

                    else:
                        error_text = await response.text()
                        last_error = f"HTTP {response.status}: {error_text}"

                        if attempt < retry_count and response.status >= 500:
                            # Server error, retry
                            wait_time = 2 ** attempt
                            logger.warning(
                                f"Module {module_name} returned {response.status}, "
                                f"retrying in {wait_time}s (attempt {attempt + 1}/{retry_count + 1})"
                            )
                            await asyncio.sleep(wait_time)
                            continue

                        return ModuleExecutionResult(
                            success=False,
                            error=last_error,
                            error_type="http_error",
                            module_name=request_payload.get('module_name'),
                            module_version=request_payload.get('module_version'),
                            transport_used="http"
                        )

            except asyncio.TimeoutError:
                last_error = f"Timeout after {timeout_seconds}s"
                if attempt < retry_count:
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Module {module_name} timeout, "
                        f"retrying in {wait_time}s (attempt {attempt + 1}/{retry_count + 1})"
                    )
                    await asyncio.sleep(wait_time)
                    continue

            except Exception as e:
                last_error = str(e)
                if attempt < retry_count:
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Module {module_name} execution error: {e}, "
                        f"retrying in {wait_time}s (attempt {attempt + 1}/{retry_count + 1})"
                    )
                    await asyncio.sleep(wait_time)
                    continue

        # All retries exhausted
        return ModuleExecutionResult(
            success=False,
            error=f"Module execution failed after {retry_count + 1} attempts: {last_error}",
            error_type="execution",
            module_name=request_payload.get('module_name'),
            module_version=request_payload.get('module_version'),
            transport_used="http"
        )

    def _get_module_http_url(self, module_name: str) -> Optional[str]:
        """
        Get HTTP URL for a module.

        Returns module-specific URL or generic module service URL.
        """
        # Try to get module-specific environment variable
        env_key = f"{module_name.upper()}_HTTP_URL"
        if hasattr(Config, env_key):
            return getattr(Config, env_key)

        # Fallback to generic module service URL
        if hasattr(Config, 'MODULE_SERVICE_URL'):
            return Config.MODULE_SERVICE_URL

        # Construct from module name
        # action modules: http://discord-action:8051, etc.
        module_type = 'action' if 'action' in module_name else 'core'
        module_short = module_name.replace('_action', '').replace('_module', '')

        if module_type == 'action':
            return f"http://{module_short}-action:8051"
        else:
            return f"http://{module_short}-core:8050"

    async def extract_variables(
        self,
        response: ModuleExecutionResult,
        output_mapping: Dict[str, str],
        context: ExecutionContext
    ) -> Dict[str, Any]:
        """
        Extract variables from module response using output mapping.

        Args:
            response: Module execution result
            output_mapping: Mapping of response fields to context variables
            context: Execution context to store variables in

        Returns:
            Dictionary of extracted variables
        """
        extracted = {}

        if not response.success or not response.output:
            return extracted

        # Apply output mapping
        for response_field, context_var in output_mapping.items():
            if response_field in response.output:
                value = response.output[response_field]
                context.set_variable(context_var, value)
                extracted[context_var] = value

        return extracted

    async def substitute_expressions(
        self,
        input_data: Dict[str, Any],
        context: ExecutionContext
    ) -> Dict[str, Any]:
        """
        Substitute workflow variables in input data.

        Supports expressions like:
        - {variable_name}
        - {variable_name|default_value}
        - {nested.object.path}

        Args:
            input_data: Input data with variable references
            context: Execution context with variables

        Returns:
            Input data with substituted values
        """
        substituted = {}

        for key, value in input_data.items():
            substituted[key] = self._substitute_value(value, context)

        return substituted

    def _substitute_value(self, value: Any, context: ExecutionContext) -> Any:
        """Recursively substitute variables in a value"""
        if isinstance(value, str):
            return self._substitute_string(value, context)
        elif isinstance(value, dict):
            return {k: self._substitute_value(v, context) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._substitute_value(v, context) for v in value]
        else:
            return value

    def _substitute_string(self, value: str, context: ExecutionContext) -> str:
        """Substitute variables in a string"""
        import re

        def replace_var(match):
            var_expr = match.group(1)

            # Handle default values: {var|default}
            if '|' in var_expr:
                var_name, default_value = var_expr.split('|', 1)
            else:
                var_name = var_expr
                default_value = None

            var_name = var_name.strip()

            # Get variable from context
            try:
                val = context.get_variable(var_name)
                if val is not None:
                    return str(val)
                elif default_value is not None:
                    return default_value.strip()
                else:
                    return match.group(0)  # Return original if not found
            except Exception:
                if default_value is not None:
                    return default_value.strip()
                return match.group(0)

        # Replace {variable} patterns
        result = re.sub(r'\{([^}]+)\}', replace_var, value)
        return result
