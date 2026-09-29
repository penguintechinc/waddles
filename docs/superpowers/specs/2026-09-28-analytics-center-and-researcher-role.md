# Waddles v3 Analytics Center + Researcher Role — Design

**Date:** 2026-09-28
**Status:** Proposed design — no code
**Scope:** `hub_api` (analytics query layer, auth/scopes, license gating), `admin/hub_module/frontend` (hub-webui Analytics Center page), a new analytics rollup store, license-server entitlement wiring.
**Reconciles with:** `flask_core/authz.py`+`tenancy.py` (scope/tenant enforcement, read verbatim for this doc), `hub_api/blueprints/v1/analytics.py`+`services/analytics_service.py` (existing platform/community/self analytics), `docs/bundle-telemetry-capability` branch (admin/vendor telemetry visibility — community/tenant/global admins see logs+metrics per scope, vendors see only where their bundle is activated/running), GH #419 (bundle permission catalog + 3-tier consent flow — same consent-grant shape reused for tenant researcher opt-in), `2026-09-28-tenant-envelope-encryption-design.md` (per-tenant DEK hierarchy reused for the analytics store), `critical-rules.md` (Feature Flags & License Tiers, PII Tokenization, Licensing Model).

**Justin's framing (verbatim):** "our webui is going to need a full analytics center in general with 'view filters' depending on the scope of the visitor (global admin, tenant admin, community admin, vendor, researcher) ... the researcher should be a new role (group of scopes) for third party sponsors and analytics consumers which we can sell data to and scoped to one or many tenants. Researchers should get aggregate metadata about communities and the tenant... not exact sensitive data."

**Normative addendum (Justin, mid-review):** researcher role exists **only on Enterprise-licensed deployments**, gated globally and per-tenant — see §2.4.

---

## 0. Gemini Review Resolution (PR #423)

Round 1 findings F-01–F-07: **RESOLVED**. Round 2 came back **PASS-WITH-CONDITIONS**, adding N-01/N-02 as normative requirements, which this revision folds in. The role matrix (§1), the Enterprise-only researcher rule (§2.4), and the legal-review flags (§4) are unchanged across both rounds.

| # | Finding | Resolution | Where |
|---|---|---|---|
| F-01 | Query-time opt-out joins on precomputed aggregates leak pre-opt-out data until the next ad-hoc query touches it | Dropped. Researcher aggregates are **regenerated daily from consent-filtered source data** — opt-outs/Do-Not-Sell/erasures are applied at the source, before aggregation, not joined in afterward. ≤24h propagation SLA, with CCPA/GDPR statutory-deadline context and a post-deadline audit. | §4 (Do-Not-Sell row, new Consent Propagation SLA subsection), §5 (pipeline rewrite) |
| F-02 | DP described as optional/per-tenant | DP is **mandatory, global, enforced at the query engine for every researcher endpoint** — not an opt-in tenant control. One privacy budget per researcher, accounted across all their tenants/queries. No response ever mixes raw and DP-noised numbers. k-anonymity/suppression remain as additional layers on top, not substitutes. Refined further by N-01/N-02 below (order of operations and determinism). | §3 |
| F-03 | Arbitrary/rolling date ranges let a researcher average out noise via overlapping queries | Researcher time dimension is restricted to **static, non-overlapping, calendar-aligned windows** (week or coarser) — no rolling windows, no arbitrary start/end. Noise is added at window boundaries too, not just cell values. | §3 |
| F-04 | Unclear whether one component ever holds all tenants' keys | Explicit key flow added: **no component decrypts more than one tenant's rows at a time.** Superseded by N-02's more precise boundary (below): each tenant emits only exact aggregate numbers, never row data or keys, to a separate restricted service. | §5 (Key flow), refined by N-02 |
| F-05 | Pre-aggregation identifiers could be linkable across tenants | Any user-level identifier touched *inside* a tenant's own boundary (to compute a distinct-count) is a **per-tenant HMAC pseudonym** (tenant-specific salt/key) that never itself leaves that boundary — only the resulting count/sum does (N-02 makes this explicit: "never rows or pseudonyms" cross the tenant boundary). | §5 |
| F-06 | No traceability on exported CSVs | Added **per-researcher, per-export watermarking** — mechanism revised by N-01 (below) to live entirely outside the DP noise, so it can never be averaged away. Plus an export audit log. | §6, §2.3 |
| F-07 | Erasure/DSAR requests not tied to the rollup pipeline | An erasure or DSAR request now triggers **source-data purge + recomputation of every affected bucket**, within the same ≤24h SLA as F-01 — same pipeline, same deadline, not a separate process. | §4, §5 |
| N-01 | DP noise must not be fresh/random per call, or a researcher can repeat/re-export a query and average the noise away; watermarking was described as living *inside* the DP noise draw, which is the same flaw | **DP noise is now deterministic** per `(researcher, query shape, bucket)`, derived from a fixed, server-held seed — identical repeated or re-exported queries return bit-identical noisy values, so repetition gains a researcher nothing. **Watermarking is now fully separate from and outside the DP mechanism**: per-researcher row-order permutation, a rounding-direction pattern applied to already-noised values, and a signed export manifest, alongside the existing export audit log — none of these add randomness that could be averaged away. | §3 (DP determinism), §6 (watermarking rewrite) |
| N-02 | The multi-tenant boundary wasn't precise enough about what crosses it and when suppression/noise apply | **Redefined, three-stage boundary:** (1) each tenant computes **exact** aggregates (counts/sums per static bucket only — never rows, never pseudonyms) entirely inside its own encryption boundary; (2) a **restricted researcher-aggregation service**, which holds no tenant DEKs and no row-level data, combines those exact per-tenant numbers into cross-tenant cells, applies k-suppression to the **final combined cell** (not a per-tenant pre-check), and enforces a **minimum per-tenant-contribution threshold** (suppress if one tenant supplies >80% of a cell, or if fewer than N tenants contribute to a cross-tenant cell) to block tenant-level inference; (3) DP noise is added **once, at release**, by the query engine (deterministically, per N-01). | §5 (pipeline + key flow rewrite), §3 (layer order) |

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
- **Every query audit-logged**: `researcher_query_audit(id, researcher_user_id, tenant_ids_requested, tenant_ids_served, query_params, result_cohort_sizes, created_at)` — populated server-side after the full §3 layer stack (exact per-tenant aggregation → cross-tenant combination → k-anonymity/anti-dominance suppression → deterministic DP release), never client-supplied. This is both the differencing-attack control (§3) and the commercial usage meter (§7). Exports additionally write to `researcher_export_audit` (§6, F-06/N-01), which carries the export's watermark identifiers (row-order/rounding-pattern seed and signed manifest).
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

**Layer order (each is mandatory, none substitutes for another; N-02 fixes this order precisely):**

1. **Per-tenant exact aggregation** — inside each tenant's own encryption boundary: fixed windows, source-side consent filtering (§4), internal-only pseudonymization (§5). Output: exact counts/sums per static bucket, nothing else.
2. **Cross-tenant combination** — the restricted researcher-aggregation service (§5) combines exact per-tenant numbers into cross-tenant cells. Still exact, still no noise.
3. **k-anonymity + minimum-per-tenant-contribution suppression** — applied to the **final combined cell**, not any per-tenant pre-check.
4. **Deterministic DP noise, added once, at release** — by the query engine, per (researcher, query shape, bucket) (N-01).
5. **Watermarking** — applied to the exported artifact, entirely outside and after the DP step (N-01).
6. **Differencing-attack controls** — across queries, over time.

A response a researcher receives is never anything but the fully-layered, step-4 output — there is no "raw," "pre-suppression," or "pre-DP" response path reachable by a researcher-scoped call.

| Control | Rule |
|---|---|
| **Exact aggregation only inside the tenant boundary** | Each tenant's per-bucket aggregation produces **counts and sums only** — never a row, never a pseudonym, never anything below full-bucket granularity — and this exact output is the only thing that leaves that tenant's encryption boundary (N-02; full detail in §5). |
| **k-anonymity + minimum-per-tenant-contribution floor (on the FINAL combined cell)** | Minimum cohort size **k=25**, checked against the combined (not per-tenant) cell. Additionally, a cell is suppressed if **one tenant supplies >80% of its mass** (single-tenant dominance would let a researcher infer that tenant's near-exact number from a "cross-tenant" cell) **or if fewer than a minimum number of tenants (N, tenant-configurable, ≥3 by default) contribute** to it at all. Suppressed cells return `null`/"insufficient data," never a rounded or approximated value. |
| **Small-cell suppression** | Any dimension combination (e.g., community-category × week) with <k underlying communities across all contributing tenants is dropped from the response entirely, not merged silently into an adjacent bucket the researcher didn't ask for. |
| **Deterministic differential privacy — mandatory, global, applied once at release** | DP is **not a per-tenant or per-researcher opt-in** — the query engine is the single choke point for **every** researcher-facing endpoint, unconditionally, and it is the *only* place noise is added (never per-tenant, never per-combination-step — see the layer order above). Each researcher has **one privacy budget (ε)**, accounted across **all** tenants and **all** queries. **Noise is deterministic**, derived from a fixed, server-held seed keyed on `(researcher_id, query_shape_hash, bucket_id)` — an identical repeated or re-exported query returns a bit-identical noisy value every time, so a researcher gains nothing by repetition or re-export (N-01). Budget accounting follows from this: a distinct `(query shape, bucket)` tuple a researcher has not yet observed consumes a slice of budget on first release; every subsequent identical request re-derives the same value from the seed at **zero additional budget cost**, since no new information is revealed. Budget exhaustion on a genuinely new tuple → 429 until the next reset window (monthly). **No response ever mixes raw and DP-noised values** — the query engine has no code path that returns an un-noised (or un-suppressed) number to a researcher-scoped caller, structurally. |
| **Static, non-overlapping windows** | Researcher time dimension is restricted to **fixed, calendar-aligned, non-overlapping windows — week or coarser** (ISO week, month, quarter). No rolling windows (`last 7 days`) and no arbitrary/custom start-end ranges — both let a researcher combine overlapping queries to reconstruct the underlying signal, and determinism alone (above) doesn't prevent that, since two *different* query shapes are two different deterministic outputs. Noise is added at window **boundaries** as well as cell values, so adjacent windows can't be differenced against each other to isolate a boundary-crossing cohort. |
| **Coarse geography** | Minimum granularity: **country/region** (ISO-3166 country or a coarser platform-defined region). No city, postal code, or IP-derived geolocation ever reaches a researcher response. |
| **No user-level rows** | Every researcher-facing endpoint returns pre-aggregated rows only — no endpoint accepts a `user_id`/`hub_user_id` path or query param, structurally (route table has none), not just by convention. |
| **No UUIDs/free text/rare categories/cross-tenant linkage** | No `row_uuid`, `hub_user_id`, message content, username, or free-text field in any researcher response. Rare categorical values (e.g., a community "type" with only 1-2 instances tenant-wide) are bucketed into an "other" category below the reporting floor. Any identifier used *before* aggregation (source-side, inside one tenant's boundary) is a **per-tenant HMAC pseudonym with a tenant-specific salt/key** (§5) that never itself leaves that boundary — the same person appearing in two tenants' source data produces two unrelated pseudonyms, and neither pseudonym crosses into the aggregation service, only the resulting exact count/sum does. |
| **Differencing-attack protection** | Three layers, all mandatory, on top of everything above: (1) **query audit** — every query's cohort filter set is logged (`researcher_query_audit`) and a background job flags query pairs whose set-difference would isolate a cohort <k; (2) **fixed pre-computed aggregates over fixed windows** — the only query surface is a catalog of pre-approved rollup views over the static windows above (not an arbitrary ad-hoc filter builder), which bounds the attack surface structurally; (3) **rate limits** — per-researcher query-rate cap (e.g., N/hour) independent of the DP budget, to slow brute-force differencing between *distinct* query shapes (determinism, above, already closes the same-shape repetition/averaging attack — this layer covers the cross-shape case it doesn't). |

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

**Three-stage boundary (N-02) — each stage is a hard trust/permission cliff, not just a logical grouping:**

```
STAGE 1 — Per tenant, inside that tenant's own encryption boundary (daily, one run per tenant):

  OTel metrics/traces ─┐
  hub-api event stream ─┼─→ Source extract  ─→ Consent filter  ─→ Internal-only    ─→ EXACT aggregate
  hub-api DB (this      ┘   (this tenant       (F-01/F-07:         HMAC pseudonymize   (counts/sums per
  tenant's rows only)       only, decrypted     opt-out/DNS/        for distinct-       static bucket ONLY
                            with THIS tenant's  erasure excluded    count purposes      — never a row,
                            DEK only)           BEFORE aggregation) (F-05, tenant       never a pseudonym,
                                                                     salt, never          crosses the
                                                                     exported)            boundary below)
                                                                                 │
                                                                                 ▼  (only exact numbers cross)
STAGE 2 — Restricted researcher-aggregation service (holds NO tenant DEKs, NO row-level data, ever):

  Combine exact per-tenant   ─→  k-anonymity + minimum-per-tenant-   ─→  Suppressed-safe EXACT
  aggregates into cross-          contribution suppression on the        combined cells written to
  tenant cells (still exact,      FINAL combined cell (k=25; reject       the Combined Researcher
  no noise yet)                   if 1 tenant >80% of mass, or <N          Store (still exact, not
                                   tenants contribute) — N-02              yet DP-noised)
                                                                                 │
                                                                                 ▼
STAGE 3 — Query engine, at researcher request time (§3):

  Read suppressed-safe combined cell(s)  ─→  Deterministic DP noise, added ONCE,     ─→  Response to
                                              seeded by (researcher_id, query_shape_       researcher
                                              hash, bucket_id) (N-01)                      (§2.3 audit)
```

| Aspect | Design |
|---|---|
| **Sources** | (1) OTel metrics already emitted per `critical-rules.md` (histograms for load/latency, counters for events) — scraped via the existing OTLP collector, not a second export path; (2) the hub-api event stream (`event.py`/`ingest_sources`) for activity-shaped data; (3) direct hub-api DB tables (`communities`, `community_members`, `reputation_module` scores, bundle install/activation tables) for structural/state data. No new client-side telemetry is introduced — this reuses what's already mandatory. |
| **Key flow — no component ever holds all tenants' keys (F-04, redefined by N-02)** | **Stage 1** (per-tenant exact aggregation) runs as N independent per-tenant jobs, each acquiring and releasing exactly one tenant's DEK, reusing `2026-09-28-tenant-envelope-encryption-design.md`'s existing per-tenant DEK hierarchy — the same process that already decrypts that tenant's rows for its own admin dashboards, not a new decrypt path. Its output crossing into Stage 2 is **numbers only** — exact counts/sums per bucket, never a row, a pseudonym, or a key. **Stage 2 (the restricted researcher-aggregation service) is a structurally separate service that holds no tenant DEK, ever, and never receives row-level data** — its inputs are exclusively the numeric aggregates Stage 1 emits, so even a full compromise of Stage 2 exposes no tenant's raw rows or keys, only already-exact-but-not-yet-privacy-protected combined numbers. **Stage 3** (the query engine) reads only Stage 2's output (already k-suppressed, dominance-checked, and exact) and never touches tenant DEKs or row data at all — its only job is the deterministic DP release (§3). No single component spans more than one stage's trust boundary. |
| **Pseudonymization before aggregation (F-05)** | Any user-level identifier Stage 1's aggregation step touches (e.g., to count distinct active members) is replaced with `HMAC(tenant_specific_salt, user_id)` before the aggregation logic ever sees it — the salt is derived from that tenant's own DEK (HKDF, same derivation pattern as the envelope-encryption design's blind-index subkey), so the same underlying user produces an unrelated pseudonym in every other tenant. Per N-02: **the pseudonym is used only to compute the exact distinct-count inside Stage 1 and is discarded there** — neither the pseudonym, the salt, nor any raw identifier ever appears in Stage 1's output to Stage 2, only the resulting count. |
| **k-anonymity and anti-dominance apply once, on the combined cell (N-02)** | Stage 1 never makes a suppression decision — it has no visibility into other tenants' contributions to a would-be combined cell, so it cannot correctly judge k-anonymity or dominance on its own. That judgment happens exactly once, in Stage 2, after combination, against the true final cell (§3). |
| **Daily regeneration, not incremental patching (F-01/F-07)** | Researcher-facing buckets are **fully recomputed from the current consent-filtered extract every day** — all three stages re-run in full — not incrementally updated from the prior day's output plus a delta. This is what makes source-side opt-out/erasure filtering sufficient on its own (§4 SLA): there is no stale prior aggregate to separately track down and correct, because every day's Stage-1→2 output supersedes the prior day's in full. Stage 3's deterministic DP output is re-derivable at any time from Stage 2's current combined cell, so a same-day repeat query is free (§3) but a next-day query against a *regenerated* cell is treated as a new tuple if the underlying exact value changed. |
| **Separate analytics store** | The Combined Researcher Store (Stage 2's output) is a dedicated read-optimized store (column-oriented; exact engine TBD — ClickHouse or a Postgres analytics schema, evaluated at implementation time against the 10k-channel/100s-of-tenants sizing below) — never queried directly against the OLTP `hub_api` Postgres primary. It holds **exact, suppressed-safe combined cells only** — never a per-tenant intermediate, never DP-noised values (those are computed at Stage 3 read time, per researcher, not stored). |
| **Admin/tenant/community/vendor rollups** | Separate, finer-grained (hourly) rollups for §1's non-researcher roles continue as today, staying entirely inside each tenant's own Stage-1-equivalent boundary/DEK (no Stage 2/3 combination or DP step) — unaffected by the researcher-pipeline changes in this revision. |
| **Retention** | Stage 1's raw event data / per-tenant intermediate extract: not retained beyond that day's job run — regenerated fresh each day, so there is nothing older to retain or separately purge (reduces the erasure/DSAR surface, F-07). Stage 2's combined-cell store: retained longer (e.g., 3 years) since it carries no single-tenant attribution below the k/dominance floor once published; Stage 3 never persists its per-researcher noised output beyond the audit log (§2.3), so noise reproducibility relies entirely on the fixed server-held seed, not a cache. Exact numbers are a product/compliance decision, flagged alongside §4. |
| **Freshness** | Admin/tenant/community/vendor dashboards (§1): near-real-time via hourly rollups, consistent with existing `docs/bundle-telemetry-capability` expectations for admin log/metric visibility. Researcher dashboards (§1): daily by design (§3's static-window floor doesn't benefit from sub-day freshness, and daily regeneration is what makes the consent-propagation SLA in §4 correct-by-construction rather than query-time-patched). |
| **Scale** | Sized for ~300 tenants, ~20,000 communities, ~10,000 channels (matching the envelope-encryption and connections-credentials designs' stated sizing) — Stage 1 is naturally parallelizable across tenants (each is independent, bounded by that one tenant's data volume), Stage 2's combination work is bounded by bucket count (not row count, since its inputs are already-aggregated numbers), and the daily full-recompute cost across all three stages is checked against this sizing at implementation time to confirm it fits the batch window. |

---

## 6. hub-webui Analytics Center

**Location:** `admin/hub_module/frontend/src/pages/` — consolidates the existing role-scattered pages (`AdminAnalytics.jsx`, `AdminMemberAnalytics.jsx`, `TenantDashboard.jsx`, `VendorAnalytics.jsx`, `MyAnalytics.jsx`) into one role-aware `AnalyticsCenter` page family, rather than one bespoke page per role.

| Element | Design |
|---|---|
| **Page layout per role** | One shell component renders a role-appropriate nav (tabs: Overview / Communities / Bundles / Export, minus whichever tabs a role's §1 row excludes) — same shell, different data-fetch scope, not five separate page components. Researcher gets a distinct, simpler shell: Overview + Export only, no Communities/Bundles drill-down tabs (since researchers have no drill-down per §1). |
| **Filters** | Date range (clamped to each role's minimum granularity — day for admins, week for researchers), platform, community (hidden entirely for researcher role — no community-level filter exists for them), bundle (admin/vendor only). Filter state is a UI affordance; the API independently re-derives allowed scope server-side per §1's "never client-trusted" rule. |
| **Exports** | CSV button gated by the `analytics.export:read` scope (researcher) or existing role checks (admin/tenant/community — already CSV-capable today per existing pages). Every researcher export is watermarked and audit-logged — see Export Watermarking below. |
| **Charts** | Reuse whatever charting library the existing `*.jsx` analytics pages already use; no new charting dependency introduced by this design. |
| **Tracked debt note** | `admin/hub_module/frontend` is plain JS/JSX on React 18.3.1 with no TanStack Query — the TypeScript + TanStack migration is separately tracked debt (per `backend.md`/react standards) and is **not** a prerequisite for this feature; the Analytics Center ships in the current stack and migrates whenever the broader frontend migration reaches it. |

### Export Watermarking (F-06, mechanism revised by N-01)

Every researcher CSV export carries a **traceable, per-researcher, per-export fingerprint**, so a leaked or resold export can be traced back to the researcher and export event that produced it. **N-01: watermarking lives entirely outside and after the DP step (§3)** — it must never add fresh randomness of its own, since anything randomized could be averaged away across repeated exports the same way un-deterministic DP noise could.

| Aspect | Design |
|---|---|
| **Row-order permutation** | The order rows are written to the CSV is a deterministic permutation of the (already fixed, since windows are static per §3) row set, keyed by `HMAC(watermark_key, researcher_id, export_id)`. Distinct exports get distinct, reproducible orderings; the underlying values are untouched. |
| **Rounding-direction pattern** | Where an already-DP-noised value falls exactly on a display-rounding boundary (e.g., formatting to 1 decimal place), the round-up-vs-round-down choice at each such boundary is deterministically selected by the same `HMAC(watermark_key, researcher_id, export_id, cell_id)`, rather than always rounding the same direction — a pattern across many boundary-hitting cells in one export, invisible in any single cell, becomes a distinguishing fingerprint across the export as a whole. This never changes a value by more than the display rounding already would. |
| **Signed export manifest** | Each export ships with a manifest (`export_id`, researcher identity, timestamp, a hash per exported row/cell) signed with a server-held key — non-repudiable proof that a given file came from a given export event, independent of the CSV's own content surviving intact. |
| **Why not inside the DP noise, and not zero-width characters** | N-01: embedding the watermark in the DP noise draw (round-1 design) meant the mark and the privacy noise were the same randomness — indistinguishable from "just re-seed per export," which is exactly the averaging-vulnerable pattern N-01 forbids DP noise from having. Zero-width/invisible-Unicode watermarking (common for text documents) separately fails for numeric CSV exports: spreadsheet tools, `pandas.read_csv`, copy-into-another-numeric-pipeline, and re-aggregation all normalize or strip non-numeric characters, destroying the mark. Row-order and rounding-direction patterns survive because they ride on properties (order, boundary rounding) that a normal consumption path doesn't erase, without introducing any value the DP layer didn't already produce. |
| **Export audit log** | Every export writes a row to `researcher_export_audit(id, researcher_user_id, export_id, view_name, tenant_ids_included, requested_at, row_count)` — separate from (but joinable with) `researcher_query_audit` (§2.3), since an export can bundle multiple underlying queries into one file. `export_id` is the same value used to derive the row-order and rounding-pattern watermarks, so a recovered CSV's fingerprint maps directly to one audit row and one signed manifest. |

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
| 8 | `hub_api`: **Stage 1a** per-tenant source-extract job — decrypts only that tenant's rows (its DEK), filters out `research_data_opt_out`/DNS/erased records BEFORE any aggregation (F-01), writes to a per-tenant scratch table that never persists past the daily run | 3 |
| 9 | `hub_api`: **Stage 1a** per-tenant HMAC pseudonymization step (F-05) — `HMAC(tenant_dek_derived_salt, user_id)` applied to any identifier the extract step touches, used only for a distinct-count inside this stage, discarded before Stage 1b | 8 |
| 10 | `hub_api`: **Stage 1b** per-tenant exact aggregation — produces counts/sums per static bucket ONLY (never a row, never a pseudonym); this is the only output that crosses into Stage 2 (N-02) | 9 |
| 11 | `hub_api`: **Stage 2a** restricted researcher-aggregation service scaffold — new service/process boundary with no DEK grants and no access to any tenant row-level table, ingests only Stage 1b's numeric aggregates, combines them into cross-tenant cells (still exact, no noise) (N-02) | 10 |
| 12 | `hub_api`: **Stage 2b** k-anonymity + minimum-per-tenant-contribution/anti-dominance suppression — applied to the FINAL combined cell from #11 (k=25; suppress if 1 tenant >80% of mass or <N tenants contribute) (N-02) | 11 |
| 13 | `hub_api`: static/non-overlapping window definition (week-or-coarser, calendar-aligned) used by Stage 1b's bucketing (#10) and enforced again on researcher query params at read time — rejects rolling/arbitrary ranges (F-03) | — (feeds 10 and 20) |
| 14 | `hub_api`: Combined Researcher Store writer — persists #12's suppressed-safe EXACT cells only; test-covered to confirm no per-tenant intermediate or pre-suppression cell is ever persisted here | 12 |
| 15 | Security check: isolation audit/automated test confirming the Stage 2 service (#11) holds no tenant DEK access and no row-level table grants — config/IAM review plus a test that asserts the service's DB role and secret access are both empty of tenant-key material (N-02) | 11 |
| 16 | `hub_api`: **Stage 3** deterministic DP release module — the single query-engine choke point for every researcher-scoped read; noise derived from a fixed, server-held seed via `HMAC(researcher_id, query_shape_hash, bucket_id)`, not a fresh random draw; one privacy-budget ledger per researcher across all tenants/queries, charged once per distinct `(query shape, bucket)` tuple and free on repeat (N-01/F-02) | 14 |
| 17 | `hub_api`: query-time tenant-eligibility helper — active grant ∩ per-tenant Enterprise gate (`feature_enabled` per tenant) ∩ tenant opt-in (`research_data_opt_in_at` not null) ∩ not-revoked ∩ not-expired — filters which of the Combined Store's cells a given researcher may even request before Stage 3 runs | 3, 5, 7 |
| 18 | `hub_api`: `researcher_query_audit` write-path — every researcher-facing route writes one audit row after Stage 3 (#16), including `tenant_ids_requested` vs `tenant_ids_served` divergence | 2, 16, 17 |
| 19 | `hub_api`: per-researcher rate limit (existing rate-limit middleware, new bucket key `researcher:{user_id}`) | 18 |
| 20 | `hub_api`: differencing-attack flag job — background check comparing recent `researcher_query_audit` filter sets for isolating set-differences <k across DISTINCT query shapes (determinism from #16 already closes the same-shape repeat-query case) | 18 |
| 21 | `hub_api`: 2-3 pre-computed aggregate view endpoints under `/api/v1/analytics/researcher/*` (e.g., community-category distribution, tenant growth trend) over the fixed weekly windows (#13) — gated by scopes from #4, tenant filter from #17, reading only from the Combined Store (#14) through Stage 3 (#16) | 14, 16, 17 |
| 22 | `hub_api`: export endpoint with watermarking (F-06/N-01 — deterministic row-order permutation + rounding-direction pattern + signed manifest, NONE of it inside the DP noise), gated on `analytics.export:read`, writes `researcher_export_audit` | 2, 21 |
| 23 | `hub_api`: consent-propagation post-SLA audit job (F-01/F-07) — re-derives expected bucket values from the current clean extract, diffs against what's published, pages on-call and blocks next publish on mismatch | 8, 14 |
| 24 | `hub_api`: erasure/DSAR hook — a deletion request triggers immediate source-row purge (existing DSAR flow) so it's excluded from the next daily extract (step 8), same ≤24h SLA as opt-out (F-07) | 8 |
| 25 | Admin/tenant/community/vendor rollup job (hourly, unaffected by the researcher pipeline) — unchanged from today, documented here for completeness | — (parallelizable with 1-24) |
| 26 | hub-webui: `AnalyticsCenter` shared shell component (tabs driven by role, per §6) replacing the entry points of `AdminAnalytics.jsx`/`TenantDashboard.jsx`/`VendorAnalytics.jsx`/`MyAnalytics.jsx` (existing pages' data-fetch logic reused, not rewritten) | — (parallelizable with 1-25) |
| 27 | hub-webui: Researcher shell variant (Overview + Export tabs only, static-week picker, no community filter, no custom date range) wired to #21/#22 endpoints | 21, 22, 26 |
| 28 | Tests: unit tests for the DP determinism module (#16 — same input always same output, distinct researchers get distinct noise, budget charged once per tuple), k-anonymity/anti-dominance suppression (#12), window enforcement (#13), and tenant-eligibility helper (#17) — table-driven edge cases (exactly k, k-1, one-tenant->80% mass, <N tenants contributing, budget exhausted, rolling-range rejected, revoked mid-window, tenant license lapse mid-window) | 12, 13, 16, 17 |
| 29 | Tests: integration test — opt-out/erasure recorded day N is absent from researcher output day N+1 (F-01/F-07); global license lapse revokes all researcher queries next-call; per-tenant lapse excludes only that tenant | 8, 17, 23, 24 |
| 30 | Tests: regression test for the differencing-attack flag job (#20) and for watermark round-trip (export → recovered row-order/rounding-pattern/manifest maps to the correct `researcher_export_audit` row) | 20, 22 |
| 31 | OpenAPI spec update: `hub_api/openapi/v1.yaml` additions for the new researcher routes (#7, #21×N, #22) | 7, 21, 22 |
| 32 | Docs: DPA/contract-ref field documented in admin runbook; flag the 4 legal-review items from §4 as tracked follow-ups (separate GH issues, not blocking this implementation) | 7 |

Tasks 1-4, 13, 25-26 have no interdependencies and can run in parallel. The Stage 1→2→3 chain (8→9→10→11→12→14→16) and its isolation guarantee (#15) must land before any researcher-facing route (21+) merges — retrofitting source-side consent filtering, the tenant-key isolation boundary, or deterministic DP after a query-time-only version ships is a much larger diff (and a live compliance/security gap) than building all three in from the start.
