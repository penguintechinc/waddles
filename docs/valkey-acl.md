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
| `svc-action` | `waddles:t:*:app:*:action`, `waddles:dlq:action`, `waddles:transport:*`, `waddles:ratelimit:*`, `waddles:event-sync:*`, `waddles:usage`, `waddles:platform-unban:*` (consumer), `waddles:canary:latency:result`, read-only `bundle:grants:invalidate`, `waddles:bundle_config:v*` | `bundlekv:*` | base |
| `svc-analytics` | `waddles:analytics`, `waddles:analytics:dlq` | none | `+@connection +@stream` |
| `hub-api` | `waddles:jwt:*`, `waddles:landing:*`, `waddles:bundle_config:*`, `waddles:bundle-admin:requests`, `waddles:bundle-admin:result:*`, `waddles:event-sync:discord:requests`, `waddles:event-sync:discord:result:*`, `waddles:usage`, `bundle:grants:invalidate`, `keys:tenant-dek:invalidate`, `hub_api_rate_limit:*`, `sso:state:*`, `sso:saml:assertion:*`, `oauth:{tenant_discord_install,twitch_install,conn}:state:*`, `dsar:rl:*`, `dsar:once:*`; selector `(~waddles:t:*:src:* ~waddles:t:*:app:* -@all +xgroup\|create +xgroup\|destroy)` | `bundlekv:*:data:*`, `bundlekv:*:count` (erasure) | durable base; kv `+scan +unlink +exists +decrby +del` only |
| `role-sync-inbound` | `waddles:t:*:src:discord:*`, `waddles:dlq:role_sync_inbound` | none | `+@connection +@read +@write +@stream` |
| `landing-stats` | `waddles:landing:*` | none | `+@connection +@read +@write +@keyspace` |
| `core-bundle-seeder` | `bundle:grants:invalidate`, `waddles:bundle_config:*`; selector `(~waddles:t:*:app:* -@all +xgroup\|create +xgroup\|destroy)` | none | base |
| bundle executors | **no user** | **no user** | none |
| `probe` (chart-owned) | none | none | `+ping` only (liveness and readiness) |
| `admin` (chart-owned) | `~*` `+@all` (replication masteruser and break-glass; its own server-only Secret) | none | all |
| `default` | `reset` (off; unauthenticated connections get NOAUTH) | same | none |

**Base allow-lists:**

- durable: `-@all +@connection +@read +@write +@stream +@keyspace +@transaction +@scripting -@dangerous`
- kv: the same, without `@stream`, plus `+time +config|get`, which come after `-@dangerous` (see the comment in `valkey-kv.yaml`)

**Selectors** grant a narrow command set on keys a user does not own. Only consumer-group
provisioning is allowed there: XGROUP CREATE/DESTROY and XINFO GROUPS/STREAM. hub-api and the seeder
can create the groups on delivery streams but can never XADD to or XREADGROUP from them.

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
- Clients mount only their own Secret's key(s) by `secretKeyRef` (helpers `waddlebot.valkeyAcl.durableEnv`
  and `waddlebot.valkeyAcl.kvEnv`). Rust reads `VALKEY_USERNAME`/`VALKEY_PASSWORD` (penguin-spine) and
  `VALKEY_KV_USERNAME`/`VALKEY_KV_PASSWORD`. Python clients connect straight from `VALKEY_URL`, so they
  get the provisioned per-user `VALKEY_DURABLE_URL_TLS_AUTH` (`rediss://<user>:<pw>@...`, recomputed
  every run). The `with_password` helpers do nothing when the URL already carries credentials.

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
3. **Clients (part B).** Each workload switches to its own user and rolls, and
   `legacyDefaultUser` is false, so `default` becomes `reset`. To keep alpha safe mid-roll, deploy
   part A first and let every client roll. Then confirm no client authenticates as `default` any
   more before deploying part B:
   `valkey-cli --user admin --pass "$ADMIN_PW" CLIENT LIST USER default` must return no entries,
   checked on each durable pod and on kv. If A and B land in one upgrade, Valkey restarts with
   `default` off while old client pods are still connected. Those pods reconnect with NOAUTH until
   their own rollout finishes. Delivery state is durable in AOF, so they retry and nothing is lost,
   but alpha sees a short error burst. After B, the shared `VALKEY_DURABLE_PASSWORD` in the
   envFrom'd `waddlebot-secrets` is inert.

**Rollback:** set `legacyDefaultUser: true`. Per-service users stay and clients keep their own
users; only the legacy shared-password `default` user returns.

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

## Tests

- `k8s/helm/waddlebot/tests/test_valkey_acl_render.py`: render, gate (planted wide, cross-service,
  selector and shared-password grants FAIL), client wiring, real Valkey.
- `k8s/helm/waddlebot/tests/test_valkey_acl_service_paths.py`: every service replays the Valkey ops
  its code issues (each cited path:line) against the rendered ACL as ONLY its own user. Zero
  NOPERM is allowed, plus cross-service negatives.
- `core/bundle_host_kv/tests/valkey_chart_acl_real_path.rs`: every `KvHost` op as svc-process's
  own kv user.

## Notes

- **Erasure (fixed, separate PR):** `hub_api/services/erasure_external.py` scanned `bundlekv:*` on
  the durable instance. It now uses only valkey-kv, as hub-api's own kv user.
- `libs/flask_core/flask_core/stream_pipeline.py`: hub-api imports only its key-builder functions
  (`source_stream_key`, `bundle_stream_key`) for consumer-group provisioning. The `StreamPipeline`
  producer class is not instantiated anywhere in hub_api, so it needs no grant.
- `waddles:platform-unban:*` lives on unmerged feature branches. svc-ingest has write access to
  `:events` and svc-action owns the family.
- The legacy cache instance `infrastructure.redis` (`REDIS_PASSWORD`, plus the kv fallback when
  valkey-kv is disabled) has no ACL users. That is out of scope here.
