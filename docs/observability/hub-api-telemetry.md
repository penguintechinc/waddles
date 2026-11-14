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

> **Restart required.** The flag is evaluated **once, at process startup** (PostHog, then the
> `FLAG_WADDLES_DISABLE_HUB_API_OTEL` env baseline, then default OFF; a flag-plane outage or a
> 2 s evaluation timeout means OFF, i.e. telemetry on). Flipping it in PostHog or changing the env
> var has no effect on running pods: roll the hub-api Deployment
> (`kubectl rollout restart deployment/hub-api`) for the change to apply. There is deliberately no
> hot toggle: providers, exporters and instrumentation are installed once and removed only at
> shutdown.

## What is emitted

| Signal | Content |
|---|---|
| Traces | One SERVER span per HTTP request (named `METHOD route-template`, `http.route`, numeric `tenant_id` / `community_id` where known), SQLAlchemy (penguin-dal) query spans, pydal query spans (`flask_core` raw `dal` and `AsyncDAL`), one SERVER span per internal gRPC call continuing the caller's W3C `traceparent`, CLIENT spans for every outbound `httpx` call and any `grpc.aio` channel opened in-process. Probe routes (`/health`, `/healthz`, `/ready`) are not traced |
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

## Outbound calls (httpx)

Every `httpx` client (sync and async) is instrumented. W3C `traceparent`, `tracestate` and
`baggage` are propagated **only to cluster-internal hosts**: private/loopback IPs, single-label
Kubernetes service names (`svc-streaming`), `*.svc`, `*.cluster.local`, plus any suffix in
`TELEMETRY_INTERNAL_HOST_SUFFIXES` (comma separated). A guard wrapped around the real httpx
transport strips those headers for every other host on every hop (including redirects), so a
third party never receives our trace ids or baggage. Client-span URLs are reduced to
`scheme://host[:port]` for external hosts (webhook tokens live in paths) and
`scheme://host/path` (no query string) for internal ones.

## Database spans

- penguin-dal runs on SQLAlchemy and is covered by the SQLAlchemy instrumentation (pool metrics
  included).
- `flask_core`'s pydal connections (`app.config["dal"]`, `AsyncDAL`) are wrapped at
  `SQLAdapter.execute`: operation (`SELECT`), `db.system` and table name only. Statement text is
  never recorded (pydal inlines literal values into its SQL) and driver error text is replaced by
  the exception class name. `AsyncDAL` copies the caller's `contextvars` onto its pool thread, so
  these spans are children of the request span. Queries with no trace context (bootstrap,
  background loops) are not traced.
- The exporter scrubber drops `db.statement` / `db.query.text` from every span.

## Retired: `flask_core.tracing`

`TracingManager` / `init_tracing` were removed: they read non-standard variables
(`OTLP_ENDPOINT`, `TRACING_*`) and hardcoded an insecure OTLP channel, and nothing used them.
Services use the OpenTelemetry API plus an env-configured SDK; `hub_api/telemetry.py` is the
reference. `hub_api/tests/test_tracing_retired.py` fails if either anti-pattern reappears in the
repo.

## Known gaps

- Background loops with no request context do not produce pydal spans (SQLAlchemy ones do).
