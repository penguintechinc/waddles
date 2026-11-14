# Sending telemetry to SigNoz on dal2alpha

Waddles workloads send OpenTelemetry (traces, metrics, logs) to the self-hosted SigNoz in namespace
`signoz` on dal2alpha. The destination comes ONLY from standard `OTEL_*` env vars, rendered by the
Helm chart from one values block. Nothing is hardcoded in code or images, and no key is needed.

## What is wired

| Piece | Where |
|---|---|
| Values block | `global.otel` in `k8s/helm/waddlebot/values.yaml` (default **off**); `values-dal2alpha.yaml` turns it **on** |
| Env helper | `waddlebot.otel.env` in `templates/_otel.tpl`, one include line at the top of each emitter's `env:` |
| Egress rule | `waddlebot.otel.egressRule`, added inside each emitter's existing egress CiliumNetworkPolicy |
| Collector | `http://signoz-otel-collector.signoz.svc.cluster.local:4317` (OTLP gRPC) |
| Auth | none. The SigNoz OTLP receiver has no auth extension and is reachable from in-cluster only |

Emitters (`OTEL_SERVICE_NAME` = component name): `hub-api`, `svc-ingest-rust`, `svc-process-rust`,
`svc-action-rust`, `svc-bundles-executor`, `svc-presentation`, `svc-streaming`, `egress-proxy`.
hub-webui, databases, caches and cron/seed jobs get no OTEL env and no collector egress.

## Env vars rendered (per emitter container)

| Var | Value (dal2alpha) |
|---|---|
| `OTEL_SERVICE_NAME` | component name, for example `hub-api` |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=alpha,service.namespace=waddlebot,k8s.pod.name=$(K8S_POD_NAME)` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `_PROTOCOL` | collector URL above / `grpc` |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `5000` (ms; caps how long one export may block, so a dead collector cannot stall a service) |
| `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER` | `otlp` (set `global.otel.exporters.<signal>: none` to turn one off) |
| `OTEL_TRACES_SAMPLER` / `_ARG` | `parentbased_traceidratio` / `1.0` |
| `OTEL_METRIC_EXPORT_INTERVAL` | `15000` |

`svc-presentation` keeps its older override: `presentation.rust.otlpEndpoint`, when set, wins over
`global.otel.endpoint`. If an operator adds `OTEL_*` through `hubApi.extraEnv`, the later entry wins.

## Which signals actually leave each service today

Enabling the env is necessary but not sufficient. What leaves a pod depends on the service's code
(survey of the repo at the time of this change):

| Service | Traces | Metrics | Logs (OTLP) | Notes |
|---|---|---|---|---|
| svc-presentation | yes | yes | no (stdout JSON) | honors endpoint, protocol, service name, resource attributes |
| svc-streaming | yes | yes | no (stdout JSON) | same; W3C TraceContext propagator installed |
| svc-bundles-executor | no | yes (gRPC only) | no | reads endpoint, metrics endpoint, service name only |
| svc-ingest / svc-process / svc-action / egress-proxy | via `penguin-logging` | via `penguin-logging` | via `penguin-logging` | exporter lives in the external crate, not verifiable in this repo |
| hub-api | no | no | no | OTel API only; no SDK provider or exporter is installed, so spans and metrics are dropped |

Stdout logs of every pod are still collected by the SigNoz k8s-infra agents (`signoz-agents`).
Follow-ups for the owners are listed in the PR that introduced this page.

## Find a service in SigNoz

1. Open the SigNoz UI, then Services. Filter `deployment.environment = alpha`.
2. Service names are the component names in the table above (`hub-api`, `svc-ingest-rust`, ...).
3. Narrow to one pod with the resource attribute `k8s.pod.name`.
4. If a service is missing, check in this order: the pod has `OTEL_EXPORTER_OTLP_ENDPOINT` set
   (`kubectl --context dal2alpha -n waddlebot get deploy <name> -o yaml`), the egress rule exists
   in its CiliumNetworkPolicy, and the service's code actually exports (table above).

## Follow one command through traces, metrics and logs

1. Traces: Traces explorer, filter `service.name = svc-ingest-rust` (the entry hop) and the time of
   the command. Open a trace and note its `trace_id`.
2. Logs: Logs explorer, filter `trace_id = <id>`. Logs that carry the trace context link back to the span.
3. Metrics: from the span's service page, Metrics shows latency histograms and request rate for the
   same `service.name`; compare the spike with the trace's timestamp.
4. Hop boundaries: the spine envelope between ingest, process and action carries an application-level
   `traceparent`, not the OTel propagator on gRPC hops, so a command may appear as one trace per service
   joined by that value. A single end-to-end trace needs W3C propagation on those hops (open follow-up).

Telemetry carries UUIDs and component names only. Never put raw user handles, emails or message
text into span attributes, metric labels or log fields.

## Sampling

`samplerRatio: "1.0"` on alpha keeps every trace. Head sampling below 1.0 decides before a trace is
complete, so it cannot keep "all slow or failed traces". That needs tail sampling in the SigNoz
collector. This is an ask for the SigNoz owner (the collector is not managed by this chart):

```yaml
# signoz-otel-collector config (otel-collector-config.yaml)
processors:
  tail_sampling:
    decision_wait: 10s
    num_traces: 50000
    expected_new_traces_per_sec: 200
    policies:
      - name: keep-errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: keep-slow
        type: latency
        latency: { threshold_ms: 500 }
      - name: baseline
        type: probabilistic
        probabilistic: { sampling_percentage: 10 }
service:
  pipelines:
    traces:
      receivers: [otlp, jaeger]
      processors:
        - signozspanmetrics/delta   # before sampling, so RED metrics see 100% of spans
        - tail_sampling             # decide on whole traces
        - signozspanmapper
        - signozllmpricing
        - filter/drop_stale
        - batch
      exporters: [clickhousetraces, metadataexporter, signozmeter]
```

Caveats for the owner: the collector image must contain the `tail_sampling` processor (check
`otelcol components`); tail sampling needs every span of a trace on the same collector replica, and
there are two replicas, so either run one replica or front them with a trace-id-aware load balancer
(`loadbalancing` exporter) before enabling it.

## Network path

| Hop | Policy | State |
|---|---|---|
| waddlebot pod to collector TCP 4317 | egress rule added to the emitter's own CiliumNetworkPolicy by this chart | rendered when `global.otel.enabled` and `global.otel.egress.enabled` |
| into collector 4317/4318 | `signoz/allow-otlp-ingress`: `fromEntities: [cluster]` | already admits the waddlebot namespace, no change needed |

The rule is added inside the existing egress policies, not as a separate policy: a standalone policy
that selects a pod with no other egress policy would flip that pod to default-deny egress (alpha and
local run `egressPolicy.workloadEgress.enabled: false`). Cilium unions allows, so this only adds.

Optional tightening for the SigNoz owner: replace `fromEntities: [cluster]` with
`fromEndpoints` matching `io.kubernetes.pod.namespace: waddlebot` (and `signoz-agents`).

Egress on the live cluster before this change is deployed (2026-11-14, checked with
`cilium-dbg monitor` while a probe connected from hub-api): `egress, action deny, match none,
10.244.2.168:39294 -> 10.244.4.243:4317 tcp SYN`. That deny is what the rule removes.

Tetragon exec allowlists are unaffected: the exporters run in-process (no new binary).
