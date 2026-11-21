# Unified Bundle Marketplace Sources — Design

Status: OWNER DECISIONS APPLIED (2026-10-27). Design only; no plan, no code. Phase 1 = build now; Phase 2 (vendor commercial layer) = design only (section 6).

## 1. Intent and why

Global admins can add **marketplace sources**: git repos whose `bundles/` tree holds Waddles app bundles. A scanner discovers bundles, validates them, builds or trusts them, and feeds them through the *existing* publish -> approve -> Ed25519-sign -> install pipeline.

**Unification (key decision):** our own bundles are not a special case. The waddles repo is the **built-in marketplace source**; `seed_core_bundles.py` / `core-bundles.yaml` become the built-in source's *trust profile and activation policy*, not a parallel mechanism. One scan -> validate -> build -> sign -> install path serves first- and third-party; only the **gate** differs (system auto-approve vs mandatory human approve).

Why now: install today is one-bundle-at-a-time (`POST /bundles`, `bundle_versions.py:97`) or a deploy-time seeder. Third-party discovery, trust, and update have no first-class home.

## 2. Goals / non-goals / anti-goals

| Goals | Non-goals | Anti-goals |
|---|---|---|
| API-first source CRUD (`platform:admin`), CLI + webui inherit | A public/hosted marketplace storefront or ratings | Any silent install or silent update from an external source |
| Both build models: prebuilt and sandboxed | Per-tenant private sources (global-admin only, v1) | A second install pipeline beside the existing one |
| Scheduled auto-update, human re-gated | Signing with source/publisher keys at runtime (executor trusts platform keys only) | Letting a source shadow `penguintechinc.waddles.*` or another source |
| First-party folded into the same mechanism | Rewriting the executor's signature verification | In-cluster builds with network egress or privileged pods |

## 3. Constraints (from the code)

| Constraint | Where |
|---|---|
| Executor verifies only platform Ed25519 keys; fail-closed without `BUNDLE_SIGNING_PUBLIC_KEYS` | `core/bundle_executor/src/signing.rs:149,203,237` |
| Platform signs at approval; private key env-only | `hub_api/services/bundle_signing_service.py:48,107,173` |
| Signing payload binds `app_id, version, digest, approval_id` | `bundle_signing_service.py:78` |
| `app_id` shape `<github-org>.<repo>.<bundle-name>`; the owner is the first segment (`<github-org>`) | `bundle_manifest_v2.py:42,432-447` |
| `penguintechinc.waddles.` reserved; seeder-only via `_guard_core_namespace` | `vendor_bundle_authz.py:56`, `seed_core_bundles.py:420` |
| Third-party namespace precedent: `penguintechinc.waddles.vendor-{id}-{app}` (no regex widening) | `vendor_bundle_authz.py:59` |
| Version FSM UPLOADED..PUBLISHED/REJECTED; prebuilt publishes straight to PUBLISHED, "staged != approved" | `bundle_version_service.py:73-95,455-517` |
| Global install is human-gated, does NOT activate; SYSTEM `install_source` only for the seeder | `bundle_approval_service.py:444` |
| Global-admin scope `platform:admin` | `marketplace_lifecycle.py:427`, `bundle_approvals.py:133` |
| Lifecycle tables | `config/postgres/migrations/069_app_bundle_tiers.sql:21-58`; `app_versions`/`app_install_approvals` (alembic 0023) |
| SSRF: `validate_outbound_url`, `guarded_get` (HTTP only; no git support) | `hub_api/services/url_guard.py:117,133,196` |
| Existing pinned, reproducible builder toolchains (rust 1.97.1, py 3.13, dotnet 10.0.401, digest-pinned) | `bundles/Dockerfile.core-bundles:18-34,106,170`, `.github/workflows/core-bundles-reproducible.yml` |
| Real layout `bundles/{language}/{app}/` with `bundle.yaml` + `hub-manifest.yaml`; `core-bundles.yaml` `source_dir` | `bundles/python/about/*`, `bundles/core-bundles.yaml:12-18` |

Alembic head is `0061` (`alembic/versions/0061_port_legacy_sql_103_codec.py`); the new migration chains after the current graph head at build time.

## 4. Decided design

### 4.1 Data model (alembic migration, runtime via penguin-dal)

`marketplace_sources` (numeric PK, `public_id` UUID — API/URLs/logs use UUID only):

| Column | Notes |
|---|---|
| `id` BIGINT PK, `public_id` UUID | Entity-Identity rules |
| `name` | display alias only, never a reference |
| `is_builtin` bool + `git_url` NULL for builtin | builtin = sentinel row, exactly one, seeded by migration/startup, immutable (no delete, no edit of url/mode) |
| `git_url`, `ref` | https only |
| `pinned_commit_sha` (40-hex) | **the only thing ever cloned/built**; `ref` is just the tracking hint |
| `build_mode` int enum | 1 prebuilt, 2 sandbox, 3 builtin-image (see 4.6) |
| `publisher_key_fingerprint` + `publisher_public_key` | **REQUIRED** for prebuilt sources: Ed25519 key registered per source; prebuilt bundles must carry a matching signature verified before approval (4.4). Not required for sandbox (platform builds it) or builtin |
| `sandbox_ack_by`, `sandbox_ack_at`, `sandbox_ack_text_hash` | required non-null when `build_mode=2` |
| `status` int enum | pending-scan / active / needs-approval / disabled / error |
| `last_scanned_at`, `last_scan_error`, `pending_commit_sha` NULL | `pending_commit_sha` = newer commit awaiting re-gate (4.5) |
| `created_by`, timestamps | audit trigger like `app_versions_audit_log` |

`source_id` column added to `app_versions` and `app_catalog` (nullable; NULL/builtin = first-party). Enforces provenance and lets uninstall-source cascade to *deactivation* (never row deletion; versions are immutable).

### 4.2 Source CRUD API (new blueprint `marketplace_sources.py`, `platform:admin`)

`POST /marketplace-sources`, `GET` (list), `GET /<public_id>`, `DELETE /<public_id>` (disables + deactivates its bundles; builtin refused), `POST /<public_id>/rescan`, `POST /<public_id>/approve-update`.
- `build_mode=sandbox` add requires `sandbox_trust_ack` = the exact current warning text hash (4.8 text); missing/stale -> 422. The ack is stored and audit-logged.
- Response DTOs via `@validate_response`; rate-limited; OpenAPI `openapi/v{major}.yaml` updated.
- Gated by a PostHog flag `waddles.marketplace-sources` (default OFF; external sources are a new capability) plus license gate (decided): **prebuilt external source = Professional; sandbox in-cluster build = Enterprise**; the built-in first-party source is never gated. Tier resolved via `get_tier()` at platform/deployment scope per the licensing model; the sandbox ack endpoint also enforces Enterprise server-side. The built-in source is a core mechanism: opt-out kill-switch `waddles.disable-builtin-source`.

### 4.3 Scanner service (`services/marketplace_source_scanner.py`)

1. **Clone safely:** validate `git_url` via `url_guard.validate_outbound_url(..., allowed_schemes=("https",))` (`url_guard.py:196`); `git` subprocess (no shell) with `--depth 1`, `GIT_TERMINAL_PROMPT=0`, no submodules, no LFS, no hooks, `protocol.allow=never` except https, redirects limited. **Gap closed (decided):** `url_guard` is HTTP-only and resolves DNS separately from git (TOCTOU/rebind), so it is only a pre-check. The actual clone is routed **through the egress-proxy with a host allowlist**; direct git egress from the hub is denied by network policy. Public https repos only; no git credentials in v1.
2. **Pin:** resolve `ref` -> SHA; verify the checked-out HEAD == SHA. Only the SHA is recorded as active.
3. **Guards:** max clone bytes, max file count, max single-file size, max path depth, symlink rejection (no links escaping the root), no path traversal; wall-clock timeout. A violation rejects the whole scan with a logged reason.
4. **Walk** exactly `bundles/{python,rust,csharp,javascript}/*/`; require `bundle.yaml` + `hub-manifest.yaml`; ignore everything else.
5. **Validate** `hub-manifest.yaml` with `parse_bundle_manifest_v2` (`bundle_manifest_v2.py:399`); reject, don't repair, on error. `version` must be semver.
6. **Namespace enforce:** manifest `app_id` must be `<github-org>.<repo>.<bundle-name>` with `<github-org>.<repo>` equal to the source's own GitHub org and repo. A manifest claiming `penguintechinc` from any other source is a hard reject. `app_id` collisions across sources are impossible by construction.
7. **Supply-chain check:** parse the bundle's dependency manifests (`pyproject.toml`, `Cargo.toml`/`Cargo.lock`, `*.csproj`, `package.json`/lock) against the PRC/sanctioned denylist (general.md); a hit rejects the bundle. Also reuse the permission summary (`get_permission_summary`, `bundle_approval_service.py:315`) so the approver sees capabilities.
8. Output: per-bundle `ScanResult` (slotted dataclass) -> feeds 4.4 / 4.5.

### 4.4 Build paths — converge on the existing pipeline

```
 git source @ pinned SHA
      |
   scanner (4.3) ---> validated bundle candidates
      |                         |
      |  prebuilt               |  sandbox
      v                         v
 verify committed .wasm     K8s Job (rootless, no egress,
 digest == manifest/sidecar pinned toolchain image, caps)
 [+ publisher-key attestation]  -> .wasm + digest artifact
      \                         /
       v                       v
   process_prebuilt_component()  (bundle_version_service.py:517)
       -> app_versions PUBLISHED (scan_status=not_scanned)
       -> [HUMAN platform:admin approve]  bundle_approvals.py:133
       -> sign_and_record_version()  (platform Ed25519)  signing_service:173
       -> install_version_globally()  approval_service:444
       -> tenant availability -> community activation (unchanged)
```

**(a) Prebuilt:** repo commits the built `.wasm`, its digest (sha256) and a detached publisher signature beside `hub-manifest.yaml`. Scanner recomputes the digest, compares, and **verifies the signature against the source's registered publisher key before the version may enter approval** (missing/invalid -> reject; no key registered -> source cannot be prebuilt). This is defense-in-depth on top of the human `platform:admin` approval. **The publisher signature is an attestation only** — the executor trusts platform keys, so the platform re-signs at approval (`signing.rs:149` unchanged). This keeps the executor, key rotation and `BUNDLE_SIGNING_PUBLIC_KEYS` untouched.

**(b) Sandboxed builder:** an in-cluster K8s Job per bundle build.
- Rootless (`runAsNonRoot`, `USER appuser`), read-only root FS, `emptyDir` scratch only (stateless), PSA `restricted`, Tetragon allowlist for the builder binaries, **CiliumNetworkPolicy default-deny egress incl. DNS**. Source is injected by the hub-side orchestrator (which cloned it) as a tarball; dependencies must be **vendored** in the repo (`cargo vendor`, wheels, nuget cache) because no network exists — an unvendored build fails loudly rather than reaching out.
- Toolchain = the *existing digest-pinned* builder stages (`Dockerfile.core-bundles:18,106,170`), run as separate generic builder images; reproducibility flags (`SOURCE_DATE_EPOCH` etc.) reused.
- Caps: CPU/mem/ephemeral-storage limits, `activeDeadlineSeconds`, output-size cap, source-size cap (zip-bomb), output must be a single componentized `.wasm`.
- Result handed back as a digest + artifact; hub then runs the same `process_prebuilt_component()` path. The builder holds **no** signing key and **no** DB/S3 credentials; it cannot approve or sign.
- Optional reproducibility hardening: if the repo also commits a prebuilt artifact, a sandbox build whose digest matches is flagged "reproducible" for the approver.
- Requires **vendored dependencies** (decided; no package mirror in v1). Enterprise tier. Mandatory trust warning + stored ack (4.8). The builder Job is itself the arbitrary-code boundary; a compromise yields at most a malicious *candidate* that still needs human approval, but a sandbox escape is the residual risk — hence the warning.

Both converge (and both are mandatory-human-approved for externals); the single mandatory difference vs first-party is that **external sources never use the SYSTEM install path**: `install_source="human"`, `installed_by=<platform admin>` (`bundle_approval_service.py:444`).

### 4.5 Auto-update (scheduled, never silent)

- Decided: periodic re-scan + **notify platform admins**; every update (including patch) requires human approval in v1, no lighter path.
- A scheduled job (CronJob, penguin-dal sync-worker pattern — mind the AsyncDB vs AsyncDAL trap in memory) re-resolves each active source's `ref`.
- New commit -> **do not move `pinned_commit_sha`**. Record `pending_commit_sha`, run scan+build in the background, produce new `app_versions` (new semver; a same-version/different-digest conflict is refused, as the seeder already does) at PUBLISHED, and set source `status=needs-approval`.
- The currently pinned, already-approved version **keeps running**. Advancing `pinned_commit_sha` happens only on `POST /<id>/approve-update` (and per-version approve for the bundle) — the same human `platform:admin` gate; `classify_diff` (`bundle_approval_service.py:341`) is shown so a permission *expansion* is highlighted.
- A source that force-pushes/rewrites history behind a pinned SHA is detected (SHA no longer reachable) and set `error`, bundles remain pinned, admin alerted.
- Auto-build for sandbox sources runs only post-add-ack; the ack is bound to the **source**, but any change to `build_mode` or `git_url` voids it (re-ack required).

### 4.6 First-party unification

| Today | After |
|---|---|
| `seed_core_bundles.py` reads `core-bundles.yaml` (list + `source_dir` + activation targets) from the baked image | Same code runs as the **built-in source adapter**: source row `is_builtin`, `build_mode=builtin-image`, pinned SHA = the image's baked commit |
| `core-bundles.yaml` = catalog + build input + activation policy | Retained as the built-in source's **activation policy overlay** (which bundles auto-activate where) — unchanged semantics; scan of `bundles/*/*/` becomes the *discovery*; yaml entries that don't match a scanned bundle fail loud |
| `Dockerfile.core-bundles` prebuilds all artifacts in-image | Stays the built-in's build path (reproducible, CI-gated by `core-bundles-reproducible.yml`); equivalent to "prebuilt" with the image as the attestation |
| `_guard_core_namespace` (`seed_core_bundles.py:420`) | Moves into the shared namespace gate: `core.*` is valid **iff `source.is_builtin`**; every external source is forced into `src-{id}` |
| `SYSTEM_ACTOR` auto-install (`:113`) | Preserved, built-in only; assertion in the install path: `install_source=SYSTEM` requires `is_builtin` |
| `reconcile_removed_core_bundles` (`:1063`) | Scoped by `source_id = builtin`; extendable to external sources as "deactivate on source removal" (never auto-delete human-approved rows) |

Migration is additive: the built-in row + `source_id` backfill (NULL->builtin for `penguintechinc.waddles.*`). No behavior change for existing alpha/beta deploys; the seeder keeps working throughout. Doing the refactor to *literally* call the shared scanner (rather than just alias) is left to the builder, behind the kill-switch flag.

### 4.7 Supply chain and security

| Control | Mechanism |
|---|---|
| PRC/sanctioned deps | Denylist check in scanner (4.3 step 7); failure rejects the bundle; list owned centrally, not per-source |
| SSRF / egress | https-only, `url_guard` + resolved-IP pinning for git; sandbox Job has no egress |
| Zip-bomb / oversize | clone, file-count, depth, per-file, and artifact caps; timeouts |
| Pin-to-SHA | only SHA is built/installed; `ref` is a hint; tag/branch moves never auto-trusted |
| Namespace isolation | `penguintechinc.waddles.src-{id}-*` for externals; `core.*` builtin-only; hard guard shared with install path |
| Tenant/blast radius | external bundles need human global approve, then tenant admin `make_available`, then community activation (unchanged 3-tier) |
| Capability gate | WASM capability gate (`core/bundle_capability_gate`) + approver sees permission summary; external bundles never get auto-granted permissions (the seeder's `_grant_core_bundle_permissions` is builtin-only) |
| Secrets | builder has no secrets; git creds (private repos) are out of scope v1 |
| Audit | structured logs + audit-table rows for: source add/remove/rescan, ack text hash, scan outcome (counts), pending update, approve/deny, signature issued. Logs contain `public_id` + counts; never raw git URL credentials or PII |
| PII | no user data touches sources; usernames never logged |
| OTel | logs + metrics (scan count, bundles examined/rejected, build duration) + traces; examined counts reported — zero examined = FAIL |

### 4.8 Trust warning (sandbox build) — required UX text

> **Building a marketplace source runs its code on your platform.** Sandboxed build compiles source code from `<git_url>` @ `<sha>` inside Waddles infrastructure. That code is untrusted and could attempt to compromise or disrupt the platform. Builds run isolated (no network, no privileges, resource-limited) but isolation is not a guarantee. Only add sources you trust and have reviewed. Every build result still requires your explicit approval before it is installed, and you remain responsible for what you approve.
> [ ] I understand and accept this risk for this source.

The text is versioned; the ack stores user, time, source SHA at ack, and the text hash. Prebuilt sources show a shorter notice (the artifact is third-party code you are about to run).

## 5. Left to the builder

- Exact status enum values and column types, migration numbering/graph-head, index design.
- Builder-Job manifest (Helm chart, CNP/Tetragon policy, runtime-class), builder images, and orchestrator<->Job hand-off (tarball volume vs object-store).
- The CronJob cadence, backoff, and jitter; webui screens.
- Unit/regression/integration tests, including: a malicious-source suite (traversal, symlink, `core.*` shadow, SHA mismatch, oversize, SSRF host) and a flag-on E2E that really builds + approves + executes a bundle (no-stubs gate).
- How `seed_core_bundles` is mechanically refactored to share the scanner (4.6), without regressing the existing 1700-line test suite.

## 6. Phasing

**Phase 1 (build now)**
1. `marketplace_sources` table + alembic migration; `source_id` on `app_versions`/`app_catalog`; audit trigger.
2. `platform:admin` CRUD API (add/list/get/remove/rescan + approve-update), PostHog flag, Pro (prebuilt) / Enterprise (sandbox) gate; OpenAPI.
3. Git clone via egress-proxy allowlist + scanner: pin SHA, size/zip-bomb/symlink guards, walk `bundles/{lang}/{app}/`, `bundle_manifest_v2` validation, `src-{id}` namespace enforcement, PRC-dependency denylist.
4. Publisher-key registration + signature verification (prebuilt).
5. Prebuilt path into `process_prebuilt_component()` -> human approve -> platform re-sign -> install.
6. Sandbox builder Job (rootless, no egress, pinned toolchains, vendored deps, caps) + trust-warning ack (Enterprise).
7. Auto-update: scheduled re-scan, `pending_commit_sha`, admin notification, re-approval.
8. Fold the waddles repo in as the `is_builtin` source: `seed_core_bundles` as activation overlay, `core.*` reserved, SYSTEM install builtin-only.
9. Tests (malicious-source suite, flag-on E2E), docs, OTel/audit.

**Phase 2 (design only now): vendor commercial wrapper**

Layering: `marketplace_source` **supplies bundles** (git repo + `bundles/`); a **vendor** is a *commercial wrapper over a source* that attaches pricing, seating, community-licensing and sales collection to bundles riding in it. A global admin can run their own external marketplace AND onboard vendors who bring their own marketplace, licensing and collect sales. This reframes (does not deprecate) the existing vendor surface: `marketplace_sellers`, `marketplace_subscriptions`, `marketplace_payments`, `marketplace_premium_offerings/_subscriptions` (`config/postgres/migrations/017_add_marketplace.sql:85,113,135,156,220`), `community_premium_subscriptions` (`059_marketplace_consolidation.sql:92`), and the vendor tables (`021_add_vendor_submissions.sql:8,109,187`; services `marketplace_billing_service.py`, `marketplace_vendor_service.py`) become the commercial layer, keyed to a `source_id` (vendor 1:N sources; a source has at most one commercial owner). Phase 1 leaves a nullable `vendor_id` out on purpose and adds nothing that blocks this. The large, separate surface is the **three payment-provider integrations (PayPal, Wix, Square)** (checkout, webhooks, payouts, reconciliation); scope them as their own effort.

## 7. Open questions — resolution

| # | Question | Status | Answer |
|---|---|---|---|
| 1 | License tier | RESOLVED | Prebuilt external = Professional; sandbox build = Enterprise; built-in never gated |
| 2 | Prebuilt trust | RESOLVED | Pinned publisher key required per source; signature verified before approval, on top of human approval; platform still re-signs |
| 3 | Private repos | RESOLVED | No credentials in v1; public https only |
| 4 | Namespace | RESOLVED | `penguintechinc.waddles.src-{id}-{app}`; `_APP_ID_RE` not widened; platform assigns the scoped segment |
| 5 | Git egress | RESOLVED | Through egress-proxy with allowlist; `url_guard` pre-check only |
| 6 | Vendored deps | RESOLVED | Required for sandbox builds; no mirror in v1 |
| 7 | Update cadence/approval | RESOLVED | Periodic re-scan + admin notification; every update needs human approval in v1. Exact cadence value left to builder |
| 8 | Tenant-private sources | DEFERRED | Roadmap (per-Enterprise-tenant), not v1 |
| 9 | Vendor path | RESOLVED | Reframed as commercial wrapper over a source; Phase 2 (section 6) |
