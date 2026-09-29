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

**Gemini PR-review conditions (PASS-WITH-CONDITIONS on #421) — folded in as normative requirements:**

| # | Condition | Resolved in | Requirement |
|---|---|---|---|
| 1 | Cardinality tracker must be memory-bounded, LFU eviction, hourly window reset | §4 | Bounded per-instrument tracker, LFU eviction on overflow (not deny-new-blind), hourly window reset |
| 2 | SSRF: custom dialer connects only to the validated IP (no re-resolution); block RFC1918/loopback/link-local/metadata + IPv6 `fc00::/7`/`fe80::/10`/`::1` + IPv4-mapped IPv6 | §3.5 | Full blocklist + pinned-IP custom dialer, normative |
| 3 | Log injection: strip 0x00-0x1F (except `\n`, encoded)/0x7F-0x9F/ESC/Unicode bidi overrides; render as untrusted text in UI | §2.4, §4 | Expanded sanitizer spec + hub-webui untrusted-text rendering rule |
| 4 | `logs.fields` allowlist is the PRIMARY scrubbing boundary; regex scrubbing is defense-in-depth only; reject/redact high-entropy strings | §4 | Reordered scrubbing pipeline + entropy check |
| 5 | Cross-tenant: per-tenant actor/pipeline isolation + tenant-id assertion at final serialization (mismatch drops batch + alerts) | §3.4 | New isolation + assertion rows |
| 6 | `InvokeScope` passed explicitly or via `tokio::task_local!`, never thread-local; yield-across-threads tests | §2.3, §8 | Explicit-context requirement + test |
| 7 | Fully non-blocking OTLP IO; exporters isolated on their own runtime/pool | §3.4, §3.7 | Dedicated runtime/pool requirement |
| 8 | Data residency: Enterprise tenant opt-out disabling the internal telemetry mirror (+ hides hub-webui tab); TTL cap on the internal mirror | §6 | New opt-out + retention requirement |

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

**Normative: `InvokeScope` (tenant, community, app_id, message_id) is passed explicitly through the async call chain — as an argument, or via `tokio::task_local!` scoped for the duration of one `invoke` future — never a thread-local static.** `svc_process`/`svc_action` run on Tokio's multi-threaded work-stealing runtime, where a single `.await` can resume the same logical task on a different OS thread; a `thread_local!`-keyed scope would silently read/write the wrong invocation's context after such a resume, which is exactly the cross-tenant leak vector this spec closes in §3.4. `tokio::task_local!` is bound to the task, not the thread, and survives a cross-thread resume correctly; a plain argument threaded through every capability-handler call achieves the same guarantee with no macro magic and is preferred where the call depth is shallow enough to avoid signature sprawl. Either is acceptable; a bare `thread_local!`/`std::sync::OnceLock`-as-ambient-global for per-invoke scope is not, for any of `metrics`, `log`, or the existing capabilities. Test requirement in §8.

### 2.4 Logs specifically

Unchanged from today's bridge: `log::Host::write` sanitizes via `penguin_logging::sanitize::sanitize_object` (SENSITIVE_KEYS rule), strips control characters, truncates to `MAX_BUNDLE_LOG_MESSAGE_LEN` (4096), then emits via `tracing::{error,warn,info,debug}!`. New in this spec: `fields-json` keys not in the manifest's declared `logs.fields` allowlist (SS1.3) are dropped **before** sanitization even runs (SS4), and the effective emission level is clamped to the community/tenant's configured ceiling (SS4) before the `tracing` macro call is chosen.

**Log injection / rendering (normative, Gemini condition 3):** the control-character strip is expanded from "control characters" to a precise set — `0x00`-`0x1F` (all C0 controls) except `\n`, which is retained but **encoded** (`\n` → literal two-character `\n` in the stored/emitted string, never a raw line-feed byte) so a bundle can never forge additional log lines or terminal escape sequences; `0x7F`-`0x9F` (DEL + C1 controls); ESC (`0x1B`, redundant with the C0 range but called out explicitly since it's the terminal/log-injection payload of concern); and the Unicode bidirectional-override code points `U+202A`-`U+202E` and `U+2066`-`U+2069` (RLO/LRO/PDF/RLE/LRE/LRI/RLI/FSI/PDI — the "Trojan Source" character class). This applies to both the `message` and every `fields-json` value, before truncation. **hub-webui renders bundle log content as escaped, untrusted text** (no raw HTML/markdown interpretation, no terminal-control interpretation) — the same trust boundary as any other user-supplied string reaching the UI; sanitization at the host is defense-in-depth, not a substitute for output-encoding at render time, and vice versa.

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
| Per-tenant actor/pipeline isolation *(Gemini condition 5)* | Each tenant's exporter is its own actor/task with its own queue, client, and backoff state (SS3.7's forwarder runs one pipeline instance per tenant, not a shared pipeline keyed by a runtime label) — no shared mutable state between tenants beyond the read-only `GrantSnapshot`/`TenantExporterSnapshot` config each reads independently |
| Final-serialization tenant assertion *(Gemini condition 5)* | Immediately before a batch is handed to a tenant's exporter client, assert every record in the batch carries that exact `tenant_id` — a mismatch drops the **whole batch**, increments `waddles.telemetry.tenant_export.assertion_failed_total{tenant}`, and pages/alerts (never silently drops one record and ships the rest); this is the last-line check behind §2.3's task-local scoping and §3.7's per-tenant actor isolation, not a replacement for either |
| Non-blocking IO *(Gemini condition 7)* | All OTLP export IO (batch send, backoff sleep, connection setup) is `async`/non-blocking (`tonic`/`hyper` async clients) — no blocking call anywhere in the emission or export hot path |

### 3.5 Security

| Control | Detail |
|---|---|
| TLS mandatory | Reject plaintext `http://` or unencrypted gRPC targets regardless of protocol choice — TLS 1.2+ (prefer 1.3), same floor as `security.md` |
| SSRF guard *(normative, Gemini condition 2 — expanded)* | A **custom dialer** resolves the hostname once, validates the resulting IP against the blocklist below, and connects **only to that validated IP** — the connector never re-resolves the hostname mid-connection (defeats DNS rebinding by construction, not by a second check). Blocked: IPv4 RFC1918 (`10/8`, `172.16/12`, `192.168/16`), loopback (`127/8`, `::1`), link-local (`169.254/16`, `fe80::/10`), the `169.254.169.254` cloud metadata address specifically, IPv6 unique-local (`fc00::/7`), and IPv4-mapped IPv6 addresses (`::ffff:0:0/96`) — checked against the *decoded* IPv4 form, not the outer IPv6 literal, since that's the classic mapped-address bypass. No redirect following at the HTTP client layer (OTLP export doesn't need it and redirects are a re-resolution vector). Same pattern/precedent as the webhook/JWKS receiver design (connections-credentials-design), now stated as the literal blocklist rather than by reference |
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

**Runtime isolation *(normative, Gemini condition 7):*** whichever option ships, per-tenant exporters run on a dedicated Tokio runtime (or a bounded task pool separate from the runtime serving bundle invokes/host-calls) — a slow or backed-up tenant exporter competing for the same scheduler as invoke-handling tasks is exactly the "dead exporter breaks the app" failure mode this spec's global-telemetry rule already forbids, now stated for the per-tenant case explicitly. The global exporter (`penguin_logging`'s existing OTLP pipeline) already runs off the request-handling hot path; the per-tenant forwarder (Option B) gets the same treatment as its own process/pool, not folded into a service's primary runtime.

---

## 4. Protections and filtering (all host-side)

| Protection | Mechanism |
|---|---|
| Declared-only instruments/labels | `metrics.*` calls checked against the manifest's `telemetry.metrics` block (SS1.3) before anything else; undeclared instrument → `not-declared` error + `waddles.telemetry.rejected_total{app_id, reason="undeclared_instrument"}` counter, never forwarded |
| Label cardinality cap *(normative, Gemini condition 1 — expanded)* | ≤4 label keys/instrument (manifest cap, SS1.3). Per `(tenant, app, instrument)`, a **bounded-memory tracker** (fixed-capacity map, ≤100 entries) holds the active label-value combinations with a per-entry hit counter; a new combination arriving at capacity **evicts the least-frequently-used entry** (LFU, not blind deny-new) rather than permanently locking in whichever 100 combinations happened to appear first — a bundle with a slowly-shifting label distribution (e.g. daily `pond` rotation) doesn't get stuck reporting stale series forever. The tracker's window resets **hourly** (counts zeroed, eviction candidacy re-established), bounding both memory (fixed capacity, never unbounded growth) and the lifetime of any one LFU ranking. An evicted/rejected combination's data point is dropped + counted (`rejected_total{reason="cardinality_evicted"}`), never forwarded |
| Value sanitization & length caps | Label values: strip control chars (same expanded set as §2.4: C0 minus encoded `\n`, C1, DEL, ESC, Unicode bidi overrides), cap 128 bytes, reject non-UTF8; log messages: existing `sanitize_bundle_log_message` pipeline, same expanded strip, `SENSITIVE_KEYS` redaction, 4096-char cap |
| PII/secret scrubbing *(normative, Gemini condition 4 — reordered)* | **Primary boundary:** the manifest's `logs.fields` allowlist (SS1.3) — any `fields-json` key not declared is dropped before anything else runs, full stop; this is the actual security boundary, not a courtesy filter. **Defense in depth, applied to every declared field's value and to `message`:** (1) `penguin_logging::sanitize::sanitize_object`'s `SENSITIVE_KEYS` rule; (2) regex scrub for email-shaped, phone-shaped, and token-shaped (`^[A-Za-z0-9_-]{20,}$`) values wherever they appear, not just under a sensitive key name; (3) a **high-entropy string check** (Shannon entropy above a threshold tuned against the platform's own UUID/token corpus) on any remaining string value — a high-entropy hit is redacted (replaced with `<redacted:high-entropy>`) rather than silently passed, on the reasoning that an unrecognized secret shape is more likely than an unrecognized benign one at that entropy level; UUID-shaped values are explicitly exempted from the entropy check (they're already the platform's tokenized identity form and would otherwise false-positive on every message) |
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

**Data residency (normative, Gemini condition 8):** the internal mirror above is a retention decision, not just a query-path one.

| Requirement | Detail |
|---|---|
| TTL cap on the internal mirror | Every record in the internal telemetry mirror (bundle logs/metrics, regardless of tenant) expires and is purged after a fixed retention window (default 30 days, config-tunable) — the mirror is an operational/support store, never an indefinite archive; this bounds both storage cost and the data-residency exposure this condition is about |
| Enterprise tenant opt-out | A tenant admin on an Enterprise-tier tenant may disable the internal mirror **for their tenant** entirely — once disabled, no new bundle telemetry for that tenant is written to the internal mirror (existing rows still age out under the TTL above; this is an opt-out of future collection, not a right-to-erasure mechanism, which is handled elsewhere per `critical-rules.md` PII Tokenization's statutory-rights carve-out) |
| hub-webui reflects the opt-out | With the internal mirror disabled, that tenant's telemetry tab in hub-webui is hidden (not shown-but-empty — the distinction matters: an empty tab reads as "no telemetry occurred," a hidden tab correctly reads as "this tenant chose not to collect it here") |
| Global export unaffected | The opt-out only removes the *internal mirror* copy; if the tenant separately runs their own per-tenant OTLP destination (SS3, also Enterprise), that fan-out is untouched — a tenant can have their own external destination while opting out of the platform's internal copy, and vice versa |
| Gating | Same two-gate pattern as SS3.6: `get_tier() == "enterprise"` plus a PostHog flag, default OFF; domain-bypass only, never an env var/CLI toggle |

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
| Yield-across-threads (Gemini condition 6) | Drive two concurrent invokes for different tenants on a multi-threaded Tokio test runtime, forcing task migration across worker threads mid-invoke (e.g. via an intentional `.await` on a cross-thread-scheduled future) → each invoke's `metrics`/`log` calls are still attributed to its own `InvokeScope` after the migration | FAIL if either invoke's telemetry is attributed to the other tenant/app after a thread hop |
| Cardinality eviction (Gemini condition 1) | Exceed an instrument's 100-combination cap with a shifting label distribution over a simulated multi-hour run → tracker memory stays bounded, LFU eviction occurs (not blind rejection of all new combinations), hourly reset observed | FAIL on unbounded memory growth or on the tracker never admitting a new combination after the first 100 |
| SSRF: pinned-IP dialer (Gemini condition 2) | Point a tenant destination at a hostname that resolves to a public IP at validation time but a private/metadata IP at connect time (simulated DNS rebinding) → connection is refused, not silently redirected | FAIL if the connection succeeds against the rebound private/metadata address |
| Log injection (Gemini condition 3) | Bundle logs a message containing a raw `\n`, a bidi-override code point, and an ESC sequence → stored/emitted record contains none of them unescaped; hub-webui rendering test confirms the log entry displays as inert text, not interpreted markup/control sequence | FAIL if any of the three reaches the sink un-neutralized, or renders as anything but text in the UI |
| Data-residency opt-out (Gemini condition 8) | Enterprise tenant disables the internal mirror → no new records for that tenant appear in the mirror after the toggle, hub-webui hides the tab, and the tenant's own external destination (if configured) is unaffected | FAIL if new records still land, the tab still renders, or the external destination stops receiving data |

All counts reported (examined/rejected/scrubbed/dropped/evicted), never a bare pass — per `critical-rules.md` Verification Integrity.

---

## 9. Phased implementation plan (agent-sized, ≤30 min each)

| Phase | Task | Component | Depends on |
|---|---|---|---|
| 0 | Add `metrics` WIT interface (SS1.1) to `wit/waddle-bundle/stage.wit`; regenerate bindings | WIT | none |
| 0 | `_INSTRUMENT_ID_RE` + `telemetry:` manifest block validation (SS1.3) in `bundle_manifest_v2.py` | hub-api | none |
| 0 | Add `telemetry.logs`/`telemetry.metrics` to the permission catalog module (SS5) | Rust (`bundle_capability_gate`) | permissions spec Phase 0 |
| 1 | `#[tracing::instrument]` on `Connection::invoke` (SS2.3) in `svc_process`; span-event bridge for `log.write` | Rust (svc_process) | Phase 0 |
| 1 | Same for `svc_action` | Rust (svc_action) | Phase 0 |
| 1 | Manifest `logs.fields` allowlist enforcement (as the *primary* scrub boundary, Gemini condition 4) + expanded control-char/bidi-override strip (Gemini condition 3) + log-level-ceiling config keys (SS2.4, SS4) | Rust (both stages) | Phase 0 |
| 1 | `InvokeScope` threaded via explicit argument or `tokio::task_local!`, never thread-local, through every capability-handler call path (Gemini condition 6) | Rust (both stages) | Phase 0 |
| 2 | `metrics::Host` impl in `bundle_executor/src/host/imports.rs` bridging to `CapabilityKind::Metrics`; declared-instrument/label validation host-side (SS4) | Rust (bundle_executor) | Phase 0 |
| 2 | `handle_metrics` in `svc_process`/`svc_action` `capabilities.rs`: counter/gauge/histogram emission via `penguin_logging`'s meter | Rust (both stages) | previous task |
| 2 | Bounded-memory, LFU-eviction, hourly-reset cardinality tracker per instrument (Gemini condition 1) | Rust (both stages) | previous task |
| 2 | High-entropy string check as scrubbing layer 3, behind the `logs.fields` allowlist + regex layers (Gemini condition 4) | Rust (both stages) | Phase 1 |
| 2 | Per-app/(tenant,app) rate limiter + byte budget reusing `UsageBatcher` (SS4) | Rust (both stages) | previous task |
| 3 | Aggregate per-app platform-wide cardinality cap (SS7 backstop) | Rust (both stages) | Phase 2 |
| 4 | `tenant_otel_destinations` migration + hub-api CRUD (`tenant:admin`) with header encryption via existing tenant DEK (SS3.3) | hub-api | tenant-envelope-encryption design |
| 4 | Data-plane hot-swap of `TenantExporterSnapshot` into existing `BUNDLE_CONFIG_POLL_SECONDS` loop | Rust (both stages) | previous task |
| 5 | Shared telemetry-forwarder (Option B, SS3.7) on its own dedicated runtime/pool (Gemini condition 7): per-tenant routing rule, per-tenant actor isolation, bounded queue/backoff | Rust or Collector config | Phase 4 |
| 5 | Custom pinned-IP SSRF dialer with the full blocklist (Gemini condition 2) | Rust (forwarder) | previous task |
| 5 | Final-serialization tenant-id assertion + alert-on-mismatch (Gemini condition 5) | Rust (forwarder) | previous task |
| 5 | Per-tenant export metrics (`sent`/`dropped`/`errors`/`queue_depth`/`assertion_failed`, SS3.4) | Rust (forwarder) | previous task |
| 5 | `get_tier()`/PostHog `waddles.tenant-otel-export` gate + graceful tier-lapse handling (SS3.6) | Rust (forwarder) + hub-api | previous task |
| 6 | hub-webui: tenant OTel destination config screen + Enterprise-lapse banner; render bundle log content as escaped/untrusted text (Gemini condition 3) | React | Phase 4 |
| 6 | Internal-mirror TTL purge job + Enterprise opt-out toggle + tab-hiding (Gemini condition 8) | hub-api + React | Phase 0 |
| 7 | hub-api scoped read endpoints (SS6): global/tenant/community/vendor | hub-api | Phase 2 |
| 7 | hub-webui: telemetry views per role (SS6) | React | previous task |
| 8 | Smoke test: telemetry gate (SS8 row 1) | Rust/CI | Phase 2 |
| 8 | Negative tests: undeclared label, PII scrub, rate limit, cross-tenant leak, tier lapse (SS8 rows 2-6) | Rust/CI | Phase 5, Phase 7 |
| 8 | Gemini-condition tests: yield-across-threads, cardinality eviction, SSRF pinned-IP/rebinding, log injection/rendering, data-residency opt-out (SS8, remaining rows) | Rust/CI + Playwright | Phase 2, Phase 5, Phase 6 |

Phases 0-3 (WIT + catalog + emission + host-side gate) have no dependency on Phase 4-6 (tenant fan-out) and can land and ship independently — per-tenant export is a pure Enterprise add-on to an already-complete global telemetry path.

---

## 10. Open questions (not blockers, flagged for follow-up)

- **OTLP backend product choice** — this spec is backend-agnostic by design (`critical-rules.md`: destination always configurable); whether the platform's own default (e.g. Killkrill, if adopted) is used for the global destination, and whether it natively supports the tenant/community filtering SS6 requires, is a separate infra decision this spec doesn't block on.
- **Exact quota numbers** (SS4/SS5 defaults: 100 series/instrument, hourly LFU-reset window, 500 lines/min, 2,000 points/min, 2,000-series/app aggregate cap, high-entropy threshold, internal-mirror TTL default of 30 days) are starting points, not load-tested — Phase 3/8 should tune them against real fixture load before the aggregate cap ships as a hard gate.
- **Shared forwarder implementation** (SS3.7 Option B) — OTel Collector contrib `routing`/`groupbyattrs` processors vs. a small purpose-built Rust forwarder is an implementation-phase call, not designed further here.
