# svc-analytics deployment (Helm)

Helm wiring for the Rust `svc-analytics` consumer. Data contract, schema, roles and privacy rules:
[analytics-foundation.md](analytics-foundation.md). Template: `k8s/helm/waddlebot/templates/svc-analytics.yaml`.

Everything is **OFF by default**.

| Switch | Default | Meaning |
|---|---|---|
| `pipeline.svcAnalytics.enabled` | `false` | Renders the Deployment, Service, CiliumNetworkPolicy, Tetragon policy (+ HPA/PDB when scaled) |
| `config.analyticsStreamEnabled` | `false` | ENV baseline `FLAG_WADDLES_ANALYTICS_STREAM` (PostHog -> ENV -> default); gates the stream producer and writer |

## Posture

- gRPC-only, no HTTP/metrics port; native `grpc.health.v1` startup/liveness/readiness probes on `pipeline.svcAnalytics.healthPort`; telemetry via OTLP (`OTEL_*`).
- Rootless (uid 10001), read-only rootfs, caps dropped, stateless (`/tmp` emptyDir only), no service-account token, graceful shutdown (preStop + grace period), resources set; HPA/PDB via `autoscaling.services.svcAnalytics` / `podDisruptionBudget`.
- CiliumNetworkPolicy: ingress deny-all (kubelet probes are host-local); egress only DNS, OTLP collector, Postgres primary, Valkey (durable instance when enabled, else cache).
- Tetragon `sys_execve` allowlist (enforced); PSA `restricted` comes from the chart namespace labels.
- Reads the stream `waddles:analytics` (`streamKey`) as consumer group `svc_analytics` (`consumerGroup`); durable-Valkey TLS wiring is identical to svc-process.

## Database identity

The pod connects as LOGIN role `waddles_svc_analytics` (`config/postgres/service-roles.yaml`), a member of the NOLOGIN group `analytics_writer` created by migration 0129, so it inherits only `analytics_fp` DML plus `SELECT` on `data_use_exclusions`.

- Password: key `PW_WADDLES_SVC_ANALYTICS` in `<release>-db-credentials`, generated create-if-absent by the provision-secrets hook, never overwritten; `infrastructure.postgresql.serviceRoles.existingSecret` is honoured.
- `LOGIN` + password are (re)asserted idempotently by the existing db-migrate Job on every run; nothing is printed.

## Enabling

1. Image `svc-analytics` must exist in the registry and be registered in CI image enumeration (see PR notes).
2. `--set pipeline.svcAnalytics.enabled=true` (then `config.analyticsStreamEnabled=true` once the producer ships).
