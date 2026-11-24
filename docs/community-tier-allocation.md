# Community tier allocation

A community is **Free** (code 1) or **Premium** (code 2). There is exactly ONE pool per tenant (Premium).

| Rule | Behaviour |
|------|-----------|
| Free | No allocation row, consumes no pool slot, never blocked. |
| Premium | Allocated by a tenant admin from the tenant's single team-axis pool (`tenant_license_pools`, tier 2). |
| Effective tier | `premium` iff the community has an active allocation (any source: tenant admin, operator override, backfill), else `free`; any error fails closed to Free. The tenant-level license tier does NOT uplift a community: the pool is the entitlement. |
| Overage | A NEW Premium allocation beyond the pool is blocked (`PoolExhausted`, 402 + upgrade path). An existing allocation is NEVER stripped; a smaller pool or tenant downgrade only blocks new allocations. |
| Operator override | Super admins may set Free or Premium. Never blocked by pool EXHAUSTION, still counted (reported as `overage`), audited as `license.community_tier_overridden`. The tenant must still be licensed (see Free tenants). |
| Free tenants | Only Professional and Enterprise tenants can have Premium communities. A tenant with NO license key, or whose pool resolves to "not licensed" (`limit` null), is a Free tenant: allocation by the tenant admin AND by the operator override is refused with `402 TENANT_TIER_REQUIRED` (`TenantNotLicensed`, audited as `license.community_tier_allocation_blocked`). On bypass domains the operator makes a tenant eligible by setting its pool first (`PUT /superadmin/tenants/<id>/license-pools/2`); the default tenant there stays unlimited. Existing Premium communities are grandfathered (never stripped, effective tier unchanged); releasing to Free always works. |
| Staleness | Reporting only. Automatic slot reclamation is NOT active (see Staleness below): every unreleased allocation counts against the pool. |
| Pool unknown | `PoolUnavailable` (503) when a NEW slot would be consumed, for the tenant admin AND the operator override (a genuine failure: license-server outage beyond grace, no team axis, unreadable stored key, store error). Never a silent zero, never confused with "no license key" (that is a Free tenant, 402). An idempotent re-PUT of the tier the community already holds succeeds without the pool (no slot consumed); releases (Free) never need it. |

## Staleness

The licensing rule "30 days with no contact frees the slot" needs a real activity signal, and hub-api has none: community activity happens in the data plane and `touch_active` has no production caller. Counting only recently-active rows would let a tenant at its limit allocate a whole new pool every 30 days (pool cap defeat), so **automatic slot reclamation is NOT active**:

- Every allocation with `released_at IS NULL` counts toward `used` for new-allocation checks, `pool_usage`, tier-change checks and the utilization gauge, regardless of `last_active_at`.
- `stale` (per allocation) and `stale_excluded` (per tier, legacy field name) are informational: `last_active_at` older than 30 days (`STALE_AFTER`). A stale allocation keeps its tier and its slot.
- A slot is freed only when the tenant admin or an operator releases the allocation (never stripped automatically).
- The rule will be enabled by wiring a real activity signal into `touch_active` (deferred issue); there is no env/flag toggle for the old behaviour.

## Tables (alembic `0126_community_tier_allocations`)

- `community_tier_allocations`: ledger, one active row per community (partial unique on `community_id WHERE released_at IS NULL`). Codes: `source_code` 1 tenant_admin / 2 operator_override / 3 backfill; `release_reason_code` 1 dealloc / 2 tier_change / 3 community_deleted.
- `tenant_license_pools`: last observed pool per `(tenant, axis=3 team, tier=2)` (CHECK `tier_code = 2`). `limit_count` NULL = not licensed, -1 = unlimited. `source_code` 1 license_server / 2 offline / 3 operator_set. The bypass-domain default tenant's unlimited pool (`source` `bypass_default`) is computed in code and never stored.
- `communities.license_tier` / `is_premium` are denormalized copies, written in the same transaction as the ledger row.
- Backfill: every premium community (`is_premium`, or `license_tier` enterprise/professional/pro/premium) without an active allocation gets ONE Premium row (tier 2, source 3). Downgrade REFUSES while either table holds rows unless `ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE=1`; it leaves the denormalized columns as they are. Both migrations set `lock_timeout` and use plain `CREATE TABLE`.

## Service (`hub_api/services/community_tier_service.py`)

All functions take `(async_dal, dal, ...)` and numeric ids. The guarded section is one executor job under
`pg_advisory_xact_lock(hashtext('team_pool:<tenant_id>:<tier>'))` (same pattern as `free_tier_caps`); the stored pool row is re-read INSIDE the lock (a pool shrink committed after the async provider lookup is honored) and every executor job ends in commit/rollback (no idle-in-transaction). Pool usage is counted in SQL, never by loading the ledger, so racing
allocators for the last slot serialize and exactly one wins.

- `allocate(async_dal, dal, *, tenant_id, community_id, tier, actor, source, pool, reason=None, audit=None)`
- `resolve_effective_tier(async_dal, dal, tenant_id, community_id)`
- `pool_usage(async_dal, dal, tenant_id, *, pool)` one entry (`TIER_CODE_PREMIUM`): used / limit / available / overage / stale_excluded (stale rows are included in `used`)
- `touch_active(async_dal, dal, community_id)` rate-limited (5 min in-process, 1 h in DB)
- Constants: `TIER_CODE_FREE = 1`, `TIER_CODE_PREMIUM = 2`, `TIER_NAMES`, `POOLED_TIER_CODES = (2,)`.
- `PoolInfo`, `PoolProvider` (Protocol), `PoolStore`, `StorePoolProvider`, `PoolExhausted`, `PoolUnavailable`, `TenantNotLicensed`

## Telemetry and audit

Metrics: `waddles_community_tier_allocations_total{tier,result,source}`, `waddles_community_tier_allocation_duration_seconds{result}`,
`waddles_team_pool_utilization{tier}` (peak ratio across tenants, no tenant label); span `community_tier.allocate`.
Audit (category `license`): `license.community_tier_allocated`, `_released`, `_overridden`, `_allocation_blocked`
(outcome `denied`). Audit events are written after the DB commit; a failed audit write is retried once, then logged at ERROR (`community_tier_audit_write_failed`), counted (`waddles_audit_write_failed_total{area}`) and propagated -- never reported as clean success.

## Gates

Every gate reads the single resolver (`resolve_effective_tier`), keyed by numeric tenant and community ids; the tenant id
comes from the community row, never a caller-supplied slug. A resolver error is Free for the gate (never a paid tier, never
a 500) and logs `community_tier_resolve_failed error_type=...` at ERROR. The denormalized `communities.license_tier` /
`is_premium` columns are display copies and are not read by any gate in these files.

| Gate | File | Requires effective tier |
|------|------|-------------------------|
| Workflows (create / publish / execute) | `hub_api/services/workflow_service.py` | Premium |
| Premium local-metered AI | `hub_api/services/ai_routing/router.py` | Premium |
| BYOK AI | `hub_api/services/ai_routing/router.py` | Premium |
| Media (Gemini) BYOK + tier RPC | `hub_api/services/media_byok_service.py` (via `grpc_internal/dataplane_servicers.py`) | Premium |

## API

Flag `waddles.community-tier-allocation` (default OFF; Helm `config.communityTierAllocationEnabled` ->
`FLAG_WADDLES_COMMUNITY_TIER_ALLOCATION`) gates ONLY the tenant-admin API below (OFF = uniform 404). The superadmin
override is never flag-gated. Enum fields are int codes; only community `public_id` UUIDs cross the boundary.
`pool_provider.get_pool_provider(app)` builds the app-wide license-server-backed provider (`LICENSE_SERVER_URL`,
tenant key from `tenant_license_service`, bypass domains by request host only); `app.config["POOL_PROVIDER"]` overrides it.

### Tenant admin (`/api/v1/tenant/<slug>/...`)

Scopes: `tenant:admin` AND `tenant.community_tier:read` (GETs) / `tenant.community_tier:allocate` (PUT), both in the tenant
`admin` bundle. The URL slug must equal the JWT tenant (else 403).

| Route | Purpose |
|-------|---------|
| `GET license/pool` | `{tiers:[{tier,limit,used,available,overage}], observedAt, stale, source}`; `tiers` has exactly ONE element (tier 2); `limit` null = not licensed (a Free tenant: 200, never 503), -1 = unlimited |
| `GET communities/tier-allocations?tier=&source=&limit=&offset=` | `{allocations:[{communityId,tier,source,allocatedAt,stale}], meta:{total,limit,offset}}`; active Premium rows only; `tier` accepts 2 only; invalid filter = 400 |
| `PUT communities/<community_uuid>/tier` body `{tier}` | 1 = Free (releases), 2 = Premium; anything else = 400 `INVALID_TIER` -> `{communityId,tier,previousTier,changed,used,limit,overage}` |

Errors: `402 TENANT_TIER_REQUIRED` ("Premium communities require a Professional or Enterprise license"; `error.details`:
`requiredTenantTier: "professional"`, `upgrade: true`; same envelope from the superadmin override), `402 TEAM_POOL_EXHAUSTED` (`error.details`: `requiredTier`, `limit`, `used`, `upgrade: true` -- no pricing/upgrade URL is
configured in hub-api yet, so none is sent), `503 TEAM_POOL_UNAVAILABLE` (pool unknown, fail loud), `404` unknown / other-tenant /
malformed UUID or flag OFF, `400` invalid `tier` or query value.

### Tier hint in `GET /api/v1/auth/me`

Each `communities[]` item carries `licenseTier` (1 Free, 2 Premium): the community's own active
allocation (`released_at IS NULL`), read for all the user's communities in ONE batched query; no active allocation = 1.
It is a display/UX hint for the webui (e.g. unlocking premium community admin pages): the backend gates (above) stay authoritative. A lookup error logs `community_tier_lookup_failed
error_type=...` at ERROR and reports 1 for every community; `/auth/me` never fails because of it.

### Superadmin override (`PUT /api/v1/superadmin/communities/<id>`)

- `licenseTier` (1/2) requires `reasonCode` (one of `support_request`, `sales_agreement`, `trial_extension`,
  `billing_correction`, `partner_program`, `data_correction`, `other`: the audit reason is a short token, never free text) and the
  global-admin scope `users:admin` on top of `communities:admin`. It allocates with `source=operator_override`: never blocked,
  counted as `overage`, audited `license.community_tier_overridden`. The caller's validated tenant must also be the
  reserved `system` tenant (403 otherwise, defence in depth; the same guard covers the `license-pools` operator routes).
  If the allocation commits but its audit write fails, the call returns 500 `TIER_OVERRIDE_FAILED` (ERROR log +
  `waddles.superadmin.tier_override_failed` counter), never a silent success.
- `isPremium` in a request is a 400 (derived); responses keep `isPremium` and add `licenseTier`.
- Absent key = unchanged; explicit `null` clears `seatLimit` (unlimited) and `description`; `displayName`/`isActive`/`isPublic`
  reject null (400).
