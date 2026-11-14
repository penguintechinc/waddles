# Live per-community bundle config

A bundle invoked for community C receives C's latest saved config in `get-context().config_json`
(manifest `community_config_schema` property `default`s overlaid with the saved version).

Architecture rule (issue #1306): **hub-api is the control-plane writer; the data plane reads the
database directly** -- from the read-only replica, under per-service column-scoped roles -- and
never calls hub-api on the hot path. svc-process / svc-action keep serving config while hub-api is
down.

```
admin PUT config -> bundle_admin_gateway_service.save_config -> bundle_community_config (vN+1)   [hub-api writes]
                                                              -> Valkey SET waddles:bundle_config:v:{tenant_id}:{community_id}:{app_id} = N+1
svc-process / svc-action  handle_context (verified envelope scope only)
  -> hub_client::BundleConfigCache.lookup((tenant_id, community_id, app_id))
       hit & fresh ........ cached JSON        (recheck: GET version key every >=2s; newer -> reload)
       miss / expired ..... RoutedConfigSource -> DbConfigSource.get   (ONE read-only SELECT, replica)
                            (kill switch ON -> legacy gRPC BundleConfigService.GetCommunityConfig)
       DB down ............ last-known value, served indefinitely (ERROR + counter) | ERROR log + {}
  seed: every bundle load -> enqueue_seed(); scopes queued within 250ms are loaded by ONE batched
        SELECT (unnest of all scopes) -- ~160 bundles at warm-up = 1 query, not 160
```

## Read path (issue #1306)

* Code: `core/bundle_config_db/src/lib.rs` (`DbConfigSource`, `RoutedConfigSource`; its own crate so hub_client stays sea-orm-free,
  `CONFIG_SQL`), wired in svc-process / svc-action via
  `bundle_config_db::connect_and_install`.
* One statement answers like hub-api's `get_runtime_config`: the community must belong to the
  tenant (`community_id = 0` = tenant-wide), the app must be active for the scope (a community
  activation beats the tenant-wide one), and the result is the manifest
  `community_config_schema.properties.*.default` values `||` the latest saved config (a saved key
  wins). An inactive/cross-tenant scope is simply absent (lazy `get` -> `NOT_FOUND`). Ids are
  numeric only, never slugs.
* Role/grants: alembic `0106_bundle_config_db_reads` + `config/postgres/service-roles.yaml`
  (`columns:`) give `waddles_svc_process` and `waddles_svc_action` **SELECT-only, column-scoped**
  access: `bundle_community_config(tenant_id, community_id, app_id, version, config)`,
  `app_active_versions(app_id, tenant_id, community_id, version_id)`, `app_versions(id)`,
  `app_version_uploads(id, app_version_id, manifest_json)` and (svc-process;
  svc-action has table-level) `communities(id, tenant_id)`. Never `updated_by`, `schema_sha256`,
  staging keys, digests; no writes. Downgrade REVOKEs exactly these (no column ACL left behind,
  so a later `DROP ROLE` works).
* Connection env: `DATABASE_REPLICA_URL` (read-only replica DSN); when unset the primary
  `DATABASE_URL` is used and a WARN is logged. The chart renders the replica DSN from
  `infrastructure.postgresql.readReplicas.host` (empty on alpha: no replica yet). The DSN carries
  the role credential and is never logged. The pool is lazy -- a database that is down at startup
  never disables reads for the process lifetime.
* **Replica lag:** hub-api bumps the Valkey version key right after the commit, possibly before the
  replica has it. The stage then reloads, gets the older version, stores it with that version, and
  the next recheck (>=2s) sees the key is still newer and reloads again -- self-healing, no extra
  config.
* **DB failure behaviour:** ERROR log (error kind + DSN-redacted message), no panic; the cache
  serves the last-known value for as long as the database is unavailable (no max-age cutoff and no
  staleness alerting by design -- fail-loud for stale config lives in hub-api, the writer, and is
  tracked separately); a never-seen scope gets the loud `{}` (ERROR). Nothing is silently
  defaulted to different semantics.
* Telemetry: spans `bundle_config_db.read`, `bundle_config.cache.*`; metrics
  `waddles_bundle_config_db_reads{op,outcome}`, `waddles_bundle_config_db_read_seconds{op}`,
  `waddles_bundle_config_read_path{path=db|hub_api}`, plus the existing
  `waddles_bundle_config_cache_lookups{result}`, `waddles_bundle_config_load_errors`,
  `waddles_bundle_config_load_seconds`. Logs carry numeric ids only, never the config body.

## Legacy hub-api gRPC path (behind the kill switch)

* Proto: `proto/waddles/hub/internal/v1/bundle_config.proto` (`api_version: "v1"` required).
* hub-api: `hub_api/grpc_internal/bundle_config_servicer.py`, `services/bundle_community_config_runtime.py`.
  Tenant comes from the verified token (`system` stages name `tenant_id` in the request, any other
  token is pinned to its own); the community must belong to it (else `PERMISSION_DENIED`).
  Caller allow-list: `svc-process`, `svc-action` (`BUNDLE_CONFIG_ALLOWED_SERVICES`).
* Client: `BundleConfigClient` in `core/hub_client/src/bundle_config.rs`;
  `bundle-config:read` is granted to the `svc-process` and `svc-action` service identities.
* Cache timing (both paths): TTL safety net 300s, recheck 2s.
* The hub-api Valkey user must be able to `SET waddles:bundle_config:v:*` and the stage users `GET`
  it; if not, propagation falls back to the TTL (a WARN is logged on both sides).

## Kill switches (both PostHog, opt-out: unseen/OFF/license-server-unreachable/bypass-domain = on)

* `waddles.disable-bundle-community-config`: ON = svc-process / svc-action do no `config_json`
  lookup and add no `config_json` to `get-context` (pre-feature behaviour). Read per call through
  `BundleConfigCache::lookup_if_enabled`.
* `waddles.disable-db-bundle-config-reads`: ON = serve through the legacy hub-api gRPC path instead
  of the direct database read (so alpha keeps working without the PostHog key: unseen = database
  path). Read per load by `RoutedConfigSource`; with the flag ON and hub-api unconfigured, loads
  fail loudly (stale cache served) rather than silently reading the database.

## Never-seen scope while the source is down

The stages' active-bundle rows (`bundle_active_set::ActiveBundleRow`) carry the consent
`summary_json`, which does not include `community_config_schema`, so the schema defaults are NOT
available stage-side and `BundleConfigCache::set_defaults` is not wired. That path therefore stays
the loud one: ERROR log + `{}` (never silent). Wiring it needs the defaults carried in the
distribution/active-set payload (a consent-hash-neutral field) -- deferred.

## Valkey

hub-api `SET waddles:bundle_config:v:{tenant}:{community}:{app} <version> EX 7d` and the stages'
`GET` both run on the durable spine Valkey, whose `default` ACL user is `~* &*` with
`+@read +@write` (`templates/infrastructure/valkey-durable.yaml`), so no extra ACL is needed.

## Tests

* Real Postgres: `core/bundle_config_db/tests/bundle_config_db_pg.rs` (`TEST_DATABASE_URL`, fails loudly
  without it; CI starts the container via `rust-core-libs.yml` `postgres: true` for bundle_config_db):
  batched warm-up = 1 query, lazy-load, invalidation refresh, read-only role refused writes and
  non-granted columns, DB-down serves stale.
* Real migration chain: `alembic/tests/test_0106_bundle_config_db_reads.py` (the Rust statement is
  extracted from the source and run as each real role; downgrade/upgrade; `DROP ROLE`).
* Chart: `k8s/helm/waddlebot/tests/test_bundle_config_db_replica_env_render.py`.
* Real Valkey: `hub_api/tests/test_bundle_config_publish_valkey.py`,
  `core/hub_client/tests/bundle_config_valkey.rs`.
