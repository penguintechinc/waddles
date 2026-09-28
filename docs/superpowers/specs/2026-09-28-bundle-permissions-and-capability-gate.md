# Bundle Permissions & Capability Gate — Design

**Status:** Proposed design (no code in this doc) — the umbrella other capability specs plug into.
**Date:** 2026-09-28
**Scope:** `wit/waddle-bundle/stage.wit`, `core/svc_process`, `core/svc_action`, new `core/bundle_capability_gate` crate, `hub_api/services/bundle_manifest_v2.py`, `hub_api/services/bundle_approval_service.py`, `hub_api/services/permission_summary_service.py`, `hub_api/services/marketplace_lifecycle_service.py`, new hub-webui consent screens, `core/reputation_module`.
**Driver:** Justin's requirement — bundles get their own tables/object storage and the ability to move community/tenant reputation, but only after an Android-style permission request reviewed at each of the 3 lifecycle tiers, with one standard gate every host import calls to check/allow/validate that the call is app-bundle-scoped or reputation-scoped and that the bundle actually holds the grant.
**Builds on (read, reconciled, not re-litigated):**
- `wit/waddle-bundle/stage.wit` (v1.0, live) — 8 `CapabilityKind` variants (`Context, Http, Kv, Db, Relay, Flags, Log, Clock`), each "always granted" or gated on a manifest shape signal (e.g. `db` on non-empty `data.tables`). **`db`/`kv` are hardcoded `denied` today, unconditionally** (`core/svc_process/src/capabilities.rs:221-228`, `core/svc_action/src/capabilities.rs:582-589`) — no enforcement gate exists to wire them into.
- `docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md` (APPROVED-WITH-CONDITIONS) — adds manifest `capabilities: [scheduled, moderation, db-batch]`, per-component `Linker` registering only a component's declared imports (§5), and `moderation`'s action-stage-only + relay-authz-gated + rate-limit-before-call + destructive-ops-bypass-cache pattern (§2, §7). **This spec's permission catalog subsumes `capabilities:`** — see §2.4.
- `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-schemas.md` (PR #415, `docs/bundle-db-capability-design`) — wires `db.execute`/`execute-batch`, a separate `bundle-data` Postgres database, per-app schema+role+RLS, a manifest DSL→DDL schema compiler, quotas, migrations, uninstall deletion, backups. **This spec's `storage.tables` permission is the grant that authorizes exactly what PR #415 provisions** — not a competing design.
- `docs/superpowers/specs/2026-09-28-superpenguin-fish-game-port.md` (PR #414) — a real bundle needing `storage.kv`+`storage.tables`; explicitly flags that no capability exists for a bundle to move platform points, recommending a future capability "mirroring how `moderation` was added — new WIT interface, new `CapabilityKind` variant, action-stage-only, rate-limited, audited." **This spec's `reputation.*` follows that exact template.**
- `feature/bundle-kv-capability` — fetched; **zero commits ahead of `release/v3.0.X` today** (tip is an unrelated merge). No implementation to reconcile yet; §9 hands it concrete tasks against this design.
- Existing hub-api consent machinery, already live: `hub_api/services/permission_summary_service.py` (`build_permission_summary`/`permission_hash`/`canonical_json`) and `bundle_approval_service.classify_diff` (`initial|widened|narrowed|unchanged`) already do install-time summarization and upgrade diffing — **today only over the 8 derived `CapabilityKind` names, egress hosts, tables, and `routes_to`.** This spec extends that same machinery to the full permission catalog (§2) rather than inventing a parallel one.
- The real 3-tier scope gates, confirmed in code: `platform:admin` on `app_catalog` install/uninstall + vendor-version approve/deny (`hub_api/blueprints/v1/marketplace_lifecycle.py:370,387`, `bundle_approvals.py:81,99,140`); `tenant:admin` on `app_tenant_availability` make/unavailable (`marketplace_lifecycle.py:440,464`); community-scoped activate/deactivate on `app_activations` (`marketplace_lifecycle.py:522,556`). Invariant: `activated <= available <= installed`.
- `hub_api/cli/seed_core_bundles.py` — the existing system-actor pattern (`approved_by=None`, `approval_source="system:core-seeder"`, hard-guarded to `waddles.core.*`) this spec reuses verbatim for core-bundle permission pre-grant (§3.6).
- Existing reputation system: `core/reputation_module` (gRPC `ReputationService.RecordEvent`/`GetScore`, `reputation_service.py::adjust()` the sole write path, score band 300-850 default 600, `REPUTATION_AUTO_BAN_THRESHOLD=450`), `hub_api/services/community_reputation_service.py` (read-only, two scores: `community_members.reputation` and `reputation_global.score`), `core/svc_process/bundles/community_reputation_process.py` (the legacy native `!rep` bundle, untouched by this design). **No capability today lets a WASM bundle read or write either score.**

---

## 1. Permission catalog

Android-style: every permission has a stable id, a risk level, and a default quota. `normal` permissions are shown but not blocked on; `dangerous` permissions require explicit reviewer action at every tier that sees them for the first time (§3).

| id | Risk | Default quota | `CapabilityKind` | Notes |
|---|---|---|---|---|
| `storage.kv` | normal | 64 KiB/value, 10k keys/app, `KV_MAX_TTL_S` | `Kv` | Bundle's own `...:state` hash — always-available shape, but the *grant* is now explicit, not implicit ("always granted" retired, see §2.4) |
| `storage.tables` | normal | 100k rows / 50 MB per (tenant, app) — PR #415 §6 | `Db` | Requires `data.schema` (PR #415's DSL); table names still validated by existing `_TABLE_RE`/`_RESERVED_TABLES` |
| `storage.objects` | normal | 1k objects / 500 MB per (tenant, app) | `Objects` *(new)* | §6 |
| `net.http:<host>` | dangerous | 10 rps, 1 MB response, no private hosts | `Http` | One permission id per allowlisted host, e.g. `net.http:api.example.com`; matches existing per-manifest `egress` rule shape, now catalog-typed |
| `chat.send:<platform>` | normal | existing `relay` rate limits (`UsageBatcher`) | `Relay` | `<platform>` ∈ compiled-in providers (`twitch`, `discord`, ...); action-stage only, unchanged from today |
| `moderation.<platform>` | dangerous | existing relay-authz + `UsageBatcher` (wit-v1.1 §2) | `Moderation` *(v1.1)* | Subsumes v1.1's bare `moderation` capability flag — same enforcement, now catalog-typed per platform |
| `reputation.read` | normal | unlimited reads | `Reputation` *(new)* | Own-scope + community/tenant leaderboard reads only — never cross-tenant |
| `reputation.community.write` | dangerous | ±5/user/day, ±50/community/day aggregate | `Reputation` *(new)* | §7 |
| `reputation.tenant.write` | dangerous | ±5/user/day, ±200/tenant/day aggregate | `Reputation` *(new)* | §7; strictly a superset grant of `reputation.community.write`'s bound, never looser per-call |
| `flags.read` | normal | unlimited | `Flags` | Was "always granted" — now explicit, still auto-approved (§3.5) |
| `platform.scheduled` | normal | 60s floor interval, existing tenant quota (wit-v1.1 §1) | *(none — hub-api CRUD only)* | Subsumes v1.1's bare `scheduled` capability flag |
| `platform.context` / `platform.clock` / `platform.log` | normal | n/a | `Context`/`Clock`/`Log` | Always-granted, zero-config; listed for completeness of the enforcement gate's exhaustive match, never shown on a consent screen (§8) |

**Risk-level rule:** `dangerous` = crosses a trust boundary this platform otherwise protects by construction (talks to the open internet, mutates another user's platform-visible reputation/moderation state, or bundle-approval-scoped destructive schema changes per PR #415 §7). Everything AppScoped and contained to the bundle's own data is `normal`.

---

## 2. Manifest declaration

### 2.1 Format

New manifest v2 top-level block, replacing the dead free-form `permissions: []` (§2.4) and `data.tables`'s implicit `db` grant:

```yaml
permissions:
  - id: storage.tables
    justification: "Stores each cast's fish, weight, and gold for the player's inventory."
    params:
      schema: data/schema.yaml          # PR #415 DSL file, relative to bundle root
  - id: storage.objects
    justification: "Stores the seasonal leaderboard banner image players can upload."
    params:
      max_object_bytes: 2097152
      max_total_bytes: 52428800
      content_types: ["image/png", "image/jpeg"]
  - id: net.http:api.weatherapi.example.com
    justification: "Looks up real-world weather to theme the fishing spot."
    params:
      methods: ["GET"]
  - id: chat.send:discord
    justification: "Announces rare catches in the channel."
  - id: reputation.community.write
    justification: "Rewards consistent players with +1 community reputation per legendary catch, capped daily."
    params:
      delta_min: -1
      delta_max: 1
      reason_codes: ["fishing.legendary_catch"]
```

### 2.2 Per-permission `params`

| Permission | Required `params` | Validated by |
|---|---|---|
| `storage.tables` | `schema` (path to PR #415 DSL doc) | hub-api schema compiler (PR #415 §4) |
| `storage.objects` | `max_object_bytes`, `max_total_bytes`, `content_types[]` | New `_ALLOWED_CONTENT_TYPES` allowlist, both bytes fields ≤ catalog default ceiling (§1) |
| `net.http:<host>` | `methods[]` | Existing `_ALLOWED_METHODS`/`_EGRESS_HOST_RE` (unchanged) |
| `chat.send:<platform>` | none | Existing `RELAY_PROVIDERS` allowlist |
| `reputation.community.write` / `reputation.tenant.write` | `delta_min`, `delta_max` (integers, `|delta| <= 5`), `reason_codes[]` (non-empty) | New validator: bounds inside the catalog ceiling (§1), every `reason_codes` entry `[a-z][a-z0-9_.]*` |
| `reputation.read` | none | — |
| `flags.read` | none | — |

`justification` is mandatory on every entry, 1-280 chars, plain text (no markdown/HTML) — the string a human reviewer and the community-admin consent screen both render verbatim (§8).

### 2.3 Validation additions to `bundle_manifest_v2.py`

- New `_PERMISSION_ID_RE` accepting the catalog's static ids plus the two parameterized families (`net.http:<host>` reuses `_EGRESS_HOST_RE`; `chat.send:<platform>`/`moderation.<platform>` reuse `RELAY_PROVIDERS`).
- Reject an unknown permission id outright (`unknown_permission`) — the catalog is closed, not extensible per-bundle.
- Reject `storage.tables` with no `data.schema` and vice versa (mutual requirement, one source of truth — `data.tables` as a bare table-name list is deprecated, see §2.4).
- Reject any `dangerous`-risk permission with an empty `justification`.

### 2.4 Reconciling three existing manifest fields into one

| Existing field | Status | Disposition |
|---|---|---|
| `permissions: []` (`bundle_manifest_v2.py:102`) | Parsed, never validated, never enforced (dead) | **Becomes this spec's structured block** (§2.1) — same field name, real schema |
| `capabilities: [scheduled, moderation, db-batch]` (wit-v1.1 §4, not yet merged) | Proposed, not implemented | **Folded into `permissions:`** as `platform.scheduled` / `moderation.<platform>` / an implicit quota bump on `storage.tables` (batch is a quota dimension, not a separate grant) — land as part of the same PR that merges wit-v1.1, never ship both fields |
| `data.tables: []` (live) | Table-name allowlist only, no schema | **Retained as the DSL's own table-name list** (PR #415 §4.3 already reuses `_TABLE_RE`/`_RESERVED_TABLES`) — `storage.tables`'s grant is what makes `data.tables` reachable at all now that `db` is no longer implicitly granted |

`_derive_capabilities()` (`bundle_approval_service.py:92-101`) — which infers `http`/`db`/`relay` from manifest shape — is retired once permissions are explicit; the consent summary reads `permissions:` directly instead of re-deriving it.

---

## 3. Consent flow across the 3 tiers

```
vendor SUBMITS ──▶ GLOBAL admin (platform:admin)   reviews permission catalog at catalog approval
                          │  app_catalog + app_install_approvals
                          ▼
                   TENANT admin (tenant:admin)      sees requested permissions in the marketplace,
                          │  app_tenant_availability  MAY RESTRICT (narrow, never widen) before making available
                          ▼
                   COMMUNITY admin                  grants at activation (the install prompt) —
                          │  app_activations           Android-style permission list, dangerous ones highlighted
                          ▼
                     bundle runs, gate enforces the community's actual grant set
```

### 3.1 Global admin — catalog approval

- `bundle_approvals.py::post_approve` (`platform:admin`) already computes `get_permission_summary()`/`permission_hash()`. Extend `build_permission_summary()` (§4 of `permission_summary_service.py`) to render the full catalog (§1) instead of the 8 derived capability names.
- New required review step: every `dangerous` permission must be individually acknowledged (`approved_permissions: ["net.http:...", "reputation.community.write"]` on the approve request) — approving the version without listing every dangerous id it requests is refused (`incomplete_dangerous_ack`, 422), mirroring PR #415's `destructive_schema_change_requires_ack` posture.
- Approval writes the **maximal grant set** for the app version into a new `app_permission_requests` table (§4) — this is the ceiling every lower tier can only narrow.

### 3.2 Tenant admin — marketplace restriction

- `make_available()` (`tenant:admin`) gains an optional `restricted_permissions: []` — any subset of the catalog-approved set the tenant admin chooses to **exclude** tenant-wide. Cannot add a permission the global admin didn't approve (rejected `permission_not_in_catalog_grant`).
- Marketplace listing (hub-webui) shows the full requested list with the tenant's current restrictions pre-checked-off, same Android "toggle off what you don't want your users to have" framing, but restriction is opt-out per-permission, not per-install.
- Writes `app_tenant_availability`'s new `restricted_permission_ids` column (§4).

### 3.3 Community admin — activation (install prompt)

- `activate_bundle()` gains a mandatory `granted_permissions: []` — the community admin's actual consent, a subset of (catalog-approved minus tenant-restricted). Missing any permission the bundle's manifest lists as required (i.e. not opportunistically-used) blocks activation with the specific missing ids (`consent_required`, 422) — never a silent partial-grant activation.
- Every `dangerous` permission renders on its own line with its `justification`, exactly the Android permission-request screen shape (§8) — no "select all" for the dangerous tier.
- Writes `community_permission_grants` (§4) — the row the data plane actually reads at runtime.

### 3.4 Version upgrade requiring new/broader permissions

- `classify_diff()` (already live, `bundle_approval_service.py:253-278`) is extended to diff the full permission catalog, not just the 8 capability names + egress/tables/routes.
- `widened` (new permission id, or a `params` change that raises a bound — e.g. `delta_max` increases, a quota increases, a new `net.http` host) **requires the same 3-tier re-consent as a first install**: the global admin re-approves the new/changed dangerous permissions, and every tenant/community that had the prior version active **stays pinned to the prior version** until each tier re-consents at its own level — never auto-upgraded.
- `narrowed`/`unchanged` upgrades auto-apply at the community's next poll cycle (§4) — no re-consent needed, consistent with today's non-permission upgrade behavior.
- A community that never re-consents simply never receives the new version — `app_active_versions` keeps pointing at the last-consented `version_id`, exactly the existing FK-pointer mechanism, no new state machine.

### 3.5 Auto-approved (non-`dangerous`) permissions

`normal`-risk permissions (`storage.kv`, `storage.tables`, `storage.objects` within default quota, `chat.send:<platform>`, `reputation.read`, `flags.read`, `platform.*`) still appear on every consent screen for transparency (Android shows all permissions, not just dangerous ones) but never block approval/activation on an explicit per-id ack — only the aggregate "I've seen this list" click each tier already performs today.

### 3.6 Core `waddles.core.*` bundles

**Pre-granted, not exempted.** `seed_core_bundles.py` calls `approve_version(approved_by=None, approval_source="system:core-seeder")` — extended to also write `app_permission_requests` with `approved_by=None`/`approval_source="system:core-seeder"` for every permission the core bundle's manifest declares, still hard-guarded to `CORE_NAMESPACE_PREFIX` before any DB write. Tenant/community tiers still see the same permission list on their own consent screens (a core bundle is still individually activated per community, same as today) — only the **global catalog-approval step** is system-automated; tenant restriction and community activation consent are unchanged human steps. This keeps one code path for "does this app_id's permission set have a global-tier record," while flagging system-approved rows distinctly in the audit trail (`approval_source`) for periodic security review, since no human reviewed the dangerous asks.

### 3.7 Revocation

- Any tier can revoke a previously-granted permission at any time (tenant admin adds a restriction; community admin deactivates a specific permission without deactivating the whole bundle — new `deactivate_permission(app_id, permission_id)` alongside the existing `deactivate_bundle`).
- Revocation takes effect at the data plane's next poll (§4) — bounded by `BUNDLE_CONFIG_POLL_SECONDS`, never instant, same staleness window the rest of the 3-tier config already accepts.
- A revoked `dangerous` permission that a bundle's declared manifest requires (not opportunistic) auto-deactivates the whole bundle for that community, logged `WARN`, rather than running the bundle in a state its own manifest says it can't function in.

---

## 4. Grant storage

**hub-api is the only RW path; the data plane (`svc_process`/`svc_action`) holds a read-only role against a read replica**, identical pattern to PR #415 §2's control-plane/`bundle-data` split — grants are control-plane metadata, not bundle data, so they live in the existing control-plane DB, not `bundle-data`.

| Table | Written by | Key columns |
|---|---|---|
| `app_permission_requests` | `bundle_approval_service.approve_version()` (global tier) | `(app_id, version)` PK, `permission_id`, `risk`, `params_json`, `approved_by`, `approval_source`, `approved_at` |
| `app_tenant_permission_restrictions` | `marketplace_lifecycle_service.make_available()` (tenant tier) | `(tenant_id, app_id, permission_id)` PK — presence = restricted (excluded) |
| `community_permission_grants` | `marketplace_lifecycle_service.activate_bundle()` (community tier) | `(community_id, app_id, permission_id)` PK, `granted_by`, `granted_at`, `params_json` (community's own bound within the approved ceiling, e.g. a tighter `delta_max`) |
| `app_permission_grant_versions` | Any of the above, append-only | `(app_id, version, permission_snapshot_hash, effective_at)` — the version-pinning row §3.4 reads to decide "is this community re-consented for this version" |
| `bundle_reputation_adjustments` | Data plane (svc_action), via hub-api's existing audit path — see §7 | `(id, app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code, occurred_at, reversal_of)` |

**Versioning:** a grant is scoped to `(app_id, version)`, not just `app_id` — re-using `app_permission_grant_versions.permission_snapshot_hash` (same `canonical_json`/sha256 pattern as `permission_hash()`) as the single value the data plane compares to know whether its cached grant set for a given active version is current, without re-fetching the full row set on every poll tick.

**Data-plane read path (hot-swap polling):** `svc_process`/`svc_action` already poll bundle config every `BUNDLE_CONFIG_POLL_SECONDS` (default 300, confirmed live at `core/svc_action/src/config.rs:293`, `core/svc_process/src/config.rs:264`). Extend that same poll to also refresh an in-memory `GrantSnapshot` keyed `(tenant, community, app_id) -> {permission_id -> params}`, sourced from `community_permission_grants` JOIN `app_permission_requests` (never trusting a request-time claim). This is the snapshot §5's gate reads — zero per-invoke DB round trips.

---

## 5. The standard enforcement gate — `core/bundle_capability_gate`

**One crate, one function, every host import calls it first:**

```rust
pub fn authorize(
    scope: &InvokeScope,                 // (tenant, community, app_id) -- never bundle input
    permission: PermissionId,            // e.g. Permission::StorageTables, Permission::ReputationCommunityWrite
    resource: ResourceRef,               // AppScoped | ReputationScoped(target_user_uuid)
) -> Result<AuthorizedCall, Denied>;
```

- `CapabilityHandler::handle` in both `svc_process/src/capabilities.rs` and `svc_action/src/capabilities.rs` calls `authorize()` as the very first statement in every match arm, replacing today's ad hoc "check `data.tables` non-empty" style gating and the hardcoded `db`/`kv` denials.
- `AuthorizedCall` carries the resolved, server-derived resource (schema name, kv key prefix, object prefix, or verified target-user) so the capability implementation never re-derives it from bundle args — the gate is the only place resource derivation happens.

### 5.1 Two scope types

| Scope | Resource derivation | Examples | Guarantee |
|---|---|---|---|
| **`AppScoped`** | Server-computed from `(tenant, community, app_id)` alone — **never accepts a bundle-supplied resource identifier for the scoping part** | `storage.kv` key prefix (`waddles:app:{tenant}:{community}:{app_id}:state`, per stage.wit's existing `...:state` convention), `storage.tables` schema name (`app_<sha256(app_id)[:16]>`, PR #415 §4.4), `storage.objects` bucket prefix (§6) | A bundle can never name another app's schema/prefix — the gate rejects before the capability implementation even runs a query |
| **`ReputationScoped`** | Bundle names a `target_user` (a UUID); the gate independently verifies that user belongs to the invocation's community (`reputation.community.write`) or tenant (`reputation.tenant.write`) via a membership check against the same RO snapshot | `reputation.adjust(target_user, delta, reason_code)` | Membership is checked **at call time**, not just at grant time — a user who left the community since the bundle was activated cannot be adjusted |

### 5.2 Defense in depth

| Layer | Mechanism |
|---|---|
| Compile/link time | Per-component `Linker` (wit-v1.1 §5, reused verbatim) registers only host functions for permissions the manifest declares **and** the community has actually granted (not just requested) — an ungranted function is never linked; a call to it fails at instantiation, before any guest code runs |
| Runtime (every call) | `core/bundle_capability_gate::authorize()` re-checks the grant against the current `GrantSnapshot` (§4) — catches a mid-poll-interval revocation the linker (built at instantiation, which may predate the revocation) hasn't yet caught up to |
| Post-authorize | Capability-specific validation continues exactly as designed elsewhere (sqlparser-rs AST gate for `db`, content-type/size checks for `storage.objects`, delta-bound/rate-limit for `reputation`) — the gate answers "is this call allowed at all," not "is this specific SQL/object/delta valid" |

**No kill-switch.** Per `critical-rules.md`'s security-sensitive-mechanism rule, there is no env var, CLI flag, or config setting that disables `authorize()` — the only sanctioned bypass is the core-bundle system-approval path (§3.6), which still goes through the same table and the same gate, just with a distinguishable `approval_source`.

### 5.3 Denials

- Typed WIT error variant per capability interface (`denied(string)` already the pattern for `http`/`db`/`relay`; extended with a stable `reason` string: `not_granted`, `resource_scope_mismatch`, `quota_exceeded`, `rate_limited`, `user_not_in_scope`, `delta_out_of_bounds`).
- Every denial: (1) audit-logged (`tracing` + OTel, sanitized per existing `capabilities.rs` pattern), (2) counted (`waddles_bundle_capability_denied_total{app_id, permission, reason}`), (3) returned to the guest as `access-denied`, never a fabricated success — same posture PR #415 and wit-v1.1 already commit to for their own denial paths.

### 5.4 Performance budget

- Grant lookup: one hashmap read against the in-memory `GrantSnapshot` (§4), O(1), no lock contention beyond a `RwLock` read guard already used for the existing bundle-config hot-swap — sub-microsecond, no allocation on the hot path.
- Reputation membership check: one additional hashmap read against a `(community_id) -> HashSet<user_uuid>` membership snapshot, refreshed on the same `BUNDLE_CONFIG_POLL_SECONDS` cadence as everything else — never a synchronous DB call inside `authorize()`.
- Target: `authorize()` adds ≤ 5µs p99 to a host-call round trip — validated by a criterion benchmark in the gate crate's own test suite (`writing-rust-tests` skill pattern), gating merge the same way wit-v1.1's other performance-sensitive paths do.

---

## 6. Object storage (`storage.objects`)

| Aspect | Design |
|---|---|
| Bucket/prefix | New dedicated bucket `waddles-bundle-objects` (parallels PR #415's dedicated `bundle-data` Postgres DB — blast-radius isolation from the existing control-plane `_bucket()` used for bundle artifacts/community logos), prefixed `tenant/{tenant_id}/community/{community_id}/app/{app_id}/` — server-derived (`AppScoped`, §5.1), a bundle-supplied `key` is appended under this prefix only after rejecting `..`/leading `/`/absolute paths |
| Quotas | `max_object_bytes`, `max_total_bytes`, object-count ceiling — manifest-declared within the catalog default ceiling (§1), enforced pre-write via a periodic size/count scan job, same "cached last-known, fail closed on exceed" pattern as PR #415 §6's row/byte quota gate |
| Content-type allowlist | Manifest `content_types[]`, validated against a small platform allowlist (`image/png`, `image/jpeg`, `image/webp`, `application/json`, `text/plain` — no `application/octet-stream`/executable types without an explicit, dangerous-risk override) |
| Max object size | Catalog default 5 MB per object, manifest may request lower, never higher, without a dangerous-risk re-ack |
| Encryption | Per-tenant, at rest — same MinIO server-side encryption baseline `security.md` already mandates for every bucket; no new KMS integration |
| Deletion on uninstall | Same lifecycle as PR #415 §8's table deletion: per-tenant uninstall deletes that tenant's prefix immediately (audited); last-tenant-uninstalled retains the app's objects for the same 30-day grace window, then a background sweep deletes the whole `app/{app_id}/` prefix |
| Guest WIT interface | `put`/`get`/`list`/`delete`, scoped exactly as sketched below |

```wit
/// Bundle-scoped object storage under the app's own tenant/community
/// prefix (server-derived, never bundle-supplied — spec SS5.1 AppScoped).
/// Capability: granted only when `storage.objects` is declared and granted.
interface objects {
  record object-meta {
    key: string,
    size: u64,
    content-type: string,
    etag: string,
    last-modified: string,
  }

  variant error {
    denied(string),
    not-found,
    too-large(u64),
    invalid-content-type(string),
    quota-exceeded(string),
    backend(string),
  }

  put: func(key: string, content-type: string, body: list<u8>) -> result<object-meta, error>;
  get: func(key: string) -> result<option<tuple<object-meta, list<u8>>>, error>;
  list: func(prefix: string, cursor: option<string>, limit: u32)
    -> result<tuple<list<object-meta>, option<string>>, error>;
  delete: func(key: string) -> result<_, error>;
}
```

---

## 7. Reputation (`reputation.*`)

**No existing capability lets a WASM bundle touch either reputation score** (`community_members.reputation` or `reputation_global.score`). This spec routes bundle-originated changes through the **existing** `core/reputation_module` gRPC service rather than a raw table write, reusing its clamping/weighting/audit logic instead of duplicating it.

### 7.1 WIT interface

```wit
/// Capability: action-stage-only (spec SS6.5's "granted only to
/// action-stage bundles" rule, same as `relay`/`moderation`). `read` needs
/// `reputation.read`; `adjust`'s `scope` must be covered by the caller's
/// actual grant (`reputation.community.write` covers `community`;
/// `reputation.tenant.write` covers both).
interface reputation {
  enum scope-kind { community, tenant }

  variant error {
    denied(string),
    user-not-in-scope,
    delta-out-of-bounds(s32),
    rate-limited(u32),
    backend(string),
  }

  /// `target-user` is always the canonical Waddles user UUID -- never a
  /// platform-native id or username (PII-tokenization rule).
  read: func(target-user: string, scope: scope-kind) -> result<s32, error>;

  /// Returns the new score. `reason-code` must be one of the manifest's
  /// declared `reason_codes` for the granted permission.
  adjust: func(target-user: string, scope: scope-kind, delta: s32, reason-code: string)
    -> result<s32, error>;
}
```

### 7.2 Call path

1. `core/bundle_capability_gate::authorize()` — `ReputationScoped(target_user)`: verify `target_user` belongs to the invocation's community (or tenant, for `tenant` scope) via the membership snapshot (§5.4). Not in scope → `user-not-in-scope`, no RPC attempted.
2. Delta bound check against the manifest's granted `delta_min`/`delta_max` (community admin's own bound, §3.3, never looser than the global-approved ceiling). Out of bounds → `delta-out-of-bounds`.
3. Rate-limit check — per-app-per-user-per-day and per-app-per-community/tenant-per-day aggregate token buckets (`UsageBatcher`, same primitive `relay`/`moderation` already use). Exceeded → `rate-limited`, **before** any RPC, matching moderation's "rate-limit strictly before the external call" merge gate (wit-v1.1 §7.1).
4. `reason_code` allowlist check against the manifest's declared `reason_codes`.
5. gRPC call to `core/reputation_module`'s `ReputationService` — **needs a new RPC**, `AdjustScore(tenant_id, community_id, user_id, scope, delta, reason_code, actor_app_id) -> SuccessResponse`, since the existing `RecordEvent` is weight-table-driven (`chat_message`/`command_usage`), not an arbitrary signed-delta API. Flagged as an open question (§10) — not designed further here.
6. Unconditional audit row: `bundle_reputation_adjustments` (§4), regardless of RPC success/failure.

### 7.3 Anti-abuse controls

| Control | Mechanism |
|---|---|
| Per-user cap | `|delta| <= delta_max` per call, plus a per-app-per-user-per-day aggregate cap (catalog default ±5, §1) |
| Per-community/tenant cap | Aggregate daily cap across all users (catalog default ±50 community / ±200 tenant, §1) — bounds a bundle distributing many small adjustments across many users to avoid the per-user cap |
| Mandatory reason code | Every `adjust()` requires a manifest-declared `reason_code` — no free-text, no blank reason; makes bulk-reversal and audit trivial |
| Reversibility | Every adjustment is a ledger row (`bundle_reputation_adjustments`), never an in-place score edit outside `reputation_module`'s own clamp logic; a reversal is a new `adjust()` call with the negated delta and `reason_code = "reversal:<original_id>"`, itself audited — never a destructive delete of the original row |
| Statistical anomaly flag | A background hub-api job flags an app whose daily adjustment distribution deviates > 3σ from its own trailing 30-day baseline for tenant-admin review (surfaced as a dashboard warning, never an automatic grant revocation — a human decides) |
| Scope containment | `community.write` can never touch `reputation_global.score` — only `tenant.write` can, and only for users within that tenant — enforced by the gate's membership check, not by convention |
| Auto-ban interaction | A bundle-driven adjustment that would cross `REPUTATION_AUTO_BAN_THRESHOLD` (450) is **not** treated as a bundle-triggerable side effect — `reputation_module`'s own auto-ban logic runs downstream of any write, unchanged; bundles never get a distinct "ban" verb here (that's `moderation.ban`, wit-v1.1 §2) |

---

## 8. hub-webui consent screens

| Screen | Tier | Content |
|---|---|---|
| Catalog approval | Global admin | Full permission list, `dangerous` ones visually separated ("Dangerous permissions" section, red/amber accent) with per-id checkbox ack; cannot submit approval with an unchecked dangerous permission |
| Marketplace listing | Tenant admin | Same list, each permission toggleable **off only** (opt-out), dangerous ones pre-expanded by default; a tooltip shows the bundle's own `justification` string verbatim |
| Install/activation prompt | Community admin | Android-style single scrollable list grouped `Dangerous` (top, expanded, each with an icon + justification) then `Standard` (collapsed by default, "View all N permissions" disclosure) — big "Allow" / "Don't install" buttons, no partial-grant default (every listed permission must be explicitly allowed or the specific missing ones are called out, per §3.3) |
| Re-consent banner | Community admin | Shown when a pinned-old-version community's app has a newer version awaiting re-consent (§3.4) — diffs old vs. new permission set inline (`+ reputation.tenant.write`, styled like a diff) |
| Revoke | Tenant/Community admin | Per-permission toggle from the app's settings page, calling `deactivate_permission` (§3.7); dangerous permissions get a confirmation step naming what breaks (from the manifest's own "required vs. opportunistic" flag) |

---

## 9. Phased implementation plan (agent-sized, ≤30 min each)

| Phase | Task | Component | Depends on |
|---|---|---|---|
| 0 | Define `Permission`/`PermissionId`/`Risk` enums + catalog table (§1) as a shared Rust module in the new `core/bundle_capability_gate` crate; unit tests for id parsing (`net.http:<host>`, `chat.send:<platform>`) | Rust | none |
| 0 | Add `_PERMISSION_ID_RE` + catalog validation to `bundle_manifest_v2.py`, replacing dead `permissions: []` parsing (§2.3) | hub-api | Phase 0 (catalog agreed) |
| 0 | Migration: `app_permission_requests`, `app_tenant_permission_restrictions`, `community_permission_grants`, `app_permission_grant_versions`, `bundle_reputation_adjustments` (§4) | hub-api (Alembic-style raw migration, control-plane DB) | none |
| 1 | Extend `permission_summary_service.build_permission_summary()` to render the full catalog instead of `_derive_capabilities()`'s 8 names; update `classify_diff()`'s flatten set to include permission ids + params | hub-api | Phase 0 |
| 1 | `bundle_approvals.py::post_approve` gains `approved_permissions[]` ack requirement + `incomplete_dangerous_ack` refusal (§3.1) | hub-api | previous task |
| 2 | `marketplace_lifecycle_service.make_available()` gains `restricted_permissions[]` (§3.2); `activate_bundle()` gains mandatory `granted_permissions[]` + `consent_required` refusal (§3.3) | hub-api | Phase 1 |
| 2 | `deactivate_permission(app_id, permission_id)` endpoint + service function (§3.7) | hub-api | previous task |
| 2 | `seed_core_bundles.py` extended to also write `app_permission_requests` rows with `approval_source="system:core-seeder"` (§3.6) | hub-api | Phase 0 |
| 3 | `core/bundle_capability_gate` crate: `authorize()`, `InvokeScope`, `AppScoped`/`ReputationScoped` resource derivation, `GrantSnapshot` type, unit tests against fixed grant fixtures | Rust | Phase 0 |
| 3 | `GrantSnapshot` hydration wired into `svc_process`/`svc_action`'s existing `BUNDLE_CONFIG_POLL_SECONDS` poll loop (§4) | Rust (both stages) | previous task |
| 3 | `svc_process`/`svc_action`'s `CapabilityHandler::handle` calls `authorize()` first in every match arm, replacing today's hardcoded `db`/`kv` `not_implemented` denials with `not_granted` where applicable | Rust (both stages) | previous task |
| 4 | Wire `storage.kv` end-to-end through the gate (`feature/bundle-kv-capability` — currently zero commits; this phase **is** that branch's scope): `AppScoped` key-prefix derivation, quota enforcement, real Valkey hash get/set/delete/increment | Rust (both stages) | Phase 3 |
| 4 | Wire `storage.tables` through the gate atop PR #415's `db.execute` implementation once it lands — the gate supplies the `AppScoped` schema-name resolution PR #415 §3.2 step 3 currently sketches as "resolved server-side"; this phase makes that resolution literally the gate's output | Rust (both stages) | Phase 3, PR #415's Phase 3 |
| 5 | `storage.objects`: bucket/prefix provisioning at community activation, quota scan job, WIT interface (§6) wired into `bundle_executor`'s linker + both stages' `CapabilityHandler` | Rust + hub-api | Phase 3 |
| 6 | `reputation.proto`: add `AdjustScore` RPC (§7.2 step 5, open question §10) | Python (`core/reputation_module`) | none — can start in parallel with Phase 0-3 |
| 6 | `reputation` WIT interface (§7.1) + gate wiring (membership snapshot, delta/rate-limit checks) in `svc_action` only (action-stage-only) | Rust (svc_action) | Phase 3, previous task |
| 6 | `bundle_reputation_adjustments` audit write path + anomaly-flag background job (§7.3) | hub-api | Phase 6 (schema exists) |
| 7 | hub-webui: catalog-approval dangerous-permission ack UI (§8 row 1) | React | Phase 1 |
| 7 | hub-webui: marketplace restriction toggles (§8 row 2) | React | Phase 2 |
| 7 | hub-webui: community activation Android-style consent screen + re-consent banner (§8 rows 3-4) | React | Phase 2 |
| 7 | hub-webui: per-permission revoke UI (§8 row 5) | React | Phase 2 |
| 8 | OTel metrics/logs/traces for `authorize()` denials and grant-snapshot refresh (§5.3) | Rust (both stages) | Phase 3 |
| 9 | Fishing bundles (PR #414) re-request permissions against the final catalog once Phases 0-5 land — no fishing-specific platform work beyond what PR #414/#415 already scoped | fishing spec owner | Phases 0-5 |

Phases 0-2 (hub-api schema + consent flow) and the `reputation.proto` RPC addition (Phase 6's first task) have no Rust dependency and can proceed fully in parallel with Phase 3 (the gate crate itself).

---

## 10. Open questions (not blockers, flagged for follow-up)

- **`reputation_module`'s `AdjustScore` RPC** (§7.2 step 5) is named but not designed here — needs its own short addendum covering how it interacts with `reputation_service.py::adjust()`'s existing weight/clamp pipeline (does a bundle-driven delta bypass weighting entirely, or get treated as a new weighted `event_type` per app?).
- **`storage.objects` bucket topology** — a single `waddles-bundle-objects` bucket with tenant/community/app prefixes (this doc) vs. PR #415's "separate database" isolation philosophy applied to storage too (a bucket-per-tenant) is a cost/isolation tradeoff not fully resolved here; default to the single-bucket-with-prefixes model unless a security review calls for stronger isolation.
- **Cross-bundle permission sharing** (e.g. fishing-shop's tables read by fishing-core, PR #415 §10) is out of scope for the permission *catalog* — it's an existing open question in PR #415, unaffected by this design.
- **`moderation.<platform>` catalog entry vs. wit-v1.1's bare `moderation` capability flag** — this doc assumes the fold happens in the same PR that lands wit-v1.1 (§2.4); if wit-v1.1 merges first with the bare flag, a follow-up migration renames it without changing enforcement semantics.
