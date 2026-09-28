"""
Custom Prometheus Metrics
==========================

Business metrics and custom instrumentation for Waddles microservices.

Provides:
- Counter metrics (total commands, errors, etc.)
- Gauge metrics (active channels, concurrent users, etc.)
- Histogram metrics (request latency, processing time, etc.)
- Summary metrics (percentiles)
- Custom business metrics
- Integration with existing /metrics endpoint
"""

import time
import logging
from typing import Optional, Dict, Any, List, Callable
from functools import wraps
from datetime import datetime
from collections import defaultdict
from threading import Lock

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        Summary,
        Info,
        CollectorRegistry,
        REGISTRY,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    # Fallback implementations for when prometheus_client is not available
    class DummyMetric:
        def labels(self, *args, **kwargs):
            return self
        def inc(self, *args, **kwargs):
            pass
        def dec(self, *args, **kwargs):
            pass
        def set(self, *args, **kwargs):
            pass
        def observe(self, *args, **kwargs):
            pass
        def info(self, *args, **kwargs):
            pass
        def time(self):
            return DummyTimer()

    class DummyTimer:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    Counter = Gauge = Histogram = Summary = Info = DummyMetric
    CollectorRegistry = object
    REGISTRY = None

logger = logging.getLogger(__name__)


class MetricsManager:
    """
    Manager for custom Prometheus metrics with business-specific instrumentation.
    """

    def __init__(
        self,
        service_name: str,
        service_version: str,
        namespace: str = "waddlebot",
        registry: Optional[CollectorRegistry] = None,
        enable_default_metrics: bool = True
    ):
        """
        Initialize metrics manager.

        Args:
            service_name: Name of the service
            service_version: Version of the service
            namespace: Metrics namespace prefix
            registry: Optional custom registry (uses global if None)
            enable_default_metrics: Enable default business metrics
        """
        self.service_name = service_name
        self.service_version = service_version
        self.namespace = namespace
        self.registry = registry or REGISTRY
        self.enable_default_metrics = enable_default_metrics

        # Thread-safe metrics storage
        self._lock = Lock()
        self._custom_counters: Dict[str, Counter] = {}
        self._custom_gauges: Dict[str, Gauge] = {}
        self._custom_histograms: Dict[str, Histogram] = {}
        self._custom_summaries: Dict[str, Summary] = {}

        # Initialize default metrics
        if self.enable_default_metrics:
            self._init_default_metrics()

        logger.info(f"Metrics manager initialized for {service_name}:{service_version}")

    def _init_default_metrics(self):
        """Initialize default Waddles business metrics."""

        # Service info
        self.service_info = Info(
            f'{self.namespace}_service_info',
            'Service information',
            registry=self.registry
        )
        self.service_info.info({
            'service': self.service_name,
            'version': self.service_version
        })

        # Command metrics
        self.commands_total = Counter(
            f'{self.namespace}_commands_total',
            'Total number of commands processed',
            ['service', 'command', 'platform', 'status'],
            registry=self.registry
        )

        self.command_duration_seconds = Histogram(
            f'{self.namespace}_command_duration_seconds',
            'Command processing duration in seconds',
            ['service', 'command', 'platform'],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=self.registry
        )

        # Channel/Server metrics
        self.active_channels = Gauge(
            f'{self.namespace}_active_channels',
            'Number of active channels/servers',
            ['service', 'platform'],
            registry=self.registry
        )

        self.messages_total = Counter(
            f'{self.namespace}_messages_total',
            'Total number of messages processed',
            ['service', 'platform', 'message_type'],
            registry=self.registry
        )

        # User metrics
        self.active_users = Gauge(
            f'{self.namespace}_active_users',
            'Number of active users',
            ['service', 'platform'],
            registry=self.registry
        )

        self.user_commands_total = Counter(
            f'{self.namespace}_user_commands_total',
            'Total commands per user',
            ['service', 'user_id', 'command'],
            registry=self.registry
        )

        # Error metrics
        self.errors_total = Counter(
            f'{self.namespace}_errors_total',
            'Total number of errors',
            ['service', 'error_type', 'severity'],
            registry=self.registry
        )

        # Database metrics
        self.db_queries_total = Counter(
            f'{self.namespace}_db_queries_total',
            'Total number of database queries',
            ['service', 'operation', 'table', 'status'],
            registry=self.registry
        )

        self.db_query_duration_seconds = Histogram(
            f'{self.namespace}_db_query_duration_seconds',
            'Database query duration in seconds',
            ['service', 'operation', 'table'],
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
            registry=self.registry
        )

        self.db_connections = Gauge(
            f'{self.namespace}_db_connections',
            'Number of active database connections',
            ['service', 'pool', 'state'],
            registry=self.registry
        )

        # Cache metrics
        self.cache_operations_total = Counter(
            f'{self.namespace}_cache_operations_total',
            'Total number of cache operations',
            ['service', 'operation', 'status'],
            registry=self.registry
        )

        self.cache_hit_ratio = Gauge(
            f'{self.namespace}_cache_hit_ratio',
            'Cache hit ratio (0.0 to 1.0)',
            ['service', 'cache_name'],
            registry=self.registry
        )

        # API metrics
        self.http_requests_total = Counter(
            f'{self.namespace}_http_requests_total',
            'Total number of HTTP requests',
            ['service', 'method', 'endpoint', 'status'],
            registry=self.registry
        )

        self.http_request_duration_seconds = Histogram(
            f'{self.namespace}_http_request_duration_seconds',
            'HTTP request duration in seconds',
            ['service', 'method', 'endpoint'],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
            registry=self.registry
        )

        self.http_requests_in_progress = Gauge(
            f'{self.namespace}_http_requests_in_progress',
            'Number of HTTP requests currently being processed',
            ['service', 'method', 'endpoint'],
            registry=self.registry
        )

        # Queue metrics
        self.queue_size = Gauge(
            f'{self.namespace}_queue_size',
            'Number of items in queue',
            ['service', 'queue_name'],
            registry=self.registry
        )

        self.queue_processing_duration_seconds = Histogram(
            f'{self.namespace}_queue_processing_duration_seconds',
            'Queue item processing duration in seconds',
            ['service', 'queue_name'],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
            registry=self.registry
        )

        # Circuit breaker metrics
        self.circuit_breaker_state = Gauge(
            f'{self.namespace}_circuit_breaker_state',
            'Circuit breaker state (0=closed, 1=open, 2=half_open)',
            ['service', 'circuit_name'],
            registry=self.registry
        )

        self.circuit_breaker_failures_total = Counter(
            f'{self.namespace}_circuit_breaker_failures_total',
            'Total number of circuit breaker failures',
            ['service', 'circuit_name'],
            registry=self.registry
        )

        # Rate limiter metrics
        self.rate_limit_exceeded_total = Counter(
            f'{self.namespace}_rate_limit_exceeded_total',
            'Total number of rate limit violations',
            ['service', 'resource', 'user'],
            registry=self.registry
        )

        # Business metrics
        self.communities_total = Gauge(
            f'{self.namespace}_communities_total',
            'Total number of communities',
            ['service'],
            registry=self.registry
        )

        self.premium_users_total = Gauge(
            f'{self.namespace}_premium_users_total',
            'Total number of premium users',
            ['service'],
            registry=self.registry
        )

        self.module_executions_total = Counter(
            f'{self.namespace}_module_executions_total',
            'Total number of module executions',
            ['service', 'module_type', 'module_name', 'status'],
            registry=self.registry
        )

    def create_counter(
        self,
        name: str,
        description: str,
        labels: Optional[List[str]] = None
    ) -> Counter:
        """
        Create a custom counter metric.

        Args:
            name: Metric name
            description: Metric description
            labels: Optional list of label names

        Returns:
            Counter instance
        """
        with self._lock:
            if name in self._custom_counters:
                return self._custom_counters[name]

            metric_name = f'{self.namespace}_{name}'
            counter = Counter(
                metric_name,
                description,
                labels or [],
                registry=self.registry
            )
            self._custom_counters[name] = counter
            return counter

    def create_gauge(
        self,
        name: str,
        description: str,
        labels: Optional[List[str]] = None
    ) -> Gauge:
        """
        Create a custom gauge metric.

        Args:
            name: Metric name
            description: Metric description
            labels: Optional list of label names

        Returns:
            Gauge instance
        """
        with self._lock:
            if name in self._custom_gauges:
                return self._custom_gauges[name]

            metric_name = f'{self.namespace}_{name}'
            gauge = Gauge(
                metric_name,
                description,
                labels or [],
                registry=self.registry
            )
            self._custom_gauges[name] = gauge
            return gauge

    def create_histogram(
        self,
        name: str,
        description: str,
        labels: Optional[List[str]] = None,
        buckets: Optional[tuple] = None
    ) -> Histogram:
        """
        Create a custom histogram metric.

        Args:
            name: Metric name
            description: Metric description
            labels: Optional list of label names
            buckets: Optional bucket boundaries

        Returns:
            Histogram instance
        """
        with self._lock:
            if name in self._custom_histograms:
                return self._custom_histograms[name]

            metric_name = f'{self.namespace}_{name}'
            kwargs = {
                'name': metric_name,
                'documentation': description,
                'labelnames': labels or [],
                'registry': self.registry
            }
            if buckets:
                kwargs['buckets'] = buckets

            histogram = Histogram(**kwargs)
            self._custom_histograms[name] = histogram
            return histogram

    def create_summary(
        self,
        name: str,
        description: str,
        labels: Optional[List[str]] = None
    ) -> Summary:
        """
        Create a custom summary metric.

        Args:
            name: Metric name
            description: Metric description
            labels: Optional list of label names

        Returns:
            Summary instance
        """
        with self._lock:
            if name in self._custom_summaries:
                return self._custom_summaries[name]

            metric_name = f'{self.namespace}_{name}'
            summary = Summary(
                metric_name,
                description,
                labels or [],
                registry=self.registry
            )
            self._custom_summaries[name] = summary
            return summary

    def track_command(
        self,
        command: str,
        platform: str = "unknown",
        status: str = "success"
    ):
        """
        Track a command execution.

        Args:
            command: Command name
            platform: Platform (twitch, discord, slack, etc.)
            status: Status (success, error, timeout, etc.)
        """
        self.commands_total.labels(
            service=self.service_name,
            command=command,
            platform=platform,
            status=status
        ).inc()

    def track_command_duration(
        self,
        command: str,
        duration_seconds: float,
        platform: str = "unknown"
    ):
        """
        Track command processing duration.

        Args:
            command: Command name
            duration_seconds: Duration in seconds
            platform: Platform
        """
        self.command_duration_seconds.labels(
            service=self.service_name,
            command=command,
            platform=platform
        ).observe(duration_seconds)

    def track_message(
        self,
        platform: str,
        message_type: str = "chat"
    ):
        """
        Track a message processed.

        Args:
            platform: Platform (twitch, discord, slack, etc.)
            message_type: Message type (chat, event, webhook, etc.)
        """
        self.messages_total.labels(
            service=self.service_name,
            platform=platform,
            message_type=message_type
        ).inc()

    def track_error(
        self,
        error_type: str,
        severity: str = "error"
    ):
        """
        Track an error occurrence.

        Args:
            error_type: Type of error
            severity: Severity (warning, error, critical)
        """
        self.errors_total.labels(
            service=self.service_name,
            error_type=error_type,
            severity=severity
        ).inc()

    def track_db_query(
        self,
        operation: str,
        table: str,
        duration_seconds: float,
        status: str = "success"
    ):
        """
        Track a database query.

        Args:
            operation: Operation type (select, insert, update, delete)
            table: Table name
            duration_seconds: Query duration in seconds
            status: Query status (success, error, timeout)
        """
        self.db_queries_total.labels(
            service=self.service_name,
            operation=operation,
            table=table,
            status=status
        ).inc()

        self.db_query_duration_seconds.labels(
            service=self.service_name,
            operation=operation,
            table=table
        ).observe(duration_seconds)

    def track_http_request(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        duration_seconds: float
    ):
        """
        Track an HTTP request.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: Endpoint path
            status_code: HTTP status code
            duration_seconds: Request duration in seconds
        """
        self.http_requests_total.labels(
            service=self.service_name,
            method=method,
            endpoint=endpoint,
            status=str(status_code)
        ).inc()

        self.http_request_duration_seconds.labels(
            service=self.service_name,
            method=method,
            endpoint=endpoint
        ).observe(duration_seconds)

    def measure_time(self, metric_name: str, **labels):
        """
        Context manager to measure execution time.

        Args:
            metric_name: Name of the histogram metric to update
            **labels: Label values

        Usage:
            with metrics_manager.measure_time('command_duration_seconds', command='help'):
                # ... do work ...
        """
        class TimerContext:
            def __init__(ctx_self, manager, name, labels):
                ctx_self.manager = manager
                ctx_self.name = name
                ctx_self.labels = labels
                ctx_self.start_time = None

            def __enter__(ctx_self):
                ctx_self.start_time = time.time()
                return ctx_self

            def __exit__(ctx_self, exc_type, exc_val, exc_tb):
                duration = time.time() - ctx_self.start_time

                # Try to find histogram in custom or default metrics
                if ctx_self.name in ctx_self.manager._custom_histograms:
                    metric = ctx_self.manager._custom_histograms[ctx_self.name]
                    if ctx_self.labels:
                        metric.labels(**ctx_self.labels).observe(duration)
                    else:
                        metric.observe(duration)

        return TimerContext(self, metric_name, labels)

    def track_decorator(
        self,
        metric_type: str = "histogram",
        metric_name: Optional[str] = None,
        labels: Optional[Dict[str, Any]] = None
    ):
        """
        Decorator to track function execution metrics.

        Args:
            metric_type: Type of metric (histogram, counter)
            metric_name: Name of metric (defaults to function name)
            labels: Optional labels to add

        Usage:
            @metrics_manager.track_decorator(metric_type='histogram')
            async def process_command(command: str):
                # ... do work ...
        """
        def decorator(func: Callable) -> Callable:
            name = metric_name or f"{func.__name__}_duration_seconds"
            func_labels = labels or {}

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = await func(*args, **kwargs)
                    status = "success"
                    return result
                except Exception as e:
                    status = "error"
                    raise
                finally:
                    duration = time.time() - start_time

                    if metric_type == "histogram":
                        metric = self.create_histogram(
                            name,
                            f"Duration of {func.__name__}",
                            list(func_labels.keys()) + ['status']
                        )
                        metric.labels(**func_labels, status=status).observe(duration)
                    elif metric_type == "counter":
                        metric = self.create_counter(
                            name,
                            f"Count of {func.__name__} calls",
                            list(func_labels.keys()) + ['status']
                        )
                        metric.labels(**func_labels, status=status).inc()

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = func(*args, **kwargs)
                    status = "success"
                    return result
                except Exception as e:
                    status = "error"
                    raise
                finally:
                    duration = time.time() - start_time

                    if metric_type == "histogram":
                        metric = self.create_histogram(
                            name,
                            f"Duration of {func.__name__}",
                            list(func_labels.keys()) + ['status']
                        )
                        metric.labels(**func_labels, status=status).observe(duration)
                    elif metric_type == "counter":
                        metric = self.create_counter(
                            name,
                            f"Count of {func.__name__} calls",
                            list(func_labels.keys()) + ['status']
                        )
                        metric.labels(**func_labels, status=status).inc()

            # Return appropriate wrapper based on function type
            import asyncio
            if asyncio.iscoroutinefunction(func):
                return async_wrapper
            else:
                return sync_wrapper

        return decorator

    def export_metrics(self) -> bytes:
        """
        Export metrics in Prometheus text format.

        Returns:
            Metrics in Prometheus format
        """
        if PROMETHEUS_AVAILABLE:
            return generate_latest(self.registry)
        else:
            return b"# Prometheus client not available\n"

    def get_content_type(self) -> str:
        """
        Get content type for metrics export.

        Returns:
            Content type string
        """
        if PROMETHEUS_AVAILABLE:
            return CONTENT_TYPE_LATEST
        else:
            return "text/plain"


def create_metrics_manager(
    service_name: str,
    service_version: str,
    namespace: Optional[str] = None,
    enable_default_metrics: Optional[bool] = None
) -> MetricsManager:
    """
    Factory function to create a MetricsManager with environment-based defaults.

    Args:
        service_name: Name of the service
        service_version: Version of the service
        namespace: Metrics namespace prefix
        enable_default_metrics: Enable default business metrics

    Returns:
        Configured MetricsManager instance

    Environment Variables:
        METRICS_NAMESPACE: Metrics namespace (default: waddlebot)
        METRICS_ENABLED: Enable metrics collection (default: true)
    """
    import os

    metrics_enabled = os.getenv('METRICS_ENABLED', 'true').lower() == 'true'

    return MetricsManager(
        service_name=service_name,
        service_version=service_version,
        namespace=namespace or os.getenv('METRICS_NAMESPACE', 'waddlebot'),
        enable_default_metrics=(
            enable_default_metrics if enable_default_metrics is not None
            else metrics_enabled
        )
    )


# Global instance
_global_metrics_manager: Optional[MetricsManager] = None


def init_metrics(
    service_name: str,
    service_version: str,
    **kwargs
) -> MetricsManager:
    """
    Initialize global metrics manager.

    Args:
        service_name: Name of the service
        service_version: Version of the service
        **kwargs: Additional arguments for create_metrics_manager

    Returns:
        MetricsManager instance
    """
    global _global_metrics_manager
    _global_metrics_manager = create_metrics_manager(service_name, service_version, **kwargs)
    return _global_metrics_manager


def get_metrics_manager() -> Optional[MetricsManager]:
    """
    Get the global metrics manager.

    Returns:
        Global MetricsManager instance or None
    """
    return _global_metrics_manager
