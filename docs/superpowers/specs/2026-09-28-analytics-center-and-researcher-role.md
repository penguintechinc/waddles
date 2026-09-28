# Waddles v3 Analytics Center + Researcher Role — Design

**Date:** 2026-09-28
**Status:** Proposed design — no code
**Scope:** `hub_api` (analytics query layer, auth/scopes, license gating), `admin/hub_module/frontend` (hub-webui Analytics Center page), a new analytics rollup store, license-server entitlement wiring.
**Reconciles with:** `flask_core/authz.py`+`tenancy.py` (scope/tenant enforcement, read verbatim for this doc), `hub_api/blueprints/v1/analytics.py`+`services/analytics_service.py` (existing platform/community/self analytics), `docs/bundle-telemetry-capability` branch (admin/vendor telemetry visibility — community/tenant/global admins see logs+metrics per scope, vendors see only where their bundle is activated/running), GH #419 (bundle permission catalog + 3-tier consent flow — same consent-grant shape reused for tenant researcher opt-in), `2026-09-28-tenant-envelope-encryption-design.md` (per-tenant DEK hierarchy reused for the analytics store), `critical-rules.md` (Feature Flags & License Tiers, PII Tokenization, Licensing Model).

**Justin's framing (verbatim):** "our webui is going to need a full analytics center in general with 'view filters' depending on the scope of the visitor (global admin, tenant admin, community admin, vendor, researcher) ... the researcher should be a new role (group of scopes) for third party sponsors and analytics consumers which we can sell data to and scoped to one or many tenants. Researchers should get aggregate metadata about communities and the tenant... not exact sensitive data."

**Normative addendum (Justin, mid-review):** researcher role exists **only on Enterprise-licensed deployments**, gated globally and per-tenant — see §2.4.

---

## 1. Role Matrix

Every row is enforced **server-side only** — `tenant_middleware` → `require_scope` → a query-layer tenant/community/researcher-grant filter, in that order (security.md ordering contract). The webui's "view filter" dropdown is a UI convenience over the caller's own JWT scopes; it is never trusted to restrict a query — the API returns exactly what the JWT+grant allows regardless of any client-supplied filter param.

| Role | Dashboards | Dimensions | Metrics | Drill-down depth | Exports | Enforcement |
|---|---|---|---|---|---|---|
| **Global admin** | Platform overview, all tenants, all communities, license/entitlement health | tenant, community, bundle, platform, time (any bucket) | raw + aggregate, incl. PII-adjacent audit fields | full — down to individual user/message row via existing admin tooling | CSV, all dashboards | `*:read` wildcard bundle (existing `admin` bundle) |
| **Tenant admin** | Tenant overview, all communities in tenant, bundle usage in tenant | community, bundle, platform, time | raw + aggregate for owned tenant only | community → member (existing `community.analytics:admin`-shaped routes) | CSV, tenant-scoped only | `tenant_scoped()` ORM filter + `analytics:read`/`community.analytics:admin` |
| **Community admin** | Single-community dashboard | member, bundle, time | raw + aggregate for owned community only | member-level (existing `/community/<id>/members/<id>/*`) | CSV, community-scoped only | `community_in_tenant()` + `community_member_exists()` (existing, `analytics_service.py`) |
| **Vendor** | Bundle-health dashboard, scoped to communities where the vendor's bundle is installed/activated | community (only where bundle running), version, time | bundle-emitted logs/metrics only (per `docs/bundle-telemetry-capability`) — no cross-vendor or platform data | bundle-instance level, never member/user rows | CSV of own bundle's telemetry only | bundle-install grant join (same table the telemetry-visibility spec uses to scope "where activated and running") |
| **Researcher (NEW)** | Aggregate cross-tenant/cross-community research dashboard, scoped to the researcher's tenant grant list | tenant (opted-in + Enterprise only), community-type/category, coarse time (week/month), coarse geography (country/region) | **aggregate only** — counts, rates, distributions above k-anonymity floor; never a metric keyed to <k users | **none below cohort level** — no community/member drill-down, no raw event access | CSV of pre-approved aggregate views only, watermarked with query-audit id | see §2–§3 |

**View filters (date range, tenant, community, bundle, platform)** are request parameters the query layer intersects with the caller's server-resolved scope — a filter can only *narrow* what the JWT+grant already allows, never widen it. A community admin passing `?tenant=other` gets that param ignored/404'd, not honored, matching the existing `community_in_tenant` 404-not-403 convention.

---

## 2. Researcher Role

### 2.1 Scope bundle

New bundle `researcher`, following the existing `<domain>.<resource>:<action>` convention (`community.analytics:admin` precedent):

| Scope | Grants |
|---|---|
| `analytics.aggregate:read` | Aggregate cross-community/tenant dashboards, subject to §3 privacy floor |
| `analytics.export:read` | CSV export of pre-approved aggregate views (separate scope so a researcher grant can be view-only) |

Deliberately **excludes** `analytics:read` (platform raw overview) and `community.analytics:admin` (member-level) — a researcher token can never satisfy those routes even if union-granted by accident; routes stay scope-exact, no `*:read` wildcard is ever added to this bundle.

### 2.2 Tenant scoping (1..N tenants)

New table `researcher_tenant_grants(researcher_user_id, tenant_id, granted_by_user_id, contract_ref, consent_recorded_at, expires_at, revoked_at)` — one row per (researcher, tenant) pair, mirroring the GH #419 3-tier-consent grant-storage shape (hub-api RW table, no client-writable path).

- **Issued only by global admin** (`users:admin` scope) — a tenant admin cannot self-grant a researcher into their own tenant; they can only countersign consent (below).
- **Per-tenant consent/contract required** — `contract_ref` (DPA/commercial-agreement pointer) and `consent_recorded_at` are mandatory columns, not nullable; a grant row with no consent reference cannot be created. This is the tenant's opt-in (§4).
- Query-time filter: every researcher query joins against `researcher_tenant_grants` (active, unexpired, unrevoked) **intersected with §2.4's Enterprise+opt-in check** — a tenant absent from that intersection is silently excluded from results, not an error (so a researcher with 5 tenants and 1 lapsed license just sees 4 tenants' data, no partial-failure noise).

### 2.3 Tokens, seats, audit

- **Short-lived tokens**: researcher JWTs use the existing 1h default / 24h max ceiling (security.md JWT Claims) — no long-lived researcher API key; refresh requires re-auth through the same OIDC flow as any other user.
- **Every query audit-logged**: `researcher_query_audit(id, researcher_user_id, tenant_ids_requested, tenant_ids_served, query_params, result_cohort_sizes, created_at)` — populated server-side after the k-anonymity/suppression pass (§3), never client-supplied. This is both the differencing-attack control (§3) and the commercial usage meter (§7).
- **Seat, not a license file**: a researcher is a row in the identity table like any other user (`critical-rules.md` Licensing Model — "seat = any customer identity"). Creating a researcher consumes a seat slot under normal seat-overage rules (block new researcher creation on overage, never revoke an existing one mid-cycle). Researcher seats are visibly labeled in the seat/billing UI (not a hidden internal identity) since they're the thing being commercially metered.

### 2.4 Enterprise license gating (normative)

The researcher role is an **Enterprise-only capability**, gated at two independent points, both of which must pass on every query — not just at grant time:

| Gate | Check | Failure mode |
|---|---|---|
| **Global/platform gate** | `feature_enabled("waddles.researcher-role", tenant=<platform-default-tenant>)` — the deployment itself must resolve to `get_tier() == "enterprise"` (via `EntitlementClient`/`resolve_tier()`, same mechanism `feature_enabled` already uses) | If the platform isn't Enterprise-licensed: no researcher role can be **issued** at all (global admin's grant-creation endpoint 402s), and any existing researcher JWT fails every query (fail closed) |
| **Per-tenant gate** | For each tenant in the researcher's grant list, `feature_enabled("waddles.researcher-role", tenant=<that tenant's slug>)` — that specific tenant's own license must independently resolve to `enterprise` | A tenant that is NOT Enterprise-licensed is **excluded from query results** even if a `researcher_tenant_grants` row exists for it — same silent-exclusion behavior as an unopted-in tenant (§2.2); this is deliberate so a tenant that downgrades from Enterprise is immediately out of every researcher dataset without needing its grant row deleted |

- **PostHog flag**: `waddles.researcher-role`, defaulted **OFF**, `tier_requirements["waddles.researcher-role"] = "enterprise"` in the `EntitlementClient` config — both gates route through the same flag key, just evaluated against different `tenant=` values (global default tenant vs. each grant's tenant), reusing `feature_enabled`'s existing two-gate (PostHog AND license) + outage-degrade-to-cache behavior verbatim.
- **License lapse → immediate revoke, fail closed.** This is why §2.3 mandates short-lived tokens and §2.2 mandates a query-time (not mint-time) recheck: a global or per-tenant license lapse takes effect on the *next* researcher query, not at next token refresh. Revocation is not a separate workflow — it's the natural consequence of `feature_enabled` returning `false` for that tenant on the next call. Every lapse-caused exclusion is written to `researcher_query_audit` (`tenant_ids_requested` vs `tenant_ids_served` diverging is itself the lapse signal — no separate revocation-event table needed).
- **Bypass is domain-based only** (`penguintech.md` License Bypass Domains) — the internal `*.penguintech.cloud`/`*.penguincloud.io` bypass applies uniformly through the same `feature_enabled` call; there is no env var, CLI flag, or config toggle that unlocks the researcher role, matching `critical-rules.md`'s bypass rule exactly.
- **Statutory rights are never gated by this.** Do-Not-Sell/Share, consent withdrawal, and DSAR/erasure (§4) apply in every tier regardless of the platform's or a tenant's Enterprise status — a Free/Professional tenant that never has researcher access still gets full statutory rights; those two things are orthogonal, not the same gate.

---

## 3. Privacy Protections (Aggregate-Only Data)

| Control | Rule |
|---|---|
| **k-anonymity floor** | Minimum cohort size **k=25** for any reported cell (count, rate, or distribution bucket). A query whose result would expose a cell below k is suppressed (returned as `null`/"insufficient data"), not rounded or approximated. |
| **Small-cell suppression** | Any dimension combination (e.g., tenant × community-category × week) with <k underlying users/communities is dropped from the response entirely, not merged silently into an adjacent bucket the researcher didn't ask for. |
| **Coarse time bucketing** | Minimum granularity: **week**. No day/hour buckets exposed to researchers (day/hour buckets remain available to admins in §1, where identity isn't the same risk). |
| **Coarse geography** | Minimum granularity: **country/region** (ISO-3166 country or a coarser platform-defined region). No city, postal code, or IP-derived geolocation ever reaches a researcher response. |
| **No user-level rows** | Every researcher-facing endpoint returns pre-aggregated rows only — no endpoint accepts a `user_id`/`hub_user_id` path or query param, structurally (route table has none), not just by convention. |
| **No UUIDs/free text/rare categories** | No `row_uuid`, `hub_user_id`, message content, username, or free-text field in any researcher response. Rare categorical values (e.g., a community "type" with only 1-2 instances tenant-wide) are bucketed into an "other" category below the reporting floor rather than named, to prevent re-identification via a unique category label. |
| **Differential-privacy noise (optional)** | Per-researcher **privacy budget** (ε), consumed per query against a running total tracked alongside `researcher_query_audit`; Laplace/Gaussian noise calibrated to ε added to counts before the k-anonymity check. Budget exhaustion → 429 until the next reset window (monthly, tenant-configurable). Ships as an opt-in per-tenant control (some Enterprise customers may prefer strict k-anonymity + suppression without added noise) rather than mandatory day one — flagged for compliance/DPO review on whether it should instead be mandatory for higher-sensitivity tenants (§4). |
| **Differencing-attack protection** | Three layers, all mandatory: (1) **query audit** — every query's cohort filter set is logged (`researcher_query_audit`) and a background job flags query pairs whose set-difference would isolate a cohort <k; (2) **fixed pre-computed aggregates** — the default/only query surface is a catalog of pre-approved rollup views (not an arbitrary ad-hoc filter builder), which bounds the attack surface structurally; (3) **rate limits** — per-researcher query-rate cap (e.g., N/hour) independent of the DP budget, to slow brute-force differencing regardless of whether DP noise is enabled for that tenant. |

---

## 4. Legal & Compliance

| Area | Requirement | Status |
|---|---|---|
| **CCPA/CPRA "sale/share"** | Providing aggregate tenant/community data to a paying third party is very likely a "sale" or "share" under CPRA's broad definition (monetary or other valuable consideration) even though it's aggregate, not row-level. | **FLAG for legal review** — CPRA's de-identification safe harbor (§1798.140(m)) requires (a) technical safeguards preventing re-identification, (b) business process commitments not to re-identify, and (c) contractual flow-down to the researcher forbidding re-identification attempts. §3's k=25 floor + audit is the technical half; (b) and (c) need counsel-drafted DPA/contract language before the first sale. |
| **GDPR lawful basis** | Selling/sharing personal-data-derived aggregates to a third party needs a lawful basis (Art. 6) — consent or legitimate interest, balanced against data-subject rights. Even aggregate outputs derived from personal data can trigger GDPR obligations on the underlying processing. | **FLAG for legal review** — recommend consent (opt-in, §below) over legitimate-interest balancing test for a commercial resale use case; counsel should confirm. |
| **Do-Not-Sell/Share + consent withdrawal (statutory, every tier)** | Per `critical-rules.md`: these are statutory rights available in **every** license tier, never gated. Users who exercise Do-Not-Sell/Share, and communities/tenants that withdraw consent, are **excluded from researcher datasets** — enforced at the same query-time join as §2.2/§2.4 (a `dns_opt_outs` set intersected before aggregation, independent of tier/license). | Design: reuse existing DSAR/consent infrastructure (`cookie_consent_service.py` precedent) — add a `research_data_opt_out` flag alongside existing consent flags, checked at aggregation time, not at grant time (so a mid-cycle opt-out takes effect on the next query, same fail-closed pattern as §2.4). |
| **Tenant-level opt-in** | A tenant must affirmatively opt in before ANY of its data is eligible for a researcher grant — default is opted-out. Opt-in is a tenant-admin action recorded with a timestamp, separate from and in addition to the Enterprise-license requirement (§2.4) and the per-grant contract (§2.2) — three independent, all must pass. | Design: `tenants.research_data_opt_in_at` (nullable — null means opted out) |
| **Data processing agreements** | Each researcher relationship needs a DPA (or equivalent commercial contract with data-protection terms) referenced by `researcher_tenant_grants.contract_ref` (§2.2) — not optional, the grant row schema makes it structurally required. | **FLAG for legal review** — DPA template authorship is legal work, not engineering. |
| **Region restrictions** | Some tenants/jurisdictions may prohibit cross-border transfer of even aggregate derived data to a researcher outside their region (e.g., Schrems II-style EU data residency concerns). | **FLAG for legal review** — recommend a `researcher_tenant_grants.allowed_regions` constraint (researcher's own registered region must be in the tenant's allowed list) once legal defines the actual restriction set; not designed in detail here pending that input. |
| **Enterprise-gate does not substitute for the above** | §2.4's license gate is a commercial/product control, not a compliance control — it answers "can we build/sell this feature at all," not "may we lawfully sell this tenant's data." Both gates apply independently; passing one never implies the other. | — |

---

## 5. Data Pipeline

```
OTel metrics/traces (per critical-rules.md Observability)  ─┐
hub-api event stream (existing `event.py` blueprint)        ─┼─→  Analytics ingest  ─→  Rollup store  ─→  Researcher query layer (§2-§3)
hub-api DB (communities, community_members, reputation, …)  ─┘         (batch/stream)      (pre-aggregated,
                                                                                              k-anon-ready)
```

| Aspect | Design |
|---|---|
| **Sources** | (1) OTel metrics already emitted per `critical-rules.md` (histograms for load/latency, counters for events) — scraped via the existing OTLP collector, not a second export path; (2) the hub-api event stream (`event.py`/`ingest_sources`) for activity-shaped data; (3) direct hub-api DB tables (`communities`, `community_members`, `reputation_module` scores, bundle install/activation tables) for structural/state data. No new client-side telemetry is introduced — this reuses what's already mandatory. |
| **Separate analytics store** | A dedicated read-optimized store (column-oriented; exact engine TBD — ClickHouse or a Postgres analytics schema with pre-aggregated materialized views, evaluated at implementation time against the 10k-channel/100s-of-tenants sizing below) — never queried directly against the OLTP `hub_api` Postgres primary, to keep researcher query load off the transactional path. |
| **Pre-computed rollups** | Nightly (daily-grain) + hourly (near-real-time admin dashboards) rollup jobs write into the analytics store at the coarsest granularity §3 requires for researchers (week/country) plus finer granularity for admin/vendor/tenant views (§1) — one pipeline, multiple output grains, so the k-anonymity floor is a query-time filter on top of finer rollups already computed for internal use, not two separate pipelines. |
| **Retention** | Raw event data: standard retention window (align with existing `audit_log`/`activity_logs` retention policy, not redefined here). Rollup aggregates: retained longer (e.g., 3 years) since they carry far less re-identification risk once k-anonymized, enabling trend-over-time research views. Exact numbers are a product/compliance decision, flagged alongside §4. |
| **Per-tenant encryption at rest** | Reuses `2026-09-28-tenant-envelope-encryption-design.md`'s existing per-tenant DEK hierarchy — any tenant-attributable row in the rollup store (pre-k-anonymity intermediate aggregates) is encrypted under that tenant's DEK, same AES-256-GCM/AAD-bound mechanism, no second KMS integration. Fully-k-anonymized, cross-tenant-safe output views (what researchers actually query) are the one thing in this store that is *not* tenant-DEK-scoped, since by construction they no longer carry single-tenant attribution below the k floor. |
| **Freshness** | Admin/tenant/community/vendor dashboards (§1): near-real-time via hourly rollups, consistent with existing `docs/bundle-telemetry-capability` expectations for admin log/metric visibility. Researcher dashboards (§1): daily freshness is sufficient given the weekly time-bucket floor (§3) — no need for hourly researcher-facing rollups. |
| **Scale** | Sized for ~300 tenants, ~20,000 communities, ~10,000 channels (matching the envelope-encryption and connections-credentials designs' stated sizing) — rollup tables partition by tenant+week to keep any single aggregation job bounded regardless of total channel count. |

---

## 6. hub-webui Analytics Center

**Location:** `admin/hub_module/frontend/src/pages/` — consolidates the existing role-scattered pages (`AdminAnalytics.jsx`, `AdminMemberAnalytics.jsx`, `TenantDashboard.jsx`, `VendorAnalytics.jsx`, `MyAnalytics.jsx`) into one role-aware `AnalyticsCenter` page family, rather than one bespoke page per role.

| Element | Design |
|---|---|
| **Page layout per role** | One shell component renders a role-appropriate nav (tabs: Overview / Communities / Bundles / Export, minus whichever tabs a role's §1 row excludes) — same shell, different data-fetch scope, not five separate page components. Researcher gets a distinct, simpler shell: Overview + Export only, no Communities/Bundles drill-down tabs (since researchers have no drill-down per §1). |
| **Filters** | Date range (clamped to each role's minimum granularity — day for admins, week for researchers), platform, community (hidden entirely for researcher role — no community-level filter exists for them), bundle (admin/vendor only). Filter state is a UI affordance; the API independently re-derives allowed scope server-side per §1's "never client-trusted" rule. |
| **Exports** | CSV button gated by the `analytics.export:read` scope (researcher) or existing role checks (admin/tenant/community — already CSV-capable today per existing pages); export requests are also audit-logged (§2.3) when the caller is a researcher. |
| **Charts** | Reuse whatever charting library the existing `*.jsx` analytics pages already use; no new charting dependency introduced by this design. |
| **Tracked debt note** | `admin/hub_module/frontend` is plain JS/JSX on React 18.3.1 with no TanStack Query — the TypeScript + TanStack migration is separately tracked debt (per `backend.md`/react standards) and is **not** a prerequisite for this feature; the Analytics Center ships in the current stack and migrates whenever the broader frontend migration reaches it. |

---

## 7. Licensing

| Item | Placement | Rationale |
|---|---|---|
| **Advanced analytics (cross-community trends, retention cohorts, engagement funnels, bad-actor detection, community-health scoring)** | **Enterprise** | Matches `critical-rules.md`'s existing tier table entry ("advanced analytics") and the existing `analytics.community_health`/`analytics.bad_actor_detection`/etc. license-catalog flags already referenced in `blueprints/v1/analytics.py`'s module docstring — no change, this design just confirms placement. |
| **Basic analytics for community/tenant admins (own-scope counts, activity breakdown, growth trends — the existing `/platform/*`/`/community/*` routes)** | **Proposed: Free** for single-community/own-scope views (a community admin seeing their own community's basic stats is core product, not an upsell), **Professional** for tenant-wide cross-community rollups (aggregating multiple communities in one tenant view is where the value step-up is) | Keeps the Free tier "core product, no license-gated functionality" per the tier table, while placing the genuinely multi-community aggregation capability at Professional, consistent with Professional's "established orgs, 50-200 employees" sizing (an org with enough communities to want a tenant-wide rollup is past the individual/startup Free profile). |
| **Researcher role / data resale** | **Enterprise only, both globally and per-tenant** — see §2.4. Not merely "advanced analytics Enterprise," but a hard product gate: the role cannot exist at all outside Enterprise. | Selling data is a distinct commercial product line from in-app analytics, and carries the compliance surface in §4 — Enterprise's existing "advanced analytics, WaddleAI" bracket is the natural home, extended with an explicit license-catalog flag `analytics.researcher_data_program`. |
| **Metering the researcher product commercially** | **Proposed: hybrid metering** — (a) **seat-based** floor: each researcher identity is a seat (§2.3), billed per the standard seat table; (b) **usage-based** overlay on top: query volume and/or tenant-count-in-grant metered separately (e.g., a per-tenant-per-month research-access fee, tracked via `researcher_tenant_grants` row-months), since the commercial value scales with data breadth, not just researcher headcount. Exact price points are a commercial decision outside this design's scope — flagged for product/finance input, not blocking the technical design. | Pure seat-only metering underprices a researcher scoped to 50 tenants the same as one scoped to 1; pure usage-only metering doesn't cover the fixed cost of onboarding/DPA per researcher identity. Hybrid captures both. |

---

## 8. Phased Implementation Plan (agent-sized, ≤30 min/task)

| # | Task | Depends on |
|---|---|---|
| 1 | Migration: `researcher_tenant_grants` table (researcher_user_id, tenant_id, granted_by_user_id, contract_ref NOT NULL, consent_recorded_at NOT NULL, expires_at, revoked_at) | — |
| 2 | Migration: `researcher_query_audit` table (id, researcher_user_id, tenant_ids_requested, tenant_ids_served, query_params JSONB, result_cohort_sizes JSONB, created_at) | — |
| 3 | Migration: `tenants.research_data_opt_in_at` (nullable) + `hub_users`/community-level `research_data_opt_out` flag alongside existing consent flags | — |
| 4 | Register scope bundle `researcher` = `{analytics.aggregate:read, analytics.export:read}` in the scope-bundle table/docs, alongside existing `admin`/`maintainer`/`viewer` bundles | — |
| 5 | Register PostHog flag `waddles.researcher-role` (default OFF) + `tier_requirements["waddles.researcher-role"] = "enterprise"` in `EntitlementClient` config; register license-catalog flag `analytics.researcher_data_program` | 4 |
| 6 | `hub_api`: global-gate check on the grant-creation endpoint (`feature_enabled("waddles.researcher-role", tenant=<platform default>)`) — 402 if platform isn't Enterprise | 5 |
| 7 | `hub_api`: `POST /api/v1/admin/researchers/{user_id}/grants` (global-admin-only, `users:admin` scope) — creates a `researcher_tenant_grants` row, rejects if `contract_ref`/`consent_recorded_at` missing | 1, 6 |
| 8 | `hub_api`: query-time tenant-intersection helper — active grant ∩ per-tenant Enterprise gate (`feature_enabled` per tenant) ∩ tenant opt-in (`research_data_opt_in_at` not null) ∩ not-revoked ∩ not-expired | 3, 5, 7 |
| 9 | `hub_api`: k-anonymity + small-cell-suppression helper (k=25 floor) applied to any researcher-facing aggregate query result | — |
| 10 | `hub_api`: coarse time/geography bucketing helper (week floor, country/region floor) for researcher query params | — |
| 11 | `hub_api`: `researcher_query_audit` write-path — every researcher-facing route writes one audit row post-suppression, including `tenant_ids_requested` vs `tenant_ids_served` divergence | 2, 8 |
| 12 | `hub_api`: per-researcher rate limit (existing rate-limit middleware, new bucket key `researcher:{user_id}`) | 11 |
| 13 | `hub_api`: differencing-attack flag job — background check comparing recent `researcher_query_audit` filter sets for isolating set-differences <k (can start as a scheduled job stub with the detection logic, alerting only, no auto-block) | 11 |
| 14 | `hub_api`: 2-3 pre-computed aggregate view endpoints under `/api/v1/analytics/researcher/*` (e.g., community-category distribution, tenant growth trend) — the fixed-catalog surface from §3, gated by scopes from #4, tenant filter from #8, suppression from #9-10 | 8, 9, 10 |
| 15 | `hub_api`: Do-Not-Sell/opt-out exclusion join wired into #14's query layer (independent of Enterprise gate, applies every tier) | 3, 14 |
| 16 | `hub_api`: CSV export endpoint for researcher pre-approved views, gated on `analytics.export:read`, audit-logged | 11, 14 |
| 17 | Analytics rollup job (nightly, daily-grain writes to the new analytics store) sourcing from OTel metrics + event stream + hub-api DB tables named in §5 | — (parallelizable with 1-16) |
| 18 | Per-tenant encryption wiring for pre-k-anon intermediate rollup rows, reusing the tenant-envelope-encryption DEK from `2026-09-28-tenant-envelope-encryption-design.md` | 17 |
| 19 | hub-webui: `AnalyticsCenter` shared shell component (tabs driven by role, per §6) replacing the entry points of `AdminAnalytics.jsx`/`TenantDashboard.jsx`/`VendorAnalytics.jsx`/`MyAnalytics.jsx` (existing pages' data-fetch logic reused, not rewritten) | — (parallelizable with 1-18) |
| 20 | hub-webui: Researcher shell variant (Overview + Export tabs only, week-floor date picker, no community filter) wired to #14/#16 endpoints | 14, 16, 19 |
| 21 | Tests: unit tests for k-anonymity/suppression helper (#9), bucketing helper (#10), and query-time intersection helper (#8) — table-driven edge cases (exactly k, k-1, revoked mid-window, tenant license lapse mid-window) | 8, 9, 10 |
| 22 | Tests: integration test — global license lapse revokes all researcher queries next-call; per-tenant lapse excludes only that tenant; opt-out excludes a user/community regardless of tier | 6, 8, 15 |
| 23 | Tests: regression test for the differencing-attack flag job (#13) — two queries whose sift isolates <k triggers a flag | 13 |
| 24 | OpenAPI spec update: `hub_api/openapi/v1.yaml` additions for the 5 new researcher routes (#7, #14×N, #16) | 7, 14, 16 |
| 25 | Docs: DPA/contract-ref field documented in admin runbook; flag the 4 legal-review items from §4 as tracked follow-ups (separate GH issues, not blocking this implementation) | 7 |

Tasks 1-4, 9-10, 17, 19 have no interdependencies and can run in parallel; everything gating on the Enterprise check (6, 8, 22) should land before any researcher-facing route (14+) merges, since a query-time gate retrofitted after launch is a much larger diff than building it in from the start.
