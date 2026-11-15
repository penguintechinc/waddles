# Waddles Observability Quick Reference

## Fast Integration (Copy-Paste Ready)

### 1. Minimal Setup (3 lines)

```python
from flask_core import init_correlation, init_metrics

# Tracing: standard OTEL_* env only (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME, ...);
# flask_core no longer ships a tracing manager (see hub_api/telemetry.py).
correlation = init_correlation(app)
metrics = init_metrics("my-service", "1.0.0")
```

### 2. Complete Setup (Production Ready)

```python
from flask_core import (
    init_correlation, init_metrics,
    setup_aaa_logging, setup_correlation_logging
)

# Initialize
correlation = init_correlation(app)
metrics = init_metrics("my-service", "1.0.0")
logger = setup_aaa_logging("my-service", "1.0.0")

# Integrate
setup_correlation_logging(correlation)
```

## Common Usage Patterns

### Manual Tracing

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("operation_name") as span:
    span.set_attribute("key", "value")
    # ... do work ...
```

### Get Correlation IDs

```python
from flask_core import get_correlation_id, get_request_id

correlation_id = get_correlation_id()
request_id = get_request_id()
```

### Track Metrics

```python
from flask_core import get_metrics_manager

metrics = get_metrics_manager()

# Track command
metrics.track_command("!help", platform="twitch", status="success")

# Track duration
metrics.track_command_duration("!help", 0.125, platform="twitch")

# Track error
metrics.track_error("ValidationError", severity="warning")

# Track DB query
metrics.track_db_query("select", "users", 0.015, status="success")

# Track HTTP request
metrics.track_http_request("POST", "/api/command", 200, 0.250)
```

### Service-to-Service Call

```python
import httpx
from opentelemetry import propagate
from flask_core import get_correlation_manager

correlation = get_correlation_manager()

headers = {"Content-Type": "application/json"}
propagate.inject(headers)  # httpx is auto-instrumented in hub-api; manual only if needed
correlation.inject_into_headers(headers)

async with httpx.AsyncClient() as client:
    response = await client.post(url, headers=headers, json=data)
```

### Custom Metrics

```python
from flask_core import get_metrics_manager

metrics = get_metrics_manager()

# Counter
counter = metrics.create_counter("my_counter", "Description", labels=["label1"])
counter.labels(label1="value").inc()

# Gauge
gauge = metrics.create_gauge("my_gauge", "Description", labels=["label1"])
gauge.labels(label1="value").set(42)

# Histogram
histogram = metrics.create_histogram("my_histogram", "Description", labels=["label1"])
histogram.labels(label1="value").observe(0.125)
```

## Environment Variables

### Quick Setup (Development)

```bash
# Tracing (standard OpenTelemetry env)
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_SERVICE_NAME=my-service

# Correlation
export CORRELATION_HEADER=X-Correlation-ID
export REQUEST_HEADER=X-Request-ID

# Metrics
export METRICS_ENABLED=true
```

### Production Setup

```bash
# Tracing - OTLP collector (SigNoz / Jaeger OTLP receiver), 10% head sampling
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
export OTEL_SERVICE_NAME=my-service
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=0.1

# Correlation
export CORRELATION_HEADER=X-Correlation-ID
export REQUEST_HEADER=X-Request-ID
export GENERATE_REQUEST_ID=true

# Metrics
export METRICS_ENABLED=true
export METRICS_NAMESPACE=waddles
```

## Docker Compose Commands

```bash
# Start observability stack
cd /home/penguin/code/waddlebot/config/observability
docker-compose -f docker-compose.observability.yml up -d

# Stop stack
docker-compose -f docker-compose.observability.yml down

# View logs
docker-compose -f docker-compose.observability.yml logs -f

# Restart single service
docker-compose -f docker-compose.observability.yml restart prometheus
```

## Access URLs

```
Prometheus:  http://localhost:9090
Jaeger UI:   http://localhost:16686
Grafana:     http://localhost:3000  (admin/admin)
```

## PromQL Examples

```promql
# Commands per minute
sum(rate(waddlebot_commands_total[1m])) by (service, command)

# 95th percentile latency
histogram_quantile(0.95, sum(rate(waddlebot_command_duration_seconds_bucket[5m])) by (le, command))

# Error rate
sum(rate(waddlebot_errors_total[5m])) by (error_type)

# Active channels
waddlebot_active_channels{platform="twitch"}

# HTTP request rate by endpoint
sum(rate(waddlebot_http_requests_total[1m])) by (endpoint, status)
```

## Common Metric Names

```
waddlebot_commands_total
waddlebot_command_duration_seconds
waddlebot_active_channels
waddlebot_messages_total
waddlebot_active_users
waddlebot_errors_total
waddlebot_db_queries_total
waddlebot_db_query_duration_seconds
waddlebot_http_requests_total
waddlebot_http_request_duration_seconds
waddlebot_cache_operations_total
waddlebot_queue_size
waddlebot_circuit_breaker_state
```

## Logging with Correlation

```python
# Logs automatically include correlation IDs when using flask_core logger
logger.info("Processing command", command="!help", platform="twitch")

# Output format:
# [2025-12-09 14:30:15.123] INFO [cid=550e8400-e29b-41d4-a716-446655440000] [rid=6ba7b810-9dad-11d1-80b4-00c04fd430c8] my-service:1.0.0 AUDIT Processing command
```

## Troubleshooting

### No traces appearing?
```bash
# Check Jaeger is running
docker ps | grep jaeger

# Check service logs
docker logs waddlebot-jaeger

# Verify the OTLP endpoint and sampler
echo $OTEL_EXPORTER_OTLP_ENDPOINT
echo $OTEL_TRACES_SAMPLER_ARG
```

### Metrics not showing?
```bash
# Check Prometheus targets
curl http://localhost:9090/api/v1/targets

# Verify /metrics endpoint
curl http://localhost:8000/metrics

# Check Prometheus logs
docker logs waddlebot-prometheus
```

### Correlation IDs missing?
```python
# Verify middleware is registered
correlation = init_correlation(app)  # Must pass app!

# Check headers in response
curl -i http://localhost:8000/api/example/simple
# Should see: X-Correlation-ID and X-Request-ID
```

## File Locations

```
Python Modules:
  /home/penguin/code/Waddles/hub_api/telemetry.py
  /home/penguin/code/Waddles/libs/flask_core/flask_core/correlation.py
  /home/penguin/code/Waddles/libs/flask_core/flask_core/custom_metrics.py

Configuration:
  /home/penguin/code/Waddles/config/observability/prometheus.yml
  /home/penguin/code/Waddles/config/observability/jaeger-config.yaml
  /home/penguin/code/Waddles/config/observability/jaeger-sampling.json
  /home/penguin/code/Waddles/config/observability/docker-compose.observability.yml

Documentation:
  /home/penguin/code/Waddles/config/observability/README.md
  /home/penguin/code/Waddles/config/observability/IMPLEMENTATION_SUMMARY.md
  /home/penguin/code/Waddles/config/observability/QUICK_REFERENCE.md

Examples:
  /home/penguin/code/Waddles/config/observability/example_integration.py
```

## Quick Test

```bash
# 1. Start observability stack
cd /home/penguin/code/waddlebot/config/observability
docker-compose -f docker-compose.observability.yml up -d

# 2. Run example service
python example_integration.py

# 3. Generate traffic
curl http://localhost:8000/api/example/simple
curl -X POST http://localhost:8000/api/example/traced -H "Content-Type: application/json" -d '{"command":"test"}'

# 4. View results
# - Metrics: http://localhost:8000/metrics
# - Prometheus: http://localhost:9090
# - Jaeger: http://localhost:16686
```

## Import Cheatsheet

```python
# Everything you need
from flask_core import (
    # Initialization
    init_correlation,
    init_metrics,
    setup_aaa_logging,
    setup_correlation_logging,

    # Getters
    get_correlation_manager,
    get_metrics_manager,
    get_correlation_id,
    get_request_id,

    # Classes (if needed)
    CorrelationIDManager,
    MetricsManager,
)
```
