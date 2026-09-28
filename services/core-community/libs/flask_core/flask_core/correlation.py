"""
Correlation ID Middleware
==========================

Request correlation tracking for distributed tracing across Waddles microservices.

Provides:
- Automatic correlation ID generation
- Correlation ID propagation across service calls
- Integration with logging (all logs include correlation ID)
- Integration with OpenTelemetry tracing
- Support for custom header names
"""

import uuid
import logging
from typing import Optional, Dict, Any
from functools import wraps
from contextvars import ContextVar

try:
    from quart import request, g
    QUART_AVAILABLE = True
except ImportError:
    QUART_AVAILABLE = False

logger = logging.getLogger(__name__)

# Context variable to store correlation ID across async calls
_correlation_id: ContextVar[Optional[str]] = ContextVar('correlation_id', default=None)
_request_id: ContextVar[Optional[str]] = ContextVar('request_id', default=None)


class CorrelationIDManager:
    """
    Manager for correlation ID generation and propagation.
    """

    def __init__(
        self,
        correlation_header: str = "X-Correlation-ID",
        request_header: str = "X-Request-ID",
        generate_request_id: bool = True,
        validate_uuid: bool = False
    ):
        """
        Initialize correlation ID manager.

        Args:
            correlation_header: HTTP header name for correlation ID
            request_header: HTTP header name for request ID
            generate_request_id: Generate new request ID for each request
            validate_uuid: Validate that IDs are valid UUIDs
        """
        self.correlation_header = correlation_header
        self.request_header = request_header
        self.generate_request_id = generate_request_id
        self.validate_uuid = validate_uuid

    def generate_id(self) -> str:
        """
        Generate a new correlation/request ID.

        Returns:
            New UUID string
        """
        return str(uuid.uuid4())

    def validate_id(self, id_value: str) -> bool:
        """
        Validate that an ID is a valid UUID.

        Args:
            id_value: ID to validate

        Returns:
            True if valid UUID, False otherwise
        """
        if not self.validate_uuid:
            return True

        try:
            uuid.UUID(id_value)
            return True
        except (ValueError, AttributeError):
            return False

    def get_correlation_id(self) -> Optional[str]:
        """
        Get the current correlation ID from context.

        Returns:
            Current correlation ID or None
        """
        return _correlation_id.get()

    def get_request_id(self) -> Optional[str]:
        """
        Get the current request ID from context.

        Returns:
            Current request ID or None
        """
        return _request_id.get()

    def set_correlation_id(self, correlation_id: str):
        """
        Set the correlation ID in context.

        Args:
            correlation_id: Correlation ID to set
        """
        _correlation_id.set(correlation_id)

    def set_request_id(self, request_id: str):
        """
        Set the request ID in context.

        Args:
            request_id: Request ID to set
        """
        _request_id.set(request_id)

    async def extract_ids_from_request(self) -> Dict[str, str]:
        """
        Extract correlation and request IDs from HTTP request headers.

        Returns:
            Dictionary with correlation_id and request_id
        """
        if not QUART_AVAILABLE:
            return {
                'correlation_id': self.generate_id(),
                'request_id': self.generate_id()
            }

        # Extract from headers
        correlation_id = request.headers.get(self.correlation_header)
        request_id = request.headers.get(self.request_header)

        # Validate and generate if needed
        if correlation_id and self.validate_id(correlation_id):
            correlation_id = correlation_id
        else:
            correlation_id = self.generate_id()

        if self.generate_request_id or not request_id:
            request_id = self.generate_id()
        elif request_id and self.validate_id(request_id):
            request_id = request_id
        else:
            request_id = self.generate_id()

        return {
            'correlation_id': correlation_id,
            'request_id': request_id
        }

    def inject_into_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """
        Inject correlation and request IDs into HTTP headers for outgoing requests.

        Args:
            headers: Dictionary of HTTP headers

        Returns:
            Headers dictionary with correlation/request IDs added
        """
        correlation_id = self.get_correlation_id()
        request_id = self.get_request_id()

        if correlation_id:
            headers[self.correlation_header] = correlation_id

        if request_id:
            headers[self.request_header] = request_id

        return headers

    def create_middleware(self, app):
        """
        Create middleware for Flask/Quart application.

        Args:
            app: Flask or Quart application instance
        """

        @app.before_request
        async def before_request():
            """Extract and set correlation/request IDs before each request."""
            ids = await self.extract_ids_from_request()

            # Set in context
            self.set_correlation_id(ids['correlation_id'])
            self.set_request_id(ids['request_id'])

            # Also store in g for easy access
            if QUART_AVAILABLE:
                g.correlation_id = ids['correlation_id']
                g.request_id = ids['request_id']

            # Add to OpenTelemetry span if available
            try:
                from opentelemetry import trace
                span = trace.get_current_span()
                if span:
                    span.set_attribute("correlation_id", ids['correlation_id'])
                    span.set_attribute("request_id", ids['request_id'])
            except ImportError:
                pass

        @app.after_request
        async def after_request(response):
            """Add correlation/request IDs to response headers."""
            correlation_id = self.get_correlation_id()
            request_id = self.get_request_id()

            if correlation_id:
                response.headers[self.correlation_header] = correlation_id

            if request_id:
                response.headers[self.request_header] = request_id

            return response

        logger.info(f"Correlation ID middleware registered for {app.name}")

    def track_request(self, func):
        """
        Decorator to track correlation/request IDs for a function.

        Usage:
            @correlation_manager.track_request
            async def process_command(command: str):
                correlation_id = correlation_manager.get_correlation_id()
                # ... do work ...
        """
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Generate IDs if not in request context
            if not self.get_correlation_id():
                self.set_correlation_id(self.generate_id())

            if not self.get_request_id():
                self.set_request_id(self.generate_id())

            return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            # Generate IDs if not in request context
            if not self.get_correlation_id():
                self.set_correlation_id(self.generate_id())

            if not self.get_request_id():
                self.set_request_id(self.generate_id())

            return func(*args, **kwargs)

        # Return appropriate wrapper based on function type
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

        return wrapper


class CorrelationIDFilter(logging.Filter):
    """
    Logging filter to inject correlation and request IDs into log records.
    """

    def __init__(
        self,
        correlation_manager: CorrelationIDManager,
        include_in_message: bool = False
    ):
        """
        Initialize correlation ID filter.

        Args:
            correlation_manager: CorrelationIDManager instance
            include_in_message: Include IDs in log message (not just extra fields)
        """
        super().__init__()
        self.correlation_manager = correlation_manager
        self.include_in_message = include_in_message

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Add correlation and request IDs to log record.

        Args:
            record: Log record to filter

        Returns:
            Always True (don't filter out any records)
        """
        # Get IDs from context
        correlation_id = self.correlation_manager.get_correlation_id() or "N/A"
        request_id = self.correlation_manager.get_request_id() or "N/A"

        # Add as extra fields
        record.correlation_id = correlation_id
        record.request_id = request_id

        # Optionally include in message
        if self.include_in_message:
            original_msg = record.getMessage()
            record.msg = f"[cid={correlation_id}] [rid={request_id}] {original_msg}"
            record.args = ()

        return True


class CorrelationIDFormatter(logging.Formatter):
    """
    Logging formatter that includes correlation and request IDs.
    """

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        style: str = '%'
    ):
        """
        Initialize formatter.

        Args:
            fmt: Format string (supports %(correlation_id)s and %(request_id)s)
            datefmt: Date format string
            style: Format style ('%', '{', or '$')
        """
        if fmt is None:
            fmt = (
                '[%(asctime)s] %(levelname)s [cid=%(correlation_id)s] [rid=%(request_id)s] '
                '%(name)s: %(message)s'
            )

        super().__init__(fmt=fmt, datefmt=datefmt, style=style)

    def format(self, record: logging.LogRecord) -> str:
        """
        Format log record with correlation/request IDs.

        Args:
            record: Log record to format

        Returns:
            Formatted log string
        """
        # Ensure correlation_id and request_id exist
        if not hasattr(record, 'correlation_id'):
            record.correlation_id = "N/A"
        if not hasattr(record, 'request_id'):
            record.request_id = "N/A"

        return super().format(record)


def create_correlation_manager(
    correlation_header: Optional[str] = None,
    request_header: Optional[str] = None,
    generate_request_id: Optional[bool] = None,
    validate_uuid: Optional[bool] = None
) -> CorrelationIDManager:
    """
    Factory function to create a CorrelationIDManager with environment-based defaults.

    Args:
        correlation_header: HTTP header name for correlation ID
        request_header: HTTP header name for request ID
        generate_request_id: Generate new request ID for each request
        validate_uuid: Validate that IDs are valid UUIDs

    Returns:
        Configured CorrelationIDManager instance

    Environment Variables:
        CORRELATION_HEADER: Header name for correlation ID (default: X-Correlation-ID)
        REQUEST_HEADER: Header name for request ID (default: X-Request-ID)
        GENERATE_REQUEST_ID: Generate request IDs (default: true)
        VALIDATE_UUID: Validate UUIDs (default: false)
    """
    import os

    return CorrelationIDManager(
        correlation_header=correlation_header or os.getenv('CORRELATION_HEADER', 'X-Correlation-ID'),
        request_header=request_header or os.getenv('REQUEST_HEADER', 'X-Request-ID'),
        generate_request_id=(
            generate_request_id if generate_request_id is not None
            else os.getenv('GENERATE_REQUEST_ID', 'true').lower() == 'true'
        ),
        validate_uuid=(
            validate_uuid if validate_uuid is not None
            else os.getenv('VALIDATE_UUID', 'false').lower() == 'true'
        )
    )


def setup_correlation_logging(
    correlation_manager: CorrelationIDManager,
    logger_name: Optional[str] = None,
    include_in_message: bool = False
):
    """
    Setup correlation ID logging for a logger.

    Args:
        correlation_manager: CorrelationIDManager instance
        logger_name: Logger name (None for root logger)
        include_in_message: Include IDs in log message

    Returns:
        Logger instance with correlation filter added
    """
    target_logger = logging.getLogger(logger_name)

    # Add correlation filter
    correlation_filter = CorrelationIDFilter(
        correlation_manager,
        include_in_message=include_in_message
    )
    target_logger.addFilter(correlation_filter)

    return target_logger


# Global instance
_global_correlation_manager: Optional[CorrelationIDManager] = None


def init_correlation(
    app=None,
    **kwargs
) -> CorrelationIDManager:
    """
    Initialize global correlation ID manager.

    Args:
        app: Optional Flask/Quart app to register middleware
        **kwargs: Additional arguments for create_correlation_manager

    Returns:
        CorrelationIDManager instance
    """
    global _global_correlation_manager
    _global_correlation_manager = create_correlation_manager(**kwargs)

    if app:
        _global_correlation_manager.create_middleware(app)

    return _global_correlation_manager


def get_correlation_manager() -> Optional[CorrelationIDManager]:
    """
    Get the global correlation ID manager.

    Returns:
        Global CorrelationIDManager instance or None
    """
    return _global_correlation_manager


def get_correlation_id() -> Optional[str]:
    """
    Get the current correlation ID (convenience function).

    Returns:
        Current correlation ID or None
    """
    return _correlation_id.get()


def get_request_id() -> Optional[str]:
    """
    Get the current request ID (convenience function).

    Returns:
        Current request ID or None
    """
    return _request_id.get()
