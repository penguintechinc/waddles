# Valkey durability (delivery state)

Three Valkey instances:

| Instance | Workload | Policy | Holds |
|---|---|---|---|
| `infra-redis` | Deployment | allkeys-lru, 256mb | sessions, cache |
| `infra-valkey-kv` | StatefulSet `valkey-kv` (single pod, STATEFUL EXCEPTION, approved 2026-10-23) | **noeviction**, AOF everysec, PVC (retained) | bundle-kv quota counters / kv state |
| `infra-valkey-durable` | StatefulSet `valkey-durable` (STATEFUL EXCEPTION, approved 2026-10-23) | **noeviction**, AOF everysec, PVC | spine streams, `waddles:transport:*` drain queues, op_id acks, interaction tokens |

- Primary = ordinal 0; ordinals 1+ are warm replicas (`replicaof`, TLS replication). Service pins ordinal 0.
- Clients read `VALKEY_DURABLE_URL_TLS[_AUTH]` / `VALKEY_DURABLE_PASSWORD` (secrets.yaml); env var names in pods stay `VALKEY_URL`/`VALKEY_PASSWORD`.
- Persistence off fails the render. At maxmemory writes error loudly (alert at 80%).
- `appendfsync always` (instance-wide) is the zero-loss option for acks/tokens; default `everysec` (<=1s loss on host crash).

## Transport security (TLS-only)
- `infrastructure.redis.tls.enabled=true` => durable instance runs `--port 0` (plaintext listener OFF), `--tls-port` only; Services/NetworkPolicy/egress expose only the TLS port; `VALKEY_DURABLE_URL` (plaintext) is NOT published.
- Rust spine + hub-api + seeder: `VALKEY_DURABLE_URL_TLS` (credential-free) + `VALKEY_DURABLE_PASSWORD` + CA at `/etc/waddles/ca/valkey-ca.crt`.
- Python stages (svc-ingest/process/action): `VALKEY_DURABLE_URL_TLS_AUTH` = `rediss://:<pw>@host:port/0?ssl_cert_reqs=required&ssl_ca_certs=/etc/waddles/ca/valkey-ca.crt` (redis-py parses both from the URL; CA volume mounted by the chart; no code change).
- TLS off (local/ci only) keeps the plaintext port + `VALKEY_DURABLE_URL`. **TLS now defaults ON and a durable-without-TLS render fails; beta/gamma/prod need the valkey-tls cert rotation (see Upgrade notes).**

## ACL hardening
- `default` user (what every client authenticates as -- password-only AUTH) is an ALLOW-list: `-@all +@connection +@read +@write +@stream +@keyspace +@transaction +@scripting -@dangerous` (keys `~*`, channels `&*`). Built from the verified client command set (AUTH/PING/CLIENT, XADD/XLEN/XGROUP/XACK/XAUTOCLAIM/XPENDING/XINFO/XREADGROUP, GET, SET/DEL/LPUSH/LTRIM/EXPIRE/GETDEL, EVAL, MULTI/EXEC). The trailing `-@dangerous` is needed because `@write`/`@keyspace` still include FLUSHALL/FLUSHDB/SWAPDB/KEYS/MIGRATE (verified against the pinned image); CONFIG/INFO/ACL/SHUTDOWN/REPLICAOF/DEBUG are never granted (NOPERM).
- `admin` user: full-access, replication `masteruser` + operator break-glass (`valkey-cli --user admin`). Its password lives in a DEDICATED Secret `<fullname>-valkey-durable-admin` (key `VALKEY_DURABLE_ADMIN_PASSWORD`, `infrastructure.valkeyDurable.adminPassword`), referenced ONLY by the `valkey-durable` StatefulSet via `secretKeyRef` -- it is NOT in the envFrom'd shared secret, so an app pod cannot AUTH as admin (render-tested). There is NO migration from the old shared-secret key. Passwords loaded as SHA-256 hashes.
- Unverified: if a spine pod fails with NOPERM (e.g. an unexpected INFO/CONFIG call at startup), fix the spine, do not widen the ACL.
- bundle-kv (`svc-action-rust`/`svc-process-rust`) runs on its OWN instance, `valkey-kv` (see below), via `VALKEY_KV_URL` (credential-free `rediss://`, secretKeyRef) + `VALKEY_KV_PASSWORD`. Spine/relay/drain/acks stay on the durable instance (`VALKEY_URL`). The old `VALKEY_CACHE_URL`/`VALKEY_CACHE_PASSWORD` env are gone. kv disabled (local) => `VALKEY_KV_URL`/`VALKEY_KV_PASSWORD` fall back to the cache instance.
- TODO: per-client named users (least privilege per service) -- needs a username option in `SpineConfig`.

## Dedicated kv instance (`valkey-kv`)
- Why: quota counters are integrity-critical -- eviction = quota bypass -- so they cannot live on the LRU cache; they also must not fill/share the delivery (durable) instance.
- Simple on purpose: 1 pod, NO replication/Sentinel/admin user (not on the 99.99% delivery path; a restart degrades quota checks, not delivery). `maxmemory-policy noeviction` hard-wired, `appendonly yes`, `appendfsync everysec`, PVC retained on delete/scale-down, persistence off fails the render. TLS-only when `redis.tls.enabled` (`--port 0`), rootless uid 999, CiliumNetworkPolicy.
- ACL (single password-only `default` user, SHA-256 hashed): `-@all +@connection +@read +@write +@keyspace +@transaction +@scripting -@dangerous '+config|get'`. `+config|get` MUST come after `-@dangerous` (CONFIG GET is itself `@dangerous`; verified against the pinned image: the user-specified order leaves the probe NOPERM). Result: `CONFIG GET maxmemory-policy` works, CONFIG SET/FLUSHALL/KEYS/INFO stay NOPERM.
- Password in its own Secret `<fullname>-valkey-kv` (`VALKEY_KV_PASSWORD`, `VALKEY_KV_URL_TLS` credential-free, `VALKEY_KV_URL_TLS_AUTH` with password+CA; plaintext `VALKEY_KV_URL` only when TLS is off). Never envFrom'd; referenced only by the kv StatefulSet and the two rust pods via secretKeyRef. Nothing secret-derived appears in any non-secret field (render-tested). Existing cluster: password REPLACE_ME => generated alpha/local, required elsewhere (`infrastructure.valkeyKv.password`).
- Sizing: alpha 64mb/512Mi, beta/gamma 128mb/1Gi, prod 256mb/2Gi; local disabled.

## Durable requires TLS (fail-render)
`infrastructure.valkeyDurable.enabled` && TLS material unavailable (`infrastructure.redis.tls.enabled=false`) => `helm template/install` FAILS (`waddlebot.assertDurableTls`, called from valkey-durable.yaml, secrets.yaml, and the svc-action/process-rust VALKEY_URL override). Otherwise delivery state would silently fall back to the cache/plaintext VALKEY_URL, bypassing durability. Fix: `infrastructure.redis.tls.enabled=true` + rotate/supply the valkey-tls cert. `infrastructure.redis.tls.enabled` now defaults to true in `values.yaml` (local sets it false with durable+kv disabled). beta/gamma/prod therefore need the valkey-tls cert (SANs now include `infra-valkey-kv`) before they render.

## Failover (NOT automatic)
`penguin_spine` 0.2.0 (redis 1.7.0, no sentinel/cluster code) cannot discover a promoted replica, so Sentinel is deliberately NOT deployed. Manual promote: `valkey-cli REPLICAOF NO ONE` on ordinal 1, repoint the Service selector pod-name. Follow-up: add Sentinel/Cluster support to `penguin_spine`, then deploy Sentinel.

## Upgrade notes
- valkey-tls SANs gain the durable AND kv names (`infra-valkey-kv`): alpha/local regenerate; beta/gamma/prod must rotate the cert (render fails closed otherwise).
- beta/gamma/prod must set `infrastructure.valkeyDurable.password`, `adminPassword` AND `infrastructure.valkeyKv.password` (or pre-populate `VALKEY_DURABLE_PASSWORD` in waddlebot-secrets).
- In-flight state in the old `infra-redis` is not migrated; drain queues before cutover.
- **ROTATE the admin password (security).** The old admin password was stored in the shared `waddlebot-secrets` (envFrom to every app pod) and is **COMPROMISED**; it must be rotated to a genuinely NEW value.
  - beta/gamma/prod: the admin password comes ONLY from an explicit `infrastructure.valkeyDurable.adminPassword`. Unset, empty or `REPLACE_ME*` => the render FAILS closed ("Set a FRESH infrastructure.valkeyDurable.adminPassword ..."). alpha/local auto-generate a fresh value into `<release>-valkey-durable-admin`.
  - The key is blanked (`""`) in `waddlebot-secrets`, removing the leaked value from the shared secret on upgrade. The real value lives only in `<release>-valkey-durable-admin`, referenced only by the `valkey-durable` StatefulSet.
  - **Operator responsibility:** the chart does NOT detect a deliberately re-entered old password (stateless renders cannot reliably compare against a secret that has been blanked). Never reuse the old value.
  - Rotation procedure: (1) set a new `infrastructure.valkeyDurable.adminPassword` (out-of-git values/ExternalSecret), (2) bump `infrastructure.valkeyDurable.adminPasswordRevision` (non-secret) -> pod annotation `valkey.waddles/admin-revision` changes -> primary + replicas roll and re-establish replication with the new `masteruser` password. No password or hash is placed in pod metadata.

## Admin password rotation (REQUIRED, operator-enforced)

- **Why:** the previous durable admin password was exposed in the shared `waddlebot-secrets` (readable by every app pod). It is **COMPROMISED** and MUST be rotated to a genuinely new value on EVERY environment.
- **Chart enforces:**
  - beta/gamma/prod FAIL the render unless `infrastructure.valkeyDurable.adminPassword` is set (not empty, not `REPLACE_ME*`).
  - alpha/local auto-generate a fresh value.
  - The admin key in the shared `waddlebot-secrets` is blanked.
  - The admin secret (`<release>-valkey-durable-admin`) is isolated — only `valkey-durable` reads it.
- **Chart does NOT enforce (operator responsibility):** that the new password DIFFERS from the old compromised one — impossible to verify safely in a stateless render. NEVER re-enter the old value.
- **Runbook / GitOps enforcement (how reuse is actually caught):**
  1. Before upgrading, record `sha256` of the OLD admin password (`printf %s "$OLD" | sha256sum`) — store the hash only, never the value.
  2. GitOps/PR review, or a CI check on the values files, REJECTS any `adminPassword` whose sha256 matches the recorded old hash.
  3. Bump `infrastructure.valkeyDurable.adminPasswordRevision` on every rotation — the pod annotation `valkey.waddles/admin-revision` changes, so primary + replicas restart with the new `masteruser` password.
- **One-time migration:** on first upgrade past this change, every environment MUST set a NEW `adminPassword` AND bump the revision. The old value stops working for replication once pods restart.
