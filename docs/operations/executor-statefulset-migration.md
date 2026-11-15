# svc-bundles-executor: Deployment -> StatefulSet upgrade (executor Phase 1)

The executor is now one StatefulSet per pool (`templates/svc-bundles-executor.yaml`).
Pod name is the `executor_id` that stages use for rendezvous slot sharding, so it has to stay
the same across restarts. A Deployment's random pod suffix changes on every restart.

## What changes on `helm upgrade`

| Object | Before | After |
|---|---|---|
| `waddlebot-svc-bundles-executor` | Deployment | StatefulSet (`podManagementPolicy: Parallel`) |
| `waddlebot-svc-bundles-executor-headless` | none | headless Service with no ports (StatefulSet `serviceName`) |
| `waddlebot-svc-bundles-executor` HPA (cpu/mem) | rendered (targets `<unknown>` on dal2alpha) | removed |
| `waddlebot-svc-bundles-executor` PDB | minAvailable 1 | maxUnavailable 1 (one owner of each slot is always warm) |
| Executor env | none | `EXECUTOR_ID` / `POD_NAME` (downward API), `EXECUTOR_POOL`, `EXECUTOR_MEMORY_BUDGET_MB`, `EXECUTOR_PRECOMPILED_PREFIX`, `OTEL_*` |

The Deployment and the StatefulSet share a name but are different kinds, so Helm creates the
StatefulSet and then deletes the Deployment (its pods are removed by cascade) in the same upgrade.
No manual delete is needed. The StatefulSet selector adds `waddles.io/executor-pool`, which means it
never adopts the old Deployment's pods.

## Procedure (any environment)

1. Pre-check:
   `kubectl --context <ctx> -n waddlebot get deploy,sts,hpa,pdb | grep bundles-executor`
2. Upgrade as usual (helm v3.17.3 on dal2alpha, see `make dal2alpha-deploy`).
3. Verify:
   - `kubectl --context <ctx> -n waddlebot rollout status sts/waddlebot-svc-bundles-executor`
   - `get deploy waddlebot-svc-bundles-executor` returns NotFound.
   - The pods are named `waddlebot-svc-bundles-executor-0` and `-1`, and both are Ready.
4. During the overlap, old pods send no `executor_id`. Stages treat any executor without an id
   as broadcast, log a WARN, and stop doing so once the old pods are gone. This takes seconds
   and has no correctness impact.

Rollback: `helm rollback` re-creates the Deployment and deletes the StatefulSet in the same way.
If a rollback stalls on the Deployment, delete the StatefulSet with the default cascade. Never use
`--cascade=orphan`, because orphaned pods would keep dialing stages with a stale identity.

## Autoscaling prerequisites

`pipeline.rustDataPlane.bundleExecutor.autoscaling.mode` takes one of these values:

- `none` is the default and is used on dal2alpha, which as of 2026-11-11 has no KEDA, no
  metrics API and no OTel collector. Replicas stay fixed (2 or more, because R=2 owners per slot).
- `keda` requires KEDA (`keda.sh/v1alpha1` CRDs) plus a Prometheus that receives the OTLP
  metrics `queue_wait_seconds` and `pool_exhausted_total` with an `executor_pool` label. Set
  `autoscaling.prometheus.serverAddress`. If the CRD is absent, the render fails.
- `hpa-external` requires an `external.metrics.k8s.io` adapter (for example prometheus-adapter)
  that exposes `executor_queue_wait_seconds_p95` and `executor_pool_exhausted_per_second`. If the
  API is absent, the render fails.

Install KEDA (operator action, cluster-scoped, pinned chart version):
`helm --kube-context <ctx> install keda kedacore/keda -n keda --create-namespace --version <pinned>`.
After that, set `mode: keda` in the environment's values file.

## OTLP

The endpoint comes from values and is never hardcoded:

1. `otel.exporter.endpoint`, if set.
2. Otherwise `http://<fullname>-otel-collector:4317` when `otel.collector.enabled`. The executor
   CNP and data-plane egress CNPs then allow port 4317 to that collector.
3. Otherwise no `OTEL_EXPORTER_OTLP_ENDPOINT` is set and export stays off. A missing exporter
   never breaks the app.
