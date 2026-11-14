# hub-api telemetry (traces, metrics, logs)

hub-api exports all three OpenTelemetry signals over OTLP/gRPC. Implementation:
`hub_api/telemetry.py`; tests: `hub_api/tests/test_telemetry_otlp.py` (real in-process OTLP
receiver, signal counts printed, zero = FAIL).

## Configuration (env only)

The destination is never hardcoded. The Helm `global.otel` helper (`templates/_otel.tpl`) renders
these variables for the hub-api Deployment:

| Variable | Use |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` (or per-signal `..._TRACES/_METRICS/_LOGS_ENDPOINT`) | Collector URL. **Unset = telemetry disabled** (one INFO line: `telemetry export is disabled: ...`). The scheme decides TLS: `http://` insecure, `https://` TLS |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` (default, the only bundled protocol; anything else disables export loudly) |
| `OTEL_EXPORTER_OTLP_HEADERS` | Auth headers, from a Secret (never a literal) |
| `OTEL_EXPORTER_OTLP_TIMEOUT` (+ per-signal) | Export timeout in **milliseconds** (spec). Converted here because the Python exporter would read the raw value as seconds. Clamped 0.1-30 s, default 5 s |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | Resource. Defaults: `service.name=hub-api`, `service.version`, `deployment.environment`, plus a per-process `service.instance.id` |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `always_on`, `always_off`, `traceidratio`, `parentbased_*` (default `parentbased_always_on`) |
| `OTEL_{TRACES,METRICS,LOGS}_EXPORTER` | `otlp` (default) or `none` per signal |
| `OTEL_METRIC_EXPORT_INTERVAL` | Metric push interval, ms (default 60000; chart sets 15000) |
| `OTEL_BSP_*` / `OTEL_BLRP_*` | Span / log batch queue size, batch size, schedule delay (queue is bounded, drop-oldest) |

## Kill switch

`waddles.disable-hub-api-otel` is an **opt-out** switch: unseen/OFF = telemetry on, ON = disabled.
Evaluated once at startup (PostHog, then the `FLAG_WADDLES_DISABLE_HUB_API_OTEL` env baseline,
then default OFF), so flipping it needs a pod restart.

## What is emitted

| Signal | Content |
|---|---|
| Traces | One SERVER span per HTTP request (named `METHOD route-template`, `http.route`, numeric `tenant_id` / `community_id` where known), SQLAlchemy (penguin-dal) query spans, one SERVER span per internal gRPC call continuing the caller's W3C `traceparent`, CLIENT spans for any `grpc.aio` channel opened in-process. Probe routes (`/health`, `/healthz`, `/ready`) are not traced |
| Metrics | `http.server.request.duration` histogram (method, route template, status), `http.server.active_requests` up-down counter, `grpc_server_request_duration_seconds` histogram and `grpc_server_handled` counter (method, status code), SQLAlchemy pool metrics, the existing app metrics |
| Logs | All Python logging (root + the AAA `waddlebot.<module>` logger) at the configured `LOG_LEVEL`, with `trace_id` / `span_id` of the active span |

## Safety guarantees

- A dead or unreachable collector never breaks startup, slows a request or crashes the process:
  bounded drop-oldest queues, export timeouts, non-blocking batch processors, and a shutdown
  flush bounded to 5 s on a daemon thread.
- PII never leaves the process. Every span passes a scrubbing exporter and every log record a
  scrubbing handler: emails, bearer/JWT tokens, `token=`/`code=`/`password=`-style query values,
  attributes named like `user`/`email`/`authorization`/`cookie`, client IP and user agent are
  removed or redacted; query strings are stripped from URLs; the raw path is replaced by the
  route template. Free-text log messages must still never contain bare usernames (no regex can
  find them) -- log ids, not names.
- The ASGI middleware's own HTTP metrics (raw-path labels) and `send`/`receive` child spans (raw
  path in the span name) are disabled in favour of the route-templated equivalents.

## Known gaps

- `flask_core`'s pydal connections (`app.config["dal"]`) are not SQLAlchemy, so only penguin-dal
  queries produce DB spans.
- Outbound `httpx` calls (e.g. hub-api -> svc-streaming REST in `services/calls_proxy.py`) are
  not instrumented, so no `traceparent` is injected on them yet.
- `libs/flask_core/flask_core/tracing.py` (`TracingManager`) is unused by hub-api and still
  reads non-standard variables (`OTLP_ENDPOINT`, hardcoded `insecure=True`); it should be fixed
  or retired before any service adopts it.
