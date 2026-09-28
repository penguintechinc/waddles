# Waddles v3 Analytics Center + Researcher Role — Design

**Date:** 2026-09-28
**Status:** Proposed design — no code
**Scope:** `hub_api` (analytics query layer, auth/scopes, license gating), `admin/hub_module/frontend` (hub-webui Analytics Center page), a new analytics rollup store, license-server entitlement wiring.
**Reconciles with:** `flask_core/authz.py`+`tenancy.py` (scope/tenant enforcement, read verbatim for this doc), `hub_api/blueprints/v1/analytics.py`+`services/analytics_service.py` (existing platform/community/self analytics), `docs/bundle-telemetry-capability` branch (admin/vendor telemetry visibility — community/tenant/global admins see logs+metrics per scope, vendors see only where their bundle is activated/running), GH #419 (bundle permission catalog + 3-tier consent flow — same consent-grant shape reused for tenant researcher opt-in), `2026-09-28-tenant-envelope-encryption-design.md` (per-tenant DEK hierarchy reused for the analytics store), `critical-rules.md` (Feature Flags & License Tiers, PII Tokenization, Licensing Model).

**Justin's framing (verbatim):** "our webui is going to need a full analytics center in general with 'view filters' depending on the scope of the visitor (global admin, tenant admin, community admin, vendor, researcher) ... the researcher should be a new role (group of scopes) for third party sponsors and analytics consumers which we can sell data to and scoped to one or many tenants. Researchers should get aggregate metadata about communities and the tenant... not exact sensitive data."

**Normative addendum (Justin, mid-review):** researcher role exists **only on Enterprise-licensed deployments**, gated globally and per-tenant — see §2.4.

---

## 0. Gemini Review Resolution (PR #423, round 1)

Every finding fixed in this revision; the role matrix (§1), the Enterprise-only researcher rule (§2.4), and the legal-review flags (§4) are unchanged from round 1.

| # | Finding | Resolution | Where |
|---|---|---|---|
| F-01 | Query-time opt-out joins on precomputed aggregates leak pre-opt-out data until the next ad-hoc query touches it | Dropped. Researcher aggregates are **regenerated daily from consent-filtered source data** — opt-outs/Do-Not-Sell/erasures are applied at the source, before aggregation, not joined in afterward. ≤24h propagation SLA, with CCPA/GDPR statutory-deadline context and a post-deadline audit. | §4 (Do-Not-Sell row, new Consent Propagation SLA subsection), §5 (pipeline rewrite) |
| F-02 | DP described as optional/per-tenant | DP is now **mandatory, global, enforced at the query engine for every researcher endpoint** — not an opt-in tenant control. One privacy budget per researcher, accounted across all their tenants/queries. No response ever mixes raw and DP-noised numbers. k-anonymity/suppression remain as additional layers on top, not substitutes. | §3 |
| F-03 | Arbitrary/rolling date ranges let a researcher average out noise via overlapping queries | Researcher time dimension is restricted to **static, non-overlapping, calendar-aligned windows** (week or coarser) — no rolling windows, no arbitrary start/end. Noise is added at window boundaries too, not just cell values. | §3 |
| F-04 | Unclear whether one component ever holds all tenants' keys | Explicit key flow added: **no component decrypts more than one tenant's rows at a time.** Aggregation runs inside each tenant's own encryption boundary (per-tenant DEK); only the DP-noised, k-safe *output* aggregate — which by construction carries no single-tenant attribution — crosses into the combined researcher store. | §5 (Key flow) |
| F-05 | Pre-aggregation identifiers could be linkable across tenants | Any user-level identifier touched before aggregation is a **per-tenant HMAC pseudonym** (tenant-specific salt/key) — the same person in two tenants produces two unrelated pseudonyms; nothing links across tenants. | §5 |
| F-06 | No traceability on exported CSVs | Added **per-researcher, per-export watermarking**: a deterministic, researcher-keyed perturbation applied *within* the existing DP noise budget (not an added artifact) so it survives copy/paste, re-aggregation, and numeric transforms — justified against zero-width-character watermarking, which numeric/CSV pipelines strip or never carry. Plus an export audit log. | §6, §2.3 |
| F-07 | Erasure/DSAR requests not tied to the rollup pipeline | An erasure or DSAR request now triggers **source-data purge + recomputation of every affected bucket**, within the same ≤24h SLA as F-01 — same pipeline, same deadline, not a separate process. | §4, §5 |

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
- **Every query audit-logged**: `researcher_query_audit(id, researcher_user_id, tenant_ids_requested, tenant_ids_served, query_params, result_cohort_sizes, created_at)` — populated server-side after the full §3 layer stack (DP → k-anonymity → suppression), never client-supplied. This is both the differencing-attack control (§3) and the commercial usage meter (§7). Exports additionally write to `researcher_export_audit` (§6, F-06), which carries the export-specific watermark seed.
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

**Layer order (each is mandatory, none substitutes for another):** (1) fixed windows + pseudonymization at the source → (2) mandatory global differential privacy at the query engine → (3) k-anonymity + small-cell suppression on the DP output → (4) differencing-attack controls across queries. A response is never anything but the fully-layered result — there is no "raw" or "pre-DP" response path a researcher can reach.

| Control | Rule |
|---|---|
| **Differential privacy — mandatory, global, query-engine-enforced** | DP is **not a per-tenant or per-researcher opt-in** — it is enforced centrally in the query engine for **every** researcher-facing endpoint, unconditionally. Each researcher has **one privacy budget (ε)**, accounted across **all** tenants and **all** queries they run (not one budget per tenant) — the engine is the single choke point, so a researcher can't reset their effective budget by spreading queries across grants. Laplace/Gaussian noise calibrated to the remaining budget is added to every count/rate before k-anonymity is checked. Budget exhaustion → 429 until the next reset window (monthly). **No response ever mixes raw and DP-noised values** — the query engine has no code path that returns an un-noised number to a researcher-scoped caller, structurally (the researcher route handlers call only the DP-wrapped aggregation function, never the raw one admin/tenant/community routes use). |
| **k-anonymity floor (additional layer, on top of DP)** | Minimum cohort size **k=25** for any reported cell, checked against the *noised* value. A cell that would expose <k after DP noise is suppressed (returned as `null`/"insufficient data"), not rounded or approximated. |
| **Small-cell suppression** | Any dimension combination (e.g., tenant × community-category × week) with <k underlying users/communities is dropped from the response entirely, not merged silently into an adjacent bucket the researcher didn't ask for. |
| **Static, non-overlapping windows** | Researcher time dimension is restricted to **fixed, calendar-aligned, non-overlapping windows — week or coarser** (ISO week, month, quarter). No rolling windows (`last 7 days`) and no arbitrary/custom start-end ranges — both let a researcher average independent noise draws across overlapping queries to reconstruct the underlying signal. Noise is added at window **boundaries** as well as cell values, so adjacent windows can't be differenced against each other to isolate a boundary-crossing cohort. |
| **Coarse geography** | Minimum granularity: **country/region** (ISO-3166 country or a coarser platform-defined region). No city, postal code, or IP-derived geolocation ever reaches a researcher response. |
| **No user-level rows** | Every researcher-facing endpoint returns pre-aggregated rows only — no endpoint accepts a `user_id`/`hub_user_id` path or query param, structurally (route table has none), not just by convention. |
| **No UUIDs/free text/rare categories/cross-tenant linkage** | No `row_uuid`, `hub_user_id`, message content, username, or free-text field in any researcher response. Rare categorical values (e.g., a community "type" with only 1-2 instances tenant-wide) are bucketed into an "other" category below the reporting floor. Any identifier used *before* aggregation (source-side, never exposed to the researcher) is a **per-tenant HMAC pseudonym with a tenant-specific salt/key** (§5) — the same person appearing in two tenants' source data produces two unrelated pseudonyms, so nothing is linkable across tenants even inside the pipeline. |
| **Differencing-attack protection** | Three layers, all mandatory, on top of the DP budget above: (1) **query audit** — every query's cohort filter set is logged (`researcher_query_audit`) and a background job flags query pairs whose set-difference would isolate a cohort <k; (2) **fixed pre-computed aggregates over fixed windows** — the only query surface is a catalog of pre-approved rollup views over the static windows above (not an arbitrary ad-hoc filter builder), which bounds the attack surface structurally; (3) **rate limits** — per-researcher query-rate cap (e.g., N/hour) independent of the DP budget, to slow brute-force differencing. |

---

## 4. Legal & Compliance

| Area | Requirement | Status |
|---|---|---|
| **CCPA/CPRA "sale/share"** | Providing aggregate tenant/community data to a paying third party is very likely a "sale" or "share" under CPRA's broad definition (monetary or other valuable consideration) even though it's aggregate, not row-level. | **FLAG for legal review** — CPRA's de-identification safe harbor (§1798.140(m)) requires (a) technical safeguards preventing re-identification, (b) business process commitments not to re-identify, and (c) contractual flow-down to the researcher forbidding re-identification attempts. §3's k=25 floor + audit is the technical half; (b) and (c) need counsel-drafted DPA/contract language before the first sale. |
| **GDPR lawful basis** | Selling/sharing personal-data-derived aggregates to a third party needs a lawful basis (Art. 6) — consent or legitimate interest, balanced against data-subject rights. Even aggregate outputs derived from personal data can trigger GDPR obligations on the underlying processing. | **FLAG for legal review** — recommend consent (opt-in, §below) over legitimate-interest balancing test for a commercial resale use case; counsel should confirm. |
| **Do-Not-Sell/Share + consent withdrawal (statutory, every tier)** | Per `critical-rules.md`: these are statutory rights available in **every** license tier, never gated. Users who exercise Do-Not-Sell/Share, and communities/tenants that withdraw consent, are **excluded from researcher datasets**. | **Revised (F-01): enforced at the SOURCE, not at query time.** Researcher aggregates are never live-queried against current-state data with an opt-out filter bolted on — they are **regenerated daily** from a source extract that has already excluded opted-out users/communities *before* any aggregation runs. There is no query-time join to defeat and no window where a stale cached aggregate still reflects an opted-out user, other than the SLA below. See Consent Propagation SLA. |
| **Tenant-level opt-in** | A tenant must affirmatively opt in before ANY of its data is eligible for a researcher grant — default is opted-out. Opt-in is a tenant-admin action recorded with a timestamp, separate from and in addition to the Enterprise-license requirement (§2.4) and the per-grant contract (§2.2) — three independent, all must pass. | Design: `tenants.research_data_opt_in_at` (nullable — null means opted out); also enforced at the source-extract step (opted-out tenants never enter the daily extract), not as a downstream filter. |
| **Data processing agreements** | Each researcher relationship needs a DPA (or equivalent commercial contract with data-protection terms) referenced by `researcher_tenant_grants.contract_ref` (§2.2) — not optional, the grant row schema makes it structurally required. | **FLAG for legal review** — DPA template authorship is legal work, not engineering. |
| **Region restrictions** | Some tenants/jurisdictions may prohibit cross-border transfer of even aggregate derived data to a researcher outside their region (e.g., Schrems II-style EU data residency concerns). | **FLAG for legal review** — recommend a `researcher_tenant_grants.allowed_regions` constraint (researcher's own registered region must be in the tenant's allowed list) once legal defines the actual restriction set; not designed in detail here pending that input. |
| **Enterprise-gate does not substitute for the above** | §2.4's license gate is a commercial/product control, not a compliance control — it answers "can we build/sell this feature at all," not "may we lawfully sell this tenant's data." Both gates apply independently; passing one never implies the other. | — |

### Consent Propagation SLA (F-01, F-07)

**Rule:** any opt-out, Do-Not-Sell/Share election, consent withdrawal, erasure, or DSAR deletion is reflected in the researcher-facing aggregates within **≤24 hours**, via the same mechanism: the daily source extract excludes it, and every rollup bucket the excluded record could have contributed to is recomputed from that clean extract — not patched or subtracted after the fact.

| Regime | Statutory deadline | How this design compares |
|---|---|---|
| CCPA/CPRA | Opt-out requests: honor "as soon as feasible," service providers must be instructed within a reasonable time; deletion/access DSARs: **45 calendar days** (extendable), but opt-out propagation to downstream recipients is expected promptly, not at the 45-day ceiling | ≤24h is well inside any CCPA-referenced window — chosen as an engineering SLA, not because the statute demands sub-day propagation, since a daily batch pipeline is what the analytics architecture (§5) naturally supports |
| GDPR | Erasure/rectification: "without undue delay," **Art. 12(3)** default **one month**, extendable | ≤24h substantially exceeds "without undue delay" — flagged as a design choice, not a floor; counsel should confirm no regime requires *faster* than 24h for this specific derived-aggregate use case (§4 legal flags) |

- **Mechanism**, detailed in §5: the daily rollup job's first step is the source extract (per-tenant, inside that tenant's encryption boundary), which applies opt-out/DNS/erasure state as a `WHERE NOT IN (excluded_ids)` filter *before* any pseudonymization or aggregation touches the row. An opt-out recorded at any point during day N is reflected in the extract that runs at the start of day N+1 — worst case just under 24h from election to reflection.
- **Erasure/DSAR-specific action (F-07):** an erasure or DSAR-deletion request additionally triggers (a) a purge of the affected record from the source extract's input tables (not just exclusion from the next extract — the underlying source row itself is deleted/anonymized per the product's existing DSAR flow) and (b) forced recomputation of every rollup bucket that record could have contributed to, within the same ≤24h SLA — this is the same daily job, not a parallel deletion pipeline, so there is only one place to verify correctness.
- **Post-deadline audit:** a scheduled job runs after every 24h SLA window and verifies, for each opt-out/erasure recorded in the prior 24-48h, that no researcher-facing bucket still reflects the excluded record (re-derives the expected bucket values from the current clean extract and diffs against what was published). A verification failure pages on-call and blocks the next day's rollup publish until resolved — the SLA is asserted, not assumed.

---

## 5. Data Pipeline

**Two pipelines share sources but diverge at the tenant boundary — admin/tenant/community/vendor rollups (§1, not privacy-floor-constrained) vs. the researcher pipeline (privacy-floor-constrained, this section's focus).** The researcher pipeline regenerates fully from scratch daily; it never mutates or patches a previous day's output.

```
Per tenant, inside that tenant's encryption boundary (daily, one run per tenant):

  OTel metrics/traces ─┐
  hub-api event stream ─┼─→ Source extract  ─→ Consent filter   ─→ Per-tenant       ─→ Per-tenant DP-noised,
  hub-api DB (this      ┘   (this tenant       (F-01/F-07:          HMAC pseudonymize    k-safe aggregate
  tenant's rows only)       only, decrypted     opt-out/DNS/         + aggregate          ("safe to leave the
                            with THIS tenant's  erasure excluded     (F-05: tenant-        tenant boundary")
                            DEK only)           BEFORE aggregation)  specific salt)
                                                                                 │
                                                                                 ▼
                                                          Combined researcher store (§3-gated query engine:
                                                          mandatory DP budget, k-anonymity, static windows,
                                                          fixed pre-computed views only)
```

| Aspect | Design |
|---|---|
| **Sources** | (1) OTel metrics already emitted per `critical-rules.md` (histograms for load/latency, counters for events) — scraped via the existing OTLP collector, not a second export path; (2) the hub-api event stream (`event.py`/`ingest_sources`) for activity-shaped data; (3) direct hub-api DB tables (`communities`, `community_members`, `reputation_module` scores, bundle install/activation tables) for structural/state data. No new client-side telemetry is introduced — this reuses what's already mandatory. |
| **Key flow — no component ever holds all tenants' keys (F-04)** | The per-tenant extract/pseudonymize/aggregate step runs as N independent per-tenant jobs (or a single job that acquires and releases one tenant's DEK per iteration, never more than one at a time), reusing `2026-09-28-tenant-envelope-encryption-design.md`'s existing per-tenant DEK hierarchy — the same process that already decrypts that tenant's rows for its own admin dashboards, not a new decrypt path. **Only the output of that step — a DP-noised, k-safety-checked aggregate row that by construction carries no single-tenant-identifying content below the reporting floor — is written to the combined researcher store.** The combined store's writer process never requests or holds a second tenant's DEK while processing the first tenant's data; there is structurally no point in the pipeline where row-level data from two tenants is decrypted concurrently in the same process/memory space. |
| **Pseudonymization before aggregation (F-05)** | Any user-level identifier the per-tenant aggregation step touches (e.g., to count distinct active members) is replaced with `HMAC(tenant_specific_salt, user_id)` before the aggregation logic ever sees it — the salt is derived from that tenant's own DEK (HKDF, same derivation pattern as the envelope-encryption design's blind-index subkey), so the same underlying user produces an unrelated pseudonym in every other tenant. No pseudonym, salt, or raw identifier crosses into the combined researcher store — only the post-aggregation counts do. |
| **Daily regeneration, not incremental patching (F-01/F-07)** | Researcher-facing buckets are **fully recomputed from the current consent-filtered extract every day**, not incrementally updated from the prior day's output plus a delta — this is what makes source-side opt-out/erasure filtering sufficient on its own (§4 SLA): there is no stale prior aggregate to separately track down and correct, because every day's output supersedes the prior day's in full. |
| **Separate analytics store** | A dedicated read-optimized combined store (column-oriented; exact engine TBD — ClickHouse or a Postgres analytics schema with pre-aggregated materialized views, evaluated at implementation time against the 10k-channel/100s-of-tenants sizing below) — never queried directly against the OLTP `hub_api` Postgres primary, to keep researcher query load off the transactional path. Only holds the cross-tenant-safe, post-DP, post-k-anonymity output described above; it never holds a per-tenant intermediate aggregate at rest. |
| **Admin/tenant/community/vendor rollups** | Separate, finer-grained (hourly) rollups for §1's non-researcher roles continue as today, staying inside each tenant's own boundary/DEK (no cross-tenant combination step) — unaffected by the researcher-pipeline changes in this revision. |
| **Retention** | Raw event data / per-tenant intermediate extracts: not retained beyond the daily job's run — regenerated fresh each day, so there is nothing older to retain or separately purge (reduces the erasure/DSAR surface, F-07). Combined researcher-store aggregates: retained longer (e.g., 3 years) since they carry no single-tenant attribution below the k floor once published. Exact numbers are a product/compliance decision, flagged alongside §4. |
| **Freshness** | Admin/tenant/community/vendor dashboards (§1): near-real-time via hourly rollups, consistent with existing `docs/bundle-telemetry-capability` expectations for admin log/metric visibility. Researcher dashboards (§1): daily by design (§3's static-window floor doesn't benefit from sub-day freshness, and daily regeneration is what makes the consent-propagation SLA in §4 correct-by-construction rather than query-time-patched). |
| **Scale** | Sized for ~300 tenants, ~20,000 communities, ~10,000 channels (matching the envelope-encryption and connections-credentials designs' stated sizing) — the per-tenant extract step is naturally parallelizable across tenants (each is independent, bounded by that one tenant's data volume), and the daily full-recompute cost is checked against this sizing at implementation time to confirm it fits the batch window. |

---

## 6. hub-webui Analytics Center

**Location:** `admin/hub_module/frontend/src/pages/` — consolidates the existing role-scattered pages (`AdminAnalytics.jsx`, `AdminMemberAnalytics.jsx`, `TenantDashboard.jsx`, `VendorAnalytics.jsx`, `MyAnalytics.jsx`) into one role-aware `AnalyticsCenter` page family, rather than one bespoke page per role.

| Element | Design |
|---|---|
| **Page layout per role** | One shell component renders a role-appropriate nav (tabs: Overview / Communities / Bundles / Export, minus whichever tabs a role's §1 row excludes) — same shell, different data-fetch scope, not five separate page components. Researcher gets a distinct, simpler shell: Overview + Export only, no Communities/Bundles drill-down tabs (since researchers have no drill-down per §1). |
| **Filters** | Date range (clamped to each role's minimum granularity — day for admins, week for researchers), platform, community (hidden entirely for researcher role — no community-level filter exists for them), bundle (admin/vendor only). Filter state is a UI affordance; the API independently re-derives allowed scope server-side per §1's "never client-trusted" rule. |
| **Exports** | CSV button gated by the `analytics.export:read` scope (researcher) or existing role checks (admin/tenant/community — already CSV-capable today per existing pages). Every researcher export is watermarked and audit-logged — see Export Watermarking (F-06) below. |

### Export Watermarking (F-06)

Every researcher CSV export carries a **traceable, per-researcher, per-export fingerprint**, so a leaked or resold export can be traced back to the researcher and export event that produced it.

| Aspect | Design |
|---|---|
| **Mechanism** | A deterministic, researcher-keyed perturbation applied **within the DP noise already being added** (§3) — for each exported cell, the noise draw is seeded from `HMAC(researcher_key, export_id, cell_coordinates)` instead of a pure-random draw, so the specific pattern of noise values across the export is unique to that (researcher, export) pair while each individual value still lands inside the same DP-calibrated distribution as an unwatermarked query. Verifying a suspect CSV means re-deriving the expected noise pattern for a given researcher/export_id and checking the match — no separate visible mark is added anywhere. |
| **Why this instead of zero-width characters** | Zero-width/invisible-Unicode watermarking (common for text documents) doesn't survive a **numeric** CSV export: spreadsheet tools, `pandas.read_csv`, copy-into-another-numeric-pipeline, and re-aggregation all normalize or strip non-numeric characters from a numeric column, destroying the mark. A perturbation embedded in the actual noise value survives any transform that preserves the number itself — including re-aggregation into a derived report — because the mark *is* the number, not an attachment to it. It also costs zero additional privacy budget, since the watermark reuses noise the DP layer was already adding. |
| **Export audit log** | Every export writes a row to `researcher_export_audit(id, researcher_user_id, export_id, view_name, tenant_ids_included, requested_at, row_count)` — separate from (but joinable with) `researcher_query_audit` (§2.3), since an export can bundle multiple underlying queries into one file. `export_id` is the same value used as the watermark seed, so a recovered CSV's fingerprint maps directly to one audit row. |
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
| 2 | Migration: `researcher_query_audit` table (id, researcher_user_id, tenant_ids_requested, tenant_ids_served, query_params JSONB, result_cohort_sizes JSONB, created_at) + `researcher_export_audit` table (id, researcher_user_id, export_id, view_name, tenant_ids_included, requested_at, row_count) | — |
| 3 | Migration: `tenants.research_data_opt_in_at` (nullable) + `hub_users`/community-level `research_data_opt_out` flag alongside existing consent flags | — |
| 4 | Register scope bundle `researcher` = `{analytics.aggregate:read, analytics.export:read}` in the scope-bundle table/docs, alongside existing `admin`/`maintainer`/`viewer` bundles | — |
| 5 | Register PostHog flag `waddles.researcher-role` (default OFF) + `tier_requirements["waddles.researcher-role"] = "enterprise"` in `EntitlementClient` config; register license-catalog flag `analytics.researcher_data_program` | 4 |
| 6 | `hub_api`: global-gate check on the grant-creation endpoint (`feature_enabled("waddles.researcher-role", tenant=<platform default>)`) — 402 if platform isn't Enterprise | 5 |
| 7 | `hub_api`: `POST /api/v1/admin/researchers/{user_id}/grants` (global-admin-only, `users:admin` scope) — creates a `researcher_tenant_grants` row, rejects if `contract_ref`/`consent_recorded_at` missing | 1, 6 |
| 8 | `hub_api`: per-tenant source-extract job — decrypts only that tenant's rows (its DEK), filters out `research_data_opt_out`/DNS/erased records BEFORE any aggregation (F-01), writes to a per-tenant scratch table that never persists past the daily run | 3 |
| 9 | `hub_api`: per-tenant HMAC pseudonymization step (F-05) — `HMAC(tenant_dek_derived_salt, user_id)` applied to any identifier the extract step touches, before aggregation | 8 |
| 10 | `hub_api`: per-tenant aggregation step producing candidate rollup rows (pre-DP, pre-k-check) — runs entirely inside step 8's per-tenant boundary, never holds a second tenant's decrypted rows concurrently (F-04) | 9 |
| 11 | `hub_api`: mandatory global DP module — single query-engine choke point, one privacy-budget ledger per researcher across all tenants/queries, Laplace/Gaussian noise applied to every candidate cell; no code path bypasses it for a researcher-scoped call (F-02) | — |
| 12 | `hub_api`: k-anonymity + small-cell-suppression helper (k=25 floor), applied to DP-noised values only (never pre-DP) | 11 |
| 13 | `hub_api`: static/non-overlapping window enforcement (week-or-coarser, calendar-aligned) on all researcher query params — rejects rolling/arbitrary ranges; boundary noise applied here too (F-03) | 11 |
| 14 | `hub_api`: combined researcher store writer — takes step 10's per-tenant candidates through steps 11-13, writes only the fully-layered output; confirms (test-covered) no intermediate per-tenant row is ever persisted to the combined store | 10, 11, 12, 13 |
| 15 | `hub_api`: query-time tenant-eligibility helper — active grant ∩ per-tenant Enterprise gate (`feature_enabled` per tenant) ∩ tenant opt-in (`research_data_opt_in_at` not null) ∩ not-revoked ∩ not-expired — filters which of the combined store's already-safe rows a given researcher may see | 3, 5, 7 |
| 16 | `hub_api`: `researcher_query_audit` write-path — every researcher-facing route writes one audit row post-suppression, including `tenant_ids_requested` vs `tenant_ids_served` divergence | 2, 15 |
| 17 | `hub_api`: per-researcher rate limit (existing rate-limit middleware, new bucket key `researcher:{user_id}`) | 16 |
| 18 | `hub_api`: differencing-attack flag job — background check comparing recent `researcher_query_audit` filter sets for isolating set-differences <k (can start as a scheduled job stub with the detection logic, alerting only, no auto-block) | 16 |
| 19 | `hub_api`: 2-3 pre-computed aggregate view endpoints under `/api/v1/analytics/researcher/*` (e.g., community-category distribution, tenant growth trend) over the fixed weekly windows — gated by scopes from #4, tenant filter from #15, reading only from the combined store (#14) | 14, 15 |
| 20 | `hub_api`: export endpoint with per-researcher/per-export watermarking (F-06 — noise reseeded from `HMAC(researcher_key, export_id, cell_coords)`), gated on `analytics.export:read`, writes `researcher_export_audit` | 2, 19 |
| 21 | `hub_api`: consent-propagation post-SLA audit job (F-01/F-07) — re-derives expected bucket values from the current clean extract, diffs against what's published, pages on-call and blocks next publish on mismatch | 8, 14 |
| 22 | `hub_api`: erasure/DSAR hook — a deletion request triggers immediate source-row purge (existing DSAR flow) so it's excluded from the next daily extract (step 8), same ≤24h SLA as opt-out (F-07) | 8 |
| 23 | Admin/tenant/community/vendor rollup job (hourly, unaffected by the researcher pipeline) — unchanged from today, documented here for completeness | — (parallelizable with 1-22) |
| 24 | hub-webui: `AnalyticsCenter` shared shell component (tabs driven by role, per §6) replacing the entry points of `AdminAnalytics.jsx`/`TenantDashboard.jsx`/`VendorAnalytics.jsx`/`MyAnalytics.jsx` (existing pages' data-fetch logic reused, not rewritten) | — (parallelizable with 1-23) |
| 25 | hub-webui: Researcher shell variant (Overview + Export tabs only, static-week picker, no community filter, no custom date range) wired to #19/#20 endpoints | 19, 20, 24 |
| 26 | Tests: unit tests for the DP module (#11), k-anonymity/suppression (#12), window enforcement (#13), and tenant-eligibility helper (#15) — table-driven edge cases (exactly k, k-1, budget exhausted, rolling-range rejected, revoked mid-window, tenant license lapse mid-window) | 11, 12, 13, 15 |
| 27 | Tests: integration test — opt-out/erasure recorded day N is absent from researcher output day N+1 (F-01/F-07); global license lapse revokes all researcher queries next-call; per-tenant lapse excludes only that tenant | 8, 15, 21, 22 |
| 28 | Tests: regression test for the differencing-attack flag job (#18) and for watermark round-trip (export → recovered fingerprint maps to the correct `researcher_export_audit` row) | 18, 20 |
| 29 | OpenAPI spec update: `hub_api/openapi/v1.yaml` additions for the new researcher routes (#7, #19×N, #20) | 7, 19, 20 |
| 30 | Docs: DPA/contract-ref field documented in admin runbook; flag the 4 legal-review items from §4 as tracked follow-ups (separate GH issues, not blocking this implementation) | 7 |

Tasks 1-4, 11-13, 23-24 have no interdependencies and can run in parallel. Everything in the per-tenant boundary chain (8→9→10→14) and the mandatory-DP chain (11→12/13→14) must land before any researcher-facing route (19+) merges — retrofitting source-side consent filtering or mandatory DP after a query-time-only version ships is a much larger diff (and a live compliance gap) than building both in from the start.
