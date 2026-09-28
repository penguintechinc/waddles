"""
API Utilities
=============

Standardized API response formatting, error handling, and decorators.
"""

from quart import jsonify, request, Blueprint
from functools import wraps
from typing import Any, Dict, Optional, Callable, List
import time
import logging
import os
import psutil
from datetime import datetime

logger = logging.getLogger(__name__)

#: Placeholder `SECRET_KEY` values that must never sign a token in
#: production (C1 security fix, A02:2021/CWE-798).
_KNOWN_PLACEHOLDER_SECRETS = frozenset({'change-me-in-production', ''})


def _require_secret_key() -> str:
    """Fail closed on an unset/placeholder `SECRET_KEY` when `RELEASE_MODE=true`.

    This file is a standalone vendored copy of `flask_core.api_utils`
    (`services/core-community/libs/flask_core`), not the shared
    `libs/flask_core` package used elsewhere in this repo, so it cannot
    import `flask_core.secrets.require_secret_key()` and instead
    duplicates the same fail-closed check against this service's own
    `RELEASE_MODE` convention (`config.py`'s `RELEASE_MODE = os.getenv(
    'RELEASE_MODE', 'false').lower() == 'true'`). Previously this decorator
    silently accepted `'change-me-in-production'` as a valid HS256 signing
    key -- a publicly known key forges tokens for every request this
    decorator protects.
    """
    value = os.getenv('SECRET_KEY', 'change-me-in-production')
    release_mode = os.getenv('RELEASE_MODE', 'false').strip().lower() == 'true'
    if release_mode and value in _KNOWN_PLACEHOLDER_SECRETS:
        raise RuntimeError(
            "SECRET_KEY is unset or still the insecure default with RELEASE_MODE=true -- "
            "refusing to verify tokens against a publicly known key. Set SECRET_KEY to a "
            "unique, randomly generated secret."
        )
    return value


# Metrics storage for Prometheus format
_metrics = {
    'requests_total': 0,
    'requests_success': 0,
    'requests_error': 0,
    'request_duration_seconds': []
}


def create_health_blueprint(module_name: str, module_version: str):
    """
    Create a blueprint with /health, /healthz, and /metrics endpoints.

    Args:
        module_name: Name of the module
        module_version: Version of the module

    Returns:
        Blueprint with health and metrics endpoints
    """
    health_bp = Blueprint('health', __name__)

    @health_bp.route('/health')
    async def health():
        """Basic health check endpoint."""
        return jsonify({
            "status": "healthy",
            "module": module_name,
            "version": module_version,
            "timestamp": datetime.utcnow().isoformat()
        }), 200

    @health_bp.route('/healthz')
    async def healthz():
        """Kubernetes liveness/readiness probe endpoint."""
        try:
            # Basic checks
            checks = {
                "status": "healthy",
                "module": module_name,
                "version": module_version,
                "checks": {
                    "memory": "ok",
                    "cpu": "ok"
                }
            }

            # Memory check (fail if > 90% used)
            memory = psutil.virtual_memory()
            if memory.percent > 90:
                checks["checks"]["memory"] = "warning"
                checks["status"] = "degraded"

            # CPU check (fail if > 95% used)
            cpu_percent = psutil.cpu_percent(interval=0.1)
            if cpu_percent > 95:
                checks["checks"]["cpu"] = "warning"
                checks["status"] = "degraded"

            checks["timestamp"] = datetime.utcnow().isoformat()

            status_code = 200 if checks["status"] == "healthy" else 503
            return jsonify(checks), status_code

        except Exception as e:
            return jsonify({
                "status": "unhealthy",
                "module": module_name,
                "version": module_version,
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }), 503

    @health_bp.route('/metrics')
    async def metrics():
        """Prometheus metrics endpoint."""
        try:
            # Get process metrics
            process = psutil.Process(os.getpid())
            memory_info = process.memory_info()

            # Calculate average request duration
            avg_duration = 0
            if _metrics['request_duration_seconds']:
                recent = _metrics['request_duration_seconds'][-100:]
                avg_duration = sum(recent) / len(recent)

            # Format as Prometheus text format
            metrics_output = f"""# HELP waddlebot_info Module information
# TYPE waddlebot_info gauge
waddlebot_info{{module="{module_name}",version="{module_version}"}} 1

# HELP waddlebot_requests_total Total number of requests
# TYPE waddlebot_requests_total counter
waddlebot_requests_total{{module="{module_name}"}} {_metrics['requests_total']}

# HELP waddlebot_requests_success_total Total successful requests
# TYPE waddlebot_requests_success_total counter
waddlebot_requests_success_total{{module="{module_name}"}} {_metrics['requests_success']}

# HELP waddlebot_requests_error_total Total failed requests
# TYPE waddlebot_requests_error_total counter
waddlebot_requests_error_total{{module="{module_name}"}} {_metrics['requests_error']}

# HELP waddlebot_request_duration_seconds Average request duration
# TYPE waddlebot_request_duration_seconds gauge
waddlebot_request_duration_seconds{{module="{module_name}"}} {avg_duration:.6f}

# HELP waddlebot_memory_bytes Memory usage in bytes
# TYPE waddlebot_memory_bytes gauge
waddlebot_memory_bytes{{module="{module_name}",type="rss"}} {memory_info.rss}
waddlebot_memory_bytes{{module="{module_name}",type="vms"}} {memory_info.vms}

# HELP waddlebot_cpu_percent CPU usage percentage
# TYPE waddlebot_cpu_percent gauge
waddlebot_cpu_percent{{module="{module_name}"}} {process.cpu_percent()}

# HELP waddlebot_open_files Number of open file descriptors
# TYPE waddlebot_open_files gauge
waddlebot_open_files{{module="{module_name}"}} {len(process.open_files())}

# HELP waddlebot_threads Number of threads
# TYPE waddlebot_threads gauge
waddlebot_threads{{module="{module_name}"}} {process.num_threads()}
"""
            from quart import Response
            return Response(metrics_output, mimetype='text/plain')

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return health_bp


def record_request_metrics(success: bool, duration: float):
    """Record request metrics for Prometheus."""
    _metrics['requests_total'] += 1
    if success:
        _metrics['requests_success'] += 1
    else:
        _metrics['requests_error'] += 1
    _metrics['request_duration_seconds'].append(duration)
    # Keep only last 1000 durations
    if len(_metrics['request_duration_seconds']) > 1000:
        _metrics['request_duration_seconds'] = _metrics['request_duration_seconds'][-1000:]


def success_response(
    data: Any,
    status_code: int = 200,
    message: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None
) -> tuple:
    """
    Create standardized success response.

    Args:
        data: Response data
        status_code: HTTP status code
        message: Optional success message
        meta: Optional metadata (pagination, etc.)

    Returns:
        Tuple of (response_dict, status_code)
    """
    response = {
        "success": True,
        "data": data,
        "timestamp": datetime.utcnow().isoformat()
    }

    if message:
        response["message"] = message

    if meta:
        response["meta"] = meta

    return jsonify(response), status_code


def error_response(
    message: str,
    status_code: int = 400,
    error_code: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None
) -> tuple:
    """
    Create standardized error response.

    Args:
        message: Error message
        status_code: HTTP status code
        error_code: Optional error code
        details: Optional error details

    Returns:
        Tuple of (response_dict, status_code)
    """
    response = {
        "success": False,
        "error": {
            "message": message,
            "code": error_code or f"ERROR_{status_code}",
            "timestamp": datetime.utcnow().isoformat()
        }
    }

    if details:
        response["error"]["details"] = details

    return jsonify(response), status_code


def paginate_response(
    items: List[Any],
    total: int,
    page: int,
    per_page: int,
    endpoint: Optional[str] = None
) -> tuple:
    """
    Create paginated response with metadata.

    Args:
        items: List of items for current page
        total: Total number of items
        page: Current page number
        per_page: Items per page
        endpoint: Optional endpoint name for pagination links

    Returns:
        Success response with pagination meta
    """
    total_pages = (total + per_page - 1) // per_page

    meta = {
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1
        }
    }

    # Add pagination links if endpoint provided
    if endpoint:
        base_url = request.base_url
        meta["pagination"]["links"] = {
            "self": f"{base_url}?page={page}&per_page={per_page}",
            "first": f"{base_url}?page=1&per_page={per_page}",
            "last": f"{base_url}?page={total_pages}&per_page={per_page}"
        }

        if page < total_pages:
            meta["pagination"]["links"]["next"] = f"{base_url}?page={page + 1}&per_page={per_page}"

        if page > 1:
            meta["pagination"]["links"]["prev"] = f"{base_url}?page={page - 1}&per_page={per_page}"

    return success_response(items, meta=meta)


def async_endpoint(f: Callable) -> Callable:
    """
    Decorator for async endpoints with automatic error handling and timing.

    Usage:
        @app.route('/endpoint')
        @async_endpoint
        async def my_endpoint():
            return success_response({"data": "value"})
    """
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        start_time = time.time()

        try:
            result = await f(*args, **kwargs)

            # Log successful request
            execution_time = int((time.time() - start_time) * 1000)
            logger.info(
                f"Request to {f.__name__} completed successfully",
                extra={
                    'event_type': 'API_REQUEST',
                    'action': f.__name__,
                    'result': 'SUCCESS',
                    'execution_time': execution_time
                }
            )

            return result

        except ValueError as e:
            # Client error (bad request)
            execution_time = int((time.time() - start_time) * 1000)
            logger.warning(
                f"Request to {f.__name__} failed: {str(e)}",
                extra={
                    'event_type': 'API_REQUEST',
                    'action': f.__name__,
                    'result': 'CLIENT_ERROR',
                    'execution_time': execution_time
                }
            )
            return error_response(str(e), status_code=400)

        except PermissionError as e:
            # Authorization error
            execution_time = int((time.time() - start_time) * 1000)
            logger.warning(
                f"Request to {f.__name__} forbidden: {str(e)}",
                extra={
                    'event_type': 'API_REQUEST',
                    'action': f.__name__,
                    'result': 'FORBIDDEN',
                    'execution_time': execution_time
                }
            )
            return error_response(str(e), status_code=403)

        except Exception as e:
            # Server error
            execution_time = int((time.time() - start_time) * 1000)
            logger.error(
                f"Request to {f.__name__} failed with exception: {str(e)}",
                extra={
                    'event_type': 'API_REQUEST',
                    'action': f.__name__,
                    'result': 'ERROR',
                    'execution_time': execution_time
                },
                exc_info=True
            )
            return error_response("Internal server error", status_code=500)

    return decorated_function


def auth_required(f: Callable) -> Callable:
    """
    Decorator to require authentication for endpoint.

    Checks for JWT token in Authorization header or API key in X-API-Key header.

    Usage:
        @app.route('/protected')
        @auth_required
        async def protected_endpoint():
            current_user = request.current_user
            return success_response({"user": current_user})
    """
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        from .auth import verify_jwt_token, verify_api_key_async

        # Get token from Authorization header
        auth_header = request.headers.get('Authorization')
        api_key_header = request.headers.get('X-API-Key')

        user_info = None

        # Try JWT token first
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header[7:]  # Remove 'Bearer ' prefix
            secret_key = _require_secret_key()
            payload = verify_jwt_token(token, secret_key)

            if payload:
                user_info = {
                    'user_id': payload['sub'],
                    'username': payload['username'],
                    'email': payload['email'],
                    'roles': payload.get('roles', []),
                    'auth_type': 'jwt'
                }

        # Try API key if JWT failed
        elif api_key_header:
            from quart import current_app
            dal = current_app.config.get('dal')

            if dal:
                user_info = await verify_api_key_async(api_key_header, dal)
                if user_info:
                    user_info['auth_type'] = 'api_key'

        # Check if authentication succeeded
        if not user_info:
            logger.warning(
                "Unauthorized access attempt",
                extra={
                    'event_type': 'AUTH',
                    'action': f.__name__,
                    'result': 'UNAUTHORIZED'
                }
            )
            return error_response(
                "Authentication required",
                status_code=401,
                error_code="UNAUTHORIZED"
            )

        # Attach user info to request
        request.current_user = user_info

        # Log successful authentication
        logger.info(
            f"Authenticated request to {f.__name__}",
            extra={
                'event_type': 'AUTH',
                'user': user_info['username'],
                'action': f.__name__,
                'result': 'SUCCESS'
            }
        )

        return await f(*args, **kwargs)

    return decorated_function


def rate_limit(requests_per_minute: int = 60):
    """
    Decorator to apply rate limiting to endpoint.

    Args:
        requests_per_minute: Maximum requests allowed per minute

    Usage:
        @app.route('/limited')
        @rate_limit(requests_per_minute=30)
        async def limited_endpoint():
            return success_response({"data": "value"})
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def decorated_function(*args, **kwargs):
            from quart import current_app

            # Get rate limiter from app
            rate_limiter = current_app.config.get('rate_limiter')

            if not rate_limiter:
                # No rate limiter configured, skip check
                return await f(*args, **kwargs)

            # Build rate limit key from user and endpoint
            user_info = getattr(request, 'current_user', None)
            user_id = user_info['user_id'] if user_info else request.remote_addr

            rate_key = f"rate_limit:{user_id}:{f.__name__}"

            # Check rate limit
            is_allowed = await rate_limiter.check_rate_limit(
                key=rate_key,
                limit=requests_per_minute,
                window_seconds=60
            )

            if not is_allowed:
                logger.warning(
                    f"Rate limit exceeded for {user_id} on {f.__name__}",
                    extra={
                        'event_type': 'RATE_LIMIT',
                        'user': user_id,
                        'action': f.__name__,
                        'result': 'EXCEEDED'
                    }
                )
                return error_response(
                    "Rate limit exceeded. Please try again later.",
                    status_code=429,
                    error_code="RATE_LIMIT_EXCEEDED"
                )

            return await f(*args, **kwargs)

        return decorated_function
    return decorator


# validate_request decorator has been REMOVED in favor of the new Pydantic-based
# validation system in flask_core.validation (validate_json, validate_query, validate_form).
# See flask_core/validation.py for the replacement implementation.


def cors_headers(
    origins: List[str] = ["*"],
    methods: List[str] = ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    headers: List[str] = ["Content-Type", "Authorization", "X-API-Key"]
):
    """
    Decorator to add CORS headers to response.

    Args:
        origins: Allowed origins
        methods: Allowed HTTP methods
        headers: Allowed headers

    Usage:
        @app.route('/api/data')
        @cors_headers(origins=['https://example.com'])
        async def get_data():
            return success_response({"data": "value"})
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def decorated_function(*args, **kwargs):
            # Handle preflight request
            if request.method == 'OPTIONS':
                response = jsonify({"status": "ok"})
            else:
                response = await f(*args, **kwargs)
                if not isinstance(response, tuple):
                    response = (response, 200)

            # Add CORS headers
            if isinstance(response, tuple):
                resp, status_code = response
            else:
                resp = response
                status_code = 200

            # Convert to Response object if needed
            if not hasattr(resp, 'headers'):
                resp = jsonify(resp) if not isinstance(resp, str) else resp

            resp.headers['Access-Control-Allow-Origin'] = ', '.join(origins)
            resp.headers['Access-Control-Allow-Methods'] = ', '.join(methods)
            resp.headers['Access-Control-Allow-Headers'] = ', '.join(headers)
            resp.headers['Access-Control-Max-Age'] = '3600'

            return resp, status_code

        return decorated_function
    return decorator
