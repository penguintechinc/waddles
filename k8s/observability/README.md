# Waddles observability-as-code

Grafana dashboards and Prometheus alert rules for the metrics the Waddles services emit, versioned next to the
code. Every metric name is verified against the source tree (see Validation), never typed from memory.

```
k8s/observability/
├── dashboards/          5 Grafana dashboards (generated JSON, committed)
├── alerts/
│   ├── waddles-slo.rules.yml        recording + alert rules (Prometheus rule-file format)
│   └── waddles-slo.rules.test.yml   promtool unit tests (alerts must fire / stay silent)
├── manifests/
│   ├── servicemonitor.yaml          scrape config for the /metrics surface (prometheus-operator)
│   └── prometheusrule.yaml          GENERATED from the rule file (prometheus-operator)
├── scripts/
│   ├── obs_tool.py                  dashboard generator, manifest renderer, metric-existence check
│   └── validate.sh                  full gate (also: make -C k8s/observability validate)
└── Makefile
```

## Telemetry destinations are configured, not hardcoded

Nothing here names a backend. Services export through the standard OTLP environment variables, set per
deployment (Helm values / K8s env), so the same build points at any OTLP-capable backend:

| Env var | Purpose |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector/backend URL - the only thing that differs between environments. Unset = no OTLP export (stdout + `/metrics` still work) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` (default) or `http/protobuf` |
| `OTEL_EXPORTER_OTLP_HEADERS` | Auth headers, from a secret, never CLI args |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | Service identity, `deployment.environment`; never PII |

Dashboards use a `datasource` variable (any Prometheus-compatible source: Prometheus, Mimir, Thanos, VictoriaMetrics,
Grafana Cloud) plus a free-form `filters` ad-hoc variable for `namespace` / `job` / `cluster` scoping. The alert
rules carry no URLs or receivers - route on the `severity` (`warning` / `critical`) and `team` labels in Alertmanager.

## How the metrics reach Prometheus

| Path | Services | Series |
|---|---|---|
| Scrape `/metrics` on port `metrics` (:9090) | svc-ingest, svc-process, svc-action, svc-presentation, svc-streaming, egress-proxy | `svc_*`, `host_api_*`, `dispatch_*`, `rtc_*`, `rtmp_*`, `hls_*`, `recording_*`, `egress_proxy_*`; OTel instruments in the same processes are bridged into the same registry (`penguin-logging` -> `opentelemetry-prometheus`), e.g. `waddles_bundle_kv_*`, `waddles_bundle_tables_*` |
| OTLP push -> Collector/Prometheus OTLP receiver | hub-api (Python), svc-streaming `stream_*` (PR #758) | `grpc_server_*`, `hub_api_*`, `waddles_hub_*`, `waddles_bundle_active_set_*`, `odoo_sync_*`, `stream_*` |

Assumptions baked into the queries:

- OTLP -> Prometheus uses the default translation (dots -> underscores, unit suffix, `_total` on counters).
  Millisecond histograms (`hub_api.identity.*_ms`, `odoo_sync.rpc_duration_ms`) get a `_milliseconds` suffix on some
  pipelines, so those panels match both spellings with a `__name__` regex.
- Counters are cumulative. OTel default export interval is 60 s, so the **Rate window** variable defaults to 5m
  (minimum 2m) instead of `$__rate_interval`.

## Dashboards

| File (uid) | Covers | Key metrics |
|---|---|---|
| `waddles-hub-api.json` (`waddles-hub-api`) | hub-api: internal gRPC RED, identity resolution, flags, active-set watermark, usage aggregation, role/event/Odoo sync | `grpc_server_request_duration_seconds`, `hub_api_identity_*`, `waddles_hub_api_flag_resolution_requests_total`, `waddles_bundle_active_set_*`, `waddles_hub_usage_*`, `waddles_bar_citizen_*`, `waddles_event_sync_*`, `odoo_sync_*` |
| `waddles-data-plane.json` (`waddles-data-plane`) | svc-ingest / svc-process / svc-action: event flow, receiver health, EventSub latency, backlog proxies, dead letters, bundle loads, egress proxy, HTTP RED per service | `svc_ingest_*`, `svc_process_*`, `svc_action_*`, `host_api_*`, `dispatch_dead_lettered_no_executor_total`, `egress_proxy_*` |
| `waddles-streaming.json` (`waddles-streaming`) | svc-streaming: sessions, stream latency histograms (PR #758), HLS segments, RTMP/WebRTC, relay, recording | `stream_*` (#758), `rtc_*`, `rtmp_*`, `hls_*`, `recording_*`, `svc_streaming_relay_*` |
| `waddles-presentation.json` (`waddles-presentation`) | svc-presentation: overlay push latency, fan-out latency, subscribers, dropped frames, image uploads | `svc_presentation_overlay_fanout_latency_seconds`, `svc_presentation_http_request_duration_seconds{POST .../push}`, `svc_presentation_overlay_*`, `svc_presentation_image_*` |
| `waddles-bundle-executor.json` (`waddles-bundle-executor`) | Executor link, host-capability call latency (kv, db), capability-gate / quota / egress denials | `host_api_*`, `svc_*_bundle_loads_total`, `waddles_bundle_kv_*`, `waddles_bundle_tables_*`, `waddles_bundle_quota_denied_total`, `svc_*_egress_denied_total` |

## Alerts

Rule file: `alerts/waddles-slo.rules.yml`. Thresholds are starting points; tune per environment.

| Group | Alert | Fires when |
|---|---|---|
| `slo.http` | `WaddlesHttpHighErrorRate` / `...Critical` | per-service 5xx ratio > 5% (10m) / > 20% (5m) with real traffic |
| | `WaddlesHttpLatencyP99High` / `...Critical` | per-service request p99 > 1 s / > 5 s (10m) |
| `slo.hub-api` | `WaddlesHubApiGrpcErrorRate`, `...SlowCalls` | internal gRPC server-error ratio > 5%; > 1% of calls over 5 s |
| | `WaddlesActiveSetWatermarkStalled`, `...TickFailures` | safe_seq watermark not advanced for > 120 s; failing ticks for 15 m |
| `slo.data-plane` | `WaddlesIngestPublishFailing`, `...ReceiverUnhealthy`, `...EventSubRejections`, `...EventSubLatencyHigh` | publish failures > 2%; receiver stopped on a fatal close; EventSub rejections > 20%; EventSub p99 > 1 s |
| | `WaddlesConsumerLoopDown`, `WaddlesSpineReconnectLoop` | any spine consumer loop 0 for 5 m; > 5 spine connects in 15 m |
| | `WaddlesChangelogLagHigh`, `...GapDetected` | change-log lag > 500; gap / retention overrun forced a reconcile |
| | `WaddlesExecutorDisconnected`, `...HeartbeatTimeouts`, `WaddlesDispatchDeadLettered`, `WaddlesBundleLoadFailures` | no executor session 5 m; missed heartbeats; dispatches dead-lettered; bundle loads failing |
| `slo.capability-gate` | `WaddlesCapabilityDenialSpike` | denial rate > 0.2/s and > 5x the previous hour (capability + quota + egress denials combined) |
| | `WaddlesKvMaxmemoryPolicyViolation` | Valkey `allkeys-*` eviction policy (silently resets kv quotas) |
| `slo.dependencies` (DB connection failures) | `WaddlesBundleDbBackendErrors`, `WaddlesBundleKvBackendErrors` | backend / timeout errors > 5% of db or kv capability calls |
| | `WaddlesChangelogScopeReadFailures`, `WaddlesStreamingNoDatabase` (#758), `WaddlesPostgresDown` (needs postgres_exporter) | active-set DB reads failing; streaming sessions failing with `no_database`; `pg_up == 0` |
| `slo.streaming` | fanout p99 > 250 ms, time-to-first-egress p95 > 15 s, ingest handoff p99 > 1 s, session failures, ICE failures, recording upload failures, relay target failures | stream_* ones need PR #758 |
| `slo.presentation` | `WaddlesOverlayFanoutLatencyHigh`, `...FramesDropped`, `...ImageUploadErrors` | fan-out p99 > 100 ms; dropped frames > 1/s; upload error share > 10% |
| `telemetry-health` (dead exporter) | `WaddlesScrapeTargetDown` | `up == 0` on a Waddles metrics target for 5 m |
| | `WaddlesTelemetryAbsent{Ingest,Process,Action,Presentation,Streaming}` | `svc_*_up` absent 10 m **while** kube-state-metrics shows available replicas (pods alive, exporter dead) |
| | `WaddlesOtlpExportFailing`, `...QueueFilling` | OTel Collector self-metrics: send failures > 0 for 10 m; exporter queue > 80% |
| `workload-health` | `WaddlesPodCrashLooping`, `WaddlesPodRestartingFrequently` | CrashLoopBackOff 5 m; >= 3 restarts in 30 m (kube-state-metrics, namespace `waddlebot` or `waddles`) |

Assumes kube-state-metrics (`kube_*`) and, for the OTLP leg, the Collector's own `otelcol_*` metrics are scraped.
`WaddlesPostgresDown` is optional: delete it if no `postgres_exporter` runs.

## Import and deploy

**Dashboards**

| Method | Steps |
|---|---|
| Grafana UI | Dashboards -> New -> Import -> upload `dashboards/<file>.json` -> pick the Prometheus datasource |
| File provisioning | Point a `providers:` entry of type `file` at this `dashboards/` directory (`allowUiUpdates: false`) |
| Kubernetes sidecar | `kubectl -n <monitoring-ns> create configmap waddles-dashboards --from-file=dashboards/ --dry-run=client -o yaml \| kubectl label --local -f - grafana_dashboard=1 -o yaml \| kubectl apply -f -` (label name per your Grafana sidecar config; stays well under the 1 MiB ConfigMap limit per file) |

**Alert rules**

| Method | Steps |
|---|---|
| Plain Prometheus | `rule_files: ["/etc/prometheus/rules/waddles-slo.rules.yml"]` and mount `alerts/waddles-slo.rules.yml` |
| prometheus-operator | `kubectl apply -f manifests/prometheusrule.yaml` (add the `release:` label your Prometheus `ruleSelector` expects) |

**Scraping**: `kubectl apply -f manifests/servicemonitor.yaml`. The metrics Services are `ClusterIP`; under the
default-deny `CiliumNetworkPolicy` baseline the Prometheus namespace also needs an ingress allow to port 9090 of the
Waddles namespace. The chart's `monitoring.prometheus.serviceMonitor.*` / `monitoring.grafana.dashboards.*` values are
unimplemented stubs; this directory is the standalone path until a chart template consumes it.

## Validation

`make -C k8s/observability validate` (needs `jq`, `python3` + PyYAML, and `promtool` or `docker`; promtool runs from a
digest-pinned `prom/prometheus` container with no network when not on `PATH`). Each step prints what it examined.

| Step | Proves |
|---|---|
| jq structure | every dashboard has a uid, title, unique panel ids, a datasource variable, and every panel has a non-empty query |
| drift | committed dashboards / `prometheusrule.yaml` equal what `obs_tool.py` generates |
| PromQL parse | every dashboard query is valid PromQL (wrapped into a rule file for `promtool check rules`) |
| `check-metrics` | every metric a dashboard or alert uses exists as a literal in `core/**/*.rs`, `hub_api/**/*.py` or `libs/**/*.py`, is a recording rule, an allowlisted external exporter metric (`kube_*`, `otelcol_*`, `pg_*`, `up`), or is explicitly pending PR #758 |
| `promtool check/test rules` | rule syntax, plus 15 unit tests asserting alerts fire (incl. dead exporter, crash loop, denial spike) and stay silent (low traffic, steady-state denials, undeployed service) |

## Changing things

- Dashboards: edit the board functions in `scripts/obs_tool.py`, run `make -C k8s/observability dashboards`, commit the JSON.
- Alerts: edit `alerts/waddles-slo.rules.yml`, add / adjust a case in the `.test.yml`, run `make -C k8s/observability render-rules`.
- When #758 merges, remove the `stream_*` names from `PENDING_758` in `obs_tool.py` (`check-metrics` warns once they appear in the tree).

## Instrumentation gaps found while building this

Dashboards and alerts only use metrics the code emits; these are the holes, and what stands in for them.

| Gap | Effect | Stand-in |
|---|---|---|
| hub-api has no HTTP request-latency / error / throughput instrument, and its code only uses the OTel *API* (no SDK provider installed in `hub_api/`), so its instruments are no-ops until the deployment installs one | no hub-api HTTP RED | internal gRPC RED (`grpc_server_request_duration_seconds`) + domain counters |
| OTel instruments without a bucket View (hub-api gRPC, kv, db) use the SDK's millisecond-oriented defaults | quantiles of these seconds histograms are coarse (all sub-5 s calls share one bucket) | "slower than 5 s" share panels / alerts |
| `bundle-executor` binary exports no metrics (plain atomics, logged) | no executor-side invoke latency | executor link seen from svc-process/svc-action (`host_api_*`); kv/db host-capability latency as the invoke-path proxy |
| `waddles_bundle_capability_denied/authorized_total` use the `metrics` facade with no recorder installed | the central gate counter is not exported | per-capability denial counters (`*_authorize_denied_total`, quota, egress) |
| svc-process / svc-action wire `penguin_spine::NoopMetrics` | no `waddles_group_lag`, `waddles_group_pending`, `waddles_spine_dlq_total`, `waddles_tenant_boundary_violations_total`, per-event stream latency | change-log lag, consumer-loop gauges, dead-letter counter, EventSub latency |
| svc-streaming registers `srt_*` on `prometheus::default_registry()`, which `/metrics` does not serve | no SRT panels | RTMP / WebRTC / HLS series |
| `svc_presentation_overlay_render*` registration helper is never called outside tests | render metrics absent | push + fan-out latency |
| All five HTTP metric middlewares label `path` with the raw URI path (svc-streaming on this branch includes WHIP/WHEP tokens; #758 switches it to the route template) | unbounded label cardinality, and a secret in a label on svc-streaming | dashboards and alerts never group by `path` |
