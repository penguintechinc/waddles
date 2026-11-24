# Marketplace Sources Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a global admin (`platform:admin`) add git "marketplace sources" whose `bundles/{lang}/{app}/` trees are scanned, validated, built or trusted, and fed through the existing publish -> human-approve -> platform-Ed25519-sign -> install pipeline; fold the waddles repo in as the `is_builtin` source.

**Architecture:** A new `marketplace_sources` table + `source_id` provenance on `app_versions`/`app_catalog`. A hub-side scanner (egress-proxy clone, pinned SHA, guards, manifest validation, `src-{id}` namespace rewrite, PRC-dep denylist) emits `ScanResult`s into the unchanged `process_prebuilt_component()` -> approve -> `sign_and_record_version()` -> `install_version_globally()` path. Prebuilt sources attest with a registered publisher key; sandbox sources compile in a rootless, no-egress K8s Job. A CronJob re-scans and only ever records `pending_commit_sha`; advancing the pin is human-gated.

**Tech Stack:** Python 3.13 + Quart + penguin-dal + alembic (hub_api), quart-schema/OpenAPI, PostHog flags via `flask_core.feature_flags.feature_enabled`, Rust (egress-proxy allowlist config only; builder-job orchestration is Python + Helm/k8s manifests), Helm chart `k8s/helm/waddlebot`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-27-unified-bundle-marketplace-sources-design.md` (sections 4.1-4.8, 6 Phase 1, 7). Phase 2 (vendor/payments) is OUT of scope; do not add `vendor_id`.

**Base:** fresh worktree off the current release tip (`origin/release/v3.0.X`); re-verify every path/line below with Read/Grep first. Alembic head today is `0061_port_legacy_sql_103_codec.py`; chain the new revision after the graph head at build time (ids <=32 chars, heads==1, explicit downgrade targets in tests).

## Global Constraints

- **DB:** penguin-dal for all runtime ops; alembic for schema only. Parameterized queries only. Note the sync-worker trap: CronJob mains must use `AsyncDAL`, not read `.dal` off `AsyncDB` (memory: sync-worker-dal-asyncdb-vs-asyncdal).
- **Entity identity:** BIGINT surrogate PK internal only; `public_id` UUID in API bodies, URLs, logs, metrics, traces; names/URLs are display aliases never references; status/build_mode as int enums.
- **Flag + license:** every endpoint behind PostHog flag `waddles.marketplace-sources` (default OFF); tier via `get_tier()` at platform scope: prebuilt external = Professional, sandbox = Enterprise (also enforced server-side at ack + rescan + auto-build); built-in source never gated, with opt-out kill-switch `waddles.disable-builtin-source`. Domain-bypass only, never env/CLI bypass.
- **OTel + logging:** logs, metrics, AND traces per scan/build/approve; penguin logging only (no `print`/`logging.basicConfig`); logs carry `public_id` + counts, never raw git URLs with creds, never PII/usernames/manifest free text. Counts of bundles examined/rejected reported; zero examined = FAIL.
- **Coverage:** >=90% on touched code; malicious-source suite + flag-on E2E are blocking.
- **No-stubs / fail-loud:** no silent fallbacks (unvendored build fails loudly; yaml entry with no scanned bundle fails loud); the `check-no-stubs` gate must pass; no `|| true` gates.
- **Signing unchanged:** executor trusts platform keys only (`core/bundle_executor/src/signing.rs` untouched). Publisher signature is an attestation; platform re-signs at approval; signing payload `app_id, version, digest, approval_id` unchanged. Builder holds no signing key, no DB/S3 creds.
- **Containers:** rootless (`runAsNonRoot`, `USER appuser`), read-only root FS, stateless (`emptyDir` only), PSA `restricted`, Tetragon allowlist, CiliumNetworkPolicy default-deny egress incl. DNS for builder.
- **Git egress:** clone only via the egress-proxy with host allowlist; `url_guard` is a pre-check only (TOCTOU/rebind); no git credentials, https public repos only, no submodules/LFS/hooks.
- **Supply chain:** PRC/sanctioned-dependency denylist (central list, not per-source) rejects bundles; pinned toolchains (reuse `bundles/Dockerfile.core-bundles` digest pins).
- **Security:** SSRF, zip-bomb, symlink, traversal, size/count/depth/time caps; `core.*` reserved to `is_builtin`; SYSTEM install_source only for built-in; never a silent install or update from an external source.
- **Output validation:** every response through `@validate_response` DTOs, no raw rows; exact-field-set regression test.
- **Branching:** one PR per task (or small stack), feature branches off the release branch; no direct merges.

## Review Focus

1. **Malicious repo:** traversal/symlink-escape/zip-bomb/huge-file/deep-path/manifest claiming `penguintechinc.*` or another source's prefix; hostile `bundle.yaml`; dependency manifests naming denylisted packages. Reviewer confirms each is a hard reject with a logged reason and a test.
2. **DNS rebind / SSRF:** pre-check passes then host resolves internal; clone must be impossible except through the egress-proxy allowlist (network policy denies direct git egress from hub). Reviewer verifies the policy, not just the Python check.
3. **Namespace shadowing:** `core.*` valid iff `source.is_builtin`; externals forced to `penguintechinc.waddles.src-{short}-{app}`; cross-source collision impossible; SYSTEM install asserts `is_builtin`; seeder-only permission grants stay builtin-only.
4. **Oversized/hostile build:** sandbox caps (CPU/mem/ephemeral-storage, `activeDeadlineSeconds`, source+output size, single componentized `.wasm`), unvendored deps fail loud, no egress.
5. **Unapproved auto-update:** a new commit must never move `pinned_commit_sha`, never reach INSTALLED, never replace the running approved version; force-push behind the pin sets `error`; changing `git_url`/`build_mode` voids the sandbox ack; a same-version/different-digest is refused.

---

### Task 1: `marketplace_sources` table, migration, DAL model, shared enums

**Files:**
- Create: `alembic/versions/0062_marketplace_sources.py` (name/number set from graph head)
- Create: `hub_api/services/marketplace_source_service.py` (model helpers + enums; CRUD logic lives here, blueprint stays thin)
- Create: `hub_api/tests/test_marketplace_sources_migration.py`, `hub_api/tests/test_marketplace_source_service.py`
- Modify: the DAL table registration module that defines `app_versions`/`app_catalog` (locate via grep for `app_versions` in `hub_api/services/schema.py`) to register `marketplace_sources` and the new `source_id` columns.

**Interfaces:**
- Produces: `class BuildMode(IntEnum): PREBUILT=1, SANDBOX=2, BUILTIN_IMAGE=3`; `class SourceStatus(IntEnum): PENDING_SCAN, ACTIVE, NEEDS_APPROVAL, DISABLED, ERROR`; `@dataclass(slots=True, frozen=True) SourceRecord(public_id, name, is_builtin, git_url, ref, pinned_commit_sha, build_mode, publisher_key_fingerprint, publisher_public_key, sandbox_ack_*, status, last_scanned_at, last_scan_error, pending_commit_sha, created_by, ...)`; `async create_source(...) / get_source(public_id) / list_sources() / disable_source(public_id)`.
- Table per design 4.1; CHECK constraints: builtin row has NULL `git_url`; `build_mode=SANDBOX` requires ack columns non-null; `build_mode=PREBUILT` requires publisher key columns; `pinned_commit_sha` ~ `^[0-9a-f]{40}$`; https-only `git_url`; exactly one `is_builtin` (partial unique index). Seed the builtin sentinel row in the migration. `source_id` nullable FK on `app_versions` + `app_catalog`, backfill `penguintechinc.waddles.*` rows to the builtin row. Audit trigger mirroring `app_versions_audit_log` (`069_app_bundle_tiers.sql:21-58`, alembic `0023`). Builtin immutable: trigger/guard blocks update of url/mode and delete.

- [ ] **Step 1: Failing tests** - migration up/down on a fresh DB AND on an existing DB with pre-populated `app_versions` rows (backfill assertion; existing-DB recipe from #806); single-head assertion; CHECK constraint rejections (non-https, bad SHA, sandbox without ack, prebuilt without key, second builtin); builtin delete/edit refused; audit row written on insert/update.
- [ ] **Step 2:** Run, confirm failures.
- [ ] **Step 3: Implement** migration + service helpers + DAL registration.
- [ ] **Step 4:** Tests pass; `make lint` clean; verify `downgrade` explicit target.
- [ ] **Step 5: Commit.**

---

### Task 2: Namespace + install-source gates (shared guards)

**Files:**
- Modify: `hub_api/services/vendor_bundle_authz.py` (add `source_namespace_prefix(source_short)` + `enforce_source_namespace(app_id, source)` beside `enforce_vendor_namespace` `:127`; do NOT widen `_APP_ID_RE`)
- Modify: `hub_api/services/bundle_approval_service.py` (`install_version_globally` `:444`: assert `install_source == SYSTEM` requires the version's source `is_builtin`)
- Modify: `hub_api/cli/seed_core_bundles.py` (`_guard_core_namespace` `:420` delegates to the shared gate)
- Create: `hub_api/tests/test_source_namespace_gate.py`

**Interfaces:**
- Consumes: `SourceRecord` (Task 1). Produces: `enforce_source_namespace(*, app_id: str, source: SourceRecord) -> None` raising on `core.*` for non-builtin, on another source's prefix, or on a non-`src-{short}` feature segment; `short = first 12 hex of public_id`, collision-checked at source creation.
- Existing seeder tests (~1700 lines) must stay green unchanged.

- [ ] **Step 1: Failing tests** - external claiming `penguintechinc.waddles.x-y` rejected; claiming `src-<other>` rejected; builtin accepts `core.*`; external cannot be installed with SYSTEM actor; `_APP_ID_RE` unchanged (regression test).
- [ ] **Step 2-5:** run failing, implement, pass (run the full existing seeder suite), commit.

---

### Task 3: Scanner core - safe clone via egress-proxy + guards + walk + validate

**Files:**
- Create: `hub_api/services/marketplace_source_scanner.py`, `hub_api/services/marketplace_source_guards.py` (archive/tree guards), `hub_api/services/dependency_denylist.py` + `config/supply-chain-denylist.yaml`
- Modify: `hub_api/services/url_guard.py` only if a `git`-scheme-aware pre-check wrapper is needed (reuse `validate_outbound_url(..., allowed_schemes=("https",))` `:196`)
- Modify: egress-proxy allowlist config (locate in `core/` egress-proxy crate + `k8s/helm/waddlebot/templates`): add a git-fetch allowlist route for sources; and a CiliumNetworkPolicy denying direct hub git egress.
- Create: `hub_api/tests/test_marketplace_source_scanner.py`, `hub_api/tests/fixtures/malicious_sources/` (builder helpers that construct hostile repos in tmp dirs; never commit real exploit binaries)

**Interfaces:**
- Produces: `@dataclass(slots=True, frozen=True) ScanResult(source_public_id, commit_sha, bundles: tuple[BundleCandidate, ...], rejected: tuple[RejectedBundle, ...], examined: int)`; `BundleCandidate(language, dir_name, assigned_app_id, version, manifest, wasm_path|None, sidecar_digest|None, publisher_sig|None, permission_summary, dep_findings)`; `async scan_source(source: SourceRecord, *, workdir: Path) -> ScanResult`; `GuardLimits` frozen dataclass (max bytes/files/file size/depth/timeout from config).
- Consumes: `parse_bundle_manifest_v2` (`bundle_manifest_v2.py:399`), `enforce_source_namespace` (Task 2), `get_permission_summary` (`bundle_approval_service.py:315`).
- Behavior: validate URL (pre-check) -> git subprocess (no shell, `--depth 1`, `GIT_TERMINAL_PROMPT=0`, no submodules/LFS/hooks, `protocol.allow=never` except https, proxy env pointing at egress-proxy, wall-clock timeout) -> verify HEAD == resolved SHA -> guards -> walk only `bundles/{python,rust,csharp,javascript}/*/` requiring `bundle.yaml`+`hub-manifest.yaml` -> validate manifest (reject, don't repair; semver version) -> rewrite `app_id` to the assigned `src-{short}` id (hard reject if manifest claims `core.*`/other prefix) -> dependency denylist over `pyproject.toml`, `Cargo.toml/lock`, `*.csproj`, `package.json/lock`. A guard violation rejects the whole scan.

- [ ] **Step 1: Failing malicious-source suite (owns Review Focus 1, 2, 3):** path traversal, symlink escaping root, oversized clone, file-count/depth/per-file bombs, SHA mismatch (checked-out != resolved), `core.*` shadow, other-source prefix, non-semver, malformed manifest, denylisted dependency in each of 4 ecosystems, SSRF hosts (loopback, link-local, RFC1918, metadata IP, DNS name resolving to private), non-https scheme, `file://`/`ssh://`, redirect to disallowed host; git subprocess invoked with no shell and sanitized env (assert argv/env); examined/rejected counts correct; zero bundles examined => error.
- [ ] **Step 2-5:** failing, implement, pass, commit. Local clone tests use a local bare repo fixture through a test-only transport seam that is NOT reachable in prod config (assert the seam is disabled unless an explicit test flag); real-path network clone covered in Task 11.

---

### Task 4: Publisher-key registration + attestation verification (prebuilt)

**Files:**
- Create: `hub_api/services/marketplace_publisher_attestation.py`, `hub_api/tests/test_marketplace_publisher_attestation.py`
- Modify: `hub_api/services/marketplace_source_service.py` (key fingerprint computation/storage, required for PREBUILT)
- Reuse (read-only): `hub_api/services/bundle_signing_service.py` Ed25519 helpers; do not write new crypto (penguin-crypto/cryptography lib only).

**Interfaces:**
- Produces: `fingerprint_publisher_key(pubkey_b64: str) -> str`; `verify_prebuilt_attestation(candidate: BundleCandidate, source: SourceRecord, wasm_bytes: bytes) -> VerifiedPrebuilt` (recomputes sha256, compares to sidecar digest, verifies detached Ed25519 sig over `(app_id, version, digest)` against the source's registered key; raises `AttestationError` on missing/invalid/mismatch). The signed message binds the *assigned* `src-` app_id so a signature cannot be replayed across sources/names.

- [ ] **Step 1: Failing tests** - valid sig passes; wrong key, tampered wasm, digest mismatch, missing sig, signature for different app_id/version, key-less source all rejected; replay across two sources rejected.
- [ ] **Step 2-5:** implement, pass, commit.

---

### Task 5: Prebuilt path into the existing pipeline (approve -> re-sign -> install)

**Files:**
- Create: `hub_api/services/marketplace_source_ingest.py` (turns attested candidates into `app_versions` rows with `source_id`), `hub_api/tests/test_marketplace_source_ingest.py`
- Modify: `hub_api/services/bundle_version_service.py` (`create_version` `:209` + `process_prebuilt_component` `:517`: accept/persist `source_id`; refuse same-version/different-digest)
- Modify: `hub_api/services/bundle_approval_service.py` (external versions only ever installed with `install_source="human"`, `installed_by=<admin>`; no auto-grant of permissions for non-builtin)

**Interfaces:**
- Produces: `async ingest_candidates(install_dal, source, result: ScanResult, *, fetch_wasm) -> IngestSummary` (counts: created, skipped-identical, refused-conflict). Versions land PUBLISHED, scan_status not_scanned, never approved/signed/installed here.
- Reuses unchanged: `/bundle_approvals` approve (`bundle_approvals.py:133`), `sign_and_record_version`, `install_version_globally`, tenant `make_available`, community activation.

- [ ] **Step 1: Failing tests** - end-to-end against real test DB: ingest -> version PUBLISHED with `source_id`; not installed; approve (platform:admin) -> signed by platform key -> installed `install_source=human`; executor-verifiable signature payload unchanged; same-version/different-digest refused; permission summary + `classify_diff` surfaced; external bundle gets zero auto-grants.
- [ ] **Step 2-5:** implement, pass, commit.

---

### Task 6: Source CRUD API (flag, tier gate, OpenAPI, rate limit)

**Files:**
- Create: `hub_api/blueprints/v1/marketplace_sources.py`, `hub_api/tests/test_v1_marketplace_sources_blueprint.py`
- Modify: blueprint registration module for v1 blueprints; `openapi/v{major}.yaml` (spectral clean); `hub_api/services/marketplace_source_service.py` (orchestrate add/remove/rescan/approve-update)

**Interfaces:**
- Routes (all `@require_scope("platform:admin")`, flag `waddles.marketplace-sources`, penguin-limiter, `@validate_request`/`@validate_response`): `POST /marketplace-sources`, `GET /marketplace-sources`, `GET /marketplace-sources/<public_id>`, `DELETE /marketplace-sources/<public_id>` (disables source + *deactivates* its bundles, never deletes versions; builtin -> 409/refused), `POST .../rescan`, `POST .../approve-update`.
- DTOs (slotted dataclass/pydantic): `SourceCreateRequest(name, git_url, ref, build_mode, publisher_public_key?, sandbox_trust_ack?)`, `SourceDTO` (public_id only, no numeric ids, no raw key private material), `ScanSummaryDTO`.
- Gate: flag off -> 404; PREBUILT requires Pro, SANDBOX requires Enterprise via `get_tier()` (server-side, regardless of UI); sandbox add requires `sandbox_trust_ack` equal to the hash of the current versioned warning text (design 4.8) else 422; ack stores user, time, SHA-at-ack, text hash and audit-logs; `git_url` or `build_mode` edit voids ack. Add runs initial scan asynchronously (status PENDING_SCAN).

- [ ] **Step 1: Failing tests** - flag off/on, missing/insufficient scope, tenant-scoped admin refused, tier matrix (Free/Pro/Ent x prebuilt/sandbox), stale/missing ack 422, builtin delete refused, DTO exact field set (no numeric id leakage), rate-limit, SSRF-host add rejected at API layer, OpenAPI spectral lint.
- [ ] **Step 2-5:** implement, pass, commit.

---

### Task 7: Sandbox builder (K8s Job orchestrator + builder images + policies)  [Python + Helm/k8s; builder images wrap existing Rust/py/dotnet toolchains]

**Files:**
- Create: `hub_api/services/marketplace_sandbox_builder.py` (Job spec builder + hand-off), `hub_api/tests/test_marketplace_sandbox_builder.py`
- Create: `bundles/Dockerfile.sandbox-builder` (generic per-language builder targets reusing the digest-pinned stages from `bundles/Dockerfile.core-bundles:18-34,106,170` and `bundles/build_python_bundles.py`; `USER appuser`; `SOURCE_DATE_EPOCH` reproducibility flags reused; offline entrypoint that fails loudly on missing vendored deps)
- Create: `k8s/helm/waddlebot/templates/marketplace-sandbox-builder.yaml` (RBAC for hub to create Jobs in a dedicated namespace, ResourceQuota, PSA `restricted`, CiliumNetworkPolicy default-deny egress+DNS, Tetragon TracingPolicy allowlist, no service-account token mounted into the Job); extend `k8s/helm/waddlebot/values.yaml`
- Modify: `.github/workflows` builder-image build/digest-pin workflow, parallel to `core-bundles-reproducible.yml`

**Interfaces:**
- Produces: `async run_sandbox_build(source: SourceRecord, candidate: BundleCandidate, source_tarball: Path) -> BuildArtifact(wasm_bytes_ref, sha256, reproducible: bool|None, duration_s)`; Job spec: `runAsNonRoot`, read-only root FS, `emptyDir` scratch only, CPU/mem/ephemeral-storage limits, `activeDeadlineSeconds`, `automountServiceAccountToken: false`, no secrets/env creds. Hand-off decision for implementer: tarball in via `emptyDir`-populating initContainer from the hub-served object-store presigned (internal) URL vs hub-attached volume; choose the option with no builder DB/S3 credentials, document in the PR. Output validated: single componentized `.wasm`, size cap, hub recomputes sha256 (never trusts builder-reported digest). Result feeds Task 5 ingest. If repo also commits a prebuilt artifact and digests match, mark "reproducible".
- Enterprise-only, enforced here too (defense in depth). Orchestrator refuses to run for a source whose ack is missing/voided.

- [ ] **Step 1: Failing tests** - Job manifest assertions (securityContext, no egress CNP, limits, deadline, no SA token, no secret mounts); unvendored deps -> loud failure; oversize source/output rejected; builder-reported digest ignored in favor of recomputed; non-Enterprise refused; voided ack refused; cannot approve/sign (no key reachable). Real-cluster test in Task 11.
- [ ] **Step 2-5:** implement, `helm template` + `kubeconform`/lint clean, pass, commit.

---

### Task 8: Auto-update CronJob (re-scan, `pending_commit_sha`, notify, re-approval)

**Files:**
- Create: `hub_api/cli/marketplace_source_rescan.py` (CronJob main; `AsyncDAL` per the sync-worker trap), `hub_api/services/marketplace_source_update.py`, `k8s/helm/waddlebot/templates/marketplace-source-rescan-cronjob.yaml`, `hub_api/tests/test_marketplace_source_update.py`
- Modify: `hub_api/services/marketplace_source_service.py` (`approve_update`), notification hook (reuse the existing platform-admin notification mechanism; grep the existing audit/notification services rather than build a new one)

**Interfaces:**
- Produces: `async check_source_for_update(source) -> UpdateOutcome` (UNCHANGED | PENDING | HISTORY_REWRITTEN | ERROR); `async approve_update(source_public_id, *, admin_id) -> SourceRecord` (the ONLY place `pinned_commit_sha` advances, after the new versions are approved; plus per-version approve).
- Behavior: re-resolve `ref`; new commit -> record `pending_commit_sha`, scan+build in the background, ingest new semver versions as PUBLISHED, status NEEDS_APPROVAL, notify admins with `classify_diff` (permission expansion highlighted); the currently approved pinned version keeps running; SHA unreachable (force-push) -> status ERROR, pinned bundles untouched, admin alerted; sandbox auto-build only if ack valid and tier still Enterprise (tier downgrade stops builds, never uninstalls); cadence/backoff/jitter via values (implementer picks defaults, document).

- [ ] **Step 1: Failing tests (owns Review Focus 5):** new commit never moves the pin; never installed without approval; running version unaffected; force-push -> ERROR; same-version/different-digest refused; git_url/build_mode edit voids ack and blocks auto-build; notification emitted exactly once per pending commit; permission widening flagged; job idempotent on re-run; per-source failure isolated and counted.
- [ ] **Step 2-5:** implement, pass, commit.

---

### Task 9: First-party unification (`is_builtin` source, overlay, reserved `core.*`)

**Files:**
- Modify: `hub_api/cli/seed_core_bundles.py` (run as built-in source adapter: builtin row, `build_mode=BUILTIN_IMAGE`, pinned SHA = baked image commit; `core-bundles.yaml` becomes activation-policy overlay; `reconcile_removed_core_bundles` `:1063` scoped by `source_id = builtin`; stamp `source_id` on rows; yaml entry with no scanned/real bundle fails loud; SYSTEM actor `:113` builtin-only)
- Modify: `bundles/core-bundles.yaml` only if loader contract needs an explicit `source: builtin` key (additive)
- Modify: flag read for kill-switch `waddles.disable-builtin-source` (unseen/OFF = mechanism on)
- Create: `hub_api/tests/test_seed_core_bundles_builtin_source.py`

**Interfaces:**
- Consumes: Task 1 builtin row, Task 2 gates, scanner discovery (Task 3) in a builtin-trust mode. Per spec 4.6: literally calling the shared scanner is done behind the kill-switch; default path must be behavior-identical for existing alpha/beta deploys (existing ~1700-line seeder suite green and unmodified).

- [ ] **Step 1: Failing tests** - seeding stamps `source_id=builtin`; backfill idempotent; yaml/scan mismatch fails loud; `core.*` only valid for builtin; reconcile never touches external-source rows or human-approved rows; kill-switch ON restores legacy path; existing suite untouched.
- [ ] **Step 2-5:** implement, run FULL existing seeder suite, commit.

---

### Task 10: Observability + audit + docs

**Files:**
- Create/Modify: `hub_api/services/marketplace_source_telemetry.py` (OTel metrics/spans: scan count, bundles examined/rejected, build duration histogram, pending-update gauge; OTLP from `OTEL_*` env only), audit events in the service layer for add/remove/rescan/ack text hash/scan outcome counts/pending update/approve/deny/signature issued
- Create: `docs/marketplace-sources.md` (admin guide + trust-warning text versioning), `hub_api/README` section or per-feature README, update human docs index; `openapi/v{major}.yaml` finalize
- Create: `hub_api/tests/test_marketplace_source_telemetry.py`

- [ ] **Step 1: Failing tests** - scan emits >=1 log, >=1 metric point, >=1 histogram, spans with `public_id` only; assert no raw URL creds/PII/usernames in log bodies/labels; zero-examined path emits failure metric; exact audit rows written per action; telemetry exporter failure never fails a request.
- [ ] **Step 2-5:** implement, pass, commit.

---

### Task 11: Flag-on end-to-end + real-path security regression

**Files:**
- Create: `tests/api/test_marketplace_sources_e2e.py` (or the repo's e2e location; align with the existing alpha-smoke layout), `scripts/` helper to stand up a local fixture git server reachable only via the egress-proxy, `hub_api/tests/test_marketplace_sources_security_regression.py`
- Modify: smoke script / Makefile target so the e2e is a gated target (no `|| true`)

- [ ] **Step 1:** Flag-ON, real path, counts asserted, covering: add prebuilt source (valid publisher signature) -> scan via the real egress-proxy -> approve -> platform re-sign -> install -> tenant make_available -> community activate -> executor really loads and runs it (exercises real signature verification); add sandbox source (Enterprise tier) -> real Job built in cluster (no egress verified by an attempted-egress negative fixture) -> approve -> install -> execute; auto-update: push new commit to fixture -> pending, NOT installed, admin notified, approve-update advances; force-push -> ERROR; hostile fixture repos (Review Focus 1-4) rejected end-to-end including direct-git-egress-denied under CNP and DNS-rebind fixture; flag OFF -> 404; builtin source present and `core.*` shadow attempt rejected.
- [ ] **Step 2:** Mutation check: temporarily disable each gate (SHA pin, namespace gate, ack check, tier gate, attestation verify) and confirm the matching test fails; record in PR.
- [ ] **Step 3:** run the full no-stubs gate, `make lint`, `make test-security`, coverage >=90%; commit.

---

## Self-Review (design section 6 coverage)

| Design Phase-1 item | Task |
|---|---|
| 1 table + migration + `source_id` + audit trigger | 1 |
| 2 CRUD API + flag + tier gates + OpenAPI | 6 (gates assert again in 7, 8) |
| 3 clone via egress-proxy + scanner + guards + namespace + denylist | 3 (+2 gates) |
| 4 publisher-key + signature verify | 4 |
| 5 prebuilt -> approve -> re-sign -> install | 5 |
| 6 sandbox builder + ack (Enterprise) | 7 (+6 ack) |
| 7 auto-update | 8 |
| 8 built-in unification | 9 (+2) |
| 9 tests, docs, OTel/audit | per-task tests; 10; 11 |

Order: 1 -> 2 -> 3 -> 4 -> 5 -> 6 (can parallel with 3-5 after 1) -> 7 -> 8 -> 9 -> 10 -> 11. Tasks 3/4 are file-disjoint and may run in parallel after Task 2.
