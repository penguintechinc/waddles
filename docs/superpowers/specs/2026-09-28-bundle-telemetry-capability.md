# Bundle Telemetry Capability — Design

**Status:** Proposed design (no code in this doc)
**Date:** 2026-09-28
**Scope:** `wit/waddle-bundle/stage.wit`, `core/bundle_executor`, `core/svc_process`, `core/svc_action`, `core/bundle_capability_gate` (reused, not re-litigated), `hub_api` (tenant OTLP destination config + scoped telemetry read endpoints), hub-webui (telemetry views + Enterprise-lapse banner).
**Driver:** Justin, verbatim — *"we should also have a standard way (with protections / filtering) for app bundles to emit metrics and logs which we convert into otel outputs."*

**Builds on (read, reconciled, not re-litigated):**
- `wit/waddle-bundle/stage.wit` v1.0 (live) — `log` already exists (`write(level, message, fields-json)`, sanitized via `penguin_logging::sanitize::sanitize_object` before emission, spec §7.4). No `metrics` interface exists today.
- `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md` (umbrella) — this spec adds `telemetry.logs`/`telemetry.metrics` to its permission catalog (§1), reuses its `core/bundle_capability_gate::authorize()` gate, its `AppScoped` resource-derivation rule ("no bundle host call accepts a tenant or community argument," §5.11), and its manifest/consent-flow machinery verbatim — it does not invent a second permission system.
- `critical-rules.md` Observability (OTel) — OTLP destination always env/config-configurable, never hardcoded; histograms first; deliberate log levels; a dead exporter never breaks the app; PII/secrets never in spans/attributes/labels/bodies.
- `critical-rules.md` PII Tokenization / `client.md` — bundles only ever hold UUIDs; usernames and display names are PII and never cross into bundle-visible or bundle-emitted telemetry.
- `docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-design.md` — per-tenant DEK/AAD pattern this spec reuses verbatim for encrypting a tenant's OTLP auth headers, no second KMS integration.
- `docs/superpowers/specs/2026-09-28-connections-credentials-design.md` — SSRF-guard precedent (webhook/JWKS receivers) this spec reuses for tenant OTLP endpoint validation.
- Sizing target, shared with the above two specs: ~300 tenants, ~20,000 communities, ~10,000 channels.
- `hub_api/blueprints/v1/marketplace_lifecycle.py` — the three real scope gates this spec's read endpoints reuse: `require_scope("platform:admin")`, `require_scope("tenant:admin")` + tenant match, `authorize_community(..., admin=True|False)`. `services/vendor_bundle_authz.py` — `vendor:onboard` scope + `waddles.integrations.vendor-{id}.*` namespace enforcement, reused for vendor status visibility (§6).

---

## 1. WIT additions

### 1.1 New `metrics` interface (additive — new imported interface, existing bundles unaffected)

```wit
/// Bundle-declared metrics, emitted through the host's existing OTel
/// pipeline (SS2). Every instrument name and every label key/value used
/// here MUST match a `telemetry.metrics` entry in the bundle's manifest
/// (SS1.3) -- an undeclared instrument or label is rejected host-side,
/// counted, and never forwarded to any exporter (SS4).
/// Capability: `telemetry.metrics` (normal, always granted, still
/// declared -- SS5).
interface metrics {
  variant error {
    denied(string),
    not-declared(string),
    invalid-label(string),
    rate-limited(u32),
  }

  counter-add: func(name: string, value: f64, labels: list<tuple<string, string>>) -> result<_, error>;
  up-down-counter-add: func(name: string, value: f64, labels: list<tuple<string, string>>) -> result<_, error>;
  /// Last-value; overwrites, never accumulates.
  gauge-set: func(name: string, value: f64, labels: list<tuple<string, string>>) -> result<_, error>;
  /// Recorded into the manifest-declared bucket set (SS1.3); a value is
  /// still accepted with no bucket match (OTel semantics), just uncounted
  /// by any bucket.
  histogram-record: func(name: string, value: f64, labels: list<tuple<string, string>>) -> result<_, error>;
}
```

`world stage` gains `import metrics;`. No existing function signature changes — SDKs already built against v1.0 keep working unmodified.

### 1.2 `log` — unchanged wire signature, extended manifest contract

`log.write(level, message, fields-json)` keeps its exact v1.0 shape (host-facing SDKs stay wire-compatible). What's new is manifest-declared structure around it (SS1.3) and host-side enrichment (SS2.3): a declared field-key allowlist, a level ceiling resolved through the existing 3-tier config, and automatic correlation to the invoke span. No new WIT function is needed for any of this.

### 1.3 Manifest block

```yaml
telemetry:
  metrics:
    - name: fish_caught_total
      type: counter                 # counter | up_down_counter | gauge | histogram
      unit: "1"
      description: "Legendary and normal fish catches."
      labels:
        rarity: [common, rare, legendary]   # declared allowed values (closed set)
        pond: null                          # declared key, open value set (cardinality-capped, SS4)
    - name: cast_duration_ms
      type: histogram
      unit: ms
      description: "Time from cast to catch/miss resolution."
      buckets: [10, 50, 100, 250, 500, 1000, 5000]
      labels: {}
  logs:
    fields: [pond, rarity, catch_id]   # optional structured-field allowlist; undeclared keys dropped (SS4)
```

Validated the same way `bundle_manifest_v2.py` validates `data.schema`/`egress` today: instrument `name` matches `_INSTRUMENT_ID_RE` (`[a-z][a-z0-9_]{0,63}`), ≤20 instruments/app, ≤4 label keys/instrument, closed label value sets ≤20 values, histogram ≤20 buckets. `logs.fields`, if present, is ≤20 keys; if absent, the host falls back to a default cap (SS4) rather than rejecting the manifest.

### 1.4 Span events, not free-form spans

Bundles do not get a spans/tracing WIT interface in this spec. Justification: an untrusted-WASM-controlled span name/count is a cardinality and cost vector with no bounded manifest declaration to gate it (unlike metrics, which are name+label declared up front); the invoke already has one host-created span, and every `log.write`/metric call is correlated to it (SS2.3) without needing the bundle to create its own. Revisit only if a concrete attribution need surfaces that span events can't satisfy — none has.

---

## 2. Host conversion to OTel

### 2.1 Pipeline reuse

The host does not stand up a second OTel pipeline. Both `svc_process` and `svc_action` already initialize `penguin_logging::init()` (wraps `tracing-subscriber` + OTLP export + a Prometheus registry, reads the standard `OTEL_EXPORTER_OTLP_*`/`OTEL_SERVICE_NAME`/`OTEL_RESOURCE_ATTRIBUTES` env vars, `critical-rules.md` Observability). Bundle metrics/logs emit through that exact same `tracing`/OTel meter provider — no bundle-specific exporter, no second hardcoded destination (destination fan-out is a routing concern, SS3, not a second pipeline).

### 2.2 Namespacing

| Signal | Name/identity | Resource attributes | Scope attributes |
|---|---|---|---|
| Metric | `waddles.bundle.<app_id>.<name>` | `service.name` (host service, e.g. `svc-process`), `deployment.environment` | `app_id`, `app_version`, `bundle_language`, `stage` (`process`\|`action`) — never `tenant`/`community` as a resource attribute globally (SS3.4) |
| Log record | `tracing` target `waddles::bundle::<app_id>`, `level` from `log::Level` | same resource attrs as above | `app_id`, `app_version`, `bundle_language`, `message_id` (from `bundle-context`, spec's existing dedup key) |
| Span event | `bundle.log` on the host-created invoke span | n/a (inherits the span's resource) | `level`, sanitized `message` (truncated to `MAX_BUNDLE_LOG_MESSAGE_LEN`), no arbitrary fields |

`tenant`/`community` are never global resource attributes (would multiply every series/record by tenant at the source) — they are per-record/per-span **attributes**, always host-derived from the invoke's `InvokeScope` (permissions spec §5.11's rule: no bundle host call accepts a tenant/community argument), carried as opaque ids, never a display name. This is also what SS3's fan-out router keys on.

`app_id`/`app_version`/`bundle_language` are the only bundle-identity values baked into the metric *name*; note this does **not** reduce total series count vs. putting `app_id` in a label — a TSDB counts `name+labels` as one series regardless of which half `app_id` lives in. It is chosen for isolation (no accidental cross-app collision, no bundle can ever emit under another app's name) and for simple per-app quota/allowlist enforcement keyed on a name prefix (SS4), not for cardinality savings — SS7's math accounts for this honestly.

### 2.3 Trace-context correlation

`Connection::invoke` in both `svc_process` and `svc_action` gains `#[tracing::instrument(name = "bundle.invoke", skip_all, fields(tenant, community, app_id, stage, message_id))]`. This is the span every `log.write` and every metrics call for that invocation correlates to:
- If the inbound `stage-envelope.trace-context` (existing W3C traceparent field) is present, the invoke span continues that trace; otherwise it roots a new one — exactly the correlation stage.wit already carries for action-stage dispatch, now also used process-side.
- Every `log.write` call is recorded as a `bundle.log` span event (SS2.2) on this span; `ERROR`-level calls additionally set the span status to `Error` (informational only — never affects control flow, matching the existing rule that "a bundle's own logging must never be able to affect its control flow").
- Metric calls do not create additional spans; they carry the invoke's `trace_id`/`span_id` as OTel Exemplars where the metrics SDK in use supports them (best-effort, not a hard requirement).

### 2.4 Logs specifically

Unchanged from today's bridge: `log::Host::write` sanitizes via `penguin_logging::sanitize::sanitize_object` (SENSITIVE_KEYS rule), strips control characters, truncates to `MAX_BUNDLE_LOG_MESSAGE_LEN` (4096), then emits via `tracing::{error,warn,info,debug}!`. New in this spec: `fields-json` keys not in the manifest's declared `logs.fields` allowlist (SS1.3) are dropped **before** sanitization even runs (SS4), and the effective emission level is clamped to the community/tenant's configured ceiling (SS4) before the `tracing` macro call is chosen.

---

## 3. Telemetry destinations: global + per-tenant fan-out (Enterprise)

### 3.1 Two destinations, one sanitize path

| Destination | Config source | Scope | Tier |
|---|---|---|---|
| **Global** | Standard OTLP env vars (`OTEL_EXPORTER_OTLP_ENDPOINT`/`_PROTOCOL`/`_HEADERS`) per `critical-rules.md` | Platform-wide, every record | All tiers |
| **Per-tenant** (max 1/tenant) | `tenant_otel_destinations` row, configured by the tenant admin via hub-api | Only that tenant's qualifying records | **Enterprise**, flag-gated (SS3.6) |

Sanitization (SS4) happens exactly once, before the fan-out branch — both destinations receive the identical scrubbed payload; the tenant destination never sees a less-sanitized copy.

### 3.2 What qualifies for tenant fan-out

| Telemetry | Qualifies? | Why |
|---|---|---|
| Bundle-emitted logs/metrics (SS1) | Yes | Already carries exactly one unambiguous `tenant_id` from the invoke scope |
| Host per-invoke signals: invoke latency histogram, capability-denial counters (permissions spec §5.3), telemetry rate-limit/drop counters (SS4) | Yes | Single-tenant-attributable, describes that tenant's own bundle activity |
| Per-tenant exporter health (`sent`/`dropped`/`errors`/`queue_depth`, SS3.4) | Yes | About that tenant's own export pipeline — a tenant should see its own delivery health |
| Process-level infra metrics (CPU/mem/GC, DB pool saturation, connection-pool gauges) | No | Describes the service's own health across all tenants — inherently cross-tenant, stays global-only |
| Any record without a single resolvable `tenant_id` in its own data model | No | Rule of thumb: qualifies iff the record's own scope is unambiguous, never a shared/aggregate gauge |

### 3.3 Storage of tenant destination config

| Table | Written by | Key columns |
|---|---|---|
| `tenant_otel_destinations` | hub-api tenant-admin endpoint (`tenant:admin` scope) | `tenant_id` PK, `endpoint`, `protocol` (`grpc`\|`http/protobuf`), `headers_ciphertext`, `dek_version`, `enabled`, `created_by`, `updated_at` |

Auth headers are secrets: encrypted with the tenant's existing per-tenant DEK (tenant-envelope-encryption design, AAD = `tenant_id|tenant_otel_destinations|headers|row_uuid|dek_version`), never logged, decrypted only in-process to build the exporter client — same posture as `connection_credentials`. hub-api is the only RW path; `svc_process`/`svc_action`/`svc_ingest`/`svc_streaming` read via the existing RO-replica role (`waddles_bundle_reader`-equivalent) on the same `BUNDLE_CONFIG_POLL_SECONDS` hot-swap cadence already used for grants (permissions spec §4) — zero new poll loop, one more table joined into the existing refresh.

### 3.4 Isolation & resilience

| Control | Mechanism |
|---|---|
| Bounded queue | Per-tenant exporter has its own bounded channel (e.g. 10k records / 8 MB); full queue drop-oldest + counter, never blocks the emitting host-call |
| Backoff | Exponential backoff on export failure, capped; a dead/slow tenant endpoint's retries never share a threadpool with, or throttle, another tenant's export or the global export |
| Never affects request handling | Bundle telemetry emission (SS1) never awaits tenant-export completion — fire-and-forget into the per-tenant queue, same "a dead exporter never breaks the app" rule extended per-tenant |
| Per-tenant export metrics | `waddles.telemetry.tenant_export.sent_total{tenant}`, `.dropped_total{tenant,reason}`, `.errors_total{tenant,code}`, `.queue_depth{tenant}` (gauge) — visible to that tenant's own admins (SS6), and also flow to the global destination (only 300 tenants, cheap) |
| Cross-tenant leak test | Mandatory negative test (SS8): tenant A's destination receives zero records carrying tenant B's `tenant_id` |

### 3.5 Security

| Control | Detail |
|---|---|
| TLS mandatory | Reject plaintext `http://` or unencrypted gRPC targets regardless of protocol choice — TLS 1.2+ (prefer 1.3), same floor as `security.md` |
| SSRF guard | Same pattern as the webhook/JWKS receiver design (connections-credentials-design): public addresses only — block RFC1918/loopback/link-local/`169.254.169.254` metadata IP; resolve once, pin the checked IP for the connection (defeats DNS rebinding); no following redirects |
| Optional allowlist | Tenant admin may additionally restrict to a specific host/domain suffix |
| Size/rate budget | Independent per-tenant byte/record budget (SS4), separate from the global export budget |
| PII scrubbing | Identical sanitize path as SS3.1 — no separate, weaker path for the tenant destination |

### 3.6 Licensing

Per-tenant OTel export is an **Enterprise**-tier feature, gated by `get_tier() == "enterprise"` (license server) **and** PostHog flag `waddles.tenant-otel-export` (default OFF) — two-gate per `critical-rules.md` Feature Flags & License Tiers. Bypass is domain-based only (`*.penguincloud.io`/`*.penguintech.cloud`/product `.app` domains), never an env var/CLI arg. Admin **visibility** views (SS6: community/tenant/global read scopes) are **not** tier-gated — every tier can see its own bundle telemetry in the internal store; only shipping a copy to the tenant's own external destination is Enterprise.

**Tier lapse:** stop exporting to the tenant destination gracefully — config row stays intact (nothing deleted), hub-webui shows a banner ("Per-tenant telemetry export paused — requires Enterprise"), global export is entirely unaffected, and the transition is logged once at INFO (not per-record).

### 3.7 Scale: shared forwarder vs. in-process fan-out

At ~300 tenants × N service replicas (`svc_process`, `svc_action`, `svc_ingest`, `svc_streaming`), naive in-process fan-out means every pod holds up to 300 live export clients.

| Option | Pros | Cons |
|---|---|---|
| **A. In-process per-tenant exporters** (each Rust service dials every tenant's endpoint directly) | No new infra component; reuses `penguin_logging`'s existing exporter machinery | Connection multiplication = tenants × pods × services (thousands of long-lived clients cluster-wide); SSRF/backoff/queue logic reimplemented per service; config hot-swap must land in every service independently |
| **B. Shared telemetry-forwarder/collector** (single deployment per cluster; services keep emitting to the global OTLP endpoint unchanged; the collector applies a per-tenant routing rule keyed on the `tenant` attribute and additionally forwards matching records to that tenant's destination) | SSRF/backoff/queueing logic lives once; connection multiplication is collector→tenant, not pod→tenant; zero change to service OTLP emission code beyond the `tenant` attribute they already carry | New stateful component in the telemetry path — must be deployed HPA'd and must never gate the global path (services point primarily at the global backend; the collector's own health never blocks a service's own emission) |

**Recommendation: Option B.** Isolates the SSRF/backoff/cardinality blast radius to one component instead of duplicating it across 4+ Rust services, and is the only option whose cost doesn't scale with pod count. Exact collector choice (OTel Collector contrib `routing` connector vs. a small purpose-built forwarder) is an implementation-phase decision, not a blocker here.

---

## 4. Protections and filtering (all host-side)

| Protection | Mechanism |
|---|---|
| Declared-only instruments/labels | `metrics.*` calls checked against the manifest's `telemetry.metrics` block (SS1.3) before anything else; undeclared instrument → `not-declared` error + `waddles.telemetry.rejected_total{app_id, reason="undeclared_instrument"}` counter, never forwarded |
| Label cardinality cap | ≤4 label keys/instrument (manifest cap, SS1.3), ≤100 distinct label-value combinations per `(tenant, app, instrument)` — deny-new-series-when-full (existing series keep updating; a brand-new combination is dropped + counted), not an unbounded LRU |
| Value sanitization & length caps | Label values: strip control chars, cap 128 bytes, reject non-UTF8; log messages: existing `sanitize_bundle_log_message` pipeline (control-char strip, `SENSITIVE_KEYS` redaction, 4096-char cap) |
| PII/secret scrubbing | Reuses `penguin_logging::sanitize::sanitize_object`'s `SENSITIVE_KEYS` rule for every `fields-json`/label map; additionally regex-scrubs email-shaped, phone-shaped, and token-shaped (`^[A-Za-z0-9_-]{20,}$` bearer-token heuristic) values wherever they appear, not just under a sensitive key name; UUID-shaped values pass through unredacted (they're already the platform's tokenized identity form) |
| Per-invocation budget | ≤50 log lines / invoke, ≤200 metric calls / invoke — exceeding either drops the remainder of that invoke's telemetry, counted, never fails the invoke itself |
| Per-app rate limit & byte budget | Token-bucket per `(tenant, app_id)` — e.g. 500 log lines/min, 2000 metric points/min, 256 KiB/min — reuses the existing `UsageBatcher` primitive (`core/svc_action/src/usage.rs`) pattern already used for `relay`/`moderation`; excess → drop-oldest + `waddles.telemetry.dropped_total{app_id, reason="rate_limited"}` |
| Log-level ceiling | Resolved through the existing 3-tier config (`bundle-context.config-json`) as reserved keys `_telemetry.log_level_ceiling` (default `info`) and `_telemetry.log_level_expires_at`; a community/tenant admin can raise the ceiling to `debug` only with an expiry (auto-reverts to `info`); the host clamps `effective_level = min(bundle_requested_level, ceiling)` — a bundle can never self-escalate |
| No spoofed resource attributes | `tenant`/`community`/`app_id`/`service.name` are always host-derived from the invoke's `InvokeScope`, never a bundle-supplied argument — the WIT signatures (SS1.1) take no such parameter at all, mirroring the permissions spec's §5.11 rule |
| Telemetry can never break the invoke | Every telemetry host-call runs under the same host-call timeout as `http`/`db`/etc.; any internal error is logged at DEBUG and swallowed (matches today's `log::Host::write` bridge failure handling) — a telemetry failure is never propagated as an invoke failure |

---

## 5. Permission catalog additions

| id | Risk | Default quota | `CapabilityKind` | Notes |
|---|---|---|---|---|
| `telemetry.logs` | normal | 4096 B/message, 50 lines/invoke, 500 lines/min/(tenant,app) | `Log` | Was "always granted" (stage.wit v1.0) — now explicit and declared, still auto-approved (permissions spec §3.5) |
| `telemetry.metrics` | normal | ≤20 instruments/app, ≤4 label keys/instrument, ≤100 series/(tenant,app,instrument), ≤20 histogram buckets, 2000 points/min/(tenant,app), 256 KiB/min/(tenant,app) | `Metrics` *(new)* | Requires the `telemetry.metrics` manifest block (SS1.3); an undeclared instrument is denied `not_declared` before the permission grant is even checked |

Both are `normal` risk (contained to the bundle's own declared, capped surface — no external network, no cross-tenant reach) and granted by default like `flags.read`/`platform.log`, but still appear on every consent screen for transparency (permissions spec §3.5/§8) and are still declared explicitly, closing the same "always granted was implicit" gap that spec retires for `kv`/`flags`.

---

## 6. Visibility & scoped views (normative)

| Role | Sees | Scope enforcement |
|---|---|---|
| **Global (`platform:admin`)** | Everything — every tenant, every community, every bundle | `require_scope("platform:admin")`, no additional filter |
| **Tenant admin (`tenant:admin`)** | All bundle logs/metrics across their own tenant | `require_scope("tenant:admin")` + tenant-match (existing `_tenant_id`/`require_matching_tenant` pattern) — query filtered `WHERE tenant_id = ctx.tenant_id`, never a client-supplied tenant id |
| **Community admin** | Logs/metrics of bundles activated in *their* community only | `authorize_community(request, dal, community_id=..., admin=True)` (existing pattern) — query filtered `WHERE community_id = :community_id AND community_in_tenant(community_id, ctx)` before any row is read |
| **Vendor (`vendor:onboard`)** | **No logs, no metrics.** Only which communities/tenants have the vendor's bundle **activated and running** — opaque/display-safe identifiers only | `require_scope("vendor:onboard")` + `enforce_vendor_namespace(app_id)` restricting to `waddles.integrations.vendor-{caller_id}.*` (existing pattern) — never another vendor's `app_id` |

**"Running," defined:** `app_activations` row exists (activated) **and** at least one successful invoke or health signal within a rolling window (default 15 min, config-tunable) — an activation alone is not "running"; a bundle that's activated but has thrown nothing but errors, or hasn't been invoked, is not "running" either unless a health/heartbeat signal substitutes. The vendor status endpoint returns `{tenant_id (opaque), community_id (opaque), last_seen_at, status: running|activated_idle}` — never a tenant/community display name, never log/metric content.

**hub-api read endpoints (all under existing scope-gated blueprints, mirroring `marketplace_lifecycle.py`'s pattern):**

| Endpoint (sketch) | Scope | Filter applied server-side |
|---|---|---|
| `GET /telemetry/global/bundles/{app_id}` | `platform:admin` | none (unrestricted) |
| `GET /telemetry/tenants/{tenant_slug}/bundles/{app_id}` | `tenant:admin` | `tenant_id = ctx.tenant_id` |
| `GET /telemetry/communities/{community_id}/bundles/{app_id}` | community-admin | `community_in_tenant(community_id, ctx)` + `community_id = :community_id` |
| `GET /telemetry/vendor/apps/{app_id}/status` | `vendor:onboard` | `enforce_vendor_namespace(app_id)`; returns running/activated-idle status only, never logs/metrics |

Tenant/community/app ids are **never** taken from the request body for scoping purposes — always from the validated JWT (`ctx`) or a path segment re-validated against it, per `security.md` Tenant Isolation.

**Backend requirement:** the OTLP backend behind these reads must support this filtering natively (native multi-tenant query isolation), **or** hub-api needs its own scoped query path independent of the raw backend. If the chosen backend is external/customer-chosen and doesn't support server-side tenant/community filtering, hub-api must maintain a per-tenant (or per-community) log/metric index (or a narrower internal mirror keyed by `tenant_id`/`community_id`/`app_id`) that these endpoints query instead of trusting the backend's own access controls — this queryable store is independent of, and unaffected by, whether a tenant also configured their own external OTel destination (SS3): a tenant losing/misconfiguring their own destination never removes the platform's internal visibility.

---

## 7. Cost and cardinality math (~300 tenants, ~20,000 communities, ~10,000 channels)

| Quantity | Assumption | Value |
|---|---|---|
| Distinct `app_id`s platform-wide (catalog) | Order-of-magnitude target this design must not choke on | ~2,000 |
| Avg declared instruments/app | Manifest cap 20, typical usage far lower | ~5 |
| Avg tenants an app is activated in | Most apps activate in a minority of tenants | ~10 |
| Per-instrument series cap | SS4 | 100 / (tenant, app, instrument) |

**Global destination, worst case** (every activated app hits its full cardinality cap in every tenant it's active in):

```
2,000 apps × 5 instruments × 100 series × 10 tenants ≈ 10,000,000 series
```

This is the number a naive "apply the per-instrument cap independently per tenant with no aggregate ceiling" design produces — too high to treat as acceptable headroom. **Required backstop, not optional:** a platform-wide aggregate cap **per app** (sum across all tenants, not just per-tenant), e.g. 2,000 series/app platform-wide, enforced the same drop-new-combination way as the per-tenant cap, bringing the worst case down to `2,000 apps × 2,000 = 4,000,000` series ceiling — still a ceiling, not a target; typical usage (most instruments never near their cap, most apps active in far fewer than 10 tenants) should sit one to two orders of magnitude below it. This aggregate cap is a required SS4 addition, not just documented here.

**Per-tenant destination** (SS3, Enterprise only): bounded by that tenant's own activation footprint — e.g. a tenant with 50 communities and 30 distinct activated apps: `30 apps × 5 instruments × 100 series ≈ 15,000 series` worst case for that one tenant's own external backend, an order of magnitude smaller than the global sink and the tenant's own problem to size for (their own OTLP backend, their own cost).

**Logs** are not cardinality-bound the same way (no series concept) but are volume-bound: at the per-app rate limit (500 lines/min/(tenant,app)) with 2,000 apps × 10 tenants active-fraction, worst-case sustained log volume is bounded at `2,000 × 10 × 500 = 10,000,000 lines/min` platform-wide absolute ceiling — again a backstop number for capacity planning, not an expected steady-state, and exactly why the per-app/per-tenant rate limits (SS4) exist as hard caps rather than soft guidance.

**Takeaway:** namespacing `app_id` into the metric name (SS2.2) does not reduce series count — the aggregate per-app cap (this section) is what actually bounds global cardinality; it must ship alongside the per-instrument/per-tenant caps in SS4, not as a later addition.

---

## 8. Testing

| Test | Assertion | On failure |
|---|---|---|
| Smoke: telemetry gate | A test bundle declaring 1 counter + 1 histogram + `log.write` at INFO emits ≥1 log record and ≥1 metric data point, received and counted by a local OTLP test sink | FAIL (zero received = FAIL, per `critical-rules.md` Verification Integrity, never a skip) |
| Negative: undeclared label dropped | Bundle calls `counter-add` with a label key not in its manifest → call returns `invalid-label`, counter data point for that series is never forwarded, `rejected_total` counter increments by exactly 1 | FAIL if the series reaches the sink or the counter doesn't increment |
| Negative: PII scrubbed | Bundle logs a message containing an email-shaped string and a field under a `SENSITIVE_KEYS`-matching key → received log record contains neither in plaintext | FAIL if either appears unredacted in the sink |
| Negative: rate limit | Bundle exceeds its per-invoke log-line budget (SS4) → excess lines dropped, `dropped_total{reason="rate_limited"}` increments, invoke itself still succeeds | FAIL if the invoke errors, or if excess lines reach the sink |
| Negative: cross-tenant leak (SS3.4) | Tenant A configures a per-tenant destination; tenant B's bundle telemetry is generated; tenant A's destination sink receives zero records tagged `tenant=B` | FAIL on any leaked record |
| Negative: tier lapse | Enterprise flag flipped OFF mid-run for a tenant with a configured destination → tenant fan-out stops within one poll cycle, global export continues uninterrupted, config row untouched | FAIL if tenant export continues, or if global export is disrupted |

All counts reported (examined/rejected/scrubbed/dropped), never a bare pass — per `critical-rules.md` Verification Integrity.

---

## 9. Phased implementation plan (agent-sized, ≤30 min each)

| Phase | Task | Component | Depends on |
|---|---|---|---|
| 0 | Add `metrics` WIT interface (SS1.1) to `wit/waddle-bundle/stage.wit`; regenerate bindings | WIT | none |
| 0 | `_INSTRUMENT_ID_RE` + `telemetry:` manifest block validation (SS1.3) in `bundle_manifest_v2.py` | hub-api | none |
| 0 | Add `telemetry.logs`/`telemetry.metrics` to the permission catalog module (SS5) | Rust (`bundle_capability_gate`) | permissions spec Phase 0 |
| 1 | `#[tracing::instrument]` on `Connection::invoke` (SS2.3) in `svc_process`; span-event bridge for `log.write` | Rust (svc_process) | Phase 0 |
| 1 | Same for `svc_action` | Rust (svc_action) | Phase 0 |
| 1 | Manifest `logs.fields` allowlist enforcement + log-level-ceiling config keys (SS2.4, SS4) | Rust (both stages) | Phase 0 |
| 2 | `metrics::Host` impl in `bundle_executor/src/host/imports.rs` bridging to `CapabilityKind::Metrics`; declared-instrument/label validation host-side (SS4) | Rust (bundle_executor) | Phase 0 |
| 2 | `handle_metrics` in `svc_process`/`svc_action` `capabilities.rs`: counter/gauge/histogram emission via `penguin_logging`'s meter, per-instrument cardinality cap (SS4) | Rust (both stages) | previous task |
| 2 | Per-app/(tenant,app) rate limiter + byte budget reusing `UsageBatcher` (SS4) | Rust (both stages) | previous task |
| 3 | Aggregate per-app platform-wide cardinality cap (SS7 backstop) | Rust (both stages) | Phase 2 |
| 4 | `tenant_otel_destinations` migration + hub-api CRUD (`tenant:admin`) with header encryption via existing tenant DEK (SS3.3) | hub-api | tenant-envelope-encryption design |
| 4 | Data-plane hot-swap of `TenantExporterSnapshot` into existing `BUNDLE_CONFIG_POLL_SECONDS` loop | Rust (both stages) | previous task |
| 5 | Shared telemetry-forwarder (Option B, SS3.7): per-tenant routing rule, bounded queue/backoff, SSRF guard | Rust or Collector config | Phase 4 |
| 5 | Per-tenant export metrics (`sent`/`dropped`/`errors`/`queue_depth`, SS3.4) | Rust (forwarder) | previous task |
| 5 | `get_tier()`/PostHog `waddles.tenant-otel-export` gate + graceful tier-lapse handling (SS3.6) | Rust (forwarder) + hub-api | previous task |
| 6 | hub-webui: tenant OTel destination config screen + Enterprise-lapse banner | React | Phase 4 |
| 7 | hub-api scoped read endpoints (SS6): global/tenant/community/vendor | hub-api | Phase 2 |
| 7 | hub-webui: telemetry views per role (SS6) | React | previous task |
| 8 | Smoke test: telemetry gate (SS8 row 1) | Rust/CI | Phase 2 |
| 8 | Negative tests: undeclared label, PII scrub, rate limit, cross-tenant leak, tier lapse (SS8 rows 2-6) | Rust/CI | Phase 5, Phase 7 |

Phases 0-3 (WIT + catalog + emission + host-side gate) have no dependency on Phase 4-6 (tenant fan-out) and can land and ship independently — per-tenant export is a pure Enterprise add-on to an already-complete global telemetry path.

---

## 10. Open questions (not blockers, flagged for follow-up)

- **OTLP backend product choice** — this spec is backend-agnostic by design (`critical-rules.md`: destination always configurable); whether the platform's own default (e.g. Killkrill, if adopted) is used for the global destination, and whether it natively supports the tenant/community filtering SS6 requires, is a separate infra decision this spec doesn't block on.
- **Exact quota numbers** (SS4/SS5 defaults: 100 series/instrument, 500 lines/min, 2,000 points/min, 2,000-series/app aggregate cap) are starting points, not load-tested — Phase 3/8 should tune them against real fixture load before the aggregate cap ships as a hard gate.
- **Shared forwarder implementation** (SS3.7 Option B) — OTel Collector contrib `routing`/`groupbyattrs` processors vs. a small purpose-built Rust forwarder is an implementation-phase call, not designed further here.
