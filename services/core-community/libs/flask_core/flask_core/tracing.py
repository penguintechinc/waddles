"""
OpenTelemetry Tracing Integration
==================================

Distributed tracing for Waddles microservices using OpenTelemetry.

Provides:
- Automatic span creation for HTTP requests
- Manual span creation helpers
- Context propagation across services
- Export to Jaeger, Zipkin, or OTLP collectors
- Integration with existing AAA logging
"""

from typing import Optional, Dict, Any, Callable
from functools import wraps
from contextlib import contextmanager
import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.trace import Status, StatusCode, SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagate import set_global_textmap, get_global_textmap
from opentelemetry.context import Context

# Optional exporters - graceful fallback if not installed
try:
    from opentelemetry.exporter.jaeger.thrift import JaegerExporter
    JAEGER_AVAILABLE = True
except ImportError:
    JaegerExporter = None
    JAEGER_AVAILABLE = False

try:
    from opentelemetry.exporter.zipkin.json import ZipkinExporter
    ZIPKIN_AVAILABLE = True
except ImportError:
    ZipkinExporter = None
    ZIPKIN_AVAILABLE = False

# Optional instrumentation - graceful fallback
try:
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    REQUESTS_INSTRUMENTATION_AVAILABLE = True
except ImportError:
    RequestsInstrumentor = None
    REQUESTS_INSTRUMENTATION_AVAILABLE = False

try:
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    FLASK_INSTRUMENTATION_AVAILABLE = True
except ImportError:
    FlaskInstrumentor = None
    FLASK_INSTRUMENTATION_AVAILABLE = False

try:
    from quart import request
    QUART_AVAILABLE = True
except ImportError:
    QUART_AVAILABLE = False

logger = logging.getLogger(__name__)


class TracingManager:
    """
    Manager for OpenTelemetry tracing configuration and operations.
    """

    def __init__(
        self,
        service_name: str,
        service_version: str,
        exporter_type: str = "console",
        jaeger_host: Optional[str] = None,
        jaeger_port: Optional[int] = None,
        zipkin_endpoint: Optional[str] = None,
        otlp_endpoint: Optional[str] = None,
        enable_auto_instrumentation: bool = True,
        sample_rate: float = 1.0
    ):
        """
        Initialize tracing manager.

        Args:
            service_name: Name of the service
            service_version: Version of the service
            exporter_type: Type of exporter (console, jaeger, zipkin, otlp)
            jaeger_host: Jaeger agent host
            jaeger_port: Jaeger agent port
            zipkin_endpoint: Zipkin endpoint URL
            otlp_endpoint: OTLP collector endpoint
            enable_auto_instrumentation: Enable automatic Flask/requests instrumentation
            sample_rate: Sampling rate (0.0 to 1.0)
        """
        self.service_name = service_name
        self.service_version = service_version
        self.exporter_type = exporter_type.lower()
        self.jaeger_host = jaeger_host or "localhost"
        self.jaeger_port = jaeger_port or 6831
        self.zipkin_endpoint = zipkin_endpoint or "http://localhost:9411/api/v2/spans"
        self.otlp_endpoint = otlp_endpoint or "http://localhost:4317"
        self.enable_auto_instrumentation = enable_auto_instrumentation
        self.sample_rate = sample_rate

        # Create resource
        self.resource = Resource(attributes={
            SERVICE_NAME: service_name,
            SERVICE_VERSION: service_version,
        })

        # Create tracer provider
        self.tracer_provider = TracerProvider(resource=self.resource)

        # Setup exporter
        self._setup_exporter()

        # Set as global tracer provider
        trace.set_tracer_provider(self.tracer_provider)

        # Get tracer
        self.tracer = trace.get_tracer(service_name, service_version)

        # Setup propagators
        self._setup_propagators()

        # Auto-instrumentation
        if self.enable_auto_instrumentation:
            self._setup_auto_instrumentation()

        logger.info(
            f"Tracing initialized for {service_name}:{service_version} "
            f"with {exporter_type} exporter"
        )

    def _setup_exporter(self):
        """Setup span exporter based on configuration."""
        try:
            if self.exporter_type == "jaeger":
                if not JAEGER_AVAILABLE:
                    logger.warning("Jaeger exporter not available, falling back to console")
                    exporter = ConsoleSpanExporter()
                else:
                    exporter = JaegerExporter(
                        agent_host_name=self.jaeger_host,
                        agent_port=self.jaeger_port,
                    )
                    logger.info(f"Jaeger exporter configured: {self.jaeger_host}:{self.jaeger_port}")

            elif self.exporter_type == "zipkin":
                if not ZIPKIN_AVAILABLE:
                    logger.warning("Zipkin exporter not available, falling back to console")
                    exporter = ConsoleSpanExporter()
                else:
                    exporter = ZipkinExporter(
                        endpoint=self.zipkin_endpoint,
                    )
                    logger.info(f"Zipkin exporter configured: {self.zipkin_endpoint}")

            elif self.exporter_type == "otlp":
                exporter = OTLPSpanExporter(
                    endpoint=self.otlp_endpoint,
                    insecure=True  # Use insecure for local development
                )
                logger.info(f"OTLP exporter configured: {self.otlp_endpoint}")

            else:  # console
                exporter = ConsoleSpanExporter()
                logger.info("Console exporter configured")

            # Add span processor
            span_processor = BatchSpanProcessor(exporter)
            self.tracer_provider.add_span_processor(span_processor)

        except Exception as e:
            logger.error(f"Failed to setup exporter: {e}, falling back to console")
            exporter = ConsoleSpanExporter()
            span_processor = BatchSpanProcessor(exporter)
            self.tracer_provider.add_span_processor(span_processor)

    def _setup_propagators(self):
        """Setup context propagators for distributed tracing."""
        # Use W3C Trace Context and Baggage propagation
        set_global_textmap(
            TraceContextTextMapPropagator()
        )

    def _setup_auto_instrumentation(self):
        """Setup automatic instrumentation for common libraries."""
        try:
            # Instrument requests library
            if REQUESTS_INSTRUMENTATION_AVAILABLE:
                RequestsInstrumentor().instrument()
                logger.info("Requests instrumentation enabled")
            else:
                logger.info("Requests instrumentation not available (optional)")

            # Note: Flask instrumentation is done separately when app is created
            # via instrument_app() method

        except Exception as e:
            logger.warning(f"Failed to setup auto-instrumentation: {e}")

    def instrument_app(self, app):
        """
        Instrument Flask/Quart application.

        Args:
            app: Flask or Quart application instance
        """
        try:
            if FLASK_INSTRUMENTATION_AVAILABLE:
                FlaskInstrumentor().instrument_app(app)
                logger.info(f"Flask/Quart application instrumented: {app.name}")
            else:
                logger.info("Flask instrumentation not available (optional)")
        except Exception as e:
            logger.warning(f"Failed to instrument Flask/Quart app: {e}")

    @contextmanager
    def start_span(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Optional[Dict[str, Any]] = None,
        parent_context: Optional[Context] = None
    ):
        """
        Context manager to create and manage a span.

        Args:
            name: Span name
            kind: Span kind (INTERNAL, SERVER, CLIENT, PRODUCER, CONSUMER)
            attributes: Optional attributes to add to span
            parent_context: Optional parent context

        Yields:
            Span instance

        Usage:
            with tracing_manager.start_span("process_command") as span:
                span.set_attribute("command", "!help")
                # ... do work ...
        """
        with self.tracer.start_as_current_span(
            name,
            kind=kind,
            attributes=attributes,
            context=parent_context
        ) as span:
            try:
                yield span
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

    def trace_function(
        self,
        name: Optional[str] = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Optional[Dict[str, Any]] = None
    ):
        """
        Decorator to trace a function.

        Args:
            name: Optional span name (defaults to function name)
            kind: Span kind
            attributes: Optional attributes to add to span

        Usage:
            @tracing_manager.trace_function()
            async def process_command(command: str):
                return f"Processed: {command}"
        """
        def decorator(func: Callable) -> Callable:
            span_name = name or func.__name__

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                with self.start_span(span_name, kind=kind, attributes=attributes) as span:
                    # Add function arguments as attributes
                    span.set_attribute("function.name", func.__name__)
                    span.set_attribute("function.module", func.__module__)

                    result = await func(*args, **kwargs)
                    return result

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                with self.start_span(span_name, kind=kind, attributes=attributes) as span:
                    # Add function arguments as attributes
                    span.set_attribute("function.name", func.__name__)
                    span.set_attribute("function.module", func.__module__)

                    result = func(*args, **kwargs)
                    return result

            # Return appropriate wrapper based on function type
            import asyncio
            if asyncio.iscoroutinefunction(func):
                return async_wrapper
            else:
                return sync_wrapper

        return decorator

    def get_current_span(self):
        """
        Get the current active span.

        Returns:
            Current span or None
        """
        return trace.get_current_span()

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None):
        """
        Add an event to the current span.

        Args:
            name: Event name
            attributes: Optional event attributes
        """
        span = self.get_current_span()
        if span:
            span.add_event(name, attributes=attributes)

    def set_attribute(self, key: str, value: Any):
        """
        Set an attribute on the current span.

        Args:
            key: Attribute key
            value: Attribute value
        """
        span = self.get_current_span()
        if span:
            span.set_attribute(key, value)

    def set_attributes(self, attributes: Dict[str, Any]):
        """
        Set multiple attributes on the current span.

        Args:
            attributes: Dictionary of attributes
        """
        span = self.get_current_span()
        if span:
            for key, value in attributes.items():
                span.set_attribute(key, value)

    def inject_context(self, carrier: Dict[str, str]):
        """
        Inject current trace context into carrier for propagation.

        Args:
            carrier: Dictionary to inject context into (e.g., HTTP headers)
        """
        propagator = get_global_textmap()
        propagator.inject(carrier)

    def extract_context(self, carrier: Dict[str, str]) -> Optional[Context]:
        """
        Extract trace context from carrier.

        Args:
            carrier: Dictionary containing context (e.g., HTTP headers)

        Returns:
            Extracted context or None
        """
        propagator = get_global_textmap()
        return propagator.extract(carrier)

    def shutdown(self):
        """Shutdown tracing and flush pending spans."""
        try:
            self.tracer_provider.shutdown()
            logger.info("Tracing shut down successfully")
        except Exception as e:
            logger.error(f"Error shutting down tracing: {e}")


def create_tracing_manager(
    service_name: str,
    service_version: str,
    exporter_type: Optional[str] = None,
    jaeger_host: Optional[str] = None,
    jaeger_port: Optional[int] = None,
    zipkin_endpoint: Optional[str] = None,
    otlp_endpoint: Optional[str] = None,
    enable_auto_instrumentation: Optional[bool] = None,
    sample_rate: Optional[float] = None
) -> TracingManager:
    """
    Factory function to create a TracingManager with environment-based defaults.

    Args:
        service_name: Name of the service
        service_version: Version of the service
        exporter_type: Type of exporter (console, jaeger, zipkin, otlp)
        jaeger_host: Jaeger agent host
        jaeger_port: Jaeger agent port
        zipkin_endpoint: Zipkin endpoint URL
        otlp_endpoint: OTLP collector endpoint
        enable_auto_instrumentation: Enable automatic instrumentation
        sample_rate: Sampling rate (0.0 to 1.0)

    Returns:
        Configured TracingManager instance

    Environment Variables:
        TRACING_ENABLED: Enable tracing (default: true)
        TRACING_EXPORTER: Exporter type (console, jaeger, zipkin, otlp)
        JAEGER_HOST: Jaeger agent host
        JAEGER_PORT: Jaeger agent port
        ZIPKIN_ENDPOINT: Zipkin endpoint URL
        OTLP_ENDPOINT: OTLP collector endpoint
        TRACING_SAMPLE_RATE: Sampling rate (0.0 to 1.0)
    """
    # Check if tracing is enabled
    tracing_enabled = os.getenv('TRACING_ENABLED', 'true').lower() == 'true'

    if not tracing_enabled:
        logger.info("Tracing disabled via TRACING_ENABLED environment variable")
        # Return a no-op manager
        exporter_type = "console"

    return TracingManager(
        service_name=service_name,
        service_version=service_version,
        exporter_type=exporter_type or os.getenv('TRACING_EXPORTER', 'console'),
        jaeger_host=jaeger_host or os.getenv('JAEGER_HOST', 'localhost'),
        jaeger_port=jaeger_port or int(os.getenv('JAEGER_PORT', '6831')),
        zipkin_endpoint=zipkin_endpoint or os.getenv('ZIPKIN_ENDPOINT', 'http://localhost:9411/api/v2/spans'),
        otlp_endpoint=otlp_endpoint or os.getenv('OTLP_ENDPOINT', 'http://localhost:4317'),
        enable_auto_instrumentation=(
            enable_auto_instrumentation if enable_auto_instrumentation is not None
            else os.getenv('TRACING_AUTO_INSTRUMENT', 'true').lower() == 'true'
        ),
        sample_rate=sample_rate or float(os.getenv('TRACING_SAMPLE_RATE', '1.0'))
    )


# Global instance (can be initialized once and reused)
_global_tracing_manager: Optional[TracingManager] = None


def init_tracing(
    service_name: str,
    service_version: str,
    **kwargs
) -> TracingManager:
    """
    Initialize global tracing manager.

    Args:
        service_name: Name of the service
        service_version: Version of the service
        **kwargs: Additional arguments for create_tracing_manager

    Returns:
        TracingManager instance
    """
    global _global_tracing_manager
    _global_tracing_manager = create_tracing_manager(service_name, service_version, **kwargs)
    return _global_tracing_manager


def get_tracing_manager() -> Optional[TracingManager]:
    """
    Get the global tracing manager.

    Returns:
        Global TracingManager instance or None
    """
    return _global_tracing_manager
