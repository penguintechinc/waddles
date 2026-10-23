# Valkey durability (delivery state)

Two Valkey instances:

| Instance | Workload | Policy | Holds |
|---|---|---|---|
| `infra-redis` | Deployment | allkeys-lru, 256mb | sessions, cache, bundlekv |
| `infra-valkey-durable` | StatefulSet `valkey-durable` (STATEFUL EXCEPTION, approved 2026-10-23) | **noeviction**, AOF everysec, PVC | spine streams, `waddles:transport:*` drain queues, op_id acks, interaction tokens |

- Primary = ordinal 0; ordinals 1+ are warm replicas (`replicaof`, TLS replication). Service pins ordinal 0.
- Clients read `VALKEY_DURABLE_URL[_TLS]` / `VALKEY_DURABLE_PASSWORD` (secrets.yaml); env var names in pods stay `VALKEY_URL`/`VALKEY_PASSWORD`.
- Persistence off fails the render. At maxmemory writes error loudly (alert at 80%).
- `appendfsync always` (instance-wide) is the zero-loss option for acks/tokens; default `everysec` (<=1s loss on host crash).

## Failover (NOT automatic)
`penguin_spine` 0.2.0 (redis 1.7.0, no sentinel/cluster code) cannot discover a promoted replica, so Sentinel is deliberately NOT deployed. Manual promote: `valkey-cli REPLICAOF NO ONE` on ordinal 1, repoint the Service selector pod-name. Follow-up: add Sentinel/Cluster support to `penguin_spine`, then deploy Sentinel.

## Upgrade notes
- valkey-tls SANs gain the durable names: alpha/local regenerate; beta/gamma/prod must rotate the cert (render fails closed otherwise).
- beta/gamma/prod must set `infrastructure.valkeyDurable.password` (or pre-populate `VALKEY_DURABLE_PASSWORD` in waddlebot-secrets).
- In-flight state in the old `infra-redis` is not migrated; drain queues before cutover.
