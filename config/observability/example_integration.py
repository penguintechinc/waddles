"""
Example Waddles Service with Full Observability
==================================================

This example demonstrates how to integrate all three observability components:
1. OpenTelemetry Tracing
2. Correlation IDs
3. Custom Metrics

This is a complete, production-ready example that can be used as a template
for any Waddles module.
"""

import os
import time
from quart import Quart, request, jsonify
from flask_core import (
    # Tracing
    init_tracing,
    get_tracing_manager,
    # Correlation
    init_correlation,
    get_correlation_manager,
    get_correlation_id,
    get_request_id,
    setup_correlation_logging,
    # Metrics
    init_metrics,
    get_metrics_manager,
    # Logging
    setup_aaa_logging,
    # API Utils
    success_response,
    error_response,
    create_health_blueprint,
    async_endpoint,
    auth_required
)

# Module configuration
MODULE_NAME = os.getenv('MODULE_NAME', 'example-service')
VERSION = os.getenv('VERSION', '1.0.0')
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '8000'))

# Create Quart application
app = Quart(__name__)


def init_observability():
    """
    Initialize all observability components.
    This should be called before the app starts.
    """
    print(f"Initializing observability for {MODULE_NAME}:{VERSION}")

    # 1. Initialize Tracing
    tracing = init_tracing(
        service_name=MODULE_NAME,
        service_version=VERSION,
        exporter_type=os.getenv('TRACING_EXPORTER', 'console')
    )
    print(f"✓ Tracing initialized with {os.getenv('TRACING_EXPORTER', 'console')} exporter")

    # 2. Initialize Correlation IDs
    correlation = init_correlation(app)
    print("✓ Correlation ID middleware registered")

    # 3. Initialize Metrics
    metrics = init_metrics(
        service_name=MODULE_NAME,
        service_version=VERSION
    )
    print("✓ Metrics manager initialized")

    # 4. Initialize Logging
    logger = setup_aaa_logging(
        module_name=MODULE_NAME,
        version=VERSION
    )
    print("✓ AAA logging initialized")

    # 5. Integrate correlation with logging
    setup_correlation_logging(correlation)
    print("✓ Correlation logging integrated")

    # 6. Instrument Flask app for automatic tracing
    tracing.instrument_app(app)
    print("✓ Flask app instrumented for tracing")

    return tracing, correlation, metrics, logger


# Initialize observability
tracing, correlation, metrics, logger = init_observability()


# Register health/metrics endpoints
health_bp = create_health_blueprint(MODULE_NAME, VERSION)
app.register_blueprint(health_bp)


@app.before_request
async def before_request_logging():
    """Log all incoming requests with correlation IDs."""
    logger.system(
        f"Incoming request: {request.method} {request.path}",
        correlation_id=get_correlation_id(),
        request_id=get_request_id(),
        method=request.method,
        path=request.path,
        remote_addr=request.remote_addr
    )


@app.after_request
async def after_request_logging(response):
    """Log all outgoing responses."""
    logger.system(
        f"Response: {response.status_code}",
        correlation_id=get_correlation_id(),
        request_id=get_request_id(),
        status_code=response.status_code
    )
    return response


# Example endpoints demonstrating observability features

@app.route('/api/example/simple', methods=['GET'])
@async_endpoint
async def simple_endpoint():
    """
    Simple endpoint - automatic tracing and correlation.
    All requests are automatically traced and correlation IDs are added.
    """
    logger.info("Processing simple request")

    # Track custom metric
    metrics.track_command(
        command="simple",
        platform="http",
        status="success"
    )

    return success_response({
        "message": "Hello from Waddles!",
        "correlation_id": get_correlation_id(),
        "request_id": get_request_id()
    })


@app.route('/api/example/traced', methods=['POST'])
@async_endpoint
async def traced_endpoint():
    """
    Endpoint with manual span creation and attributes.
    Demonstrates how to add custom attributes to traces.
    """
    data = await request.get_json()
    command = data.get('command', 'unknown')

    # Get tracing manager
    tracer = get_tracing_manager()

    # Create manual span for specific operation
    with tracer.start_span("process_command") as span:
        # Add custom attributes to span
        span.set_attribute("command", command)
        span.set_attribute("platform", "http")
        span.set_attribute("correlation_id", get_correlation_id())

        # Simulate processing
        time.sleep(0.1)

        # Add event to span
        tracer.add_event("command_validated", {
            "command": command,
            "valid": True
        })

        # Track metrics
        start_time = time.time()
        result = f"Processed: {command}"
        duration = time.time() - start_time

        metrics.track_command(command, platform="http", status="success")
        metrics.track_command_duration(command, duration, platform="http")

        logger.info(f"Command processed: {command}", command=command, result="SUCCESS")

    return success_response({
        "result": result,
        "correlation_id": get_correlation_id()
    })


@app.route('/api/example/metrics', methods=['GET'])
@async_endpoint
async def metrics_endpoint():
    """
    Endpoint demonstrating custom metrics.
    Shows how to create and update custom metrics.
    """
    metrics_mgr = get_metrics_manager()

    # Create custom counter
    custom_counter = metrics_mgr.create_counter(
        "example_requests",
        "Number of example requests",
        labels=["endpoint"]
    )
    custom_counter.labels(endpoint="metrics").inc()

    # Create custom gauge
    custom_gauge = metrics_mgr.create_gauge(
        "example_active_connections",
        "Active connections",
        labels=["type"]
    )
    custom_gauge.labels(type="http").set(42)

    # Create custom histogram
    custom_histogram = metrics_mgr.create_histogram(
        "example_latency",
        "Request latency",
        labels=["endpoint"],
        buckets=(0.01, 0.05, 0.1, 0.5, 1.0)
    )
    custom_histogram.labels(endpoint="metrics").observe(0.125)

    logger.info("Custom metrics updated")

    return success_response({
        "message": "Custom metrics updated",
        "metrics": {
            "counter": "example_requests",
            "gauge": "example_active_connections",
            "histogram": "example_latency"
        }
    })


@app.route('/api/example/service-call', methods=['POST'])
@async_endpoint
async def service_call_endpoint():
    """
    Endpoint demonstrating service-to-service calls with full context propagation.
    Shows how to propagate tracing and correlation context to downstream services.
    """
    import httpx

    data = await request.get_json()
    target_service = data.get('target_service', 'http://router-service:8000')

    tracer = get_tracing_manager()
    corr_mgr = get_correlation_manager()
    metrics_mgr = get_metrics_manager()

    # Create span for service call
    with tracer.start_span(
        "call_downstream_service",
        kind=trace.SpanKind.CLIENT
    ) as span:
        # Build headers with context
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": "service-key"
        }

        # Inject trace context
        tracer.inject_context(headers)

        # Inject correlation IDs
        corr_mgr.inject_into_headers(headers)

        logger.info(
            f"Calling downstream service: {target_service}",
            target=target_service,
            correlation_id=get_correlation_id()
        )

        # Make HTTP call
        start_time = time.time()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{target_service}/health",
                    headers=headers
                )

                duration = time.time() - start_time

                # Track metrics
                metrics_mgr.track_http_request(
                    method="GET",
                    endpoint="/health",
                    status_code=response.status_code,
                    duration_seconds=duration
                )

                # Add span attributes
                span.set_attribute("http.status_code", response.status_code)
                span.set_attribute("http.url", target_service)
                span.set_attribute("http.duration_ms", duration * 1000)

                logger.info(
                    "Downstream service responded",
                    status_code=response.status_code,
                    duration_ms=int(duration * 1000)
                )

                return success_response({
                    "downstream_response": response.json(),
                    "duration_ms": int(duration * 1000),
                    "correlation_id": get_correlation_id()
                })

        except Exception as e:
            duration = time.time() - start_time

            # Record error in span
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)

            # Track error metrics
            metrics_mgr.track_error(
                error_type=type(e).__name__,
                severity="error"
            )

            logger.error(
                f"Downstream service call failed: {str(e)}",
                error=str(e),
                error_type=type(e).__name__
            )

            return error_response(
                f"Service call failed: {str(e)}",
                status_code=500
            )


@app.route('/api/example/error', methods=['GET'])
@async_endpoint
async def error_endpoint():
    """
    Endpoint demonstrating error tracking.
    Shows how errors are automatically captured in traces and metrics.
    """
    logger.error("Simulated error", error_type="SimulatedError", severity="error")

    # Track error metric
    metrics.track_error(
        error_type="SimulatedError",
        severity="error"
    )

    # This will be captured in the trace
    raise ValueError("This is a simulated error for testing")


@app.route('/api/example/slow', methods=['GET'])
@async_endpoint
async def slow_endpoint():
    """
    Slow endpoint for testing latency tracking.
    Useful for testing histogram metrics and trace analysis.
    """
    tracer = get_tracing_manager()

    with tracer.start_span("slow_operation") as span:
        # Simulate slow operation
        delay = float(request.args.get('delay', 2.0))
        span.set_attribute("delay_seconds", delay)

        logger.info(f"Starting slow operation (delay={delay}s)")
        time.sleep(delay)

        # Track metrics
        metrics.track_command_duration(
            command="slow",
            duration_seconds=delay,
            platform="http"
        )

        logger.info("Slow operation completed")

    return success_response({
        "message": f"Completed after {delay} seconds",
        "delay": delay
    })


@app.route('/api/example/database', methods=['GET'])
@async_endpoint
async def database_endpoint():
    """
    Endpoint demonstrating database operation tracking.
    Shows how to track database queries in metrics.
    """
    tracer = get_tracing_manager()

    with tracer.start_span("database_query") as span:
        # Simulate database query
        start_time = time.time()

        span.set_attribute("db.operation", "SELECT")
        span.set_attribute("db.table", "users")

        # Simulate query
        time.sleep(0.05)

        duration = time.time() - start_time

        # Track database metrics
        metrics.track_db_query(
            operation="select",
            table="users",
            duration_seconds=duration,
            status="success"
        )

        logger.info(
            "Database query executed",
            operation="select",
            table="users",
            duration_ms=int(duration * 1000)
        )

    return success_response({
        "message": "Database query completed",
        "duration_ms": int(duration * 1000)
    })


# Error handlers

@app.errorhandler(404)
async def not_found(e):
    """Handle 404 errors."""
    logger.error("Route not found", path=request.path, status_code=404)
    return error_response("Route not found", status_code=404)


@app.errorhandler(500)
async def internal_error(e):
    """Handle 500 errors."""
    logger.error(f"Internal error: {str(e)}", error=str(e), status_code=500)
    metrics.track_error(error_type="InternalError", severity="critical")
    return error_response("Internal server error", status_code=500)


# Shutdown handler

@app.before_serving
async def startup():
    """Application startup."""
    logger.system("Application starting", module=MODULE_NAME, version=VERSION)


@app.after_serving
async def shutdown():
    """Application shutdown - cleanup observability."""
    logger.system("Application shutting down")

    # Shutdown tracing (flush pending spans)
    tracer = get_tracing_manager()
    if tracer:
        tracer.shutdown()
        logger.system("Tracing shut down")


if __name__ == '__main__':
    print(f"\n{'='*60}")
    print(f"Starting {MODULE_NAME} v{VERSION}")
    print(f"{'='*60}")
    print(f"Health endpoint: http://{HOST}:{PORT}/health")
    print(f"Metrics endpoint: http://{HOST}:{PORT}/metrics")
    print("Example endpoints:")
    print(f"  GET  http://{HOST}:{PORT}/api/example/simple")
    print(f"  POST http://{HOST}:{PORT}/api/example/traced")
    print(f"  GET  http://{HOST}:{PORT}/api/example/metrics")
    print(f"  POST http://{HOST}:{PORT}/api/example/service-call")
    print(f"  GET  http://{HOST}:{PORT}/api/example/error")
    print(f"  GET  http://{HOST}:{PORT}/api/example/slow?delay=2.0")
    print(f"  GET  http://{HOST}:{PORT}/api/example/database")
    print(f"{'='*60}\n")

    # Run application
    app.run(host=HOST, port=PORT)
