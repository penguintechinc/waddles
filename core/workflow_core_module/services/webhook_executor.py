"""
Webhook Executor Service

Executes outbound webhook actions in workflows with support for:
- Multiple HTTP methods (POST, GET, PUT, DELETE)
- HMAC signature generation for security
- Request body templating with expression substitution
- Response parsing and variable extraction
- Timeout and retry handling
- Custom headers
- Async execution with httpx
"""

import ast
import asyncio
import hashlib
import hmac
import json
import logging
import operator
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from flask_core import describe_db_error

logger = logging.getLogger(__name__)

# SECURITY (PII in logs): httpx logs every request URL at INFO ("HTTP Request: GET <url>"),
# and a user-configured webhook URL routinely carries tokens / ids in its query string.
logging.getLogger("httpx").setLevel(logging.WARNING)


class WebhookExecutionError(Exception):
    """Base exception for webhook execution errors."""

    pass


class WebhookTimeoutError(WebhookExecutionError):
    """Raised when webhook request times out."""

    pass


class WebhookRetryableError(WebhookExecutionError):
    """Raised when webhook request fails but is retryable."""

    pass


class WebhookNonRetryableError(WebhookExecutionError):
    """Raised when webhook request fails and should not be retried."""

    pass


class HMACSignatureGenerator:
    """Generates HMAC signatures for webhook authentication."""

    def __init__(self, secret: str, algorithm: str = "sha256"):
        """
        Initialize HMAC signature generator.

        Args:
            secret: Secret key for HMAC generation
            algorithm: HMAC algorithm (sha256, sha512, sha1)
        """
        self.secret = secret.encode() if isinstance(secret, str) else secret
        self.algorithm = algorithm.lower()

        if self.algorithm not in ["sha256", "sha512", "sha1"]:
            raise ValueError(f"Unsupported HMAC algorithm: {self.algorithm}")

    def generate(self, payload: str) -> str:
        """
        Generate HMAC signature for payload.

        Args:
            payload: Data to sign

        Returns:
            Hex-encoded HMAC signature
        """
        payload_bytes = payload.encode() if isinstance(payload, str) else payload
        signature = hmac.new(
            self.secret, payload_bytes, getattr(hashlib, self.algorithm)
        )
        return signature.hexdigest()

    def verify(self, payload: str, signature: str) -> bool:
        """
        Verify HMAC signature.

        Args:
            payload: Original data
            signature: Signature to verify

        Returns:
            True if signature is valid
        """
        expected_signature = self.generate(payload)
        return hmac.compare_digest(expected_signature, signature)


class UnsafeExpressionError(Exception):
    """Raised when a `$(...)` webhook-template expression uses a disallowed construct."""


class SafeExpressionEvaluator:
    """AST-walking evaluator for the `$(...)` webhook-template mini-language.

    Replaces `eval(expr, {"__builtins__": {}}, context)` (SECURITY C8,
    OWASP A03/RCE): stripping `__builtins__` from an `eval()` globals dict
    is a well-known escapable sandbox -- attribute-chain gadgets such as
    `().__class__.__bases__[0].__subclasses__()` reach arbitrary Python
    objects (and from there, `os.system`/`subprocess`/etc.) without ever
    touching the stripped `__builtins__` mapping, since object attribute
    access happens through the object's own `__class__`, not through
    builtins lookup. This evaluator never calls `eval`/`exec`/`compile` on
    attacker-controlled input -- it parses the expression into an
    `ast.Expression` and walks it, evaluating only an explicit allowlist
    of node types (arithmetic, comparisons, boolean logic, bare-name
    lookups into `context`). Attribute access, subscripting, function
    calls, comprehensions, lambdas, and imports are all refused
    (`UnsafeExpressionError`) -- there is no gadget surface to escape
    because nothing here ever resolves an object's `__class__`/
    `__globals__`/`__subclasses__`; those are `ast.Attribute` nodes, which
    this evaluator doesn't implement at all.
    """

    _BINOPS: Dict[type, Any] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    _UNARYOPS: Dict[type, Any] = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
        ast.Not: operator.not_,
    }
    _COMPARES: Dict[type, Any] = {
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.Lt: operator.lt,
        ast.LtE: operator.le,
        ast.Gt: operator.gt,
        ast.GtE: operator.ge,
        ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b,
    }

    def __init__(self, context: Dict[str, Any]):
        self._context = context

    def evaluate(self, expr: str) -> Any:
        """Parse and evaluate `expr`; raises `UnsafeExpressionError` for any disallowed construct."""
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as exc:
            raise UnsafeExpressionError(f"invalid expression syntax: {exc}") from exc
        return self._eval(tree)

    def _eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return self._eval(node.body)

        if isinstance(node, ast.Constant):
            return node.value

        if isinstance(node, ast.Name):
            # Dunder/private names are refused even though this evaluator
            # never implements ast.Attribute -- defense in depth against a
            # `context` dict that happens to hold a key like `__class__`.
            if node.id.startswith("_"):
                raise UnsafeExpressionError(f"disallowed name: {node.id}")
            return self._context.get(node.id)

        if isinstance(node, ast.BinOp):
            op = self._BINOPS.get(type(node.op))
            if op is None:
                raise UnsafeExpressionError(f"disallowed operator: {type(node.op).__name__}")
            return op(self._eval(node.left), self._eval(node.right))

        if isinstance(node, ast.UnaryOp):
            op = self._UNARYOPS.get(type(node.op))
            if op is None:
                raise UnsafeExpressionError(f"disallowed operator: {type(node.op).__name__}")
            return op(self._eval(node.operand))

        if isinstance(node, ast.BoolOp):
            values = [self._eval(v) for v in node.values]
            if isinstance(node.op, ast.And):
                result: Any = values[0]
                for value in values[1:]:
                    result = result and value
                return result
            result = values[0]
            for value in values[1:]:
                result = result or value
            return result

        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            outcome = True
            for op_node, comparator in zip(node.ops, node.comparators):
                op = self._COMPARES.get(type(op_node))
                if op is None:
                    raise UnsafeExpressionError(
                        f"disallowed comparison: {type(op_node).__name__}"
                    )
                right = self._eval(comparator)
                if not op(left, right):
                    outcome = False
                    break
                left = right
            return outcome

        if isinstance(node, (ast.Tuple, ast.List)):
            # Literal tuples/lists of otherwise-allowed elements only --
            # needed for `x in (1, 2, 3)`/`x in ['a', 'b']`. Each element
            # still goes through this same allowlisted `_eval`, so this
            # adds no new gadget surface (no arbitrary object is ever
            # constructed, only nested constants/names/expressions).
            values = tuple(self._eval(elt) for elt in node.elts)
            return values if isinstance(node, ast.Tuple) else list(values)

        raise UnsafeExpressionError(
            f"disallowed expression construct: {type(node).__name__}"
        )


class ExpressionTemplater:
    """Handles expression substitution in webhook payloads."""

    EXPRESSION_PATTERN = re.compile(r"\$\{([^}]+)\}")
    BRACKET_PATTERN = re.compile(r"\$\(([^)]+)\)")

    @staticmethod
    def substitute(template: str, context: Dict[str, Any]) -> str:
        """
        Substitute expressions in template using context variables.

        Supports two formats:
        - ${variable_name} - Simple variable substitution
        - $(expression) - JavaScript-like expression evaluation

        Args:
            template: Template string with expressions
            context: Context dictionary with variables

        Returns:
            Template with substitutions applied
        """
        result = template

        # Handle ${variable} format
        def replace_variable(match):
            var_name = match.group(1)
            keys = var_name.split(".")

            value = context
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                else:
                    return match.group(0)  # Return original if path invalid

            if value is None:
                return ""
            return str(value)

        result = ExpressionTemplater.EXPRESSION_PATTERN.sub(replace_variable, result)

        # Handle $(expression) format -- arithmetic/comparison/boolean
        # evaluation via SafeExpressionEvaluator's AST allowlist, never
        # eval() (SECURITY C8 -- see that class's own docstring for why
        # `eval(expr, {"__builtins__": {}}, context)` was an escapable
        # sandbox, not a safe one).
        def evaluate_expression(match):
            expr = match.group(1)
            try:
                return str(SafeExpressionEvaluator(context).evaluate(expr))
            except UnsafeExpressionError as e:
                logger.warning(f"Rejected unsafe expression '{expr}': {describe_db_error(e)}")
                return match.group(0)  # Return original if expression is disallowed
            except Exception as e:
                logger.warning(f"Failed to evaluate expression '{expr}': {describe_db_error(e)}")
                return match.group(0)  # Return original if evaluation fails

        result = ExpressionTemplater.BRACKET_PATTERN.sub(evaluate_expression, result)

        return result

    @staticmethod
    def substitute_json(data: Any, context: Dict[str, Any]) -> Any:
        """
        Recursively substitute expressions in JSON data structure.

        Args:
            data: JSON-serializable data (dict, list, str, etc.)
            context: Context dictionary with variables

        Returns:
            Data with substitutions applied
        """
        if isinstance(data, dict):
            return {k: ExpressionTemplater.substitute_json(v, context) for k, v in data.items()}
        elif isinstance(data, list):
            return [ExpressionTemplater.substitute_json(item, context) for item in data]
        elif isinstance(data, str):
            return ExpressionTemplater.substitute(data, context)
        else:
            return data


class ResponseExtractor:
    """Extracts and parses webhook responses."""

    @staticmethod
    def extract_json(response: httpx.Response) -> Dict[str, Any]:
        """
        Extract JSON from response.

        Args:
            response: httpx.Response object

        Returns:
            Parsed JSON or error dict

        Raises:
            WebhookExecutionError: If response is not valid JSON
        """
        try:
            return response.json()
        except json.JSONDecodeError as e:
            raise WebhookExecutionError(f"Invalid JSON in webhook response: {e}")

    @staticmethod
    def extract_variables(
        response: httpx.Response, extractors: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Extract variables from webhook response using JSON pointers/paths.

        Supports formats like:
        - "status" - Simple key access
        - "data.user.id" - Nested key access
        - "items[0].name" - Array access

        Args:
            response: httpx.Response object
            extractors: Dict mapping variable name to JSON path

        Returns:
            Dictionary of extracted variables
        """
        if not extractors:
            return {}

        extracted = {}

        try:
            if response.headers.get("content-type", "").startswith("application/json"):
                data = response.json()
            else:
                data = None
        except json.JSONDecodeError:
            data = None

        for var_name, path in extractors.items():
            try:
                value = ResponseExtractor._get_nested_value(data, path)
                extracted[var_name] = value
            except (KeyError, IndexError, TypeError, AttributeError) as e:
                logger.warning(
                f"Failed to extract variable '{var_name}' from path '{path}': "
                f"{describe_db_error(e)}"
            )
                extracted[var_name] = None

        return extracted

    @staticmethod
    def _get_nested_value(data: Any, path: str) -> Any:
        """
        Get nested value from data using path notation.

        Args:
            data: Data to traverse
            path: Path like "a.b.c" or "a[0].b"

        Returns:
            Value at path

        Raises:
            KeyError, IndexError, TypeError if path is invalid
        """
        if data is None:
            raise TypeError("Cannot traverse None")

        # Parse path segments
        segments = []
        current_segment = ""

        for char in path:
            if char == ".":
                if current_segment:
                    segments.append(current_segment)
                    current_segment = ""
            elif char == "[":
                if current_segment:
                    segments.append(current_segment)
                    current_segment = ""
            elif char == "]":
                if current_segment:
                    segments.append(f"[{current_segment}]")
                    current_segment = ""
            else:
                current_segment += char

        if current_segment:
            segments.append(current_segment)

        # Traverse data
        current = data
        for segment in segments:
            if segment.startswith("[") and segment.endswith("]"):
                index = int(segment[1:-1])
                current = current[index]
            else:
                current = current[segment]

        return current


class RetryPolicy:
    """Handles retry logic for webhook requests."""

    def __init__(
        self,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        retryable_status_codes: Optional[List[int]] = None,
    ):
        """
        Initialize retry policy.

        Args:
            max_retries: Maximum number of retries
            initial_delay: Initial retry delay in seconds
            max_delay: Maximum retry delay in seconds
            exponential_base: Base for exponential backoff
            retryable_status_codes: HTTP status codes to retry on
        """
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.retryable_status_codes = retryable_status_codes or [408, 429, 500, 502, 503, 504]

    def is_retryable(self, error: Exception, status_code: Optional[int] = None) -> bool:
        """
        Determine if request should be retried.

        Args:
            error: Exception from request
            status_code: HTTP status code (if available)

        Returns:
            True if request should be retried
        """
        if isinstance(error, (WebhookTimeoutError, WebhookRetryableError)):
            return True

        if status_code and status_code in self.retryable_status_codes:
            return True

        if isinstance(error, httpx.RequestError):
            # Retry connection errors
            return True

        return False

    def get_delay(self, attempt: int) -> float:
        """
        Calculate delay for retry attempt using exponential backoff.

        Args:
            attempt: Attempt number (0-indexed)

        Returns:
            Delay in seconds
        """
        delay = self.initial_delay * (self.exponential_base ** attempt)
        return min(delay, self.max_delay)


class WebhookExecutor:
    """Executes webhook actions with full feature support."""

    def __init__(
        self,
        timeout: float = 30.0,
        retry_policy: Optional[RetryPolicy] = None,
        verify_ssl: bool = True,
    ):
        """
        Initialize webhook executor.

        Args:
            timeout: Request timeout in seconds
            retry_policy: RetryPolicy instance
            verify_ssl: Whether to verify SSL certificates
        """
        self.timeout = timeout
        self.retry_policy = retry_policy or RetryPolicy()
        self.verify_ssl = verify_ssl

    async def execute(
        self,
        url: str,
        method: str = "POST",
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        context: Optional[Dict[str, Any]] = None,
        hmac_secret: Optional[str] = None,
        hmac_header: str = "X-Webhook-Signature",
        hmac_algorithm: str = "sha256",
        extractors: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Execute webhook request with full feature support.

        Args:
            url: Webhook URL to call
            method: HTTP method (POST, GET, PUT, DELETE)
            body: Request body (will be JSON-encoded)
            headers: Custom headers
            context: Context variables for expression substitution
            hmac_secret: Secret for HMAC signature generation
            hmac_header: Header name for HMAC signature
            hmac_algorithm: HMAC algorithm (sha256, sha512, sha1)
            extractors: Variable extractors from response

        Returns:
            Dict with keys:
            - success: bool
            - status_code: int
            - response_body: str or dict
            - extracted_variables: dict
            - error: str (if failed)
            - execution_time: float

        Raises:
            WebhookExecutionError: If execution fails permanently
        """
        start_time = time.time()
        context = context or {}
        headers = headers or {}
        method = method.upper()

        # Validate method
        if method not in ["POST", "GET", "PUT", "DELETE"]:
            raise WebhookExecutionError(f"Unsupported HTTP method: {method}")

        # Prepare request body
        request_body = None
        request_body_str = None

        if body:
            # Substitute expressions in body
            substituted_body = ExpressionTemplater.substitute_json(body, context)
            request_body = substituted_body
            request_body_str = json.dumps(substituted_body, separators=(",", ":"))

        # Prepare headers
        final_headers = {"User-Agent": "WaddleBot/1.0"}.copy()
        final_headers.update(headers)

        if request_body_str:
            final_headers["Content-Type"] = "application/json"

        # Generate HMAC signature if secret provided
        if hmac_secret and request_body_str:
            sig_gen = HMACSignatureGenerator(hmac_secret, hmac_algorithm)
            signature = sig_gen.generate(request_body_str)
            final_headers[hmac_header] = signature

        # Execute with retry logic
        last_error = None
        last_response = None

        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                async with httpx.AsyncClient(verify=self.verify_ssl) as client:
                    response = await client.request(
                        method,
                        url,
                        json=request_body if method in ["POST", "PUT"] else None,
                        content=request_body_str if method in ["POST", "PUT"] else None,
                        headers=final_headers,
                        timeout=self.timeout,
                    )

                    last_response = response

                    # Check for HTTP errors
                    if response.status_code >= 400:
                        error_msg = f"HTTP {response.status_code}: {response.text[:500]}"

                        # `is_retryable()` short-circuits True for any
                        # WebhookRetryableError/WebhookTimeoutError instance
                        # regardless of status code -- passing a
                        # WebhookRetryableError here (as if it were already
                        # known-retryable) made every HTTP error status
                        # retry unconditionally, silently making
                        # `retryable_status_codes` and the
                        # WebhookNonRetryableError branch below dead code.
                        # The base WebhookExecutionError isn't in that
                        # isinstance allowlist, so this now actually
                        # defers to the status-code check.
                        if self.retry_policy.is_retryable(
                            WebhookExecutionError(error_msg), response.status_code
                        ):
                            if attempt < self.retry_policy.max_retries:
                                delay = self.retry_policy.get_delay(attempt)
                                logger.warning(
                                    f"Webhook request failed with {response.status_code}, "
                                    f"retrying in {delay}s (attempt {attempt + 1}/{self.retry_policy.max_retries + 1})"
                                )
                                await asyncio.sleep(delay)
                                continue
                            else:
                                raise WebhookRetryableError(error_msg)
                        else:
                            raise WebhookNonRetryableError(error_msg)

                    # Success
                    return self._build_response(
                        response, extractors, start_time, request_body_str
                    )

            except httpx.TimeoutException as e:
                last_error = WebhookTimeoutError(f"Request timeout after {self.timeout}s")

                if attempt < self.retry_policy.max_retries:
                    delay = self.retry_policy.get_delay(attempt)
                    logger.warning(
                        f"Webhook request timed out, retrying in {delay}s "
                        f"(attempt {attempt + 1}/{self.retry_policy.max_retries + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue

            except httpx.RequestError as e:
                last_error = e

                if self.retry_policy.is_retryable(e):
                    if attempt < self.retry_policy.max_retries:
                        delay = self.retry_policy.get_delay(attempt)
                        logger.warning(
                            f"Webhook request failed: {describe_db_error(e)}, retrying in {delay}s "
                            f"(attempt {attempt + 1}/{self.retry_policy.max_retries + 1})"
                        )
                        await asyncio.sleep(delay)
                        continue
                # Non-retryable (or retries already exhausted) -- stop now.
                # Previously fell through to the next loop iteration
                # unconditionally, retrying errors the policy had just said
                # not to retry (harmless when this was already the last
                # attempt, but wasted real HTTP calls against a
                # known-permanently-failing endpoint on every earlier one).
                break

            except (WebhookRetryableError, WebhookNonRetryableError) as e:
                last_error = e

                if isinstance(e, WebhookRetryableError) and attempt < self.retry_policy.max_retries:
                    delay = self.retry_policy.get_delay(attempt)
                    logger.warning(
                        f"Webhook request failed: {describe_db_error(e)}, retrying in {delay}s "
                        f"(attempt {attempt + 1}/{self.retry_policy.max_retries + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                # Non-retryable -- stop now instead of silently retrying a
                # request the status-code check just said not to retry.
                break

        # All retries exhausted
        execution_time = time.time() - start_time

        if last_response is not None:
            return {
                "success": False,
                "status_code": last_response.status_code,
                "response_body": last_response.text,
                "extracted_variables": {},
                "error": str(last_error),
                "execution_time": execution_time,
            }

        return {
            "success": False,
            "status_code": None,
            "response_body": None,
            "extracted_variables": {},
            "error": str(last_error),
            "execution_time": execution_time,
        }

    def _build_response(
        self,
        response: httpx.Response,
        extractors: Optional[Dict[str, str]],
        start_time: float,
        request_body_str: Optional[str],
    ) -> Dict[str, Any]:
        """Build successful response dictionary."""
        execution_time = time.time() - start_time

        # Parse response body
        content_type = response.headers.get("content-type", "")

        if "application/json" in content_type:
            try:
                response_body = response.json()
            except json.JSONDecodeError:
                response_body = response.text
        else:
            response_body = response.text

        # Extract variables
        extracted_variables = ResponseExtractor.extract_variables(response, extractors)

        return {
            "success": True,
            "status_code": response.status_code,
            "response_body": response_body,
            "extracted_variables": extracted_variables,
            "error": None,
            "execution_time": execution_time,
        }


class WebhookActionNode:
    """Represents a webhook action node in a workflow."""

    def __init__(
        self,
        node_id: str,
        url: str,
        method: str = "POST",
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        hmac_secret: Optional[str] = None,
        hmac_header: str = "X-Webhook-Signature",
        hmac_algorithm: str = "sha256",
        extractors: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
        retry_config: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize webhook action node.

        Args:
            node_id: Unique node identifier
            url: Webhook URL
            method: HTTP method
            body: Request body template
            headers: Custom headers
            hmac_secret: HMAC secret for signing
            hmac_header: Header name for signature
            hmac_algorithm: HMAC algorithm
            extractors: Variable extractors
            timeout: Request timeout
            retry_config: Retry configuration dict
        """
        self.node_id = node_id
        self.url = url
        self.method = method
        self.body = body
        self.headers = headers
        self.hmac_secret = hmac_secret
        self.hmac_header = hmac_header
        self.hmac_algorithm = hmac_algorithm
        self.extractors = extractors
        self.timeout = timeout

        # Initialize retry policy from config
        retry_config = retry_config or {}
        self.retry_policy = RetryPolicy(
            max_retries=retry_config.get("max_retries", 3),
            initial_delay=retry_config.get("initial_delay", 1.0),
            max_delay=retry_config.get("max_delay", 60.0),
            exponential_base=retry_config.get("exponential_base", 2.0),
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute webhook action.

        Args:
            context: Workflow execution context

        Returns:
            Execution result dict
        """
        executor = WebhookExecutor(timeout=self.timeout, retry_policy=self.retry_policy)

        try:
            result = await executor.execute(
                url=self.url,
                method=self.method,
                body=self.body,
                headers=self.headers,
                context=context,
                hmac_secret=self.hmac_secret,
                hmac_header=self.hmac_header,
                hmac_algorithm=self.hmac_algorithm,
                extractors=self.extractors,
            )

            return {
                "node_id": self.node_id,
                "type": "webhook",
                "success": result["success"],
                "status_code": result["status_code"],
                "response_body": result["response_body"],
                "extracted_variables": result["extracted_variables"],
                "error": result["error"],
                "execution_time": result["execution_time"],
            }

        except Exception as e:
            logger.error(
                f"Webhook execution failed for node {self.node_id}: "
                f"{describe_db_error(e)}"
            )
            return {
                "node_id": self.node_id,
                "type": "webhook",
                "success": False,
                "status_code": None,
                "response_body": None,
                "extracted_variables": {},
                "error": str(e),
                "execution_time": 0.0,
            }
