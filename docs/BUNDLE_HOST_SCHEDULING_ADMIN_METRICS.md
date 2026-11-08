# Bundle host seams 2: scheduled reconcile, admin transport, bundle metrics

Generic host seams only -- no platform or consumer logic lives here.

## 1. Scheduled reconcile (APP and SERVICE bundles)

A bundle declares `schedule: {interval_s, jitter}` in `bundle.yaml` (interval 30s..24h, jitter a
fraction 0..0.5, default 0.2; unknown keys refused -- `hub_api/services/bundle_manifest_v2.py`
`parse_schedule`). The host delivers one `hub` / `bundle.reconcile` process event per
`(tenant, community, app)` on that cadence; the payload is `{app_id, community_id, interval_s,
scheduled_at}` -- ids and cadence only. The bundle must consume `hub` / `bundle.reconcile`.

```
svc-ingest replica A --\                    +--> hub source stream (per app) --> process stage --> bundle
                        >-- Valkey lease --+
svc-ingest replica B --/   SET NX PX 0.9*interval
```

* `core/svc_ingest/src/connector/bundle_schedule.rs`: `ScheduleRunner` (lease + single-flight +
  backoff), `SpineReconcileSink` (schema-v3 envelope via `publish_event_scoped`, source id =
  `app_id_slug(app_id)`), `run_manager` (one loop per target, diffed against the listing),
  `HubScheduleSource` (`GET /api/v1/internal/bundle-schedules`, machine JWT scope
  `bundle-schedules:read`).
* **hub-api side** (`hub_api/blueprints/v1/bundle_schedules.py`, `hub_api/services/bundle_schedule_service.py`):
  `GET /api/v1/internal/bundle-schedules[?tenant_id=&community_id=&app_id=]` -> `{"targets":[{tenant_id,
  community_id, tenant_slug, community_name, app_id, interval_s, jitter}]}` (numeric ids, the data plane
  never keys on slugs; `community_id` 0 = tenant-wide, `community_name` null). Machine JWT scope
  `bundle-schedules:read` from the `system` tenant (chart: svc-ingest identity only) plus a caller-name
  allow-list (`BUNDLE_SCHEDULES_ALLOWED_SERVICES`, default `svc-ingest`). Lists only active, available,
  globally-installed bundles whose manifest declares `schedule`.
* **Binding contract**: a manifest with `schedule` must consume `hub` / `bundle.reconcile` with `source_id`
  absent or `app_id_slug(app_id)` (`schedule_requires_hub_consume` otherwise; Python mirror of the Rust
  `app_id_slug`). Activation (`app_source_binding_service.sync_bindings`) writes the
  `(hub, app_id_slug)` `app_source_bindings` row and provisions the consumer group. The listing re-checks
  manifest + binding per row; a failing candidate is excluded, logged at ERROR and counted on
  `bundle_schedules_excluded_total{reason}`, never returned.
* Exactly one replica publishes per interval; a lease-backend error never publishes (fail closed);
  a failed publish extends the lease to `min(interval * 2^fails, 1h)` so no replica retries early.
* Telemetry: span `bundle_reconcile`, series `svc_ingest_connector_reconcile_{total,seconds}`
  (`platform="bundle"`, outcomes `ok|failed|skipped_in_flight|skipped_lease`).
* Boot: `BUNDLE_SCHEDULES_ENABLED=true` (env baseline, default OFF). ON with a missing prerequisite
  fails the process.
* `reconcile_hook.rs` (connector exports) now allows intervals from 30s and an optional jitter.

## 2. Bundle-admin transport (hub-api gateway host side)

Contract: `docs/BUNDLE_ADMIN_GATEWAY.md` (PR #1123). `core/svc_process/src/bundle_admin.rs`:

1. `BRPOP waddles:bundle-admin:requests`; verify `sig` (`kid:hex`, HMAC-SHA256, label
   `waddles.bundle-admin.v1`, length-prefixed fields in `sign_call` order) against
   `BUNDLE_ADMIN_REQUEST_KEYS` (rotation = several kids). Unverifiable: dropped, **no reply**.
2. Reject expired / implausibly far-future `exp_ms`; honour each `request_id` once (`SET NX`).
3. Signed `actor_scope` -> cumulative community scopes (`community:moderate|admin|owner`); this is
   the verified actor for `required_scope` tools. Tenant/community come from the signed request.
4. `bundle_tool_call::authorize_admin_call`: another bundle's tool = the full P1 matrix + the
   caller's `requested_tools` + the `tool.call` gate with `caller_app_id`; the caller's OWN tool
   (a service bundle's own `admin_tools`) needs no grant but must be active, exported and (if a
   write) keyed -- the `request_id` is the idempotency key.
5. Reply `LPUSH waddles:bundle-admin:result:<request_id>` (`EXPIRE 120`):
   `{"ok":true,"result":{..}}` | `{"ok":false,"code":"..."[,"tool_error":true]}`.

Bundles see the actor id and its scopes (`__ctx`), never a token. Key ring: Helm secret
`<fullname>-bundle-admin-request` (`bundleAdminRequest.existingSecret` for ESO/Sealed Secrets),
mounted only into hub-api (signer) and svc-process-rust (verifier).

## 3. Bundle metrics (WIT `waddle:bundle/metrics`)

`counter-add`, `histogram-record`, `gauge-set` on `stage-next`, `stage-v2`, `stage-tools`; mapped by
the executor onto OTel instruments `waddles_bundle_<name>` with a host-stamped `app_id`. Host-side
bounds (`core/bundle_executor/src/host/stage_next_metrics.rs`): names `^[a-z][a-z0-9_]{0,47}$`,
32 names per bundle, one kind per name, <= 4 labels, values `^[A-Za-z0-9_.-]{1,32}$`, <= 16 distinct
values per label (then `other`), counter delta <= 1e6, histogram >= 0, finite values. Rejections are
typed WIT errors and counted on `waddles_executor_bundle_metric_rejects_total{reason}`.
