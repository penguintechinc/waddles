# Valkey durability (delivery state)

Two Valkey instances:

| Instance | Workload | Policy | Holds |
|---|---|---|---|
| `infra-redis` | Deployment | allkeys-lru, 256mb | sessions, cache, bundlekv |
| `infra-valkey-durable` | StatefulSet `valkey-durable` (STATEFUL EXCEPTION, approved 2026-10-23) | **noeviction**, AOF everysec, PVC | spine streams, `waddles:transport:*` drain queues, op_id acks, interaction tokens |

- Primary = ordinal 0; ordinals 1+ are warm replicas (`replicaof`, TLS replication). Service pins ordinal 0.
- Clients read `VALKEY_DURABLE_URL_TLS[_AUTH]` / `VALKEY_DURABLE_PASSWORD` (secrets.yaml); env var names in pods stay `VALKEY_URL`/`VALKEY_PASSWORD`.
- Persistence off fails the render. At maxmemory writes error loudly (alert at 80%).
- `appendfsync always` (instance-wide) is the zero-loss option for acks/tokens; default `everysec` (<=1s loss on host crash).

## Transport security (TLS-only)
- `infrastructure.redis.tls.enabled=true` => durable instance runs `--port 0` (plaintext listener OFF), `--tls-port` only; Services/NetworkPolicy/egress expose only the TLS port; `VALKEY_DURABLE_URL` (plaintext) is NOT published.
- Rust spine + hub-api + seeder: `VALKEY_DURABLE_URL_TLS` (credential-free) + `VALKEY_DURABLE_PASSWORD` + CA at `/etc/waddles/ca/valkey-ca.crt`.
- Python stages (svc-ingest/process/action): `VALKEY_DURABLE_URL_TLS_AUTH` = `rediss://:<pw>@host:port/0?ssl_cert_reqs=required&ssl_ca_certs=/etc/waddles/ca/valkey-ca.crt` (redis-py parses both from the URL; CA volume mounted by the chart; no code change).
- TLS off (local/ci only) keeps the plaintext port + `VALKEY_DURABLE_URL`. **beta/gamma/prod values currently leave `infrastructure.redis.tls.enabled` off, so they still run plaintext -- enabling it needs the valkey-tls cert rotation (see Upgrade notes); TODO before real customer traffic.**

## ACL hardening
- `default` user (what every client authenticates as -- password-only AUTH) is an ALLOW-list: `-@all +@connection +@read +@write +@stream +@keyspace +@transaction +@scripting -@dangerous` (keys `~*`, channels `&*`). Built from the verified client command set (AUTH/PING/CLIENT, XADD/XLEN/XGROUP/XACK/XAUTOCLAIM/XPENDING/XINFO/XREADGROUP, GET, SET/DEL/LPUSH/LTRIM/EXPIRE/GETDEL, EVAL, MULTI/EXEC). The trailing `-@dangerous` is needed because `@write`/`@keyspace` still include FLUSHALL/FLUSHDB/SWAPDB/KEYS/MIGRATE (verified against the pinned image); CONFIG/INFO/ACL/SHUTDOWN/REPLICAOF/DEBUG are never granted (NOPERM).
- `admin` user: full-access, replication `masteruser` + operator break-glass (`valkey-cli --user admin`). Its password lives in a DEDICATED Secret `<fullname>-valkey-durable-admin` (key `VALKEY_DURABLE_ADMIN_PASSWORD`, `infrastructure.valkeyDurable.adminPassword`), referenced ONLY by the `valkey-durable` StatefulSet via `secretKeyRef` -- it is NOT in the envFrom'd shared secret, so an app pod cannot AUTH as admin (render-tested). Existing clusters: the value is migrated once from the old shared-secret key on upgrade. Passwords loaded as SHA-256 hashes.
- Unverified: if a spine pod fails with NOPERM (e.g. an unexpected INFO/CONFIG call at startup), fix the spine, do not widen the ACL.
- bundle-kv (`svc-action-rust`/`svc-process-rust`) runs on the CACHE instance (`infra-redis`) via `VALKEY_CACHE_URL`/`VALKEY_CACHE_PASSWORD` (TLS: `VALKEY_URL_TLS` + `REDIS_PASSWORD`); unset falls back to `VALKEY_URL`. Spine/relay/drain/acks stay on the durable instance, so kv quota counters cannot fill it and the `CONFIG GET maxmemory-policy` probe runs where it is permitted (no NOPERM).
- TODO: per-client named users (least privilege per service) -- needs a username option in `SpineConfig`.

## Failover (NOT automatic)
`penguin_spine` 0.2.0 (redis 1.7.0, no sentinel/cluster code) cannot discover a promoted replica, so Sentinel is deliberately NOT deployed. Manual promote: `valkey-cli REPLICAOF NO ONE` on ordinal 1, repoint the Service selector pod-name. Follow-up: add Sentinel/Cluster support to `penguin_spine`, then deploy Sentinel.

## Upgrade notes
- valkey-tls SANs gain the durable names: alpha/local regenerate; beta/gamma/prod must rotate the cert (render fails closed otherwise).
- beta/gamma/prod must set `infrastructure.valkeyDurable.password` AND `adminPassword` (or pre-populate `VALKEY_DURABLE_PASSWORD` in waddlebot-secrets).
- In-flight state in the old `infra-redis` is not migrated; drain queues before cutover.
