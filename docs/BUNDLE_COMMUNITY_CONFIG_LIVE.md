# Live per-community bundle config

A bundle invoked for community C receives C's latest saved config in `get-context().config_json`
(manifest `community_config_schema` property `default`s overlaid with the saved version).

```
admin PUT config -> bundle_admin_gateway_service.save_config -> bundle_community_config (vN+1)
                                                              -> Valkey SET waddles:bundle_config:v:{tenant_id}:{community_id}:{app_id} = N+1
svc-process / svc-action  handle_context (verified envelope scope only)
  -> hub_client::BundleConfigCache.lookup((tenant_id, community_id, app_id))
       hit & fresh ........ cached JSON        (recheck: GET version key every >=2s; newer -> reload)
       miss / expired ..... gRPC BundleConfigService.GetCommunityConfig  (scope bundle-config:read)
       hub-api down ....... last-known value (WARN) | schema defaults | ERROR log + {}
  seed: ensure_loaded() success -> BundleConfigCache.seed (BatchGetCommunityConfig)
```

* Proto: `proto/waddles/hub/internal/v1/bundle_config.proto` (`api_version: "v1"` required).
* hub-api: `hub_api/grpc_internal/bundle_config_servicer.py`, `services/bundle_community_config_runtime.py`.
  Tenant comes from the verified token (`system` stages name `tenant_id` in the request, any other
  token is pinned to its own); the community must belong to it (else `PERMISSION_DENIED`).
  Caller allow-list: `svc-process`, `svc-action` (`BUNDLE_CONFIG_ALLOWED_SERVICES`).
* Stage: `core/hub_client/src/bundle_config.rs`; TTL safety net 300s, recheck 2s.
* Telemetry: spans `hub_client.bundle_config.*`, `bundle_config.cache.*`; metrics
  `waddles_bundle_config_cache_lookups{result}`, `waddles_bundle_config_load_errors`,
  `waddles_bundle_config_load_seconds`; hub-api `bundle_config_requests_total`,
  `bundle_config_lookup_seconds`. Logs carry numeric ids only, never the config body.
* Helm: `bundle-config:read` granted to the `svc-process` and `svc-action` service identities.
* The hub-api Valkey user must be able to `SET waddles:bundle_config:v:*` and the stage users `GET`
  it; if not, propagation falls back to the TTL (a WARN is logged on both sides).

## Kill switch

`waddles.disable-bundle-community-config` (PostHog, opt-out): unseen/OFF/license-server
unreachable/bypass-domain = live config **on**; ON = svc-process / svc-action do no `config_json`
lookup and add no `config_json` to `get-context` (pre-feature behaviour). Read per call through
`BundleConfigCache::lookup_if_enabled`.

## Never-seen scope while hub-api is down

The stages' active-bundle rows (`bundle_active_set::ActiveBundleRow`) carry the consent
`summary_json`, which does not include `community_config_schema`, so the schema defaults are NOT
available stage-side and `BundleConfigCache::set_defaults` is not wired. That path therefore stays
the loud one: ERROR log + `{}` (never silent). Wiring it needs the defaults carried in the
distribution/active-set payload (a consent-hash-neutral field) -- deferred.

## Valkey

hub-api `SET waddles:bundle_config:v:{tenant}:{community}:{app} <version> EX 7d` and the stages'
`GET` both run on the durable spine Valkey, whose `default` ACL user is `~* &*` with
`+@read +@write` (`templates/infrastructure/valkey-durable.yaml`), so no extra ACL is needed.
Real-Valkey tests: `hub_api/tests/test_bundle_config_publish_valkey.py`,
`core/hub_client/tests/bundle_config_valkey.rs`.
