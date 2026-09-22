# Waddles Rust Data Plane + Sandboxed WASM App Bundles — Design Specification

**Date:** 2026-09-14
**Status:** Approved design — pending user review of written spec
**Scope:** `core/svc_ingest`, `core/svc_process`, `core/svc_action`, `core/svc_streaming`, two new binaries (`bundle-executor`, `bundle-compiler`), five shared crates in `penguin-libs`, one Python SDK (`waddle-sdk`), the Helm chart, and the bundle authoring contract.
**Ships:** before the v3.0 MVP, as a single clean cut-over. No Python fallback path is built.

---

## 0. Document map and sources

### 0.1 Grounding research (git-ignored working files)

Two read-only inventories were produced for this design. They are **git-ignored working files** under the `gazer-mobile-v2` worktree's `.superpowers/` directory, not part of repository history, and are cited here for provenance only:

| Working file (git-ignored, not in the repo) | What it grounds |
|---|---|
| `.superpowers/research/core-services-inventory.md` | The current four services, spine keys and cadence, envelope dataclasses, bundle contract, manifest schema, Module→Feature→App model, ingest intake surface, connectors, sizing. Sections 2–5 are the load-bearing ones for this spec. |
| `.superpowers/research/penguin-libs-inventory.md` | Existing `penguin-libs` Rust crates (`penguin-licensing`, `penguin-rpc`, `penguin-h3-tower`), publishing/branching conventions, per-crate lint and CI conventions, `penguin-dal`'s surface, proposed crate placement. |

Because both are git-ignored, every claim this spec takes from them is additionally traceable to a committed path, cited inline below.

### 0.2 Documents this spec supersedes or amends

| Document | Relationship |
|---|---|
| `docs/APP_BUNDLE_AUTHORING.md` (header: **FROZEN**, v1) | **Superseded.** Rewritten as v2 in milestone M6. v1's entrypoint contract survives semantically for `process`/`action`; the `ingest` stage stops being bundle-pluggable, and `importlib` in-process loading is replaced by WASM components. |
| `docs/plans/2026-08-31-app-bundle-sdk-design.md`, line 612 — *"Native-script sandboxing model — subprocess, WASM, or a separate pod per activation … needs an explicit call"* | **Resolved by this spec.** The explicit call is: WASM components inside a credential-less executor that runs as its own gVisor-sandboxed Deployment per stage (topology A, §11.2). |
| `docs/plans/2026-08-31-v3-sccebm-program-plan.md` open item #9, lines 407-408 — *"native = first-party only; community-native deferred until real sandbox"* | **Closed by this spec.** Community-authored native bundles become possible because the sandbox now exists. |
| `docs/plans/2026-08-26-v3-scbm-apps-design.md` | **Retained.** The Module → Feature → App model, the binding/resolution ladder and the third-party (`webhook_push`/`rest_pull`) trust boundary are unchanged. |
| `docs/plans/2026-08-31-svc-streaming-design.md` | **Retained.** `core/svc_streaming`'s Rust build is the reference implementation the other three services imitate; only the Python alpha is deleted. |

### 0.3 Committed sources of truth cited by this spec

- `libs/flask_core/flask_core/stream_pipeline.py` — envelope dataclasses, key builders, `BUNDLE_STAGES`, `PROCESS_TARGET_APP_ID_KEY`.
- `libs/flask_core/flask_core/app_manifest.py` — v1 manifest schema, `KNOWN_MODULES`, `KNOWN_SURFACES`, id/SemVer regexes, reason codes.
- `libs/flask_core/flask_core/stage_runner.py` — `BundlePoller`, `load_entrypoint`.
- `libs/flask_core/flask_core/bundle_runtime.py` — `get_bundle_dal()` / `get_bundle_context()`, the only sanctioned bundle side channel.
- `libs/flask_core/flask_core/feature_contract.py`, `.../entitlement.py`, `.../feature_flags.py` — the two-gate flag/license model and the `waddles.{module}.{feature}` flag-key rule.
- `core/svc_ingest/{app,runner,fanout,eventsub,outbound_drain,socket_lease,supervisor}.py`, `core/svc_ingest/receivers/*.py` — intake surface and connector behaviour to port.
- `core/svc_process/runner.py`, `core/svc_action/runner.py` — stage loops, hardcoded hooks, retry/backoff and audit semantics.
- `core/svc_streaming/{Cargo.toml,deny.toml,Dockerfile.rust,README.md}`, `core/svc_streaming/src/telemetry.rs` — the Rust service template (stack, pins, lints, OTel wiring, container shape).
- `hub_api/services/distribution_service.py`, `hub_api/blueprints/v1/distribution.py` — the distribution API this spec extends.
- `k8s/helm/waddlebot/values.yaml`, `k8s/helm/waddlebot/templates/svc-{ingest,process,action,streaming}.yaml` **(legacy identifier — the chart directory and release name are not renamed by this project)** — chart shape and values keys.
- `.github/workflows/rust-svc-streaming.yml` — the per-service Rust CI gate set to replicate.
- `docs/APP_BUNDLE_AUTHORING.md` §5 — the bundle-facing DAL surface as it exists today (`await dal.execute(sql, params)`, `get_bundle_context()`), superseded by `penguin-dal` per D21a.
- `/home/penguin/code/penguin-libs/packages/python-dal/src/penguin_dal/__init__.py` — the `penguin-dal` public API the bundles migrate to and the SDK facade reproduces.

---

## 1. Goals & non-goals

### 1.1 Goals

| # | Goal | Verified by |
|---|---|---|
| G1 | The four data-plane services are Rust, on the `core/svc_streaming` Axum/tokio/SeaORM/OTel template. | `find core -name Cargo.toml` returns exactly four crates; zero `.py` files remain under `core/svc_{ingest,process,action,streaming}/`. |
| G2 | Every app bundle runs as a WASI 0.2 WebAssembly component inside a credential-less executor Deployment — rootless, all capabilities dropped, read-only rootfs, `RuntimeDefault` seccomp; an optional gVisor `RuntimeClass` may be layered on where the cluster supports it, default **off** (D32). | Negative sandbox tests (§14.6): arbitrary outbound connections are refused by the NetworkPolicy, the executor holds no stage credentials, and undeclared egress is denied and counted. |
| G3 | Existing Python bundles keep running unchanged apart from their database-access lines, which move to the `penguin-dal` API first (D21a). | The DAL migration lands and every bundle's pytest suite passes **natively** before compilation work starts; then CI compiles every file under `bundles/python/` with the real compiler and replays golden events through the executor, with the same suites still passing. |
| G4 | Bundles are pluggable at `process` and `action`. `ingest` is fixed code. | `bundle.yaml` v2 rejects `stages.ingest` with reason code `ingest_not_pluggable`; svc-ingest links no executor. |
| G5 | Ingest accepts platform-specific inputs **and** a generic signed webhook + authenticated REST intake. | §10's endpoint table is covered by integration tests including auth failure, replay, oversize body and rate-limit paths. |
| G6 | The spine is at-least-once with a DLQ and bounded queues, on the existing Valkey key scheme and envelope JSON. | Golden fixtures (§14.1) asserted identical by both `flask_core` (Python) and `penguin-spine` (Rust); a crash-mid-processing test re-delivers. |
| G7 | Multi-language bundles: Python, Rust, JavaScript/TypeScript ship with SDK + compiler recipe + example (Tier 1); any WASI 0.2 component implementing the WIT world is accepted prebuilt (Tier 2). | One example bundle per Tier 1 language passes the WIT conformance suite; a hand-built Tier 2 component uploads, validates, loads and runs. |
| G8 | Bundle artifacts are content-addressed, signed, verified before load, and hot-swapped without a restart. | Digest-mismatch test refuses the load, keeps the previous version serving, increments `waddles_bundle_digest_mismatch_total`. |
| G9 | Postgres and Valkey are authenticated and TLS-encrypted by default, in every environment. | Startup refuses a plaintext/unauthenticated URL while `security.transport.tls`/`.auth` are true; flipping either to false emits the warning, sets `waddles_insecure_transport{component}` to 1, and reports `transport: insecure` on `/health`. |
| G10 | Logs, metrics **and** traces are emitted from all four services to an env-configurable OTLP endpoint, plus penguin logging. | Smoke-test telemetry gate (§14.7) asserts ≥1 log record, ≥1 metric data point, ≥1 histogram, ≥1 span, and prints the counts. |
| G11 | End-to-end latency stays within 3 s (text) / 5 s (A/V) from input to response. | `waddles_stage_latency_seconds` plus an end-to-end histogram asserted against the SLA in the alpha e2e run. |

### 1.2 Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Rewriting the control plane. `hub_api` stays Python 3.13 + Quart. | Not in-line of traffic (`critical-rules.md` Data Plane boundary table). |
| N2 | Rewriting `core/svc_presentation` or its `presentation` surface. | Outside the four-service scope; `presentation` stays a non-script, client-side surface. |
| N3 | Keeping a Python execution path as a fallback. | Decision D3: clean cut, v3 is a major-version jump. |
| N4 | Changing the envelope JSON field names, the Module→Feature→App model, or the chart's service/value names. **The Valkey key scheme does change** — per-ingest-source streams replace per-bundle process lists (D23, D24) — but the envelope carried inside them is byte-identical. | Keeps the cut-over an implementation change rather than a re-modelling of the domain; the transport change is deliberate and is the one place this spec touches the key scheme. |
| N5 | Changing the third-party (`webhook_push`/`rest_pull`) execution model. | Already out-of-process across a network hop. |
| N6 | Per-`(tenant, stage)` Valkey ACL users. | Per-**service** ACL users are in scope (§11.6); the per-tenant scheme in `stream_pipeline.py`'s docstring remains design-doc-only. |
| N7 | Replacing the 5 s distribution poll with a push mechanism. | Unchanged; it governs bundle-set refresh only, not event latency. |
| N8 | A marketplace billing/review redesign. | `hub_api/services/marketplace_*` is untouched except for the install hooks in §9. |

---

## 2. Decisions

Every row was decided by the human product owner during the 2026-09-14 design session and is authoritative.

| # | Decision | Rationale | Decided by / when |
|---|---|---|---|
| D1 | Convert `svc_ingest`, `svc_process`, `svc_action`, `svc_streaming` to Rust. | All four sit in-line of traffic; `critical-rules.md` Data Plane makes tier, not volume, the test. | Human, 2026-09-14 |
| D2 | Ship before the v3.0 MVP. | Doing it after means rewriting a shipped surface and migrating live tenants. | Human, 2026-09-14 |
| D3 | Cut over all four at once — clean cut, no Python fallback. | v3 is a major-version jump; a dual-path period doubles the test matrix and hides drift. | Human, 2026-09-14 |
| D4 | Keep the Module → Feature → App bundle architecture; implement Apps as WASM. | The logical model is sound and already enforced in `flask_core`; only the execution mechanism was unsafe. | Human, 2026-09-14 |
| D5 | Bundles allowed at every stage **except** ingest. | Ingest holds platform credentials and long-lived sockets — exactly the surface that must not run foreign code. | Human, 2026-09-14 |
| D6 | Ingest exposes a generic webhook + REST intake alongside Twitch EventSub webhook and EventSub websocket input. | Removing ingest bundles removes the only extension point; a signed generic intake restores it without executing foreign code. | Human, 2026-09-14 |
| D7 | Existing Python bundles stay byte-for-byte unchanged — **except their database-access lines** (D21a) — and are compiled with `componentize-py` plus a same-API `waddle-sdk` shim. | Bundle authors and their test suites must not be disturbed by a host rewrite; the one carve-out is a correction those bundles already owed. | Human, 2026-09-14 (amended) |
| D8 | **All** bundles run as WASM components — first-party and third-party alike. | One execution path; no privileged tier that skips the sandbox. | Human, 2026-09-14 |
| D9 | Sandbox topology A: a per-stage, credential-less `bundle-executor` running as **its own Deployment** (`svc-process-executor`, `svc-action-executor`) — rootless, `allowPrivilegeEscalation: false`, all capabilities dropped, read-only rootfs, `RuntimeDefault` seccomp — with a default-deny `CiliumNetworkPolicy` allowing only executor→stage on one mTLS port and executor→bucket egress; all host calls capability-scoped. A gVisor `RuntimeClass` (`runsc`) may **optionally** be layered on top where the cluster can host it — default **off** everywhere (D32). | Defence in depth: a WASM escape lands in a workload holding nothing worth stealing and able to reach nothing but the mTLS host-API port and the artifact bucket. Where present, gVisor additionally intercepts syscalls in user space before they reach the host kernel; §18 R3 found that posture undeliverable in every environment Waddles runs (no `runsc` on MicroK8s, none survivable on DOKS), so it is opt-in rather than assumed (D32). | Human, 2026-09-14 (revised after the sandbox spike; gVisor made opt-in by D32, 2026-09-21) |
| D10 | The compiler Job's untrusted `build` container runs rootless, no credentials, all capabilities dropped, read-only rootfs, `RuntimeDefault` seccomp, with no network except the bucket and the hub-api callback (reached only via the trusted `publisher` container, §4.6). The same optional gVisor `RuntimeClass` as D9 may be layered on top where available — default **off** (D32). | `componentize-py` executes bundle top-level code at build time, so compilation is itself untrusted-code execution — the same layered defence as the executor, not a lesser one. | Human, 2026-09-14 (revised after the sandbox spike; gVisor made opt-in by D32, 2026-09-21) |
| D11 | Tier 1 languages (SDK + compiler recipe + example): Python, Rust, JavaScript/TypeScript. Tier 2: any language producing a WASI 0.2 component implementing the WIT world, uploaded prebuilt. | Covers the realistic author population without committing to maintain every toolchain. | Human, 2026-09-14 |
| D12 | Source uploads preferred and scanned (SAST, dependency audit, secrets, Skauswatch when configured). Prebuilt components accepted with a permanent "not security-scanned" warning/badge, governed by global-admin setting `bundles.allow_prebuilt` (default on). | Scannability is a real security property; refusing prebuilt entirely would exclude Tier 2 languages. | Human, 2026-09-14 |
| D13 | Manifest gains an `egress` section (FQDN or wildcard host, optional method list), enforced by the host with SSRF rules on top, plus a tenant-level global denylist. | Network reach must be declared and reviewable at install time, not discovered at runtime. | Human, 2026-09-14 |
| D14 | Compiled components are content-addressed by SHA-256; the digest is stored on the `app_catalog` version row; pods verify the digest **and** a deploy-key signature on the bucket sidecar before loading, refusing on mismatch. | The bucket is not a trust root; hub-api's DB plus a signature is. | Human, 2026-09-14 |
| D15 | Artifacts live in an S3-compatible bucket: MinIO by default, Nest when configured. | Already the pattern `svc_streaming` uses (`object_store` crate) for recordings. | Human, 2026-09-14 |
| D16 | Pods poll the bucket roughly every minute and hot-swap. | Simple, outage-tolerant, and no inbound control channel into a data-plane pod. | Human, 2026-09-14 |
| D17 | DRY via shared crates in `penguin-libs`: `penguin-spine`, `penguin-bundle-host`, `penguin-logging`, `penguin-connectors` (one crate per platform); `penguin-licensing` gains CI + publish jobs. | Four services need the same spine, host API, logging and connectors; duplicating them is how they drift. | Human, 2026-09-14 |
| D18 | Valkey naming throughout (not Redis), except where naming the wire protocol itself. | House naming; the product deploys Valkey. | Human, 2026-09-14 |
| D19 | Authentication **and** TLS required by default for Postgres and Valkey. | Credentials and event bodies cross the cluster network; default-off encryption is how it stays off. | Human, 2026-09-14 |
| D20 | The TLS/auth opt-out is a normal chart/values setting in **every** environment (`security.transport.tls`, `security.transport.auth`, both default `true`). When either is false: a loud warning at every startup, `waddles_insecure_transport{component}` = 1, `/health` reports `transport: insecure`. No environment's values file rejects it. | Operators must be able to run without TLS (bare-metal labs, constrained edge) without editing code; visibility, not prohibition, is the control. | Human, 2026-09-14 (amendment) |
| D21 | The `waddle-sdk` ships **one** database facade: the `penguin-dal` public API, implemented over the WIT `db` import. No `flask_core.database.AsyncDAL` facade and no pydal facade exist. | Waddles' DAL is `penguin-dal`; shipping two surfaces would institutionalize the legacy one. | Human, 2026-09-14 (correction) |
| D21a | Every existing Python bundle that imports `flask_core.database.AsyncDAL` or reaches a DAL through `get_bundle_dal()` is **migrated to the `penguin-dal` API first**, in Python, with its own pytest suite updated and passing natively, before any WASM compilation work. Bundle logic and entrypoint signatures are otherwise untouched. | Those bundles should already have been on `penguin-dal`; migrating them is a correction, not new scope, and it removes the need for a compatibility facade entirely. | Human, 2026-09-14 (supersedes "byte-identical" for DB-access lines only) |
| D21b | The compiler **rejects** any bundle that still imports `flask_core.database` or `pydal`, with a message naming the module and pointing at the `penguin-dal` equivalent. | A gate that cannot be bypassed is what stops the legacy surface from creeping back in through a new bundle. | Human, 2026-09-14 |
| D23 | **The spine is Valkey Streams, not lists.** Ingest receives or polls each event once and `XADD`s it once onto the stream of the ingest source it came from; every subscribing process bundle reads it through its own consumer group. **Supersedes** the list-based design: the per-bundle `LPUSH` copies, the `LMOVE` processing keys, the consumer index, the heartbeat sorted set and the reaper are all removed, replaced by the pending-entries list and `XAUTOCLAIM`. | One write per event regardless of how many bundles subscribe, real per-bundle isolation of failure and lag, and at-least-once from the server rather than from hand-rolled bookkeeping. The repo already contains a tested implementation of this model (`libs/flask_core/flask_core/stream_pipeline.py:341-1047`), gated off. | Human, 2026-09-14 |
| D24 | **Stream granularity is per ingest source**, and access is **manifest-requested, hub-api-granted**: `consumes` is a request, hub-api resolves it into explicit `app_stream_grants` rows and creates consumer groups only on granted streams, the admin sees the grant list in words at install, and the stage reads only granted streams. Admins may revoke individual grants; the Valkey ACL stays pattern-scoped, so the selectivity is logical and lives in the stage. | Per-platform streams would force "all Twitch or none"; per-source plus explicit grants is what lets an operator give a bundle one channel. Bundles hold no Valkey connection, so the stage is the only possible enforcement point and the spec says so rather than implying Valkey enforces it. | Human, 2026-09-14 |
| D27 | **The compiler Job splits into an untrusted `build` container and a trusted `publisher` container** sharing one `emptyDir`: `build` runs the per-language build (which executes bundle code) rootless, all capabilities dropped, with no credentials and **no network at all** — the same optional gVisor layer as D9/D10 may sit under it, default off (D32); `publisher` runs on the default runtime, validates the component, computes the digests, precompiles, signs, uploads and `INSERT`s the version row. **The hash is never computed inside the sandbox that ran bundle code.** The `app_versions` digest table has **exactly two writers** — `waddles_publisher` and hub-api — with every write audited (role, key, old/new digest) and no privileges for any other role; activation and rollback live in the hub-api-owned `app_active_versions`. hub-api's artifact callback is a notification that triggers an independent re-hash and audit-logged cross-check, not the digest authority. Executors reconcile purely by digest. | A build stage that both runs untrusted code and measures its own output can choose the hash it is judged by. Splitting the containers separates "executes guest code" from "holds credentials" so no component has both, and the two-writer rule plus audit makes an unexpected write detectable rather than invisible. | Human, 2026-09-14 |
| D28 | **Least User Access via RBAC.** Every Postgres role and every Valkey ACL user gets exactly the privileges its job needs, generated from two versioned, normative matrices (`config/postgres/rbac-matrix.yaml`, `config/valkey/acl-matrix.yaml`) and asserted equal to the live configuration by CI, with non-zero denominators. | Least privilege stated once and enforced mechanically, rather than re-derived by hand in each service and drifting. A matrix that CI compares against reality is the difference between a policy and a wish. | Human, 2026-09-14 |
| D26 | **Install is an explicit permission-consent step, and the runtime enforces the approval rather than the manifest.** The admin sees the bundle's full contract — granted streams in words, egress hosts and methods, tables with read/write, host capabilities actually imported, `routes_to`, limits, scan status, tier and flag — approves it, and the approval is recorded with a `permission_hash` over the canonical summary. Grants, egress allowlists, table roles and capability wiring are generated from that record. Widening on upgrade requires re-approval against a diff; narrowing auto-approves with an audit entry; headless installs must pass the expected hash or fail closed. | A manifest is a request from the bundle's author; an approval is a decision by the operator. Deriving runtime authorization from the manifest would let a new version widen access silently, which is precisely what the consent step exists to prevent. | Human, 2026-09-14 |
| D25 | **Action streams are strictly per bundle** — one stream, one consumer group, never read on another bundle's behalf — and the `_target_app_id` cross-bundle redirect becomes a **declared, approved capability** (`routes_to`, exact ids, no wildcards), enforced by the stage, which is also the only writer. | Ingest streams are shared read-only platform data; action envelopes are a bundle's own output and routinely carry its private state. The asymmetry is deliberate, and the one cross-bundle path is visible at install rather than implicit. | Human, 2026-09-14 |
| D22 | **Naming: Waddles is the product and repo name; `waddlebot` survives only as the legacy identifiers listed here.** The repo becomes `penguintechinc/waddles` (local clone `~/code/waddles`), images become `ghcr.io/penguintechinc/waddles/<service>`, the Kubernetes namespace and in-cluster DNS become `waddles` (`hub-api.waddles.svc.cluster.local`), and chart Secrets become `waddles-*`. Flag keys (`waddles.*`) and Valkey keys (`waddles:*`) already used the name. **The complete list of surviving `waddlebot` literals:** (1) the Helm chart directory and release name `k8s/helm/waddlebot`, which this project does not rename (N4 keeps the chart's names and values stable); (2) the Postgres `DB_NAME` default `waddlebot`, which this project does not migrate; (3) the legacy `waddlebot:stream:*` / `waddlebot:dlq:*` key prefixes belonging to the unused `flask_core.stream_pipeline.StreamPipeline` class, which this spec does not use and does not rename; (4) Python package paths and the scratchpad path of the sandbox spike report. Every other occurrence is Waddles. | One product name, and a short, explicit list of the places a rename would mean a migration this project is not doing. | Human, 2026-09-14 |
| D29 | **Webhook intake source restriction (user review 3).** Every generic webhook source (`POST /intake/webhook/{tenant}/{source}`) must configure at least one of a source-IP allowlist, a bearer token or HTTP basic credentials in addition to the mandatory per-source HMAC signature; hub-api refuses to activate a generic source configured with none of the three. Twitch EventSub and Kick webhooks additionally require the client address to forward-confirm (FCrDNS) to `twitch.tv` / `kick.com`, or match an operator-configured static CIDR allowlist, on top of each platform's own signature scheme (§4.1.1). | A per-source secret is a single point of failure once it leaks or is guessed; a signature alone cannot tell a forged request from a genuine one once the secret is compromised. An origin or auth factor that travels out-of-band from the secret closes exactly that gap: an attacker holding only the secret still cannot originate from `twitch.tv`, land inside an allowlisted CIDR, or present a bearer/basic credential they were never given. Alternatives rejected: **signature-only** (the prior design — a leaked or guessed per-source secret was sufficient on its own to forge any generic or platform webhook); **IP-only without signature** (drops per-message tamper-evidence and is brittle against a sending platform's own IP rotation, so it was rejected in favor of requiring both). | Human, 2026-09-14 (review 3) |
| D30 | **Workstream identity, end-to-end trace, and the tenant wall (user review 3).** Every configured ingest source gets a hub-api-owned **workstream** (`workstreams`, 1:1 with `intake_sources`, owned by exactly one tenant and one community or the tenant-wide `_tenant` scope). svc-ingest mints `workstream_id`, a fresh `event_id`, a W3C `trace` (a new trace per inbound event) and an optional `session_id` (the platform's own connection/session, when it has one) onto every envelope from its source registry alone, never from payload, and signs the binding with `binding.mac = HMAC-SHA256(k_binding[kid], tenant‖community‖workstream_id‖event_id‖trace_id)` under a key held only by the Rust stage services. Every stage verifies the MAC and the tenant/community/grant/approval chain on every read, before any other processing; a failure is `error.kind = "tenant_boundary"`, DLQ'd, never retried, counted (`waddles_tenant_boundary_violations_total`) and audited. Bundle output can never carry or overwrite these fields — the stage copies them from the input envelope unconditionally — and `routes_to` (D25) is refused cross-tenant both at approval and independently at runtime. Host calls (`db`, `kv`) were already tenant-scoped by construction (`SET LOCAL waddles.tenant`/`waddles.community`, §7.4; scoped keys, §6.2); D30 makes the envelope driving them tamper-evident. Full spec: §5.11; envelope changes §6.1.2 (schema bumped to `2`, no dual-read — D3). | A session trace that can be followed end to end, and a tenant boundary that holds even against a malicious bundle, a compromised stage, or a forged Valkey entry — not just against a well-behaved one. Binding tenant/community/workstream cryptographically to the event, rather than trusting that every reader sources them correctly from the key (§5.10's prior invariant), closes the gap between "the design says never from payload" and "a bug or an escape can't make that false." | Human, 2026-09-14 (review 3) |
| D31 | **Workstream usage metering (user review 3).** Every stage records usage per `(tenant_id, community_id, workstream_id, stage, app_id)` — events, bundle invocations, fuel/CPU-ms, host calls by kind, actions delivered, outbound bytes, and svc-streaming's media-minutes — batched onto `waddles:usage` (stages `XADD`-only) at most every `metering.flushIntervalSeconds` (default `10`). hub-api's aggregator writes `workstream_usage_hourly` (append-only) and exposes a per-community admin usage view. Usage is never an OTel metric label (cardinality); the usage table is the billing source of truth. No charging, quota or enforcement ships now — per-workstream pricing, if adopted, is a future commercial metering axis alongside nodes/seats, gated by the license server like any other entitlement. Full spec: §5.12, tables §6.11–§6.12. | Charging per workflow stream and tying that to a community is a data-modeling decision today, not a billing feature today — the data has to exist in the right shape before there is anything to charge against, and building the recording path now is far cheaper than reconstructing history later. | Human, 2026-09-14 (review 3) |
| D32 | **gVisor dropped from the default sandbox posture — opt-in only. Revises D9/D10.** §18 R3's platform findings (2026-09-21) close both outstanding halves negative: MicroK8s has no `gvisor` addon and never has (`kata` is its sandboxed-runtime addon; §12.2.1's `microk8s enable gvisor` conflated MicroK8s with minikube), and DOKS — gamma and production — cannot host `runsc` at all, installer DaemonSet included, because DigitalOcean's node reconciler reverts node-level container-daemon changes on every node replacement. The default posture is deliverable in **none** of the environments Waddles runs. `sandbox.gvisor.enabled` now defaults **`false`** everywhere; clusters that already have `runsc` (GKE Sandbox especially) may still turn it on at no cost, and startup no longer fails closed when gVisor is requested but absent (§12.2). Resolves §19 Q6 (option c). | Every other sandbox layer is unchanged and now carries more weight: the WASM sandbox itself, the credential-less executor as its own Deployment, rootless, all capabilities dropped, read-only rootfs, `RuntimeDefault` seccomp, and the default-deny `CiliumNetworkPolicy` with its two allowed destinations. §11 is re-rated honestly on the remaining layers rather than left implying protection gVisor no longer delivers by default (§11.1–§11.3). Cost: a native-code escape from wasmtime now reaches the host kernel's syscall surface directly in the default configuration, mitigated by the remaining layers rather than intercepted first in a user-space kernel. | Human, 2026-09-21 |

---

## 3. Architecture

### 3.1 System diagram

```
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │                       CONTROL PLANE — Python 3.13 / Quart                     │
  │  hub-api                                                                      │
  │    registries: app_catalog · app_activations · app_tenant_availability        │
  │    GET  /api/v1/distribution/bundles?stage=…     (5 s poll, unchanged cadence)│
  │    POST /api/v1/apps/{app_id}/versions           (install: source | prebuilt) │
  │    marketplace install/review · global setting bundles.allow_prebuilt         │
  └───┬───────────────────────────────┬───────────────────────────────┬──────────┘
      │ creates K8s Job               │ serves bundle set + digests   │ records
      │ (one per uploaded version)    │ (5 s poll from each stage)    │ digest on
      v                               │                               │ version row
  ┌───────────────────────────────┐   │                               │
  │ bundle-compiler   (K8s Job)   │   │                               │
  │  ┌─────────────────────────┐  │   │                               │
  │  │ gVisor optional, off    │  │   │                               │
  │  │ by default · egress:    │  │   │                               │
  │  │  bucket + hub-api only  │  │   │                               │
  │  │  scan → compile →       │  │   │                               │
  │  │  validate → digest      │  │   │                               │
  │  └─────────────────────────┘  │   │                               │
  └───────────┬───────────────────┘   │                               │
              │ PUT component + signed sidecar                        │
              v                                                       │
  ┌──────────────────────────────────────────────────┐                │
  │ S3-compatible bucket  (MinIO default / Nest)     │<───────────────┘
  │   bundles/{app_id}/{version}/{sha256}.wasm       │
  │   bundles/{app_id}/{version}/{sha256}.json (sig) │
  └───────┬──────────────────────────┬───────────────┘
          │ ~60 s poll, fetch changed digests only
          │                          │
══════════╪══════════════════════════╪═══════════════════════════════════════════
          │        DATA PLANE — Rust (Axum · tokio · SeaORM · OTel)
 platforms│                          │
 ─────────┼──────────────┐           │
 Twitch EventSub (webhook│/websocket)│
 Twitch IRC · Discord GW │           │
 Slack Socket Mode       │           │
 YouTube poll · Kick     │           │
 POST /intake/webhook/…  │           │
 POST /intake/events     │           │
          ┌──────────────v────────────────┐
          │ svc-ingest        :8200        │   NO executor, NO bundles
          │  fixed per-platform normalizers│   holds every platform credential
          │  generic intake + rate limits  │
          │  socket leases · supervisor    │
          │  Twitch outbound relay (BLMOVE)│<───────────────┐ outbound relay
          └──────────────┬─────────────────┘                │ (dedicated conn)
                         │ ONE XADD per event               │
                         v                                  │
          ╔═══════════════════════════════════════╗         │
          ║ Valkey Streams (TLS + ACL per service) ║         │
          ║  {scope}:src:{platform}:{src_id}:events ║       │
          ║     one group per granted bundle        ║       │
          ║  {scope}:app:{app_id}:action            ║       │
          ║     one group: {app_id}                 ║       │
          ║  waddles:dlq:{stage}                    ║       │
          ╚═══════════════════════════════════════╝         │
                   │ XREADGROUP (granted streams only)      │
                         v                                  │
          ┌────────────────────────────────┐                │
          │ svc-process       :8201        │                │
          │  built-ins: moderation gate,   │                │
          │   enforcement routing,         │                │
          │   cross-app routing            │                │
          │  host API listener      :8301  │                │
          │   (mTLS, capability-scoped)    │                │
          └───────────────▲────────────────┘                │
                          │ length-prefixed frames over TLS │
                          │ (NetworkPolicy: this port only) │
            ┌─────────────┴──────────────────────────┐      │
            │ Deployment: svc-process-executor       │      │
            │ rootless · gVisor optional (off)       │      │
            │ credential-less · ro rootfs · caps ALL │      │
            │ dropped · RuntimeDefault seccomp       │      │
            │ egress: stage :8301 + bucket ONLY      │      │
            │   wasmtime instance pool               │      │
            │   ┌──────────┐ ┌──────────┐            │      │
            │   │ bundle A │ │ bundle B │  … WASM    │      │
            │   └──────────┘ └──────────┘            │      │
            └──────────────────┬─────────────────────┘      │
                               │ fetch components by digest │
                               └──▶ S3 bucket (read-only)   │
                          host calls (context/http/kv/db/   │
                          relay/flags/log/clock) are         │
                          answered by the stage on :8301     │
                       Valkey                               │
                          │                                 │
                          v                                 │
          ┌────────────────────────────────┐                │
          │ svc-action        :8202        │                │
          │  built-in platform senders     │──── Twitch ────┘
          │  retry_with_backoff · audit log│──── Discord/Slack/YouTube/Kick ──▶
          │  host API listener      :8302  │
          └───────────────▲────────────────┘
                          │  ┌──────────────────────────────┐
                          └──┤ Deployment: svc-action-      │
                             │ executor (optional gVisor)   │
                             └──────────────────────────────┘

          ┌────────────────────────────────┐
          │ svc-streaming     :8208        │  unchanged Rust service
          │  RTMP/SRT/WHIP in              │  Python alpha deleted
          │  HLS/WHEP/relay/record out     │  no bundles, no executor
          └────────────────────────────────┘
```

### 3.2 Trust zones

| Zone | Holds | Reachable from |
|---|---|---|
| **Control plane** (hub-api) | Registries, install approvals, bundle digests, per-source intake secrets, global settings. | Operators, webui, stage runners (read-only distribution API with a `distribution:read`-scoped JWT). |
| **Stage runner** (svc-ingest / svc-process / svc-action / svc-streaming) | Platform credentials, Postgres roles, Valkey ACL credentials, the egress HTTP client, secret resolution. | Cluster network; its own executor Deployment, over one mTLS host-API port. |
| **Executor** (`svc-process-executor` / `svc-action-executor` Deployments; gVisor `runsc` optional, off by default — D32) | Nothing. No stage credentials, no Valkey or Postgres access, no writable filesystem beyond a private scratch `emptyDir`, read-only rootfs. Holds only its client certificate for the host-API port and read-only bucket credentials. | Nothing inbound — the NetworkPolicy allows no ingress to the executor at all; it only initiates to the stage's host-API port and to the bucket. |
| **Bundle** (WASM component) | Only what the WIT imports grant, scoped by its manifest. | The executor's wasmtime store. |
| **Compiler `build`** (init container; gVisor `runsc` optional, off by default — D32) | Nothing. No credentials, and its NetworkPolicy allows no egress at all. Runs bundle code. | hub-api, which creates the Job. |
| **Compiler `publisher`** (trusted container) | Bucket write key, Ed25519 signing key, the `waddles_publisher` DB role. Never runs bundle code. | hub-api; egress limited to the bucket, Postgres and the hub-api notification endpoint. |

### 3.3 What deliberately does not change

- The scope prefix and its `_tenant` rendering (`libs/flask_core/flask_core/stream_pipeline.py:67-112`), plus `:cfg` and `:state` verbatim. The **process hop's** keys change to per-ingest-source streams (D23, D24); the action hop keeps `…:app:{app_id}:action`, as a stream.
- The envelope JSON shape (`PlatformEvent` / `StageEnvelope`), including the `event`-not-`payload` naming and the `_target_app_id` reserved payload key, which gains a declaration requirement (D25) but no shape change.
- The 5 s distribution-poll cadence and its graceful-degrade-to-last-known-good behaviour.
- Helm chart structure: service names, values keys, ports 8200/8201/8202/8208, metrics 9090.
- `app_catalog` / `app_activations` / `app_tenant_availability` resolution semantics and 3-tier config precedence.
- Python bundle sources — moved from `core/svc_{process,action}/bundles/*.py` to `bundles/python/` with only their database-access lines rewritten against `penguin-dal` (D21a); logic and entrypoint signatures untouched.

---

## 4. Components

### 4.0 Repository layout after the cut-over

The repository is `penguintechinc/waddles` (local clone `~/code/waddles`); see D22 for the naming rule and the surviving legacy identifiers.

```
waddles/
  core/
    svc_ingest/        Cargo.toml  src/  tests/  Dockerfile  deny.toml  README.md
    svc_process/       Cargo.toml  src/  tests/  Dockerfile  deny.toml  README.md
    svc_action/        Cargo.toml  src/  tests/  Dockerfile  deny.toml  README.md
    svc_streaming/     Cargo.toml  src/  tests/  Dockerfile  deny.toml  README.md   (Python alpha deleted)
    bundle_executor/   Cargo.toml  src/  tests/  Dockerfile  deny.toml  README.md     (its own image and
                                                                                      Deployment per stage)
    bundle_compiler/   Cargo.toml  src/  tests/  Dockerfile  deny.toml  README.md
  bundles/
    python/            every existing *_process.py / *_send_action.py, moved (DB lines on penguin-dal)
                       + one bundle.yaml per bundle (new file, no source edits)
    rust/              example bundle + template
    javascript/        example bundle + template
  sdk/
    waddle-sdk/        Python package: same import names as flask_core/waddle_transports
                       bundle-facing API, implemented over WIT imports
    waddle-sdk-rs/     Rust crate for Tier 1 Rust bundles
    waddle-sdk-js/     npm package for Tier 1 JavaScript/TypeScript bundles
  wit/
    waddle-bundle/     stage.wit — the single normative copy of the WIT world
  hub_api/             unchanged Python, plus the install/version endpoints of §9
  k8s/helm/waddlebot/  same chart, new values keys (§12.3)   (legacy identifier)
  docs/
    APP_BUNDLE_AUTHORING.md      rewritten as v2 in M6
    superpowers/specs/2026-09-14-rust-data-plane-design.md   (this file)
```

`penguin-libs` (separate repo, `~/code/penguin-libs`) gains:

```
penguin-libs/packages/
  rust-spine/          crate penguin-spine
  rust-bundle-host/    crate penguin-bundle-host
  rust-logging/        crate penguin-logging
  rust-connectors/     workspace: penguin-connector-{twitch,discord,slack,youtube,kick}
  rust-licensing/      existing crate penguin-licensing — gains CI + publish jobs
```

### 4.1 `core/svc_ingest` (Rust, rewrite)

**Responsibility.** Own every inbound platform connection and every inbound HTTP intake surface; normalize raw platform payloads into `PlatformEvent` with fixed, non-pluggable code; write each finished `StageEnvelope` **once** onto the Valkey stream of the ingest source it came from (§10.6). Additionally run the Twitch outbound relay drain. **Holds every platform credential. Runs no bundle and links no executor.**

**Interfaces.**

| Direction | Interface |
|---|---|
| Inbound HTTP | `:8200` — the endpoint table in §10.1. |
| Inbound sockets | Twitch IRC (per channel), Twitch EventSub websocket (per tenant, when selected), Discord gateway, Slack Socket Mode, Kick Pusher; YouTube live poll. |
| Outbound | One Valkey `XADD` per event onto `{scope}:src:{platform}:{source_id}:events`; Twitch IRC sends drained from the outbound relay list. |
| Control | `GET {HUB_API_URL}/api/v1/distribution/bundles?stage=process` every `POLL_INTERVAL_S`, with a `distribution:read`-scoped HS256 service JWT minted from `SECRET_KEY` (unchanged mechanism). |

**Normalizers absorbed as code.** The six `core/svc_ingest/bundles/*_ingest.py` `normalize()` functions become Rust functions in `src/normalize/{twitch,twitch_eventsub,discord,slack,youtube,kick,generic}.rs`. Their behaviour is preserved exactly; the ported tests are the acceptance criterion (§14.4).

**Activation-resolution fix.** `core/svc_ingest/fanout.py`'s `NULL_INSTALLATIONS` lookup always returned zero rows, so the gateway fan-out path always fell through to each Feature's shipped default App and ignored real per-community activation. The Rust ingest resolves the target bundle set **only** through the distribution API (the same path `BundlePoller` already used correctly), so per-community activation is honoured on every path. This is a behaviour change and is called out in §15.3.

**Dependencies.** `penguin-spine`, `penguin-logging`, `penguin-connectors` (all five platform crates), `penguin-licensing`; `axum`, `tokio`, `tower-http`, `reqwest` (rustls), `serde`/`serde_json`, `hmac`, `sha2`, `subtle` (constant-time compare), `jsonwebtoken` (`aws_lc_rs` backend, never `rust_crypto` — see `core/svc_streaming/Cargo.toml`'s RUSTSEC-2023-0071 note), `governor` (token-bucket rate limiting), `redis`/`deadpool-redis` via `penguin-spine`. No SeaORM: ingest has no database, matching today.

**Configuration.**

| Var | Default | Notes |
|---|---|---|
| `MODULE_PORT` | `8200` | Matches `pipeline.svcIngest.port`. Read at runtime — fixes the current Dockerfile's hardcoded 8210 vs chart 8200 conflict. |
| `METRICS_PORT` | `9090` | Separate Prometheus listener. |
| `BIND_ADDR` | `0.0.0.0` | |
| `RUNNER_TENANT_SLUG` | `global` | Fixed tenant slug per deployment, unchanged semantics. |
| `TWITCH_EVENTSUB_MODE` | `webhook` | `webhook` or `websocket`; per-tenant, mutually exclusive (§10.2). |
| `TWITCH_EVENTSUB_SECRET` | *(unset)* | Unset ⇒ the webhook route is **not registered at all**, matching today. |
| `KICK_WEBHOOK_SECRET` | *(unset)* | Unset ⇒ route mounted but returns `503`, matching today. |
| `INTAKE_MAX_BODY_BYTES` | `262144` | 256 KiB cap on every intake route. |
| `INTAKE_REPLAY_WINDOW_S` | `300` | Signature timestamp skew window. |
| `INTAKE_RATE_LIMIT_SOURCE_RPS` / `_BURST` | `20` / `40` | Per-source token bucket. |
| `INTAKE_RATE_LIMIT_TENANT_RPS` / `_BURST` | `100` / `200` | Per-tenant token bucket. |
| `DRAIN_SOCKET_TIMEOUT_S` | `65` | Outbound-relay connection read timeout; must exceed the blocking-pop block time (§5.5). |
| `RELAY_BLOCK_TIMEOUT_S` | `30` | Blocking-pop block time for the outbound relay. |
| `WADDLES_INGEST_TRUSTED_PROXIES` | *(empty)* | CIDR list; `X-Forwarded-For` honoured only when the direct TCP peer is in this list (§4.1.1). |

#### 4.1.1 Webhook intake: source restriction and authentication

Per-source HMAC verification (§10.1, §10.3) is **necessary but not sufficient** — a leaked or guessed per-source secret must not, by itself, be enough to forge an event. Every webhook-shaped intake route therefore also restricts by origin, by a second authentication factor, or both. This section states the rule; §10.1's endpoint table and §11.1's threat table carry the per-route detail.

**Generic webhook (`POST /intake/webhook/{tenant}/{source}`).** The per-source HMAC signature (§10.1) stays mandatory. In addition, each generic source **MUST** be configured with at least one of:

- **Source-IP allowlist** — one or more CIDRs, checked against the resolved client address (trusted-proxy rules below);
- **Bearer token** — `Authorization: Bearer <token>`, a per-source secret compared with `subtle::ConstantTimeEq`;
- **HTTP basic authentication** — a per-source username/password, both compared with `subtle::ConstantTimeEq`.

Configured modes combine as **AND** — with the HMAC signature and with each other, so a source configured with both an IP allowlist and a bearer token must satisfy both on every request. A generic source with **none** of the three configured cannot be activated: hub-api's source create/update validation rejects it with `422 auth_factor_required` (§10.3). If such a source's HMAC-only configuration ever reaches svc_ingest regardless, svc_ingest answers `401 auth_not_configured` rather than accepting on signature alone.

**Twitch EventSub and Kick webhooks.** Platform signature verification is unchanged from §10.1 (Twitch: `Twitch-Eventsub-Message-Signature` HMAC-SHA256 over `id + timestamp + body`, 600 s / 10-minute timestamp window, message-id replay cache; Kick: the platform's public-key signature header). In addition, the client address **MUST** forward-confirmed reverse-DNS (FCrDNS: a PTR lookup on the client address, then an A/AAAA lookup of the returned name, which must contain the original address) to the platform's domain — `twitch.tv` for Twitch, `kick.com` for Kick, by default, each configurable per platform (`ingest.platforms.twitch.originSuffixes`, `ingest.platforms.kick.originSuffixes`, §12.3). An optional per-platform CIDR allowlist (`ingest.platforms.twitch.originCidrs`, `ingest.platforms.kick.originCidrs`) lets an operator pin static ranges instead of relying on FCrDNS. FCrDNS results are cached (TTL 10 minutes, bounded size). A signature failure and an origin failure are both rejections but are distinguished: signature failure is `401 bad_signature` (unchanged, §10.1); an origin failure is `403 origin_not_trusted`. Every rejection increments `ingest_webhook_rejected_total{platform|source, reason="origin"|"signature"|"auth"|"replay"}` and logs a sanitized WARN — never the token, the signature, or the body.

**Trusted proxies.** `WADDLES_INGEST_TRUSTED_PROXIES` (`ingest.trustedProxies` in the chart, §12.3) is a CIDR list, default empty. Empty means the client address used for every check above is the direct TCP peer, full stop. `X-Forwarded-For` is honoured **only** when the direct peer is in this list, and then only as the rightmost hop that is **not** itself in the trusted set — a spoofed header presented by an untrusted peer is ignored entirely, never partially trusted.

**`POST /intake/events` (JWT).** Already authenticated by the hub-api-issued JWT (§10.4) and unaffected by the requirements above; it **may** additionally carry a per-caller IP allowlist as an optional hardening layer, checked against the same trusted-proxy-derived client address.

### 4.2 `core/svc_process` (Rust, rewrite)

**Responsibility.** `XREADGROUP` from each activated bundle's **granted** ingest-source streams, skip non-matching entries cheaply (§5.3), run the stage's built-ins around each bundle call, invoke the bundle's `transform` through the executor, and `XADD` the result onto that bundle's own `:action` stream — or, for an approved `_target_app_id` redirect, onto the declared target's.

**Built-ins vs bundles.** The Python runner's always-on hooks are re-homed with **no third path** — each is either a Rust built-in of the stage or an always-installed bundle:

| Current hook (`core/svc_process/runner.py`) | Becomes |
|---|---|
| Content-moderation gate (`services/moderation_gate.py`), which no community may opt out of | **Rust built-in**, runs before the bundle call |
| Moderation-enforcement routing (stamp + synthetic enqueue to `waddles.community.moderation.default`'s `:action`) | **Rust built-in**, runs after the gate |
| Cross-app routing via `PROCESS_TARGET_APP_ID_KEY` (`_target_app_id`) | **Rust built-in** of the enqueue step — the reserved key is popped off the payload before enqueue, exactly as today |
| `bot_process` command dispatch | **Bundle** (`waddles.bot.commands.default`) |
| Raid auto-shoutout (`_maybe_shoutout_raid`, flag `waddles.bot.shoutout`) | **Bundle** |
| Live ON/OFF status recording (`_maybe_live_status`, flag `waddles.streaming.live_status`) | **Bundle** |
| Activity-feed emission (`live_activity_events`) | **Bundle** |
| Reputation accrual (`reputation_gate_client.py`) | **Bundle** |

Rationale for the split: a hook that must run for every event regardless of activation, or that manipulates the routing of the envelope itself, is stage behaviour; everything flag-gated and per-feature is an App. This assignment is an Assumption (§20, A5) — the approved text fixed the rule ("bundles or built-ins, no third path") but not each hook.

**Interfaces.** Valkey drain/enqueue via `penguin-spine`; the capability-scoped host API on `:8301` (mTLS, executor-facing only); Postgres via SeaORM for the built-ins' own tables and for serving bundles' `db` host calls; `GET /api/v1/distribution/bundles?stage=process` poll; `/health`, `/healthz`, `/metrics` on `:8201` / `:9090`.

**Dependencies.** `penguin-spine`, `penguin-bundle-host`, `penguin-logging`, `penguin-licensing`; `axum`, `tokio`, `sea-orm` (`sqlx-postgres`, `runtime-tokio-rustls`), `serde`, `reqwest` (rustls, for bundle `http` host calls), `governor`.

### 4.3 `core/svc_action` (Rust, rewrite)

**Responsibility.** Terminal stage. `XREADGROUP` each activated bundle's own `:action` stream through the single group `{app_id}`, invoke `dispatch` through the executor, classify the returned `transport-error.retryable`, apply retry-with-backoff, and write the outcome to `action_dispatch_log`. Platform senders become **Rust built-ins** the bundles reach through the host API (`relay` for Twitch, `http` with guarded egress for REST platforms) — a bundle never holds a platform credential.

**Retry semantics (preserved from `core/svc_action/runner.py`).** The runner owns all backoff timing; a bundle never sleeps. `transport-error.retryable = true` ⇒ retry; `false` ⇒ terminal failure recorded to `action_dispatch_log`. Defaults: `ACTION_MAX_RETRIES=3`, `ACTION_BASE_BACKOFF_MS=250`, `ACTION_MAX_BACKOFF_MS=8000`, full jitter. A `retry-after-ms` returned by the bundle (or by a built-in sender parsing a `Retry-After` header) overrides the computed backoff when larger, capped at `ACTION_MAX_BACKOFF_MS`.

**Interfaces.** Same shape as svc-process, on `:8202` / `:9090`, plus outbound platform REST calls and the Twitch outbound relay `LPUSH`.

### 4.4 `core/svc_streaming` (Rust, kept)

**Change set is deletion and alignment only:**

1. Delete the Python alpha: `app.py`, `blueprints/`, `services/`, `openapi/`, its `Dockerfile`, and its pytest tree.
2. Rename `Dockerfile.rust` → `Dockerfile`; point `build-svc-streaming.yml` at it.
3. Adopt `penguin-logging` in place of the hand-rolled `src/telemetry.rs` wiring, keeping the same env-var contract.
4. Adopt `penguin-licensing` for flag/entitlement checks.
5. No spine, no executor, no bundles — media stays fully isolated from the chat pipeline, as today.

### 4.5 core/bundle_executor (M2, not yet built) — binary `bundle-executor`

**Responsibility.** Hold a wasmtime engine and an instance pool, accept `Invoke` frames from its stage over one mTLS connection, run the bundle's exported function under a per-call epoch deadline and memory cap, and issue host-call frames back to the stage for every capability the bundle imports. **Holds no stage credentials, no Valkey or Postgres access, and can reach exactly two network destinations: its stage's host-API port and the artifact bucket.**

**Deployed as its own Deployment per stage** — `svc-process-executor` and `svc-action-executor` — rootless, all capabilities dropped, read-only rootfs, `RuntimeDefault` seccomp; an optional gVisor `RuntimeClass` may be layered on where the cluster supports it, default off (§11.2, §12.2, D32). It dials out to the stage's host-API port (`:8301` for process, `:8302` for action) and maintains a connection pool of `EXECUTOR_STAGE_CONNECTIONS` (default `4`). Replica count tracks the stage's (`pipeline.executor.replicas`, default `2`). A stage whose executor connections are all down drains nothing and fails readiness after `EXECUTOR_UNAVAILABLE_READY_S=15`; the executor reconnects with exponential backoff (`1 s` base, `30 s` cap).

**Interfaces.** The wire protocol in §6.6 over mTLS, plus read-only bucket `GET`s for component fetches — the only two interfaces it has.

**Dependencies.** `wasmtime` (component model + WASI 0.2, exact pinned version), `tokio` (`net`, `rt-multi-thread`, `io-util`), `rustls` + `tokio-rustls` (mTLS client), `object_store` (bucket reads), `serde`/`serde_json`, `penguin-bundle-host` (shared frame types), `penguin-logging` (log frames are forwarded to the stage, never written directly). No `redis`, no `sea-orm`, no `sqlx` — the dependency set is itself part of the security argument and is asserted by a test (§14.6).

### 4.6 core/bundle_compiler (M2, not yet built) — binary `bundle-compiler`

**Responsibility.** One rootless, credential-less, no-network sandboxed run per uploaded bundle version — an optional gVisor layer available on top where the cluster supports it, default off (D32) — validate the manifest, scan the source, compile it to a WASI 0.2 component (or validate an uploaded prebuilt one), content-address it, and write the component plus a signed metadata sidecar to the bucket. Exits non-zero with a machine-readable reason on any failure.

**Run as** a Kubernetes Job created by hub-api, one Job per version, `backoffLimit: 0`, `activeDeadlineSeconds: 900`, `ttlSecondsAfterFinished: 86400`. The Job has **two containers sharing one `emptyDir`**, with sharply different trust:

```
  Job: bundle-compile-{app_id}-{version}
  ┌────────────────────────────────────────┐   ┌────────────────────────────────────────┐
  │ initContainer: build      UNTRUSTED    │   │ container: publisher        TRUSTED    │
  │ rootless · gVisor optional (off)       │   │  cluster-default runtime               │
  │  NO credentials, NO network at all     │──▶│  bucket key, signing key, DB role      │
  │  runs the per-language build, which    │   │  validates, hashes, precompiles,       │
  │  EXECUTES BUNDLE CODE                  │   │  signs, uploads, records the version   │
  └──────────────┬─────────────────────────┘   └──────────────▲─────────────────────────┘
                 │        shared emptyDir /work (source in, component out)
                 └───────────────────────────────────────────┘
```

| Container | Trust | `RuntimeClass` | Network | Credentials | Does |
|---|---|---|---|---|---|
| `build` (init container) | **Untrusted** — it runs bundle code | `sandbox.runtimeClassName` (`runsc`) when `sandbox.gvisor.enabled: true`; cluster default otherwise (D32) | **None** — the NetworkPolicy allows nothing, in or out | **None mounted** | Manifest validation, source security scan, the per-language build with the §4.6 flags. Writes the candidate component to `/work`. |
| `publisher` | **Trusted** — it never runs bundle code | cluster default | bucket, Postgres, hub-api | bucket write key, Ed25519 signing key, the `waddles_publisher` DB role | Validates the component (`wasm-tools component wit` + the per-language import allowlist), computes the component and `.cwasm` SHA-256 digests, precompiles, signs the sidecar, uploads under the digest key, and `INSERT`s the version row |

**Why the split.** The digest is the artifact's identity and the basis of every load-time verification (§11.7). Computing it inside the same process boundary that just executed untrusted bundle code would let a compromised build stage choose the hash it is measured by. Splitting the Job means **the hash is never computed inside the sandbox that ran bundle code**: the publisher reads bytes out of the shared `emptyDir` and measures them itself, and a build container that tampered with those bytes changes the digest rather than hiding it.

The `build` container still gets the strongest sandbox available (rootless, no network, no credentials, all capabilities dropped, plus gVisor where the operator has opted in — D32); the publisher gets credentials but never executes guest code. Neither container has both.

**Hermeticity is measured, not assumed.** The compiler-sandbox spike ran all three Tier 1 toolchains under the pod-boundary model the Job provides regardless of the gVisor opt-in (`--network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp`): Rust built in 0.40 s, Python in 5.52 s, JavaScript in 5.13 s, with DNS resolution failing closed (`gaierror` / `EAI_AGAIN`) rather than hanging. It also found that `componentize-py`'s **own** build sandbox has zero filesystem preopens by default, so a build-time file write from bundle code fails — a welcome additional layer, and explicitly **not** a replacement for the Job's sandbox, since it constrains only what the guest does inside componentization and not what the toolchain process itself could do.

**`componentize-py` does execute guest code at build time** — round 2 of the spike confirmed it: componentization performs a sandboxed dry-run of the bundle with the WIT imports trapped, and the compiler's own `pkgutil.walk_packages` pre-import (§4.12) deliberately widens that execution to every module in the package. The build container is therefore genuinely untrusted-code execution, which is why it stays rootless with no credentials and no network at all (D27, D10) — with gVisor available as an optional additional layer, default off (D32) — rather than a phase of a credentialed process.

**Build flags are part of the contract, not an implementation detail.** The compiler-sandbox spike (`spike/bundle-compiler-sandbox`, commit `1f1d75a4`, report `spikes/bundle-compiler-sandbox/REPORT.md`) found that default builds leak imports beyond the bundle's own world. The compiler therefore builds each Tier 1 language with exactly these flags, and a build that omits them fails validation rather than shipping a component with extra reach:

| Language | Required build invocation | Why |
|---|---|---|
| Python | `componentize-py … --stub-wasi` | Without it the component imports the **real** `wasi:sockets`; with it, the import set is our world plus the permitted WASI set |
| JavaScript/TypeScript | `componentize-js … --disable all` | Without it the component imports `wasi:http` |
| Rust | `cargo component build --target wasm32-wasip2` | No equivalent flag exists, so the validation allowlist for Rust-built components additionally permits `wasi:cli` and `wasi:filesystem` (read-only `/scratch` only) — see §6.5's per-language allowlist |

**Toolchain images must be complete and pinned.** `cargo-component` silently attempts to install the `wasm32-wasip1` rustup target on first use, which a network-free build cannot do. The compiler image pre-installs and pins that target (and every other toolchain component) at build time, and the container structure test asserts its presence — otherwise the first Rust bundle build fails with a DNS error rather than a useful message.

**Dependencies.** `wasmtime` (validation only), the pinned Tier 1 toolchains (`componentize-py`, `cargo`+`cargo-component`+`wasm32-wasip2`+`wasm32-wasip1`, `componentize-js`), `wasm-tools` (the `component wit` front end of the validate step), `object_store` (S3), `ed25519-dalek` (sidecar signing), `sha2`, `serde`, `penguin-logging`.

### 4.7 `penguin-spine` (new crate, `penguin-libs/packages/rust-spine`)

**Responsibility.** The Valkey Streams spine: key builders, envelope types, `XADD`/`XREADGROUP`/`XACK`/`XAUTOCLAIM`, consumer-group management, grant-scoped reads, the DLQ, and `MAXLEN ~` bounding.

**Public surface.**

```rust
pub struct Scope { pub tenant: String, pub community: Option<String> }
impl Scope {
    pub fn source_stream(&self, platform: &str, source_id: &str) -> String;  // …:src:{platform}:{source_id}:events
    pub fn action_stream(&self, app_id: &str) -> String;                     // …:app:{app_id}:action
    pub fn config_key(&self, app_id: &str) -> String;                        // …:app:{app_id}:cfg
    pub fn state_key(&self, app_id: &str) -> String;                         // …:app:{app_id}:state
}
pub enum Stage { Process, Action }
pub struct PlatformEvent { /* §6.1 */ }
pub struct StageEnvelope { /* §6.1 */ }
pub struct Grant { pub stream: String, pub platform: String, pub source_id: String }
pub struct Delivered { pub stream: String, pub entry_id: String, pub env: StageEnvelope, pub deliveries: u64 }

pub struct SpineClient { /* pooled, TLS-aware, ACL-aware; admin + write traffic only */ }
impl SpineClient {
    pub async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError>; // XADD → entry id
    pub async fn ensure_group(&self, stream: &str, app_id: &str) -> Result<(), SpineError>;      // BUSYGROUP-tolerant
    pub async fn destroy_group(&self, stream: &str, app_id: &str) -> Result<(), SpineError>;
    pub async fn ack(&self, d: &Delivered, app_id: &str) -> Result<(), SpineError>;
    pub async fn claim_stale(&self, stream: &str, app_id: &str) -> Result<Vec<Delivered>, SpineError>; // XAUTOCLAIM
    pub async fn dead_letter(&self, d: &Delivered, err: &DlqError) -> Result<(), SpineError>;
    pub async fn group_stats(&self, stream: &str) -> Result<Vec<GroupStats>, SpineError>;        // XINFO GROUPS
}

/// Dedicated connection, own socket timeout — §5.7. Reads ONLY granted streams.
pub struct GroupReader { /* … */ }
impl GroupReader {
    pub fn new(grants: Vec<Grant>, app_id: String, consumer_id: String) -> Self;
    pub async fn read(&mut self) -> Result<Vec<Delivered>, SpineError>;  // XREADGROUP … BLOCK
}
```

**Invariants the crate enforces (each has a test):** strict serde on every envelope read; `community: None` always renders as the literal `_tenant` segment; a `GroupReader` asked for a stream outside its `grants` fails with `SpineError::StreamNotGranted`; administrative traffic never shares a connection with an in-flight blocking read; an entry payload is always the envelope JSON under the single field `env`.

### 4.8 `penguin-bundle-host` (new crate, `penguin-libs/packages/rust-bundle-host`)

**Responsibility.** Everything both sides of the executor socket must agree on, plus the stage-side implementation of the host capabilities.

| Module | Contents |
|---|---|
| `wire` | Frame codec (§6.6), message enums, correlation-id mux. Depended on by both the executor and the stages. |
| `manifest` | `bundle.yaml` v2 parse + the 22 numbered validation rules (§6.4). |
| `host::http` | Guarded egress client: manifest allowlist, SSRF rules, tenant denylist, rate limiter, timeouts, response cap, secret-ref header injection. |
| `host::kv` | Bundle-scoped key/value over `bundle_state_key`, TTL-bounded. |
| `host::db` | Parameterized statement execution under the per-bundle Postgres role, table allowlist pre-check, RLS scoping. |
| `host::relay` | Outbound relay push (Twitch). |
| `host::flags` | `penguin-licensing` two-gate resolution. |
| `host::log` | Sanitized, levelled forwarding into the stage's OTel pipeline. |
| `host::clock` | Monotonic + wall clock. |
| `loader` | Bucket poller, digest + signature verification, precompilation, hot-swap, trip accounting. |

### 4.9 `penguin-logging` (new crate, `penguin-libs/packages/rust-logging`)

Closes the gap recorded verbatim in `backend-rust.md` (*"No Rust penguin logging crate exists yet — KNOWN GAP pending `rust-logging` package in penguin-libs"*).

- Ports the `SENSITIVE_KEYS` sanitization contract from `penguin-libs/packages/python-utils/.../logging.py` **verbatim**: the same key set, matched by exact key **or** substring, replaced with the literal `[REDACTED]`; email-shaped string values replaced with `[email]@{domain}`; recursion into nested maps and lists.
- Wires `tracing` + `tracing-subscriber` + `tracing-opentelemetry` + `opentelemetry-otlp` + a `prometheus` registry, configured only from the standard OTLP env vars; an unset `OTEL_EXPORTER_OTLP_ENDPOINT` means tracing-only with no OTLP attempt (same behaviour as `core/svc_streaming/src/telemetry.rs` today).
- Exposes the shared health/metrics surface: `/health`, `/healthz`, `/metrics`, and the `transport:` field of §11.6.
- A dead exporter never fails a request: bounded buffer, drop-oldest, a counter for drops.

### 4.10 `penguin-connectors` (new workspace, `penguin-libs/packages/rust-connectors`)

One crate per platform, so a service pulls only the transitive dependencies of the platforms it actually uses.

| Crate | Covers |
|---|---|
| `penguin-connector-twitch` | IRC client (port of `libs/waddle_transports/.../transports/irc.py`), EventSub webhook verification, EventSub websocket client, Helix REST, the outbound relay queue contract. |
| `penguin-connector-discord` | Gateway client, REST message send. |
| `penguin-connector-slack` | Socket Mode client, `chat.postMessage`. |
| `penguin-connector-youtube` | Live-chat poll with the existing backoff/quota behaviour, `liveChatMessages.insert`. |
| `penguin-connector-kick` | Pusher chat client, webhook verification, REST send. |

Every crate exposes the same shape: a `Receiver` producing raw platform payloads, a `Sender` performing outbound calls, and a `verify_signature` function where the platform has one. None of them touch Valkey or Postgres.

### 4.11 `penguin-licensing` (existing crate, operational work only)

Already behaviourally complete (`LicenseClient`, two-gate flag + entitlement, 5-minute cache, exponential backoff, 72 h offline grace, hardcoded domain bypass). The work here is process, not code:

1. Add a `build-rust-licensing` job to `penguin-libs/.github/workflows/ci.yml`, mirroring `build-rust-rpc` (`cargo fmt --check`, `clippy -D warnings`, `cargo test`, `cargo deny check`).
2. Add a `publish-rust-licensing` job to `publish.yml`, tag `rust-licensing-v*`, trusted publishing, mirroring `publish-rust-rpc`.
3. Cut `release/rust-licensing/v0.1.x` and publish `0.1.0` to crates.io so the four services can pin it by exact version.

### 4.12 `waddle-sdk` (new Python package)

The shim that makes D7 true. It exposes **the same import names and the same public API** the bundles use after the M1.5 DAL migration, implemented over the WIT imports instead of over the Python host:

| Bundle-facing name today | `waddle-sdk` implementation |
|---|---|
| `from flask_core import get_bundle_context` | Reads the `context` import. |
| `from flask_core import get_bundle_dal` | Returns the `penguin-dal` facade's `AsyncDB` (below). |
| `flask_core.stream_pipeline.PlatformEvent` / `StageEnvelope` | Dataclasses with identical fields and `to_dict`/`from_dict`, constructed from the WIT records. |
| `flask_core.feature_flags.feature_enabled` | Reads the `flags` import. |
| `waddle_transports.signing.resolve_secret` | Returns an opaque `SecretRef`; the value never enters the WASM module — the stage injects it (§8.3). |
| `waddle_transports.base.RetryableTransportError` / `NonRetryableTransportError` | Raised as usual; the SDK maps them onto `transport-error.retryable`. |
| `httpx.AsyncClient` passed to action entrypoints | A drop-in client object whose `request`/`get`/`post` route to the `http` import. |
| `logging` / `flask_core.logging_config` | Routed to the `log` import. |

**The database facade (D21).** `waddle-sdk` ships exactly **one** database surface: the `penguin-dal` public API, implemented over the WIT `db` import. There is no `flask_core.database.AsyncDAL` facade and no pydal facade — the bundles that used those are migrated to `penguin-dal` first (D21a, milestone M1.5).

The surface to reproduce is `penguin_dal`'s package exports (`/home/penguin/code/penguin-libs/packages/python-dal/src/penguin_dal/__init__.py`, summarized in the penguin-libs inventory):

| Module | Names the facade implements |
|---|---|
| `penguin_dal.db` | `DB`, `AsyncDB`, `DatabaseManager` |
| `penguin_dal.query` | `Query`, `QuerySet`, `AsyncQuerySet`, `Row`, `Rows` |
| `penguin_dal.field` / `.field_proxy` / `.table_proxy` | `Field`, `FieldProxy`, `TableProxy` |
| `penguin_dal.pagination` | `Page`, `Cursor` |
| `penguin_dal.exceptions` | `DALError`, `TableNotFoundError`, `ValidationError` |
| `penguin_dal.factory` | `create_dal` |

`get_bundle_dal()` returns the facade's `AsyncDB`. Every query the builder composes is lowered to a parameterized statement sent over the WIT `db` import and executed **by the stage**, under the bundle's own Postgres role with row-level security.

**The facade is single-threaded and synchronous underneath, with async-compatible signatures.** WASI gives the guest no thread pool — the spike found `asyncio.to_thread` simply unavailable — so `await`ing a facade call runs the work inline on the `PollLoop` and returns an already-completed awaitable. Bundle code that says `await db(...).select()` is unchanged; nothing runs concurrently inside a bundle, by construction. Any `penguin-dal` construct the facade cannot lower raises an explicit `NotImplementedError` naming the construct — never a silent mis-execution. Fidelity here remains the riskiest SDK component and keeps its spike (§18, R1).

**The SDK runtime shims.** Three WASI realities mean `waddle-sdk` is not only an API surface; it also carries a small runtime layer, installed at `sitecustomize` level so bundle sources stay untouched. All three are **CONFIRMED working** by round 2 of the spike (`spikes/penguin-dal-wasm/REPORT.md`, branch `spike/penguin-dal-wasm`, commits `f45e6578`, `964f2729`), not assumed:

| Shim | Why | Behaviour | Spike status |
|---|---|---|---|
| `asyncio.to_thread` and `loop.run_in_executor` | The bundles wrap their DB reads and writes in `asyncio.to_thread(...)`, and WASI has no thread pool, so the real implementations cannot run. | Execute the callable **synchronously** and return an already-completed awaitable. Semantics for a single-threaded guest are equivalent: the call was always awaited immediately afterwards. | **CONFIRMED** — the unchanged alias bundle's full `!alias add` write path ran end to end through the shim: 4 `db-execute` round trips, 9–18 ms total; the read-only `!alias foo` path returned in 0.7–1.0 ms with no DB call. |
| Component entry point | `asyncio.run()` cannot start under WASI — it creates a `socketpair`. | The generated component entry drives `componentize-py`'s `PollLoop` instead, and the bundle's coroutine is scheduled on it. | **CONFIRMED** — the same end-to-end run is driven by `PollLoop`. |
| Module pre-import | `componentize-py`'s static import discovery misses lazily imported modules, which then fail at call time inside the guest. | The **compiler** walks the bundle package with `pkgutil.walk_packages` and pre-imports every module before componentization, so discovery sees them all. | **CONFIRMED** — build-time pre-import generation resolved every lazy import in the spike's bundle set. |

The synchronous `to_thread` replacement is the piece most likely to surprise an author, so it is called out in `APP_BUNDLE_AUTHORING.md` v2: inside a bundle, `to_thread` does not parallelize — it runs inline.

`waddle-sdk-rs` and `waddle-sdk-js` are thinner: they expose idiomatic bindings over the same WIT world with no compatibility obligation to a prior API.

---

## 5. Spine and envelopes

### 5.1 Transport: Valkey Streams, written once per event

**Supersedes the list-based spine.** Ingest receives or polls each event once and writes it **once**; every subscribing process bundle then reads it from there through its own consumer group. Nothing is copied per bundle.

**Prior art in this repository.** `libs/flask_core/flask_core/stream_pipeline.py:341-1047` already implements exactly this model — `XADD`/`XREADGROUP`/`XACK`/`XPENDING`/`XTRIM`, `BUSYGROUP`-tolerant `create_consumer_group`, and a bounded `move_to_dlq` — but is gated off by default (`STREAM_PIPELINE_ENABLED`, false) and uses the legacy `waddlebot:stream:*` / `waddlebot:dlq:*` namespace **(legacy identifier)**. This design activates that model under the `waddles:` namespace, with per-bundle groups; the DLQ record shape is taken from it rather than invented.

**One stream per configured ingest source**, not per platform:

```
waddles:t:{tenant}:c:{community|_tenant}:src:{platform}:{source_id}:events
```

`{source_id}` is the stable identifier hub-api assigns to one ingest configuration — one Twitch channel/account, one Discord guild connection, one Slack workspace, one generic webhook source, one REST-intake custom platform — and it is the same value carried in `PlatformEvent.source.account_id`/`source.channel_id`'s owning record. Per-source granularity is what makes selective access possible: a bundle can be granted "Twitch #channelA" without being granted every Twitch channel in the tenant.

Ingest writes with approximate trimming:

```
XADD {stream} MAXLEN ~ {SPINE_STREAM_MAXLEN} * env {envelope_json}
```

- The entry has exactly one field, `env`, whose value is the `StageEnvelope` JSON of §6.1.2 — byte-identical to what the list-based design carried, so the envelope contract and its golden fixtures are unchanged.
- `MAXLEN ~` (default `SPINE_STREAM_MAXLEN` = `100000`) is approximate on purpose: exact trimming is O(N) on every write.
- Every trim increments `waddles_stream_trimmed_total{stream}`.

### 5.2 Consumer groups: requested by `consumes`, granted by hub-api

A bundle's `consumes` (§6.4.3) is a **request**, not an entitlement. At activation, hub-api resolves the request against the tenant/community's configured ingest sources and produces an explicit **grant list**:

```
app_id  →  [ waddles:t:acme:c:main:src:twitch:tw-channelA:events,
             waddles:t:acme:c:main:src:discord:dg-guildX:events ]
```

| Step | Who | What |
|---|---|---|
| Resolve | hub-api, at activation | Expand every `consumes` rule against the configured sources. `platform: twitch` with no `source_id` matches every Twitch source; a rule naming a `source_id` matches exactly one. |
| Record | hub-api | One row per granted stream in the new `app_stream_grants` table (§6.8). |
| Show | hub-api → admin | The grant list is rendered in words on the install/activation screen — "this bundle will read: Twitch #channelA, Discord guild X" — with wildcards spelled out into the concrete sources they resolve to. |
| Create | hub-api | `XGROUP CREATE {stream} {app_id} $ MKSTREAM`, `BUSYGROUP`-tolerant, on **granted streams only**. |
| Ensure | the stage, at startup and on every distribution refresh | Re-issues the same `BUSYGROUP`-tolerant create for each granted stream, so a group lost to a Valkey restore is restored without operator action. |
| Destroy | hub-api, at deactivation | `XGROUP DESTROY {stream} {app_id}` for every grant, then deletes the rows. |

**The stage is the enforcement point.** Bundles hold no Valkey connection and never name a stream; the stage reads on a bundle's behalf **only from that bundle's granted streams**. A consumer group existing on a stream is not itself authority — the stage will not read a stream that is not in the bundle's grant list, which is what makes the negative test in §14.6 (a Discord-only bundle never seeing Twitch entries even when a group exists) meaningful rather than tautological.

**Re-resolution.** Adding an ingest source later re-runs the resolution for every activated bundle in that tenant/community: a bundle whose rule is `platform: twitch` with no `source_id` automatically gains a group and a grant on the new channel's stream; a bundle scoped to a specific `source_id` does not. Removing a source destroys the groups on its stream and deletes those grants.

**Revocation.** An admin can revoke an individual grant without uninstalling or deactivating the bundle (`DELETE /api/v1/apps/{app_id}/grants/{grant_id}`, §9.6): the row is deleted, the group destroyed, the stage stops reading that stream within one distribution poll, and the action is audit-logged with the actor's UUID.

**Valkey ACLs are unchanged by this.** The stage's ACL user stays pattern-scoped to `waddles:t:*` (§11.6.1). Per-bundle selectivity is **logical, implemented in the stage**, not enforced by Valkey — stated plainly here so nobody mistakes the ACL for the boundary. The boundary that matters is that bundles have no Valkey access at all.

### 5.3 Reading, filtering and acking

Each process-stage worker, per granted stream:

```
XREADGROUP GROUP {app_id} {consumer_id} COUNT {SPINE_READ_COUNT} BLOCK {SPINE_BLOCK_MS}
           STREAMS {stream} >
```

- `{consumer_id}` is the pod identity (`SPINE_CONSUMER_ID`, defaulting to the pod name, else `{hostname}-{uuid-v4}`).
- `COUNT` defaults to `64`, `BLOCK` to `1000` ms. The blocking read runs on a **dedicated connection** under the rules of §5.7 — that rule survives the transport change untouched.
- **Consumer-side matching.** The stage re-evaluates the bundle's `consumes.event_types` and `filters` against the entry it read. A non-match is a **cheap skip**: `XACK` immediately, no executor call, `waddles_consumer_skipped_total{app_id,reason}` +1. This is where a `!sr`-only bundle avoids paying for every chat line, and it costs one string comparison per entry rather than a wasted WASM invocation.
- A match is handed to the executor; on success the entry is `XACK`ed. Failures follow the retry policy and then go to the DLQ (§5.5).

Filters remain an optimization, not a security boundary: a bundle validates its own input, and the stage never promises that a delivered event satisfied a filter the bundle did not declare.

### 5.4 At-least-once, recovery and idempotency

At-least-once comes from the **pending-entries list** (PEL), not from a hand-rolled processing key. The `LMOVE`/processing-key/consumer-index/reaper machinery of the list design is removed entirely.

- An entry delivered by `XREADGROUP` sits in the group's PEL until `XACK`.
- A crash between read and ack leaves it pending; another consumer recovers it with

```
XAUTOCLAIM {stream} {app_id} {consumer_id} {SPINE_CLAIM_IDLE_MS} 0 COUNT 64
```

  run every `SPINE_CLAIM_INTERVAL_MS` (default `15000`) against entries idle longer than `SPINE_CLAIM_IDLE_MS` (default `30000`). Each claim increments `waddles_stream_claimed_total{app_id}`.
- **Redelivery cap.** `XAUTOCLAIM`/`XPENDING` report a delivery count per entry; at `SPINE_MAX_DELIVERIES` (default `5`) the entry is DLQ'd with `error.kind = "max_deliveries"` and `XACK`ed rather than claimed again.

**Idempotency is a bundle expectation, stated in the authoring guide.** A bundle may see the same event twice after a crash. The stream entry id is the stable de-duplication key and is exposed to the bundle as `message-id` on the `context` capability (§6.5) — a bundle that writes must either be naturally idempotent or record the `message-id` it last applied. Every first-party bundle is reviewed for this in M2.

### 5.5 Dead-letter queue

- Key: `waddles:dlq:{stage}` — one per stage, unchanged.
- Write: `XADD waddles:dlq:{stage} MAXLEN ~ {SPINE_DLQ_MAXLEN} * rec {dlq_record_json}` (default `10000`, matching the existing `DEFAULT_DLQ_MAXLEN`), then `XACK` the source entry so it stops being redelivered. The record shape is §6.3, taken from `StreamPipeline.move_to_dlq`'s prior art.
- Written on: envelope deserialization failure, bundle trap or error return, executor call timeout, `max_deliveries` exhaustion, a disabled-by-trip bundle's events, and executor unavailability.
- **Not** written on: an entry no bundle subscribed to (nothing is enqueued in the first place), or a consumer-side filter skip (normal operation).
- `waddles_spine_dlq_total{stage,reason}` on every write, `reason` = the record's `error.kind`.

### 5.6 Backpressure

- Streams are bounded by `MAXLEN ~` at write time (§5.1); the oldest entries fall off, and `waddles_stream_trimmed_total{stream}` makes that visible rather than silent.
- The signal that matters operationally is **group lag**, not stream length: `waddles_group_lag{app_id,stream}` and `waddles_group_pending{app_id,stream}`, sampled from `XINFO GROUPS` every `SPINE_STATS_INTERVAL_MS` (default `10000`). A stuck bundle shows as growing PEL on its own group while every other group stays healthy — which is exactly the isolation the per-bundle group buys, and it is bounded by the three-strike disable of §7.5.
- An operator alert fires on `waddles_group_pending` above `SPINE_PEL_ALERT` (default `5000`) for a single group, or on any trimming of a stream whose slowest group has not caught up.

### 5.7 Client rules carried over verbatim

Two gotchas documented in `core/svc_ingest/outbound_drain.py` are properties of the protocol, not of `redis-py`, and apply identically to `redis-rs`/`deadpool-redis` and identically to `XREADGROUP ... BLOCK`. `penguin-spine` encodes both:

1. **A blocking read runs on its own dedicated connection with its own socket timeout.** The block timeout is a *server-side command argument*, not the client socket's read timeout. `SPINE_BLOCK_MS` (default `1000`) and `RELAY_BLOCK_TIMEOUT_S` (default `30`) must each be strictly less than the owning connection's socket timeout (`DRAIN_SOCKET_TIMEOUT_S`, default `65`); `penguin-spine` refuses to construct a blocking-read client where that does not hold, with a startup error naming both values.
2. **Administrative traffic (`XGROUP`, `XACK`, `XAUTOCLAIM`, `XINFO`) never shares a connection with an in-flight blocking read.** Cancelling the future does not tell the Valkey server to abandon the command, so a pooled connection can hand the next caller a socket with a stale pending reply. The blocking reader owns a connection that is never returned to the shared pool, and the shared pool refuses `XREADGROUP ... BLOCK`, `BRPOP` and `BLMOVE` outright.

Both rules have dedicated tests (§14.2).

### 5.8 Cadence and the latency SLA

| Timer | Value | Governs |
|---|---|---|
| `POLL_INTERVAL_S` | `5.0` | **Bundle-set and grant refresh only** — how soon a newly activated bundle, a new grant, or a revocation is noticed. Unchanged from `core/svc_ingest/config.py:47`. |
| `SPINE_BLOCK_MS` | `1000` | How long a `XREADGROUP` blocks when its stream is idle. An entry arriving during the block wakes the reader immediately — this is a push, not a poll. |
| `SPINE_CLAIM_INTERVAL_MS` | `15000` | How often abandoned entries are reclaimed. |
| `RELAY_BLOCK_TIMEOUT_S` | `30` | The Twitch outbound relay's blocking pop (still a list). |

Because `XREADGROUP` blocks rather than polls, a waiting consumer is woken by the `XADD` itself; the added scheduling latency per hop is sub-millisecond in the common case, not one drain interval. Worst case is bounded by the executor call budget, leaving the 3 s text and 5 s A/V budgets dominated by platform round-trips and bundle work. The end-to-end budget is asserted in the alpha e2e run (§14.8).

### 5.9 Process → action

The action hop is **strictly per app bundle**:

```
waddles:t:{tenant}:c:{community|_tenant}:app:{app_id}:action      (stream, one group: {app_id})
```

A bundle's process output goes **only** to its own action stream, read by exactly one consumer group, `{app_id}`. The stage never reads another bundle's action stream on a bundle's behalf.

**This is a data-leakage boundary, and the asymmetry with ingest is deliberate.** Ingest source streams carry shared, read-only platform data — the same public chat line every subscribed bundle is entitled to see — so many groups read one stream by design. An action envelope is different: it is a bundle's own output, and it routinely carries the results of that bundle's private state and its resolved config. One bundle must never observe another bundle's action entries, and the per-bundle stream plus single-group model is what makes that structural rather than conventional. §14.6 asserts it directly.

**The one cross-bundle path is `_target_app_id`, and it is now a declared capability.** A process bundle that wants to route an event to another app must list that app in its manifest:

```yaml
routes_to: ["waddles.community.forums.default"]
```

| Rule | Behaviour |
|---|---|
| Declaration | Exact `app_id`s only. No wildcards, no prefixes. |
| Install-time | hub-api validates that each target exists in `app_catalog`, is installed in the **same tenant** as the declaring bundle — a cross-tenant target is refused outright at approval, not merely left unapproved (D30) — and renders "may send events to app X" on the consent screen (§9.7). |
| Runtime | The stage compares the `_target_app_id` a bundle set against the **approved** `routes_to` set. A redirect to an undeclared or unapproved target, or one whose target resolves to a different tenant than the source envelope's, is **dropped** — the event is not delivered anywhere — `waddles_route_denied_total{app_id,target}` is incremented, and the attempt is logged at WARN with both ids. The tenant check is independent of the install-time one (D30): impossible by construction once approval refuses it, but the stage never trusts that alone. |
| Write path | The **stage** writes to the target's action stream. The source bundle never names a stream, never holds a Valkey connection, and cannot write to another bundle's stream even if the redirect is approved. |
| Invariants | A redirect changes only the destination key's `app_id` segment; tenant and community still come from the stream the entry was read from, and the reserved payload key is popped off before the write. |

### 5.10 Ordering, scope and tenancy

- **Ordering:** per-stream FIFO per consumer group. Entries from one ingest source reach one bundle in the order they were written. No ordering guarantee across sources, across bundles, or across the process→action hop — the same guarantee the list design offered, now with an explicit name.
- **Tenant** is the deployment's `RUNNER_TENANT_SLUG`; **community** is `Option<String>` where `None` renders as the literal `_tenant` segment.
- Tenant and community are sourced **exclusively** from the stream key the entry was read from, never from event payload. `PlatformEvent.source` identifies the connection, never the tenancy. The `_target_app_id` escape hatch changes only the destination key's `app_id` segment — the invariant documented at `libs/flask_core/flask_core/stream_pipeline.py:251-286` is preserved bit for bit.

### 5.11 Workstream identity, end-to-end trace and the tenant wall (D30)

**Workstream.** A hub-api entity, `workstreams(id uuid, tenant_id, community_id nullable = tenant-wide, source_id unique, created_at, disabled_at)` (§6.11), created 1:1 with each row in `intake_sources` (§10.3, §15.4) — one workstream per configured ingest source, so `workstream_id` and `source_id` are two names for the same relationship. Owned by exactly one tenant and one community, or the tenant-wide `_tenant` scope, mirroring §5.10's `Option<String>` rule. Shown on the source's config view (§10.3) and on the consent screen (§9.7.1) alongside the source it reads.

**Minting.** svc-ingest builds the binding for every inbound event — never the bundle, never hub-api after the fact — from its own `intake_sources` cache, the same lookup that already resolves `source_id` and the stream key (§10.6): `tenant_id`, `community_id` and `workstream_id` come from that record, not from the payload, exactly as tenant/community already do (§5.10, §11.8). Alongside them, ingest mints a fresh `event_id` (UUID v4, distinct from the platform's own message id, which stays in `PlatformEvent`, and from the Valkey stream entry id `XADD` assigns afterward, §5.4) and a new W3C trace — one trace per inbound event, never a continuation of an inbound request's own trace — with the platform's own message id recorded as an `intake.request`/`ingest.normalize` span attribute, not folded into the trace id. `session_id` is populated when the platform surfaces a connection/broadcast session (a Twitch/Kick EventSub websocket session, a Discord gateway session, a live-broadcast id) and left absent otherwise; it groups traces, it does not replace them.

**Binding MAC.** Ingest computes

```
binding.mac = hex(HMAC-SHA256(k_binding[binding.kid],
                 tenant ‖ community ‖ workstream_id ‖ event_id ‖ trace_id))
```

where `trace_id` is the 32-hex trace-id segment of `trace.traceparent`, `community` renders as the literal `_tenant` exactly as the key segment does (§6.2), and `k_binding[kid]` is a symmetric key held only by the Rust stage services (`security.envelopeBinding.keySecretRef`, §12.3) — never by hub-api, never by a bundle, never by the compiler. Keys are named by `kid` and rotated with an overlap window (`security.envelopeBinding.rotationOverlapSeconds`, §12.3): a verifier accepts a MAC produced under any `kid` still inside the overlap window, and always mints new MACs under the current `kid`.

**Verification at every hop.** svc-process and svc-action (and svc-streaming, where it reads an envelope-bearing control call) each verify, on every entry read, before any other processing — bundle invocation included:

1. `binding.mac` recomputes to the value on the envelope, under the `kid` it names.
2. `tenant`/`community` on the envelope equal the `t:`/`c:` segments of the stream key the entry was read from (§5.10 — D30 makes this cryptographically checked, not merely sourced correctly).
3. The bundle's installation, its stream grant (§6.8) and its install approval (§6.9) belong to that same tenant, and to that community or a tenant-wide (`_tenant`) grant.
4. For action and streaming, the outbound credential and target are resolved from the envelope's `(tenant, community)` — never from anything a bundle's output supplied.

A failure of any of the four is `error.kind = "tenant_boundary"` (§6.3): the entry is `XACK`ed and DLQ'd, never retried, `waddles_tenant_boundary_violations_total{stage,reason}` +1, a sanitized ERROR log, and an audit event.

**Bundles cannot move a workstream.** `tenant_id`, `community_id`, `workstream_id`, `event_id` and `trace` never travel *from* bundle output — a process bundle's `transform` return value is not read for them, the stage copies them from the input envelope onto the output envelope unconditionally, and a bundle-supplied field of the same name is dropped and counted (`waddles_tenant_boundary_violations_total{stage="process",reason="bundle_set_identity"}`). `routes_to` (§5.9, D25) gains the tenant check described there: refused at install-time approval and independently at runtime.

**Host calls stay tenant-scoped by construction.** No new mechanism is introduced here: `db` already runs on a connection where `SET LOCAL waddles.tenant`/`waddles.community` drives RLS from the envelope the stage is currently servicing (§7.4), and `kv`/config/state keys already carry `{tenant, community}` in the `Scope` that builds them (§4.7, §6.2). D30's contribution is that both are now driven by an envelope whose tenant/community have passed binding-MAC verification, so a forged or replayed envelope cannot reach either. No bundle host call accepts a tenant or community argument at all — every one is scope-implicit, taken from the invocation in flight.

**Trace propagation.** `trace.traceparent`/`trace.tracestate` (superseding the single-field `trace_context` of the pre-D30 schema, §6.1.2) travel on the envelope through every stream hop, into the executor frame for every host call and back (the executor still creates no spans of its own, §13.2 — it returns durations, the stage records them), and onto outbound platform calls. Every span (§13.2) additionally carries `waddles.tenant_id`, `waddles.community_id`, `waddles.workstream_id`, `waddles.app_id` — ids only, never PII or message bodies. Pipeline log lines already carry `trace_id` (§13.3); `workstream_id` is added alongside it.

**Envelope schema.** `StageEnvelope` gains `workstream_id`, `event_id`, `session_id` (optional), `trace` (replacing `trace_context`) and `binding`, detailed in §6.1.2, and the schema version is bumped to `2`. No dual-read: this subsystem ships inside the v3.0 cut-over (D3), so a `schema_version` other than `2` — including its absence — is rejected rather than interpreted as the pre-D30 shape.

### 5.12 Workstream usage metering (D31)

**What is recorded.** Every stage that touches an envelope records usage keyed by `(tenant_id, community_id, workstream_id, stage, app_id)`: events ingested, bundle invocations, bundle fuel/CPU-ms (from the executor's per-call accounting, §7.2/§7.3), host calls by kind (`http`/`kv`/`db`/`relay`/`flags`/`log`), actions delivered, outbound bytes, and — svc-streaming only — stream-media minutes.

**Transport.** Deltas are batched and `XADD`ed to `waddles:usage` (§6.2) at most every `METERING_FLUSH_INTERVAL_S` (default `10`, `metering.flushIntervalSeconds`) per stage replica — not per event, so a chatty channel does not multiply the write rate. Stages are **write-only** on this stream (§11.10.2): they `XADD` and never read it back.

**Aggregation.** hub-api owns a consumer that reads `waddles:usage` and writes `workstream_usage_hourly` (§6.12, Postgres), one row per `(tenant_id, community_id, workstream_id, stage, app_id, hour)`, **append-only** for the aggregator's own writes — corrections are new rows for the same key, summed at query time, never an `UPDATE` of a settled hour — and exposes a per-community usage view/API for admins (read-only). §11.10.1's RBAC matrix and §11.10.2's ACL matrix both gain the corresponding rows.

**Not metric labels.** `workstream_id` and `app_id` are high-cardinality by design (one per install, one per channel) and never become an OTel metric label (§13.1) — usage lives in `workstream_usage_hourly`; metrics stay tenant-safe and low-cardinality. The two systems answer different questions: metrics say "is this healthy," the usage table says "how much did this workstream cost."

**Not billed yet.** `metering.enabled` (chart value, default `true`) turns the recording and aggregation on; there is no charging, quota or enforcement wired to it in this spec. Per-workstream pricing, if adopted, is a future commercial decision — a metering axis alongside the existing per-node/per-seat model (`critical-rules.md` Licensing Model), gated by the license server like any other entitlement, not by this recording pipeline. The data model is built so that "usage per workstream per community for a billing period" is one query against `workstream_usage_hourly` filtered by `tenant_id`, `community_id` and an `hour` range.

## 6. Data contracts

Everything in this section is normative. Where a value is a default, the environment variable that overrides it is named.

### 6.1 Envelope JSON

The queue-crossing shape is unchanged from `libs/flask_core/flask_core/stream_pipeline.py:204-328`, plus the workstream-identity and trace fields of §5.11 (`workstream_id`, `event_id`, `session_id`, `trace` — replacing `trace_context` — and `binding`, D30) and the `schema_version` bump to `2`.

#### 6.1.1 `PlatformEvent`

```json
{
  "platform": "twitch",
  "event_type": "chat.message",
  "actor": "some_user",
  "payload": {"text": "!songrequest foo", "channel_id": "12345", "message_id": "abc"},
  "occurred_at": "2026-09-14T12:00:00.000Z",
  "source": {"platform": "twitch", "account_id": "bot-primary", "channel_id": "12345"}
}
```

| Field | JSON type | Required | Constraint | Violation |
|---|---|---|---|---|
| `platform` | string | yes | non-empty | `EnvelopeError` / `SpineError::Envelope`, DLQ `error.kind = "envelope_invalid"` |
| `event_type` | string | yes | non-empty; a dotted, lowercase namespace (`chat.message`, `channel.follow`, `stream.online`) — the namespace `consumes.event_types` globs match against | same |
| `actor` | string \| null | yes (may be null) | string when present | same |
| `payload` | object | yes | JSON object; may be empty `{}`; never a scalar or array | same |
| `occurred_at` | string | yes | non-empty; RFC 3339 UTC with millisecond precision, `Z` suffix | same |
| `source` | object \| null | no | When present: `{platform, account_id, channel_id}`, all strings, `channel_id` nullable; `source.platform` **must equal** the top-level `platform` | same |

**`source` — which connection this event came in on.** Tenant and community stay *outside* the event, on the envelope and the key, exactly as before; `source` answers a different question: *which* of possibly several connections to the same platform produced this. Without it, a deployment with two Twitch bot accounts, or one bot in forty channels, cannot tell its events apart, and a bundle cannot filter by channel.

| Field | Meaning |
|---|---|
| `source.platform` | The platform slug (`twitch`, `discord`, …, `waddles`, or a tenant-registered `custom:<name>`). Mirrors the top-level `platform` so a consumer reading only `source` is never wrong. |
| `source.account_id` | The identity of the *connection*: the bot account, app id, or intake source name — e.g. the Twitch bot login, the Discord application id, the Slack app id, the generic-webhook `{source}` path segment. Stable across restarts, never a secret. |
| `source.channel_id` | The platform's own channel/guild/room identifier the event occurred in, or `null` for events with no channel (an account-level notification). |

**How today's `normalize()` outputs map into it.** The six ported normalizers each already know their connection's identity, because the receiver that produced the raw payload owns it; `source` is populated by the normalizer, not inferred later:

| Normalizer | `source.account_id` | `source.channel_id` |
|---|---|---|
| Twitch IRC | the bot login the `IrcTransport` authenticated as | the IRC channel (without `#`) |
| Twitch EventSub (webhook or websocket) | the subscription's client/app id | `event.broadcaster_user_id` from the notification |
| Discord gateway | the bot application id | the channel id (guild id stays in `payload`) |
| Slack Socket Mode | the Slack app id (`xapp-` app, not the token) | the channel id |
| YouTube live poll | the configured channel's OAuth client or API-key identity | the live chat's video/broadcast id |
| Kick (Pusher or webhook) | the configured Kick app id | the channel slug |
| Generic webhook intake | the `{source}` path segment | from the source's mapping, or `null` |
| Generic REST intake | the JWT `sub` | the `community` query parameter's channel, or `null` |

`source` is optional in deserialization (absent or `null` → `None`) for the same backward-compatibility reason as `target_app_id`, but **svc-ingest always populates it** — every event entering the pipeline through any route in §10.1 carries one, and a bundle's `consumes` channel filtering depends on it.

#### 6.1.2 `StageEnvelope`

```json
{
  "schema_version": 2,
  "tenant": "global",
  "community": null,
  "app_id": "waddles.bot.commands.default",
  "stage": "process",
  "event": { "...PlatformEvent..." },
  "ts": "2026-09-14T12:00:00.123Z",
  "target_app_id": null,
  "workstream_id": "8f14e45f-ceea-467e-adde-3fb5c9752730",
  "event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "session_id": null,
  "trace": {
    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    "tracestate": null
  },
  "binding": {
    "kid": "2026-09",
    "mac": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
  }
}
```

| Field | JSON type | Required | Constraint |
|---|---|---|---|
| `schema_version` | integer | yes | must equal `2`; a `1` or absent value is the pre-D30 shape and is rejected — no dual-read (D3, D30) |
| `tenant` | string | yes | non-empty; equals the `t:` segment of the key it was taken from |
| `community` | string \| null | yes (may be null) | `null` ⇔ the key's `c:` segment is the literal `_tenant` |
| `app_id` | string | yes | matches `^waddles\.[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_-]*$` |
| `stage` | string | yes | one of `ingest`, `process`, `action` |
| `event` | object | yes | a `PlatformEvent` object; a message without an `event` key is refused, never coerced |
| `ts` | string | yes | non-empty; RFC 3339 UTC, millisecond precision |
| `target_app_id` | string \| null | no | absent or `null` ⇒ `None`; when set, changes only the destination key's `app_id` segment |
| `workstream_id` | string | yes | UUID; minted by svc-ingest from `intake_sources`/`workstreams` (§5.11, §6.11), never from payload; copied verbatim by every later stage, never accepted from bundle output |
| `event_id` | string | yes | UUID v4, minted once by svc-ingest per inbound event; distinct from the platform's own message id (kept in `PlatformEvent`) and from the Valkey stream entry id (§5.4); an input to `binding.mac` (§5.11) |
| `session_id` | string \| null | no | The platform connection/broadcast session (EventSub websocket session, Discord gateway session, a live-broadcast id), when the platform has one; absent or `null` otherwise (§5.11) |
| `trace` | object \| null | no | `{traceparent, tracestate}`; `traceparent` is the W3C string (`00-<32 hex>-<16 hex>-<2 hex>`) as `trace_context` carried alone before D30; `tracestate` is the W3C `tracestate` string or `null`; absent ⇒ no parent span. **Supersedes the pre-D30 `trace_context` field** (§5.11) |
| `binding` | object | yes | `{kid, mac}` — `kid` names the active HMAC key version, `mac` is the lowercase-hex `HMAC-SHA256` of §5.11's formula; verified by every stage on every read before any other processing |

**Strictness.** Both implementations use strict deserialization: a missing required field, a wrong-typed field, an unknown top-level field, or a `stage` outside the fixed set is an error. No coercion, ever. `flask_core.stream_pipeline`'s `from_dict` is tightened to reject unknown keys, to require `schema_version == 2`, and to carry the D30 fields in M1, so the Python (hub-api, tests) and Rust (data plane) readers agree byte for byte.

**Reserved payload key.** `_target_app_id` (`PROCESS_TARGET_APP_ID_KEY`) may be set by a process bundle inside `event.payload`; the stage pops it back out before enqueuing, so it never reaches an action bundle or a chat reply. Unchanged.

### 6.2 Key scheme

Scope prefix: `waddles:t:{tenant}:c:{community|_tenant}`. Per-bundle base: `{scope}:app:{app_id}`.

| Purpose | Key | Valkey type | Written by | Read by |
|---|---|---|---|---|
| **Ingest source stream** | `{scope}:src:{platform}:{source_id}:events` | stream, `MAXLEN ~ SPINE_STREAM_MAXLEN` | svc-ingest, once per event | every process bundle **granted** that stream, each through its own consumer group named `{app_id}` |
| **Action stream** | `{scope}:app:{app_id}:action` | stream, `MAXLEN ~ SPINE_STREAM_MAXLEN` | svc-process | svc-action, one consumer group `{app_id}` |
| Bundle config | `{scope}:app:{app_id}:cfg` | string (JSON) | the stage, on distribution refresh | the stage (host `context`) |
| Bundle state | `{scope}:app:{app_id}:state` | hash | host `kv` calls | host `kv` calls |
| Dead letter | `waddles:dlq:{stage}` | stream, `MAXLEN ~ SPINE_DLQ_MAXLEN` | any stage | operators, replay tooling |
| Twitch outbound relay | the provider-scoped key from `waddle_transports.transports.irc_relay.outbound_queue_key("twitch")` | list (unchanged — a single-consumer relay, not a fan-out) | svc-action's `relay` host call | svc-ingest's outbound drain |
| Usage metering | `waddles:usage` | stream, `MAXLEN ~ SPINE_STREAM_MAXLEN` | every stage, batched (§5.12, D31) | hub-api's usage aggregator only — stages are `XADD`-only and never read it back (§11.10.2) |

Entry payload for every stream above is a single field, `env` (or `rec` for the DLQ), holding the JSON of §6.1.2 (or §6.3). Consumer groups are named by `app_id`; consumers within a group are named by pod identity.

`{community}` is the community slug, or the literal `_tenant` when the activation is tenant-wide — never omitted, so splitting a key on `:` always yields the same field count. `{source_id}` is hub-api's stable identifier for one ingest configuration.

**Removed by the Streams design** (they belonged to the list-based spine and have no successor): the per-stage queue key `{base}:{stage}`, the `{base}:ingest` key, the in-flight lease key `{base}:{stage}:proc:{consumer_id}`, the consumer index `waddles:proc:idx:*`, the consumer heartbeat sorted set `waddles:consumers:*`, and the delivery-counter hash `waddles:deliv:*`. The PEL replaces all six.

### 6.3 DLQ record

One JSON object per `XADD` onto the `waddles:dlq:{stage}` stream, carried in a single field `rec`:

```json
{
  "schema_version": 1,
  "stage": "process",
  "key": "waddles:t:global:c:_tenant:src:twitch:tw-channelA:events",
  "entry_id": "1757851200000-0",
  "group": "waddles.bot.commands.default",
  "tenant": "global",
  "community": null,
  "app_id": "waddles.bot.commands.default",
  "workstream_id": "8f14e45f-ceea-467e-adde-3fb5c9752730",
  "artifact_digest": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "consumer_id": "svc-process-7d9c4f",
  "deliveries": 5,
  "failed_at": "2026-09-14T12:00:01.500Z",
  "error": {
    "kind": "call_timeout",
    "code": "EXECUTOR_DEADLINE",
    "message": "bundle call exceeded 2000 ms",
    "detail": null
  },
  "trace": {
    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    "tracestate": null
  },
  "raw": "{\"tenant\":\"global\",\"community\":null,...}"
}
```

| Field | Notes |
|---|---|
| `schema_version` | Always `1` for this spec. |
| `entry_id` | The Valkey stream entry id the record came from — the stable de-duplication key, the same value handed to the bundle as `message-id`. |
| `workstream_id` | Copied from the envelope (§5.11, D30); present even on a `tenant_boundary` rejection, which is exactly the record an operator needs to trace a boundary violation back to its source. |
| `group` | The consumer group (`app_id`) that was processing the entry. |
| `raw` | The original envelope JSON **as a string, verbatim**, so a malformed envelope is still replayable/inspectable. |
| `artifact_digest` | `null` when the failure happened before a bundle was selected (e.g. `envelope_invalid`). |
| `error.kind` | One of the ten values below; equals the `reason` label on `waddles_spine_dlq_total`. |

| `error.kind` | Raised when |
|---|---|
| `envelope_invalid` | Strict deserialization failed. |
| `bundle_trap` | The WASM component trapped (panic, unreachable, OOM inside the guest). |
| `bundle_error` | The component returned an error the stage classifies as terminal (`retryable = false`, or a process bundle raising). |
| `call_timeout` | The per-call epoch deadline fired. |
| `memory_limit` | The instance exceeded its memory cap. |
| `host_call_denied` | A capability check refused the call (undeclared egress host, table outside `data.tables`, missing capability). |
| `max_deliveries` | `deliveries` reached `SPINE_MAX_DELIVERIES`. |
| `tenant_boundary` | The §5.11 hop verification failed — `binding.mac` mismatch, envelope tenant/community disagreeing with the stream key, a grant/approval scoped to a different tenant or community, or a bundle output that tried to set an identity field. **Never retried** (D30). |

| `bundle_disabled` | The bundle is disabled after three sandbox trips (§7.5). |
| `executor_unavailable` | The executor was down past `EXECUTOR_UNAVAILABLE_READY_S` and the event could not be attempted. |

### 6.4 `bundle.yaml` v2

One file per bundle, at the root of the source tarball (Tier 1) or uploaded alongside the component (Tier 2). This is the **first time bundles carry an on-disk manifest** — today first-party registration is a Postgres migration row in `app_catalog.stages` (`docs/APP_BUNDLE_AUTHORING.md` §3). `bundle.yaml` becomes the source of truth for a bundle's identity, capability requests and limits; the `app_catalog` row is generated from it at install time.

#### 6.4.1 Complete example

```yaml
schema_version: 2
app_id: waddles.socials.music.default
name: Music Station Song Request
version: 3.0.0
feature: waddles.socials.music
module: socials
provider: builtin
language: python
artifact: source
execution_model: native
is_default: true

stages:
  process:
    entry: "bundles.social_music_process:transform"
    # MANDATORY on a process stage — this is what ingest fans out against.
    consumes:
      - platform: twitch
        event_types: ["chat.message"]
        filters:
          command_prefix: ["!sr", "!songrequest"]
      - platform: discord
        event_types: ["chat.message"]
        filters:
          command_prefix: ["!sr", "!songrequest"]
    produces: ["waddles.music.request"]
    config:
      command_prefix: "!"
    spec:
      required_config: []
  action:
    entry: "bundles.social_music_action:send_request"
    config:
      api_base: "https://hub-api.waddles.svc.cluster.local:8204"
    spec:
      required_config: ["music_station_token_ref"]

egress:
  - host: "api.spotify.com"
    methods: ["GET", "POST"]
  - host: "*.googleapis.com"

data:
  tables:
    - music_queue
    - music_history

limits:
  timeout_ms: 2000
  memory_mb: 64
  egress_rps: 10

permissions:
  - "music:read"
  - "music:write"

config_schema:
  command_prefix:
    type: string
    default: "!"

compatible_with: []
incompatible_with: ["waddles.socials.music.legacy"]

platform_compatibility:
  tested_with: "3.0.0"
  min_version: "3.0.0"
  max_version: null
```

#### 6.4.2 Field reference

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `schema_version` | integer | yes | — | Must be exactly `2`. A `1` or absent value is a v1 manifest and is rejected with an upgrade message. |
| `app_id` | string | yes | — | `waddles.<module>.<feature>.<app>`, exactly four dot-separated segments matching `[a-z0-9][a-z0-9_-]*`. |
| `name` | string | yes | — | 1–120 characters, human-readable. |
| `version` | string | yes | — | SemVer 2.0.0 (core, optional pre-release, optional build metadata). |
| `feature` | string | yes | — | `waddles.<module>.<feature>`; must equal `app_id` minus its last segment. |
| `module` | string | yes | — | Member of `KNOWN_MODULES` (`libs/flask_core/flask_core/app_manifest.py:62-83`); must equal `feature`'s second segment. |
| `provider` | string | yes | — | `builtin` or `thirdparty`. |
| `language` | string | yes | — | `python`, `rust`, `javascript`, `typescript`, or `other`. `other` is only legal with `artifact: prebuilt`. |
| `artifact` | string | yes | — | `source` or `prebuilt`. |
| `execution_model` | string | no | `native` | `native` (WASM component) or `thirdparty` (out-of-process webhook/REST, unchanged from v1 and never compiled). |
| `is_default` | boolean | no | `false` | At most one `true` per Feature, enforced at registration. |
| `stages` | map | yes | — | Keys from `{process, action, presentation}`. **`ingest` is rejected.** At least one key required. |
| `stages.<s>.entry` | string | yes for `process`/`action` when `artifact: source` | — | Language-specific entry reference, recorded for provenance and shown in the UI. Dispatch itself is by WIT export, not by this string. |
| `stages.process.consumes` | list of consume-rules | **yes for a `process` stage** | — | The subscription ingest fans out against (§6.4.3). Must be non-empty. **An `action` stage must not declare `consumes`** — an action bundle receives what its own process stage, or a `_target_app_id` redirect, sends it, exactly as today. |
| `stages.<s>.produces` | list of string | no | `[]` | Event tags this stage produces; informational. |
| `stages.<s>.config` | map | no | `{}` | The bundle's own **non-secret** shipped defaults. Never per-activation values, never secrets. |
| `stages.<s>.spec.required_config` | list of string | no | `[]` | Config keys an activation must supply; surfaced at install/activation time. |
| `stages.presentation.*` | — | — | — | `html_entrypoint`, `assets`, `browser_source_path` as in v1; never compiled to WASM, served by `svc_presentation`. |
| `egress` | list | no | `[]` | Each item `{host: string, methods?: list of string}`. |
| `egress[].host` | string | yes within an item | — | A lowercase FQDN, or a single-label wildcard prefix `*.example.com`. No scheme, no path, no port, no bare `*`, no IP literal. |
| `egress[].methods` | list of string | no | all of `GET,HEAD,POST,PUT,PATCH,DELETE` | Uppercase HTTP methods. |
| `data.tables` | list of string | no | `[]` | Postgres table names (unqualified, `[a-z][a-z0-9_]{0,62}`) the bundle may read/write through the `db` capability. |
| `limits.timeout_ms` | integer | no | `2000` | Per-call wall-clock deadline; must be ≥ `50` and ≤ `EXECUTOR_MAX_CALL_TIMEOUT_MS` (`10000`). |
| `limits.memory_mb` | integer | no | `64` | Per-instance linear-memory cap; must be ≥ `8` and ≤ `EXECUTOR_MAX_MEMORY_LIMIT_MB` (`256`). |
| `limits.egress_rps` | integer | no | `10` | Per-bundle egress rate; must be ≥ `1` and ≤ `EGRESS_RATE_LIMIT_RPS` (`10`) unless the operator raised the ceiling. |
| `permissions` | list of string | no | `[]` | OIDC scopes (`resource:action`) the bundle's Feature requires; mirrors v1's `permissions`/`requires_scopes`. |
| `config_schema` | map | no | `{}` | Per-key `{type, default}` documentation used by the activation UI. |
| `routes_to` | list of string | no | `[]` | Exact `app_id`s this bundle may redirect events to via `_target_app_id` (§5.9). No wildcards. A redirect to an id outside the **approved** list is dropped and counted. |
| `compatible_with` | list of string | no | `[]` | Other `app_id`s. |
| `incompatible_with` | list of string | no | `[]` | Other `app_id`s; checked pairwise before activation (`detect_conflict`). |
| `platform_compatibility` | map | no | `{tested_with: "", min_version: null, max_version: null}` | SemVer strings, parsed not enforced, exactly as v1. |

#### 6.4.3 The `consumes` contract

Bundles no longer live inside ingest, so the system needs a declarative statement of which process bundles want which events. `consumes` is a **request**: hub-api resolves it at activation into an explicit list of granted ingest streams (§5.2, §6.8), and the stage reads only those. One Twitch chat line is written once and read by however many bundles subscribed to that source.

```yaml
consumes:
  - platform: twitch                      # required
    event_types: ["chat.message"]         # required, ≥1 glob
    filters:                              # optional, all cheap, all ANDed
      command_prefix: ["!sr", "!songrequest"]
      actor_roles: ["broadcaster", "moderator"]
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `platform` | string | yes | One of `twitch`, `discord`, `slack`, `youtube`, `kick`, `waddles`, `custom:<name>`, or `*`. `custom:<name>` must name a platform registered for the tenant (§10.4). `*` requires the tenant setting `allow_wildcard_consumes` (rule V30). |
| `source_id` | string | no | Restrict this rule to **one** configured ingest source (one Twitch channel, one Discord guild connection, one webhook source). Absent ⇒ every source of that platform in the tenant/community, including sources added later (§5.2 re-resolution). |
| `event_types` | list of string | yes, ≥ 1 | Glob patterns over the `PlatformEvent.event_type` namespace — a dotted, lowercase namespace such as `chat.message`, `chat.message.deleted`, `channel.follow`, `channel.subscribe`, `stream.online`, `stream.offline`, `member.join`. `*` matches one segment, `**` matches one or more; a bare `*` therefore matches only single-segment types and `**` is the true catch-all, which also requires `allow_wildcard_consumes`. |
| `filters.command_prefix` | list of string | no | Match when `event.payload.text` starts with any listed prefix, after trimming leading whitespace, compared case-insensitively. This is the filter that stops a `!sr`-only bundle being woken by every chat line. |
| `filters.actor_roles` | list of string | no | Match when `event.payload.actor_roles` (normalized by ingest) intersects the list. Values: `broadcaster`, `moderator`, `vip`, `subscriber`, `member`, `everyone`. |

Semantics, fixed so the algorithm in §10.6 is unambiguous:

- A bundle matches an event when **any** of its `consumes` rules matches (rules are ORed).
- Within one rule, `platform`, `event_types` and every present `filters` key must all match (ANDed). An absent filter key is not a constraint.
- `platform` and `source_id` determine **which streams the bundle is granted** (resolved once, at activation). `event_types` and `filters` are evaluated **consumer-side**, by the stage, on each entry it reads: a non-match is acked immediately and never reaches the executor (§5.3).
- Filters are an **optimization, not a security boundary**. A bundle still validates its own input; the stage never promises that a delivered event satisfied a filter the bundle did not declare.
- **Why the wildcard gate still exists.** Under Streams a wildcard no longer multiplies *write* cost — the event is written once regardless. It multiplies **consumer** cost: `platform: "*"` grants a group on every stream in the tenant, so the stage wakes for and filters every event in the tenant on that bundle's behalf, and every such group's PEL must be tracked. That is the cost `allow_wildcard_consumes` (default false) exists to make deliberate.
- `consumes` is part of the capability surface shown at install/approval time, next to `egress` and `data.tables`, rendered as the concrete grant list (§5.2) rather than as the raw rule — an operator approves "reads Twitch #channelA, Discord guild X", not a glob.

#### 6.4.4 Validation rules

Every rule below fails the install with a machine-readable `reason` code. Rules run in order; the first failure is reported. V1–V13 reproduce `parse_manifest()`'s existing ordered rules; V14–V24 are new.

| # | Rule | `reason` code |
|---|---|---|
| V1 | Every required field present | `missing_field` |
| V2 | `version` is valid SemVer 2.0.0 | `bad_semver` |
| V3 | `app_id` matches the four-segment pattern | `not_namespaced` |
| V4 | `feature` matches the three-segment pattern | `not_namespaced` |
| V5 | `module` ∈ `KNOWN_MODULES` | `unknown_module` |
| V6 | `feature` == `app_id` minus its last segment, and `module` == `feature`'s second segment | `feature_prefix_mismatch` |
| V7 | `provider` ∈ `{builtin, thirdparty}` | `invalid_provider` |
| V8 | Every `stages` key ∈ `{process, action, presentation}` | `unknown_surface` |
| V9 | `execution_model` ∈ `{native, thirdparty}` | `invalid_execution_model` |
| V10 | `platform_compatibility` version strings are valid SemVer or null | `bad_platform_compat_semver` |
| V11 | Every `compatible_with`/`incompatible_with` entry is a valid `app_id` | `invalid_compat_app_id` |
| V12 | A `presentation` stage declares `html_entrypoint` and no script entry | `presentation_missing_html_entrypoint` / `presentation_has_script_entrypoint` |
| V13 | A `process`/`action` stage declares no `html_entrypoint` | `script_stage_has_html_entrypoint` |
| V14 | `schema_version` == `2` | `unsupported_schema_version` |
| V15 | `stages` contains no `ingest` key | `ingest_not_pluggable` |
| V16 | `stages` is non-empty | `no_stages_declared` |
| V17 | `language` ∈ the five allowed values; `other` only with `artifact: prebuilt` | `unsupported_language` |
| V18 | `artifact` ∈ `{source, prebuilt}`; `prebuilt` is refused when the global setting `bundles.allow_prebuilt` is false | `prebuilt_not_allowed` |
| V19 | Every `egress[].host` is a lowercase FQDN or a single-label `*.` wildcard: no scheme, path, port, credentials, bare `*`, IPv4/IPv6 literal, or `localhost` | `invalid_egress_host` |
| V20 | Every `egress[].methods` entry is one of the six allowed uppercase methods | `invalid_egress_method` |
| V21 | No `egress[].host` matches the tenant-level global denylist | `egress_host_denylisted` |
| V22 | `egress` is non-empty when the compiled component imports `waddle:bundle/http` | `http_import_without_egress` |
| V23 | Every `data.tables` entry matches `^[a-z][a-z0-9_]{0,62}$` and is not a reserved Waddles identity table (`users`, `tenants`, `communities`, `app_catalog`, `app_activations`, `app_tenant_availability`) | `invalid_data_table` / `reserved_data_table` |
| V24 | Each `limits.*` value is within its allowed range (§6.4.2) | `limit_out_of_range` |

Two further checks run against the **compiled artifact**, not the YAML, and use the same reason vocabulary:

| # | Rule | `reason` code |
|---|---|---|
| V25 | The component's exports satisfy the WIT world for every declared script stage | `wit_export_missing` |
| V27 | A `process` stage declares a non-empty `consumes` list | `consumes_required` |
| V28 | An `action` stage declares no `consumes` | `consumes_on_action_stage` |
| V29 | Every `consumes[].platform` is a known platform, or `custom:<name>` naming a platform registered for the tenant, or `*`; every `event_types` entry is a valid glob over the dotted `event_type` namespace; every `filters` key is one of `command_prefix`, `actor_roles`, with values of the documented shape | `unknown_consumes_platform` / `invalid_event_type_pattern` / `invalid_consumes_filter` |
| V30 | A `consumes` rule using `platform: "*"` or an `event_types` entry of `**` is accepted only when the tenant setting `allow_wildcard_consumes` is true | `wildcard_consumes_not_allowed` |
| V30a | Every `routes_to` entry is a syntactically valid `app_id`, contains no wildcard, and names a bundle present in `app_catalog` | `invalid_routes_to_target` / `unknown_routes_to_target` |
| V31 | The component imports nothing outside the WIT world's import list **plus the denying `wasi:sockets` stub set** (§6.5): no other `wasi:*` interface, and no `wasi:filesystem` beyond the read-only scratch preopen. A component importing `wasi:sockets/*` must instantiate cleanly against the stubs — Python-built components always do; a Tier 2 upload that does not is rejected | `forbidden_host_import` |

### 6.5 The WIT world

Normative copy: `wit/waddle-bundle/stage.wit`. Version `waddle:bundle/stage@1.0.0`.

```wit
package waddle:bundle@1.0.0;

/// Values that cross the host boundary. WIT has no dynamic JSON value, so
/// every open-ended structure is carried as canonical UTF-8 JSON text and
/// validated on both sides.
interface types {
  record platform-event {
    platform: string,
    event-type: string,
    actor: option<string>,
    /// Canonical JSON object text. Never a scalar or array.
    payload-json: string,
    /// RFC 3339 UTC, millisecond precision.
    occurred-at: string,
  }

  record stage-envelope {
    tenant: string,
    community: option<string>,
    app-id: string,
    stage: string,
    event: platform-event,
    ts: string,
    target-app-id: option<string>,
    /// W3C traceparent, when the stage had one.
    trace-context: option<string>,
  }

  record transport-result {
    ok: bool,
    status: option<u16>,
    detail: option<string>,
    provider-message-id: option<string>,
  }

  record transport-error {
    /// The single field the action stage branches on.
    retryable: bool,
    code: string,
    message: string,
    retry-after-ms: option<u32>,
  }

  /// Returned by a stage export the bundle does not implement.
  record unsupported-stage {
    stage: string,
  }
}

/// Immutable, per-call scope. Capability: always granted.
interface context {
  record bundle-context {
    tenant: string,
    community: option<string>,
    app-id: string,
    feature: string,
    version: string,
    /// The Valkey stream entry id of the event being processed. Stable and
    /// unique per delivery target; the de-duplication key a bundle records
    /// to stay idempotent under at-least-once redelivery (Sec 5.4).
    message-id: string,
    /// Resolved 3-tier config (activation > tenant availability > bundle default),
    /// as canonical JSON object text.
    config-json: string,
  }

  get-context: func() -> bundle-context;
}

/// Guarded outbound HTTP. Capability: granted only when `egress` is non-empty.
interface http {
  record header { name: string, value: string }

  record request {
    method: string,
    url: string,
    headers: list<header>,
    body: option<list<u8>>,
    /// Header name -> secret reference name. The stage resolves the reference
    /// and injects the header; the secret value never enters the component.
    secret-refs: list<tuple<string, string>>,
  }

  record response {
    status: u16,
    headers: list<header>,
    body: list<u8>,
    truncated: bool,
  }

  variant error {
    denied(string),
    timeout,
    too-large(u64),
    rate-limited(u32),
    transport(string),
  }

  send: func(req: request) -> result<response, error>;
}

/// Bundle-scoped key/value, stored under the bundle's own `…:state` key.
/// Capability: always granted.
interface kv {
  variant error { too-large(u64), backend(string) }

  get: func(key: string) -> result<option<list<u8>>, error>;
  /// ttl-seconds = 0 means "no expiry"; the host clamps to KV_MAX_TTL_S.
  set: func(key: string, value: list<u8>, ttl-seconds: u32) -> result<_, error>;
  delete: func(key: string) -> result<_, error>;
  increment: func(key: string, delta: s64, ttl-seconds: u32) -> result<s64, error>;
}

/// Parameterized SQL executed BY THE STAGE under the bundle's own Postgres
/// role, restricted to the manifest's `data.tables` and row-level-security
/// scoped to the envelope's tenant/community. Capability: granted only when
/// `data.tables` is non-empty.
interface db {
  variant value {
    null-value,
    bool-value(bool),
    int-value(s64),
    float-value(f64),
    text-value(string),
    bytes-value(list<u8>),
  }

  record rows {
    columns: list<string>,
    rows: list<list<value>>,
    rows-affected: u64,
  }

  variant error {
    denied(string),
    syntax(string),
    conflict(string),
    timeout,
    backend(string),
  }

  /// Statement text with $1..$n placeholders. String interpolation of
  /// parameters is impossible across this boundary by construction.
  execute: func(statement: string, params: list<value>) -> result<rows, error>;
}

/// Push onto a provider-scoped outbound relay queue owned by svc-ingest.
/// Capability: granted only to action-stage bundles.
interface relay {
  variant error { denied(string), backend(string) }

  push: func(provider: string, message-json: string) -> result<_, error>;
}

/// PostHog flag + license entitlement, two-gate, cached, fail-open to the
/// supplied default. Capability: always granted.
interface flags {
  enabled: func(key: string, default-value: bool) -> bool;
  /// "free" | "professional" | "enterprise"
  tier: func() -> string;
}

/// Sanitized, levelled logging into the stage's OTel pipeline.
/// Capability: always granted.
interface log {
  enum level { error, warn, info, debug }
  /// `fields-json` is a canonical JSON object; the host sanitizes it with the
  /// penguin logging SENSITIVE_KEYS rule before emission.
  write: func(lvl: level, message: string, fields-json: string);
}

/// Capability: always granted.
interface clock {
  /// Milliseconds since the Unix epoch, as the stage sees it.
  now-millis: func() -> u64;
  /// RFC 3339 UTC, millisecond precision.
  now-rfc3339: func() -> string;
  /// Monotonic nanoseconds, for in-bundle duration measurement only.
  monotonic-nanos: func() -> u64;
}

interface process-stage {
  use types.{platform-event, unsupported-stage};
  /// `none` means "no reply"; the event is dropped, exactly as v1's
  /// `transform() -> PlatformEvent | None`.
  transform: func(event: platform-event) -> result<option<platform-event>, unsupported-stage>;
}

interface action-stage {
  use types.{stage-envelope, transport-result, transport-error};
  /// `config` is canonical JSON object text (the resolved 3-tier config).
  dispatch: func(envelope: stage-envelope, config: string)
    -> result<transport-result, transport-error>;
}

world stage {
  import context;
  import http;
  import kv;
  import db;
  import relay;
  import flags;
  import log;
  import clock;

  export process-stage;
  export action-stage;
}
```

**Both stage interfaces are always exported.** A bundle that implements only one stage exports a stub for the other: `process-stage.transform` returns `err(unsupported-stage)` and `action-stage.dispatch` returns a `transport-error` with `code = "UNSUPPORTED_STAGE"`, `retryable = false`. Tier 1 SDKs generate the stub automatically. The manifest's `stages` map is authoritative about which export a stage actually calls; calling an unimplemented export is a manifest/registration bug and is DLQ'd with `error.kind = "bundle_error"`.

**Explicitly absent from the world:** `wasi:sockets` (no network from inside the guest — all egress goes through the guarded `http` import), `wasi:filesystem` beyond a single read-only preopen at `/scratch` (empty at load), `wasi:cli/environment` (no environment variables). `wasi:random/random@0.2.x` **is** permitted, because deterministic-only randomness breaks legitimate bundles (shuffles, giveaways).

**The `wasi:sockets` stub rule.** `componentize-py`'s runtime links the full WASI Preview 2 import set — including `wasi:sockets/*` — regardless of the world we declare, so a Python-built component will not instantiate unless those imports are satisfiable. The executor therefore provides **denying implementations** for every `wasi:sockets` import: instantiation succeeds, and any actual socket call returns an error immediately (`wasi:sockets` `error-code::access-denied`, surfacing in guest Python as `PermissionError`), is logged at DEBUG, and counts as a `denied` host call toward the trip threshold. The spike confirmed the behaviour end to end: guest socket use fails cleanly with `PermissionError` and **the component keeps running** — it does not trap, and the invocation completes normally.

**The denying interfaces must be implemented natively in the Rust executor.** Round 2 established that hand-authored stub components are impractical (they were tried against the Python `wasmtime` host and did not hold up); `bundle-executor` links its own implementations of the `wasi:sockets` interfaces that refuse every operation. This is an executor requirement, not a build-time trick, and it is covered by §14.6 test 1.

**Per-language import allowlist.** The compiler-sandbox spike established that the permitted set is not identical across toolchains, so the validator carries one allowlist per language rather than a single list. In every case the executor supplies denying or empty implementations for everything outside our own world, so a permitted import is never a usable capability:

| Language | Permitted beyond the `waddle:bundle/stage` world | Executor provides |
|---|---|---|
| Python (`--stub-wasi`) | `wasi:sockets/*` (the runtime links them regardless), `wasi:random/random`, `wasi:clocks`, `wasi:io` | Natively implemented refusing `wasi:sockets`; real `random`/`clocks`/`io` |
| JavaScript/TypeScript (`--disable all`) | `wasi:random/random`, `wasi:clocks`, `wasi:io` | Real implementations; no `wasi:http`, which `--disable all` removes |
| Rust (no equivalent flag) | `wasi:cli/*`, `wasi:filesystem/*`, `wasi:random/random`, `wasi:clocks`, `wasi:io` | `wasi:cli` with empty args and environment; `wasi:filesystem` with a single read-only `/scratch` preopen and nothing else |
| Tier 2 (prebuilt, any language) | The union above, and only if the component instantiates against the executor's implementations | As above |

The rule for validation (V31) is then precise rather than absolute:

- a component built by the **Python** Tier 1 toolchain may import `wasi:sockets/*`, because it is linked against the stubs and cannot use them;
- a **prebuilt (Tier 2)** upload may import `wasi:sockets/*` only under the same condition — it is instantiated against the same denying stubs — and the validator records the fact on the version row; any Tier 2 component that both imports `wasi:sockets` **and** fails the stubbed-instantiation check is rejected with `forbidden_host_import`;
- any import outside the world's list plus the `wasi:sockets` stub set is rejected outright, for every tier.

The guarantee the rule preserves is unchanged: no bundle of any language opens a socket. What changed is that the enforcement point is the stub, not the absence of the import.

**Capability scoping.** Grants are computed once per bundle load from the manifest and are enforced on the **stage** side, not by trusting the component's import list:

| Capability | Granted when |
|---|---|
| `context`, `kv`, `flags`, `log`, `clock` | Always. |
| `http` | `egress` is non-empty; each call additionally matched against the host/method allowlist. |
| `db` | `data.tables` is non-empty; each statement additionally matched against the table allowlist. |
| `relay` | The bundle declares an `action` stage. |

A call to an ungranted capability returns `denied(...)` and increments `waddles_host_call_denied_total{app_id,capability}`; three denials within `EXECUTOR_TRIP_WINDOW_S` count as a sandbox trip (§7.5).

### 6.6 Executor wire protocol

A single TCP listener per stage, carrying mutual TLS: `:8301` on svc-process, `:8302` on svc-action (`HOST_API_PORT`). The **executor dials the stage**; the stage never dials the executor and exposes this port to nothing else. Both peers present certificates:

- **SPIFFE-ready:** where SPIRE is live, the peers use X.509-SVIDs and each verifies the other's SPIFFE ID (`spiffe://penguintech.io/<env>/svc-process` and `…/svc-process-executor`).
- **Otherwise:** chart-provisioned certificates from the same CA as §11.6.3, with the peer's Common Name pinned by configuration.

TLS 1.3 preferred, 1.2 minimum. A connection whose peer certificate does not verify, or whose identity is not the expected counterpart, is closed before a single frame is read, and increments `waddles_host_api_rejected_total{reason}`.

**Framing.** Every message is a 4-byte big-endian unsigned length followed by that many bytes of UTF-8 JSON, sent inside the TLS stream. A length greater than `EXECUTOR_MAX_FRAME_BYTES` (default `1048576`) is a fatal protocol error: the stage logs it and closes the connection, which the executor re-establishes. Frames are multiplexed by `id`; both sides may have many in flight. The executor holds `EXECUTOR_STAGE_CONNECTIONS` (default `4`) connections and spreads invocations across them.

**Common envelope.**

```json
{"v": 1, "id": 42, "kind": "<message kind>", "...": "..."}
```

`v` is always `1`. `id` is a monotonically increasing `u64` allocated by the sender of the initiating message; every reply reuses it.

**Executor → stage** (the executor dials, so it speaks first)

| `kind` | Fields | Reply |
|---|---|---|
| `hello` | `protocol_version` (int, `1`), `executor_version`, `wasmtime_version`, `wasmtime_abi`, `collector` (e.g. `drc`), `sandbox` (`{runtime: "gvisor"\|"runc", verified: bool}`) | `hello-ok` or `error` |
| `loaded` | `app_id`, `digest`, `precompile_ms`, `exports` (list of export names actually present) | none |
| `unloaded` | `app_id`, `digest` | none |
| `result` | `payload` (the export's return value as JSON), `duration_ms`, `fuel_used` (integer, `0` when fuel metering is off) | none |
| `host-call` | `app_id`, `capability` (`http`\|`kv`\|`db`\|`relay`\|`flags`\|`log`\|`clock`\|`context`), `op`, `args` (capability-specific JSON), `call_id` (the originating `invoke` id) | `host-result` |
| `error` | `code`, `message`, `detail` (nullable) | none |
| `pong` | — | none |

**Stage → executor**

| `kind` | Fields | Reply |
|---|---|---|
| `hello-ok` | `stage`, `protocol_version`, `limits` (`{call_timeout_ms, memory_mb, max_concurrent_calls}`) | none |
| `load` | `app_id`, `version`, `digest` (`sha256:<64 hex>`), `component_key` and `sidecar_key` (bucket object keys), `capabilities` (list), `limits` (`{timeout_ms, memory_mb}`) | `loaded` or `error` |
| `unload` | `app_id`, `digest` | `unloaded` or `error` |
| `invoke` | `app_id`, `digest`, `export` (`transform` \| `dispatch`), `payload` (the export's arguments as JSON), `deadline_ms`, `trace` (`{traceparent, tracestate}`, from the envelope, §5.11) | `result` or `error` |
| `host-result` | `result` (capability-specific JSON) or `error` (`{code, message}`) — replies to a `host-call` | none |
| `ping` | — | `pong` |
| `shutdown` | `grace_ms` | connection closed after in-flight calls drain or `grace_ms` elapses |

A `hello` reporting `runtime: "runc"` is refused with `error.code = "UNSANDBOXED_EXECUTOR"` unless the stage's own `WADDLES_SANDBOX_GVISOR` is also `false`; the two sides must agree on the posture, so a stage expecting gVisor never serves host calls to an executor that is not under it (§12.2).

**Error codes** (`error.code`, stable strings): `PROTOCOL_VERSION`, `FRAME_TOO_LARGE`, `MALFORMED_FRAME`, `UNKNOWN_BUNDLE`, `DIGEST_MISMATCH`, `LOAD_FAILED`, `EXPORT_MISSING`, `EXECUTOR_DEADLINE`, `MEMORY_LIMIT`, `WASM_TRAP`, `HOST_CALL_DENIED`, `HOST_CALL_FAILED`, `UNSANDBOXED_EXECUTOR`, `SHUTTING_DOWN`.

**Deadline ownership.** The stage sets `deadline_ms` on every `invoke` and also arms its own timer at `deadline_ms + 250 ms`. If the executor has not replied by then, the stage treats that connection as wedged, closes it (the executor reconnects and the wedged instance's in-flight work is abandoned), and DLQ's the event with `error.kind = "call_timeout"`. The executor's own epoch interruption is the first line; the stage's timer is the backstop, because a wedged executor cannot report its own deadline. A connection that wedges `EXECUTOR_WEDGE_THRESHOLD` (default `3`) times within `EXECUTOR_TRIP_WINDOW_S` causes the stage to stop scheduling onto that executor replica and to report it unready, which the Deployment's own liveness probe then restarts.

**Host-call deadlines.** A `host-call` does not extend the bundle's deadline. Time spent waiting for the stage counts against `deadline_ms`, so a bundle that makes a slow HTTP call runs out of budget rather than blocking an executor slot indefinitely.

### 6.7 Distribution API additions

`GET /api/v1/distribution/bundles?stage={process|action}` keeps its current response shape and adds six fields per row. Existing fields (`appId`, `communityId`, `entrypoint`, `spec`, `config`) are unchanged so the transition needs no versioning of the endpoint.

```json
{
  "bundles": [
    {
      "appId": "waddles.socials.music.default",
      "communityId": 42,
      "entrypoint": "bundles.social_music_process:transform",
      "spec": {"required_config": []},
      "config": {"command_prefix": "!"},

      "artifactVersion": "3.0.0",
      "artifactDigest": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
      "artifactKind": "source",
      "language": "python",
      "scanStatus": "scanned",
      "manifest": {
        "egress": [{"host": "api.spotify.com", "methods": ["GET", "POST"]}],
        "data": {"tables": ["music_queue", "music_history"]},
        "limits": {"timeout_ms": 2000, "memory_mb": 64, "egress_rps": 10},
        "consumes": [
          {"platform": "twitch", "event_types": ["chat.message"],
           "filters": {"command_prefix": ["!sr", "!songrequest"]}}
        ]
      }
    }
  ]
}
```

| New field | Type | Values / constraint |
|---|---|---|
| `artifactVersion` | string | The manifest `version` of the active artifact. |
| `artifactDigest` | string | `sha256:` + 64 lowercase hex. The stage loads only this digest. |
| `artifactKind` | string | `source` or `prebuilt`. |
| `language` | string | `python`, `rust`, `javascript`, `typescript`, `other`. |
| `scanStatus` | string | `scanned` (all configured scanners ran clean), `scanned_with_findings` (ran, non-blocking findings recorded), `not_scanned` (prebuilt upload — the permanent badge), `scan_failed` (a scanner errored; the version is never activated). |
| `manifest` | object | The capability-bearing subset of `bundle.yaml` the stage must enforce: `egress`, `data.tables`, `limits`, and — for a `stage=process` row — `consumes`, which svc-ingest needs in order to route at all (§10.6). The stage never fetches the full manifest separately. |

`entrypoint` is retained verbatim for provenance and for the admin UI; the Rust stages **do not** dispatch on it — dispatch is by WIT export. A row whose `artifactDigest` is `null` (a registration without a compiled artifact) is skipped by the stage and counted in `waddles_bundle_skipped_total{reason="no_artifact"}`.

**Polling behaviour is unchanged:** every `POLL_INTERVAL_S` (5.0 s), exponential backoff on failure (base 1.0 s, cap 60.0 s), and a hub-api outage degrades gracefully to the last-known-good bundle set rather than raising.

A `stage=process` row additionally carries the bundle's resolved **stream grants**, which is what the stage reads from:

```json
"grants": [
  {"grantId": 118, "stream": "waddles:t:acme:c:main:src:twitch:tw-channelA:events",
   "platform": "twitch", "sourceId": "tw-channelA", "label": "Twitch #channelA"},
  {"grantId": 119, "stream": "waddles:t:acme:c:main:src:discord:dg-guildX:events",
   "platform": "discord", "sourceId": "dg-guildX", "label": "Discord guild X"}
]
```

The stage reads **only** the streams listed here. A grant removed by a revocation or a deactivation disappears from the next poll, and the stage stops reading that stream within one `POLL_INTERVAL_S`. An empty `grants` array means the bundle is activated but currently reads nothing — a legitimate state (every grant revoked), reported as `waddles_bundle_grants{app_id}` = `0` rather than as an error.

### 6.8 `app_stream_grants`

One row per (bundle, granted stream), written by hub-api at activation and re-resolution, deleted at revocation and deactivation.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial | Primary key; the `grantId` in the distribution response and the revoke endpoint |
| `tenant_id` | integer | FK `tenants(id)` |
| `community_id` | integer, nullable | FK `communities(id)`; `NULL` = tenant-wide, mirroring the `_tenant` key segment |
| `app_id` | text | FK `app_catalog(app_id)` |
| `stream_key` | text | The fully rendered Valkey stream key |
| `platform` | text | Denormalized for display and filtering |
| `source_id` | text | The ingest source this grant covers |
| `label` | text | Human-readable rendering shown to the admin ("Twitch #channelA") |
| `granted_by` | uuid, nullable | The approving user's UUID; `NULL` for grants created by automatic re-resolution |
| `granted_at` | timestamptz | |
| `revoked_at` | timestamptz, nullable | Set instead of deleting, so revocations remain auditable |

`UNIQUE (app_id, stream_key) WHERE revoked_at IS NULL`. No PII: the approver is a UUID into the single `users` identity table, never a name or address.

### 6.9 `app_install_approvals`

One row per approved (bundle, version, scope) — the record the runtime derives authorization from (§9.7.3).

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial | Primary key |
| `tenant_id` | integer | FK `tenants(id)` |
| `community_id` | integer, nullable | `NULL` = tenant-wide |
| `app_id` | text | FK `app_catalog(app_id)` |
| `version` | text | The approved SemVer |
| `permission_hash` | text | `sha256:` + 64 hex over the canonical JSON of the permission summary |
| `summary_json` | jsonb | The approved summary in full — grants, egress, tables with read/write, capabilities, `routes_to`, limits, provenance, entitlement |
| `approved_by` | uuid | The approving user's UUID — never a name or an email |
| `approved_at` | timestamptz | |
| `superseded_by` | bigint, nullable | The approval that replaced this one on an upgrade; `NULL` for the current row |

`UNIQUE (app_id, version, tenant_id, community_id) WHERE superseded_by IS NULL`. Canonical JSON means sorted keys, no insignificant whitespace, and arrays in a defined order, so the same permissions always produce the same hash on any machine — the property headless approval (§9.7.5) depends on.

### 6.10 `app_versions` — the digest table

The record of what was built. The property this table guarantees is **no unexpected writers**, not immutability: exactly two roles may write it, and every write is audited.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial | Primary key |
| `app_id` | text | FK `app_catalog(app_id)` |
| `version` | text | SemVer |
| `artifact_digest` | text | `sha256:` + 64 hex over the component bytes |
| `cwasm_digest` | text | `sha256:` over the precompiled artifact |
| `wasmtime_abi` | text | The engine identity the `.cwasm` was produced for |
| `collector` | text | GC collector (`drc`) the `.cwasm` was produced with (§7.2) |
| `size_bytes` | bigint | |
| `language` | text | |
| `artifact_kind` | text | `source` \| `prebuilt` |
| `built_at` | timestamptz | |
| `builder` | text | `bundle-compiler@<version>` |
| `scan_status` | text | `scanned` / `scanned_with_findings` / `not_scanned` / `scan_failed` |
| `badge` | text, nullable | The permanent "not security-scanned" badge, when applicable |
| `approval_id` | bigint, nullable | FK `app_install_approvals(id)` |

`UNIQUE (app_id, version)` and `UNIQUE (artifact_digest)`.

#### Roles — exactly two writers

| Role | Grants on `app_versions` | Rationale |
|---|---|---|
| `waddles_publisher` | `INSERT`, `UPDATE`, `DELETE` | The trusted publisher container (§4.6) — the only component that ever measures an artifact. |
| `hub_api` | `SELECT`, `INSERT`, `UPDATE`, `DELETE` | Owns scan outcome, approval linkage, the cross-check of §9.4, and orphan cleanup. |
| `svc_ingest`, `svc_process`, `svc_action`, `svc_streaming`, the executors, the webui | **No privileges at all** — not `SELECT`, not `INSERT`, not `UPDATE`, not `DELETE`. | Stages read digests through the distribution API; the executor holds no DB credential whatsoever (§11.3). A role that cannot reach the table cannot be tricked into writing it. |

Overwrites by either writer are permitted — a correction, a re-publish, or a cleanup is legitimate work, and forbidding it would only push people to work around the table. What must never happen is a write from somewhere unexpected, so:

**Every write is audited.** An `AFTER INSERT OR UPDATE OR DELETE` trigger records the operation, the writing role (`current_user`), the row key (`app_id`, `version`), and the old and new `artifact_digest` into the audit log. A digest that changes is therefore always attributable — who changed it, when, and from what to what — which is the property that makes an unexpected write detectable rather than invisible. `DELETE` is likewise audited; superseded versions are normally retained for audit (§9.1) rather than deleted.

The grant set is asserted by a negative test (§14.6): each non-writer role's `INSERT`, `UPDATE` and `DELETE` must fail at the SQL level, and the test reports how many roles it exercised.

#### `app_active_versions`

Which version is live is a separate, hub-api-owned table, so activation and rollback never touch an immutable row:

| Column | Type | Notes |
|---|---|---|
| `app_id` | text | Part of the primary key |
| `tenant_id` / `community_id` | integer / integer nullable | Scope; `NULL` community = tenant-wide |
| `version_id` | bigint | FK `app_versions(id)` |
| `activated_by` | uuid | User UUID |
| `activated_at` | timestamptz | |

`PRIMARY KEY (app_id, tenant_id, community_id)`. Only `hub_api` may write it. **Rollback is an `UPDATE` of `version_id` here** — pointing at a different, already-published, already-approved row — never an edit of a digest. The distribution API joins these two tables, which is why `artifactDigest` in §6.7 is always a value some publisher measured, written by one of exactly two roles, and recorded in the audit log if it ever changed.

### 6.11 `workstreams` (D30)

hub-api-owned, one row per configured ingest source (§10.3), created and disabled in lockstep with it (§5.11).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key; this is `workstream_id` everywhere else in this spec |
| `tenant_id` | integer | FK `tenants(id)` |
| `community_id` | integer, nullable | FK `communities(id)`; `NULL` = tenant-wide, mirroring the `_tenant` key segment |
| `source_id` | text | FK `intake_sources(source_id)`, `UNIQUE` — one workstream per source, in either direction |
| `created_at` | timestamptz | |
| `disabled_at` | timestamptz, nullable | Set when the owning source is disabled or removed; a disabled workstream mints nothing further, since there is no source left to mint from |

`UNIQUE (source_id)`. No PII: identified by UUID and by reference to `intake_sources`, never by a display name.

### 6.12 `workstream_usage_hourly` (D31)

Written by hub-api's usage aggregator only (§5.12), append-only.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial | Primary key |
| `tenant_id` | integer | FK `tenants(id)` |
| `community_id` | integer, nullable | `NULL` = tenant-wide |
| `workstream_id` | uuid | FK `workstreams(id)` |
| `stage` | text | `ingest` \| `process` \| `action` \| `streaming` |
| `app_id` | text, nullable | FK `app_catalog(app_id)`; `NULL` for ingest-stage rows, which have no bundle |
| `hour` | timestamptz | Truncated to the hour; the aggregation bucket |
| `events` / `invocations` / `host_calls` / `actions_delivered` | bigint | Counters for the hour |
| `fuel_ms` | bigint | Bundle CPU-ms, from the executor's per-call accounting (§7.2, §7.3) |
| `outbound_bytes` | bigint | |
| `media_minutes` | numeric, nullable | svc-streaming rows only |
| `recorded_at` | timestamptz | When this row was written — not when the usage occurred, since corrections append rather than update |

No `UNIQUE` constraint on the natural key: a correction is a new row for the same `(tenant_id, community_id, workstream_id, stage, app_id, hour)`, summed at query time — append-only means never rewritten, not "one row per hour." Read access is admin-reporting only (§11.10.1); no charging or quota logic reads this table today (§5.12).

---

## 7. Host interface and executor

### 7.1 Deployment model

```
 Deployment: svc-process                  Deployment: svc-process-executor
 RuntimeClass: (cluster default)          RuntimeClass: (default; gVisor optional)
 ┌──────────────────────────────────┐     ┌──────────────────────────────────┐
 │ svc-process                      │     │ bundle-executor                  │
 │  holds: Valkey ACL creds,        │     │  holds: its client certificate,  │
 │    Postgres role creds, platform │     │    read-only bucket credentials  │
 │    creds, egress HTTP client,    │     │  holds NOTHING of the stage's    │
 │    secret resolution, OTel       │     │                                  │
 │  listens :8201 (service HTTP)    │     │  rootfs read-only                │
 │          :9090 (metrics)         │     │  emptyDir /scratch  (16 Mi)      │
 │          :8301 (host API, mTLS)  │◀────┤  emptyDir /var/cache/waddles/wasm│
 │                                  │mTLS │  caps: drop ALL                  │
 └──────────────────────────────────┘     │  allowPrivilegeEscalation: false │
                                          │  runAsNonRoot, uid 10001         │
   CiliumNetworkPolicy                    │  seccompProfile: RuntimeDefault  │
     executor ─▶ stage :8301   ALLOW      │  egress ─▶ bucket (GET only)     │
     executor ─▶ bucket        ALLOW      └──────────────────────────────────┘
     executor ─▶ anything else DENY
     anything  ─▶ executor     DENY (no ingress at all)
```

The executor is a **separate workload**, not a child process. By default it runs under the cluster's own container runtime, hardened by rootless execution, all capabilities dropped, and `RuntimeDefault` seccomp; where a cluster supports it, an optional gVisor (`runsc`) `RuntimeClass` can additionally be layered on to give it a user-space kernel, so the escaped native code's syscalls are serviced by the sandbox rather than the host — §18 R3 found that layer undeliverable by default in every environment Waddles runs, which is why it is opt-in rather than assumed (D32). A separate feasibility spike proved bubblewrap cannot create namespaces inside our containers at all (§18, R3).

Three independent mechanisms make the executor credential-less in practice:

1. **Separate Deployment** — the stage's Secrets, ServiceAccount token and env are simply not mounted into the executor's pod. There is nothing to read.
2. **Rootless, capability-dropped, seccomp-filtered execution** (default) — a native-code escape out of wasmtime faces the host kernel's syscall surface, filtered by `RuntimeDefault` seccomp with every capability dropped, rather than an intercepting user-space kernel. An optional gVisor `RuntimeClass` adds that interception where the cluster can host it (default off — D32).
3. **Default-deny `CiliumNetworkPolicy`** — the only egress allowed is the stage's host-API port and the bucket; there is no ingress at all, so nothing in the cluster can reach the executor either.

The stage's `:8301`/`:8302` host-API listener is reachable only from the executor's pod selector; the same policy denies it to every other workload, so the capability-scoped API is not an internal side door.

### 7.2 Instance pool and concurrency

- One wasmtime `Engine` per executor process, configured with the component model, WASI 0.2, epoch interruption, and the pooling allocator.
- Per loaded bundle: `EXECUTOR_INSTANCES_PER_BUNDLE` (default `4`) pre-instantiated stores, checked out per call and reset afterwards. A call that finds the pool empty waits up to `EXECUTOR_POOL_WAIT_MS` (default `500`) and then fails with `HOST_CALL_FAILED`/`pool_exhausted`, which the stage treats as retryable and re-queues.
- Global ceiling: `EXECUTOR_MAX_CONCURRENT_CALLS` (default `32`) across all bundles.
- Every instance is **fresh per call** with respect to guest linear memory: no state survives between invocations. A bundle that needs state uses the `kv` or `db` capability. This is asserted by a test that sets a module-level counter in a guest and observes it reset.
- **Instances stay resident, and the precompiled artifact is what makes that affordable.** A Python component is large — the spike measured 21.6 MB — and round 2 measured the difference directly: **~4.5–5.4 ms to load a precompiled `.cwasm`, against 3.3–4.5 s uncached**, roughly three orders of magnitude. The executor therefore never instantiates from cold in the request path: it loads the `.cwasm` produced at `load` time and keeps `EXECUTOR_INSTANCES_PER_BUNDLE` stores warm. Warm call cost was 2–4 ms per `db` round trip through the WIT import, and the alias bundle's full write path (4 round trips) completed in 9–18 ms.
- **The precompile must use the same GC collector configuration as the runtime engine.** Round 2 found that a precompiled artifact produced under a different collector **fails to load** outright. `-C collector=drc` matched `componentize-py`'s output and is the pinned setting; the collector is part of the artifact's compatibility identity alongside the wasmtime version, so the cache key is `{digest}-{wasmtime_abi}-{collector}` and a mismatch on any component is discarded and recompiled rather than attempted. The executor asserts its own configuration at startup, alongside the sandbox check of §12.2: it reports `collector` in the `hello` frame, and a stage whose configured collector differs from the executor's refuses the connection with `PROTOCOL_VERSION` and an explicit message naming both values — a silently mismatched collector would otherwise surface as an unexplained load failure per bundle.

### 7.3 Limits

| Limit | Default | Per-bundle override | Hard ceiling | On breach |
|---|---|---|---|---|
| Wall-clock per call | `EXECUTOR_CALL_TIMEOUT_MS` = `2000` | `limits.timeout_ms` | `EXECUTOR_MAX_CALL_TIMEOUT_MS` = `10000` | epoch interrupt → `EXECUTOR_DEADLINE`, DLQ `call_timeout`, one trip |
| Linear memory | `EXECUTOR_MEMORY_LIMIT_MB` = `64` | `limits.memory_mb` | `EXECUTOR_MAX_MEMORY_LIMIT_MB` = `256` | allocation refused → guest trap, `MEMORY_LIMIT`, DLQ `memory_limit`, one trip |
| Egress rate | `EGRESS_RATE_LIMIT_RPS` = `10`, burst `20` | `limits.egress_rps` | `EGRESS_RATE_LIMIT_RPS` | `rate-limited(retry_after_ms)` returned to the guest, counter incremented; not a trip |
| Concurrent calls | `EXECUTOR_MAX_CONCURRENT_CALLS` = `32` | none | — | queued up to `EXECUTOR_POOL_WAIT_MS`, then retryable failure |
| Frame size | `EXECUTOR_MAX_FRAME_BYTES` = `1048576` | none | — | fatal protocol error; executor restarted |
| KV value size | `KV_MAX_VALUE_BYTES` = `65536` | none | — | `too-large` returned to the guest |
| KV TTL | `KV_MAX_TTL_S` = `2592000` (30 days) | none | — | clamped, logged at DEBUG |

A manifest value outside its hard ceiling fails validation V24 at install time — it is never silently clamped.

### 7.4 Host capability implementations (stage side)

| Capability | Implementation notes |
|---|---|
| `context` | Built once per `invoke` from the envelope the stage took off the key plus the 3-tier resolved config. Tenant and community come from the key, never from payload. |
| `http` | §8. |
| `kv` | Hash operations on `bundle_state_key(tenant, community, app_id)`; the guest's key is namespaced as `b:{key}` so a bundle cannot reach the stage's own fields. TTL applies to the whole hash via `HEXPIRE`-equivalent per-field expiry; where the deployed Valkey lacks per-field TTL the stage stores `{value, expires_at}` and filters on read. |
| `db` | The statement is parsed with a SQL parser (`sqlparser` crate) before execution; every referenced table must appear in `data.tables`, and any statement containing more than one top-level statement, a `COPY`, a `DO`, a `SET ROLE`, a `GRANT`, or a `CREATE`/`DROP`/`ALTER` is refused with `denied`. Execution then happens on a connection whose role is the bundle's own (`bundle_<app_id with dots and dashes replaced by underscores>`), with `SET LOCAL waddles.tenant`/`waddles.community` driving row-level-security policies on the bundle's tables. Two independent layers, deliberately: the parser catches mistakes, the role and RLS catch the parser being wrong. |
| `relay` | Validates `provider` against the compiled-in provider list (`twitch` today), then `LPUSH`es onto the provider-scoped relay key. Action-stage bundles only. |
| `flags` | `penguin-licensing`'s two-gate check with the bundle's Feature as the flag key. Fail-open to the supplied `default-value` on a flag-server outage, never an exception. |
| `log` | `fields-json` is sanitized with the `penguin-logging` `SENSITIVE_KEYS` rule before anything is emitted, then written at the requested level with `app_id`, `tenant`, `community` attached. A bundle cannot raise its own log level above the stage's configured `LOG_LEVEL`. |
| `clock` | Wall clock from the stage; monotonic from the stage's own `Instant`. The guest gets no other time source, which keeps timing side channels from being trivially precise. |

### 7.5 Sandbox trips and disabling

A **trip** is any of: a call deadline breach, a memory-limit breach, a guest trap, or the third `denied` host call within the window.

```
trips within EXECUTOR_TRIP_WINDOW_S (300 s), counted per (app_id, digest):
  1st  → WARN log, waddles_sandbox_trip_total{app_id,limit} +1, event to DLQ
  2nd  → WARN log, counter, event to DLQ
  3rd  → ERROR log, counter, event to DLQ,
         bundle marked DISABLED in this process,
         waddles_bundle_disabled{app_id} gauge = 1,
         every subsequent event for it goes straight to DLQ with
         error.kind = "bundle_disabled" (never silently dropped)
```

A disabled bundle is re-enabled when the pod observes a **new `artifactDigest`** for it from the distribution API, or when the pod restarts. There is no runtime re-enable switch (§19, Q1). Disabling is per-pod, not cluster-wide: one bad node does not take a bundle down everywhere, and the gauge makes partial disabling visible.

### 7.6 Bucket poller and hot-swap

The **stage** decides what should be loaded; the **executor** fetches and verifies it. The executor is the only one of the two with bucket access, and it is read-only.

**Reconciliation is purely by digest.** Every `BUNDLE_POLL_INTERVAL_S` (default `60`), the stage compares the digest set the distribution API advertises against the digest set its executors report as `loaded`, and acts only on the difference:

| Comparison | Action |
|---|---|
| Same `app_id`, same digest | **No-op.** Nothing is fetched, nothing is verified again, nothing is swapped — a steady-state poll costs one comparison per bundle. |
| Same `app_id`, different digest | Fetch, verify, precompile, hot-swap, unload the old digest after draining. |
| `app_id` present in the advertised set, absent locally | Add: fetch, verify, precompile, load. |
| `app_id` absent from the advertised set (deactivated, revoked, uninstalled) | Unload, and evict its cached artifacts. |

Version strings, timestamps and manifest text play no part in the comparison; the digest is the only identity. A version re-published with identical bytes is therefore correctly a no-op, and a rollback that points `app_active_versions` at an older row is just "different digest" and converges the same way as a roll-forward.

For each digest to add or change, the stage sends `load` with the expected `digest`, `component_key` and `sidecar_key`.

On `load`, the executor:

3. `GET`s `bundles/{app_id}/{version}/{sha256}.json` (the sidecar) and `bundles/{app_id}/{version}/{sha256}.wasm`.
4. Verifies: the Ed25519 signature on the sidecar against `BUNDLE_SIGNING_PUBLIC_KEY`; the sidecar's `digest` equals the `digest` the stage sent (which is hub-api's recorded value); the SHA-256 of the fetched component bytes equals both. Any mismatch → `error.code = "DIGEST_MISMATCH"`, the previous version keeps serving, `waddles_bundle_digest_mismatch_total{app_id}` +1, ERROR log on both sides naming all three values (truncated to 12 hex characters in the message; the full values go to the log fields).
5. Precompiles with its pinned wasmtime into `EXECUTOR_PRECOMPILE_DIR` (`/var/cache/waddles/wasm`, an `emptyDir`), keyed by `{digest}-{wasmtime_abi}-{collector}` and produced with **the same GC collector configuration the runtime engine uses** (`-C collector=drc`, matching `componentize-py`'s output — a mismatch makes the artifact unloadable, §7.2). An artifact whose `wasmtime_abi` or `collector` does not match the running engine is discarded and recompiled, never loaded.
6. Replies `loaded`, at which point the stage atomically swaps the routing entry and then sends `unload` for the old digest **after** its in-flight calls have drained (bounded by `EXECUTOR_DRAIN_MS`, default `5000`).
7. Old components are retained in the bucket for audit; each executor keeps at most `BUNDLE_CACHE_VERSIONS` (default `3`) precompiled versions per `app_id` and evicts the oldest.

Passing the expected digest in `load` is load-bearing: the executor can reach the bucket, so the bucket alone must never decide what runs. The stage's digest comes from hub-api's DB, and the executor refuses anything else.

**Bucket outage.** Fetch failures never stop the pipeline: executors keep serving the digests they already hold, log at WARN, and the stage advances `waddles_bundle_stale_age_seconds{app_id}` — the age of the newest digest an executor has *successfully verified* versus the digest the distribution API is currently advertising. An operator alert fires at `> 900` seconds.

**Rollback** is a control-plane action only: hub-api flips the active digest on the `app_catalog` version row back to a prior version, and pods converge within one distribution poll plus one bucket poll (≤ 65 s).

---

## 8. Egress model

### 8.1 Declaration

```yaml
egress:
  - host: "api.spotify.com"
    methods: ["GET", "POST"]
  - host: "*.googleapis.com"          # methods omitted ⇒ all six
```

- `host` is a lowercase FQDN or a single-label wildcard (`*.example.com` matches `a.example.com`, not `a.b.example.com` and not `example.com` itself).
- `methods` defaults to `GET, HEAD, POST, PUT, PATCH, DELETE`.
- The list is exhaustive: a request to any other host is denied.

### 8.2 Enforcement order

Every `http.send` runs this sequence on the **stage** side. Each step that rejects returns `denied(reason)` or `rate-limited(ms)` to the guest and increments `waddles_egress_denied_total{app_id,reason}`.

| # | Check | Denial reason |
|---|---|---|
| 1 | Scheme is `https` | `scheme_not_https` |
| 2 | URL has no embedded credentials, no fragment-only target, and parses cleanly | `malformed_url` |
| 3 | Host matches an `egress[].host` entry | `host_not_declared` |
| 4 | Method is in that entry's `methods` | `method_not_declared` |
| 5 | Host is not on the tenant-level global denylist (refreshed from hub-api every `EGRESS_DENYLIST_REFRESH_S` = `60`; last-known-good on outage) | `host_denylisted` |
| 6 | DNS resolution yields no address in a forbidden range: loopback (`127.0.0.0/8`, `::1`), private (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`), link-local (`169.254/16`, `fe80::/10`), unspecified, multicast, or the cloud metadata addresses (`169.254.169.254`, `fd00:ec2::254`). **The private-range half of this check is lifted when the tenant setting `bundles.egress.allowPrivateHosts` is true** (§8.5); loopback, link-local, unspecified, multicast and cloud metadata stay blocked in every configuration | `ssrf_blocked_address` |
| 7 | The connection is pinned to the resolved, checked address (no second resolution between check and connect) | `dns_rebind_blocked` |
| 8 | Per-bundle token bucket (`limits.egress_rps`, burst `EGRESS_RATE_LIMIT_BURST` = `20`) admits the call | *(returns `rate-limited`, not `denied`)* |
| 9 | TLS handshake completes at TLS 1.2 or better with a verified chain | `tls_verification_failed` |
| 10 | Redirects: at most `EGRESS_MAX_REDIRECTS` = `3`, and every hop re-runs steps 1–7 against the redirect target | `redirect_off_allowlist` |
| 11 | Response body within `EGRESS_MAX_RESPONSE_BYTES` = `1048576`; larger bodies are truncated and returned with `truncated: true` | *(not a denial)* |
| 12 | Total call within `EGRESS_TIMEOUT_MS` = `5000` | *(returns `timeout`)* |

### 8.3 Secrets by reference

A bundle never holds a secret value. It names one:

```python
headers = {}
secret_refs = {"Authorization": "SPOTIFY_BOT_TOKEN_REF"}
```

The `request.secret_refs` list maps a header name to a **secret reference name**. The stage resolves the reference the same way `waddle_transports.signing.resolve_secret` does today (an environment-variable *name* held in the activation config, resolved at call time), and injects the header immediately before sending. The resolved value:

- never crosses the WIT boundary,
- never appears in a log, span attribute or metric label (the sanitizer redacts it even if a bundle echoes it back),
- is dropped from the response the guest sees if the server reflects it.

An unresolvable reference returns `denied("secret_unresolved")` and is classified non-retryable.

### 8.4 Compiler and install-time checks

- The compiler rejects a manifest with an empty `egress` when the compiled component imports `waddle:bundle/http` (rule V22). The check is on the **component's actual import list**, not on the source text, so an undeclared import cannot slip through a dynamic call.
- The install UI shows the full `egress` list, the `data.tables` list and the `consumes` subscription on the approval screen; approving an install is approving all three.
- Changing `egress` or `data.tables` in a new version re-triggers approval; a version whose capability request is a strict subset of the approved one auto-approves.

### 8.5 The SSRF guard applies to bundles, never to infrastructure

**§8.2 governs bundle-initiated `http` calls through the host capability, and nothing else.** It does not, and must not, apply to the stage's own connections to the infrastructure it is configured with:

| Connection | Configured by | SSRF guard |
|---|---|---|
| Postgres (`DB_HOST`/`DATABASE_URL`) | operator | **Not applied** |
| Valkey (`VALKEY_URL`) | operator | **Not applied** |
| Artifact bucket (`BUNDLE_BUCKET_ENDPOINT`) | operator | **Not applied** |
| hub-api (`HUB_API_URL`) | operator | **Not applied** |
| OTLP collector (`OTEL_EXPORTER_OTLP_ENDPOINT`) | operator | **Not applied** |
| Platform APIs reached by svc-ingest/svc-action built-ins | operator + compiled-in | **Not applied** |
| Bundle `http.send` | bundle manifest | **Applied in full (§8.2)** |

These endpoints normally live on **private address space** — a cluster Service IP, an RFC 1918 VPC address, a LAN host — and reaching them is the ordinary case, not an attack. The distinction is provenance, not address range: operator configuration is trusted input, and a bundle's URL is not. A stage therefore resolves and connects to its configured endpoints without consulting the egress allowlist or the private-range rules at all; there is no code path in which an operator's `VALKEY_URL` is evaluated by the bundle egress guard.

**The per-tenant escape hatch for bundles.** Some legitimate integrations are self-hosted on the same LAN (a local media server, an on-prem ticketing system). `bundles.egress.allowPrivateHosts` (chart value, per-tenant override, default `false`, env `EGRESS_ALLOW_PRIVATE_HOSTS`) lifts **only** the private-range portion of step 6 for bundle calls whose host is already on the manifest's `egress` allowlist. Loopback, link-local, unspecified, multicast and the cloud metadata addresses remain blocked regardless — those have no legitimate integration use and are the actual SSRF targets. Enabling it is logged at WARN at startup and surfaced on the install/approval screen for every bundle in that tenant.

---

## 9. Bundle lifecycle

### 9.1 State machine

```
                     ┌──────────────┐
   POST /versions    │              │
   (source tarball   │   UPLOADED   │
    or prebuilt) ───▶│              │
                     └──────┬───────┘
                            │ hub-api creates the compiler Job
                            v
                     ┌──────────────┐   manifest invalid (V1–V24)
                     │  VALIDATING  │──────────────────────────────▶ REJECTED
                     └──────┬───────┘                                (reason code,
                            │ manifest OK                             terminal)
                            v
        artifact=source     │      artifact=prebuilt
        ┌───────────────────┴───────────────────┐
        v                                       v
 ┌──────────────┐  blocking finding      ┌──────────────┐  WIT/import/size
 │   SCANNING   │───────────────────────▶│  INSPECTING  │  check fails
 │ SAST · deps  │        REJECTED        │ WIT conform. │───────────────▶ REJECTED
 │ secrets ·    │                        │ import list  │
 │ Skauswatch   │                        │ manifest·size│
 └──────┬───────┘                        └──────┬───────┘
        │ clean or non-blocking findings        │ ok
        v                                       │
 ┌──────────────┐  compile fails                │
 │  COMPILING   │──────────────────▶ REJECTED   │
 │ componentize │                               │
 └──────┬───────┘                               │
        │ component produced                    │
        └───────────────┬───────────────────────┘
                        v
                 ┌──────────────┐
                 │  ADDRESSING  │  sha256 over the component bytes
                 └──────┬───────┘
                        v
                 ┌──────────────┐  upload or signing fails
                 │  PUBLISHING  │──────────────────────────▶ REJECTED
                 │ bucket PUT + │
                 │ Ed25519 sign │
                 └──────┬───────┘
                        │ digest recorded on the app_catalog version row
                        v
                 ┌──────────────┐
                 │  PUBLISHED   │  visible in the catalog, not yet running
                 └──────┬───────┘
                        │ permission summary built (§9.7)
                        v
                 ┌──────────────┐  admin denies, or a headless
                 │   AWAITING   │  install sends a stale hash
                 │   APPROVAL   │──────────────────▶ REJECTED
                 └──────┬───────┘
                        │ approved → app_install_approvals + audit
                        v
                 ┌──────────────┐  the record the runtime derives
                 │   APPROVED   │  grants, egress, table roles and
                 │              │  capability wiring from
                 └──────┬───────┘
                        │ activation (app_activations / app_tenant_availability)
                        v
                 ┌──────────────┐  new version published for the same app_id
                 │    ACTIVE    │──────────────────────────────▶ SUPERSEDED
                 │ served by the│                                (artifact retained
                 │ distribution │                                 for audit)
                 │ API, loaded  │
                 │ by pods      │
                 └──┬────────┬──┘
                    │        │ three sandbox trips in EXECUTOR_TRIP_WINDOW_S
                    │        └────────────────────────▶ DISABLED (per pod)
                    │                                    events → DLQ
                    │ activation removed / disabled          │
                    v                                        │ new digest
              ┌──────────────┐                               │ or pod restart
              │ DEACTIVATED  │◀──────────────────────────────┘
              │ artifact kept│
              └──────────────┘
```

### 9.2 Install (hub-api)

`POST /api/v1/apps/{app_id}/versions`, multipart:

| Part | Content | Required |
|---|---|---|
| `manifest` | `bundle.yaml` (v2) | yes |
| `source` | `.tar.zst` source tarball, ≤ `BUNDLE_MAX_SOURCE_BYTES` = `16777216` (16 MiB) | when `artifact: source` |
| `component` | `.wasm` component, ≤ `BUNDLE_MAX_COMPONENT_BYTES` = `33554432` (32 MiB) | when `artifact: prebuilt` |

Responses: `202 Accepted` with a `versionId` and the compiler Job name; `400` with a `reason` code for a manifest that fails a pure-YAML rule; `403 prebuilt_not_allowed` when `artifact: prebuilt` and the global setting `bundles.allow_prebuilt` is false; `409` when `(app_id, version)` already exists; `413` on an oversize part.

`GET /api/v1/apps/{app_id}/versions/{version}` returns the state-machine state, the reason code on rejection, `scanStatus`, the scanner findings summary, and — once published — the digest.

Approval endpoints (§9.7): `GET /api/v1/apps/{app_id}/versions/{version}/permissions` returns the permission summary and its `permission_hash`; `POST …/approve` records the approval (and requires a matching `permission_hash` from a headless caller); `POST …/deny` moves the version to `REJECTED` with the admin's reason.

### 9.3 Security check detail

**Source uploads (`artifact: source`)**, in the compiler's sandboxed phase 2:

| Check | Tool | Gate |
|---|---|---|
| Static analysis | `semgrep` with the Waddles ruleset | Any `ERROR`-severity finding blocks. |
| Dependency audit | `pip-audit` (Python), `cargo audit` (Rust), `npm audit` (JS/TS) | Any `high`/`critical` advisory blocks. |
| Secrets | `gitleaks detect` over the extracted tarball | Any finding blocks. |
| Malware / composite | Skauswatch, **when configured** (`SKAUSWATCH_URL` set) | A `fail` verdict blocks; `warn` records a finding. |
| Manifest ↔ source agreement | compiler | A declared `stages.<s>.entry` that does not resolve blocks. |
| Module pre-import (Python) | compiler | `pkgutil.walk_packages` over the bundle package, pre-importing every module so `componentize-py`'s static discovery does not miss a lazy import. A module that fails to import blocks, naming the module. |
| Legacy DAL imports (Python) | compiler | Any import of `flask_core.database` or `pydal`, anywhere in the bundle source, **blocks** with `reason: legacy_dal_import` and a message naming the module, the file and line, and the `penguin-dal` equivalent (D21b). |

Every scanner run reports **how many items it examined** (files scanned, dependencies audited, rules evaluated). A scanner that examined zero items is a **failure**, not a pass — the version is rejected with `reason: scan_empty_denominator`. No scanner invocation is wrapped in `|| true`.

**Prebuilt uploads (`artifact: prebuilt`)**, in phase 3:

| Check | Gate |
|---|---|
| Valid WASI 0.2 component, parses under the pinned wasmtime | blocks |
| Import allowlist, per the language's row in §6.5 — implemented with `wasm-tools component wit` plus the allowlist check. The ~150-line validator written for the compiler-sandbox spike is the **reference implementation**: it accepted all three good Tier 1 components and rejected both the wrong-world and the extra-`wasi:sockets` components with exact messages | blocks |
| Exports satisfy the WIT world for every declared script stage (V25) | blocks |
| Imports are a subset of the WIT world's imports within the language's allowlist of §6.5 (V31), and the component instantiates against the executor's implementations | blocks |
| Manifest checks V1–V30, including the `consumes` rules (V27–V30) | blocks |
| Size ≤ `BUNDLE_MAX_COMPONENT_BYTES` | blocks |
| Digest computed and recorded | — |

Result: `scanStatus: "not_scanned"`. That value is **permanent for the life of the version** and drives a "not security-scanned" badge everywhere the bundle is listed (marketplace, install screen, activation screen, admin bundle list). It is never upgraded by a later scan of a different artifact.

### 9.4 Publish

Everything below runs in the **publisher** container (§4.6) — never in the build container that executed bundle code.

0. Read the candidate component out of the shared `/work` `emptyDir`, validate it (`wasm-tools component wit` plus the per-language import allowlist of §6.5), and reject the version if validation fails. The publisher trusts nothing the build container wrote; it re-derives everything from the bytes.
1. Compute `sha256` over the component bytes, and separately over the precompiled `.cwasm`.
2. `PUT bundles/{app_id}/{version}/{sha256}.wasm`.
3. Build the sidecar and sign it with the deploy key (Ed25519, private half held only by the compiler Job's secret):

```json
{
  "schema_version": 1,
  "app_id": "waddles.socials.music.default",
  "version": "3.0.0",
  "digest": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "size_bytes": 2148231,
  "language": "python",
  "artifact_kind": "source",
  "scan_status": "scanned",
  "wit_world": "waddle:bundle/stage@1.0.0",
  "built_at": "2026-09-14T12:00:00.000Z",
  "builder": "bundle-compiler@1.0.0",
  "signature": "base64(ed25519 over the canonical JSON of every field above)"
}
```

4. `PUT bundles/{app_id}/{version}/{sha256}.json`.
5. `INSERT` the `app_versions` row (§6.10) **directly**, over the publisher's own `waddles_publisher` Postgres role — the DB write is the commit point, and the publisher is the only writer of a digest anywhere in the system. A bucket object with no row is unreferenced and is garbage-collected by a weekly hub-api job after `BUNDLE_ORPHAN_GRACE_H = 168`.
6. Notify hub-api that the version exists. **This callback is a notification, not the digest authority**: hub-api re-fetches the bucket object, re-hashes it, compares the result against the row the publisher inserted, and on a mismatch refuses to let the version be approved, raises an alert and writes an audit entry naming both digests. A cross-check that can only ever agree with itself proves nothing — hub-api measures independently, from the bucket, exactly as a pod does at load time.

### 9.5 Activation and rollout

Activation is unchanged in shape: a row in `app_activations` (community-scoped) or `app_tenant_availability` (tenant-wide), with the same 3-tier config precedence. What changes is that the distribution API now also hands the pod the digest, the capability-bearing manifest subset, and the resolved stream grants, so a pod can go from "this bundle is activated" to "this exact artifact reads exactly these streams" without a second lookup.

Activation additionally performs the grant resolution of §5.2: expand `consumes` against the configured ingest sources, write `app_stream_grants` rows, and `XGROUP CREATE ... MKSTREAM` (BUSYGROUP-tolerant) on each granted stream. Deactivation destroys those groups and marks the rows revoked.

Convergence bound after an activation, a revocation or a rollback: one distribution poll (≤ 5 s) + one bucket poll (≤ 60 s) = **≤ 65 seconds**.

### 9.6 Grant management (hub-api)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/apps/{app_id}/grants` | List the bundle's stream grants for a tenant/community, with labels — the same rendering the install screen shows |
| `POST /api/v1/apps/{app_id}/grants/resolve` | Re-run resolution (idempotent); called automatically when an ingest source is added or removed, and available to an admin |
| `DELETE /api/v1/apps/{app_id}/grants/{grantId}` | Revoke one grant without uninstalling or deactivating the bundle: sets `revoked_at`, destroys the consumer group, writes an audit entry with the actor's UUID |

All three require an admin scope; every mutation is audit-logged. Revoking the last grant leaves the bundle activated and reading nothing, which is a legitimate configuration, not an error.

### 9.7 Install-time permission consent

A bundle's full contract is presented to the global admin at install as an explicit consent step — the Android "permissions this app needs" model. Nothing about a bundle's reach is discovered at runtime.

#### 9.7.1 The permission summary

Derived from `bundle.yaml` **plus the validated component's actual host imports**, so a manifest that under-declares cannot hide anything: the summary is built from what the component really imports, cross-checked against what the manifest asked for, and a discrepancy fails the install (V25/V31).

| Section | Rendered as |
|---|---|
| **Ingest streams** | The resolved grant list in words — "reads Twitch #channelA, Discord guild X" — with wildcards spelled out into the concrete sources they currently resolve to, a note that a `platform`-wide rule will also pick up sources added later (§5.2), and each source's configured authentication mode shown alongside it (signature, IP allowlist, bearer, or basic — §10.3, §4.1.1) |
| **Outbound network** | Each `egress` host with its allowed methods; `*.example.com` shown as a wildcard, not flattened |
| **Database** | Each `data.tables` entry with **read / write** derived from the statements the component can issue, one line per table |
| **Host capabilities** | Which of `http`, `kv`, `db`, `relay`, `flags`, `log` the component actually imports (`context` and `clock` are always granted and are not listed as permissions) |
| **Cross-bundle routing** | "may send events to app X" for each `routes_to` entry (§5.9); absent when the list is empty |
| **Resource limits** | `timeout_ms`, `memory_mb`, `egress_rps` requested, each shown against the platform ceiling |
| **Provenance** | `language`, `artifactKind`, and `scanStatus` — a source upload shows its scanner summary; a prebuilt upload shows the permanent **"not security-scanned"** badge |
| **Entitlement** | `min_tier` and the PostHog feature flag key that gates the bundle |
| **Unusual requests** | Called out separately and first: a private-host egress request (`bundles.egress.allowPrivateHosts`), a wildcard `consumes`, a `routes_to` list, a limit at the platform ceiling, or a prebuilt artifact |

#### 9.7.2 Approval

The admin approves or denies. Approval writes one `app_install_approvals` row (§6.9) and an audit entry:

- `permission_hash` = SHA-256 over the **canonical JSON** of the summary (keys sorted, no insignificant whitespace, arrays in a defined order), so the same permissions always hash the same way.
- `approver` is a user **UUID** into the single `users` identity table — never a name or an email (PII tokenization).
- The approved summary JSON is stored in full alongside the hash, so an auditor can see what was agreed without reconstructing it.

#### 9.7.3 The runtime enforces the approval, not the manifest

This is the load-bearing half. Stream grants, the egress allowlist, the per-bundle Postgres role's table grants, and the executor's capability wiring are all generated **from the approval record**. A manifest can never widen access without a new approval, because nothing at runtime reads the manifest for authorization — the distribution API serves the approved set, and §14.6 asserts that a manifest requesting a table outside the approval is denied at runtime and counted.

#### 9.7.4 Upgrades

On a new version, hub-api diffs the new summary against the approved one:

| Change | Behaviour |
|---|---|
| **Widening** — a new stream, host, method, table, capability, `routes_to` target, or a higher limit | The version is published but **not activated**. The admin sees a field-by-field diff of exactly what is new and must re-approve. |
| **Narrowing** — anything removed or lowered | Auto-approved, with an audit entry recording the narrowing and the new `permission_hash`. |
| **Unchanged** | Auto-approved; the `permission_hash` matches and the row records the new version against the same hash. |

#### 9.7.5 Headless installs

A CLI or API install must pass the `permission_hash` it expects to approve:

```
POST /api/v1/apps/{app_id}/versions/{version}/approve
{"permission_hash": "sha256:…", "scope": {"tenant": "acme", "community": "main"}}
```

A mismatch **fails closed** with `409 permission_hash_mismatch` and returns the current summary and hash — a non-interactive installer can never approve permissions it has not seen. `GET …/permissions` returns the summary and hash for the caller to inspect first.

---

## 10. Ingest intake

### 10.1 Endpoint table

All routes are on `:8200`. `INTAKE_MAX_BODY_BYTES` = `262144` (256 KiB) applies to every POST. Every rejection increments `waddles_intake_rejected_total{source,reason}`.

| Method + path | Auth | Required headers | Limits | Success | Error codes |
|---|---|---|---|---|---|
| `POST /eventsub/twitch/webhook` | HMAC-SHA256 hex over `id + timestamp + body`, prefixed `sha256=`, constant-time compare; **and** origin restriction — FCrDNS or a pinned CIDR allowlist to `twitch.tv` (§4.1.1) | `Twitch-Eventsub-Message-Signature`, `-Timestamp`, `-Id`, `-Type` | 256 KiB body; 600 s timestamp window (Twitch's own); per-source bucket 20/40 | `200` — and for `webhook_callback_verification`, the bare `challenge` string echoed as `text/plain`, not JSON | `400 malformed_body`, `401 bad_signature`, `403 replay_window`, `403 origin_not_trusted`, `409 duplicate_message_id`, `413 body_too_large`, `429 rate_limited` (+ `Retry-After: 1`), `503 secret_unset`. Route is **not registered at all** when `TWITCH_EVENTSUB_SECRET` is unset. |
| `POST /webhook/kick` | HMAC-SHA256 hex over the raw body; fail-closed on a missing or empty signature; **and** origin restriction — FCrDNS or a pinned CIDR allowlist to `kick.com` (§4.1.1) | `X-Kick-Signature` | 256 KiB; per-source bucket 20/40 | `200` | `400`, `401 bad_signature`, `403 origin_not_trusted`, `413`, `429`, `503 secret_unset` (route always mounted) |
| `POST /intake/webhook/{tenant}/{source}` | Per-source HMAC-SHA256 hex over `{timestamp}.{raw_body}`, secret fetched from hub-api and cached 60 s; **and** the source's configured second factor — IP allowlist, bearer token, or HTTP basic (§4.1.1, §10.3) — required in addition to the signature | `X-Waddles-Signature: sha256=<hex>`, `X-Waddles-Timestamp: <unix seconds>`; optional `X-Waddles-Delivery-Id` | 256 KiB; `INTAKE_REPLAY_WINDOW_S` = 300 s; per-source bucket 20/40; per-tenant bucket 100/200 | `202 Accepted` with `{"accepted": true, "events": <n>}` | `400 malformed_body`, `401 bad_signature`, `401 second_factor_failed`, `401 auth_not_configured`, `403 replay_window`, `404 unknown_source`, `409 duplicate_delivery_id`, `413`, `422 mapping_failed`, `429`, `503 source_disabled` |
| `POST /intake/events` | hub-api-issued JWT, `Authorization: Bearer <jwt>`, scope `intake:write`, mandatory `tenant` claim; may additionally require a per-caller IP allowlist (§4.1.1) | `Authorization`, `Content-Type: application/json` | 256 KiB; per-tenant bucket 100/200 | `202 Accepted` with `{"accepted": true, "events": 1}` | `400 malformed_body`, `401 invalid_token`, `403 missing_scope` / `tenant_mismatch` / `platform_not_registered` / `ip_not_allowed`, `413`, `422 envelope_invalid`, `429` |
| `GET /health` | none | — | — | `200` with the body of §11.6.4 | `503` when not ready |
| `GET /healthz` | none | — | — | `200 ok` | — |
| `GET /metrics` | none (cluster-internal, `:9090`) | — | — | `200` Prometheus text | — |

`Retry-After: 1` accompanies every `429`. Duplicate-suppression (`409`) uses a Valkey set `waddles:intake:seen:{source}` with a `INTAKE_DEDUPE_TTL_S = 900` expiry, keyed by the platform's own message id or `X-Waddles-Delivery-Id`.

### 10.2 Fixed platform inputs

Ported with today's exact authentication and lifecycle behaviour (`core/svc_ingest/receivers/*.py`, `eventsub.py`, `bundles/kick_ingest.py`):

| Input | Mechanism | Credential | Lease |
|---|---|---|---|
| Twitch EventSub **webhook** | Inbound HTTP, HMAC as above | `TWITCH_EVENTSUB_SECRET` | n/a |
| Twitch EventSub **websocket** | Outbound WS to Twitch, per tenant; **alternative to the webhook, mutually exclusive** — selected by `TWITCH_EVENTSUB_MODE` | `TWITCH_BOT_TOKEN_REF` | single-owner |
| Twitch IRC | One TCP/TLS connection per channel | `TWITCH_BOT_TOKEN_REF` | single-owner, per `(provider, community)` |
| Discord gateway | One gateway connection | `DISCORD_BOT_TOKEN` | single-owner, `PLATFORM_COMMUNITY` |
| Slack Socket Mode | One WS | `SLACK_APP_TOKEN` (`xapp-`) + `SLACK_BOT_TOKEN` (`xoxb-`) | single-owner |
| YouTube Live | Data API v3 poll per channel, with the existing no-broadcast and quota backoff | API key or OAuth2 refresh trio | single-owner |
| Kick Pusher | One WS per channel slug | public Pusher app key | single-owner |
| Kick webhook | Inbound HTTP, HMAC, fail-closed | `KICK_WEBHOOK_SECRET` | n/a |
| Twitch outbound relay | Blocking pop on a dedicated connection (§5.5) | `TWITCH_BOT_TOKEN_REF` | single-owner, `PLATFORM_COMMUNITY` |

**Leases.** The single-owner lease is the existing `SET NX PX` mechanism from `core/svc_ingest/socket_lease.py`, ported to `penguin-spine`: key `waddles:lease:{provider}:{community}`, TTL `SOCKET_LEASE_TTL_MS` = `30000`, renewed every `SOCKET_LEASE_RENEW_MS` = `10000`, released on graceful shutdown. Losing the lease stops the receiver within one renewal interval. The supervisor restarts an exited receiver with exponential backoff (base 1 s, cap 60 s), unchanged.

### 10.3 Generic webhook intake and its mapping

A **source** is a per-tenant record held by hub-api: `{tenant, source, platform, secret_ref, community, mapping, enabled, auth}`. `platform` must be one of the tenant's registered custom platforms (see §10.4).

**`auth`** (§4.1.1, §11.1) declares the source's required authentication on top of the mandatory per-source HMAC: `auth = {modes: ["hmac", "ip_allowlist"|"bearer"|"basic", ...], cidrs: [...], secret_ref: "<id>", origin_suffixes: [...], origin_cidrs: [...]}`. `modes` always includes `"hmac"` plus, for a **generic** source, at least one of `ip_allowlist`, `bearer` or `basic` — hub-api's source create/update endpoint rejects a generic source whose `modes` carries no second factor with `422 auth_factor_required`. `cidrs` backs `ip_allowlist`. Bearer tokens and basic credentials are **never** stored inline and **never** returned by any API response — they live in the secret store, referenced by `secret_ref`, and are delivered to svc_ingest through the same per-source config lookup (hub-api, cached 60 s) that already serves the HMAC secret (§10.1, §11.5); only the reference travels in the polled config, never the value. `origin_suffixes` and `origin_cidrs` apply to the built-in Twitch/Kick sources, seeded from the chart defaults (`ingest.platforms.*`, §12.3) and overridable per source. Every change to a source's `auth` object is audit-logged (actor, old/new `modes`, timestamp), the same way grant and approval changes already are (§6.8, §9.7.2). The configured mode is shown on the source's config view in the admin UI, and alongside each source's name wherever the consent screen lists resolved sources (§9.7.1).

The mapping is declarative JSON — no expressions, no code:

```json
{
  "event_type": {"pointer": "/type", "default": "custom.event"},
  "actor":      {"pointer": "/user/id"},
  "occurred_at": {"pointer": "/created_at", "default": "$now"},
  "community":  {"pointer": "/channel/id"},
  "payload": {
    "text":       {"pointer": "/message/text"},
    "channel_id": {"pointer": "/channel/id"},
    "message_id": {"pointer": "/id"}
  }
}
```

| Rule | Behaviour |
|---|---|
| `pointer` | RFC 6901 JSON Pointer against the request body. |
| Missing pointer target | Use `default` if present; otherwise `422 mapping_failed` with the failing field named. |
| `"$now"` | The only allowed magic default; resolves to the stage's current RFC 3339 UTC millisecond timestamp. |
| Scalar to string | JSON numbers and booleans targeting a string field are rendered in their JSON text form. Objects and arrays targeting a string field are a `422`. |
| `payload.*` | Values keep their JSON type; the assembled `payload` is always a JSON object. |
| `community` | Resolves the envelope's community. Absent ⇒ the source's configured `community`; that absent too ⇒ tenant-wide (`None`). |
| `platform` | Never taken from the body — always the source record's `platform`. |

The resulting `PlatformEvent` is validated against §6.1 before anything is enqueued; a mapping that produces an invalid event is a `422`, never a DLQ entry.

### 10.4 Generic REST intake

`POST /intake/events` takes a **strict `PlatformEvent`** as its body — the exact shape of §6.1.1, no mapping layer. The caller's JWT is issued by hub-api, carries scope `intake:write` and a mandatory `tenant` claim, and is verified for `iss`, `aud`, `exp` and `scope`. A token without a `tenant` claim is rejected, never defaulted.

`platform` is restricted to the platforms registered for that tenant (`custom_platforms` rows in hub-api). A caller attempting `platform: "twitch"` through this route gets `403 platform_not_registered` — the built-in platforms are reachable only through their own authenticated connectors, so a REST caller cannot forge a Twitch event.

Optional query parameter `community=<slug>` sets the envelope community; absent ⇒ tenant-wide.

### 10.5 Common intake behaviour

- **Rate limiting** is a token bucket at two levels, per source and per tenant, with the defaults in §4.1. Exhaustion returns `429` with `Retry-After: 1` and increments `waddles_intake_rejected_total{source,reason="rate_limited"}`. Signature verification happens **after** the rate-limit check, so an unauthenticated caller cannot make the pod hash unbounded bodies.
- **Body caps** are enforced while reading, streaming-wise: the reader aborts at `INTAKE_MAX_BODY_BYTES + 1` bytes rather than buffering the whole oversize body.
- **Constant-time comparison** (`subtle::ConstantTimeEq`) for every signature check; a length mismatch still performs the comparison.
- **Activation resolution** is via the distribution API for every route, so per-community activation is honoured uniformly (§15.3).
- **Rejection metrics** carry `reason` values drawn from the error-code column of §10.1, so a dashboard can distinguish a misconfigured sender from an attack.
### 10.6 One write per event

Ingest does **not** fan out. It receives or polls each event once, normalizes it, and writes it once onto the stream belonging to the ingest source it came from:

```
for each normalized PlatformEvent E from source S at (tenant, community):
  XADD waddles:t:{tenant}:c:{community|_tenant}:src:{S.platform}:{S.source_id}:events
       MAXLEN ~ {SPINE_STREAM_MAXLEN} * env {StageEnvelope(E).json}
  waddles_stream_events_total{platform,source_id} += 1
```

- **Ingest holds no subscriber list and consults no manifest.** Which bundles read the stream is decided at activation, by hub-api's grant resolution (§5.2), and enforced by the process stage. Ingest's cost per event is one `XADD` whether the stream has zero subscribers or forty.
- **No zero-match case exists at write time.** An event nobody subscribed to is simply an entry with no consumer group reading it; it ages out under `MAXLEN ~`. Nothing is counted as a drop and nothing reaches a DLQ, which stays reserved for events that were accepted by a bundle and then failed.
- **The stream is chosen from `PlatformEvent.source`, never from payload.** `source.platform` and the source's `source_id` name the stream; tenant and community come from the ingest configuration, exactly as before.
- The one write is the only Valkey operation on the ingest hot path, which is what keeps a chat-heavy channel from costing ingest anything proportional to the number of installed bundles.

- **Ingest's own outbound connections are infrastructure, not bundle egress.** The platform sockets and REST calls (Twitch IRC and EventSub, the Discord gateway, Slack Socket Mode, the YouTube poll, Kick), the distribution-API poll to hub-api, and the Valkey connections are all operator-configured and compiled-in; none of them is evaluated by the bundle SSRF guard, and all of them may resolve to private address space (§8.5). svc-ingest links no executor and serves no bundle `http` capability at all, so the guard has no subject in this service.

---

## 11. Security model

### 11.1 Threat model

**Primary adversary:** a malicious or merely buggy app bundle. Bundles may be written by anyone — first-party, marketplace vendor, or a community member — and after this design there is no execution tier that skips the sandbox (D8).

**The governing assumption:** *the WASM sandbox will eventually be escaped.* wasmtime is well engineered, but a single CVE in the runtime, or a logic bug in a host implementation, is enough. The design is therefore built so that **an escape from WASM reaches nothing but the mTLS host-API port and the artifact bucket**: the executor workload holds none of the stage's credentials, runs rootless with every capability dropped under `RuntimeDefault` seccomp — optionally on a user-space kernel where an operator has opted into gVisor, default off everywhere (D32) — and is confined by a default-deny network policy whose only two allowed destinations are the stage's capability-scoped host API — which authorizes every request against the escaped bundle's own manifest — and read-only bucket `GET`s.

| Threat | Mitigation | Residual risk |
|---|---|---|
| Bundle reads another tenant's data | `db` statements run under the bundle's own Postgres role with RLS bound to the envelope's tenant/community, which come only from the Valkey key. Table allowlist checked twice (parser + role grants). | A bug in the RLS policy for a bundle-owned table. Mitigated by the policy being generated, not hand-written, and by a test per table. |
| Bundle exfiltrates data to an attacker host | Declared-egress allowlist + SSRF rules + tenant denylist + install-time approval of the list. | A bundle exfiltrating through an approved host (e.g. posting to its own legitimate API). Accepted: approving an egress host is approving that reach. |
| Bundle steals a platform token | Tokens never cross the WIT boundary; the stage injects them by reference after the guest's request is authorized. | A bundle inducing the stage to send a token to an approved-but-hostile host. Same accepted risk as above. |
| Bundle escapes WASM | The executor is a separate, credential-less Deployment, rootless, all capabilities dropped, read-only rootfs, `RuntimeDefault` seccomp, default-deny NetworkPolicy with two allowed destinations and no ingress; an optional gVisor `RuntimeClass` may be layered on where the cluster can host it, default off everywhere (D32). | **Default posture:** the escape reaches the host kernel's syscall surface directly, filtered by seccomp and dropped capabilities rather than intercepted by a smaller user-space kernel first. Mitigated by the credential absence and network policy, which carry most of the claim regardless; not eliminated. Where gVisor is opted in, the escape instead meets the sentry's smaller surface first. |
| Bundle exhausts the pod | Per-call epoch deadline, memory cap, instance-pool ceiling, three-strike disable. | A bundle that is slow but under the deadline on every call. Visible in `waddles_executor_call_seconds`. |
| Malicious bundle **at build time** (`componentize-py` executes module-level code) | The compiler Job's `build` container runs rootless, no credentials, all capabilities dropped, with a network policy allowing only the bucket and the hub-api callback (reached only via the trusted `publisher` container); the same optional gVisor `RuntimeClass` as the executor may be layered on, default off (D32). | A compiler-toolchain vulnerability. Mitigated by pinned toolchains with checksums. |
| Supply-chain: a hostile artifact swapped in the bucket | Content addressing + Ed25519 sidecar signature + digest cross-check against hub-api's DB; refuse on mismatch, keep the old version. | Compromise of both the DB and the signing key. |
| A compromised build stage chooses its own digest | The digest is computed in the trusted `publisher` container from the bytes in the shared `emptyDir`, never in the `build` container that ran bundle code; hub-api independently re-hashes the bucket object and audit-logs any disagreement (D27). | A compromise of the publisher itself, which runs no guest code and has a far smaller attack surface. |
| An unexpected writer rewrites a digest row | Exactly two Postgres roles may write `app_versions`; every other role has no privileges on it, asserted by a CI test per role. Every write is audited with the role, key and old/new digest (§6.10). | A compromise of one of the two legitimate writers — which the audit trail then attributes. |
| Forged inbound events | Per-platform signature verification, per-source HMAC with a replay window, JWT with a mandatory tenant claim, built-in platforms unreachable through the generic REST route — **and, per source, a required second factor** (IP allowlist, bearer token or HTTP basic for generic webhooks; FCrDNS or a pinned CIDR to the platform's own domain for Twitch/Kick) on top of the signature, never the signature alone (§4.1.1). | A leaked per-source secret **combined with** a satisfied second factor — e.g. an attacker who also holds the bearer token, reaches an allowlisted address, or originates from inside the platform's own IP/DNS space. Mitigated by rotation, by secrets never leaving hub-api's store, and by the second factor traveling out-of-band from the secret. |
| A forged Twitch/Kick webhook, signed with a leaked or guessed platform secret but sent from a non-platform origin | Origin restriction — FCrDNS to `twitch.tv`/`kick.com`, or a pinned CIDR allowlist — required in addition to platform signature verification: **webhook authenticity is signature AND an origin/auth factor, never signature alone** (§4.1.1). | A host compromised inside the platform's own IP/DNS space, or an operator-configured CIDR allowlist set too broad. |
| Prompt/config injection through event payload | Tenant and community are never read from payload; `_target_app_id` changes only the destination key's `app_id` segment. | None structural; enforced by test. |
| A cross-tenant workstream hop — via a malicious bundle setting tenant/community/workstream fields on its output, a compromised stage, or a forged envelope written directly into Valkey | Every hop verifies `binding.mac` (HMAC over tenant‖community‖workstream_id‖event_id‖trace_id, keyed by `k_binding[kid]`, held only by the Rust stage services) plus the tenant/community/grant/approval chain, before any other processing; a bundle-supplied identity field is dropped and counted rather than trusted; `routes_to` cross-tenant targets are refused both at approval and independently at runtime (D30, §5.11). | Compromise of the binding key itself, or of two colluding stage replicas — mitigated by `kid` rotation with an overlap window and by the key never leaving the stage services (§12.3). |

### 11.2 Sandbox layers

Defence in depth, outermost first. Every layer is independently testable and each has a negative test in §14.6.

**Re-rated 2026-09-21 (D32):** layer 3 is absent by default in every environment Waddles runs — it is opt-in, not assumed. Layer 2 (rootless, dropped capabilities, `RuntimeDefault` seccomp) is what a native-code escape actually meets first today, not a user-space kernel; every other layer is unchanged and now carries more of the design's claim.

| # | Layer | What it stops |
|---|---|---|
| 1 | **Workload separation** — the executor is its own Deployment, so the stage's Secrets, ServiceAccount token and environment are never mounted anywhere the bundle runs | Reading the stage's credentials at all, by any means |
| 2 | **Kubernetes pod security** — `runAsNonRoot: true`, `runAsUser: 10001`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`, read-only root filesystem, Pod Security Admission `restricted` | Privilege escalation toward the node |
| 3 | **gVisor `RuntimeClass` (`runsc`) — optional, off by default everywhere (D32).** Where enabled, syscalls are serviced by a user-space kernel, verified at startup (§12.2); §18 R3 found no environment Waddles runs can sustain it as a default (no addon on MicroK8s, not survivable on DOKS) | A native-code escape reaching the host kernel's syscall surface — **when enabled.** Absent by default, so layer 2 is the operative boundary against this specific threat in every environment we run today |
| 4 | **Default-deny `CiliumNetworkPolicy`** — egress only to the stage's host-API port and the bucket; **no ingress at all** | An escape reaching Valkey, Postgres, the platform APIs, the cluster, or the internet; and anything in the cluster reaching the executor |
| 5 | **mTLS on the host API** — SPIFFE X.509-SVIDs where SPIRE is live, chart-provisioned certificates otherwise, peer identity pinned | An unauthenticated or impersonating client using the capability API |
| 6 | **WASM component isolation** — wasmtime, component model, no `wasi:sockets`, no `wasi:filesystem` beyond a read-only empty `/scratch`, no `wasi:cli/environment` | The guest reaching anything the host did not hand it |
| 7 | **Capability scoping** — grants computed from the manifest, enforced on the stage side | A guest calling a capability its manifest never requested |
| 8 | **Per-call resource limits** — epoch deadline, memory cap, pool ceiling | Denial of service against the pipeline |
| 9 | **Three-strike disable** — trips counted per `(app_id, digest)` over a 300 s window | A bundle repeatedly tripping limits and degrading the stage |

### 11.3 What an escape can and cannot reach

| Reachable from a fully escaped executor | Not reachable |
|---|---|
| The executor's own memory (the bundle's own data) | The stage's environment, Secrets or ServiceAccount token — none are mounted in this pod |
| Its own read-only rootfs, its 16 Mi `/scratch` `emptyDir`, and the precompiled component cache | Valkey, Postgres, the platform APIs, the internet — the NetworkPolicy denies every destination but two |
| The stage's mTLS host-API port, where every request is authorized against the escaped bundle's own manifest | Platform tokens, DB passwords, Valkey ACL credentials, the bucket **write** key — none are in this pod |
| Read-only `GET`s against the artifact bucket | Another bundle's manifest, config, KV namespace or tables — the stage authorizes per `app_id` |
| The host kernel's syscall surface, filtered by `RuntimeDefault` seccomp with every capability dropped (default posture, D32) — or, only where an operator has opted into gVisor, the sentry's smaller user-space surface instead | The node filesystem, other pods, the kubelet — regardless of posture |

The worst outcome of a full escape is therefore: the attacker can issue exactly the host calls the escaped bundle was already entitled to issue, at the rate limits that bundle already had, plus read the artifact bucket's already-public-to-the-cluster components — and, in the **default** configuration (`sandbox.gvisor.enabled: false`, D32, every environment Waddles runs today), the host kernel's syscall surface itself, filtered by `RuntimeDefault` seccomp with all capabilities dropped rather than intercepted by a user-space kernel first. That is the design's central claim as it actually ships, and §14.6's tests assert each half of it.

**Where `sandbox.gvisor.enabled: true`** — opt-in, only where the cluster can sustain it (§12.2.1) — the escape instead meets the gVisor sentry's smaller, better-audited surface before the host kernel. Every other row is unchanged either way: the credential absence and the network policy, which carry most of the claim, never depended on gVisor.

### 11.4 Egress

See §8. Summarized as a security property: no bundle reaches the network except through a stage-side client that checks scheme, declared host, declared method, tenant denylist, resolved-address SSRF rules, DNS-rebind pinning, rate limit, TLS 1.2+ with verification, redirect re-checking, response size and total timeout — in that order, with a metric on every denial.

**Scope, stated as a security property (§8.5):** the SSRF rules constrain **bundle-supplied URLs**, not operator-supplied configuration. A stage's connections to Postgres, Valkey, the artifact bucket, hub-api and the OTLP collector are configuration, live on private address space by design, and are never evaluated against the bundle egress guard. Conflating the two would break every normal deployment while protecting nothing: the threat the guard addresses is a bundle choosing a destination, and an operator's `VALKEY_URL` is not a bundle choosing anything. The one deliberate overlap is `bundles.egress.allowPrivateHosts`, which lets an operator extend the *bundle* guard to private hosts already on a manifest's allowlist, while loopback, link-local and cloud metadata stay blocked in every configuration.

### 11.5 Secrets

- **Never in a distributed artifact.** No bundle component, no image layer, no chart default contains a secret value.
- **Env-var-name indirection**, unchanged from `waddle_transports.signing.resolve_secret`: activation config carries `*_token_ref`, naming an environment variable the **stage** reads at call time.
- **Never on a CLI flag.** Every service reads secrets from environment variables or files only (`clap` config mirrors `core/svc_streaming/src/config.rs`).
- **Never logged.** `penguin-logging` sanitizes the `SENSITIVE_KEYS` set by exact key and substring at every level, DEBUG included; a token that somehow reaches a log field is rendered `[REDACTED]`.
- **Never in a span attribute, metric label or DLQ record.** DLQ records carry the raw envelope, which by contract contains no secrets; the sanitizer runs over the record before it is written.
- **The signing key's private half** exists only in the compiler Job's mounted secret; the data-plane pods hold only `BUNDLE_SIGNING_PUBLIC_KEY`.
- **The executor holds no stage secret at all** — only its own mTLS client certificate and a read-only bucket credential. Secret resolution happens exclusively on the stage side of the host API.

### 11.6 Transport security

**Default on, everywhere. The opt-out is a normal chart value in every environment, and it is loud.**

#### 11.6.1 Valkey

| Property | Requirement |
|---|---|
| Scheme | `rediss://` (the wire protocol's own scheme name) with TLS 1.2 minimum, 1.3 preferred |
| Certificate verification | Full chain verification against `VALKEY_CA_FILE`; hostname verified |
| Authentication | One ACL user per service — `svc-ingest`, `svc-process`, `svc-action`, `svc-streaming`, `hub-api` — each limited to the commands it uses and to key patterns it owns |
| Password | From a Kubernetes Secret via `VALKEY_PASSWORD` / `VALKEY_PASSWORD_FILE`; never a CLI flag, never in the URL that gets logged |
| Executor | **No Valkey access at all** — no URL, no credential, and the NetworkPolicy denies the route |
| Startup | With `security.transport.tls` true, a `redis://` URL is refused at startup with a named error; with `security.transport.auth` true, a URL without a username or password is refused |

ACL sketch (rendered by the chart into the Valkey ACL file):

```
user svc-ingest   on >$(PASS_INGEST)   ~waddles:t:*  ~waddles:proc:idx:*  ~waddles:consumers:*  ~waddles:deliv:*  ~waddles:dlq:*  ~waddles:lease:*  ~waddles:intake:*  +@read +@write +@list +@set +@sortedset +@hash +@string +eval +evalsha -@admin -@dangerous
user svc-process  on >$(PASS_PROCESS)  (same patterns)  (same commands)
user svc-action   on >$(PASS_ACTION)   (same patterns)  (same commands)
user svc-streaming on >$(PASS_STREAM)  ~waddles:streaming:*  +@read +@write +@string +@hash -@admin -@dangerous
```

#### 11.6.2 Postgres

| Property | Requirement |
|---|---|
| TLS | `sslmode=verify-full` against a chart-provisioned CA (`DB_SSLROOTCERT`) |
| Roles | One role per service (`svc_ingest` has none — ingest has no DB; `svc_process`, `svc_action`, `svc_streaming`, `hub_api`), plus one role per bundle (`bundle_<app_id_underscored>`) granted only its `data.tables` |
| Auth | Password from a Secret, or client-certificate auth where the chart provisions per-service certificates |
| Startup | With `security.transport.tls` true, `sslmode` below `verify-full` is refused; with `security.transport.auth` true, a DSN with no password and no client certificate is refused |

Per-bundle roles are created and their grants updated by hub-api at version approval time, over a privileged migration connection; the role is dropped when the last activation of the `app_id` is removed and a `BUNDLE_ROLE_GRACE_H = 168` grace has elapsed.

#### 11.6.3 CA and certificate provisioning

The chart provisions everything needed for the defaults to work out of the box:

- cert-manager `Certificate` resources when cert-manager is present in the cluster (`security.transport.certManager: true`, auto-detected and overridable);
- otherwise a chart-managed CA: a self-signed CA Secret created on first install and reused on upgrade, issuing server certificates for Valkey and Postgres and client certificates where used;
- the CA bundle is mounted into every service, including hub-api, which receives the same URLs and CA path as the Rust services.

#### 11.6.4 The opt-out (D20)

```yaml
security:
  transport:
    tls: true      # default
    auth: true     # default
```

Both are ordinary values, settable in `alpha.yml`, `beta.yml`, `gamma.yml` and `production.yml` alike. **No environment's values file rejects a `false`.** When either is `false`, every affected service, on **every** startup:

1. Logs at WARN, as its first log line after telemetry init:
   `TRANSPORT SECURITY DISABLED — security.transport.tls=false: Valkey and Postgres traffic is unencrypted and readable on the network. This is an explicit, visible opt-out.` (and the matching `auth=false` wording for authentication).
2. Sets the gauge `waddles_insecure_transport{component="valkey"|"postgres",aspect="tls"|"auth"}` to `1`. The gauge is `0` for every component/aspect that is secured, so "no series" and "secure" are distinguishable.
3. Reports it on `/health`:

```json
{
  "status": "ok",
  "service": "svc-process",
  "version": "3.0.0",
  "transport": "insecure",
  "transport_detail": {"valkey": {"tls": false, "auth": true},
                       "postgres": {"tls": true, "auth": true}},
  "sandbox": "runc",
  "dependencies": {"postgres": "ok", "valkey": "ok", "bucket": "ok",
                   "hub_api": "ok", "otlp": "tcp"},
  "executor": {"state": "running", "connections": 4, "bundles_loaded": 7},
  "spine": {"stage": "process", "consumer_id": "svc-process-7d9c4f"}
}
```

`transport` is `"secure"` only when every component/aspect is on; otherwise `"insecure"`. The README-facing wording is fixed: **enabled by default; the opt-out is explicit and visible.**

### 11.7 Digest and signature verification

Three values must agree before a component is loaded: the digest hub-api recorded on the `app_catalog` version row (served as `artifactDigest`), the `digest` field of the signed bucket sidecar, and the SHA-256 the pod computes over the fetched component bytes. The sidecar's signature is verified with `BUNDLE_SIGNING_PUBLIC_KEY` before its `digest` field is trusted. Any disagreement:

- refuses the load (the pod keeps serving the previously verified digest),
- increments `waddles_bundle_digest_mismatch_total{app_id}`,
- logs at ERROR with all three values in structured fields,
- never falls back to "load it anyway".

### 11.8 Tenant isolation and PII

- Every envelope's tenant and community come from the Valkey key it was taken from, never from its payload. The `_target_app_id` escape hatch changes only the `app_id` segment of the destination key.
- Bundle `db` access is RLS-scoped to the envelope's tenant and community; a statement touching a table outside `data.tables` is refused before it reaches Postgres.
- PII tokenization is unchanged: the single `users` identity table is never in any bundle's `data.tables` (rule V23 makes it a reserved name), and bundle-owned tables reference platform-opaque identifiers, exactly as `docs/APP_BUNDLE_AUTHORING.md` §5's worked example already requires.
- Intake routes never accept a tenant from the body: the webhook route takes it from the path and validates it against the source record; the REST route takes it from the JWT claim.

### 11.9 Service identity

Every service reserves and accepts a SPIFFE identity under `spiffe://penguintech.io/<env>/<service>` — `svc-ingest`, `svc-process`, `svc-action`, `svc-streaming`, `bundle-compiler` — and is built SPIFFE-ready (accepts an mTLS X.509-SVID as a first-class identity) whether or not SPIRE is deployed in a given environment. Where SPIRE is not live, inter-service calls use short-lived (≤ 1 h) signed OIDC machine JWTs, including the stage→hub-api distribution poll, which continues to use the existing `distribution:read`-scoped service JWT.

---

### 11.10 Least User Access via RBAC

**The governing principle for every credential in this design: each Postgres role and each Valkey ACL user gets exactly the privileges its job needs — no more, and no "it was easier to grant `ALL`".** The pattern recurs throughout this spec — per-bundle Postgres roles limited to `data.tables` (§11.6.2), exactly two writers on `app_versions` (§6.10), a publisher role that touches nothing else (§4.6), an executor with no database credential at all (§11.3), per-service Valkey ACL users (§11.6.1) — and this section makes it one enforceable rule rather than a habit repeated in eight places.

Two artifacts are **normative**, versioned in the repository, and are the source the grants are generated from. They are not documentation of what was configured; they are the configuration.

#### 11.10.1 The Postgres RBAC matrix

`config/postgres/rbac-matrix.yaml` — a role × table × privilege matrix covering every role in the system:

| Role | Scope |
|---|---|
| `hub_api` | Control-plane tables: registries, approvals, grants, intake sources, `app_versions`, `app_active_versions`, `workstreams`; `workstream_usage_hourly` limited to `INSERT`/`SELECT` (append-only, D31) — no `UPDATE`/`DELETE` for anyone, including `hub_api` |
| `waddles_publisher` | `app_versions` only (§6.10) |
| `svc_ingest` | No database — the row exists and is empty, so "svc-ingest has no DB" is asserted rather than assumed |
| `svc_process` | Its built-ins' own tables; serves bundle `db` calls through the per-bundle roles, not its own |
| `svc_action` | `action_dispatch_log`, reference tables |
| `svc_streaming` | Its own media/recording tables |
| `webui` | Read-only where it reads at all |
| executor | **No role.** The matrix states this explicitly so a future change has to delete a line rather than quietly add one |
| migration runner | DDL, and only during migrations |
| per-bundle roles (`bundle_<app_id_underscored>`) | Exactly the bundle's approved `data.tables`, generated per approval (§9.7.3) |

Grants are generated from this file; nobody writes a `GRANT` by hand.

**CI test.** A test queries the live `information_schema.role_table_grants` and asserts it **equals** the matrix — set equality, both directions, so a missing grant and an extra grant both fail. It reports the number of roles and tables examined and fails if fewer than **8 roles** or **8 tables** were checked, because a query that matched nothing would otherwise report a clean pass (`critical-rules.md` Verification Integrity).

#### 11.10.2 The Valkey ACL matrix

`config/valkey/acl-matrix.yaml` — a user × commands/categories × key patterns × channels matrix, from which the mounted `users.acl` (§11.6.1) is rendered by the chart. One entry per service user, plus an explicit statement that **the executors have no Valkey user at all**.

Every stage user's grant on `waddles:usage` (§5.12, §6.2, D31) is `+xadd` only — no `xrange`, `xreadgroup` or `xrevrange` — so a stage can write its own usage deltas but never read anyone's, including its own. Only hub-api's ACL user may read the stream.

**CI test.** A test runs `ACL LIST` against the deployed Valkey and asserts it equals the rendered matrix, plus per-user negative tests that prove the scoping actually bites:

| Negative test | Expected |
|---|---|
| `svc-action` attempts `XADD` to an ingest source stream | `NOPERM` — it may write only its own action-related keys |
| `svc-ingest` attempts `XREADGROUP` on an action stream | `NOPERM` |
| `svc-process` attempts a key outside `waddles:t:*` | `NOPERM` |
| Any service attempts `@admin` or `@dangerous` commands (`FLUSHALL`, `CONFIG`, `ACL`) | `NOPERM` |
| An executor attempts to authenticate to Valkey | Fails — no user exists for it, and its NetworkPolicy denies the route anyway (§12.5) |
| Any stage attempts `XRANGE`/`XREADGROUP`/`XREVRANGE` on `waddles:usage` | `NOPERM` — stages are `XADD`-only producers; only hub-api's ACL user may read it (D31) |

Both matrices are referenced from Deployment (§12.3, where the chart renders them) and from Testing (§14.5, where the two equality tests run in every service's CI gate set).

---

## 12. Deployment

### 12.1 Images

One rootless image per service, all multi-stage, all digest-pinned bases, all non-root at `uid 10001`.

| Image | Build | Runtime contents |
|---|---|---|
| `ghcr.io/penguintechinc/waddles/svc-ingest` | `rust:1.97-slim-bookworm` builder → `debian:bookworm-slim` runtime | `svc-ingest` binary, CA bundle |
| `ghcr.io/penguintechinc/waddles/svc-process` | same | `svc-process` binary, CA bundle |
| `ghcr.io/penguintechinc/waddles/svc-action` | same | `svc-action` binary, CA bundle |
| `ghcr.io/penguintechinc/waddles/bundle-executor` | same | `bundle-executor` binary, CA bundle — its own image, deployed once per stage |
| `ghcr.io/penguintechinc/waddles/svc-streaming` | same (`Dockerfile`, renamed from `Dockerfile.rust`) | `svc-streaming` binary, `ffmpeg` |
| `ghcr.io/penguintechinc/waddles/bundle-compiler` | same builder, plus the Tier 1 toolchains | `bundle-compiler`, `componentize-py`, `cargo`+`wasm32-wasip2`, `componentize-js` |

**Vendored tool pinning.** `wasmtime` and the Tier 1 toolchains are fetched at exact versions with SHA-256 verification in the builder stage, never from a distribution's rolling package. The versions live in one place, `build/tool-versions.env`, and are asserted by a container structure test (`bundle-executor --print-wasmtime-version`). No image contains `bwrap`: sandboxing is the node runtime's job now, not a binary we ship.

**Health checks** are binary sub-commands, never `curl`: `svc-process --healthcheck` exits `0` when `/health` would return `200`; `bundle-executor --healthcheck` exits `0` when it holds at least one live stage connection. Matches `core/svc_streaming/Dockerfile.rust`.

### 12.2 Sandbox runtime requirement

gVisor is an **optional, additive** sandbox layer for the executor and compiler pods — default **off** everywhere (D32, §18 R3). Every other layer (§11.2) is mandatory and unconditional regardless of this setting: rootless, all capabilities dropped, read-only rootfs, `RuntimeDefault` seccomp, `automountServiceAccountToken: false`, and the default-deny `CiliumNetworkPolicy` with two allowed destinations.

```yaml
sandbox:
  gvisor:
    enabled: false             # default (D32); mirrored by WADDLES_SANDBOX_GVISOR
  runtimeClassName: runsc      # gvisor on GKE Sandbox; only rendered when enabled
```

`sandbox.gvisor.enabled` is mirrored into the executor and compiler containers as `WADDLES_SANDBOX_GVISOR` (`true`/`false`, default `false`), which both binaries read at startup, before accepting or issuing a single frame.

**`WADDLES_SANDBOX_GVISOR=false` (default).** The chart omits `runtimeClassName`, so the pods run under the cluster's default runtime. Every other layer stays on: own pod, rootless at `uid 10001`, `allowPrivilegeEscalation: false`, all capabilities dropped, read-only rootfs, `RuntimeDefault` seccomp, `automountServiceAccountToken: false`, the same default-deny `CiliumNetworkPolicy` with two egress destinations and no ingress, and the WASM sandbox itself untouched. The binary:

- sets `waddles_sandbox_gvisor{enabled="false"}` to `1` (and `{enabled="true"}` to `0`, so "off" and "not reporting" are distinguishable);
- reports `sandbox: "runc"` on `/health` and `sandbox: {runtime: "runc", verified: false}` in the `hello` frame;
- proceeds — this **is** the default posture, not a fallback, so no warning is logged.

**`WADDLES_SANDBOX_GVISOR=true` (opt-in, where the cluster can sustain it — §12.2.1).** The pod carries `runtimeClassName`, and the binary verifies it really is inside gVisor:

- read `/proc/version`, which under gVisor reports a gVisor kernel string rather than the node's kernel;
- corroborate with the gVisor-specific markers in `/proc/self/status` (gVisor omits several fields a Linux kernel always emits; the sentry's set is stable per pinned `runsc` version);
- both agree → proceed, set `waddles_sandbox_gvisor{enabled="true"}` to `1`, report `sandbox: "gvisor"` on `/health` and `sandbox: {runtime: "gvisor", verified: true}` in the `hello` frame;
- either check fails → log at ERROR naming the expected `RuntimeClass` and what `/proc/version` actually reported, set `waddles_sandbox_gvisor{enabled="true"}` to `0`, report `sandbox: "runc"` — **and proceed, not crash-loop.** §18 R3 found gVisor's presence non-durable on at least one distribution we run (DOKS's own reconciler can remove a node's handler after this pod already started); failing closed on that absence would turn a platform limitation into an unplanned outage. Fail-closed-on-absence is exactly what D32 removes — this is the one behaviour change from the prior design.

**The trade-off, stated once for the README and `values.yaml`:** gVisor is a defence-in-depth layer, not the design's security boundary. Running without it (the default) means a native-code escape from wasmtime faces the host kernel's syscall surface, filtered by `RuntimeDefault` seccomp with all capabilities dropped, instead of a user-space one; running with it, where a cluster can sustain it, adds that interception at the cost of per-call latency — a figure now void to benchmark (§18, R8: no environment we run retains gVisor to measure against).

**No silent change of posture is possible:** whichever way the value is set, it is visible in a gauge, a `/health` field and every `hello` frame; the opt-in path additionally logs its startup verification result.

#### 12.2.1 Distribution support matrix

For operators who want to opt into gVisor where their cluster can sustain it. **None of these paths are required — `sandbox.gvisor.enabled` defaults `false` everywhere (D32).**

| Distribution | How gVisor is obtained | Notes |
|---|---|---|
| **MicroK8s** (alpha) | **No addon exists.** Install `runsc` + `containerd-shim-runsc-v1` on the node and register the handler in the containerd **template** (`microk8s-resources/default-args/containerd-template.toml`), then create the `RuntimeClass` — or enable `sandbox.installer` below. | **Corrected 2026-09-21:** this row previously read `microk8s enable gvisor`, which does not exist — `canonical/microk8s-community-addons` has no `gvisor` addon and never has (verified against the repo's `addons/` tree and `addons.yaml`; its sandboxed-runtime addon is `kata`). The command belongs to **minikube**, see that row. MicroK8s regenerates `containerd.toml` on restart, so the template — not the generated file — is what must be edited, exactly as for k3s. |
| **k3s** | Install `runsc` + `containerd-shim-runsc-v1` on each node, add a containerd config **template** (`/var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl`) registering the handler, restart k3s, then create the `RuntimeClass`. | k3s regenerates `config.toml` on restart, so the template — not the generated file — is what must be edited. |
| **minikube** | `minikube start --container-runtime=containerd` then `minikube addons enable gvisor` | Requires the containerd runtime; the Docker runtime is unsupported. |
| **kubeadm / upstream CNCF** | Install `runsc` on nodes, register the containerd handler, create the `RuntimeClass` — or enable `sandbox.installer` below. | The general case the installer exists for. |
| **GKE** | GKE Sandbox (node pool with `--sandbox type=gvisor`); `RuntimeClass` is `gvisor`. | Set `sandbox.runtimeClassName: gvisor`. Native support, no installer — the one distribution where opting in is straightforward. |
| **EKS / AKS** | No native offering; use `sandbox.installer.enabled: true`, or a pre-baked node image. | — |
| **DOKS** (gamma, production) | **Not available, and not durably opt-in-able.** No native offering, and the installer DaemonSet does **not** work: DigitalOcean manages worker-node container-daemon configuration and its reconciler reverts node-level changes ("your changes are overwritten by the reconciler and do not persist" — official DOKS docs). No custom/BYO node image path exists in the docs, `doctl`, Terraform or Pulumi. | **Verified 2026-09-21** (§18 R3). gamma and production run `sandbox.gvisor.enabled: false` — the default — unless DigitalOcean ships native sandbox-runtime support; an open feature request has no official response. This is a platform constraint, not a schedule slip (D32). |
| **Docker Desktop Kubernetes** | Unsupported — no `runsc` handler is installable. | `sandbox.gvisor.enabled: false` (the default); every other sandbox layer stays on. |

#### 12.2.2 Optional installer DaemonSet

`sandbox.installer.enabled` (default `false`) deploys a node installer for clusters with no native offering, for operators opting into gVisor where it can actually persist (not DOKS — §12.2.1):

1. Downloads a **pinned** `runsc` and `containerd-shim-runsc-v1` (exact version plus SHA-256, from the official gVisor release bucket) and verifies both checksums before installing.
2. Installs them into `/usr/local/bin` on the node.
3. Patches the node's containerd configuration to register the `runsc` handler, idempotently (a re-run over an already-patched node changes nothing).
4. Restarts containerd.
5. Labels the node `waddles.io/gvisor=ready`.

When the installer is enabled, the executor and compiler workloads carry `nodeSelector: {waddles.io/gvisor: "ready"}`, so they schedule only where the handler actually exists.

```yaml
# ROOT EXCEPTION (approved) — sandbox installer only.
# Installing a container-runtime handler requires writing to the node
# filesystem and restarting containerd, which is not achievable rootless.
# Scope: this DaemonSet alone, default DISABLED (sandbox.installer.enabled:
# false). No other Waddles workload runs privileged or as root; the
# executor it installs support for is itself rootless with all capabilities
# dropped. Approved by the human product owner, 2026-09-14.
sandbox:
  installer:
    enabled: false
    runscVersion: ""      # gVisor release id, from build/tool-versions.env
    runscSha256: ""       # SHA-256 of the runsc binary
    shimSha256: ""        # SHA-256 of containerd-shim-runsc-v1
    nodeLabel: "waddles.io/gvisor=ready"
```

The version and both digests are set from `build/tool-versions.env` at chart-render time, alongside the wasmtime pin. **An empty or mismatching digest is a hard failure**: the installer exits non-zero and the node is never labelled, rather than fetching whatever is current. A `helm template` test asserts that enabling the installer with empty pins fails rendering.

### 12.3 Chart values

Existing keys keep their names and defaults. New keys:

| Key | Default | Meaning |
|---|---|---|
| `pipeline.svcIngest.image` / `.svcProcess.image` / `.svcAction.image` / `.svcStreaming.image` | `ghcr.io/penguintechinc/waddles/<service>:<tag>` | Replaces the `pipeline.pythonBaseImage` placeholder the three stage templates use today. |
| `pipeline.executor.callTimeoutMs` | `2000` | `EXECUTOR_CALL_TIMEOUT_MS` |
| `pipeline.executor.memoryLimitMb` | `64` | `EXECUTOR_MEMORY_LIMIT_MB` |
| `pipeline.executor.maxCallTimeoutMs` | `10000` | Hard ceiling a manifest may request |
| `pipeline.executor.maxMemoryLimitMb` | `256` | Hard ceiling a manifest may request |
| `pipeline.executor.instancesPerBundle` | `4` | Pool size per loaded bundle |
| `pipeline.executor.maxConcurrentCalls` | `32` | Global ceiling per pod |
| `pipeline.executor.tripThreshold` | `3` | Trips before disable |
| `pipeline.executor.tripWindowSeconds` | `300` | Trip window |
| `pipeline.executor.image` | `ghcr.io/penguintechinc/waddles/bundle-executor:<tag>` | Executor Deployment image |
| `pipeline.executor.replicas` | `2` | Replicas of each executor Deployment |
| `pipeline.executor.stageConnections` | `4` | `EXECUTOR_STAGE_CONNECTIONS` |
| `pipeline.executor.hostApiPort.process` / `.action` | `8301` / `8302` | The stages' mTLS host-API listeners |
| `pipeline.executor.resources` | requests `{cpu: "250m", memory: "256Mi"}`, limits `{cpu: "1000m", memory: "512Mi"}` | Executor Deployment resources |
| `sandbox.runtimeClassName` | `runsc` | `RuntimeClass` for the executor Deployments and the compiler Job when gVisor is opted in (`gvisor` on GKE); rendered only when `sandbox.gvisor.enabled: true` |
| `sandbox.gvisor.enabled` | `false` | Optional opt-in, default off everywhere (D32); sets `WADDLES_SANDBOX_GVISOR` and controls whether `runtimeClassName` is rendered at all; §12.2 |
| `sandbox.installer.enabled` | `false` | Optional node installer DaemonSet; §12.2.2 |
| `sandbox.installer.runscVersion` / `.runscSha256` / `.shimSha256` | empty | Pinned from `build/tool-versions.env`; empty fails rendering when the installer is enabled |
| `sandbox.installer.nodeLabel` | `waddles.io/gvisor=ready` | Applied by the installer; used as the executor/compiler `nodeSelector` when the installer is enabled |
| `pipeline.spine.streamMaxLen` | `100000` | `MAXLEN ~` bound on every event stream |
| `pipeline.spine.readCount` / `.blockMs` | `64` / `1000` | `XREADGROUP COUNT` / `BLOCK` |
| `pipeline.spine.claimIdleMs` / `.claimIntervalMs` | `30000` / `15000` | `XAUTOCLAIM` threshold and cadence |
| `pipeline.spine.pelAlert` | `5000` | PEL size that raises an operator alert for one group |
| `pipeline.spine.dlqMaxLen` | `10000` | DLQ bound |
| `pipeline.spine.maxDeliveries` | `5` | Redelivery cap |
| `bundles.allowPrebuilt` | `true` | Seeds hub-api's global-admin setting `bundles.allow_prebuilt`; the DB setting is authoritative at runtime |
| `bundles.allowWildcardConsumes` | `false` | Seeds the per-tenant setting `allow_wildcard_consumes` (rule V30); the DB setting is authoritative at runtime |
| `bundles.bucket.provider` | `minio` | `minio` or `nest` |
| `bundles.bucket.endpoint` | `http://minio.waddles.svc.cluster.local:9000` | S3 endpoint |
| `bundles.bucket.name` | `waddles-bundles` | Bucket name |
| `bundles.bucket.region` | `us-east-1` | Region |
| `bundles.bucket.existingSecret` | `waddles-bundle-bucket` | Holds `accessKeyId`, `secretAccessKey` |
| `bundles.pollIntervalSeconds` | `60` | Bucket poll cadence |
| `bundles.egress.allowPrivateHosts` | `false` | Per-tenant override; lifts only the private-range portion of the bundle SSRF check for hosts already on a manifest's `egress` allowlist (§8.5). Never affects the stage's own infrastructure connections, which are exempt regardless |
| `bundles.signingPublicKeySecret` | `waddles-bundle-signing` | Holds `publicKey` (pods) and `privateKey` (compiler Job only) |
| `bundles.compiler.image` | `ghcr.io/penguintechinc/waddles/bundle-compiler:<tag>` | Job image |
| `bundles.compiler.activeDeadlineSeconds` | `900` | Job deadline |
| `bundles.compiler.resources.limits` | `{cpu: "2000m", memory: "4Gi"}` | Compilation is memory-hungry |
| `ingest.trustedProxies` | `[]` | `WADDLES_INGEST_TRUSTED_PROXIES` — CIDR list; `X-Forwarded-For` honoured only from a trusted direct peer (§4.1.1) |
| `ingest.platforms.twitch.originSuffixes` | `["twitch.tv"]` | FCrDNS domain suffixes required for the Twitch EventSub webhook's client address (§4.1.1) |
| `ingest.platforms.twitch.originCidrs` | `[]` | Optional static CIDR allowlist, alternative to FCrDNS, for the Twitch webhook |
| `ingest.platforms.kick.originSuffixes` | `["kick.com"]` | FCrDNS domain suffixes required for the Kick webhook's client address (§4.1.1) |
| `ingest.platforms.kick.originCidrs` | `[]` | Optional static CIDR allowlist, alternative to FCrDNS, for the Kick webhook |
| `security.transport.tls` | `true` | §11.6.4 |
| `security.transport.auth` | `true` | §11.6.4 |
| `security.transport.certManager` | auto-detected | Use cert-manager when present, chart-managed CA otherwise |
| `security.rbac.postgresMatrix` | `config/postgres/rbac-matrix.yaml` | The normative role × table × privilege matrix the grants are generated from (§11.10.1) |
| `security.rbac.valkeyMatrix` | `config/valkey/acl-matrix.yaml` | The normative ACL matrix the mounted `users.acl` is rendered from (§11.10.2) |
| `security.envelopeBinding.keySecretRef` | `waddles-envelope-binding` | Secret holding the `kid`-keyed HMAC keys for `binding.mac` (§5.11, D30); mounted only in the four Rust stage services, never in hub-api, the compiler, or any bundle-facing workload |
| `security.envelopeBinding.rotationOverlapSeconds` | `86400` | How long a retired `kid` is still accepted for verification after a new one becomes current (§5.11) |
| `metering.enabled` | `true` | Turns on usage recording and the hub-api aggregator (§5.12, D31); no charging or enforcement is wired to it |
| `metering.flushIntervalSeconds` | `10` | Per-stage-replica batching interval for `waddles:usage` writes (§5.12) |

**Unchanged:** `pipeline.svcIngest.port` = 8200, `.svcProcess.port` = 8201, `.svcAction.port` = 8202, replicas 2/2/2, and the existing per-service `resources` blocks (requests `500m`/`512Mi`, limits `2000m`/`2Gi`). The `pipeline.pythonBaseImage` value is removed once no template references it.

### 12.4 Pod shape

**Stage Deployment** (`svc-process`, `svc-action`) — cluster-default `RuntimeClass`:

```yaml
securityContext:                 # pod
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile: {type: RuntimeDefault}
containers:
  - name: svc-process
    ports:
      - {name: http,    containerPort: 8201}
      - {name: metrics, containerPort: 9090}
      - {name: host-api, containerPort: 8301}   # executor-facing, mTLS
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: {drop: ["ALL"]}
    volumeMounts:
      - {name: ca,       mountPath: /etc/waddles/ca, readOnly: true}
      - {name: host-api-tls, mountPath: /etc/waddles/host-api, readOnly: true}
```

**Executor Deployment** (`svc-process-executor`, `svc-action-executor`):

```yaml
# runtimeClassName: runsc         # {{ .Values.sandbox.runtimeClassName }} — only rendered when sandbox.gvisor.enabled: true (default false, D32)
# nodeSelector applied only when sandbox.installer.enabled:
# nodeSelector: {waddles.io/gvisor: "ready"}
automountServiceAccountToken: false
securityContext:                 # pod
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile: {type: RuntimeDefault}
containers:
  - name: bundle-executor
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: {drop: ["ALL"]}
    env:                         # nothing from the stage's Secrets
      - {name: STAGE_HOST_API_ADDR, value: "svc-process:8301"}
    volumeMounts:
      - {name: scratch,    mountPath: /scratch}                   # emptyDir, 16Mi
      - {name: wasm-cache, mountPath: /var/cache/waddles/wasm}    # emptyDir
      - {name: client-tls, mountPath: /etc/waddles/host-api, readOnly: true}
      - {name: bucket,     mountPath: /etc/waddles/bucket, readOnly: true}  # read-only creds
```

`automountServiceAccountToken: false` is deliberate: the executor has no reason to talk to the API server, and a mounted token would be the one credential an escape could still find.

### 12.5 Network policy

`CiliumNetworkPolicy`, default deny in the `waddles` namespace, with explicit allows:

| Workload | Ingress | Egress |
|---|---|---|
| svc-ingest | Ingress/Gateway → `:8200`; Prometheus → `:9090` | Valkey, hub-api, the five platform APIs (by FQDN), DNS |
| svc-process | Prometheus → `:9090`; **`svc-process-executor` → `:8301` only** | Valkey, Postgres, hub-api, DNS, plus the union of activated bundles' `egress` hosts |
| svc-action | Prometheus → `:9090`; **`svc-action-executor` → `:8302` only** | Valkey, Postgres, hub-api, platform APIs, DNS, plus activated bundles' `egress` hosts |
| **svc-process-executor** | **none** | `svc-process:8301`, the bucket, DNS — nothing else |
| **svc-action-executor** | **none** | `svc-action:8302`, the bucket, DNS — nothing else |
| svc-streaming | Ingress → `:8208`, RTMP `:1935`, SRT `:9000`, WHIP/WHEP UDP range | Postgres, Valkey, the bucket, DNS |
| **bundle-compiler `build` container** | none | **nothing at all** — no DNS, no bucket, no cluster. It runs bundle code and has no reason to reach anything (§4.6) |
| **bundle-compiler `publisher` container** | none | the bucket, Postgres, hub-api's notification endpoint, DNS — nothing else |

The two executor rows are the load-bearing ones: with no ingress and two egress destinations, an escaped executor has no route to Valkey, Postgres, the platform APIs, the API server, or the internet. `waddles_egress_denied_total` counts what the stage refuses; the NetworkPolicy is what stops anything that never reaches the stage at all.

**Infrastructure endpoints are allowed whatever their address range.** The stage rows' egress rules are generated from the operator's configured endpoints — `DB_HOST`/`DATABASE_URL`, `VALKEY_URL`, `BUNDLE_BUCKET_ENDPOINT`, `HUB_API_URL`, `OTEL_EXPORTER_OTLP_ENDPOINT` — and allow them whether they resolve to a cluster Service IP, an RFC 1918 VPC address, or a public host. The chart renders `toCIDR`/`toFQDN`/`toEndpoints` rules accordingly; there is no private-range exclusion on a stage's infrastructure egress, because that is where these services normally live (§8.5). Only the **bundle** `egress` hosts folded into the same rows are subject to the private-range default, and lifting it is `bundles.egress.allowPrivateHosts`. The executor rows are unaffected either way: default-deny plus the stage's host-API port plus the bucket, and nothing else.

No `NodePort`, `HostPort`, `externalIPs` or `hostNetwork` in beta or production; alpha keeps its existing NodePort exemption. External ingress reaches only each pod's declared serving port.

---

### 12.6 Startup connectivity self-check

Before a stage drains anything, and before the compiler Job starts phase 1, each service probes every infrastructure endpoint it is configured with — Postgres, Valkey, the artifact bucket, hub-api, and (when an endpoint is set) the OTLP collector. These are the private-network connections of §8.5, exempt from the bundle egress guard, and a failure to reach one is a configuration problem the operator must see immediately.

Each probe reports a **classified** result, never a bare "connection failed":

| Class | Meaning | Example message |
|---|---|---|
| `dns` | The hostname did not resolve | `valkey: DNS resolution failed for 'valkey.waddles.svc.cluster.local' — check the Service name and the pod's DNS policy` |
| `tcp` | Resolved, but the connection was refused, timed out, or was blocked | `postgres: TCP connect to 10.42.0.17:5432 timed out after 5s — check the NetworkPolicy egress rule and that the endpoint is listening` |
| `tls` | Connected, but the handshake or certificate verification failed | `postgres: TLS verification failed for 10.42.0.17:5432 — certificate is not signed by DB_SSLROOTCERT (/etc/waddles/ca/postgres-ca.crt)` |
| `auth` | TLS succeeded, credentials were rejected | `valkey: authentication rejected for ACL user 'svc-process' — check the password Secret` |
| `ok` | Reachable and authenticated | — |

Behaviour:

- **Never a silent retry loop.** Each probe runs with `STARTUP_PROBE_TIMEOUT_MS` (default `5000`) and at most `STARTUP_PROBE_ATTEMPTS` (default `3`) attempts at 1 s intervals; the classified result is logged at ERROR on failure, every time, with the endpoint named.
- A required endpoint still failing after the attempts → **exit 78** (`EX_CONFIG`), so the pod crash-loops with the reason in its logs instead of appearing healthy and processing nothing.
- The OTLP collector is the one non-required probe: an unreachable collector logs at WARN and the service starts (telemetry failure is never a request failure), consistent with §13.
- Results are exposed on `/health` under `dependencies`, each with its class, so a failing probe is visible to an operator without reading logs.
- `waddles_dependency_up{dependency}` is `1`/`0` per endpoint, and `waddles_dependency_check_total{dependency,class}` counts each classified outcome.

### 12.7 Environment variables (consolidated)

Defaults are the values a service uses when the variable is unset. Every secret-bearing variable is read from the environment or a file, never from a CLI flag.

**Common to all four services**

| Var | Default | Notes |
|---|---|---|
| `MODULE_NAME` | the service name | Identity in logs |
| `MODULE_PORT` | 8200 / 8201 / 8202 / 8208 | HTTP listener; read at runtime, never hardcoded in the Dockerfile |
| `METRICS_PORT` | `9090` | Prometheus listener |
| `BIND_ADDR` | `0.0.0.0` | |
| `LOG_LEVEL` | `info` | `error`\|`warn`\|`info`\|`debug` |
| `RUNNER_TENANT_SLUG` | `global` | Fixed tenant slug per deployment |
| `HUB_API_URL` | `http://hub-api.waddles.svc.cluster.local:8204` | Distribution API base |
| `SECRET_KEY` | *(required)* | Mints the `distribution:read` service JWT |
| `POLL_INTERVAL_S` | `5.0` | Bundle-set refresh |
| `BASE_BACKOFF_S` / `MAX_BACKOFF_S` | `1.0` / `60.0` | Distribution-poll backoff |
| `VALKEY_URL` | *(required)* | `rediss://…` when TLS is on; falls back to `REDIS_URL` for compatibility |
| `VALKEY_USERNAME` | the service's ACL user | |
| `VALKEY_PASSWORD` / `VALKEY_PASSWORD_FILE` | *(required when auth is on)* | Env or file only |
| `VALKEY_CA_FILE` | `/etc/waddles/ca/valkey-ca.crt` | |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` | `postgres` / `5432` / `waddlebot` **(legacy identifier — the database name is not renamed by this project)** / the service role | svc-ingest has no DB |
| `DB_PASSWORD` | *(required when auth is on)* | Env only |
| `DB_SSLMODE` | `verify-full` | |
| `DB_SSLROOTCERT` | `/etc/waddles/ca/postgres-ca.crt` | |
| `SECURITY_TRANSPORT_TLS` | `true` | From `security.transport.tls` |
| `SECURITY_TRANSPORT_AUTH` | `true` | From `security.transport.auth` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `_PROTOCOL` / `_HEADERS` | unset / `grpc` / unset | Unset endpoint ⇒ tracing only |
| `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` | the service name / `deployment.environment=<env>` | Never PII |
| `LICENSE_KEY` / `LICENSE_SERVER_URL` / `POSTHOG_HOST` / `POSTHOG_KEY` | unset / `https://license.penguintech.io` / unset / unset | `penguin-licensing` |

**Spine** (all four services)

| Var | Default |
|---|---|
| `SPINE_CONSUMER_ID` | pod name, else `{hostname}-{uuid-v4}` |
| `SPINE_STREAM_MAXLEN` | `100000` (approximate, `MAXLEN ~`) |
| `SPINE_READ_COUNT` | `64` |
| `SPINE_BLOCK_MS` | `1000` |
| `SPINE_CLAIM_IDLE_MS` | `30000` |
| `SPINE_CLAIM_INTERVAL_MS` | `15000` |
| `SPINE_STATS_INTERVAL_MS` | `10000` |
| `SPINE_PEL_ALERT` | `5000` |
| `SPINE_DLQ_MAXLEN` | `10000` |
| `SPINE_MAX_DELIVERIES` | `5` |
| `SOCKET_LEASE_TTL_MS` / `SOCKET_LEASE_RENEW_MS` | `30000` / `10000` |
| `WADDLES_BINDING_KEY_FILE` | `/etc/waddles/envelope-binding/keys.json` — `kid → key` map, from `security.envelopeBinding.keySecretRef` (§5.11, D30) |
| `WADDLES_BINDING_KID` | *(required)* — the active `kid` this replica mints new `binding.mac` values under |
| `WADDLES_BINDING_ROTATION_OVERLAP_S` | `86400`, from `security.envelopeBinding.rotationOverlapSeconds` |
| `METERING_ENABLED` | `true`, from `metering.enabled` (§5.12, D31) |
| `METERING_FLUSH_INTERVAL_S` | `10`, from `metering.flushIntervalSeconds` |

**Executor and bundles** (svc-process, svc-action)

| Var | Default |
|---|---|
| `HOST_API_PORT` | `8301` (svc-process) / `8302` (svc-action) |
| `HOST_API_TLS_CERT_FILE` / `_KEY_FILE` / `_CA_FILE` | `/etc/waddles/host-api/{tls.crt,tls.key,ca.crt}` |
| `HOST_API_PEER_IDENTITY` | `spiffe://penguintech.io/<env>/svc-process-executor`, else the pinned certificate CN |
| `STAGE_HOST_API_ADDR` *(executor)* | `svc-process:8301` / `svc-action:8302` |
| `EXECUTOR_STAGE_CONNECTIONS` *(executor)* | `4` |
| `SANDBOX_RUNTIME_EXPECTED` | `runc` (default) — `gvisor` only when `sandbox.gvisor.enabled: true` |
| `WADDLES_SANDBOX_GVISOR` | `false` (default, D32) |
| `EXECUTOR_WEDGE_THRESHOLD` | `3` |
| `EXECUTOR_CALL_TIMEOUT_MS` | `2000` |
| `EXECUTOR_MAX_CALL_TIMEOUT_MS` | `10000` |
| `EXECUTOR_MEMORY_LIMIT_MB` | `64` |
| `EXECUTOR_MAX_MEMORY_LIMIT_MB` | `256` |
| `EXECUTOR_INSTANCES_PER_BUNDLE` | `4` |
| `EXECUTOR_MAX_CONCURRENT_CALLS` | `32` |
| `EXECUTOR_POOL_WAIT_MS` | `500` |
| `EXECUTOR_MAX_FRAME_BYTES` | `1048576` |
| `EXECUTOR_TRIP_THRESHOLD` / `EXECUTOR_TRIP_WINDOW_S` | `3` / `300` |
| `EXECUTOR_DRAIN_MS` | `5000` |
| `EXECUTOR_UNAVAILABLE_READY_S` | `15` |
| `EXECUTOR_PRECOMPILE_DIR` | `/var/cache/waddles/wasm` |
| `EXECUTOR_WASM_COLLECTOR` | `drc` — must match between precompile and runtime engine (§7.2) |
| `KV_MAX_VALUE_BYTES` / `KV_MAX_TTL_S` | `65536` / `2592000` |
| `BUNDLE_BUCKET_ENDPOINT` / `_NAME` / `_REGION` | `http://minio.waddles.svc.cluster.local:9000` / `waddles-bundles` / `us-east-1` |
| `BUNDLE_BUCKET_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` | *(required)*, env only |
| `BUNDLE_POLL_INTERVAL_S` | `60` |
| `BUNDLE_FETCH_TIMEOUT_S` | `30` |
| `BUNDLE_MAX_COMPONENT_BYTES` | `33554432` |
| `BUNDLE_CACHE_VERSIONS` | `3` |
| `BUNDLE_SIGNING_PUBLIC_KEY` | *(required)*, Ed25519, base64 |
| `EGRESS_TIMEOUT_MS` | `5000` |
| `EGRESS_MAX_RESPONSE_BYTES` | `1048576` |
| `EGRESS_RATE_LIMIT_RPS` / `_BURST` | `10` / `20` |
| `EGRESS_MAX_REDIRECTS` | `3` |
| `EGRESS_DENYLIST_REFRESH_S` | `60` |
| `EGRESS_ALLOW_PRIVATE_HOSTS` | `false` — bundle calls only; infrastructure endpoints are exempt from the guard entirely (§8.5) |
| `STARTUP_PROBE_TIMEOUT_MS` / `STARTUP_PROBE_ATTEMPTS` | `5000` / `3` (§12.6) |

**svc-action only**

| Var | Default |
|---|---|
| `ACTION_MAX_RETRIES` | `3` |
| `ACTION_BASE_BACKOFF_MS` / `ACTION_MAX_BACKOFF_MS` | `250` / `8000` |

**svc-ingest only:** the table in §4.1 (now including `WADDLES_INGEST_TRUSTED_PROXIES`, §4.1.1), plus the platform credentials of §10.2, `INTAKE_DEDUPE_TTL_S` (`900`), and the rendered per-platform origin settings `INGEST_PLATFORM_TWITCH_ORIGIN_SUFFIXES` / `_CIDRS` and `INGEST_PLATFORM_KICK_ORIGIN_SUFFIXES` / `_CIDRS` (from `ingest.platforms.*`, §12.3, §4.1.1).

**bundle-compiler only**

| Var | Default |
|---|---|
| `BUNDLE_MAX_SOURCE_BYTES` | `16777216` |
| `BUNDLE_SIGNING_PRIVATE_KEY_FILE` | `/etc/waddles/signing/privateKey`, mounted only in the Job |
| `SKAUSWATCH_URL` | unset ⇒ the Skauswatch verdict step is reported as `not_configured`, never as a pass |
| `BUNDLE_ORPHAN_GRACE_H` | `168` |
| `BUNDLE_ROLE_GRACE_H` | `168` |

---

## 13. Observability

All four services emit OTel **logs, metrics and traces** through `penguin-logging`, configured only from the standard OTLP environment variables (`OTEL_EXPORTER_OTLP_ENDPOINT`, `_PROTOCOL`, `_HEADERS`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`). No vendor SDK, no hardcoded destination. Prometheus `/metrics` on `:9090` remains a secondary scrape surface, not a replacement. A dead exporter buffers, drops oldest and keeps serving; it never fails a request.

### 13.1 Metrics

Histograms first — load and latency are the signals most often missing.

| Name | Type | Labels | Meaning |
|---|---|---|---|
| `waddles_stage_latency_seconds` | histogram | `stage`, `app_id`, `result` | Take-to-enqueue (or take-to-dispatch) time for one envelope |
| `waddles_e2e_latency_seconds` | histogram | `platform`, `kind` (`text`\|`av`) | Platform event timestamp to outbound send; the SLA signal |
| `waddles_host_call_latency_seconds` | histogram | `stage`, `capability`, `op`, `result` | One host call, stage side |
| `waddles_executor_call_seconds` | histogram | `stage`, `app_id`, `export` | One `invoke`, measured by the stage |
| `waddles_spine_queue_depth` | histogram | `stage` | Observed length of a stage key, sampled once per drain pass |
| `waddles_bundle_load_seconds` | histogram | `app_id`, `phase` (`fetch`\|`verify`\|`precompile`\|`load`) | Hot-swap cost |
| `waddles_intake_request_seconds` | histogram | `route`, `status` | Intake handler latency |
| `waddles_egress_request_seconds` | histogram | `app_id`, `host`, `status` | Guarded egress call duration |
| `waddles_spine_dlq_total` | counter | `stage`, `reason` | DLQ writes; `reason` = the record's `error.kind` |


| `waddles_egress_denied_total` | counter | `app_id`, `reason` | One per §8.2 denial reason |
| `waddles_host_call_denied_total` | counter | `app_id`, `capability` | Ungranted or out-of-allowlist host call |
| `waddles_bundle_digest_mismatch_total` | counter | `app_id` | Refused load on digest/signature disagreement |
| `waddles_sandbox_trip_total` | counter | `app_id`, `limit` (`timeout`\|`memory`\|`trap`\|`denied`) | Sandbox trips |
| `waddles_bundle_skipped_total` | counter | `reason` | Distribution rows the stage could not use (e.g. `no_artifact`) |
| `waddles_intake_rejected_total` | counter | `source`, `reason` | Every intake rejection, reason from §10.1 |
| `waddles_stream_events_total` | counter | `platform`, `source_id` | Entries written by ingest, one per event (§10.6) |
| `waddles_stream_trimmed_total` | counter | `stream` | Entries evicted by `MAXLEN ~` trimming |
| `waddles_stream_claimed_total` | counter | `app_id` | Entries recovered by `XAUTOCLAIM` from a dead consumer |
| `waddles_consumer_skipped_total` | counter | `app_id`, `reason` | Entries read and acked without an executor call (`event_type`, `filter`) |
| `waddles_consumes_matched_total` | counter | `app_id` | Entries that matched and were handed to the executor |
| `waddles_group_lag` | gauge | `app_id`, `stream` | Undelivered entries for that group, from `XINFO GROUPS` |
| `waddles_group_pending` | gauge | `app_id`, `stream` | PEL size for that group — the stuck-bundle signal |
| `waddles_bundle_grants` | gauge | `app_id` | Streams the bundle is currently granted (`0` is legitimate) |
| `waddles_route_denied_total` | counter | `app_id`, `target` | `_target_app_id` redirects dropped for being outside the approved `routes_to` set |
| `waddles_executor_restarts_total` | counter | `stage`, `cause` | Executor restarts |
| `waddles_bundles_loaded` | gauge | `stage` | Components currently resident |
| `waddles_bundle_disabled` | gauge | `app_id` | `1` while disabled by trips |
| `waddles_bundle_stale_age_seconds` | gauge | `app_id` | Age of the newest verified digest versus the advertised one |
| `waddles_insecure_transport` | gauge | `component`, `aspect` | `1` when that component/aspect is running without TLS or auth |
| `waddles_sandbox_gvisor` | gauge | `stage`, `enabled` | `1` on the series matching the active posture, `0` on the other — so "gVisor off" and "not reporting" are distinguishable (§12.2) |
| `waddles_host_api_rejected_total` | counter | `stage`, `reason` | Host-API connections refused (bad certificate, wrong peer identity, `UNSANDBOXED_EXECUTOR`) |
| `waddles_host_api_connections` | gauge | `stage` | Live executor connections per stage |
| `waddles_dependency_up` | gauge | `dependency` | `1`/`0` per configured infrastructure endpoint (§12.6) |
| `waddles_dependency_check_total` | counter | `dependency`, `class` | Startup/periodic probe outcomes, `class` ∈ `ok`, `dns`, `tcp`, `tls`, `auth` |
| `waddles_socket_lease_held` | gauge | `provider`, `community` | `1` on the replica holding the single-owner lease |

### 13.2 Traces

`trace` (`traceparent`/`tracestate`) is added to `StageEnvelope` (§6.1.2, superseding the single-field `trace_context`, D30) precisely so a chat message's journey is one trace across three services. Every span in the table below additionally carries `waddles.tenant_id`, `waddles.community_id`, `waddles.workstream_id` and `waddles.app_id` (ids only, never PII or message bodies) — the trace-continuity test of §14.11 asserts one inbound event yields a single trace spanning ingest → process → the executor's host calls → action → the outbound call.

| Span | Parent | Attributes |
|---|---|---|
| `intake.request` | incoming `traceparent` if present, else root | `route`, `source`, `platform`, `tenant`, `http.status_code` |
| `ingest.normalize` | `intake.request` or the receiver's root span | `platform`, `event_type` |
| `spine.enqueue` / `spine.take` | the stage's current span | `stage`, `app_id`, `key` |
| `bundle.invoke` | `spine.take` | `app_id`, `digest` (12 hex), `export`, `duration_ms`, `result` |
| `host.http` / `host.db` / `host.kv` / `host.relay` | `bundle.invoke` | `capability`, `op`, `host` or `table`, `result` |
| `action.dispatch` | `spine.take` | `app_id`, `platform`, `attempt`, `retryable` |
| `bundle.load` | root (the poller's span) | `app_id`, `phase`, `digest` |

Context propagates on the envelope between stages and on outbound HTTP via W3C `traceparent`. The executor never creates spans itself: it returns durations, and the stage records them, so the guest cannot forge trace data.

### 13.3 Logs

- `penguin-logging` for every line; no `println!` in service code (the check scopes to service source, not CLI output paths).
- Levels are chosen per line: ERROR for actionable failure, WARN for degraded-but-serving (bucket unreachable, trip 1 and 2, insecure transport), INFO for lifecycle and state change (bundle loaded/swapped/disabled, lease acquired/lost, executor started/restarted), DEBUG for per-event decision points — generous by design and off by default.
- Sanitization applies at every level, DEBUG included.
- Structured fields carried on every pipeline log line: `stage`, `app_id`, `tenant`, `community`, `digest` (12 hex), `trace_id`.

### 13.4 Health endpoints

`/health` (rich, §11.6.4, including the `dependencies` block of §12.6), `/healthz` (bare `ok`, for the Kubernetes probes), `/metrics` on `:9090`. Readiness is false while: the distribution poll has never succeeded, the executor has been unavailable longer than `EXECUTOR_UNAVAILABLE_READY_S` (15 s), or a required dependency probe is not `ok`.

### 13.5 Feature flags

Every new capability sits behind a PostHog flag, defaulted OFF until validated, resolved through `penguin-licensing`'s two-gate check with last-known-cached fallback. Flag keys follow `{product}.{feature}` and the `FeatureContract` rule that a Feature's flag equals `waddles.{module}.{feature}`; all of these live in the always-on `core` module and are `min_tier: free` (core product, no entitlement gate):

| Flag key | Gates |
|---|---|
| `waddles.core.rust-data-plane` | The Rust stage runners' drain loops. OFF ⇒ the stage serves health and metrics and drains nothing, which is the safe state during rollout. |
| `waddles.core.wasm-bundles` | Loading and invoking WASM components at all. |
| `waddles.core.generic-intake` | `POST /intake/webhook/{tenant}/{source}` and `POST /intake/events`. |
| `waddles.core.prebuilt-bundles` | Accepting `artifact: prebuilt` uploads, in addition to the `bundles.allow_prebuilt` admin setting. Both must permit it. |
| `waddles.core.bundle-egress` | The `http` capability. OFF ⇒ every egress call returns `denied("feature_disabled")`. |
| `waddles.core.spine-at-least-once`  Grant-scoped `XREADGROUP`/`XACK` with `XAUTOCLAIM` recovery. OFF ⇒ the stage reads without claiming abandoned entries, for an emergency rollback of just the recovery mechanism. |

The `--dev` flag convention applies unchanged: an undocumented flag unlocking Professional/Enterprise features for single-user evaluation, gated by the three fail-closed conditions (PenguinTech-controlled domain, ≤ 1 user in the identity table, user creation capped at 1 while active), with the mandatory stderr banner. It grants nothing in this subsystem, since every flag here is `free`-tier, but the services still implement it identically to the rest of the platform.

---

## 14. Testing strategy

### 14.1 Shared golden fixtures

A single committed fixture set is the contract between the Python and Rust implementations. Location: `tests/golden/` at the repo root, read by both `libs/flask_core`'s pytest suite and `penguin-spine`'s Rust tests.

| Fixture family | Contents | Asserted by |
|---|---|---|
| `envelopes/valid/*.json` | ≥ 20 `StageEnvelope` documents covering: tenant-wide (`community: null`), community-scoped, `target_app_id` set, `trace` set and absent, `session_id` set and absent, empty `payload`, unicode payload, maximum-length `app_id` | Both: deserialize → re-serialize → byte-identical |
| `envelopes/invalid/*.json` | ≥ 25 documents, one per rejection reason: missing field, wrong type, unknown top-level key, bad stage, legacy pre-`event` shape, non-object payload, empty `platform`, `schema_version` absent or `1`, missing `workstream_id`/`event_id`/`binding`, tampered `binding.mac` | Both: deserialization/verification **fails**, with the same reason classification |
| `keys/*.json` | `{tenant, community, platform, source_id} → expected source-stream key` and `{tenant, community, app_id} → expected action-stream, cfg and state keys`, including the `_tenant` rendering | Both: key builders produce the exact string |
| `entries/*.json` | Stream-entry fixtures: `{field: "env", value: <envelope JSON>}` for event streams and `{field: "rec", value: <DLQ record>}` for the DLQ | Both: the entry payload is the envelope JSON verbatim under a single field — no second encoding layer, no base64 |
| `dlq/*.json` | One record per `error.kind` | Both: serialize/deserialize round-trip, field-for-field |
| `manifests/*.yaml` | One valid `bundle.yaml` v2 per language, plus one per rejection rule V1–V31 with its expected `reason` code | `penguin-bundle-host::manifest` and hub-api's install path |

CI fails if either side skips a fixture: each suite asserts `fixtures_examined == fixtures_on_disk`, a non-zero denominator, and prints the count.

### 14.2 `penguin-spine`

- At-least-once: read → kill the consumer before `XACK` → another consumer's `XAUTOCLAIM` recovers the entry after `SPINE_CLAIM_IDLE_MS` → it is delivered again exactly once more, and the PEL is empty afterwards.
- Claim concurrency: three simulated replicas running `XAUTOCLAIM` against the same dead consumer each claim disjoint entries; no entry is processed twice concurrently.
- Redelivery cap: the sixth delivery of the same entry goes to the DLQ with `max_deliveries` and is `XACK`ed, not claimed again.
- Group isolation: a stuck group's PEL grows while a healthy group on the **same stream** keeps acking — the property the per-bundle group exists for.
- Fan-in: one `XADD` is observed by every group on the stream, each exactly once; adding a group does not change the writer's cost.
- Stream bounding: writing `SPINE_STREAM_MAXLEN + 50000` entries leaves the stream within the approximate bound and advances `waddles_stream_trimmed_total`; the test asserts trimming happened, not an exact length, because `MAXLEN ~` is approximate by design.
- DLQ capping: the DLQ stream stays within `SPINE_DLQ_MAXLEN`.
- Grant enforcement: a `SpineClient` asked to read a stream outside its grant list refuses with `StreamNotGranted`, even when the consumer group exists.
- **Client rule 1:** constructing a blocking-read client with `SPINE_BLOCK_MS`/`RELAY_BLOCK_TIMEOUT_S` not strictly less than `DRAIN_SOCKET_TIMEOUT_S` fails at startup with an error naming both values.
- **Client rule 2:** issuing `XREADGROUP ... BLOCK`, `BRPOP` or `BLMOVE` on the shared pool is refused; and a test drives a cancelled blocking read followed by an `XACK` on the *same* logical client to prove the dedicated connection prevents the stale-reply hand-off.

Backends: `redis-rs` against a real Valkey container for integration tests; no mock-only coverage of the command semantics.

### 14.3 WIT conformance suite

One example bundle per Tier 1 language (`bundles/rust/example`, `bundles/javascript/example`, `bundles/python/example`) plus one hand-built Tier 2 component. The suite asserts, for each:

1. The component compiles/validates under the pinned wasmtime.
2. Its exports satisfy `waddle:bundle/stage@1.0.0` (V25).
3. Its imports fall within its language's allowlist (§6.5, V31), and a `wasi:sockets` call fails cleanly with `PermissionError` rather than trapping or connecting.
4. `transform` on a golden event returns the expected event, and `none` for a no-reply input.
5. `dispatch` returns `transport-result` on success and a `transport-error` with the expected `retryable` value on each failure class.
6. The unimplemented stage's stub returns `unsupported-stage` / `UNSUPPORTED_STAGE` rather than trapping.
7. Every host capability is exercised at least once and the denial path is exercised at least once.

### 14.4 Bundle compatibility suite

- **Every** file under `bundles/python/` is compiled by the **real** `bundle-compiler` in CI (not a stub), and each is invoked through a real `bundle-executor` with golden events for its stage. The suite asserts `bundles_compiled == bundles_on_disk` and prints both numbers.
- The bundles' pytest suites — already green natively after the M1.5 DAL migration — keep running unchanged against the `waddle-sdk` shim; their assertions are the compatibility oracle for D7.
- A repo-wide check asserts zero `flask_core.database` and zero `pydal` imports under `bundles/python/`, printing the number of files scanned; the compiler's own `legacy_dal_import` rejection is exercised by a fixture bundle that still has one.
- The six ported ingest normalizers are covered by translating `core/svc_ingest/bundles/test_*_ingest.py`'s cases into Rust table tests, one case per original assertion; the suite asserts the case count matches.
- Retry classification parity: the action-stage cases from `core/svc_action/tests/` are replayed against the Rust runner and must produce the same retry/no-retry decision for every input.

### 14.5 Per-service and per-crate gates

Identical to `.github/workflows/rust-svc-streaming.yml`, applied to all four services, both new binaries, and every new `penguin-libs` crate:

```
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo deny check                     # advisories + licenses + bans + sources
cargo audit
cargo test
cargo llvm-cov --fail-under-lines 90
semgrep --error                      # with the Waddles ruleset
gitleaks detect --no-git
trivy image --exit-code 1 --severity HIGH,CRITICAL
```

Two RBAC equality gates run alongside them, against the deployed alpha stack (§11.10):

```
make test-rbac-postgres   # information_schema.role_table_grants == config/postgres/rbac-matrix.yaml
                          # set equality both ways; prints roles/tables examined;
                          # fails below 8 roles or 8 tables
make test-rbac-valkey     # ACL LIST == config/valkey/acl-matrix.yaml, plus the
                          # per-user negative tests of §11.10.2
```

`penguin-libs` crates run the same gates in that repo, on their own `release/{lib}/v{X}.{Y}.x` branches, and publish to crates.io through trusted publishing.

No step is wrapped in `|| true`. Where a tool is run twice (a JSON report followed by a gating run), the gating run is on the same inputs in the same job.

### 14.6 Negative sandbox tests

Each is a dedicated test whose **pass condition is the failure of the attack**, written against a purpose-built hostile bundle in `bundles/test/hostile/`:

| # | Attack | Expected |
|---|---|---|
| 1 | Guest calls `wasi:sockets` through the executor's native denying implementations | Every call returns `access-denied` immediately (guest Python sees `PermissionError`), no connection is attempted, **the component keeps running and the invocation completes**, the call is counted as `denied`, and three such calls in the window trip the bundle. A Tier 2 component importing a `wasi:*` interface outside the world plus the stub set is rejected at validation with `forbidden_host_import` |
| 2 | Escaped executor attempts to reach Valkey, Postgres, the API server or the internet (simulated by a probe container in the executor's pod) | Every connection fails — the NetworkPolicy allows only the stage's host-API port and the bucket; the probe records each failure and the test asserts all of them |
| 3 | Guest calls `http.send` to a host not in `egress` | `denied("host_not_declared")`, `waddles_egress_denied_total` +1 |
| 4 | Guest calls `http.send` to `169.254.169.254` via a DNS name that resolves there | `denied("ssrf_blocked_address")` — and the same with `bundles.egress.allowPrivateHosts: true`, which must **not** unblock cloud metadata, loopback, link-local, unspecified or multicast |
| 4a | Guest calls `http.send` to an allowlisted host resolving into RFC 1918 | `denied("ssrf_blocked_address")` by default; **allowed** with `bundles.egress.allowPrivateHosts: true` — both branches asserted |
| 4b | A stage connects to Postgres, Valkey, the bucket, hub-api and the OTLP collector on private addresses | All succeed with `bundles.egress.allowPrivateHosts: false`: the test proves the bundle SSRF guard is never consulted for operator-configured endpoints (§8.5) |
| 5 | Guest follows a redirect to an undeclared host | `denied("redirect_off_allowlist")` |
| 6 | Guest issues SQL against a table outside `data.tables` | `denied(...)` before Postgres is touched; a second test bypasses the parser check and asserts the Postgres role also refuses |
| 7 | Bucket serves a component whose bytes do not match the recorded digest | Load refused, previous version still serving, `waddles_bundle_digest_mismatch_total` +1 |
| 8 | Sidecar signature invalid | Load refused, same assertions |
| 9 | Guest spins forever | Epoch deadline fires at `limits.timeout_ms`, DLQ `call_timeout`, trip recorded |
| 10 | Guest allocates past its memory cap | Trap, DLQ `memory_limit`, trip recorded |
| 11 | Three trips within the window | Bundle DISABLED, `waddles_bundle_disabled{app_id}` = 1, subsequent events DLQ'd with `bundle_disabled` (never silently dropped) |
| 12 | Executor attempts to read the stage's environment, Secrets or ServiceAccount token | None are mounted in the executor's pod and `automountServiceAccountToken: false`; the probe's failure is asserted |
| 12a | `WADDLES_SANDBOX_GVISOR=true` but the pod is scheduled without the gVisor `RuntimeClass` | Startup verification fails: logs ERROR, `waddles_sandbox_gvisor{enabled="true"}` = `0`, `/health` reports `sandbox: "runc"` — the pod **proceeds**, never crash-loops (D32: absence is no longer fail-closed) |
| 12b | `WADDLES_SANDBOX_GVISOR=false` (default, D32) | No warning — this is the default posture, not a fallback. `waddles_sandbox_gvisor{enabled="false"}` = `1` and `{enabled="true"}` = `0`, `/health` reports `sandbox: "runc"`, and every other sandbox layer (own pod, rootless, caps dropped, read-only rootfs, NetworkPolicy, WASM isolation) is asserted still in force |
| 12c | Executor reports `runc` to a stage whose `WADDLES_SANDBOX_GVISOR` is `true` | Connection refused with `UNSANDBOXED_EXECUTOR`, `waddles_host_api_rejected_total` +1 |
| 12d | A workload other than the executor dials the stage's host-API port | Denied by the NetworkPolicy; a connection presenting a wrong or unsigned certificate is additionally refused at the TLS layer and counted |
| 13 | Guest sets a module-level variable and is invoked again | The variable is reset — no state survives a call |
| 14 | Bundle A attempts to read bundle B's KV namespace or config | Keys are namespaced per `app_id`; the read returns none and is counted |
| 14a | Bundle A's dispatch observes bundle B's action entries | Never: each action stream has exactly one consumer group and the stage reads only the bundle's own stream. The test writes to B's action stream and asserts A's dispatch is never invoked and A's group never appears on B's stream (§5.9) |
| 14b | A process bundle sets `_target_app_id` to an app outside its approved `routes_to` | The redirect is dropped, nothing is written to the target's stream, `waddles_route_denied_total{app_id,target}` +1, WARN logged with both ids |
| 14g | Each non-writer role (`svc_ingest`, `svc_process`, `svc_action`, `svc_streaming`, the executor role, the webui role) attempts `INSERT`, `UPDATE` and `DELETE` on `app_versions` | Every one of the 18 statements fails at the SQL level with a permission error; the test reports the number of roles and statements exercised, and a zero denominator is a failure (§6.10) |
| 14h | The publisher writes a digest, then it is changed | Both writes appear in the audit log with the writing role, the `(app_id, version)` key, and the old and new `artifact_digest` — the change is attributable, which is the property §6.10 guarantees |
| 14i | The build container attempts to reach the network, the bucket, Postgres or hub-api | Every attempt fails: its NetworkPolicy allows nothing and it mounts no credential. The digest is computed only in the publisher, asserted by mutating the component in `/work` after the build and observing the recorded digest change rather than the tampering going unnoticed (§4.6) |
| 14d | A version's manifest requests a table (or host, or capability, or stream) that is **not** in the approved permission summary | Denied at runtime — the stage generates its allowlists from the approval record, not the manifest — the call returns `denied(...)`, `waddles_host_call_denied_total` / `waddles_egress_denied_total` is incremented, and the mismatch is logged at ERROR naming the manifest value and the approved set (§9.7.3) |
| 14e | An upgrade widens any permission | The new version is published but not activated until re-approval; the diff names exactly the added stream/host/table/capability/`routes_to` target or raised limit. A narrowing upgrade auto-approves and writes an audit entry |
| 14f | A headless install passes a stale `permission_hash` | `409 permission_hash_mismatch`, nothing is approved, and the response carries the current summary and hash |
| 14c | A process bundle whose manifest requests only `discord` is delivered Twitch entries | Never: the stage reads only granted streams, asserted with a Twitch consumer group deliberately pre-created on the Twitch stream so the test proves the stage's grant check, not the group's absence (§5.2) |
| 15 | Executor sends a frame larger than `EXECUTOR_MAX_FRAME_BYTES` | Stage kills and restarts the executor, `waddles_executor_restarts_total` +1 |
| 16 | The executor binary links a networking or database crate | `cargo tree -p bundle-executor` contains no `reqwest`, `redis`, `deadpool-redis`, `sea-orm` or `sqlx`; the CI check fails the build if any appears, and reports the number of crates examined |

### 14.7 Telemetry validation (blocking, every commit)

Every service's smoke test runs against a local OTLP sink and asserts, printing every count:

| Assertion | Threshold | On zero |
|---|---|---|
| Log records received | ≥ 1 | FAIL |
| Metric data points received | ≥ 1 | FAIL |
| Histogram metrics received | ≥ 1 | FAIL |
| Spans received | ≥ 1 | FAIL (all four services make inter-service or DB calls) |
| `penguin-logging` in use (no hand-rolled `println!`/`eprintln!` in service source) | 0 violations across ≥ 1 scanned file | FAIL |

A sink that fails to start is a FAIL, never a skip. Zero files scanned is a FAIL.

### 14.8 End-to-end alpha stack

Against a freshly destroyed and rebuilt alpha cluster, with seeded mock data (3–4 items per feature):

1. Real Twitch and Discord traffic through the echo, music, alias, reputation and overlay bundles.
2. Assertions: the expected outbound message arrives on each platform; `action_dispatch_log` rows match; no DLQ entries beyond the ones a deliberate failure test creates.
3. OTel sink counts asserted, with the numbers printed.
4. `waddles_e2e_latency_seconds` p95 asserted under **3 s** for text and under **5 s** for A/V paths. A run whose histogram has zero observations is a FAIL, not a pass.
5. A hot-swap is exercised live: publish a new version, assert convergence within 65 s, assert zero dropped events across the swap.
6. A bucket outage is simulated: pods keep serving, `waddles_bundle_stale_age_seconds` grows, no event is lost.

### 14.9 Verification integrity

Applied to every gate in this section: no `|| true` on a linter, scanner or test; `set -euo pipefail` in every script and hook; `${PIPESTATUS[0]}` rather than `$?` after a pipeline; every "clean" result reported with the number of items examined; a zero denominator is a failure. Each new gate is made to fail on purpose once, before it is trusted.

### 14.10 Webhook intake source-restriction tests (svc_ingest)

Mandatory negative tests for §4.1.1 — each asserts the attack is **rejected**, and each is counted so a zero-match run cannot pass silently:

| # | Test | Expected |
|---|---|---|
| 1 | A **valid** Twitch EventSub signature arrives from a client address that does not forward-confirm to `twitch.tv` and matches no configured `originCidrs` | `403 origin_not_trusted`; `ingest_webhook_rejected_total{platform="twitch",reason="origin"}` +1 — signature validity alone never admits the request |
| 2 | The client address's PTR record names a `twitch.tv` host, but the forward A/AAAA lookup of that name does **not** contain the client address (a spoofed or stale PTR) | `403 origin_not_trusted` — FCrDNS requires both halves to agree, not the PTR alone |
| 3 | A generic source's per-source HMAC signature is valid, but no configured bearer token, basic credential or source IP matches | `401 second_factor_failed` (or `401 auth_not_configured` if the source has none configured) — a valid signature alone never admits a generic webhook |
| 4 | `X-Forwarded-For` is set on a request whose direct TCP peer is **not** in `WADDLES_INGEST_TRUSTED_PROXIES` | The header is ignored entirely; the client address used for every check is the direct peer, never a value taken from the spoofed header |
| 5 | A Twitch EventSub message with a message id already seen within the replay cache window is resent, with both signature and origin valid | `409 duplicate_message_id`; the second delivery is dropped and not double-enqueued |

Each test asserts the specific rejection reason and increments the specific counter — a generic "it returned non-200" assertion does not satisfy this table.

### 14.11 Workstream identity and tenant-wall tests (D30/D31)

Mandatory negative and continuity tests for §5.11/§5.12 — each is counted so a zero-match run cannot pass silently:

| # | Test | Expected |
|---|---|---|
| 1 | An envelope carrying a **valid** `binding.mac` for its own tenant/workstream is read from a stream key belonging to a **different** tenant (constructed directly in Valkey, bypassing ingest) | Rejected: `error.kind = "tenant_boundary"`, DLQ'd, never retried, `waddles_tenant_boundary_violations_total{stage,reason="tenant_mismatch"}` +1 — a valid MAC for the wrong stream is not a valid envelope for that stream (§5.11) |
| 2 | A process bundle's `transform` return value sets `tenant_id`, `community_id` or `workstream_id` in its output | The stage ignores the bundle-supplied fields entirely and copies the input envelope's identity onto the output unconditionally; the attempt is counted (`waddles_tenant_boundary_violations_total{stage="process",reason="bundle_set_identity"}`), never silently accepted |
| 3 | A bundle declares `routes_to` naming an app installed in a **different** tenant | Refused at install-time approval (§5.9, §9.7.1) — the version cannot be approved with that entry; a second test constructs the redirect at runtime regardless and confirms the stage also drops it independently, incrementing `waddles_route_denied_total` |
| 4 | `binding.mac` is tampered (one byte flipped) on an otherwise well-formed envelope | Rejected: `error.kind = "tenant_boundary"`, counted, never retried, regardless of which `kid` is claimed |
| 5 | A bundle's `db` host call, invoked under tenant A's envelope, attempts to read a row belonging to tenant B in the same bundle-owned table | Zero rows returned — RLS scoped by `SET LOCAL waddles.tenant`/`waddles.community` (§7.4) refuses the row at the database layer, not just at the application layer |
| 6 | Trace continuity: one inbound platform event is traced end to end | A single `trace_id` spans `intake.request`/`ingest.normalize` → `spine.enqueue`/`spine.take` (process) → `bundle.invoke` and its `host.*` calls → `action.dispatch` → the outbound platform call; every span in the chain carries the same `waddles.workstream_id` |
| 7 | Usage totals: an e2e run (§14.8) pushes a known number of events through one workstream | `workstream_usage_hourly` rows for that `(tenant_id, community_id, workstream_id)` sum to the same event/invocation/action counts the run pushed, once the aggregator has drained `waddles:usage` |

---

## 15. Migration & cut-over

### 15.1 Shape

One feature branch off `release/v3.0.X` carries the entire change:

- the four Rust services,
- `bundle-executor` and `bundle-compiler`,
- the `bundles/python/` tree (sources moved, DB lines migrated to penguin-dal per D21a, plus one new `bundle.yaml` per bundle),
- `sdk/waddle-sdk{,-rs,-js}`,
- `wit/waddle-bundle/stage.wit`,
- chart changes (§12.3),
- the docs rewrite,
- deletion of the Python service directories.

It merges only when every gate in §14 is green **and** the alpha end-to-end run passes. There is no fallback by design (D3).

### 15.2 Deletions

| Deleted | Replaced by |
|---|---|
| `core/svc_ingest/**/*.py` (app, runner, receivers, fanout, eventsub, supervisor, socket_lease, outbound_drain, bundles, tests) | `core/svc_ingest/src/**`, `penguin-connectors` |
| `core/svc_process/**/*.py` | `core/svc_process/src/**` |
| `core/svc_action/**/*.py` | `core/svc_action/src/**` |
| `core/svc_streaming/{app.py,blueprints,services,openapi,Dockerfile}` and its pytest tree | the existing Rust build; `Dockerfile.rust` renamed to `Dockerfile` |
| `flask_core.stage_runner.load_entrypoint` and the `importlib` loading path | the executor |
| `KNOWN_SURFACES`' `ingest` as a *bundle-pluggable* surface | fixed normalizers in svc-ingest (the string stays in the manifest vocabulary only to reject it, rule V15) |

`libs/flask_core` itself **stays** — hub-api and the tests use it. It gains: the envelope strictness tightening, the `schema_version`/`workstream_id`/`event_id`/`session_id`/`trace`/`binding` fields of §5.11 (D30), and the removal of the bundle-loading machinery.

### 15.3 Behaviour changes to announce

Three user-visible changes are not pure ports:

1. **Ingest is no longer bundle-pluggable.** Six `*_ingest.py` bundles become fixed code. Their `app_catalog.stages.ingest` rows are removed by a migration; any third-party ingest bundle (none exist today) would need to move to the generic intake.
2. **Per-community activation is now honoured on the ingest fan-out path.** `fanout.py` previously always fell through to each Feature's shipped default App. Communities that had activated a non-default App for an ingest-fed Feature will now actually get it. The migration notes list the affected `(community, feature)` pairs so operators can confirm intent before the cut-over.
3. **Bundle database access moves to `penguin-dal`.** Every bundle's DB-access lines are rewritten (M1.5); `flask_core.database` and `pydal` stop being importable from a bundle at all, enforced by the compiler (D21b). Authors of out-of-tree bundles must migrate before their next upload, and the rejection message names the module and the equivalent.
4. **Events are no longer silently lost on a crash, and are no longer silently dropped on failure.** At-least-once delivery means a bundle can see the same event twice after a crash; bundles that were accidentally relying on at-most-once must be idempotent. Every existing first-party bundle was reviewed for this and is idempotent or made so; the review is part of M2's deliverables.

### 15.4 Data migration

The envelope JSON is unchanged, but the **transport is not**: the list-based process keys are replaced by per-ingest-source streams (D23). In-flight events therefore do **not** survive the cut-over, which is acceptable and is stated rather than glossed: the cut-over is a clean-cut deploy (D3), the pipeline is drained before it, and a chat event that is seconds old has no replay value. The action hop keeps its key shape but becomes a stream, so the same applies there. `waddles:dlq:*` is re-created as a stream; any list-shaped DLQ content from a pre-cut-over alpha is exported to a file by the migration script before the key is deleted, with the record count printed.

Database migrations are additive:

| Migration | Adds |
|---|---|
| `app_catalog` version columns | `artifact_digest`, `artifact_kind`, `language`, `scan_status`, `manifest_json` |
| `custom_platforms` | per-tenant registered platform names for the REST intake |
| `intake_sources` | `{tenant, source_id, source, platform, secret_ref, community, mapping, enabled, auth}` — `source_id` is the stable id that names the source's stream; `auth` is the source-restriction/authentication object of §10.3 |
| `app_stream_grants` | §6.8 — one row per (bundle, granted stream) |
| `bundle_scan_findings` | per-version scanner findings summary |
| `app_install_approvals` | §6.9 — the approved permission summary and its hash, which the runtime derives authorization from |
| `app_versions` | §6.10 — the digest table, with its audit trigger and its two-writer grant set |
| `app_active_versions` | §6.10 — which version is live per scope; hub-api-owned, the rollback target |
| RBAC matrices | `config/postgres/rbac-matrix.yaml` and `config/valkey/acl-matrix.yaml`, plus the roles they generate (§11.10) |
| `global_settings` seed | `bundles.allow_prebuilt = true` |
| Per-bundle role bootstrap | the RLS policies on bundle-owned tables |
| Ingest-stage cleanup | removes `stages.ingest` from the six affected `app_catalog` rows |
| `workstreams` | §6.11 — one row per `intake_sources` row, backfilled 1:1 at migration time so every existing source has a `workstream_id` before D30 code ships (D30) |
| `workstream_usage_hourly` | §6.12 — append-only usage table; empty at migration, populated from the first `metering.enabled` hour onward (D31) |
| Envelope binding key | `security.envelopeBinding.keySecretRef` (§12.3) provisioned as a chart Secret, first `kid` generated at install time, mounted only in the four Rust stage services (D30) |

### 15.5 Rollback posture

There is no rollback to Python. The rollback units are smaller and each is real:

- a bad bundle version → flip the active digest (≤ 65 s convergence);
- a bad capability → turn off its PostHog flag (§13.5);
- a bad service build → Helm rollback to the previous image tag, which is still a Rust build.

---

## 16. Milestones

```
M1 ──┬──────────────▶ M2 ──┬──▶ M3 (svc_action) ──┐
     └─▶ M1.5 ─────────┘   ├──▶ M4 (svc_process) ─┼──▶ M6
        (Bundle DAL         └──▶ M5 (svc_ingest) ──┘
         migration, Python-only — gates M2's compiler work)
```

M1.5 runs alongside M1 and must finish before M2's compiler work begins — the compiler rejects the legacy DAL imports it removes (D21b), so compiling an unmigrated bundle is not possible by design. M3, M4 and M5 run in parallel once M1, M1.5 and M2 are complete. M6 requires all three.

### M1 — `penguin-libs` crates

| Deliverable | Done when |
|---|---|
| `penguin-spine` | Key builders, envelope types, `XADD`/`XREADGROUP`/`XACK`/`XAUTOCLAIM`, group management, grant-scoped reads, DLQ, `MAXLEN ~` bounding, the dedicated blocking-read client — all §14.1/§14.2 tests green, coverage ≥ 90 % |
| `penguin-bundle-host::wire` + `::manifest` | Frame codec and the 31 manifest rules, golden manifests green |
| `penguin-logging` | Sanitization ported verbatim, OTel logs/metrics/traces wired, health/metrics surface, `transport:` reporting |
| `penguin-connectors` (5 crates) | Receivers, senders and signature verification per platform, against recorded fixtures |
| `penguin-licensing` | `build-rust-licensing` + `publish-rust-licensing` jobs, `0.1.0` on crates.io |
| `flask_core` alignment | Strict envelope deserialization + `schema_version`/`workstream_id`/`event_id`/`session_id`/`trace`/`binding`, golden fixtures shared with Rust |
| **(M1a)** `penguin-spine`: envelope binding | `workstream_id`/`event_id`/`session_id`/`trace`/`binding` types, `binding.mac` compute-and-verify helpers, `kid` lookup and rotation-overlap acceptance — unit-tested against golden fixtures, coverage ≥ 90 % (D30) |
| **(M1c)** `penguin-bundle-host::host::db` / `::kv` | Tenant-scoped host calls wired to the binding-verified envelope: `SET LOCAL waddles.tenant`/`waddles.community` and KV/config/state key scoping proven to reject a mismatched-tenant invocation (§7.4, §6.2, D30) |

### M1.5 — Bundle DAL migration (Python only, parallel with M1)

Pure Python work, no WASM involved, completed and merged before M2's compiler work starts.

| Deliverable | Done when |
|---|---|
| Inventory | Every bundle under `bundles/python/` that imports `flask_core.database.AsyncDAL` or reaches a DAL through `get_bundle_dal()` is listed, with the count reported — a zero count is a failure of the inventory, not a pass |
| Migration | Each listed bundle's DB-access lines are rewritten against the `penguin-dal` public API (`/home/penguin/code/penguin-libs/packages/python-dal/src/penguin_dal/__init__.py`); bundle logic and entrypoint signatures are otherwise untouched |
| Tests | Each bundle's existing pytest suite is updated to the new DAL and passes **natively** (no WASM), with coverage unchanged or better |
| Gate | A repo-wide check reports zero occurrences of `flask_core.database` and `pydal` under `bundles/python/`, printing the number of files scanned |
| `consumes` migration | Every process bundle gains a `consumes` section in its new `bundle.yaml`, translated from today's ingest-stage manifest tags with the mapping table below; the number of migrated bundles is reported and must equal the number of process bundles on disk |

**Legacy `consumes`-tag mapping.** Today's ingest-stage manifests carry flat string tags (`"twitch.eventsub"`, `"kick.message"`, `"discord.message"`) consumed by `core/svc_ingest/fanout.py`. They translate mechanically:

| Legacy tag | New `consumes` rule |
|---|---|
| `twitch.message` (IRC) | `{platform: twitch, event_types: ["chat.message"]}` |
| `twitch.eventsub` | `{platform: twitch, event_types: ["channel.*", "stream.*"]}` |
| `discord.message` | `{platform: discord, event_types: ["chat.message"]}` |
| `slack.message` | `{platform: slack, event_types: ["chat.message"]}` |
| `youtube.message` | `{platform: youtube, event_types: ["chat.message"]}` |
| `kick.message` | `{platform: kick, event_types: ["chat.message"]}` |
| (the echo demo bundle, which consumed everything) | Five explicit rules, one per platform — **not** `platform: "*"`, so the migration does not require turning on `allow_wildcard_consumes` anywhere |

Command-only bundles additionally gain a `filters.command_prefix` matching the prefixes their `transform` already tests for, which is where most of the fan-out saving comes from. The committed per-bundle mapping is part of this milestone's output, not left to the implementer's judgement.

### M2 — Compiler, SDKs, bucket flow, hub-api hooks

*(The former "M1 verification task" — checking `runsc` availability on alpha and DigitalOcean — is removed: §18 R3 answered it definitively negative on 2026-09-21, and D32 dropped gVisor from the default posture as a result. Full history: §18 R3.)*

| Deliverable | Done when |
|---|---|
| `bundle-compiler` | Four-phase sandboxed run (rootless, no credentials, no network; optional `RuntimeClass` when gVisor is opted in — D32), the pinned toolchain image with `wasm32-wasip1` pre-installed, the `--stub-wasi` / `--disable all` build flags, the per-language import validator (`wasm-tools component wit` + allowlist), all scanners with non-zero denominators, content addressing, Ed25519 sidecar, bucket upload |
| `waddle-sdk` (Python) | Same import names, the single `penguin-dal` facade over the WIT `db` import, the `sitecustomize` runtime shims (`to_thread`/`run_in_executor`, `PollLoop` entry), every migrated bundle's pytest suite passing |
| `waddle-sdk-rs`, `waddle-sdk-js` | Example bundle per language passing the WIT conformance suite |
| Bucket flow | MinIO in alpha, Nest configurable; poller, verification, precompilation, hot-swap proven in a harness |
| hub-api install hooks | `POST /api/v1/apps/{app_id}/versions`, the version state machine, `bundles.allow_prebuilt`, the distribution API's new fields, the permission-consent screen and approval API (§9.7), `app_install_approvals` and `app_stream_grants` |
| Idempotency review | Every first-party bundle reviewed and, where needed, made idempotent under at-least-once (§15.3 item 3) |
| **(M2b)** hub-api: workstreams + usage aggregator | `workstreams` table 1:1-backfilled from `intake_sources` (§6.11), created on every new source going forward; the `waddles:usage` consumer writing `workstream_usage_hourly` (§6.12); the per-community admin usage view/API; `routes_to` cross-tenant refusal wired into version approval (§5.9, D30/D31) |

### M3 — `svc_action` (parallel)

| Deliverable | Done when |
|---|---|
| Rust service on the `svc_streaming` template | `/health`, `/healthz`, `/metrics`, OTel, config, Dockerfile, CI workflow |
| Executor integration | Load/invoke/hot-swap, all §14.6 negative tests green |
| Built-in senders | Discord, Slack, YouTube, Kick REST; Twitch via the relay |
| Retry + audit parity | `action_dispatch_log` rows and retry decisions match the Python suite's expectations for every replayed case |
| Hop verification + usage | `binding.mac` and tenant/community/grant/approval verification (§5.11) run before every dispatch; outbound credential/target resolved only from the verified envelope; usage deltas `XADD`ed to `waddles:usage` (§5.12); §14.11 tests 1, 4, 5 (as applicable), 6 and 7 green for this stage (D30/D31) |

### M4 — `svc_process` (parallel)

| Deliverable | Done when |
|---|---|
| Rust service + executor integration | As M3 |
| Built-ins | Moderation gate, enforcement routing, cross-app `_target_app_id` routing |
| Bundles | `bot_process`, shoutout, live status, activity feed, reputation accrual running as WASM bundles |
| DB host capability | Parser allowlist + per-bundle role + RLS, with the negative tests green |
| Hop verification + usage | `binding.mac` and tenant/community/grant/approval verification (§5.11) run before every bundle invocation; bundle output never overwrites identity fields; cross-tenant `routes_to` rejected at runtime; usage deltas `XADD`ed to `waddles:usage` (§5.12); §14.11 tests 1, 2, 3 (runtime half), 4, 5, 6 and 7 green (D30/D31) |

### M5 — `svc_ingest` + generic intake (parallel)

| Deliverable | Done when |
|---|---|
| Rust service | Fixed normalizers for all six platforms, ported test cases counted and green |
| Connectors live | Twitch IRC + EventSub webhook and websocket, Discord gateway, Slack Socket Mode, YouTube poll, Kick Pusher + webhook, all lease-guarded |
| Outbound relay | Dedicated-connection blocking pop, both client rules tested |
| Generic intake | `POST /intake/webhook/{tenant}/{source}` and `POST /intake/events` with auth, replay, dedupe, mapping, rate limits, every error code covered |
| Activation fix | Distribution-API resolution on every path, affected `(community, feature)` pairs listed |
| Workstream minting | Every envelope carries `workstream_id`, `event_id`, `session_id` (when the platform has one) and `trace`, minted from `intake_sources`/`workstreams` (§5.11) and never from payload; `binding.mac` computed under the active `kid`; usage deltas `XADD`ed to `waddles:usage` (§5.12); §14.11 tests 1 and 4 (mint-side fixtures) and 6 green (D30/D31) |

### M6 — Streaming retirement, charts, cut-over, docs

| Deliverable | Done when |
|---|---|
| Streaming | Python alpha deleted, `Dockerfile` renamed, `penguin-logging` and `penguin-licensing` adopted, CI building the Rust image |
| Charts | All values of §12.3, CA/cert provisioning, Valkey ACL file, required Secrets, `sandbox.*` values (gVisor optional, off by default — D32), and the optional installer DaemonSet wired |
| Docs | `docs/APP_BUNDLE_AUTHORING.md` v2 (WASM, `bundle.yaml` v2, the WIT world, capabilities, egress, limits, `ingest` removed from the pluggable surfaces), per-service READMEs, migration notes |
| Cut-over | Python service directories deleted; every §14 gate green; alpha e2e green including latency, hot-swap and bucket-outage scenarios |
| RBAC/e2e for workstreams | `config/postgres/rbac-matrix.yaml` and `config/valkey/acl-matrix.yaml` carry the D30/D31 rows (§11.10.1, §11.10.2) and their equality gates are green; the full §14.11 suite (all 7 tests) green against the alpha e2e run, including the usage-totals reconciliation (D30/D31) |

### Follow-on work (not in this spec's milestones)

Once M2 lands (compiler plus the Tier 1 SDKs), a bundle backlog of ten bundles inspired by PenguinTwitchBot (MIT; **ideas and design only, no code copied**; the creator's permission has been granted) is written against the Tier 1 SDKs, S-sized first — `stream-counters`, `weather-command`, `auto-timers`, `top-lists`, `custom-command cooldown/limits` — then `channel-points-bridge`, `go-live-announcer`, `tts-announcer`, `prize-wheel`, `clip-auto-download`; each is a process- or action-stage bundle, and collectively they double as real-world SDK validation. Reading source is the local clone at `~/external-code`. The permission carries a hard condition, which is a requirement on this work rather than a courtesy: **before the first derived bundle merges**, `SuperPenguinTV` (GitHub `Psychoboy`, PenguinTwitchBot) must be added to the repository's contributors file and to a README "Acknowledgements" section, and every derived bundle's manifest and README must carry the line "inspired by PenguinTwitchBot by SuperPenguinTV (with permission)".

---

## 17. Standards

The constraints below bind this design. They are summarized, not restated in full; the named rule file is authoritative.

| Area | Constraint | Where it lands in this spec |
|---|---|---|
| Language by tier | Everything in-line of traffic is Rust — tier, not volume, decides. Agents and CLIs are Rust regardless. Security-sensitive work is Rust or Python, never Go. | D1; all four services and both binaries are Rust |
| Rust stack | Axum + tokio + SeaORM + `tracing`; `rustls` never native TLS; `jsonwebtoken` with the `aws_lc_rs` backend | §4.1–§4.6 |
| Rust lints | `unsafe_code = "deny"`, `missing_docs = "deny"`, `clippy::unwrap_used = "deny"`, `cargo clippy -D warnings` | §14.5; every new crate |
| Supply chain | `cargo deny` + `cargo audit` in CI; exact `=x.y.z` pins, `Cargo.lock` committed; no PRC-origin or sanctioned-entity crates (the `xiu` ban in `core/svc_streaming/deny.toml` is the precedent); build tools fetched at pinned versions with SHA-256 verification | §12.1, §14.5 |
| Coverage | 90 % minimum, lines/branches/functions/statements; builds fail below | §14.5 (`cargo llvm-cov --fail-under-lines 90`) |
| Containers | Rootless at both layers: rootless runtime and `USER appuser` / `runAsNonRoot: true`. No root exception is requested by this design | §12.1, §12.4 |
| Observability | OTel logs **and** metrics **and** traces, destination configurable only through the standard OTLP env vars, no vendor SDK; penguin logging used alongside, never instead; histograms for load and latency first; a dead exporter never fails a request | §13 |
| Telemetry gate | Blocking smoke-test validation with printed counts every commit | §14.7 |
| Transport security | TLS 1.2+ everywhere, mTLS certificate validation, at-rest encryption on every store | §11.6 |
| Secrets | Never in a distributed build, never on a CLI flag, never in logs or stdout, env/file only, masked in CI | §11.5 |
| Verification integrity | No `|| true` on a gate; `set -euo pipefail`; `${PIPESTATUS[0]}`; every clean result reported with a non-zero denominator | §14.9, §9.3 |
| Feature flags | Every feature behind a PostHog flag keyed `{product}.{feature}`, defaulted OFF, two-gate with license entitlement, graceful degradation to the last cached value | §13.5 |
| Licensing model | Node/seat metering unchanged by this design; bundles are not a metered object | — |
| PII tokenization | Single `users` identity table; everything else references by UUID; no PII in logs, spans or metric labels | §11.8, rule V23 |
| Tenant isolation | Tenant from the validated key/claim, never from a request body; tenant middleware before scope checks | §11.8, §10.4 |
| OIDC scopes | Permission checks on scopes, never role names; `intake:write` and `distribution:read` are the two this design adds/uses | §10.1, §11.9 |
| SPIFFE | Every service reserves `spiffe://penguintech.io/<env>/<service>` and is SPIFFE-ready | §11.9 |
| Kubernetes | Default-deny `CiliumNetworkPolicy`, no NodePort/HostPort/hostNetwork in beta/prod, Pod Security Admission `restricted`, Helm only | §12.4, §12.5 |
| Shared libraries | Reusable code lives in `penguin-libs` (`~/code/penguin-libs`), one concern per crate directory, per-crate release branches and independent SemVer | D17, §4.7–§4.11 |
| Dependency pinning | Exact versions in `Cargo.toml`, `Cargo.lock` committed, SHA-256 digests for images and fetched tools, full commit SHAs for GitHub Actions | §12.1, §14.5 |
| Branching | Work off `release/v3.0.X` in feature branches inside worktrees; PR for every merge; release → main is user-gated | §15.1 |
| Docs | Every class and function gets a 2–3 line doc comment; no ASCII-art section dividers | applies to all new code |

---

## 18. Risks & spikes

| # | Risk | Impact | Mitigation | Spike |
|---|---|---|---|---|
| R1 | **`penguin-dal` facade in WASM.** The `waddle-sdk` facade must reproduce the `penguin-dal` public API faithfully — query composition, field/table proxies, pagination, row shape — while lowering every call to a parameterized statement over the WIT `db` import, single-threaded and synchronous underneath. A semantic gap breaks migrated bundles. | High: it is the single component that can falsify "bundles otherwise unchanged". | The bundles' own pytest suites, already green natively after M1.5, are the oracle (§14.4): the same suites must pass through the facade. Any construct the facade cannot lower raises an explicit `NotImplementedError` naming the construct, never silently mis-executes. | **Rounds 1 and 2 complete** — report `spikes/penguin-dal-wasm/REPORT.md`, branch `spike/penguin-dal-wasm`, commits `f45e6578` and `964f2729` (`componentize-py` 0.25.1, `wasmtime` 48.0.x, `wasm-tools` 1.259.0). Round 1: an **unchanged** bundle compiled in 3.4–3.9 s to a 21.6 MB component and the query builder round-tripped correctly at 2–4 ms warm per call; four blockers found. Round 2 **confirmed all three runtime mitigations**: the synchronous `to_thread`/`run_in_executor` shim carried the alias bundle's full `!alias add` write path end to end (4 `db-execute` round trips, 9–18 ms; read-only `!alias foo` 0.7–1.0 ms, no DB call); build-time `pkgutil.walk_packages` pre-import generation resolved the lazy imports; guest `wasi:sockets` use fails cleanly with `PermissionError` while the component keeps running. Round 2 also produced two hard requirements now in the spec: the denying socket interfaces must be **native to the Rust executor** (hand-authored stub components proved impractical), and precompilation must use the **same GC collector** as the runtime engine (`-C collector=drc`; a mismatch fails to load) — precompiled `.cwasm` loads in 4.5–5.4 ms versus 3.3–4.5 s uncached. |
| R2 | **`componentize-py` executes bundle guest code at build time** — **confirmed** in round 2: componentization performs a sandboxed dry-run with the WIT imports trapped, and the compiler's `pkgutil.walk_packages` pre-import deliberately widens that execution to every module in the package. | High for security, medium for compatibility. | The compiler Job's `build` container keeps its rootless, no-network, no-credential isolation (D10) with a two-destination network policy on the `publisher` side and credentials unread until the upload phase — compilation is treated as untrusted-code execution, not as a build step. An optional gVisor `RuntimeClass` may be layered on where available, default off (D32). Bundles whose import-time code needs I/O fail compilation with a clear diagnostic rather than being silently compiled with partial state. | **Complete** — `spike/bundle-compiler-sandbox`, commit `1f1d75a4`, report `spikes/bundle-compiler-sandbox/REPORT.md`. Hermetic builds confirmed for all three Tier 1 languages under the pod-boundary model (Rust 0.40 s, Python 5.52 s, JS 5.13 s), with DNS failing closed; `componentize-py` executes top-level code at build time (confirmed) but its own build sandbox has zero filesystem preopens, so a build-time file write fails — an extra layer, not a replacement for the Job sandbox. Three requirements folded into §4.6, §6.5 and §9.3: the `--stub-wasi` / `--disable all` build flags, the pre-installed pinned `wasm32-wasip1` target (`cargo-component` otherwise tries to fetch it), and the per-language import allowlist. Remaining: compile the full first-party bundle set and record any import-time behaviour changes. |
| R3 | **gVisor availability across Kubernetes distributions.** The original bubblewrap design is dead: a feasibility spike (report at `/tmp/claude-1000/-home-penguin-code-waddlebot/2142d121-d93d-453f-80fa-5e8160d63371/scratchpad/spike-bwrap/REPORT.md` — **legacy identifier**: a scratchpad path predating the rename; 2026-09-14, MicroK8s + containerd 2.1.6) found **11 of 11 test configurations failed**: `bwrap` could not create a namespace inside a rootless container even with `hostUsers: false`, as root, or with `SYS_ADMIN` — the kernel sysctls were correct (`unprivileged_userns_clone=1`, unlimited `max_user_namespaces`) but the runtime refuses `unshare(CLONE_NEW*)` inside containers. gVisor replaces it, which moves the risk to "is `runsc` installable on each cluster". | High: no `runsc`, no default-posture sandbox. | The support matrix (§12.2.1) covers MicroK8s, k3s, minikube, kubeadm, GKE Sandbox, and the managed clouds for operators who want to opt in; the optional installer DaemonSet (§12.2.2) covers the rest with pinned, checksum-verified binaries. `sandbox.gvisor.enabled: false` is now the **default** everywhere (D32), not a fallback, and startup no longer fails closed when gVisor is requested but absent (§12.2). | **Alpha half verified, 2026-09-20 — negative.** On the alpha MicroK8s node (v1.35.6 rev 9072, kernel 7.0.0-31-generic): no `runsc` on `PATH` or in `/usr/local/bin`, `kubectl get runtimeclass` returns none, containerd registers no `runsc` handler, and `gvisor` is **not in the v1.35.6 core addon list** — §12.2.1's `microk8s enable gvisor` path comes from the `community` addon repo, which is neither enabled nor fetched on this node, so that documented path is unconfirmed on this MicroK8s version. Alpha today therefore needs either the §12.2.2 installer DaemonSet or `sandbox.gvisor.enabled: false`. **Both halves now closed, 2026-09-21 — both negative.** `canonical/microk8s-community-addons` has no `gvisor` addon and never has (`addons/` tree and `addons.yaml` checked directly; `kata` is its sandboxed-runtime addon) — §12.2.1's MicroK8s row was conflating MicroK8s with minikube and has been corrected. DOKS cannot host `runsc` at all: DigitalOcean manages worker-node container-daemon configuration and its reconciler reverts node-level changes, so the §12.2.2 installer would not survive a node replacement — which removes gamma and production from the gVisor path entirely. Full findings and sources: `docs/plans/2026-09-21-gvisor-availability-findings.md`. **Consequence:** the default posture is deliverable in no environment we currently run; escalated to §19 Q6 as a decision on D9/D10 rather than a scheduling item. **Closed by D32, 2026-09-21:** gVisor dropped from the default posture, opt-in only; §11 re-rated on the remaining layers (§11.1–§11.3, §12.2). |
| R8 | **gVisor performance overhead on wasmtime.** Syscall-heavy work under a user-space kernel costs latency; the operator-facing figure must be ours, not a vendor's. | Medium: the 3 s text SLA has headroom, but the number drives the opt-in advice. | The opt-in exists precisely because some operators will want the interception; the documented trade-off sentence (§12.2) is qualitative rather than a measured figure. | **Void, 2026-09-21 (D32).** gVisor is opt-in and default off everywhere; no environment Waddles runs retains it to benchmark against, so the M2 benchmark deliverable is removed (§16). |
| R9 | **gVisor version pin versus node kernel updates.** A pinned `runsc` can lag a node kernel update, and the startup `/proc/self/status` marker check is tied to the pinned sentry's field set. | Medium, and now scoped only to clusters that opt in. | The marker check treats an unrecognized-but-clearly-gVisor `/proc/version` as verified and logs at WARN rather than failing; a genuinely absent sandbox no longer fails closed either (D32, §12.2) — it logs and proceeds. `runsc` upgrades are a chart value change with the same pinned-digest discipline as every other tool. | **Void, 2026-09-21 (D32).** gVisor is opt-in and default off everywhere; this drift risk only applies to the subset of clusters that explicitly enable it, and is no longer a scheduled spike. |
| R4 | **wasmtime version pinning versus precompiled artifacts.** A precompiled component is only loadable by the exact engine that produced it; a chart upgrade that changes the engine invalidates every cached artifact at once. | Medium: a slow, thundering-herd recompilation window after an upgrade. | Cache keyed `{digest}-{wasmtime_abi}`; a mismatched artifact is discarded and recompiled, never loaded. Precompilation is measured (`waddles_bundle_load_seconds{phase="precompile"}`), and a rolling upgrade recompiles pod by pod rather than all at once. | Measure cold-start recompilation for the full first-party bundle set and size `EXECUTOR_PRECOMPILE_DIR` accordingly. |
| R5 | **Valkey TLS + ACL rollout.** The live ACL scheme today is a flat set of legacy per-module users with no `svc-*` entries, and the chart's Secret defines only `REDIS_URL`. Turning on TLS and per-service ACL users touches every service at once, hub-api included. | Medium: a misconfigured ACL is an outage, not a degradation. | The chart provisions the ACL file, the CA and both `VALKEY_URL` and `REDIS_URL`; startup refuses a plaintext or unauthenticated URL loudly rather than connecting insecurely by accident; `security.transport.*` gives operators a documented, visible opt-out (D20). | Bring the alpha stack up with TLS + per-service ACLs before M3 starts, so the three parallel services develop against the final configuration. |
| R6 | **Schedule versus the v3.0 MVP.** This lands before the MVP (D2) and is a four-service rewrite plus a new sandbox runtime. | High: it is on the critical path. | M3/M4/M5 are parallel and disjoint by service, so the long pole is `max(M3, M4, M5)` rather than their sum. M1 and M2 are deliberately front-loaded because everything else depends on them. Any slip is visible early: M2's completion criterion (every existing bundle compiling and passing its own tests) is the real schedule signal. | Track M2's bundle-compilation count as the weekly schedule metric. |
| R10 | **hub-api owns consumer-group lifecycle.** Groups are created at activation and destroyed at deactivation/revocation, and re-resolved when an ingest source appears. A hub-api bug or a partial failure leaves a stream with no group (events read by nobody) or an orphan group (PEL growing with no reader). | Medium: silent under-delivery is worse than a loud failure. | Every create is `BUSYGROUP`-tolerant and **idempotent**, and the stage re-issues it on every distribution refresh, so a missing group self-heals within one poll. Orphan groups surface as `waddles_group_pending` growing on a group whose `app_id` is not in any activation — a reconciliation job in hub-api destroys those hourly and logs each one. | Reconciliation covered by an integration test that deletes a group behind the stage's back and asserts recovery. |
| R11 | **PEL growth from a stuck bundle.** A bundle that neither acks nor fails leaves entries pending indefinitely, and the PEL is memory. | Medium, and bounded. | `XAUTOCLAIM` reclaims after `SPINE_CLAIM_IDLE_MS`; `SPINE_MAX_DELIVERIES` sends a repeatedly-failing entry to the DLQ and acks it; the three-strike disable (§7.5) stops the bundle entirely after three trips. `waddles_group_pending` alerts at `SPINE_PEL_ALERT` per group. The isolation is the point: one stuck bundle's PEL never affects another group. | Load test: one deliberately-hanging bundle alongside four healthy ones on the same stream; assert the healthy groups' lag stays flat and the stuck one is disabled. |
| R12 | **Valkey memory for `MAXLEN`.** Streams retain entries until trimmed, so memory is roughly `sources × SPINE_STREAM_MAXLEN × entry size`, where the list design retained only undelivered work. | Medium: a sizing question, not a correctness one. | `MAXLEN ~ 100000` per source is the default and is a chart value; the e2e run records bytes per entry so the figure is measured, not guessed, and the chart docs carry the per-source estimate. Trimming is counted, and `maxmemory-policy` on the Valkey deployment is documented as `noeviction` so a memory ceiling fails loudly rather than silently dropping keys. | Measure entry size and per-source memory during the alpha e2e run; publish the sizing table. |
| R7 | **At-least-once changes bundle assumptions.** A bundle that was accidentally relying on at-most-once can now double-apply an effect. | Medium. | Idempotency review of every first-party bundle is an M2 deliverable; `waddles_stream_claimed_total` makes redelivery visible; `SPINE_MAX_DELIVERIES` bounds the blast radius. | Covered by the M2 review. |

---

## 19. Open questions

Only items genuinely undecidable from the approved design are listed. Each names who can decide it and what the spec does in the meantime.

| # | Question | Interim behaviour in this spec |
|---|---|---|
| Q1 | **Operator re-enable for a trip-disabled bundle.** The approved text fixes the three-strike disable but not how an operator clears it without waiting for a new version or a pod restart. Should hub-api expose an explicit re-enable action, and at what scope (pod, deployment, cluster)? | A disabled bundle re-enables only on a new `artifactDigest` or a pod restart (§7.5). No runtime switch exists. |
| Q2 | **The `presentation` surface's long-term home.** `bundle.yaml` v2 still accepts `presentation`, served client-side by `svc_presentation`, which is outside this rewrite. Does it eventually become a WASM stage, stay a static asset surface, or move entirely into the webui? | Accepted in the manifest, never compiled to WASM, unchanged behaviour. |
| Q3 | **Per-bundle Postgres role lifecycle at scale.** Roles are created at approval and dropped a week after the last activation, but the ceiling on concurrent roles, the password-rotation cadence, and the cleanup job's ownership are not settled. | Create at approval, drop after `BUNDLE_ROLE_GRACE_H = 168`; rotation is manual. |
| Q4 | **`waddle-sdk` distribution.** Public PyPI (so third-party authors can `pip install waddle-sdk` and test locally) or an internal index only? Public publication also publishes the WIT world's shape. | Built and versioned in-repo; publication target deferred. Tier 1 authors use the in-repo package and the compiler recipe. |
| Q5 | **Gamma/production bucket provider.** MinIO is the alpha default and Nest is "when configured", but which backs gamma and production on DigitalOcean — and whether that is Nest, DigitalOcean Spaces, or MinIO in-cluster — is not settled. | `bundles.bucket.provider` accepts `minio` or `nest`; alpha and beta use MinIO. |
| Q7 | **Should a Rust `penguin-dal` exist?** (Raised 2026-09-21.) This spec answers "no" twice: the Rust services use **SeaORM** (G1, §4), and `waddle-sdk-rs` is explicitly "thinner — idiomatic bindings over the same WIT world, with no compatibility obligation to a prior API" (§6.5). penguin-dal appears only in the **Python** facade (D21), and only because ~16 existing Python bundles must keep working unchanged — a constraint Rust bundles do not have. The open question is org-wide rather than local: `penguin-libs` ships `python-dal` and `go-dal` but no `rust-dal`, so Rust services across PenguinTech get SeaORM without the house conventions (PII tokenization, per-service accounts, redaction-on-repr). Reversing this spec's answer would also require re-rating the executor's dependency-set control, which §14.6 asserts by test (`No redis, no sea-orm, no sqlx`). | Unchanged: SeaORM in the services, no DAL crate in the executor, penguin-dal's API only in the Python `waddle-sdk` facade. A `rust-dal` crate, if wanted, is a `penguin-libs` decision that does not block any milestone here. |

---

## 20. Assumptions

Where the approved design left a detail open, the option most consistent with the approved text was chosen. Each is listed here so a reviewer can overturn it cheaply.

| # | Assumption | Why this option |
|---|---|---|
| A1 | **One WIT world (`stage`) exporting both stage interfaces**, with an auto-generated stub returning `unsupported-stage` for the stage a bundle does not implement. | The approved text names a single world, `waddle:bundle/stage@1.0.0`, with both exports. WIT worlds require all exports to be present, so a stub is the only way to have one world and single-stage bundles. |
| A2 | **Open-ended JSON crosses the WIT boundary as canonical JSON text** (`payload-json`, `config-json`, `message-json`, `fields-json`). | WIT has no dynamic value type; the alternative (a hand-rolled variant tree) would change the shape bundles see, violating D7. |
| A3 | **Executor frames are `u32` big-endian length + UTF-8 JSON, bidirectional with correlation ids**, carried over one mTLS connection per stage; the executor dials, and host calls flow executor→stage on the same connection. | The approved text fixes a length-prefixed message protocol and a credential-less executor; moving the executor to its own Deployment replaced the Unix socket with an mTLS port, and the host capabilities remain stage-side, which makes the connection bidirectional. JSON keeps golden fixtures trivial at chat volumes. |
| A4 | **Stream trimming is counted, not dead-lettered.** `MAXLEN ~` evictions increment `waddles_stream_trimmed_total`; they do not produce DLQ records. *(Supersedes the list design's queue-overflow→DLQ rule: under Streams an entry can be trimmed while several groups have already consumed it, so a DLQ record would misreport a successful delivery as a loss. The signal that a consumer actually fell behind is its group lag, which is alerted on separately.)* | The approved text pairs bounding with a metric; the DLQ is reserved for events a bundle accepted and then failed. |
| A5 | **Hook assignment** (§4.2): the moderation gate, moderation-enforcement routing and cross-app `_target_app_id` routing become Rust built-ins; `bot_process`, raid shoutout, live status, activity feed and reputation accrual become bundles. | The approved text fixes the rule ("bundles or Rust built-ins, no third path") but not each hook. Hooks that must run for every event regardless of activation, or that manipulate envelope routing, are stage behaviour; flag-gated per-feature behaviour is an App. |
| A6 | **The generic-intake mapping uses RFC 6901 JSON Pointers**, with `$now` as the only magic default. | "Declarative JSON→`PlatformEvent` mapping" rules out expressions; JSON Pointer is the smallest standard that addresses nested bodies. |
| A7 | **A trip-disabled bundle re-enables on a new digest or a pod restart only.** | The approved text specifies disable + DLQ + alert and no re-enable mechanism; adding an API would be a new feature (recorded as Q1 instead). |
| A8 | **Twitch EventSub webhook and websocket are mutually exclusive per tenant**, selected by `TWITCH_EVENTSUB_MODE`. | The approved text calls the websocket "a per-tenant alternative to the webhook"; running both would double-deliver every event. |
| A9 | **Drain cadence is a 100 ms idle sleep with immediate re-run while messages flow.** | The approved text fixes only the 5 s bundle-set refresh and the 3 s/5 s SLA; a value was needed to make the SLA arithmetic checkable. 100 ms leaves ≥ 90 % of the text budget for real work. |
| A10 | **Flag keys live in the always-on `core` module** (`waddles.core.rust-data-plane`, etc.) at `min_tier: free`. | `{product}.{feature}` plus the `FeatureContract` rule that a flag equals `waddles.{module}.{feature}`; `core` is the existing always-deployed namespace and these are core-product capabilities, not licensed ones. |
| A11 | **`trace_context` is an optional envelope field**, absent or null deserializing to none, exactly like `target_app_id`. *(Superseded by D30, review 3: the single string field becomes the optional `trace` object, `{traceparent, tracestate}`, §5.11/§6.1.2 — the field is renamed and gains `tracestate`, but stays optional and absent-safe.)* | The approved text adds the field for cross-stage spans; making it optional keeps every existing envelope valid. |
| A12 | **Artifact layout** `bundles/{app_id}/{version}/{sha256}.wasm` plus a `{sha256}.json` Ed25519-signed sidecar. | The approved text fixes content addressing, a signed bucket sidecar and a deploy key; the path shape is the smallest scheme that is both content-addressed and human-navigable. |
| A13 | **Per-bundle Postgres role naming** `bundle_<app_id with dots and dashes replaced by underscores>`. | The approved text fixes "a per-bundle Postgres role limited to manifest `data.tables`" but not the name; a deterministic derivation avoids a lookup table. |
| A14 | **`db` statements are parsed and table-checked before execution, in addition to the role and RLS.** | The approved text fixes "parameterized SQL executed by the stage under a per-bundle role limited to manifest `data.tables`"; a parser check is how the stage enforces the table list itself rather than relying solely on grants. |
| A15 | **`wasi:random` is permitted** inside the guest; every other WASI interface beyond the read-only `/scratch` preopen is excluded. | The approved text excludes `wasi:sockets`, `wasi:filesystem` beyond a read-only scratch, and environment access, and is silent on randomness; excluding randomness would break ordinary bundles (shuffles, giveaways) for no stated security gain. |
| A16 | **Executor-side deadline plus a stage-side backstop timer** at `deadline_ms + 250 ms`. | The approved text fixes a per-call epoch deadline; a wedged executor cannot enforce its own, so the stage needs a backstop for the deadline to be a real guarantee. |
| A17 | **Intake delivery-id de-duplication** (`409`, a 900 s seen-set) alongside the replay window. | The approved text fixes a replay window but not duplicate suppression; platforms retry webhooks routinely, and at-least-once downstream makes a duplicate at the edge avoidable rather than unavoidable. Removing it costs only the `409` row in §10.1. |
| A18 | **M1.5 (the bundle DAL migration) runs parallel with M1 and gates M2's compiler work**, rather than sitting inside M2. | The approved text offered "in M2 before the compiler work, or as M1.5 — your call". A separate milestone makes the dependency explicit and lets pure-Python work proceed while the Rust crates are being built; it is also the honest schedule signal, since nothing can compile until it lands. |
| A19 | **Denying `wasi:sockets` implementations are part of the host, and V31's per-language allowlist permits the imports when the executor supplies them.** | The spike showed `componentize-py`'s runtime will not instantiate without the full WASI P2 import set, so a blanket ban would exclude the Python tier entirely. Round 2 further showed hand-authored stub components impractical, so the Rust executor implements the refusing interfaces itself. Either way the actual guarantee holds — no bundle opens a socket — while the toolchain still links. **No longer an assumption: verified in round 2.** |
| A20 | **`asyncio.to_thread` and `loop.run_in_executor` are replaced with synchronous, already-completed-awaitable shims** rather than a real executor. | WASI has no thread pool; the bundles' uses are all immediately awaited, so inline execution is semantically equivalent for them. A bundle that genuinely needed background concurrency would behave differently, which is why the change is documented in the authoring guide rather than hidden. **No longer an assumption: round 2 ran the alias bundle's full write path through the shim unchanged.** |
