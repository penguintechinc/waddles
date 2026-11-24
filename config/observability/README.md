# Waddles Observability Infrastructure

Complete observability solution for Waddles microservices with OpenTelemetry, Correlation IDs, and Prometheus Metrics.

## Overview

This observability infrastructure provides:

- **Distributed Tracing**: standard OpenTelemetry SDK + OTLP (env-configured; hub-api: `hub_api/telemetry.py`, see `docs/observability/hub-api-telemetry.md`)
- **Correlation IDs**: Request tracking across all services
- **Custom Metrics**: Business metrics for Prometheus
- **Centralized Logging**: AAA logging with correlation ID integration
- **Visualization**: Grafana dashboards for metrics and traces

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Waddles Services                        │
│  (Router, Triggers, Actions, Core, Admin)                   │
│                                                             │
│  Each service exports:                                      │
│  - /metrics (Prometheus format)                             │
│  - Distributed traces (OpenTelemetry)                       │
│  - Correlation IDs in headers and logs                      │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                  Observability Stack                        │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │  Prometheus  │  │    Jaeger    │  │   Grafana    │     │
│  │   :9090      │  │   :16686     │  │    :3000     │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Start Observability Stack

```bash
cd /home/penguin/code/waddlebot/config/observability
docker-compose -f docker-compose.observability.yml up -d
```

### 2. Integrate into Your Module

```python
from flask_core import (
    init_correlation,
    init_metrics,
    setup_aaa_logging
)
from quart import Quart

# Create app
app = Quart(__name__)

# Tracing: configured from the standard OTEL_* env (OTEL_EXPORTER_OTLP_ENDPOINT, ...);
# flask_core no longer ships a tracing manager -- see "OpenTelemetry Tracing" below.
correlation = init_correlation(app)

metrics = init_metrics(
    service_name="my-service",
    service_version="1.0.0"
)

logger = setup_aaa_logging(
    module_name="my-service",
    version="1.0.0"
)

# Setup correlation ID logging
from flask_core import setup_correlation_logging
setup_correlation_logging(correlation)
```

### 3. Access Dashboards

- **Prometheus**: http://localhost:9090
- **Jaeger UI**: http://localhost:16686
- **Grafana**: http://localhost:3000 (admin/admin)

## Components

### 1. OpenTelemetry Tracing

`flask_core.tracing` (`TracingManager`) was **retired**: it read non-standard variables
(`OTLP_ENDPOINT`, `TRACING_EXPORTER`, ...) and hardcoded an insecure OTLP channel. Services
use the OpenTelemetry API directly and the SDK/exporters are configured **only** from the
standard `OTEL_*` environment (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`,
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`,
`OTEL_TRACES_SAMPLER[_ARG]`). hub-api's reference implementation is `hub_api/telemetry.py`
(`docs/observability/hub-api-telemetry.md`).

#### Basic Usage

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("process_command") as span:
    span.set_attribute("platform", "twitch")  # never usernames/emails/tokens
    # ... do work ...
```

#### Context Propagation

Outbound `httpx` and `grpc.aio` calls are instrumented (hub-api): W3C `traceparent` is
injected automatically and only to cluster-internal hosts. Manual injection, when needed:

```python
from opentelemetry import propagate

headers: dict[str, str] = {}
propagate.inject(headers)
```

#### Configuration

Standard OpenTelemetry environment variables only, for example:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://signoz-otel-collector.signoz.svc.cluster.local:4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_SERVICE_NAME=my-service
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=1.0
```

### 2. Correlation IDs (`correlation.py`)

Track requests across all services with unique IDs.

#### Automatic Tracking

```python
from flask_core import init_correlation

# Initialize with app (automatic middleware)
correlation = init_correlation(app)
```

#### Manual Access

```python
from flask_core import get_correlation_id, get_request_id

# In request context
correlation_id = get_correlation_id()
request_id = get_request_id()

logger.info(f"Processing request", extra={
    'correlation_id': correlation_id,
    'request_id': request_id
})
```

#### Service-to-Service Calls

```python
from flask_core import get_correlation_manager

correlation = get_correlation_manager()

# Inject into outgoing requests
headers = {}
correlation.inject_into_headers(headers)

async with httpx.AsyncClient() as client:
    response = await client.post(
        "http://other-service/api",
        headers=headers,
        json=data
    )
```

#### Configuration

Environment variables:

```bash
CORRELATION_HEADER=X-Correlation-ID
REQUEST_HEADER=X-Request-ID
GENERATE_REQUEST_ID=true
VALIDATE_UUID=false
```

### 3. Custom Metrics (`custom_metrics.py`)

Business metrics and Prometheus instrumentation.

#### Default Metrics

All services automatically get:

- `waddlebot_commands_total` - Total commands processed
- `waddlebot_command_duration_seconds` - Command latency
- `waddlebot_active_channels` - Active channels/servers
- `waddlebot_messages_total` - Total messages
- `waddlebot_errors_total` - Errors by type
- `waddlebot_db_queries_total` - Database queries
- `waddlebot_http_requests_total` - HTTP requests
- And many more...

#### Track Commands

```python
from flask_core import get_metrics_manager

metrics = get_metrics_manager()

# Track command execution
metrics.track_command(
    command="!help",
    platform="twitch",
    status="success"
)

# Track duration
metrics.track_command_duration(
    command="!help",
    duration_seconds=0.125,
    platform="twitch"
)
```

#### Track Database Queries

```python
metrics.track_db_query(
    operation="select",
    table="users",
    duration_seconds=0.015,
    status="success"
)
```

#### Track HTTP Requests

```python
metrics.track_http_request(
    method="POST",
    endpoint="/api/command",
    status_code=200,
    duration_seconds=0.250
)
```

#### Custom Metrics

```python
# Create custom counter
command_counter = metrics.create_counter(
    "custom_events",
    "Custom event counter",
    labels=["event_type", "severity"]
)
command_counter.labels(event_type="ban", severity="high").inc()

# Create custom histogram
latency_histogram = metrics.create_histogram(
    "api_latency",
    "API endpoint latency",
    labels=["endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
)
latency_histogram.labels(endpoint="/api/users").observe(0.125)

# Create custom gauge
active_connections = metrics.create_gauge(
    "active_websocket_connections",
    "Number of active WebSocket connections",
    labels=["platform"]
)
active_connections.labels(platform="twitch").set(42)
```

#### Decorator Usage

```python
@metrics.track_decorator(
    metric_type="histogram",
    metric_name="command_processing_time"
)
async def process_command(command: str):
    # Automatically tracked
    return result
```

#### Configuration

Environment variables:

```bash
METRICS_ENABLED=true
METRICS_NAMESPACE=waddlebot
```

## Integration Examples

### Complete Module Setup

```python
from quart import Quart
from flask_core import (
    init_correlation,
    init_metrics,
    setup_aaa_logging,
    setup_correlation_logging,
    create_health_blueprint
)

app = Quart(__name__)

# Module info
MODULE_NAME = "my-service"
VERSION = "1.0.0"

# Initialize observability
correlation = init_correlation(app)
metrics = init_metrics(MODULE_NAME, VERSION)
logger = setup_aaa_logging(MODULE_NAME, VERSION)

# Integrate correlation with logging
setup_correlation_logging(correlation)

# Add health/metrics endpoints
health_bp = create_health_blueprint(MODULE_NAME, VERSION)
app.register_blueprint(health_bp)

@app.route('/api/command', methods=['POST'])
async def handle_command():
    # Automatic tracing via the OTel ASGI/httpx instrumentation (see hub_api/telemetry.py)
    # Automatic correlation IDs via middleware
    # Manual metrics tracking

    data = await request.get_json()
    command = data.get('command')

    # Track command
    metrics.track_command(command, platform="twitch", status="success")

    # Manual span attributes
    trace.get_current_span().set_attribute("command", command)

    # Logging with correlation IDs
    logger.info(f"Processing command: {command}")

    return {"status": "ok"}

if __name__ == '__main__':
    app.run()
```

### Service-to-Service Call with Full Context

```python
import httpx
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind
from flask_core import (
    get_correlation_manager,
    get_metrics_manager
)

async def call_other_service(data: dict):
    tracer = trace.get_tracer(__name__)
    correlation = get_correlation_manager()
    metrics = get_metrics_manager()

    # Create span for this operation
    with tracer.start_as_current_span(
        "call_identity_service",
        kind=SpanKind.CLIENT
    ) as span:
        # Build headers with tracing and correlation context
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": "service-key"
        }

        # Inject trace context
        propagate.inject(headers)

        # Inject correlation IDs
        correlation.inject_into_headers(headers)

        # Make request
        start_time = time.time()
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    "http://identity-core:8050/api/link",
                    headers=headers,
                    json=data
                )

                duration = time.time() - start_time

                # Track metrics
                metrics.track_http_request(
                    method="POST",
                    endpoint="/api/link",
                    status_code=response.status_code,
                    duration_seconds=duration
                )

                # Add span attributes
                span.set_attribute("http.status_code", response.status_code)
                span.set_attribute("http.response_size", len(response.content))

                return response.json()

            except Exception as e:
                # Record error in span
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)

                # Track error metric
                metrics.track_error(
                    error_type=type(e).__name__,
                    severity="error"
                )

                raise
```

## Querying Metrics

### Prometheus Queries

Access Prometheus at http://localhost:9090 and use PromQL:

```promql
# Total commands per minute by service
sum(rate(waddlebot_commands_total[1m])) by (service)

# 95th percentile command latency
histogram_quantile(0.95, sum(rate(waddlebot_command_duration_seconds_bucket[5m])) by (le, command))

# Error rate
sum(rate(waddlebot_errors_total[5m])) by (error_type)

# Active channels by platform
waddlebot_active_channels{platform="twitch"}

# Database query latency
histogram_quantile(0.99, sum(rate(waddlebot_db_query_duration_seconds_bucket[5m])) by (le, operation))

# HTTP request rate
sum(rate(waddlebot_http_requests_total[1m])) by (endpoint, status)
```

## Viewing Traces

Access Jaeger UI at http://localhost:16686

1. **Service Selection**: Choose service from dropdown
2. **Operation**: Select specific operation (endpoint/function)
3. **Tags**: Filter by tags (platform=twitch, command=!help, etc.)
4. **Time Range**: Adjust time window
5. **Trace View**: See full request flow across services

### Trace Features

- **Service Graph**: Visual dependency map
- **Trace Comparison**: Compare slow vs fast traces
- **Critical Path**: Identify bottlenecks
- **Error Traces**: Filter by errors

## Log Correlation

All logs automatically include correlation IDs:

```
[2025-12-09 14:30:15.123] INFO [cid=550e8400-e29b-41d4-a716-446655440000] [rid=6ba7b810-9dad-11d1-80b4-00c04fd430c8] router:1.0.0 AUDIT community=test user=alice action=execute_command result=SUCCESS Processed command: !help
```

Search logs by correlation ID to trace entire request:

```bash
grep "cid=550e8400-e29b-41d4-a716-446655440000" /var/log/waddlebotlog/*.log
```

## Performance Impact

- **Tracing**: ~0.5-2ms overhead per traced operation
- **Metrics**: ~0.1-0.5ms overhead per metric update
- **Correlation IDs**: ~0.1ms overhead per request
- **Total**: ~1-3ms additional latency per request

Sampling can reduce overhead:

```python
# Sample 10% of traces (standard OTel env, no code)
# OTEL_TRACES_SAMPLER=parentbased_traceidratio
# OTEL_TRACES_SAMPLER_ARG=0.1
```

## Production Deployment

### Kubernetes

Use Jaeger Operator and Prometheus Operator:

```yaml
# Jaeger
apiVersion: jaegertracing.io/v1
kind: Jaeger
metadata:
  name: waddles-jaeger
spec:
  strategy: production
  storage:
    type: elasticsearch

# ServiceMonitor for Prometheus Operator
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: waddles-services
spec:
  selector:
    matchLabels:
      app: waddlebot
  endpoints:
    - port: metrics
      path: /metrics
```

### Pod Annotations

Add to pod specs:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/path: "/metrics"
  prometheus.io/port: "8000"
```

## Troubleshooting

### No Traces in Jaeger

1. Check Jaeger is running: `docker ps | grep jaeger`
2. Verify OTEL_EXPORTER_OTLP_ENDPOINT is set and reachable
3. Check sampling rate (must be > 0)
4. Review application logs for tracing errors

### Metrics Not Appearing

1. Check Prometheus targets: http://localhost:9090/targets
2. Verify service exposes /metrics endpoint
3. Check Prometheus scrape config
4. Review prometheus logs: `docker logs waddlebot-prometheus`

### Correlation IDs Missing

1. Verify middleware is registered
2. Check header names match
3. Review correlation manager logs
4. Ensure service-to-service calls inject headers

## Files Reference

- `/hub_api/telemetry.py` - OpenTelemetry SDK bootstrap (hub-api reference implementation)
- `/libs/flask_core/flask_core/correlation.py` - Correlation ID tracking
- `/libs/flask_core/flask_core/custom_metrics.py` - Prometheus metrics
- `/config/observability/prometheus.yml` - Prometheus configuration
- `/config/observability/jaeger-config.yaml` - Jaeger configuration
- `/config/observability/docker-compose.observability.yml` - Full stack

## Environment Variables Reference

### Tracing
Standard OpenTelemetry variables only (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`,
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`,
`OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG`). The former `TRACING_*`, `JAEGER_*`,
`ZIPKIN_ENDPOINT` and `OTLP_ENDPOINT` variables are no longer read by anything.

### Correlation
- `CORRELATION_HEADER` - Correlation ID header name (default: X-Correlation-ID)
- `REQUEST_HEADER` - Request ID header name (default: X-Request-ID)
- `GENERATE_REQUEST_ID` - Generate request IDs (default: true)
- `VALIDATE_UUID` - Validate UUID format (default: false)

### Metrics
- `METRICS_ENABLED` - Enable/disable metrics (default: true)
- `METRICS_NAMESPACE` - Metrics prefix (default: waddles)

## Next Steps

1. Add custom dashboards to Grafana
2. Configure alerting rules in Prometheus
3. Set up log aggregation (ELK/Loki)
4. Implement SLO monitoring
5. Create runbooks for common issues
