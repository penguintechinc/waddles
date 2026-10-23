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
- `default` user (what every client authenticates as -- password-only AUTH) is restricted: `-config -flushall -flushdb -replicaof -slaveof -debug -shutdown -acl -failover -module`. `admin` user (separate `VALKEY_DURABLE_ADMIN_PASSWORD` / `infrastructure.valkeyDurable.adminPassword`) is full-access: replication `masteruser` + operator break-glass (`valkey-cli --user admin`). Passwords loaded as SHA-256 hashes.
- Verified against the pinned image (NOPERM on denied cmds, TLS-only primary/replica sync via admin). Unverified: whether `penguin_spine` issues `CONFIG`/`INFO`-style admin calls at startup -- if a spine pod fails with NOPERM, fix the spine, do not widen the ACL.
- TODO: per-client named users (least privilege per service) -- needs a username option in `SpineConfig`.

## Failover (NOT automatic)
`penguin_spine` 0.2.0 (redis 1.7.0, no sentinel/cluster code) cannot discover a promoted replica, so Sentinel is deliberately NOT deployed. Manual promote: `valkey-cli REPLICAOF NO ONE` on ordinal 1, repoint the Service selector pod-name. Follow-up: add Sentinel/Cluster support to `penguin_spine`, then deploy Sentinel.

## Upgrade notes
- valkey-tls SANs gain the durable names: alpha/local regenerate; beta/gamma/prod must rotate the cert (render fails closed otherwise).
- beta/gamma/prod must set `infrastructure.valkeyDurable.password` AND `adminPassword` (or pre-populate `VALKEY_DURABLE_PASSWORD` in waddlebot-secrets).
- In-flight state in the old `infra-redis` is not migrated; drain queues before cutover.
