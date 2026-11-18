# Community-entitlement pool limits

Module: `flask_core.license_pool` (`libs/flask_core/flask_core/license_pool.py`).
Answers "how many Premium community entitlements may tenant T allocate" (one pool per tenant).

## Interface
- `PoolProvider.get_pool(tenant_id, tier_code) -> PoolInfo` (`tier_code` must be 2 = premium, the only pool; anything else raises `ValueError`).
- `PoolInfo.limit`: `None` = tier not licensed, `-1` = unlimited, else the cap. `stale` = served from last-known during an outage. `source` = `license_server | offline | operator_set`.
- `PoolStore` (`get`/`put`) persists `PoolRow(limit_count, observed_at, source_code)`; source codes 1 license_server, 2 offline, 3 operator_set.
- `PoolUnavailable` is raised whenever the size cannot be known. Callers must treat it as a failure (never as 0, free or unlimited).

## Behavior
| Case | Result |
|------|--------|
| Bypass domain (`*.penguincloud.io`, `*.penguintech.cloud`, product `.app`) | Reads only the `operator_set` row set by the global admin; no row = `limit=None`. No license-server call, ever. Domain-based only, never env/flag. |
| Bypass domain AND the DEFAULT tenant (hub-api layer, `services/pool_provider.ServicePoolProvider`) | Unlimited Premium computed in code: `limit=-1`, `stale=False`, `source=bypass_default`. No DB row, no license-server call. The default tenant is identified by the numeric id of the configured default-tenant slug row (compared by id). Other tenants keep the operator-set behaviour above; non-bypass hosts are unchanged. |
| Non-bypass, cached row younger than 5 min | Served from the store. |
| Non-bypass, stale cache | POST `/api/v2/entitlements` (strict TLS, one shared `httpx` client with a bounded pool, 10s per-request timeout, **20s total deadline**, 3 attempts with exponential backoff + jitter, **only** on transport errors/timeouts/5xx/429). Any other status is a definitive answer and is not retried. The premium pool is stored (source 1). Concurrent callers for a tenant coalesce into one request. |
| Refresh fails **transiently** (network, timeout, 5xx, 429, key-store DB error) | ERROR log (`license_pool.refresh_failed`, type + bounded reason only). Last-known license-server row younger than 72h -> `stale=True`; otherwise `PoolUnavailable`. |
| Refresh fails **definitively** (no tenant key, 401/403/404, invalid JSON, response without a `team` axis) | ERROR log; **no grace**: the cached license-server rows for the tenant are evicted and `PoolUnavailable` is raised. The 72h grace never outlives a revoked/removed key or an explicit refusal. |
| No tenant key (`license_key_resolver` returns None) | `PoolUnavailable('no_tenant_license')` |

`penguin_licensing` is intentionally not used: it returns the free tier on any error, which would hide an outage.
Operator rows are ignored on non-bypass deployments. The license key is only sent in the request body and is never logged.

## ASSUMED contract (not yet implemented by the license server)
`POST /api/v2/entitlements` body `{"license_key": "...", "product": "waddles"}` returns, in addition to today's fields:
```json
{"team": {"premium": {"limit": 5}}}
```
`team.premium.limit`: `-1` = unlimited; a missing `premium` entry or `limit: null` = not licensed. The old `professional` / `enterprise` keys are ignored. Constants: `ENTITLEMENTS_PATH`, `TEAM_AXIS_KEY`, `TIER_RESPONSE_KEYS`.
Verify against alpha (use a real key from a secret store, not the shell history):
```
curl -sS --fail-with-body -X POST "$LICENSE_SERVER/api/v2/entitlements" \
  -H 'Content-Type: application/json' --data @<(jq -n --arg k "$LICENSE_KEY" '{license_key:$k,product:"waddles"}') | jq '.team'
```
Until `.team` is non-null, every non-bypass pool lookup fails loudly with `PoolUnavailable`.

## Offline entitlements
No offline entitlement loader exists in this repo and the offline schema has no team section; offline (source 2) is not supported yet. `source_code=2` rows are read if present.

## Telemetry
`waddles_license_pool_refresh_total{outcome}` and `waddles_license_pool_refresh_duration_seconds{outcome}`; outcomes: `success`, `error`, `team_axis_absent`, `no_tenant_license`. No tenant labels.

## Entitlement hook
The generic `CommunityTierSource` hook was removed (communities are Free/Premium only; the tenant tier never uplifts a community). `EntitlementClient.effective_tier` is the tenant tier only.

## Key lifecycle and eviction

Setting, replacing or clearing a tenant license key (`tenant_license_service`) deletes that tenant's `license_server`-sourced pool rows in the same transaction (`operator_set` rows are never touched), so a cached pool cannot survive the key that produced it.

## Base URL, Host and bypass hardening

- `LICENSE_SERVER_URL` is validated at provider construction: `https` only, no userinfo/query/fragment. Plain `http` is accepted only for a loopback host AND only with `LICENSE_SERVER_ALLOW_LOOPBACK_HTTP=1` (dev); a remote http URL is always rejected.
- Bypass stays domain-based and hardcoded. The host comes from `current_request_host()` (`resolve_trusted_host`: the raw `Host` unless `TRUSTED_FORWARDED_HOST` is set and the peer is inside `TRUSTED_PROXY_CIDRS`), is normalized (lowercase, one port and trailing dot stripped) and any userinfo/path/whitespace/comma/IPv6/multi-colon form is treated as "not a bypass domain".
- Defense in depth: the operator `PUT .../license-pools/2` (any other tier is 400) is refused (409 `POOL_OPERATOR_SET_NOT_ALLOWED`) when `LICENSE_SERVER_URL` is explicitly set AND the tenant has a license key configured, regardless of the host. It also needs the `tenants:admin` scope. The body `limit` is required (`null` = not licensed, `-1` = unlimited, else 0..2147483647).
- A bypass-domain tenant with no `operator_set` row reads as `limit=None` ("not licensed", surfaced as 402 `POOL_EXHAUSTED`), not as an outage (503): the operator simply has not licensed the Premium pool.
