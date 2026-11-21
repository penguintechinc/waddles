# Valkey per-service ACL users

**Security finding:** every Valkey client authenticated as the shared `default` user with `~*`.
Any workload could read or write any key: delivery streams, bundle kv (`bundlekv:*`), dedupe and
replay keys, and the `waddles:platform-unban:*` lists. The org rule (`backend-database.md`) requires
per-service key-prefix ACLs.

The fix is one Valkey ACL user per service, on each instance that service uses. Each user is
limited to the key families it actually touches and an allow-list of command categories. The
source of truth is `infrastructure.valkeyAcl.users` in `k8s/helm/waddlebot/values.yaml`, rendered by
`templates/_valkey_acl.tpl`.

## Users

| User | valkey-durable keys | valkey-kv keys | Commands |
|---|---|---|---|
| `svc-ingest` | `waddles:t:*:src:*`, `waddles:analytics`, `waddles:transport:*`, `outbound:seen:*`, `waddles:ingest:*`, `waddles.ingest.interaction.*`, `waddles:webhook:dedup:*`, `waddles:eventsub:dedup:*`, `waddles:control:twitch:*`, `waddles:discord:*`, `waddles:connector:identify-budget:*`, `waddles:canary:latency:*`, `waddles:platform-unban:*:events` (producer), read-only `keys:tenant-dek:invalidate` | none | durable base |
| `svc-process` | `waddles:t:*:src:*`, `waddles:t:*:app:*:action`, `waddles:dlq:process`, `waddles:bundle-admin:*`, read-only `bundle:grants:invalidate`, `keys:tenant-dek:invalidate`, `waddles:bundle_config:v*` | `bundlekv:*`, `media:*` | base |
| `svc-action` | `waddles:t:*:app:*:action`, `waddles:dlq:action`, `waddles:transport:*`, `waddles:ratelimit:*`, `waddles:event-sync:*`, `waddles:usage`, `waddles:platform-unban:*` (consumer), read-only `bundle:grants:invalidate`, `waddles:bundle_config:v*` | `bundlekv:*` | base |
| `svc-analytics` | `waddles:analytics`, `waddles:analytics:dlq` | none | `+@connection +@stream` |
| `hub-api` | `waddles:jwt:*`, `waddles:landing:*`, `waddles:bundle_config:*`, `waddles:bundle-admin:requests`, `waddles:bundle-admin:result:*`, `waddles:event-sync:discord:requests`, `waddles:event-sync:discord:result:*`, `waddles:usage`, `bundle:grants:invalidate`, `keys:tenant-dek:invalidate` | `bundlekv:*:data:*` (erasure) | durable base; kv `+scan +unlink` only |
| `role-sync-inbound` | `waddles:t:*:src:discord:*`, `waddles:dlq:role_sync_inbound` | none | `+@connection +@read +@write +@stream` |
| `landing-stats` | `waddles:landing:*` | none | `+@connection +@read +@write +@keyspace` |
| `core-bundle-seeder` | `bundle:grants:invalidate`, `waddles:bundle_config:*` | none | base |
| bundle executors | **no user** | **no user** | none |
| `probe` (chart-owned) | none | none | `+ping` only (liveness and readiness) |
| `admin` (chart-owned) | `~*` `+@all` (replication masteruser and break-glass; its own server-only Secret) | none | all |
| `default` | `reset` once rollout step 3 is done (off; unauthenticated connections get NOAUTH) | same | none |

**Base allow-lists:**

- durable: `-@all +@connection +@read +@write +@stream +@keyspace +@transaction +@scripting -@dangerous`
- kv: the same, without `@stream`, plus `+time +config|get`, which come after `-@dangerous` (see the comment in `valkey-kv.yaml`)

No service user can run FLUSHALL, FLUSHDB, KEYS, CONFIG SET, ACL, DEBUG or SHUTDOWN, or use pub/sub
channels (`resetchannels`).

Bundles reach kv only through the host capability served by svc-process and svc-action
(`bundle_host_kv`). Per-app isolation inside `bundlekv:*` is enforced by `KvScope` in the host,
not by the ACL. The executor pods carry no Valkey credential at all.

## Secrets

- `<fullname>-valkey-user-<name>` holds one service's own passwords: `VALKEY_DURABLE_PASSWORD` and/or
  `VALKEY_KV_PASSWORD`. The provision-secrets hook generates them create-if-absent in every tier
  and never overwrites them (`strict-kept`). They are never envFrom'd and never logged.
- `<fullname>-valkey-durable-admin` gains `VALKEY_DURABLE_PROBE_PASSWORD`. The new server-only
  `<fullname>-valkey-kv-server` holds `VALKEY_KV_PROBE_PASSWORD`.
- The Valkey pods read every user's password by `secretKeyRef` (`VALKEY_ACL_PW_<NAME>`). They pass
  only SHA-256 hashes (`#hex`) to the ACL rules, and a pod fails loudly if any password is empty.
- Clients (PR B) mount only their own Secret's key(s) by `secretKeyRef` and send `AUTH <user> <pw>`.
  Rust reads `VALKEY_USERNAME`/`VALKEY_PASSWORD` (penguin-spine) and `VALKEY_KV_USERNAME`/`VALKEY_KV_PASSWORD`.

## Rollout order (no mid-roll breakage)

1. **Secrets.** The provision-secrets pre-install/pre-upgrade hook (weight -8) creates every
   `-valkey-user-*` and probe Secret before any pod rolls. The Valkey StatefulSets' `secretKeyRef`s
   therefore always resolve.
2. **ACL config (PR A, this change).** The Valkey StatefulSets restart with every per-service user,
   the `probe` user, and the **legacy `default` user still enabled**
   (`infrastructure.valkeyAcl.legacyDefaultUser: true`, marked by the
   `valkey.waddles/legacy-default-user` annotation). Clients are unchanged and keep authenticating
   as `default`, so nothing breaks while the pods roll (durable uses OrderedReady; delivery state is
   persisted in AOF).
3. **Clients (PR B).** Each workload switches to its own user (`VALKEY_USERNAME` plus its own
   Secret's password) and rolls. Before flipping, confirm that no client authenticates as `default`
   any more:
   `valkey-cli --user admin --pass "$ADMIN_PW" CLIENT LIST USER default` must return no entries,
   checked on each durable pod and on kv. Then set `legacyDefaultUser: false` and upgrade. `default`
   becomes `reset`, the shared `VALKEY_DURABLE_PASSWORD` in the envFrom'd `waddlebot-secrets` becomes
   inert, and the CI gate stops tolerating the legacy user.

**Rollback:** set `legacyDefaultUser: true` again. Per-service users stay; nothing else changes.

## Gate

`scripts/ci/check-valkey-acl-least-privilege.py` reads a `helm template` stream. It fails on any
of the following:

- a wide grant (`~*`, `allkeys`, a whole namespace such as `waddles:*`, or a leading wildcard)
- a forbidden command (`+@all`, `+@dangerous`, `+@admin`, FLUSHALL, KEYS, ACL, CONFIG other than `|get`)
- a channel grant, a cleartext password or `nopass`
- a cross-service grant that reaches a protected sample key (delivery streams, platform-unban,
  bundlekv, DLQs, transport queues)
- any executor user or executor Valkey credential
- an unmarked legacy `default` user
- zero users examined

It runs on every env through `k8s/helm/waddlebot/tests/test_valkey_acl_render.py`, which also
plants wide grants to prove that the gate fails. Real Valkey tests run in docker. To run the gate
and its tests locally, use `make check-valkey-acl`.

## Known follow-ups (PR B)

- **Erasure gap:** `hub_api/services/erasure_external.py` scans `bundlekv:*:data:*` over
  `VALKEY_URL`/`REDIS_URL` (the durable or cache instance), but bundle kv lives on **valkey-kv**.
  The hub-api kv user above is the grant for the corrected client.
- The hub-api, cronjob and seeder key families must be re-verified when their clients are wired;
  `libs/flask_core/flask_core/stream_pipeline.py` writes `waddles:t:*:c:*:app:*` and has no grant yet.
- `waddles:platform-unban:*` lives on unmerged feature branches (`feature/discord-native-unban-sync`).
  It is already granted here: svc-ingest has write access to `:events`, and svc-action owns the family.
- The legacy cache instance `infrastructure.redis` (`REDIS_PASSWORD`, plus the svc-action kv fallback
  when valkey-kv is disabled) is out of scope here.
