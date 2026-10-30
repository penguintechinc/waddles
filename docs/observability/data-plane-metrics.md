# Rust data-plane metrics (svc-process, svc-action, bundle-executor)

OTel **metrics** for the three Rust data-plane runtimes: histograms first (latency, load, size), counters for events,
gauges for current state. Logs and traces are unchanged (`tracing` + `penguin-logging`); this is the third leg.
Spec: `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` §13.1.

## How the metrics leave the process

| Service | Pipeline | Destination |
|---|---|---|
| svc-process, svc-action | instruments on the **global** meter provider that `penguin-logging` installs (`telemetry::init`) | OTLP push **and** the Prometheus `/metrics` scrape on `:9090` (the provider carries both readers) |
| bundle-executor | its own meter provider (`telemetry::init_metrics`), OTLP/gRPC only | OTLP push, **only when an endpoint variable is set**; no scrape port |

Destination is configuration, never code — the standard OTLP variables, set per deployment:

| Env var | Effect |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | collector URL (metrics-specific wins). **Unset = no OTLP export** (svc-*: `/metrics` still works; executor: records but exports nowhere) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` (executor supports `grpc` only; anything else logs a WARN and disables export) |
| `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_TIMEOUT` | auth headers (from a secret) / per-batch timeout |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_METRIC_EXPORT_INTERVAL` | resource identity / export period (default 60 s) |

A dead or misconfigured exporter never breaks the process: it degrades to "not exporting" with a WARN, and the SDK drops
batches while the collector is down.

> **Executor egress.** The executor is a credential-less sandbox host whose pod egress is default-deny
> (`k8s/helm/waddlebot/templates/bundle-executor-networkpolicy.yaml`: DNS, the stage host-API port, the bundle bucket).
> Export is therefore **off by default** and opens no new path. Turning it on is an explicit operator act: set the
> endpoint variable **and** allow that collector in the pod's egress policy. The chart does not do either today (no
> collector is deployed by the chart yet). The binary's dependency policy still holds — gRPC only, no `reqwest`
> (`tests/dependency_policy.rs`, `deny.toml`).

## Naming: instrument name vs exposed name

OTel instrument names carry **no** `_total` / unit suffix. The Prometheus exporter (and OTLP→Prometheus translators)
append `_total` to counters and `_<unit>` to unit-bearing instruments **unconditionally** — registering
`waddles_stage_latency_seconds` would scrape as `waddles_stage_latency_seconds_seconds`. So the code registers
`waddles_stage_latency` (unit `s`) and every path exposes the documented `waddles_stage_latency_seconds`. Tables below use
the **exposed** names; `stage_metrics::instrument_name` / `metrics::instrument_name` is the single mapping, and
`tests/otel_metrics_scrape.rs` fails on any `_total_total` / `_seconds_seconds`.

Latency histograms carry explicit second-scale buckets (1 ms–30 s; slow phases to 600 s) — the SDK's default
boundaries are millisecond-shaped and would put every sub-second sample in one bucket.

## Label policy (PII boundary, cardinality)

| Allowed label values | Never a label |
|---|---|
| closed `&'static str` vocabularies (`result`, `phase`, `limit`, `export`, `capability`, `reason`); host-derived bundle id `app_id` (already on `waddles_bundle_kv_authorize_denied_total`) | user ids/names, message text, op strings from a guest, stream/channel/source names, destination hosts, tenant/community slugs |

Guest op strings are folded through an allow-list (`KNOWN_OPS`, else `other`); the gate bridge folds
`net.http.*` permission labels to their family (`net.http.fqdn`), so a hostile bundle cannot mint series.

## svc-process and svc-action (`stage="process"` / `"action"`)

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `waddles_stage_latency_seconds` | histogram | `stage, app_id, result` | take-to-terminal-outcome time per delivered entry. `result` — process: `delivered`, `no_reply`, `route_denied`, `dead_lettered`, `spine_error`; action: `success`, `non_retryable_failure`, `retryable_failure`, `dead_lettered`, `spine_error` (`spine_error` = the ack/DLQ write itself failed, entry stays pending) |
| `waddles_executor_call_seconds` | histogram | `stage, app_id, export, result` | one executor `invoke` as the stage sees it (action: one per attempt). `export`: `transform`/`dispatch`; `result`: `ok`, `executor_error`, `malformed`, `host_api_error` |
| `waddles_host_call_latency_seconds` | histogram | `stage, capability, op, result` | one host-capability call (`kv`, `db`, `reputation`, `economy`, `identity`, `http`, `flags`, `clock`, `context`, `log`, `relay`); `result`: `ok`, `denied` (gate/egress/quota refusal), `error` |
| `waddles_tool_call_total` | counter | `stage, target, tool, outcome` | one bundle-to-bundle `tool.invoke` completion. `outcome`: `ok`, or a refusal — `not_granted`, `forbidden`, `timeout`, `depth_exceeded`, `pool_exhausted`, `target_unavailable`, `invalid_args`. `target`/`tool` are the host-resolved identities, or `unresolved` when refused before resolution (guest-supplied names never mint series) |
| `waddles_tool_call_latency_seconds` | histogram | `stage, target, tool, outcome` | one `tool.invoke` as the stage sees it (authorize + nested invoke); same labels as the counter |
| `waddles_bundle_load_seconds` | histogram | `stage, app_id, phase, result` | `phase=load` stage-observed round trip; `phase=precompile` the executor-reported `precompile_ms` (its whole load) |
| `waddles_action_dispatch_attempts` | histogram | `stage, app_id, result` | **action only** — attempts one entry consumed (1 = first try) |
| `waddles_spine_batch_size` | histogram | `stage` | entries per `XREADGROUP` drain pass; idle passes record `0` |
| `waddles_sandbox_trip_total` | counter | `stage, app_id, limit` | executor-reported trips: `timeout`, `memory`, `trap`, `denied` |
| `waddles_spine_dlq_total` | counter | `stage, reason` | dead-letter writes by the record's `error.kind` |
| `waddles_tenant_boundary_violations_total` | counter | `stage, reason` | §5.11 hop-verification failures |
| `waddles_consumer_skipped_total` | counter | `stage, app_id, reason` | read + acked without an executor call (e.g. `route_denied`) |
| `waddles_stream_trimmed_total` | counter | `stage` | `XADD` observed the stream at its approximate `MAXLEN` |
| `waddles_stream_claimed_total` | counter | `stage, app_id` | `XAUTOCLAIM` recoveries |
| `waddles_stream_events_total` | counter | `stage, platform` | `XADD` to an ingest-source stream |
| `waddles_insecure_transport` | gauge | `component, aspect` | `1` insecure, explicit `0` secure (Valkey TLS/auth) |
| `waddles_spine_queue_depth` | histogram | `stage` | consumer-group lag (undelivered entries) sampled every `SPINE_STATS_INTERVAL_MS` from `XINFO GROUPS` |
| `waddles_group_pending` | histogram | `stage, app_id` | consumer-group PEL size, same sampler (stuck-bundle signal; above `SPINE_PEL_ALERT` it also logs a WARN) |
| `waddles_bundle_capability_authorized_total` | counter | `app_id, permission` | capability-gate grants (`authorize()` allowed) |
| `waddles_bundle_capability_denied_total` | counter | `app_id, permission, reason` | capability-gate denials (`not_granted`, `quota_exceeded`, ...) |

Notes:

- The two gate counters are recorded by `bundle-capability-gate` through the `metrics` facade, which installs no recorder.
  Until this change **no recorder existed anywhere, so every grant/denial was silently dropped**; each service now installs
  an OTel-backed recorder (`metrics_facade::install`).
- `waddles_stream_events_total` fires only for `…:src:…` streams (ingest's own counter is
  `svc_ingest_events_published_total`); the stage services append to action streams, so expect it absent here.
- **Backlog sampler.** Each consumer (`spine::run` / `dispatch::run`) spawns a task that calls
  `SpineClient::group_stats` for its granted stream(s) every `SPINE_STATS_INTERVAL_MS` (the spine's own knob, default
  10 s; `0` disables) — the call feeds `waddles_spine_queue_depth` / `waddles_group_pending` through the spine's metrics
  callbacks. First sample after one full interval (consumers spawned together do not all query at t=0); a failed sample is
  logged at DEBUG and retried; the task is aborted when its drain loop ends. A group whose PEL exceeds `SPINE_PEL_ALERT`
  logs a WARN by `app_id` and counts only (stream names embed tenant slugs). Cost: one read-only `XINFO GROUPS` per
  consumer per interval.
- Pre-existing `svc_process_*` / `svc_action_*` / `host_api_*` Prometheus-native series are untouched.

## bundle-executor

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `waddles_executor_load_seconds` | histogram | `app_id, phase, result` | `phase`: `fetch` (bucket GET), `verify` (digest + Ed25519 sidecar), `precompile` (wasmtime compile), `total` (end to end). One real sample per cold compile (single-flight) |
| `waddles_executor_loads_total` | counter | `result` | `ok`, `already_resident`, `digest_mismatch`, `load_failed` |
| `waddles_executor_unloads_total` | counter | `result` | `evicted`, `still_referenced`, `orphaned`, `unknown_bundle` |
| `waddles_executor_orphaned_unloads_total` | counter | — | duplicate/orphaned unloads (replaces the unexported `orphaned_unload_total` atomic, which still counts) |
| `waddles_executor_connector_calls_total` | counter | `app_id, export, outcome` | connector-world export calls (`on-connect`/`on-frame`/`on-heartbeat-due`/`on-disconnect`); `outcome`: `ok`, `guest_error`, `fuel_exhausted`, `deadline`, `memory_limit`, `trap`, `pool_exhausted`, `instantiate_failed`, `internal`. No connection/source label (unbounded cardinality) |
| `waddles_executor_wire_frames_shed_total` | counter | `class` | sheddable frames (guest `log` host-calls) dropped because the bounded outbound writer queue (256 frames) was full; `class`: `log` |
| `waddles_executor_wire_backpressure_total` | counter | `outcome` | must-deliver frames (replies, state-mutating host-calls) that met a full writer queue; `outcome`: `waited`, `timeout` (10 s bound exceeded, frame refused) |
| `waddles_executor_connector_call_seconds` | histogram | `app_id, export, outcome` | one connector export call, slot wait excluded |
| `waddles_executor_invoke_seconds` | histogram | `app_id, export, result` | one invoke end to end; `result` is `ok` or the wire error code (`executor_deadline`, `memory_limit`, `wasm_trap`, `unknown_bundle`, ...) |
| `waddles_executor_instantiate_seconds` | histogram | `app_id` | component instantiation per invoke |
| `waddles_executor_guest_seconds` | histogram | `app_id, export` | guest export execution, host-call waits included; recorded for trapped guests too |
| `waddles_executor_host_call_seconds` | histogram | `capability, op, result` | host-call round trip executor→stage→executor; `result`: `ok`, `denied`, `error`, `connection_lost`, `backpressure`, `malformed` |
| `waddles_executor_sandbox_trip_total` | counter | `app_id, limit` | `timeout`, `memory`, `trap` |
| `waddles_executor_invocations_active` | gauge (up/down) | — | guests currently executing (decrements on every exit path) |
| `waddles_executor_bundles_loaded` | gauge | — | resident compiled components (distinct digests); explicit `0` after a disconnect wipe |
| `waddles_executor_connection_attempts_total` | counter | `result` | `connected`, `failed` |
| `waddles_executor_connection_up` | gauge | — | `1` while the host-API session is established, explicit `0` otherwise |
| `waddles_executor_session_seconds` | histogram | — | length of each established session |
| `waddles_executor_outage_seconds` | histogram | — | downtime between a session ending and the next connecting |
| `waddles_executor_reconnects_total` | counter | — | trips round the dial loop |
| `waddles_executor_heartbeat_timeouts_total` | counter | — | sessions dropped as stale |

The executor never creates spans (spec §13.2) and every number is measured by trusted host code around the guest, so a
hostile bundle cannot forge a series. Its view differs from the stage's `waddles_executor_call_seconds` by exactly the wire
round trip; both are kept.

## How tests prove emission (not just definition)

| Test | Proves |
|---|---|
| `src/spine.rs` / `src/dispatch.rs` `backlog_sampler` | the sampler loop (interval, abort-on-drop, failure tolerance, first-sample delay) against a scripted source; plus a REAL `SpineClient` + Valkey (skipped when none answers on `127.0.0.1:6379`) proving the spine's lag/pending callbacks land in the instruments with the server's numbers |
| `src/spine.rs` / `src/dispatch.rs` `metrics_emission` | real `handle_delivered` / `ensure_loaded` / `drain_batch` paths (fake executor over a real framed connection, real gate) emit the series with the right labels and results, incl. sandbox trips, retries, `spine_error` |
| `src/capabilities.rs` host-call tests | the real capability handler times every call; hostile ops fold to `other`; gate denials are `denied` |
| `src/metrics_facade.rs` + capabilities | the **real** gate's `authorize()` counters reach OTel through the bridge; 50 hostile hosts → 1 series |
| `tests/otel_metrics_scrape.rs` (svc-process, svc-action) | `telemetry::init` → global provider → `/metrics` text carries the documented names, second-scale buckets, no double suffixes, no stream/host/user text |
| `bundle_executor` `src/invoke.rs` `metrics_emission`, `src/wire.rs` | real wasmtime load / invoke / deadline trap / memory trap / host call emit every phase and outcome |
| `bundle_executor` `tests/metrics_emission.rs` | real component + real wire + real trap, read back from a process-global in-memory provider |
| `bundle_executor` `src/telemetry.rs` | with an endpoint set, metrics reach a real OTLP/gRPC collector over HTTP/2 under the documented names and `service.name` |

## Known follow-ups (not changed here)

| Item | Why it matters |
|---|---|
| Capability host crates (`bundle_host_kv`, `_db`, `_economy`, `_reputation`) and `svc_process::identity` register instruments **with** `_seconds`/`_total` suffixes | via the Prometheus bridge they scrape as `…_seconds_seconds` / `…_total_total`, so dashboards querying the single-suffixed names read empty on the scrape path (OTLP→Prometheus translators that de-duplicate are unaffected). Fix = rename the instruments (see *Naming*), a separate, dashboard-visible change |
| `host_api::Connection::invoke` has no timeout and the read loop's EOF does not fail pending requests | an executor that dies mid-invoke can leave that consumer's `invoke` pending forever; only `heartbeat`/`mark_closed` stop *new* invokes. Found while testing the closed-connection path |
| `svc_ingest` / `svc_streaming` | out of scope for this change |
