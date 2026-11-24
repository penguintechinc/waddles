# Source-stream consumer-group reconcile (hub-api)

Every `app_source_bindings` row means "svc-process runs one `XREADGROUP` consumer for
`(stream, group = app_id)`". The stream is
`waddles:t:{tenant.slug}:c:{community.name|_tenant}:src:{platform}:{source_id}:events`
(penguin-spine `Scope::source_stream`; Python mirror `flask_core.stream_pipeline.source_stream_key`).
svc-process never creates the group: until a group exists every read answers `NOGROUP`, logged
`source-binding consumer: consumer group not yet provisioned (NOGROUP), retrying`.

## Failure this fixes (alpha, 2026-11-14)

Groups were created once, right after activation commit. The key embeds the tenant **slug** and
community **name**; migration 0098 renamed the catch-all tenant `global` -> `default` (same id).
Apps activated before the rename kept their groups on the dead `...t:global:...` stream; svc-ingest
publishes to and svc-process reads `...t:default:...`, where those apps never had a group, and
nothing re-ran provisioning. `!ping` (and roll, dj, eightball, csping, pyping) never executed.

On alpha the six missing groups (ping, roll, dj, eightball, csping, pyping) were created by hand
with `XGROUP CREATE <live stream> <app> $`; this reconcile makes that unnecessary in future. The
first pass after deploying it should therefore report `groups_created=0 groups_already_present=<all bindings>`
(`BUSYGROUP` counts as already present); any `created > 0` afterwards means real drift.

## Design

`hub_api/services/source_group_reconciler.py`, started from `app.py` startup in every replica:

| Property | Choice | Why |
|---|---|---|
| Owner | hub-api (control plane) | hub-api owns consumer-group lifecycle; data plane only reads |
| Trigger | level-triggered: startup + every `SOURCE_GROUP_RECONCILE_INTERVAL_SECONDS` (default 60) | self-heals renames, flushed Valkey, failed best-effort provisioning |
| Source of truth | `app_source_bindings JOIN tenants LEFT JOIN communities` | current slug/name, never cached |
| Operation | `XGROUP CREATE <stream> <app_id> $ MKSTREAM`, `BUSYGROUP` = already present | idempotent; safe concurrently in N replicas |
| Start id | `$` | a late group must not replay days-old commands; events published in the NOGROUP window are not recovered (logged WARN) |
| Old streams | never destroyed | non-mutating cleanup left to an operator |

## Signals

| Signal | Meaning |
|---|---|
| `waddles_hub_source_group_reconcile_groups_total{result}` | `created` (was missing), `exists`, `failed`, `unresolvable` |
| `waddles_hub_source_group_reconcile_passes_total{result}` | `ok`, `error` (any failed/unresolvable binding or a dead pass) |
| `waddles_hub_source_group_reconcile_duration_ms` | pass wall time |
| ERROR `source group reconcile: XGROUP CREATE failed` | per binding, with `error_type`/`error`/`stream_key` (no PII) |
| WARN `provisioned a missing consumer group` | a group had to be created; `created > 0` in steady state means drift |

Alert on `passes_total{result="error"}` > 0 and on `groups_total{result="created"}` rate > 0 outside
a deploy window.
