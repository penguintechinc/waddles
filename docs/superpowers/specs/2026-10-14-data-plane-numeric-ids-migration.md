# Data-Plane Numeric-ID Migration — Plan (for sign-off)

**Date:** 2026-10-14 · **Status:** DRAFT for review — no ABI-breaking code until signed off.

## Goal
In the data plane, key **tenants, communities, and bundles** by **numeric id** (`tenant_id`, `community_id`, `bundle_id`). Names/slugs/`app_id` strings become **display-only** metadata, never a lookup/stream/cache/routing key.

**Why (user's rationale):** strings on the traffic path are an attack surface — injection, Unicode/homoglyph/canonicalization spoofing, key confusion, parse ambiguity. A validated integer FK has one canonical form and no parse step. (Same principle as PII tokenization + the username-in-payload gap.)

**Bonus deliverable:** gives svc_ingest the **guild→`community_id`** resolution it lacks today, which unlocks the **per-community activation** model (installed→enabled→**activated-in-community**) and removes PR #860's scope-slug gap.

## Current state (from the data-plane audit)
Two disjoint identifier spaces, bridged one-way:
- **DB / active-set / grant layer** keys on numeric `(tenant_id, community_id)` + **`app_id` STRING** (`app_catalog` PK is the string; ~10 FK tables, several composite PKs).
- **Spine envelope / Valkey streams** key on tenant **slug** + community **name** strings; `app_id`/`target_app_id` strings for bundle identity/routing.
- Bridge: `core/bundle_active_set/src/scope.rs::resolve_scope` (numeric → slug/name, one-way).
- **svc_ingest has no DB**; stamps static `RUNNER_TENANT_SLUG` (default `global`) + `RUNNER_COMMUNITY` (default empty → tenant-wide sentinel `community_id 0`). Never maps a platform guild/channel → `community_id`.
- The `StageEnvelope` is **HMAC-bound** (`penguin-spine` D30 `binding.mac`) and its **WIT record is compiled into every Wasm bundle** → changing it is an immutable wire-format + component-ABI break.

## Key design decisions (please confirm)

**D1 — Bundle identity split (RECOMMENDED):**
- `app_id` (reverse-DNS string, e.g. `waddles.core.example.about`) stays the **canonical, signed, public identity** — it remains in the signed manifest and is verified once at load. **Not re-signed, not changed.**
- A new numeric **`bundle_id`** (surrogate PK on `app_catalog`) becomes the **data-plane routing/FK/lookup key** (active-set, grants, `target_bundle_id` cross-bundle routing, executor load map, consumer-group identity).
- → The hot path keys on `bundle_id` (numeric); the `app_id` string survives only inside the verified-once manifest + display. **This avoids re-deriving signatures** (signatures stay keyed on `(app_id, version)`), which is the single biggest risk-reducer.
- **⚠ Two guards this REQUIRES (second-opinion), or it's a hijack vector:** (1) on load-by-`bundle_id`, the host **must assert** `signed_app_id == catalog.app_id(bundle_id)` — else tampering the `bundle_id→app_id` mapping runs attacker bytes under a trusted identity; (2) `app_id` must pass **strict canonical validation** (`^[a-z0-9._-]+$`, NFC) *before* it enters signature verification, since the string stays in the verified path and is still exposed to canonicalization/homoglyph tricks there.

**D2 — Envelope carries numeric keys; names ride as display-only metadata (DECIDED 2026-10-14):**
- Envelope gains `tenant_id`, `community_id`, `bundle_id`, `target_bundle_id` (the keys).
- Tenant slug / community name / `app_id` string are **kept as an explicit display-only metadata field** on the envelope — **never** a lookup/stream/cache/routing/authz key, and the pipeline never branches on them. (User choice: keep them for display convenience rather than resolve at the boundary.) They are HMAC-bound along with everything else for integrity.
- **⚠ SECURITY-CRITICAL (second-opinion):** the HMAC **must bind every numeric id** (`tenant_id`, `community_id`, `bundle_id`, `source_id`) alongside `timestamp`/`nonce`/`payload`. If the ids are added to the struct but omitted from the digest, an attacker can swap `tenant_id: 10 → 20` on a validly-signed message and have it injected into another tenant's stream + WASM sandbox (cross-tenant replay). The whole security win depends on the ids being *in the signed digest*, not just the struct. Add a regression test that mutating any id invalidates the MAC.
- **⚠ WIT is binary-breaking (second-opinion):** you **cannot remove/retype** a field on a WIT record and have existing compiled WASM bundles instantiate (Canonical ABI lift/lower mismatch / linking error). So this is **explicit WIT interface versioning** (`waddle:bundle/stage@2.0.0`), with the Rust host exposing **both** v1 (string) and v2 (numeric) interfaces during transition; a bundle moves to v2 only when rebuilt. "Additive-first" means *a new versioned interface*, not mutating v1.

**D3 — Stream key format goes numeric:**
- `waddles:t:{tenant_id}:c:{community_id}:src:{platform}:{source_id}:events` (today: `:t:{slug}:c:{name}:`). Tenant-wide sentinel stays `c:0`.

**D4 — svc_ingest gets a resolver:**
- svc_ingest gains a hub-api client (reuse the **#860 pattern**: `service_auth` machine-JWT + mTLS/HTTP) to resolve `(platform, guild/channel/source) → (tenant_id, community_id)`, cached with TTL + fail-closed. Replaces static `RUNNER_*`. This is net-new infra and the largest behavioral change.

## Phased rollout (schema-version-gated, each phase independently shippable)

**Phase 0 — Additive schema (non-breaking):**
- Add `bundle_id BIGINT GENERATED ALWAYS AS IDENTITY` surrogate PK to `app_catalog`; keep `app_id` UNIQUE. Backfill.
- Add nullable `bundle_id` FK columns alongside `app_id` on the ~10 FK tables (`app_active_versions`, `community_permission_grants`, `app_source_bindings`, grants, …); backfill from `app_catalog`. **Do not drop `app_id` yet.**
- No data-plane behavior change. Reversible.

**Phase 1 — v2 WIT interface (not field mutation) + host dual-interface + ingest resolver:**
- Introduce a **new versioned** `waddle:bundle/stage@2.0.0` interface carrying the numeric ids; the Rust host serves **both** v1 (string) and v2 (numeric) so existing compiled bundles keep working untouched. Add the numeric fields to the spine envelope struct + HMAC digest (per D2) + Python dataclass + fixtures.
- Rebuild bundles to v2 at their own pace (a v1 bundle keeps running on the v1 host interface).
- **svc_ingest resolver (D4):** async **in-memory LRU cache** (guild/source → tenant_id,community_id) with **background refresh + single-flight stampede protection**; a hub-api lookup only on cold miss; explicit fallback (dead-letter or an `unresolved` metric + tenant-wide sentinel, never mis-route). Never a synchronous blocking hub-api call on the per-message hot path.
- Consumers **prefer numeric when present, fall back to string**.

**Phase 2 — Numeric stream keys (NO dual-write) + consumer cutover:**
- **⚠ Do NOT dual-write streams (second-opinion):** writing each event to both the string- and numeric-keyed stream → consumer groups on both → **every WASM bundle executes twice** (side-effects doubled); and the `{tenant_slug}` vs `{tenant_id}` hash-tags land on **different Redis-cluster slots/nodes** (extra round-trips, no multi-key atomicity). Instead: (1) make consumers numeric-stream-*capable* first, (2) producers do an **atomic switch to numeric-only** stream keys (D3), (3) **drain** the old string-keyed streams to zero, then stop the legacy consumers. Single delivery throughout.
- Consumers key active-set/grants on `bundle_id` (D1) + numeric scope; `resolve_scope`'s key role is removed (kept only for display if needed).

**Phase 3 — Drop string keys + ring warm-up:**
- Retire the v1 WIT interface once all bundles are on v2; remove tenant slug / community name / `app_id` from the envelope wire (display resolved at the boundary).
- Drop the `app_id` FK columns once `bundle_id` FKs are authoritative.
- **⚠ Watermark re-key is a thundering-herd (second-opinion):** re-deriving the active-set watermark hash from `app_id`→`bundle_id` reshuffles the whole hash ring → every process node drops its compiled-WASM cache and recompiles at once (CPU spike + bundle-store stampede). **Pre-warm** the wasmtime module cache for the new `bundle_id` assignments and do a **staggered** rebalance *before* flipping the hash function.
- `app_id` remains ONLY as the signed-manifest identity + a display/catalog column; host asserts `signed_app_id == catalog.app_id(bundle_id)` on load (D1).

## Risks & mitigations
- **R1 — Immutable wire/ABI break:** mitigated by additive-first (Phase 1) + schema-version gating + a drain window (Phase 2). Old bundles keep working until rebuilt.
- **R2 — Signatures:** avoided entirely by D1 (signatures stay on `app_id`+version; `bundle_id` is a non-signed internal surrogate).
- **R3 — In-flight stream entries stranded at cutover:** drain/dual-consume window in Phase 2.
- **R4 — svc_ingest resolver is net-new + on the hot path:** cache with TTL, fail-closed, flag-gated; a resolver miss drops to the tenant-wide sentinel rather than mis-routing.
- **R5 — Composite-PK churn across ~10 tables:** additive columns first (Phase 0), authoritative-swap last (Phase 3); each table migrated with a backfill + a dual-read window.

## Rollback
Each phase is independently reversible until Phase 3's string-key drop. Phases 0–2 keep both representations; rollback = turn the flag off / prefer-string. Phase 3 is the point of no return (gated behind proven Phase 2 on beta).

## Decisions (locked 2026-10-14)
1. **D1:** ✅ signatures stay on `app_id`; `bundle_id` internal-only (with the two load-time guards).
2. **D2:** ✅ names kept as display-only metadata fields on the envelope (never keys); numeric ids are the keys.
3. **Scope:** ✅ v3.0.x.
4. **Sequencing (recommended):** build **slice A (D4 + per-community scoping)** first — it delivers real user value (Twitch live-chat + the per-community activation model) and is non-breaking; defer the full `bundle_id` swap (Phase 0 surrogate + Phase 3 string-drop) as slice B.

## First build slice — "A: numeric community scoping + ingest resolver" (non-breaking)
Goal: an inbound Discord/Twitch message resolves to its real `community_id`, and bundles activated **in that community** run. Pieces:
1. **hub-api:** a resolver endpoint `(platform, guild/channel/source) → (tenant_id, community_id)` for the data plane (machine-JWT scoped, like the #860 pattern).
2. **svc_ingest:** async LRU cache + single-flight resolver (D4); stamp numeric `tenant_id`/`community_id` on the spine `Scope`/envelope (via the v2 `stage@2.0.0` interface, HMAC-bound), fail-closed to the tenant-wide sentinel on a miss.
3. **svc_process/svc_action:** consume numeric scope (prefer-numeric), subscribe/route per the resolved `community_id`.
4. **Activation:** per-community activation now actually routes (the user's installed→enabled→**activated-in-community** model) — and Twitch chat can flow once a channel is resolved to a community.
Slice B (the `bundle_id` surrogate + wire string-drop) follows as a separate, deliberate effort within v3.0.x.
