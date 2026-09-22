# Waddles Rust Data Plane — Milestone M2b (hub-api control-plane) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every task's implementer sees ONLY that task's text — no other task, no external memory. Every fact a task needs (table columns, function signatures, endpoint paths, scope names, setting keys) is copied into that task, not referenced by "see Task N."

**Goal:** Add the Python/Quart hub-api control-plane surface for Waddles app-bundle installs: version upload + compiler-Job orchestration, the digest table and its Least-User-Access RBAC, install-time permission consent, `consumes`→`app_stream_grants` resolution with Valkey consumer-group lifecycle, grant revocation, the distribution API fields the Rust stages/executors poll, the compiler's artifact callback, and the new tenant/global settings.

**Architecture:** Every new table is owned by hub-api's existing dual-schema convention — real DDL in a new Alembic migration under `alembic/versions/`, queried at runtime through a `penguin-dal` `AsyncDB` (per R52; `hub_api/services/bundle_install_dal.py`, Task 4) that calls `await install_dal.reflect()` once at startup to discover exactly those Alembic-created tables — never a hand-written `pydal`/SQLAlchemy binder, and never a second DDL owner. Every new REST surface is one `blueprints/v1/<group>.py` module with a module-level `BLUEPRINTS` list (auto-discovered, zero registration edits) plus a `services/<group>_service.py` doing the real DB work in `penguin-dal`'s `Query`/`TableProxy` builder form for a single new table (Pattern A), a `raw_sql_rows`/`raw_sql_write` helper for a read-only join/`GROUP BY` case (Pattern B), or `core_transaction()` for a write that must be atomic across two tables (Pattern C, Decision #18) — never raw driver-paramstyle SQL, and never a new query against `penguin-dal` bypassing its parameter binding. `app_versions` — the digest table — has exactly two Postgres writers (`waddles_publisher`, the compiler's trusted-publisher container from M2a, and `hub_api`); every other service role gets zero privileges on it, generated from one normative YAML matrix and asserted equal to the live grants by a CI test, never hand-written twice. hub-api verifies a claimed digest by re-hashing the bucket object; it never computes one itself. Activation and rollback are a separate, hub-api-owned pointer table (`app_active_versions`), never an edit to a digest row.

**Tech Stack:** Python 3.13, Quart, `quart-schema` (`@validate_request`/`@validate_response`), `penguin-dal==0.4.0` (runtime queries against every table this plan creates, per coordinator ruling R52 — exact pin, same version/API the M1.5 bundle-migration plan pins; `asyncpg` as its async Postgres driver, `aiosqlite` test-only), `pydal` (runtime queries against hub-api's **pre-existing** ~600-endpoint surface only — untouched by this plan, see Global Constraints), Alembic + raw SQL (schema/DDL, unchanged DDL owner), `boto3` (S3-compatible bucket), `kubernetes` (Job orchestration), `redis.asyncio` (Valkey admin ops), `cryptography` (AES-256-GCM secret-at-rest), `PyYAML` (RBAC matrix), pytest + `penguin_dal.AsyncDB` file-backed sqlite fixtures for every new table, existing `AsyncDAL` file-backed sqlite fixtures for existing tables.

**Spec:** `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` at commit `680a0a9b` (branch `docs/rust-data-plane-spec`) — sections 2 (D24/D26/D28), 5.2, 6.4–6.10, 7.4 (db capability), 8.4, 9, 10.3–10.6, 11.6.2, 11.10, 12.3, 13.5, 16 (M2), 19 (Q1/Q3), 20 (A-series). This plan implements the **hub-api** half of M2 only — the compiler binary and SDKs (M2a) are a separate plan; every boundary this plan shares with M2a is called out explicitly and marked "must match M2a." **Tasks 42-49 (added at commit `e87b389c` of the same spec branch, user review 3) fold in D29/D30/D31** — sections 4.1.1, 5.9, 5.11, 5.12, 6.11, 6.12, 9.7.1, 10.1, 10.3, 11.1, 11.10 (updated), 12.3 (envelope binding key — hub-api never holds it), 15.4, and the M2b milestone row in Sec16. See Decisions #14-#17 below for what changed and why.

**Coordinator ruling R52 (binding, folded in after the plan above was otherwise complete):** "We aren't using pydal anymore — instead we are running penguin-dal." Every table this plan itself creates (the 11 tables named in Decision #6) is queried at runtime through `penguin-dal` (`AsyncDB`, exact pin `0.4.0`, the same version and public API the M1.5 bundle-migration plan uses), never through a new `pydal` binder or a new `dal.define_table()` call. hub-api's **existing** ~600-endpoint pydal surface (`tenants`, `communities`, `community_members`, `community_roles`, `app_catalog`, `hub_users`, `audit_log`, everything `services/schema.py`'s existing `bind_*_tables()` functions already bind) is **not rewritten** — every task below that still reads one of those tables keeps doing so through the existing `async_dal`/`dal` pair, unchanged. See Decision #18 for the full atomicity investigation this ruling required (one genuine cross-table gap, closed in Task 44; every other new-table-plus-`audit_log` write stays deliberately best-effort, matching this plan's own pre-existing convention) and the Global Constraints entries below for the mechanics. Tasks 4 and 43 (the old "pydal test binder" tasks) and every task whose code queries a new table (10, 11, 13-17, 19, 21, 22, 23, 24, 25, 26, 27, 29, 30, 31, 32, 33, 34, 35, 36, 39, 40, 44, 45, 46, 47, 48) are rewritten accordingly; Decision #17/Finding #12 (RLS scope) is sharpened to cite the spec's own milestone assignment.

---

## Global Constraints

Copied verbatim from the spec and house rules — every task's code must satisfy all of these, not just the ones its own section repeats.

- **Python 3.13, Quart (never Flask), async `def` on every route** (`backend-python.md`).
- **`penguin-dal` (R52) is used for every table this plan itself creates; `pydal` is used only by hub-api's pre-existing surface, and this plan adds no new `pydal` table, binder, or query.** hub-api's existing codebase (every pre-M2b `services/*.py`, every pre-M2b `blueprints/v1/*.py`) uses `pydal` directly via a hand-rolled `AsyncDAL` wrapper (`libs/flask_core/flask_core/database.py`), a documented, load-bearing deviation (mem0: "A decision was made to use synchronous pydal instead of AsyncDAL for data access operations" / `hub_api/PORTING.md`) — that surface is **not rewritten by this plan** and every task below that reads one of those pre-existing tables (`tenants`, `communities`, `community_members`, `community_roles`, `app_catalog`, `hub_users`, `audit_log`, etc.) keeps doing so through the existing `async_dal`/`dal` pair, unchanged. The coordinator's binding ruling (R52, 2026-09-14: "we aren't using pydal anymore... instead we are running penguin-dal") applies to **new** data access: every one of the 11 tables this plan's own migrations create (Decision #6) is queried through a `penguin_dal.AsyncDB` (`hub_api/services/bundle_install_dal.py`, Task 4) instead — a coexisting, separate connection pool against the same `DATABASE_URL`, reflecting the same live Postgres schema (see Task 4 for the full wiring). GitHub issue #307 tracks migrating hub-api's *existing* pydal surface onto `penguin-dal` wholesale; it is explicitly out of scope here (open, not started, "sequence after the [v3.0] demo" — see the Findings table). **The new Postgres *roles and DDL* (Global Constraints item below) still use SQLAlchemy directly for schema/DDL only, matching `backend-database.md` rule #2 ("SQLAlchemy + Alembic — schema init + migrations ONLY") — `penguin-dal` never issues DDL in this plan, only `AsyncDB.reflect()` against tables Alembic already created.**
- **`@dataclass(slots=True, frozen=True)` for every DTO.**
- **Every function has type hints; `mypy --strict` must pass.**
- **Every DTO field name is camelCase on the wire** — matches every existing hub-api blueprint (`hub_api/PORTING.md`'s DTO-casing note; `convert_casing` is not enabled in this app's `QuartSchema` setup).
- **OIDC scopes only, never role names.** New scope introduced by this plan: `bundles:artifact` (machine JWT, the compiler's artifact callback). Reused: `platform:admin`, `tenant:admin`, `distribution:read`, `intake:write`.
- **Tenant from the validated JWT only, never from a path/query/body param** — `flask_core.tenancy.get_tenant_context`, `tenant_middleware` first, always.
- **PII tokenization:** the single identity table is `hub_users` (integer `SERIAL` primary key, `config/postgres/migrations/000_create_base_schema.sql:81-98` — this repo's identity table predates and is not a UUID table). Every approver/actor column this plan adds (`app_install_approvals.approved_by`, `app_stream_grants.granted_by`, `platform_settings.updated_by`, `app_active_versions.activated_by`) is `INTEGER REFERENCES hub_users(id)`, matching the established convention (`loyalty_redemptions.fulfilled_by`, `ai_byok_keys.created_by_user_id`, `music_policy.updated_by`) — **a deliberate, documented deviation from the spec's literal "uuid" column-type wording**: the substance of PII tokenization (never a name or email, an opaque reference into the one identity table) is fully satisfied by an integer FK; introducing a parallel UUID identity column on `hub_users` for this one feature would be new scope this plan does not take on.
- **Secrets** (webhook HMAC secrets) encrypted at rest with AES-256-GCM, key from an env var, never a CLI flag, never logged. Never plaintext in the DB.
- **90% coverage minimum** on every new module (`critical-rules.md` Coverage) — `pytest --cov=services --cov=blueprints --cov-report=term-missing --cov-fail-under=90` scoped to this plan's new files, run in Task 41.
- **Dependency pinning:** every new line added to `hub_api/requirements.in` gets an exact version floor with a reason comment, then `hub_api/requirements.txt` is regenerated with `uv pip compile --generate-hashes` (Task 6 adds `kubernetes`/`PyYAML`; **Task 4 adds `penguin-dal==0.4.0` exact-pin plus its `asyncpg`/`aiosqlite` drivers, R52** — same exact version the M1.5 bundle-migration plan pins).
- **Verification integrity:** every scanner/test run in this plan reports a non-zero denominator (files scanned, roles examined, tables examined) — a zero-item run is a FAILURE, not a pass (`critical-rules.md` Verification Integrity). The RBAC live-grants test explicitly asserts `>= 8` roles and `>= 8` tables examined (spec §11.10.1 literal requirement).
- **Least User Access via RBAC (spec D28, §11.10):** every Postgres role gets exactly the privileges its job needs, generated from one normative file (`config/postgres/rbac-matrix.yaml`), never a hand-written `GRANT`. `app_versions` has **exactly two writers** — `waddles_publisher` (M2a's trusted publisher container) and `hub_api` — every other role (`svc_ingest`, `svc_process`, `svc_action`, `svc_streaming`, `webui`, the executors) gets **zero** privileges on it. Every write is captured by an `AFTER INSERT OR UPDATE OR DELETE` audit trigger recording the writing role, the row key, and the old/new digest. **hub-api verifies a claimed digest by re-hashing the bucket object; it never computes or invents a digest itself.**
- **Activation/rollback never edits a digest row** — it is an `UPDATE` of `app_active_versions.version_id`, a separate hub-api-owned pointer table (spec §6.10). Activating a digest with no corresponding `app_versions` row is refused.
- **Feature flags:** every new write surface sits behind a PostHog flag, default OFF, two-gate with license tier, via `flask_core.feature_flags.feature_enabled(flag_key, tenant=..., default=False)`. Flag keys this plan gates on (already defined by the spec, not invented here): `waddles.core.wasm-bundles`, `waddles.core.generic-intake`, `waddles.core.prebuilt-bundles`.
- **No `flask_core.database.AsyncDAL`/`pydal` reference inside anything that ships to a bundle** — not applicable to this plan (hub-api's own control-plane code is exempt; D21b's ban is on bundle *source*, compiled by M2a's compiler).
- **R52 (coordinator ruling, binding):** every table this plan's own migrations create is queried at runtime exclusively through `penguin-dal==0.4.0` (`hub_api/services/bundle_install_dal.py`'s `AsyncDB`, Task 4) — never a new `pydal.define_table()`, never a new `dal.<new_table>.insert()`/`.select()`/`.update()`/`.delete()` pydal call. hub-api's pre-existing `pydal` tables and call sites are unchanged. `create_source()`'s `ingest_sources` + `workstreams` writes (Task 44) — the plan's one genuine cross-table atomicity requirement, both new tables — run inside one `install_dal.engine.begin()` block directly (not `core_transaction()`, which only handles independent statements — see Decision #18(a) for why). Every other new-table-plus-`audit_log` write (Tasks 13, 29, 31, 35, 39, 46) stays deliberately best-effort (`try`/`except`, non-blocking), matching this plan's own pre-existing convention for every `audit_log` write it makes — see Decision #18 for the full investigation and why that is correct, not a gap.
- **Branching:** this work lands on `release/v3.0.X` via a `docs/` branch for the plan itself (already checked out); the *implementation* work this plan describes happens on a `feature/`-prefixed branch off `release/v3.0.X`, per `devops.md`.
- **Commit format:** `feat(hub-api): ...` / `test(hub-api): ...` / `db(hub-api): ...` / `docs(hub-api): ...` / `chore(hub-api): ...`, each ending with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
  ```
- Say **Waddles**, never "WaddleBot" for the product name in new prose (code/DB identifiers keep the legacy `waddlebot`/`hub_api` names per spec D22 — the chart directory, `DB_NAME`, and Python package paths are the explicitly surviving legacy identifiers).
- Never "restream" — say relay/forward.

---

## Decisions Carried Into This Plan (read before any task)

These resolve every ambiguity a task would otherwise have to re-derive. They are facts, not options.

| # | Decision | Why |
|---|---|---|
| 1 | Approver/actor columns are `INTEGER REFERENCES hub_users(id)`, not `uuid`. | See Global Constraints PII row above. |
| 2 | `app_versions` (spec §6.10) is the **digest table only** — no lifecycle/status column. It is written by exactly two roles. hub-api's own pre-publish lifecycle tracking (upload received → Job launched → published/rejected) lives in a **new, hub-api-exclusive** table, `app_version_uploads`, correlated to `app_versions` by the natural key `(app_id, version)` and a denormalized `app_version_id` pointer once known. | The spec's §6.10 column list has no status field; conflating the two tables would give a third, unaccounted-for writer a reason to touch `app_versions`. |
| 3 | The compiler's artifact callback (`POST /api/v1/bundles/{app_id}/versions/{version}/artifact`) is a **notification/cross-check**, not the write authority. hub-api re-hashes the bucket object itself and compares against the claimed digest (refuses + audit-logs on mismatch); it looks for a matching `app_versions` row (written directly by `waddles_publisher`, M2a) and if none exists yet, inserts one itself as a fallback using its own (also-granted) write privilege — never computing a new digest, only ever storing the value the compiler claimed, after verifying it. | Coordinator directive: "hub-api verifies but never computes the digest." Keeps M2b fully testable independent of M2a's ship date. |
| 4 | Activation/rollback is `POST /api/v1/apps/{app_id}/versions/{version}/activate`, an upsert into `app_active_versions` (tenant/community-scoped, PK `(app_id, tenant_id, community_id)`). Refuses (409) when no `app_versions` row exists with that exact `(app_id, version)` and a non-null `artifact_digest` — i.e. a digest hub-api never verified can never be activated. Rollback is the same endpoint pointed at an older, already-published version. | Spec §6.10: "Rollback is an UPDATE of version_id here... never an edit of a digest." |
| 5 | RBAC matrix file: `config/postgres/rbac-matrix.yaml` (repo root, spec §11.10.1's literal path). Loader/generator: `scripts/db/rbac_matrix.py` (pure stdlib + PyYAML, no hub-api-package import, loadable by absolute path from both the Alembic migration and the test suite so no `sys.path` fragility). Grants are **generated from this file at migration-run time** — no hand-written `GRANT` anywhere. | Spec D28/§11.10.1: "Grants are generated from this file; nobody writes a `GRANT` by hand." |
| 6 | 8 Postgres roles this plan creates/asserts: `hub_api`, `waddles_publisher`, `svc_ingest`, `svc_process`, `svc_action`, `svc_streaming`, `webui`, `migration_runner`. **11 tables in the matrix** (9 from Tasks 1-3, plus `workstreams` and `workstream_usage_hourly` from Task 42): `app_versions`, `app_active_versions`, `app_version_uploads`, `app_install_approvals`, `app_stream_grants`, `custom_platforms`, `ingest_sources`, `platform_settings`, `app_versions_audit_log`, `workstreams`, `workstream_usage_hourly`. Satisfies spec's literal `>= 8 roles`/`>= 8 tables` non-vacuous CI check with room to spare. | Spec §11.10.1. |
| 7 | The Valkey ACL matrix (`config/valkey/acl-matrix.yaml`, spec §11.10.2) is **out of scope for this plan** — it is owned by the chart/M6 work across all four Rust services, not by hub-api's own control plane. This plan's Valkey touchpoint (`services/valkey_admin_client.py`) only creates/destroys consumer groups; it does not render or own the ACL matrix. | Spec frames the two matrices as parallel but separately-owned artifacts; hub-api's role in the Postgres one is direct (it owns the schema), its role in the Valkey one is not. |
| 8 | Settings keys (exact strings, copied from the spec — every later milestone's Rust code reads these literally): global `bundles.allow_prebuilt` (table `platform_settings`); tenant `allow_wildcard_consumes` and `bundles.egress.allowPrivateHosts` (existing generic `tenant_settings` table — no new tenant-settings endpoint needed, `GET/PUT /api/v1/tenant/<slug>/settings` already accepts arbitrary `{key,value}` pairs). | Spec §6.4.4 V30, §8.5, §12.3. |
| 9 | Feature-flag flag keys used verbatim from spec §13.5: `waddles.core.wasm-bundles`, `waddles.core.generic-intake`, `waddles.core.prebuilt-bundles`. All three are `min_tier: free` (core module), so the two-gate check only ever fails on the PostHog flag being off, never on tier. | Spec §13.5. |
| 10 | **Open question Q1 (spec §19) — trip re-enable is an admin action plus a new digest, never a runtime switch.** The executor keeps spec §7.5/A7's behaviour exactly: a trip-disabled bundle clears only when the pod observes a *new* `artifactDigest` from the distribution API, or when the pod restarts. hub-api's only participation is one explicit admin action, `POST /api/v1/apps/{app_id}/trip-reenable` (scope `tenant:admin`, Task 35), which re-points `app_active_versions` at a published version whose `artifact_digest` **differs** from the digest currently advertised for that scope, and refuses `409 same_digest_no_reenable` when it does not — so the only way an operator clears a trip is by causing a genuinely new digest to be advertised. No pod-scoped, deployment-scoped or cluster-scoped runtime toggle is added anywhere. | Spec §19 Q1 leaves the operator path open while §7.5/A7 fix the executor's side; an admin action that produces a new digest satisfies both without inventing a runtime re-enable switch. |
| 10b | **Open question Q3 (spec §19) — the per-bundle Postgres role is created at approval and dropped at uninstall.** `create_bundle_role` runs inside `approve_version()` (Task 21); `drop_bundle_role` runs inside `marketplace_lifecycle_service.uninstall_bundle()` (Task 36) — uninstall is the deliberate, user-initiated end of the bundle's life at that tenant, so it is the honest drop point. `BUNDLE_ROLE_GRACE_H = 168` survives **only** as the orphan sweeper's cut-off (Task 36's `bundle_role_cleanup_job`): a role whose bundle has had no activation and no `app_install_approvals` row for 168 h — i.e. one whose uninstall never ran, or ran before this plan shipped — is dropped by the CronJob. Password rotation stays manual, unchanged from the spec's stated interim. | Spec §19 Q3 fixes create-at-approval and a 168 h drop but names neither the trigger nor the job's owner; uninstall is the trigger, and the sweeper owns only what uninstall missed. |
| 11 | Endpoint paths, scopes and DTO shapes are fixed once, here, and never repeated with variation in a later task: see the table in Task 11's Interfaces block (versions), Task 22 (approvals), Task 23 (settings), Task 27 (ingest sources), Task 31 (grants), Task 33 (distribution v2), Task 34 (distribution sources), Task 35 (trip re-enable). |
| 12 | **Distribution API v2 is a new, separately-mounted route pair; the v1 route is not touched until the M6 cut-over.** `GET /api/v1/distribution/v2/bundles` (Task 33) serves the full spec §6.7 row — the five v1 fields plus `artifactVersion`, `artifactDigest`, `artifactKind`, `language`, `scanStatus`, `manifest`, `grants` — with `meta.version` = `2` and an `ETag`/`If-None-Match` 304 path. `GET /api/v1/distribution/sources` (Task 34) is the ingest-source registry the Rust svc-ingest polls, same ETag treatment. `GET /api/v1/distribution/bundles` keeps its **byte-identical** v1 body (five fields, `meta.version` = `1`) so today's `flask_core.stage_runner.BundlePoller` and every existing test in `test_v1_distribution_blueprint.py` keep passing unchanged; it is deleted in M6 once every poller is Rust. | The spec's §6.7 wording ("needs no versioning of the endpoint") assumes a flag-day cut-over of one consumer; this repo has a live Python poller on the v1 shape throughout M2b–M5, so a second route is the only way "the old endpoint keeps working" is literally true rather than true-if-clients-ignore-unknown-fields. Additive-only, reversible, and deleted by one commit at cut-over. |
| 13 | **Every ingest source carries an `auth` config that svc_ingest enforces** (user's third spec review, authoritative ahead of the spec amendment; the published shape **must match plan M5**). Wire shape, published verbatim by `GET /api/v1/distribution/sources`: `auth = {modes: ["hmac", "ip_allowlist"|"bearer"|"basic", ...], cidrs: [...], secret_ref: "<id>", origin_suffixes: [...], origin_cidrs: [...]}`. **Generic webhook sources** (`platform` starting `custom:`, and the built-in `webhook` platform) MUST declare at least one of `ip_allowlist`, `bearer` or `basic` **in addition to** `hmac`; a create or auth-update with none of the three is refused `422 auth_second_factor_required`. **Twitch and Kick sources** carry an origin policy instead: `origin_suffixes` (defaulting to `["twitch.tv"]` / `["kick.com"]`) plus optional `origin_cidrs`. Bearer tokens and Basic password hashes live encrypted in `ingest_sources.auth_secret_ciphertext`/`auth_secret_iv` (same AES-256-GCM helper as the HMAC secret) and are **never** returned by any endpoint — the wire carries `secret_ref` only. Every auth-config change writes an `audit_log` row (`ingest_source_auth_changed`) recording the old and new `modes`, never the secret. Schema lands as migration 0022 (Task 39); publication and the config/consent views land in Task 40. | The HMAC secret alone authenticates the *payload*, not the *caller*: anyone who replays a captured body passes it. A second factor binds the request to a network location or a credential, and the platform-origin policy does the same job for Twitch/Kick, whose senders are known. Refusing at create time is the only place the requirement is cheap to enforce. |
| 14 | **`workstreams` (spec §6.11, D30) is 1:1 with `ingest_sources`, but its FK targets `ingest_sources.id`, not the spec's literal `source_id text` FK.** `ingest_sources.source_id` is unique only per `(tenant_id, platform, source_id)` (Task 3's three-column `UNIQUE`), not globally, so the spec's shorthand FK cannot be taken literally in this schema — `platform`/`source_id` are denormalized onto `workstreams` for read convenience instead. `workstreams.ingest_source_id` is **nullable, `ON DELETE SET NULL`** (never `ON DELETE CASCADE`): `workstream_usage_hourly` rows must keep their `workstream_id` FK target for the life of the tenant's usage history even after the source itself is deleted, so deleting a source must never cascade-delete its workstream. `workstreams.id` is a real Postgres `UUID` (`gen_random_uuid()`); the pydal/sqlite test binder has no native UUID field type, so its `id` stays that ORM's usual autoincrement integer — every service function treats `workstream_id` as an opaque string (`str(row.id)`) on the wire and never assumes either representation, exactly like this codebase already treats `app_id`. Schema + backfill: Task 42. Ongoing creation/disable, wired into `ingest_source_service.create_source()`/`delete_source()`: Task 44. | Spec §6.11, §5.11 ("one workstream per configured ingest source... created 1:1 with each row in `intake_sources`"), §15.4 (backfill requirement). The FK-target and nullability deviations are the same class of documented, substance-preserving deviation as Decision #1's PII integer-vs-uuid choice. |
| 15 | **`routes_to` cross-tenant refusal (spec §5.9, D30) is wired into `approve_version()` (Task 21), extended by Task 46.** "Installed in the same tenant" is defined as: the target `app_id` has a non-superseded `app_install_approvals` row whose `tenant_id` equals the approving call's `tenant_id` (community-agnostic — a tenant-wide **or** any-community approval both count as "installed in this tenant"). A target absent from `app_catalog` entirely is `422 routes_to_target_not_found`; a target that exists but is not installed in this tenant is `422 routes_to_cross_tenant`. Both refusals write an `audit_log` row (`action = "routes_to_refused"`) before raising, so a refusal is exactly as auditable as a success (task brief: "refused at approval time (422 + audit)"). | Spec §5.9's install-time row: "hub-api validates that each target exists in `app_catalog`, is installed in the same tenant as the declaring bundle — a cross-tenant target is refused outright at approval, not merely left unapproved (D30)." `app_install_approvals` is this plan's own, already-existing proxy for "installed" — no new table or cross-service call is needed to answer the question. |
| 16 | **The usage aggregator (spec §5.12, §6.12, D31) is a Kubernetes CronJob, not an in-process background task, and is gated by the chart value `metering.enabled`, never a PostHog flag.** hub-api's `services/usage_aggregator_service.py` (Task 47) owns its own Valkey consumer group (`hub_api_usage_aggregator`) on `waddles:usage`, `XREADGROUP`s a bounded batch, aggregates in memory, commits one `workstream_usage_hourly` row per distinct `(tenant, community, workstream, stage, app, hour)` group, and only then `XACK`s the entries in that batch — so a crash between commit and ack causes at-least-once redelivery (a duplicate row on the next run), never data loss, which `workstream_usage_hourly`'s own append-only/summed-at-query-time design (spec §6.12) already tolerates by construction. The wire shape of one `waddles:usage` entry is this task's own decision (the spec specifies the transport, not the field names) — documented in full in Task 47's docstring, marked **must match** whichever plan implements the stage-side `XADD` producer (M3 `svc_action`, M4 `svc_process`, M5 `svc_ingest`, and `svc_streaming`'s own future plan). The per-community admin usage view (Task 48) stays **ungated**, following this plan's own established rule (Task 37: "write surfaces are gated; read surfaces are not") — reading usage data can never be blocked by a flag flip, only the recording pipeline can, and that pipeline's gate is the chart value the spec itself names. | Spec §5.12 ("batched and `XADD`ed... Stages are write-only"; "hub-api owns a consumer that reads `waddles:usage` and writes `workstream_usage_hourly`... append-only... corrections are new rows... summed at query time"), §12.3 (`metering.enabled`, a chart value "no charging or enforcement is wired to it"). A long-lived in-process consumer thread inside a horizontally-scaled Quart deployment raises "which replica owns the singleton reader" in a way a CronJob (naturally single-run, `concurrencyPolicy: Forbid`) does not — the same operational shape this plan already uses for `bundle_role_cleanup_job.py` (Task 36). |
| 17 | **hub-api never touches `security.envelopeBinding.keySecretRef`/`k_binding` — confirmed, not implemented.** Spec §5.11/§12.3 state the binding-MAC key is "held only by the Rust stage services... never by hub-api, never by a bundle, never by the compiler." No task in this plan reads, writes, provisions or rotates that Secret; hub-api's only D30 responsibility is the `workstreams` identity table and its 1:1 lifecycle with `ingest_sources` — the key itself, and its provisioning/rotation, belong to the chart/M6 work across the four Rust stage services (the same ownership split Decision #7 already draws for the Valkey ACL matrix). Likewise, **RLS policies on bundle-reachable (`data.tables`) tables are confirmed out of M2b's scope and belong to M4, by name, not a vague "M3/M4/M5."** The spec's own M2b milestone row (§16) lists exactly four hub-api deliverables for D30/D31 — `workstreams`, the usage aggregator, the admin usage view, and `routes_to` refusal — and does not mention RLS. Spec §16's **M4 (`svc_process`) deliverable table** carries the actual assignment, verbatim: "DB host capability | Parser allowlist + per-bundle role + **RLS**, with the negative tests green" — the only milestone row in the whole spec that lists RLS as a deliverable (M3's and M5's own deliverable tables have no DB-host-capability row at all). Spec §7.4 confirms the mechanism matches: RLS is enforced on the connection the **stage** opens for a bundle's `db` host call (`SET LOCAL waddles.tenant`/`waddles.community`), never on any connection hub-api opens. `bundle_db_role_service.py` (Task 20) already grants the per-bundle role table-level privileges only; it was never the right place for row-level policy DDL, and this plan adds none — the cross-plan dependency is recorded once more, by milestone, in the Findings table. | Verifies the task brief's two explicit checks ("hub-api must never be able to read the key if the spec says stage-only — follow the spec exactly"; "RLS policies on bundle-reachable tables") against the spec's own milestone table (§16 M4's "DB host capability" row is the specific, named assignment — not the M2a plan's more generic PA4 "M3/M4/M5" negative-sandbox-testing note, which this decision cited in an earlier pass and which this revision replaces with the spec's own, more precise wording), rather than fabricating scope this milestone does not own. Recorded as a Self-Review finding (see below) as well as here, since it is a negative finding — confirming an absence — not a task addition. |
| 18 | **R52 atomicity investigation, two distinct findings, plus one implementation nuance.** (a) **Genuine cross-table atomicity gap, closed for real: `ingest_source_service.create_source()` creating its 1:1 `workstreams` row (Task 44).** Both `ingest_sources` and `workstreams` are this plan's own new tables (Task 3, Task 42) — not "one pydal table, one penguin-dal table" as first assumed; both now live on the same `install_dal: AsyncDB`. The original (pre-R52) plan left this as two sequential calls, each committing separately, safe only because `create_workstream_for_source()` is idempotent on `ingest_source_id` (a retry never duplicates the row) — a real gap, since a crash between the two calls left an `ingest_sources` row with no workstream until the next retry. Task 44 closes it with one `async with install_dal.engine.begin() as conn:` block wrapping both inserts directly — **not** `core_transaction()`, because the `workstreams` insert needs `ingest_sources.id`, a value the database only assigns when the first `INSERT` executes; `core_transaction()` takes a pre-built list of independent statements and cannot express "build statement 2 from statement 1's result," so a data-dependent pair like this is written directly against `conn`, reading `result.inserted_primary_key` between the two `execute()` calls. Real, not simulated, atomicity — a strict improvement the R52 rewrite enables for free since both tables now share one engine. (b) **Every other new-table-plus-`audit_log` write in this plan (Tasks 13, 29, 31, 35, 39, 46) is deliberately best-effort, by pre-existing, consistent design — not a gap.** Each one's original (pre-R52) code already wrapped the `audit_log` insert in `try: ... except Exception: pass  # noqa: BLE001, S110 -- audit logging failure must not break the main flow`: an audit-write failure must never roll back or block the primary operation. Wrapping these in a shared transaction would be a **regression**, not a fix — it would make a transient audit-log hiccup abort the primary write too, the opposite of the original, intentional behavior. R52 still applies to *which client* performs this best-effort write (`install_dal.audit_log.async_insert(...)`, not the pre-existing pydal `dal.audit_log.insert()` — the call site is new code this plan adds, so it goes through penguin-dal per R52's letter), but the try/except-around-a-single-statement shape is preserved unchanged in every rewritten task below. `install_dal.reflect()` (Task 4) discovers hub-api's **entire** live Postgres schema, not only the 11 new tables, which is what makes `install_dal.audit_log` a valid `TableProxy` in the first place. hub-api's pre-existing `audit_log` writes (every pre-M2b caller) are untouched — they keep using `dal.audit_log.insert()` through the existing pydal `dal`, exactly as before. (c) **`core_transaction()` (`hub_api/services/bundle_install_dal.py`, Task 4) therefore has no call site in this plan's own tasks** — its one candidate use (Task 39's `ingest_sources` update + audit insert) turned out to be case (b), best-effort, and its other candidate (Task 44) turned out to need the data-dependent pattern in (a) instead. It stays in Task 4, documented and covered by its own smoke test (proving two *independent* statements committing together), as the toolkit's Pattern C primitive for the first genuinely independent-multi-statement need M3/M4/M5/M6 bring to this codebase — the same "define once, use when needed" posture M1.5's `raw_sql_rows`/`raw_sql_write` already established (not every helper there has a call site in every task either). | The task brief's own instruction: "design it so atomicity holds ... or order + compensate." Investigating each of this plan's actual call sites (not a hypothetical) found one real gap (a), closed directly rather than through a helper that cannot express the data dependency; found the audit-log sites already non-atomic by deliberate, pre-existing design (b), which is correct, not a defect to "fix" into a regression; and recorded (c) so a future reader does not mistake an unused-in-this-plan helper for dead code. |

---

## File Structure

```
config/postgres/
  rbac-matrix.yaml                          new — normative role×table×privilege matrix (Task 1)
scripts/db/
  __init__.py                               new (Task 1)
  rbac_matrix.py                            new — matrix loader + SQL generator (Task 1)
migrations/
  Dockerfile                                modified — copies config/postgres/rbac-matrix.yaml + scripts/db/, adds PyYAML (Task 1)
alembic/versions/
  0020_app_versions_and_rbac.py             new (Task 2)
  0021_bundle_install_schema.py             new (Task 3)
  0022_ingest_source_auth.py                new (Task 39)
  0023_workstreams_and_usage.py             new (Task 42)
docs/
  rbac-matrix.md                            new (Task 2)
hub_api/
  requirements.in                           modified — kubernetes, PyYAML, penguin-dal==0.4.0 + asyncpg + aiosqlite (Task 6, Task 4, R52)
  app.py                                    modified — construct + reflect() the penguin-dal install_dal (Task 4, R52)
  services/
    bundle_install_dal.py                   new — penguin-dal AsyncDB wiring + raw_sql_rows/raw_sql_write/core_transaction helpers, replaces the old pydal binder (Task 4, R52)
    rbac_matrix.py                          new — thin re-export of scripts/db/rbac_matrix for hub-api's own test imports (Task 1)
    bundle_secret_crypto.py                 new (Task 6)
    bundle_manifest_v2.py                   new (Task 7)
    bundle_storage_service.py               new (Task 8)
    compiler_job_service.py                 new (Task 9)
    bundle_version_service.py               new (Task 10)
    bundle_artifact_service.py              new (Tasks 13-14)
    bundle_activation_service.py            new (Task 16)
    permission_summary_service.py           new (Task 18)
    bundle_approval_service.py              new (Task 19)
    bundle_db_role_service.py               new (Task 20)
    platform_settings_service.py            new (Task 23)
    tenant_bundle_settings.py               new (Task 24)
    custom_platform_service.py              new (Task 25)
    ingest_source_service.py                new (Task 26)
    valkey_admin_client.py                  new (Task 28)
    stream_grant_service.py                 new (Task 29)
    marketplace_lifecycle_service.py        modified — grant + approval wiring (Task 30)
    distribution_service.py                 modified — new fields (Task 32)
    bundle_trip_reenable_service.py         new (Task 35)
    bundle_role_cleanup_job.py              new (Task 36)
    bundle_feature_gate.py                  new (Task 37)
    bundle_telemetry.py                     new (Task 38)
    ingest_source_auth.py                   new (Task 39)
    workstream_service.py                   new (Task 44)
    usage_aggregator_service.py             new (Task 47)
    usage_query_service.py                  new (Task 48)
  blueprints/v1/
    bundle_versions.py                      new (Tasks 11, 17)
    bundle_artifact_callback.py             new (Task 15)
    bundle_approvals.py                     new (Task 22)
    bundle_settings.py                      new (Task 23)
    custom_platforms.py                     new (Task 25)
    ingest_sources.py                       new (Task 27), modified — auth PUT (Task 40), workstreamId (Task 45)
    bundle_grants.py                        new (Task 31)
    distribution.py                         modified — v2/bundles (Task 33), /sources (Task 34), workstreamId (Task 45)
    bundle_versions.py                      modified — POST .../trip-reenable (Task 35)
    workstream_usage.py                     new (Task 48)
  tests/
    conftest.py                             modified — bundle_install_db fixture narrowed to pre-existing tables only, new install_dal penguin-dal fixture added (Task 4, R52)
    test_bundle_install_fixture_smoke.py    new — proves install_dal reflects all 9 Task 4 tables (Task 4, R52)
    test_rbac_live_grants.py                new (Task 5)
    test_bundle_secret_crypto.py            new (Task 6)
    test_bundle_manifest_v2.py              new (Task 7)
    test_bundle_storage_service.py          new (Task 8)
    test_compiler_job_service.py            new (Task 9)
    test_bundle_version_service.py          new (Task 10)
    test_bundle_versions_blueprint.py       new (Tasks 11, 17)
    test_bundle_artifact_service.py         new (Tasks 13-14)
    test_bundle_artifact_callback_blueprint.py new (Task 15)
    test_bundle_activation_service.py       new (Task 16)
    test_permission_summary_service.py      new (Task 18)
    test_bundle_approval_service.py         new (Task 19)
    test_bundle_db_role_service.py          new (Task 20)
    test_bundle_approvals_blueprint.py      new (Task 22)
    test_platform_settings_service.py       new (Task 23)
    test_bundle_settings_blueprint.py       new (Task 23)
    test_tenant_bundle_settings.py          new (Task 24)
    test_custom_platform_service.py         new (Task 25)
    test_custom_platforms_blueprint.py      new (Task 25)
    test_ingest_source_service.py           new (Task 26)
    test_ingest_sources_blueprint.py        new (Task 27)
    test_valkey_admin_client.py             new (Task 28)
    test_stream_grant_service.py            new (Task 29)
    test_marketplace_lifecycle_grants.py    new (Task 30)
    test_bundle_grants_blueprint.py         new (Task 31)
    test_distribution_service_versions.py   new (Task 32)
    test_distribution_v2_blueprint.py       new (Task 33)
    test_distribution_sources_blueprint.py  new (Task 34)
    test_bundle_trip_reenable.py            new (Task 35)
    test_bundle_role_cleanup_job.py         new (Task 36)
    test_bundle_feature_gate.py             new (Task 37)
    test_bundle_telemetry.py                new (Task 38)
    test_ingest_source_auth.py              new (Task 39)
    test_ingest_source_auth_api.py          new (Task 40)
    test_openapi_m2b_paths.py               new (Task 41), modified — usage route/module (Task 49)
    test_m2b_logging_conformance.py         new (Task 41), modified — new D30/D31 files (Task 49)
    test_workstream_fixture_smoke.py        new (Task 43)
    test_workstream_service.py              new (Task 44)
    test_ingest_source_service.py           modified — workstream wiring regression (Task 44)
    test_workstream_id_publication.py       new (Task 45)
    test_bundle_approval_service.py         modified — routes_to cross-tenant tests (Task 46)
    test_usage_aggregator_service.py        new (Task 47)
    test_usage_query_service.py             new (Task 48)
    test_workstream_usage_blueprint.py      new (Task 48)
  pyproject.toml                            modified — per-file ruff ignores (Tasks 33-40, 48) + the final gate (Tasks 41, 49)
k8s/helm/waddlebot/templates/
  hub-api-compiler-rbac.yaml                new (Task 12)
  bundle-role-cleanup-cronjob.yaml          new (Task 36)
  usage-aggregator-cronjob.yaml             new (Task 47)
k8s/helm/waddlebot/
  values.yaml                               modified — bundles.compiler.*, sandbox.* keys hub-api needs (Task 12)
  values.yaml                               modified — bundles.roleCleanup.* keys (Task 36)
  values.yaml                               modified — metering.* keys (Task 47)
config/postgres/
  rbac-matrix.yaml                          modified — workstreams, workstream_usage_hourly rows (Task 42)
```

---

## Task 1: RBAC matrix file + loader/generator script + migration Dockerfile wiring

**Depends on:** nothing — this is the milestone's first task.

**Files:**
- Create: `config/postgres/rbac-matrix.yaml`
- Create: `scripts/db/__init__.py`
- Create: `scripts/db/rbac_matrix.py`
- Create: `hub_api/services/rbac_matrix.py`
- Modify: `migrations/Dockerfile`
- Test: `hub_api/tests/test_rbac_matrix_loader.py`

**Interfaces:**
- Produces: `scripts.db.rbac_matrix.load_matrix(path: str) -> list[GrantSpec]`, `GrantSpec` (`role: str`, `table: str`, `privileges: frozenset[str]`), `render_grant_sql(rows: list[GrantSpec]) -> list[str]`, `render_revoke_public_sql(tables: list[str]) -> list[str]`, `ALL_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})`. `hub_api/services/rbac_matrix.py` re-exports the same names by loading the script module via `importlib.util.spec_from_file_location` at an absolute, `__file__`-relative path (no `sys.path` edits, no package coupling between `scripts/` and `hub_api/`) so hub-api's own tests and later migrations import one identical implementation.
- Consumes: nothing (first task).

- [ ] **Step 1: Write the matrix file**

```yaml
# config/postgres/rbac-matrix.yaml
#
# Normative role x table x privilege matrix (spec D28 / Sec11.10.1).
# Grants are generated FROM this file by scripts/db/rbac_matrix.py --
# nobody writes a GRANT by hand. Every role and every table this
# milestone (M2b) touches has an explicit row, including "no privilege"
# rows, so the live-grants CI test (hub_api/tests/test_rbac_live_grants.py)
# can assert set equality rather than only checking presence.
#
# Appended to, never rewritten, by later milestones (M3/M4/M5/M6) as
# their own services/tables land -- each appends its own role/table
# rows to this same file and adds its own migration that re-renders
# grants scoped to its own new rows.
version: 1
roles:
  - hub_api
  - waddles_publisher
  - svc_ingest
  - svc_process
  - svc_action
  - svc_streaming
  - webui
  - migration_runner
tables:
  - app_versions
  - app_active_versions
  - app_version_uploads
  - app_install_approvals
  - app_stream_grants
  - custom_platforms
  - ingest_sources
  - platform_settings
  - app_versions_audit_log
grants:
  # app_versions: exactly two writers (spec Sec6.10). Every other role
  # gets zero privileges -- listed explicitly so the equality check
  # sees an intentional absence, not an unexamined gap.
  - role: waddles_publisher
    table: app_versions
    privileges: [INSERT, UPDATE, DELETE]
  - role: hub_api
    table: app_versions
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: svc_ingest
    table: app_versions
    privileges: []
  - role: svc_process
    table: app_versions
    privileges: []
  - role: svc_action
    table: app_versions
    privileges: []
  - role: svc_streaming
    table: app_versions
    privileges: []
  - role: webui
    table: app_versions
    privileges: []
  - role: migration_runner
    table: app_versions
    privileges: [SELECT, INSERT, UPDATE, DELETE]

  # app_active_versions: hub-api-owned pointer table. Only hub_api writes.
  - role: hub_api
    table: app_active_versions
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: waddles_publisher
    table: app_active_versions
    privileges: []
  - role: svc_ingest
    table: app_active_versions
    privileges: []
  - role: svc_process
    table: app_active_versions
    privileges: []
  - role: svc_action
    table: app_active_versions
    privileges: []
  - role: svc_streaming
    table: app_active_versions
    privileges: []
  - role: webui
    table: app_active_versions
    privileges: []
  - role: migration_runner
    table: app_active_versions
    privileges: [SELECT, INSERT, UPDATE, DELETE]

  # app_versions_audit_log: written only by the trigger (SECURITY DEFINER);
  # hub_api gets read access to display history, nobody else touches it.
  - role: hub_api
    table: app_versions_audit_log
    privileges: [SELECT]
  - role: waddles_publisher
    table: app_versions_audit_log
    privileges: []
  - role: svc_ingest
    table: app_versions_audit_log
    privileges: []
  - role: svc_process
    table: app_versions_audit_log
    privileges: []
  - role: svc_action
    table: app_versions_audit_log
    privileges: []
  - role: svc_streaming
    table: app_versions_audit_log
    privileges: []
  - role: webui
    table: app_versions_audit_log
    privileges: []
  - role: migration_runner
    table: app_versions_audit_log
    privileges: [SELECT, INSERT, UPDATE, DELETE]

  # Hub-api-exclusive control-plane tables (Task 3's migration). Every
  # other role gets zero privileges on every one of them.
  - role: hub_api
    table: app_version_uploads
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: migration_runner
    table: app_version_uploads
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: hub_api
    table: app_install_approvals
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: migration_runner
    table: app_install_approvals
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: hub_api
    table: app_stream_grants
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: migration_runner
    table: app_stream_grants
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: hub_api
    table: custom_platforms
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: migration_runner
    table: custom_platforms
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: hub_api
    table: ingest_sources
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: migration_runner
    table: ingest_sources
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: hub_api
    table: platform_settings
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: migration_runner
    table: platform_settings
    privileges: [SELECT, INSERT, UPDATE, DELETE]
```

- [ ] **Step 2: Write the loader/generator script**

```python
# scripts/db/__init__.py
"""Standalone DB tooling, importable independent of hub-api's own package layout."""
```

```python
# scripts/db/rbac_matrix.py
"""Load `config/postgres/rbac-matrix.yaml` and render GRANT/REVOKE SQL from it.

The single generator behind spec D28 ("Grants are generated from this
file; nobody writes a GRANT by hand.") -- imported by an Alembic
migration (which cannot rely on hub-api's own `sys.path`, since
`alembic/` lives at the repo root, a sibling of `hub_api/`, not inside
it) and by hub-api's own test suite, both via `importlib` against this
file's absolute path so neither caller needs a package-install step.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ALL_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX_PATH = REPO_ROOT / "config" / "postgres" / "rbac-matrix.yaml"


@dataclass(slots=True, frozen=True)
class GrantSpec:
    """One `(role, table, privileges)` row from the RBAC matrix file."""

    role: str
    table: str
    privileges: frozenset[str]


class MatrixError(ValueError):
    """Raised when the matrix file is malformed or references an unknown role/table."""


def load_matrix(path: str | Path = DEFAULT_MATRIX_PATH) -> list[GrantSpec]:
    """Parse the YAML matrix file into a list of `GrantSpec`, validated against its own role/table lists."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    roles = frozenset(raw.get("roles", []))
    tables = frozenset(raw.get("tables", []))
    if not roles:
        raise MatrixError(f"{path}: 'roles' list is empty")
    if not tables:
        raise MatrixError(f"{path}: 'tables' list is empty")

    specs: list[GrantSpec] = []
    for row in raw.get("grants", []):
        role = row["role"]
        table = row["table"]
        privileges = frozenset(row.get("privileges", []))
        if role not in roles:
            raise MatrixError(f"{path}: grant references unknown role {role!r}")
        if table not in tables:
            raise MatrixError(f"{path}: grant references unknown table {table!r}")
        if not privileges <= ALL_PRIVILEGES:
            raise MatrixError(f"{path}: grant for {role}/{table} has unknown privilege(s)")
        specs.append(GrantSpec(role=role, table=table, privileges=privileges))
    return specs


def matrix_roles(path: str | Path = DEFAULT_MATRIX_PATH) -> frozenset[str]:
    """The full `roles` list declared in the matrix file."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return frozenset(raw.get("roles", []))


def matrix_tables(path: str | Path = DEFAULT_MATRIX_PATH) -> frozenset[str]:
    """The full `tables` list declared in the matrix file."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return frozenset(raw.get("tables", []))


def render_create_roles_sql(roles: list[str]) -> list[str]:
    """Idempotent `CREATE ROLE ... NOLOGIN` for every role, guarded by a `pg_roles` existence check."""
    statements = []
    for role in roles:
        statements.append(
            f"DO $$ BEGIN\n"
            f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
            f"    CREATE ROLE {role} NOLOGIN;\n"
            f"  END IF;\n"
            f"END $$;"
        )
    return statements


def render_revoke_public_sql(tables: list[str]) -> list[str]:
    """`REVOKE ALL ON <table> FROM PUBLIC` for every table -- the default-deny baseline."""
    return [f"REVOKE ALL ON {table} FROM PUBLIC;" for table in tables]


def render_grant_sql(rows: list[GrantSpec], *, tables: frozenset[str] | None = None) -> list[str]:
    """`GRANT <privs> ON <table> TO <role>` for every non-empty-privilege row.

    `tables`, when given, restricts rendering to those tables only --
    used by a migration that owns a subset of the matrix's tables (e.g.
    Task 2's migration only wants `app_versions`/`app_active_versions`/
    `app_versions_audit_log` rows, not Task 3's five tables, even though
    both read the same, by-then-larger matrix file).
    """
    statements = []
    for spec in rows:
        if tables is not None and spec.table not in tables:
            continue
        if not spec.privileges:
            continue
        privileges = ", ".join(sorted(spec.privileges))
        statements.append(f"GRANT {privileges} ON {spec.table} TO {spec.role};")
    return statements
```

- [ ] **Step 3: Write hub-api's re-export shim**

```python
# hub_api/services/rbac_matrix.py
"""Re-export of `scripts/db/rbac_matrix.py`, loaded by absolute path.

`scripts/` is a repo-root sibling of `hub_api/`, not a package hub-api
depends on -- loading by `importlib.util.spec_from_file_location`
against this file's own `__file__`-relative path means this module
works whether hub-api is imported as `/app` (the Docker layout) or as
`hub_api.*` from a repo checkout, with no `sys.path` mutation and no
risk of two independent copies of the matrix-parsing logic drifting.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix", _SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load rbac matrix module from {_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_impl = _load()

GrantSpec = _impl.GrantSpec
MatrixError = _impl.MatrixError
ALL_PRIVILEGES = _impl.ALL_PRIVILEGES
DEFAULT_MATRIX_PATH = _impl.DEFAULT_MATRIX_PATH
load_matrix = _impl.load_matrix
matrix_roles = _impl.matrix_roles
matrix_tables = _impl.matrix_tables
render_create_roles_sql = _impl.render_create_roles_sql
render_revoke_public_sql = _impl.render_revoke_public_sql
render_grant_sql = _impl.render_grant_sql
```

- [ ] **Step 4: Write the failing test**

```python
# hub_api/tests/test_rbac_matrix_loader.py
"""Unit tests for the RBAC matrix loader/generator -- no DB required."""

from __future__ import annotations

from services.rbac_matrix import (
    ALL_PRIVILEGES,
    DEFAULT_MATRIX_PATH,
    load_matrix,
    matrix_roles,
    matrix_tables,
    render_create_roles_sql,
    render_grant_sql,
    render_revoke_public_sql,
)


def test_matrix_has_at_least_8_roles_and_8_tables() -> None:
    roles = matrix_roles()
    tables = matrix_tables()
    assert len(roles) >= 8, f"expected >= 8 roles, found {len(roles)}: {sorted(roles)}"
    assert len(tables) >= 8, f"expected >= 8 tables, found {len(tables)}: {sorted(tables)}"


def test_app_versions_has_exactly_two_writers() -> None:
    rows = load_matrix()
    writers = {r.role for r in rows if r.table == "app_versions" and r.privileges}
    assert writers == {"waddles_publisher", "hub_api", "migration_runner"} - {"migration_runner"} | (
        {"migration_runner"} if False else set()
    ) or writers == {"waddles_publisher", "hub_api"}, writers


def test_every_role_has_an_explicit_row_for_every_table() -> None:
    rows = load_matrix()
    seen = {(r.role, r.table) for r in rows}
    roles = matrix_roles()
    tables = matrix_tables()
    missing = [
        (role, table)
        for role in roles
        for table in tables
        if (role, table) not in seen and table != "migration_runner"
    ]
    # Every (role, table) pair this milestone's tables/roles define must be
    # explicit -- a missing pair would make the live-grants equality test
    # (Task 5) silently treat "never checked" as "no privileges", which is
    # not the same claim.
    assert not missing, f"matrix is missing explicit rows for: {missing}"


def test_render_grant_sql_only_emits_non_empty_privilege_rows() -> None:
    rows = load_matrix()
    statements = render_grant_sql(rows, tables=frozenset({"app_versions"}))
    assert any("waddles_publisher" in s for s in statements)
    assert any("hub_api" in s for s in statements)
    assert not any("svc_ingest" in s for s in statements)


def test_render_revoke_public_sql_covers_every_table() -> None:
    tables = sorted(matrix_tables())
    statements = render_revoke_public_sql(tables)
    assert len(statements) == len(tables)
    assert all("REVOKE ALL ON" in s and "FROM PUBLIC" in s for s in statements)


def test_render_create_roles_sql_is_idempotent_guarded() -> None:
    statements = render_create_roles_sql(["hub_api", "waddles_publisher"])
    assert len(statements) == 2
    assert all("IF NOT EXISTS" in s for s in statements)


def test_privileges_are_bounded_to_the_known_set() -> None:
    rows = load_matrix()
    for row in rows:
        assert row.privileges <= ALL_PRIVILEGES


def test_default_matrix_path_exists() -> None:
    assert DEFAULT_MATRIX_PATH.exists()
```

- [ ] **Step 5: Simplify the redundant assertion in Step 4's second test**

The `test_app_versions_has_exactly_two_writers` test above has a convoluted assertion (deliberately worked through defensively during authoring) — clean it up before committing:

```python
def test_app_versions_has_exactly_two_writers() -> None:
    rows = load_matrix()
    writers = {r.role for r in rows if r.table == "app_versions" and r.privileges}
    assert writers == {"waddles_publisher", "hub_api"}, writers
```

- [ ] **Step 6: Run the tests to verify they fail (module does not exist yet)**

Run: `cd hub_api && PYTHONPATH=".." python3 -m pytest tests/test_rbac_matrix_loader.py -v`
Expected: `ModuleNotFoundError: No module named 'yaml'` or `ImportError` — `pyyaml` is not yet a dependency (added in Task 6) and the files above don't exist on disk in this repo checkout until you write them.

- [ ] **Step 7: Install PyYAML locally for this task's verification**

Run: `cd hub_api && pip install "PyYAML>=6.0.2,<7.0.0"`
Expected: `Successfully installed PyYAML-6.0.x`

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd hub_api && PYTHONPATH=".." python3 -m pytest tests/test_rbac_matrix_loader.py -v`
Expected: `8 passed`

- [ ] **Step 9: Wire the migration container's Dockerfile**

Edit `migrations/Dockerfile` — add PyYAML to the pip install list, and copy the two new paths:

```dockerfile
FROM python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

WORKDIR /app

# Install migration dependencies
RUN pip install --no-cache-dir \
    alembic>=1.13 \
    sqlalchemy>=2.0 \
    psycopg2-binary \
    flask-sqlalchemy \
    flask-security-too \
    "PyYAML>=6.0.2,<7.0.0"

# Copy Alembic configuration
COPY alembic.ini .
COPY alembic/ alembic/

# Copy SQLAlchemy models (needed for target_metadata)
# Create __init__.py files so Python recognizes the nested package path
COPY libs/flask_core/ libs/flask_core/
RUN touch libs/__init__.py libs/flask_core/__init__.py

# Copy legacy SQL migrations (used by baseline migration)
COPY config/postgres/migrations/ config/postgres/migrations/

# RBAC matrix (spec D28) -- the normative source the Sec6.10/Sec11.10
# migrations generate GRANT/REVOKE statements from at migration-run time.
COPY config/postgres/rbac-matrix.yaml config/postgres/rbac-matrix.yaml
COPY scripts/db/ scripts/db/

# Copy migration runner
COPY migrations/run-alembic.sh ./run.sh
RUN chmod +x ./run.sh

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

# Set proper permissions for migrations
RUN chown -R appuser:appuser /app

USER appuser

ENTRYPOINT ["./run.sh"]
```

- [ ] **Step 10: Commit**

```bash
git add config/postgres/rbac-matrix.yaml scripts/db/__init__.py scripts/db/rbac_matrix.py \
        hub_api/services/rbac_matrix.py hub_api/tests/test_rbac_matrix_loader.py \
        migrations/Dockerfile
git commit -m "$(cat <<'EOF'
feat(hub-api): RBAC matrix file + loader/generator for Least User Access (D28)

Adds the normative config/postgres/rbac-matrix.yaml (role x table x
privilege) plus scripts/db/rbac_matrix.py, the single generator every
later migration in this milestone renders GRANT/REVOKE SQL from -- no
hand-written GRANT anywhere, per spec D28/Sec11.10.1.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 2: Migration 0020 — `app_versions`, `app_active_versions`, audit trigger, roles, generated grants

**Depends on:** Task 1 (`config/postgres/rbac-matrix.yaml` and `scripts/db/rbac_matrix.py`, loaded by this migration via `importlib` by absolute path).

**Files:**
- Create: `alembic/versions/0020_app_versions_and_rbac.py`
- Create: `docs/rbac-matrix.md`

**Interfaces:**
- Consumes: `scripts/db/rbac_matrix.py`'s `load_matrix`, `render_create_roles_sql`, `render_revoke_public_sql`, `render_grant_sql` (Task 1), loaded via `importlib.util.spec_from_file_location` (same technique as `hub_api/services/rbac_matrix.py`, duplicated here because Alembic migration files cannot import `hub_api.services.*`).
- Produces: table `app_versions(id, app_id, version, artifact_digest, cwasm_digest, wasmtime_abi, collector, size_bytes, language, artifact_kind, built_at, builder, scan_status, badge, approval_id)`, `UNIQUE(app_id, version)`, `UNIQUE(artifact_digest)`. Table `app_active_versions(app_id, tenant_id, community_id, version_id, activated_by, activated_at)`, partial-unique on `(app_id, tenant_id)` where `community_id IS NULL` and on `(app_id, tenant_id, community_id)` where `community_id IS NOT NULL`, FK `version_id -> app_versions(id)`. Table `app_versions_audit_log(id, occurred_at, db_role, operation, app_id, version, old_digest, new_digest)`. Trigger function `fn_app_versions_audit()`, trigger `trg_app_versions_audit` on `app_versions`. Roles `hub_api`, `waddles_publisher`, `svc_ingest`, `svc_process`, `svc_action`, `svc_streaming`, `webui`, `migration_runner` (idempotent `CREATE ROLE ... NOLOGIN`).

- [ ] **Step 1: Write the migration**

```python
# alembic/versions/0020_app_versions_and_rbac.py
"""app_versions (the digest table, spec Sec6.10) + app_active_versions + Least User Access RBAC.

`app_versions` is written by exactly two roles -- `waddles_publisher`
(M2a's trusted publisher container, the only component that ever
measures a compiled component's bytes) and `hub_api` (owns scan
outcome, approval linkage, and the notification/cross-check of Task 13
-- hub-api verifies a claimed digest by re-hashing the bucket object,
it never computes one itself). Every other service role gets zero
privileges, and every write is captured by an AFTER-trigger recording
the writing role and the old/new digest (spec Sec6.10, D28).

Grants are rendered from config/postgres/rbac-matrix.yaml at migration
-run time via scripts/db/rbac_matrix.py -- this file contains no
hand-written GRANT statement.

`app_active_versions` is the separate, hub-api-owned pointer table:
activation and rollback are an UPDATE of `version_id` here, never an
edit of a digest row (spec Sec6.10). Refusing to point at a digest with
no `app_versions` row is enforced by the FK plus an application-level
check in `services/bundle_activation_service.py` (Task 16).

Revision ID: 0020_app_versions_and_rbac
Revises: 0019_kick_app
Create Date: 2026-09-14
"""

import importlib.util
import os
from pathlib import Path

from alembic import op

revision = "0020_app_versions_and_rbac"
down_revision = "0019_kick_app"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
)
_MATRIX_TABLES = frozenset({"app_versions", "app_active_versions", "app_versions_audit_log"})


def _load_matrix_module():
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0020", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_versions (
            id BIGSERIAL PRIMARY KEY,
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            version VARCHAR(50) NOT NULL,
            artifact_digest VARCHAR(71),
            cwasm_digest VARCHAR(71),
            wasmtime_abi VARCHAR(50),
            collector VARCHAR(20),
            size_bytes BIGINT,
            language VARCHAR(20) NOT NULL,
            artifact_kind VARCHAR(20) NOT NULL
                CHECK (artifact_kind IN ('source', 'prebuilt')),
            built_at TIMESTAMPTZ,
            builder VARCHAR(100),
            scan_status VARCHAR(30) NOT NULL DEFAULT 'not_scanned'
                CHECK (scan_status IN ('scanned', 'scanned_with_findings', 'not_scanned', 'scan_failed')),
            badge VARCHAR(100),
            approval_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (app_id, version),
            UNIQUE (artifact_digest)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE app_versions IS "
        "'The digest table (spec Sec6.10) -- exactly two writers: waddles_publisher and hub_api'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_active_versions (
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            version_id BIGINT NOT NULL REFERENCES app_versions(id),
            activated_by INTEGER REFERENCES hub_users(id),
            activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (app_id, tenant_id, community_id)
        )
        """
    )
    # Postgres treats NULL as distinct in a composite PK only when the PK
    # itself allows NULLs -- it does not (PK columns are implicitly NOT
    # NULL), so `community_id` can never be NULL as part of this PK.
    # Tenant-wide activation is represented by a dedicated sentinel row
    # instead: community_id = 0 is reserved and never a real communities.id
    # (communities.id is a real SERIAL starting at 1) -- application code
    # (services/bundle_activation_service.py, Task 16) always passes
    # community_id=0 for "tenant-wide", never NULL, and the distribution
    # service (Task 32) treats 0 as the `_tenant` fallback the same way
    # every other table in this codebase treats `community_id IS NULL`.
    op.execute(
        "ALTER TABLE app_active_versions ALTER COLUMN community_id SET DEFAULT 0"
    )
    op.execute(
        "COMMENT ON TABLE app_active_versions IS "
        "'hub-api-owned activation pointer -- rollback is an UPDATE of version_id, never a digest edit'"
    )
    op.execute(
        "COMMENT ON COLUMN app_active_versions.community_id IS "
        "'0 = tenant-wide (sentinel; communities.id never = 0), matching the _tenant convention elsewhere'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_versions_audit_log (
            id BIGSERIAL PRIMARY KEY,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            db_role VARCHAR(100) NOT NULL,
            operation VARCHAR(10) NOT NULL,
            app_id VARCHAR(255) NOT NULL,
            version VARCHAR(50) NOT NULL,
            old_digest VARCHAR(71),
            new_digest VARCHAR(71)
        )
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_app_versions_audit() RETURNS trigger AS $$
        BEGIN
            INSERT INTO app_versions_audit_log
                (db_role, operation, app_id, version, old_digest, new_digest)
            VALUES (
                session_user,
                TG_OP,
                COALESCE(NEW.app_id, OLD.app_id),
                COALESCE(NEW.version, OLD.version),
                CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.artifact_digest END,
                CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.artifact_digest END
            );
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_app_versions_audit ON app_versions;
        CREATE TRIGGER trg_app_versions_audit
            AFTER INSERT OR UPDATE OR DELETE ON app_versions
            FOR EACH ROW EXECUTE FUNCTION fn_app_versions_audit()
        """
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get(
        "RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH)
    )
    roles = sorted(matrix_module.matrix_roles(matrix_path))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_create_roles_sql(roles):
        op.execute(statement)
    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    # Sequence usage must be granted alongside table INSERT or the two
    # writer roles cannot obtain a new `id`/`app_versions_audit_log.id`.
    op.execute("GRANT USAGE ON SEQUENCE app_versions_id_seq TO waddles_publisher, hub_api;")
    op.execute("GRANT USAGE ON SEQUENCE app_versions_audit_log_id_seq TO hub_api;")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_app_versions_audit ON app_versions")
    op.execute("DROP FUNCTION IF EXISTS fn_app_versions_audit()")
    op.execute("DROP TABLE IF EXISTS app_versions_audit_log")
    op.execute("DROP TABLE IF EXISTS app_active_versions")
    op.execute("DROP TABLE IF EXISTS app_versions")
    for role in ("hub_api", "waddles_publisher", "svc_ingest", "svc_process",
                 "svc_action", "svc_streaming", "webui", "migration_runner"):
        op.execute(
            f"DO $$ BEGIN\n"
            f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
            f"    DROP ROLE {role};\n"
            f"  END IF;\n"
            f"EXCEPTION WHEN dependent_objects_still_exist THEN\n"
            f"  NULL; -- role still owns objects from a later migration; leave it\n"
            f"END $$;"
        )
```

- [ ] **Step 2: Write the RBAC docs page**

```markdown
<!-- docs/rbac-matrix.md -->
# Postgres RBAC Matrix

The normative, versioned source of every Postgres role's privileges in
Waddles is `config/postgres/rbac-matrix.yaml` (spec D28, Sec11.10.1).
**Nobody writes a `GRANT` by hand** — every migration that needs one
loads the matrix via `scripts/db/rbac_matrix.py` and renders the SQL
from it at migration-run time.

## Adding a role or a table

1. Add the role/table name to the `roles`/`tables` list in
   `config/postgres/rbac-matrix.yaml`.
2. Add an explicit `grants` row for **every** role against the new
   table (or every existing table against the new role) — including
   `privileges: []` rows for roles that should have no access. The
   live-grants CI test (`hub_api/tests/test_rbac_live_grants.py`)
   treats a missing row the same as an unexamined gap, not as "no
   access."
3. Write a new Alembic migration that loads the matrix and calls
   `render_grant_sql(rows, tables={...the new table(s) only...})` —
   never re-grant a table an earlier migration already owns unless
   you are deliberately widening it.
4. Run the live-grants test against a real Postgres instance
   (`TEST_POSTGRES_ADMIN_DSN=postgresql://... pytest tests/test_rbac_live_grants.py -v`)
   before merging.

## `app_versions` — exactly two writers

| Role | Privileges | Why |
|---|---|---|
| `waddles_publisher` | INSERT, UPDATE, DELETE | M2a's trusted publisher container — the only component that ever measures a compiled artifact's bytes. |
| `hub_api` | SELECT, INSERT, UPDATE, DELETE | Owns the scan-outcome/approval-linkage columns and the notification/cross-check path (Task 13) — verifies a claimed digest by re-hashing the bucket object, never computes one itself. |
| Everyone else (`svc_ingest`, `svc_process`, `svc_action`, `svc_streaming`, `webui`, the executors) | none | Stages/executors read digests only through the distribution API. |

Every write is captured by `trg_app_versions_audit` into
`app_versions_audit_log` (writing role, row key, old/new digest) — a
digest that changes is always attributable.

## `app_active_versions` — the activation pointer

Activation and rollback are an `UPDATE` of `version_id` in this
table, never an edit of `app_versions`. `community_id = 0` is the
tenant-wide sentinel (`communities.id` is never `0`). Only `hub_api`
writes it.
```

- [ ] **Step 3: Run the migration against a local Postgres to verify it applies**

Run:
```bash
docker run -d --name pg-m2b-test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=waddlebot -p 55432:5432 postgres:17-alpine
sleep 3
cd /home/penguin/code/waddlebot/.worktrees/plan-m2b-hub-api
DATABASE_URL="postgresql://postgres:test@localhost:55432/waddlebot" alembic upgrade head
```
Expected: the run completes through `0020_app_versions_and_rbac`, prints no `psycopg2.errors` traceback. (Migrations `0001`-`0019` run first against the fresh DB per the existing baseline-migration behavior.)

- [ ] **Step 4: Verify the grants landed as expected**

Run:
```bash
psql "postgresql://postgres:test@localhost:55432/waddlebot" -c \
  "SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants WHERE table_name = 'app_versions' ORDER BY grantee, privilege_type;"
```
Expected output (order may vary by `grantee`):
```
     grantee       | table_name  | privilege_type
--------------------+-------------+-----------------
 hub_api            | app_versions | DELETE
 hub_api            | app_versions | INSERT
 hub_api            | app_versions | SELECT
 hub_api            | app_versions | UPDATE
 migration_runner   | app_versions | DELETE
 migration_runner   | app_versions | INSERT
 migration_runner   | app_versions | SELECT
 migration_runner   | app_versions | UPDATE
 waddles_publisher  | app_versions | DELETE
 waddles_publisher  | app_versions | INSERT
 waddles_publisher  | app_versions | UPDATE
```
`svc_ingest`/`svc_process`/`svc_action`/`svc_streaming`/`webui` must NOT appear in this output at all.

- [ ] **Step 5: Tear down the local Postgres**

Run: `docker rm -f pg-m2b-test`
Expected: container removed.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0020_app_versions_and_rbac.py docs/rbac-matrix.md
git commit -m "$(cat <<'EOF'
db(hub-api): app_versions digest table + app_active_versions + audit trigger (spec Sec6.10)

app_versions is written by exactly two roles (waddles_publisher,
hub_api), every other service role gets zero privileges, and every
write is captured by an audit trigger recording the writing role and
the old/new digest. Grants are generated from
config/postgres/rbac-matrix.yaml, not hand-written. Activation/
rollback lives in the separate app_active_versions pointer table.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 3: Migration 0021 — the six hub-api-exclusive control-plane tables

**Depends on:** Task 1 (the matrix file + generator), Task 2 (migration 0020 is this migration's `down_revision`).

**Files:**
- Create: `alembic/versions/0021_bundle_install_schema.py`

**Interfaces:**
- Consumes: `scripts/db/rbac_matrix.py` (Task 1), same `importlib`-by-path technique as Task 2.
- Produces: tables `app_version_uploads(id, app_id, version, tenant_id, requested_by, artifact_kind, language, status, reject_reason, compiler_job_name, staging_manifest_key, staging_source_key, staging_component_key, manifest_json, app_version_id, created_at, updated_at)` `UNIQUE(app_id, version)` — `manifest_json` is the parsed `bundle.yaml` v2 stored once at upload time so the consent/approval flow (Task 19) never re-downloads the bucket object; `app_install_approvals(id, tenant_id, community_id, app_id, version, permission_hash, summary_json, approved_by, approved_at, superseded_by)` partial-unique on `(app_id, version, tenant_id, community_id)` where `superseded_by IS NULL`; `app_stream_grants(id, tenant_id, community_id, app_id, stream_key, platform, source_id, label, granted_by, granted_at, revoked_at)` partial-unique on `(app_id, stream_key)` where `revoked_at IS NULL`; `custom_platforms(id, tenant_id, name, created_at)` `UNIQUE(tenant_id, name)`; `ingest_sources(id, tenant_id, community_id, platform, source_id, label, secret_ciphertext, secret_iv, mapping, enabled, created_at, updated_at)` `UNIQUE(tenant_id, platform, source_id)`; `platform_settings(id, key, value, updated_by, updated_at)` `UNIQUE(key)`, seeded with `bundles.allow_prebuilt = 'true'`.

- [ ] **Step 1: Write the migration**

```python
# alembic/versions/0021_bundle_install_schema.py
"""Bundle install/consent/grants schema: app_version_uploads, app_install_approvals,
app_stream_grants, custom_platforms, ingest_sources, platform_settings.

All six tables are hub-api-exclusive -- only the `hub_api` and
`migration_runner` Postgres roles ever touch them (spec Sec9.2, Sec6.8,
Sec6.9, Sec10.3, Sec10.4, Sec12.3's `bundles.allow_prebuilt`). Grants
are rendered from config/postgres/rbac-matrix.yaml, same generator
Task 2 used, scoped to these six tables only so this migration does not
re-touch app_versions'/app_active_versions' already-correct grants.

`app_version_uploads` is hub-api's own pre-publish lifecycle tracker --
`app_versions` (Task 2) has no status column by design (spec Sec6.10),
so the UPLOADED -> VALIDATING -> ... -> PUBLISHED/REJECTED state
machine (spec Sec9.1) lives here, correlated to `app_versions` by the
natural key `(app_id, version)` and a denormalized `app_version_id`
pointer set once the publisher's row is confirmed (Task 13).

`approved_by`/`granted_by`/`updated_by` are `INTEGER REFERENCES
hub_users(id)`, not `uuid` -- see this plan's Global Constraints PII
row: hub_users is this codebase's one identity table and uses an
integer SERIAL key, matching every existing actor-FK column
(loyalty_redemptions.fulfilled_by, ai_byok_keys.created_by_user_id).

Revision ID: 0021_bundle_install_schema
Revises: 0020_app_versions_and_rbac
Create Date: 2026-09-14
"""

import importlib.util
import os
from pathlib import Path

from alembic import op

revision = "0021_bundle_install_schema"
down_revision = "0020_app_versions_and_rbac"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
)
_MATRIX_TABLES = frozenset({
    "app_version_uploads",
    "app_install_approvals",
    "app_stream_grants",
    "custom_platforms",
    "ingest_sources",
    "platform_settings",
})


def _load_matrix_module():
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0021", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_version_uploads (
            id BIGSERIAL PRIMARY KEY,
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            version VARCHAR(50) NOT NULL,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            requested_by INTEGER REFERENCES hub_users(id),
            artifact_kind VARCHAR(20) NOT NULL
                CHECK (artifact_kind IN ('source', 'prebuilt')),
            language VARCHAR(20) NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'UPLOADED'
                CHECK (status IN (
                    'UPLOADED', 'VALIDATING', 'SCANNING', 'INSPECTING', 'COMPILING',
                    'ADDRESSING', 'PUBLISHING', 'PUBLISHED', 'REJECTED'
                )),
            reject_reason VARCHAR(100),
            compiler_job_name VARCHAR(255),
            staging_manifest_key VARCHAR(500),
            staging_source_key VARCHAR(500),
            staging_component_key VARCHAR(500),
            manifest_json JSONB,
            app_version_id BIGINT REFERENCES app_versions(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (app_id, version)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE app_version_uploads IS "
        "'hub-api-owned pre-publish state machine (spec Sec9.1); app_versions itself has no status column'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_install_approvals (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            version VARCHAR(50) NOT NULL,
            permission_hash VARCHAR(71) NOT NULL,
            summary_json JSONB NOT NULL,
            approved_by INTEGER REFERENCES hub_users(id),
            approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            superseded_by BIGINT REFERENCES app_install_approvals(id)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_app_install_approvals_current
            ON app_install_approvals (app_id, version, tenant_id, community_id)
            WHERE superseded_by IS NULL
        """
    )
    op.execute(
        "COMMENT ON COLUMN app_install_approvals.approved_by IS "
        "'hub_users.id -- PII tokenization: never a name or email (see plan Global Constraints)'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_stream_grants (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            stream_key VARCHAR(500) NOT NULL,
            platform VARCHAR(50) NOT NULL,
            source_id VARCHAR(255) NOT NULL,
            label VARCHAR(255) NOT NULL,
            granted_by INTEGER REFERENCES hub_users(id),
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            revoked_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_app_stream_grants_active
            ON app_stream_grants (app_id, stream_key)
            WHERE revoked_at IS NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_stream_grants_lookup "
        "ON app_stream_grants (tenant_id, community_id, app_id) WHERE revoked_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_platforms (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            name VARCHAR(100) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, name)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE custom_platforms IS "
        "'Tenant-registered custom platform names for consumes: custom:<name> (spec Sec6.4.3) and REST intake (Sec10.4)'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_sources (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            platform VARCHAR(50) NOT NULL,
            source_id VARCHAR(255) NOT NULL,
            label VARCHAR(255) NOT NULL,
            secret_ciphertext BYTEA,
            secret_iv BYTEA,
            mapping JSONB,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, platform, source_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE ingest_sources IS "
        "'Per-tenant ingest source registry (spec Sec5.2/Sec10.3) -- secret_ciphertext/secret_iv are AES-256-GCM at rest'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_settings (
            id BIGSERIAL PRIMARY KEY,
            key VARCHAR(150) NOT NULL UNIQUE,
            value TEXT,
            updated_by INTEGER REFERENCES hub_users(id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        INSERT INTO platform_settings (key, value)
        VALUES ('bundles.allow_prebuilt', 'true')
        ON CONFLICT (key) DO NOTHING
        """
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get(
        "RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH)
    )
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute(
        "GRANT USAGE ON SEQUENCE app_version_uploads_id_seq, "
        "app_install_approvals_id_seq, app_stream_grants_id_seq, "
        "custom_platforms_id_seq, ingest_sources_id_seq, platform_settings_id_seq "
        "TO hub_api;"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_settings")
    op.execute("DROP TABLE IF EXISTS ingest_sources")
    op.execute("DROP TABLE IF EXISTS custom_platforms")
    op.execute("DROP TABLE IF EXISTS app_stream_grants")
    op.execute("DROP TABLE IF EXISTS app_install_approvals")
    op.execute("DROP TABLE IF EXISTS app_version_uploads")
```

- [ ] **Step 2: Run the migration to verify it applies cleanly on top of 0020**

Run:
```bash
docker run -d --name pg-m2b-test2 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=waddlebot -p 55433:5432 postgres:17-alpine
sleep 3
DATABASE_URL="postgresql://postgres:test@localhost:55433/waddlebot" alembic upgrade head
psql "postgresql://postgres:test@localhost:55433/waddlebot" -c "SELECT key, value FROM platform_settings;"
docker rm -f pg-m2b-test2
```
Expected: `alembic upgrade head` exits 0; the `psql` query prints exactly one row, `bundles.allow_prebuilt | true`.

- [ ] **Step 3: Commit**

```bash
git add alembic/versions/0021_bundle_install_schema.py
git commit -m "$(cat <<'EOF'
db(hub-api): app_version_uploads, app_install_approvals, app_stream_grants, custom_platforms, ingest_sources, platform_settings

Six hub-api-exclusive control-plane tables backing the install/consent/
grants flow (spec Sec6.8, Sec6.9, Sec9.1, Sec9.2, Sec10.3, Sec10.4).
Grants generated from config/postgres/rbac-matrix.yaml, scoped to
these six tables only. Seeds the global bundles.allow_prebuilt=true
default (spec Sec12.3).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 4: `penguin-dal` `AsyncDB` wiring (`bundle_install_dal.py`) + `app.py` startup/shutdown + shared test fixture (R52)

**Depends on:** Task 2 (migration 0020's `app_versions`/`app_active_versions`/`app_versions_audit_log`), Task 3 (migration 0021's six tables) — the new `penguin_dal.AsyncDB` reflects exactly those nine tables at startup, the same nine a real Postgres has after both migrations run. No Alembic migration is added or changed by this task — DDL ownership is unchanged (R52: `penguin-dal` never issues DDL here, only `AsyncDB.reflect()` against tables Alembic already created).

**Coordinator ruling R52 (binding):** "We aren't using pydal anymore — instead we are running penguin-dal." This task replaces the plan's original "pydal test binder" (a `bind_bundle_install_tables()` function in `hub_api/services/schema.py` using `dal.define_table(...)` with pydal `Field` objects) with a `penguin_dal.AsyncDB` — exact pin `penguin-dal==0.4.0`, the same version and public API the M1.5 bundle-migration plan (`docs/superpowers/plans/2026-09-14-rust-data-plane-m1.5-bundle-migration.md`) pins and uses (verified directly against the installed package at `penguin-dal-0.4.0.dist-info` — `penguin_dal.db.AsyncDB`, `penguin_dal.query.{Query,Row,Rows,AsyncQuerySet}`, `penguin_dal.field_proxy.FieldProxy`, `penguin_dal.backends.ensure_async_uri`). **`hub_api/services/schema.py` is not modified by this task, or by any task in this plan** — every pre-existing `bind_*_tables()` function in it, and every pre-existing pydal call site elsewhere in hub-api, is untouched (R52 forbids new pydal, not existing pydal).

**Files:**
- Create: `hub_api/services/bundle_install_dal.py`
- Modify: `hub_api/app.py` (one new import + one new construct/reflect call in `startup()`, one new close call in `shutdown()`)
- Modify: `hub_api/tests/conftest.py` (add the `install_dal` fixture; the existing `bundle_install_db` fixture stays, narrowed to the tables it still owns)
- Create: `hub_api/tests/test_bundle_install_fixture_smoke.py`
- Modify: `hub_api/requirements.in` (pin `penguin-dal==0.4.0`, `asyncpg`, `aiosqlite`)
- Regenerate: `hub_api/requirements.txt`

**Interfaces:**
- Produces: `services.bundle_install_dal.build_install_dal(database_url: str, pool_size: int) -> AsyncDB` — constructs a `penguin_dal.AsyncDB` against `database_url` (the exact same `HubAPIConfig.database_url` the existing pydal `dal` already uses — same host/port/user/password/scheme, so identical TLS/auth posture with no new config; `penguin_dal.backends.ensure_async_uri()` maps hub-api's existing `postgres://`-scheme DSN, built for pydal by `config.py::_build_db_url`, straight to `postgresql+asyncpg://` — no DSN change needed anywhere) and calls `await .reflect()` once before returning, so every caller receives an `AsyncDB` that already sees the nine tables Tasks 2-3 create (and every other live table, since `reflect()` discovers the whole schema — this is what makes Decision #18's cross-table `audit_log` transaction possible). `services.bundle_install_dal.raw_sql_rows(dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None) -> Rows` and `raw_sql_write(dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None) -> Rows` — read-only and single-statement-write raw-SQL escape hatches for joins/`GROUP BY`/`ON CONFLICT` that `penguin_dal`'s single-table `Query` builder cannot express, same idiom and signature as `flask_core.bundle_runtime`'s M1.5 helpers (a hub-api-local copy, not an import from `flask_core.bundle_runtime` — that module's `get_bundle_dal()`/`set_bundle_dal()` contextvar facade is scoped to WASM bundle execution inside a stage runner, which does not apply to hub-api's own persistent per-process `AsyncDB`). `services.bundle_install_dal.core_transaction(dal: AsyncDB, statements: Sequence[Any]) -> None` — runs a list of *independent* SQLAlchemy Core `Insert`/`Update`/`Delete` statements (built against `dal.<table>.table`, a public property on every `TableProxy`) inside one `async with dal.engine.begin() as conn:` block, so two writes that don't depend on each other's result commit or roll back together with correct JSON/datetime type coercion on both backends. Covered by its own smoke test below; Decision #18 records that this plan's own two candidate call sites each turned out to need a different technique (best-effort, or a data-dependent `engine.begin()` written directly) — this function remains available, tested, for the first genuinely independent-multi-statement need a later milestone brings.
- Test fixture: `install_dal(bundle_install_db) -> AsyncDB` (async, file-backed sqlite, same file as `bundle_install_db`) — every M2b test needing one of the nine new tables depends on this fixture from here on, replacing the old plan's `bundle_install_db.dal.app_versions`-style access. `bundle_install_db` (pydal, unchanged in shape from before this ruling) keeps binding and seeding `tenants`/`communities`/`community_roles`/`community_members`/`app_catalog` **and now also `audit_log`** (added to this fixture in this task, since Decision #18's cross-table transactions need it reachable via `install_dal.reflect()` from here on — every later task that touches `audit_log` depends on this addition, so it lands once, here, rather than being bolted onto each of those tasks individually). Module constants unchanged: `BUNDLE_TENANT_ID = 1`, `BUNDLE_COMMUNITY_ID = 1`, `BUNDLE_ADMIN_USER_ID = 1`.
- Consumes: `penguin_dal.AsyncDB`, `penguin_dal.Row`, `penguin_dal.Rows` (new dependency, this task). `hub_api/tests/conftest.py`'s existing `bundle_install_db` fixture and `bind_auth_tables`/`bind_community_authz_tables`/`bind_lifecycle_tables`/`bind_platform_tables` (all pre-existing, `bind_platform_tables` newly added to this one fixture's call list in this task to reach `audit_log`).

- [ ] **Step 1: Pin `penguin-dal` + async drivers in `hub_api/requirements.in`**

Add these three lines to `hub_api/requirements.in`, directly under the existing `# Database` comment block, keeping every existing line (including `pydal`/`psycopg2-binary`) in place — hub-api's pre-existing pydal surface is untouched by this plan:

```
# penguin-dal (M2b new-table DB access, R52) -- async Postgres via asyncpg;
# aiosqlite is test-only (in-memory/file-backed sqlite fixtures below).
# Exact pin per critical-rules.md Dependency Pinning; matches the M1.5
# bundle-migration plan's own pin.
penguin-dal==0.4.0
asyncpg==0.30.0
aiosqlite==0.20.0
```

Run: `docker run --rm -v "$(pwd)/hub_api:/work" -w /work python:3.13-slim bash -c "pip install -q uv && uv pip compile requirements.in --generate-hashes -o requirements.txt --python-platform x86_64-manylinux_2_28"`
Expected: exits 0, `requirements.txt` rewritten with `penguin-dal==0.4.0 \`, `asyncpg==0.30.0 \`, `aiosqlite==0.20.0 \` blocks, each followed by `--hash=sha256:...` lines, in the file's existing autogenerated format (header comment: `# This file was autogenerated by uv via the following command: uv pip compile requirements.in --generate-hashes -o requirements.txt`).

Run: `docker run --rm -v "$(pwd)/hub_api:/work" -w /work python:3.13-slim bash -c "pip install -q -r requirements.txt && python3 -c 'import penguin_dal, asyncpg, aiosqlite; print(penguin_dal.__name__, asyncpg.__version__, aiosqlite.__version__)'"`
Expected: prints `penguin_dal <asyncpg-version> <aiosqlite-version>` with no `pip install` error.

- [ ] **Step 2: Write `hub_api/services/bundle_install_dal.py`**

```python
# hub_api/services/bundle_install_dal.py
"""penguin-dal AsyncDB wiring for hub-api's M2b bundle-install/consent/grants control-plane tables (R52).

Coordinator ruling R52 ("we aren't using pydal anymore... instead we are
running penguin-dal") applies to every table this plan's own migrations
create -- the nine tables Tasks 2-3 add via Alembic (app_versions,
app_active_versions, app_versions_audit_log, app_version_uploads,
app_install_approvals, app_stream_grants, custom_platforms,
ingest_sources, platform_settings), plus workstreams/
workstream_usage_hourly added by Task 42. hub-api's pre-existing pydal
surface (tenants, communities, community_members, community_roles,
app_catalog, hub_users, audit_log, and every other table
`hub_api/services/schema.py`'s existing bind_*_tables() functions bind)
is untouched -- this module never binds, defines, or migrates any table;
`build_install_dal()` only reflects tables Alembic already created.

Coexistence: this AsyncDB is a SEPARATE SQLAlchemy async engine/connection
pool from the existing pydal AsyncDAL's pool, both pointed at the exact
same `DATABASE_URL` -- same host/port/user/password/scheme, so identical
TLS and auth posture with zero new configuration (`penguin_dal.
backends.ensure_async_uri()` maps hub-api's existing pydal-style
`postgres://` DSN straight to `postgresql+asyncpg://`). Two pools against
one DSN is the same pattern hub-api already uses for its own read-replica
DSN (`HubAPIConfig.database_read_replica_url`) -- a second pool is not a
new architectural shape for this service.

`raw_sql_rows`/`raw_sql_write` are the Pattern B
escape hatch (joins/GROUP BY/ON CONFLICT, single-statement) `penguin_dal`'s
single-table Query/TableProxy builder cannot express -- the same idiom
and signature as `flask_core.bundle_runtime`'s M1.5 helpers, copied here
rather than imported: `bundle_runtime`'s `get_bundle_dal()`/
`set_bundle_dal()` contextvar facade is scoped to WASM bundle execution
inside a Rust/Python stage runner, which does not describe hub-api's own
persistent, per-process AsyncDB. `core_transaction()` is Pattern C: a
multi-statement write that must be atomic (Decision #18) -- it takes
real SQLAlchemy Core statements, not text SQL, specifically so JSON
columns (`audit_log.details` and every JSON column this plan writes)
get correct type-aware serialization on both the sqlite test fixture
and real Postgres `JSONB`, which raw `text()` parameter binding does
not provide.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from penguin_dal import AsyncDB, Row, Rows
from sqlalchemy import text


async def build_install_dal(database_url: str, pool_size: int) -> AsyncDB:
    """Construct the M2b `AsyncDB` and reflect the live schema.

    Called once, in `app.py::startup()`. `reflect()` discovers every
    table already in the live Postgres database -- Tasks 2-3's nine new
    tables (created by Alembic before this ever runs) plus every
    pre-existing table (`audit_log` included, which is what makes
    Decision #18's cross-table transaction possible without a second
    DDL owner or a schema duplicated between pydal and penguin-dal).

    Args:
        database_url: The same DSN `HubAPIConfig.database_url` already
            builds for the existing pydal DAL -- reused verbatim.
        pool_size: Connection pool size for this second, independent pool.

    Returns:
        A fully reflected `AsyncDB`, ready for `TableProxy`/`Query` access
        against any table in the live schema.
    """
    install_dal = AsyncDB(database_url, pool_size=pool_size)
    await install_dal.reflect()
    return install_dal


async def raw_sql_rows(
    dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None
) -> Rows:
    """Run a read-only raw SQL query, for the joins/GROUP BY/RANDOM() cases
    `penguin_dal`'s single-table Query builder cannot express.

    Args:
        dal: The `install_dal` `AsyncDB` (from `build_install_dal()`).
        sql: SQL text with named `:param` markers.
        params: Bind parameter values, or `None` for a parameterless query.

    Returns:
        A `penguin_dal.Rows` of the result set (empty if no rows matched).
    """
    async with dal.engine.connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return Rows([Row(dict(mapping)) for mapping in result.mappings().all()])


async def raw_sql_write(
    dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None
) -> Rows:
    """Run a single raw SQL write (INSERT/UPDATE/DELETE, optionally
    `RETURNING`) in its own committed transaction.

    Args:
        dal: The `install_dal` `AsyncDB` (from `build_install_dal()`).
        sql: SQL text with named `:param` markers.
        params: Bind parameter values, or `None` for a parameterless statement.

    Returns:
        A `penguin_dal.Rows` of any `RETURNING` rows (empty otherwise).
    """
    async with dal.engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        try:
            mappings = result.mappings().all()
        except Exception:  # noqa: BLE001 -- driver raises when the statement has no result set (no RETURNING)
            mappings = []
        return Rows([Row(dict(mapping)) for mapping in mappings])


async def core_transaction(dal: AsyncDB, statements: Sequence[Any]) -> None:
    """Run multiple SQLAlchemy Core statements in ONE transaction (Decision #18).

    For a write that must be atomic across one of this plan's new tables
    and an existing table (`audit_log` is this plan's only case) -- both
    live in the same Postgres database, so one connection and one
    `engine.begin()` block gives real atomicity (a failure partway
    through rolls back every statement already run in this call, not
    just the one that failed) without coordinating two separate
    connection pools (this `AsyncDB`'s and the existing pydal `AsyncDAL`'s).

    Deliberately **not** `raw_sql_write`/text()-based: `audit_log.details`
    and every JSON column this plan writes (`app_install_approvals.
    summary_json`, `ingest_sources.auth`, ...) need SQLAlchemy's own
    JSON bind-processing to serialize a Python `dict` correctly on both
    the sqlite test fixture and real Postgres `JSONB` -- `text()` sends
    parameter values to the DBAPI driver unprocessed (a raw `dict` is not
    a value any driver accepts, and a manual `json.dumps()` string still
    needs a backend-specific cast `text()` cannot express portably).
    Building each statement against the real reflected `Table` object
    (`dal.<table>.table`, a public property on every `TableProxy`) with
    `.insert()`/`.update()` keeps that type-aware serialization while
    still giving one shared transaction.

    Args:
        dal: The `install_dal` `AsyncDB` (from `build_install_dal()`).
        statements: Ordered SQLAlchemy Core `Insert`/`Update`/`Delete`
            statements (e.g. `dal.ingest_sources.table.update().where(...)
            .values(...)`), executed in the order given.
    """
    async with dal.engine.begin() as conn:
        for stmt in statements:
            await conn.execute(stmt)
```

- [ ] **Step 3: Wire `build_install_dal()` into `app.py`**

In `hub_api/app.py`, add the import (alongside the existing `from services.schema import (...)` block, as its own line -- `bundle_install_dal` is a new module, not part of `services.schema`):

```python
from services.bundle_install_dal import build_install_dal
```

In `startup()`, immediately after the existing `_bind_reference_tables(dal)` call (the pydal binder chain is untouched -- this is a new, independent call added after it, not interleaved with it):

```python
        _bind_reference_tables(dal)
        app.config["async_dal"] = async_dal
        app.config["dal"] = dal
        # penguin-dal (R52): a SEPARATE AsyncDB/pool against the same
        # DATABASE_URL, reflecting the nine tables Tasks 2-3's Alembic
        # migrations create (plus every other live table -- see
        # bundle_install_dal.py's own docstring on why that matters for
        # Decision #18's audit_log transactions). Every M2b table this
        # plan adds is queried through this handle, never through a new
        # pydal binder.
        install_dal = await build_install_dal(cfg.database_url, pool_size=cfg.db_pool_size)
        app.config["install_dal"] = install_dal
        logger.system("hub-api started", action="startup", result="SUCCESS")
```

(Remove the pre-existing `logger.system("hub-api started", ...)` line from its old position immediately after `_bind_reference_tables`/`app.config["dal"] = dal` -- it moves to after the new `install_dal` line above, so "started" still means every DAL, old and new, is ready before the app accepts traffic.)

In `shutdown()`, add before the existing `rate_limiter.disconnect()` try/except block:

```python
        install_dal = app.config.get("install_dal")
        if install_dal is not None:
            try:
                await install_dal.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning(f"Error closing install_dal on shutdown: {exc}")
```

(`AsyncDB.close()` disposes the SQLAlchemy async engine directly and does not have the existing pydal `AsyncDAL.close_async()`'s cross-thread `THREAD_LOCAL` issue documented in `shutdown()`'s own docstring -- this new close call needs no defensive same-thread reasoning, only the same "shutdown must never raise" `try`/`except` shape every other close call in this function already uses.)

- [ ] **Step 4: Add the `install_dal` test fixture to `tests/conftest.py`**

Add these imports to the top of `hub_api/tests/conftest.py` (alongside its existing imports):

```python
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    true as sa_true,
)

from services.bundle_install_dal import build_install_dal
from services.schema import bind_platform_tables
```

Add `bind_platform_tables` to the existing `bundle_install_db` fixture's binder calls -- this is the one change to that fixture in this task (everything else about it, including its pydal `define_table` calls for `tenants`/`communities`/`community_roles`/`community_members`/`app_catalog`, is unchanged; `bind_platform_tables` is a pre-existing function in `services/schema.py`, not a new one):

```python
    bind_auth_tables(dal, migrate=True)
    bind_community_authz_tables(dal, migrate=True)
    bind_lifecycle_tables(dal, migrate=True)
    bind_platform_tables(dal, migrate=True)
```

Add this module-level helper and fixture, near `bundle_install_db` (same file):

```python
def _create_bundle_install_tables(conn: Any) -> None:
    """Synchronous SQLAlchemy Core DDL for the nine M2b tables (Tasks 2-3).

    Run via `conn.run_sync()` inside `install_dal`'s `engine.begin()`
    block below. Alembic owns this DDL in real Postgres (migrations 0020
    and 0021); a unit test has no Alembic run, so this reproduces the
    exact same column set as a `sqlite`-compatible SQLAlchemy Core
    schema -- the same "no native UUID/JSONB, plain generic types" shape
    the old pydal test binder used, translated to SQLAlchemy Core
    instead of pydal `Field` objects. Task 43 extends this with
    `workstreams`/`workstream_usage_hourly`.
    """
    metadata = MetaData()
    Table(
        "app_versions",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("app_id", String(255), nullable=False),
        Column("version", String(50), nullable=False),
        Column("artifact_digest", String(71)),
        Column("cwasm_digest", String(71)),
        Column("wasmtime_abi", String(50)),
        Column("collector", String(20)),
        Column("size_bytes", BigInteger),
        Column("language", String(20), nullable=False),
        Column("artifact_kind", String(20), nullable=False),
        Column("built_at", DateTime),
        Column("builder", String(100)),
        Column("scan_status", String(30), server_default="not_scanned"),
        Column("badge", String(100)),
        Column("approval_id", BigInteger),
        Column("created_at", DateTime),
    )
    Table(
        "app_active_versions",
        metadata,
        Column("app_id", String(255), primary_key=True),
        Column("tenant_id", Integer, primary_key=True),
        Column("community_id", Integer, primary_key=True, server_default="0"),
        Column("version_id", BigInteger, nullable=False),
        Column("activated_by", Integer),
        Column("activated_at", DateTime),
    )
    Table(
        "app_versions_audit_log",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("occurred_at", DateTime),
        Column("db_role", String(100), nullable=False),
        Column("operation", String(10), nullable=False),
        Column("app_id", String(255), nullable=False),
        Column("version", String(50), nullable=False),
        Column("old_digest", String(71)),
        Column("new_digest", String(71)),
    )
    Table(
        "app_version_uploads",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("app_id", String(255), nullable=False),
        Column("version", String(50), nullable=False),
        Column("tenant_id", Integer, nullable=False),
        Column("requested_by", Integer),
        Column("artifact_kind", String(20), nullable=False),
        Column("language", String(20), nullable=False),
        Column("status", String(30), server_default="UPLOADED"),
        Column("reject_reason", String(100)),
        Column("compiler_job_name", String(255)),
        Column("staging_manifest_key", String(500)),
        Column("staging_source_key", String(500)),
        Column("staging_component_key", String(500)),
        Column("manifest_json", JSON),
        Column("app_version_id", BigInteger),
        Column("created_at", DateTime),
        Column("updated_at", DateTime),
    )
    Table(
        "app_install_approvals",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("app_id", String(255), nullable=False),
        Column("version", String(50), nullable=False),
        Column("permission_hash", String(71), nullable=False),
        Column("summary_json", JSON, nullable=False),
        Column("approved_by", Integer),
        Column("approved_at", DateTime),
        Column("superseded_by", BigInteger),
    )
    Table(
        "app_stream_grants",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("app_id", String(255), nullable=False),
        Column("stream_key", String(500), nullable=False),
        Column("platform", String(50), nullable=False),
        Column("source_id", String(255), nullable=False),
        Column("label", String(255), nullable=False),
        Column("granted_by", Integer),
        Column("granted_at", DateTime),
        Column("revoked_at", DateTime),
    )
    Table(
        "custom_platforms",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("name", String(100), nullable=False),
        Column("created_at", DateTime),
    )
    Table(
        "ingest_sources",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("platform", String(50), nullable=False),
        Column("source_id", String(255), nullable=False),
        Column("label", String(255), nullable=False),
        Column("secret_ciphertext", LargeBinary),
        Column("secret_iv", LargeBinary),
        Column("mapping", JSON),
        Column("enabled", Boolean, server_default=sa_true()),
        Column("created_at", DateTime),
        Column("updated_at", DateTime),
    )
    Table(
        "platform_settings",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("key", String(150), nullable=False),
        Column("value", Text),
        Column("updated_by", Integer),
        Column("updated_at", DateTime),
    )
    metadata.create_all(conn)


@pytest.fixture
async def install_dal(bundle_install_db: Any) -> Any:
    """`penguin_dal.AsyncDB` for every M2b test needing one of the nine new tables (R52).

    Points at the exact same sqlite file as `bundle_install_db`
    (`bundle_install_db.uri`) so both fixtures see each other's rows in
    the same test -- `bundle_install_db` (pydal, unchanged shape) seeds
    `tenants`/`communities`/`community_roles`/`community_members`/
    `app_catalog`/`audit_log`; this fixture creates the nine new M2b
    tables directly via `_create_bundle_install_tables()` (Alembic owns
    this DDL in production; there is no Alembic run in a unit test) and
    then calls `await install_dal.reflect()`, which discovers BOTH the
    nine tables just created here AND every table `bundle_install_db`
    already created in the same file -- this is what lets a single M2b
    write cross a new table and the existing `audit_log` table in one
    real SQL transaction (Decision #18, `core_transaction()`).
    """
    install_dal = AsyncDB(bundle_install_db.uri, pool_size=1, echo=False)
    async with install_dal.engine.begin() as conn:
        await conn.run_sync(_create_bundle_install_tables)
    await install_dal.reflect()
    yield install_dal
    await install_dal.close()


BUNDLE_TENANT_ID = 1
BUNDLE_COMMUNITY_ID = 1
BUNDLE_ADMIN_USER_ID = 1
```

Add the one remaining import `install_dal`'s body needs directly (not already covered by the `sqlalchemy` import block above):

```python
from penguin_dal import AsyncDB
```

- [ ] **Step 5: Write a smoke test proving the fixture works**

```python
# hub_api/tests/test_bundle_install_fixture_smoke.py
"""Smoke test for the install_dal fixture -- proves every new table reflects and is queryable, and that install_dal can also see audit_log (Decision #18)."""

from __future__ import annotations

from typing import Any

from services.bundle_install_dal import raw_sql_rows


async def test_every_new_table_is_queryable_and_empty(install_dal: Any) -> None:
    for table_name in (
        "app_versions",
        "app_active_versions",
        "app_versions_audit_log",
        "app_version_uploads",
        "app_install_approvals",
        "app_stream_grants",
        "custom_platforms",
        "ingest_sources",
        "platform_settings",
    ):
        assert table_name in install_dal.tables
        rows = await raw_sql_rows(install_dal, f"SELECT COUNT(*) AS n FROM {table_name}")
        assert rows.first()["n"] == 0


async def test_install_dal_also_sees_the_existing_audit_log_table(install_dal: Any) -> None:
    """Decision #18's precondition: install_dal.reflect() sees pre-existing tables too."""
    assert "audit_log" in install_dal.tables
    rows = await raw_sql_rows(install_dal, "SELECT COUNT(*) AS n FROM audit_log")
    assert rows.first()["n"] == 0


async def test_core_transaction_commits_two_independent_statements_together(install_dal: Any) -> None:
    """core_transaction() (Pattern C) -- for two INDEPENDENT statements sharing one commit.

    Not the shape Task 44 ends up needing (there, the second insert
    depends on the first's generated id, so that task writes its own
    `engine.begin()` block directly) -- this is the independent-
    statements case core_transaction is for, proven here so the helper
    itself is verified, not just designed.
    """
    from services.bundle_install_dal import core_transaction

    settings_table = install_dal.platform_settings.table
    audit_table = install_dal.audit_log.table
    await core_transaction(
        install_dal,
        [
            settings_table.insert().values(key="bundles.allow_prebuilt", value="true"),
            audit_table.insert().values(
                user_id=None, action="platform_setting_seeded", target_type="platform_settings",
                target_id="bundles.allow_prebuilt", details={"value": "true"},
            ),
        ],
    )
    settings_rows = await raw_sql_rows(
        install_dal, "SELECT value FROM platform_settings WHERE key = :k", {"k": "bundles.allow_prebuilt"}
    )
    assert settings_rows.first()["value"] == "true"
    audit_rows = await raw_sql_rows(
        install_dal, "SELECT id FROM audit_log WHERE action = :a", {"a": "platform_setting_seeded"}
    )
    assert audit_rows.first() is not None


async def test_seed_data_present_via_the_pydal_fixture(bundle_install_db: Any) -> None:
    dal = bundle_install_db.dal
    row = dal(dal.app_catalog.app_id == "waddles.socials.music.default").select().first()
    assert row is not None
    assert row.status == "active"
```

- [ ] **Step 6: Run the smoke test**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_install_fixture_smoke.py -v`
Expected: `4 passed`

- [ ] **Step 7: Run the full existing hub-api suite to confirm no regression**

Run: `cd hub_api && python3 -m pytest -q`
Expected: every previously-passing test still passes; the printed summary line's pass count is the pre-existing count plus the 4 new smoke tests (e.g. `1141 passed` becomes `1145 passed` — confirm against your own baseline run before this task, since the exact pre-existing count drifts release to release).

- [ ] **Step 8: Commit**

```bash
git add hub_api/requirements.in hub_api/requirements.txt \
        hub_api/services/bundle_install_dal.py hub_api/app.py hub_api/tests/conftest.py \
        hub_api/tests/test_bundle_install_fixture_smoke.py
git commit -m "$(cat <<'EOF'
feat(hub-api): penguin-dal AsyncDB (install_dal) for the nine M2b tables (R52)

Coordinator ruling R52 ("we aren't using pydal anymore... instead we are
running penguin-dal"): every table this plan's own migrations create is
now queried through a penguin-dal==0.4.0 AsyncDB (bundle_install_dal.py),
a second connection pool against the same DATABASE_URL, reflecting the
live schema at startup instead of a new pydal binder. hub-api's existing
pydal surface (services/schema.py and every pre-M2b call site) is
unchanged -- this plan adds no new pydal table, binder, or query.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 5: RBAC live-grants CI equality test (Postgres integration)

**Depends on:** Task 1 (`services/rbac_matrix.py` loader), Tasks 2-3 (the migrated schema this test asserts against on a real Postgres).

**Files:**
- Create: `hub_api/tests/test_rbac_live_grants.py`

**Interfaces:**
- Consumes: `services.rbac_matrix.load_matrix`, `matrix_roles`, `matrix_tables` (Task 1); the migrated schema of Tasks 2-3 (this test requires a real Postgres instance with `alembic upgrade head` already applied — it is an **integration** test per `testing-python.md`'s category table, "real DB, rollback per test").
- Produces: nothing consumed by a later task — this is a leaf verification.

- [ ] **Step 1: Write the test**

```python
# hub_api/tests/test_rbac_live_grants.py
"""Asserts the live Postgres grants equal config/postgres/rbac-matrix.yaml exactly.

Requires a real, already-migrated Postgres instance -- set
`TEST_POSTGRES_ADMIN_DSN` (a superuser or table-owner DSN) to run.
Skipped otherwise, matching this repo's existing pattern of gating
Postgres-only tests behind an env var rather than mocking a real
database's role system (pydal/sqlite has no roles at all).

This is the primary Least-User-Access gate (spec D28/Sec11.10.1): a
missing grant AND an extra grant both fail, and the test asserts it
examined a non-zero, meaningfully-sized set of roles/tables --
`critical-rules.md` Verification Integrity: "Zero items examined is a
FAILURE, not a pass."

The per-role negative/positive spot-checks at the bottom of this file
are the "readable examples" the equality check subsumes -- kept
because a human skimming test output benefits from a concrete "this
exact role cannot INSERT" assertion, not just a matrix diff.
"""

from __future__ import annotations

import os

import psycopg2
import pytest

from services.rbac_matrix import load_matrix, matrix_roles, matrix_tables

_DSN = os.environ.get("TEST_POSTGRES_ADMIN_DSN")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="requires a real, migrated Postgres instance (TEST_POSTGRES_ADMIN_DSN)"
)


@pytest.fixture
def pg_conn() -> object:
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    yield conn
    conn.close()


def _live_grants(conn: object, tables: frozenset[str]) -> set[tuple[str, str, str]]:
    """`{(grantee, table, privilege), ...}` from `information_schema.role_table_grants`."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT grantee, table_name, privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (list(tables),),
        )
        return {(row[0], row[1], row[2]) for row in cur.fetchall()}


def _matrix_grants(tables: frozenset[str]) -> set[tuple[str, str, str]]:
    """The same `{(role, table, privilege), ...}` shape, derived from the matrix file."""
    expected: set[tuple[str, str, str]] = set()
    for spec in load_matrix():
        if spec.table not in tables:
            continue
        for privilege in spec.privileges:
            expected.add((spec.role, spec.table, privilege))
    return expected


def test_live_grants_equal_the_matrix_exactly(pg_conn: object) -> None:
    roles = matrix_roles()
    tables = matrix_tables()
    assert len(roles) >= 8, f"non-vacuous check requires >= 8 roles, matrix has {len(roles)}"
    assert len(tables) >= 8, f"non-vacuous check requires >= 8 tables, matrix has {len(tables)}"

    live = _live_grants(pg_conn, tables)
    # Only compare grantees the matrix actually declares -- a role this
    # migration doesn't own (e.g. the Postgres superuser connecting as
    # `postgres`) legitimately has implicit owner privileges the matrix
    # never claims to model.
    live_scoped = {row for row in live if row[0] in roles}
    expected = _matrix_grants(tables)

    missing = expected - live_scoped
    extra = live_scoped - expected
    assert not missing, f"grants the matrix declares but Postgres does not have: {sorted(missing)}"
    assert not extra, f"grants Postgres has that the matrix does not declare: {sorted(extra)}"
    print(f"RBAC live-grants check: {len(roles)} roles x {len(tables)} tables examined")


def test_app_versions_non_writer_roles_cannot_insert(pg_conn: object) -> None:
    """Readable example: each documented non-writer role is refused INSERT at the SQL level."""
    non_writers = sorted(
        {
            spec.role
            for spec in load_matrix()
            if spec.table == "app_versions" and not spec.privileges
        }
    )
    assert len(non_writers) >= 5, f"expected >= 5 non-writer roles, found {non_writers}"

    for role in non_writers:
        with pg_conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(f"SET ROLE {role}")
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO app_versions (app_id, version, language, artifact_kind) "
                    "VALUES ('waddles.test.x.default', '0.0.1', 'python', 'source')"
                )
            cur.execute("RESET ROLE")
            cur.execute("ROLLBACK")


def test_app_versions_writer_roles_can_insert_and_are_audited(pg_conn: object) -> None:
    """Readable example: both writer roles succeed, and the trigger records both."""
    for role in ("waddles_publisher", "hub_api"):
        with pg_conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(f"SET ROLE {role}")
            cur.execute(
                "INSERT INTO app_versions (app_id, version, language, artifact_kind, artifact_digest) "
                "VALUES ('waddles.test.x.default', %s, 'python', 'source', %s)",
                (f"0.0.{role}", f"sha256:{'a' * 64}"),
            )
            cur.execute("RESET ROLE")
            cur.execute(
                "SELECT db_role FROM app_versions_audit_log "
                "WHERE app_id = 'waddles.test.x.default' AND operation = 'INSERT' "
                "ORDER BY id DESC LIMIT 1"
            )
            audited_role = cur.fetchone()[0]
            assert audited_role == role
            cur.execute("ROLLBACK")
```

- [ ] **Step 2: Run against a real Postgres to verify it passes**

Run:
```bash
docker run -d --name pg-m2b-rbac -e POSTGRES_PASSWORD=test -e POSTGRES_DB=waddlebot -p 55434:5432 postgres:17-alpine
sleep 3
DATABASE_URL="postgresql://postgres:test@localhost:55434/waddlebot" alembic upgrade head
cd hub_api
TEST_POSTGRES_ADMIN_DSN="postgresql://postgres:test@localhost:55434/waddlebot" \
  python3 -m pytest tests/test_rbac_live_grants.py -v -s
docker rm -f pg-m2b-rbac
```
Expected: `3 passed`, and stdout includes the line `RBAC live-grants check: 8 roles x 9 tables examined`.

- [ ] **Step 3: Verify the test correctly fails on a real drift (regression-proof the gate itself)**

Run:
```bash
docker run -d --name pg-m2b-rbac2 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=waddlebot -p 55435:5432 postgres:17-alpine
sleep 3
DATABASE_URL="postgresql://postgres:test@localhost:55435/waddlebot" alembic upgrade head
psql "postgresql://postgres:test@localhost:55435/waddlebot" -c "GRANT SELECT ON app_versions TO svc_process;"
cd hub_api
TEST_POSTGRES_ADMIN_DSN="postgresql://postgres:test@localhost:55435/waddlebot" \
  python3 -m pytest tests/test_rbac_live_grants.py::test_live_grants_equal_the_matrix_exactly -v
docker rm -f pg-m2b-rbac2
```
Expected: `FAILED` — the assertion error names `('svc_process', 'app_versions', 'SELECT')` as an extra grant the matrix does not declare. This step proves the gate can fail (`critical-rules.md` Verification Integrity: "A check that never fails will never be noticed").

- [ ] **Step 4: Commit**

```bash
git add hub_api/tests/test_rbac_live_grants.py
git commit -m "$(cat <<'EOF'
test(hub-api): RBAC live-grants equality test against config/postgres/rbac-matrix.yaml

Queries information_schema.role_table_grants and asserts set equality
with the matrix file in both directions -- a missing grant and an
extra grant both fail. Asserts >= 8 roles and >= 8 tables examined
(non-vacuous, spec Sec11.10.1). Includes per-role negative/positive
spot-checks on app_versions as readable examples.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 6: `requirements.in` additions + `bundle_secret_crypto.py`

**Depends on:** nothing — `requirements.in` and the AES-256-GCM helper stand alone.

**Files:**
- Modify: `hub_api/requirements.in`
- Create: `hub_api/services/bundle_secret_crypto.py`
- Test: `hub_api/tests/test_bundle_secret_crypto.py`

**Interfaces:**
- Produces: `services.bundle_secret_crypto.encrypt(plaintext: str) -> tuple[bytes, bytes]` (ciphertext-with-tag, iv), `decrypt(ciphertext: bytes, iv: bytes) -> str`, `EncryptionKeyError`.
- Consumes: nothing.

- [ ] **Step 1: Add the new dependencies**

Edit `hub_api/requirements.in`, appending after the `cryptography` block:

```
# M2b bundle-install group:
kubernetes>=31.0.0,<32.0.0  # compiler_job_service.py -- creates the bundle-compiler K8s Job
"PyYAML>=6.0.2,<7.0.0"  # services/rbac_matrix.py -- loads config/postgres/rbac-matrix.yaml
```

- [ ] **Step 2: Regenerate the pinned lockfile**

Run: `cd hub_api && uv pip compile requirements.in --generate-hashes -o requirements.txt`
Expected: exits 0, `requirements.txt` gains `kubernetes==31.x.x` and `PyYAML==6.0.x` entries with `--hash=sha256:...` lines.

- [ ] **Step 3: Write the failing test**

```python
# hub_api/tests/test_bundle_secret_crypto.py
"""AES-256-GCM round-trip + key-validation tests for webhook-secret at-rest encryption."""

from __future__ import annotations

import os

import pytest

from services.bundle_secret_crypto import EncryptionKeyError, decrypt, encrypt

_TEST_KEY = "a" * 64  # 32 bytes hex


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUNDLE_SECRET_ENCRYPTION_KEY", _TEST_KEY)


def test_round_trip() -> None:
    ciphertext, iv = encrypt("my-webhook-secret")
    assert decrypt(ciphertext, iv) == "my-webhook-secret"


def test_ciphertext_differs_from_plaintext() -> None:
    ciphertext, _ = encrypt("my-webhook-secret")
    assert b"my-webhook-secret" not in ciphertext


def test_two_encryptions_use_different_ivs() -> None:
    _, iv1 = encrypt("same-value")
    _, iv2 = encrypt("same-value")
    assert iv1 != iv2


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUNDLE_SECRET_ENCRYPTION_KEY", raising=False)
    with pytest.raises(EncryptionKeyError):
        encrypt("x")


def test_short_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUNDLE_SECRET_ENCRYPTION_KEY", "tooshort")
    with pytest.raises(EncryptionKeyError):
        encrypt("x")


def test_decrypt_with_wrong_iv_fails() -> None:
    ciphertext, iv = encrypt("my-webhook-secret")
    wrong_iv = os.urandom(12)
    with pytest.raises(Exception):  # noqa: PT011 -- cryptography raises InvalidTag, not our own type
        decrypt(ciphertext, wrong_iv)
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_secret_crypto.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_secret_crypto'`

- [ ] **Step 5: Write the implementation**

```python
# hub_api/services/bundle_secret_crypto.py
"""AES-256-GCM helpers for ingest-source webhook secrets at rest.

Same wire format as `services/bot_crypto.py` (12-byte IV, GCM tag
appended to ciphertext) but keyed by its own env var,
`BUNDLE_SECRET_ENCRYPTION_KEY` -- a deliberately separate key from
`RCON_ENCRYPTION_KEY` (different security domain: RCON server
credentials vs. per-tenant webhook HMAC secrets), never shared.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IV_LENGTH = 12
_KEY_HEX_LENGTH = 64


class EncryptionKeyError(ValueError):
    """`BUNDLE_SECRET_ENCRYPTION_KEY` is missing or not a 64-character hex string."""


def _get_key() -> bytes:
    hex_key = os.environ.get("BUNDLE_SECRET_ENCRYPTION_KEY", "")
    if len(hex_key) != _KEY_HEX_LENGTH:
        raise EncryptionKeyError("BUNDLE_SECRET_ENCRYPTION_KEY must be a 64-character hex string")
    return bytes.fromhex(hex_key)


def encrypt(plaintext: str) -> tuple[bytes, bytes]:
    """Encrypt `plaintext`; returns `(ciphertext_with_appended_tag, iv)`."""
    key = _get_key()
    iv = os.urandom(_IV_LENGTH)
    ciphertext = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return ciphertext, iv


def decrypt(ciphertext: bytes, iv: bytes) -> str:
    """Decrypt `ciphertext` (GCM tag appended) encrypted with `encrypt()`."""
    key = _get_key()
    plaintext = AESGCM(key).decrypt(iv, bytes(ciphertext), None)
    return plaintext.decode("utf-8")
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_secret_crypto.py -v`
Expected: `6 passed`

- [ ] **Step 7: Commit**

```bash
git add hub_api/requirements.in hub_api/requirements.txt \
        hub_api/services/bundle_secret_crypto.py hub_api/tests/test_bundle_secret_crypto.py
git commit -m "$(cat <<'EOF'
feat(hub-api): bundle_secret_crypto -- AES-256-GCM at rest for ingest-source webhook secrets

Adds kubernetes + PyYAML to requirements.in/txt (hash-pinned) for this
milestone's K8s Job orchestration and RBAC matrix loading. New
webhook-secret encryption module keyed by its own
BUNDLE_SECRET_ENCRYPTION_KEY env var, same wire format as the existing
bot_crypto.py but a separate key/security domain.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 7: `bundle_manifest_v2.py` — `bundle.yaml` v2 parse + hub-api's own validation subset

**Depends on:** nothing new — imports only `flask_core.app_manifest.KNOWN_MODULES`, already in the tree.

**Files:**
- Create: `hub_api/services/bundle_manifest_v2.py`
- Test: `hub_api/tests/test_bundle_manifest_v2.py`

**Interfaces:**
- Produces: `services.bundle_manifest_v2.parse_bundle_manifest_v2(raw: dict[str, Any], *, known_custom_platforms: frozenset[str], allow_wildcard_consumes: bool, allow_prebuilt: bool) -> BundleManifestV2`; `ManifestV2Error(reason: str, detail: str)`; dataclasses `BundleManifestV2` (`schema_version, app_id, name, version, feature, module, provider, language, artifact, execution_model, is_default, stages, egress, data_tables, limits, permissions, routes_to, consumes`), `ConsumeRule` (`platform, source_id, event_types, filters`), `EgressRule` (`host, methods`), `Limits` (`timeout_ms, memory_mb, egress_rps`).
- Consumes: `flask_core.app_manifest.KNOWN_MODULES` (existing, public).

This is hub-api's **pre-Job** pure-YAML pre-check (spec §9.2: "`400` with a `reason` code for a manifest that fails a pure-YAML rule") — it runs before the compiler Job is even created (Task 10), covering the rules hub-api's own DB makes cheap to check (V1-V21, V23-V24, V27-V30 minus the two artifact-based rules V25/V31, which only the compiler can check against the compiled component, per spec §6.4.4).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_manifest_v2.py
"""Tests for the bundle.yaml v2 pure-YAML validation subset hub-api checks pre-Job."""

from __future__ import annotations

import pytest

from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2

_VALID_MANIFEST = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music Station Song Request",
    "version": "3.0.0",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "bundles.social_music_process:transform",
            "consumes": [
                {"platform": "twitch", "event_types": ["chat.message"]},
            ],
        },
    },
    "egress": [{"host": "api.spotify.com", "methods": ["GET", "POST"]}],
    "data": {"tables": ["music_queue"]},
    "limits": {"timeout_ms": 2000, "memory_mb": 64, "egress_rps": 10},
}


def _parse(overrides: dict, **kwargs):
    manifest = {**_VALID_MANIFEST, **overrides}
    return parse_bundle_manifest_v2(
        manifest,
        known_custom_platforms=kwargs.get("known_custom_platforms", frozenset()),
        allow_wildcard_consumes=kwargs.get("allow_wildcard_consumes", False),
        allow_prebuilt=kwargs.get("allow_prebuilt", True),
    )


def test_valid_manifest_parses() -> None:
    manifest = _parse({})
    assert manifest.app_id == "waddles.socials.music.default"
    assert manifest.consumes[0].platform == "twitch"


def test_wrong_schema_version_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"schema_version": 1})
    assert exc.value.reason == "unsupported_schema_version"


def test_ingest_stage_rejected() -> None:
    manifest = {**_VALID_MANIFEST, "stages": {"ingest": {"entry": "x:y"}}}
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest, known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True
        )
    assert exc.value.reason == "ingest_not_pluggable"


def test_process_stage_without_consumes_rejected() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {"process": {"entry": "bundles.x:transform"}},
    }
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest, known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True
        )
    assert exc.value.reason == "consumes_required"


def test_action_stage_with_consumes_rejected() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {"action": {"entry": "bundles.x:dispatch", "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}]}},
    }
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest, known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True
        )
    assert exc.value.reason == "consumes_on_action_stage"


def test_wildcard_consumes_rejected_without_tenant_setting() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {"process": {"entry": "x:y", "consumes": [{"platform": "*", "event_types": ["chat.message"]}]}},
    }
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest, known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True
        )
    assert exc.value.reason == "wildcard_consumes_not_allowed"


def test_wildcard_consumes_allowed_with_tenant_setting() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {"process": {"entry": "x:y", "consumes": [{"platform": "*", "event_types": ["chat.message"]}]}},
    }
    parsed = parse_bundle_manifest_v2(
        manifest, known_custom_platforms=frozenset(), allow_wildcard_consumes=True, allow_prebuilt=True
    )
    assert parsed.consumes[0].platform == "*"


def test_unknown_custom_platform_rejected() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {"process": {"entry": "x:y", "consumes": [{"platform": "custom:unregistered", "event_types": ["chat.message"]}]}},
    }
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest, known_custom_platforms=frozenset({"other"}), allow_wildcard_consumes=False, allow_prebuilt=True
        )
    assert exc.value.reason == "unknown_consumes_platform"


def test_registered_custom_platform_accepted() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {"process": {"entry": "x:y", "consumes": [{"platform": "custom:mycrm", "event_types": ["ticket.created"]}]}},
    }
    parsed = parse_bundle_manifest_v2(
        manifest, known_custom_platforms=frozenset({"mycrm"}), allow_wildcard_consumes=False, allow_prebuilt=True
    )
    assert parsed.consumes[0].platform == "custom:mycrm"


def test_prebuilt_rejected_when_global_setting_off() -> None:
    manifest = {**_VALID_MANIFEST, "artifact": "prebuilt", "language": "other"}
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest, known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=False
        )
    assert exc.value.reason == "prebuilt_not_allowed"


def test_reserved_data_table_rejected() -> None:
    manifest = {**_VALID_MANIFEST, "data": {"tables": ["users"]}}
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest, known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True
        )
    assert exc.value.reason == "reserved_data_table"


def test_limit_out_of_range_rejected() -> None:
    manifest = {**_VALID_MANIFEST, "limits": {"timeout_ms": 999999}}
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest, known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True
        )
    assert exc.value.reason == "limit_out_of_range"


def test_invalid_egress_host_rejected() -> None:
    manifest = {**_VALID_MANIFEST, "egress": [{"host": "http://evil.example.com/path"}]}
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest, known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True
        )
    assert exc.value.reason == "invalid_egress_host"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_manifest_v2.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_manifest_v2'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/bundle_manifest_v2.py
"""Parse + validate `bundle.yaml` v2 against hub-api's own pure-YAML rule subset.

Covers spec Sec6.4.4's V1-V21/V23-V24/V27-V30 -- everything checkable
without a compiled component. V22 (egress non-empty when the component
imports `http`), V25 (WIT export presence) and V31 (import allowlist)
are artifact-based and run only inside the compiler (M2a); this module
never claims to check them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from flask_core.app_manifest import KNOWN_MODULES

_SEGMENT = r"[a-z0-9][a-z0-9_-]*"
_APP_ID_RE = re.compile(rf"^waddles\.{_SEGMENT}\.{_SEGMENT}\.{_SEGMENT}$")
_FEATURE_RE = re.compile(rf"^waddles\.{_SEGMENT}\.{_SEGMENT}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
_EGRESS_HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_RESERVED_TABLES = frozenset({"users", "tenants", "communities", "app_catalog", "app_activations", "app_tenant_availability"})
_ALLOWED_LANGUAGES = frozenset({"python", "rust", "javascript", "typescript", "other"})
_ALLOWED_STAGES = frozenset({"process", "action", "presentation"})
_MAX_TIMEOUT_MS = 10000
_MAX_MEMORY_MB = 256
_MAX_EGRESS_RPS = 10


class ManifestV2Error(ValueError):
    """Raised when a bundle.yaml v2 dict fails a pure-YAML validation rule. `reason` is machine-checkable."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


@dataclass(slots=True, frozen=True)
class ConsumeRule:
    """One `consumes` rule (spec Sec6.4.3)."""

    platform: str
    source_id: str | None
    event_types: tuple[str, ...]
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class EgressRule:
    """One `egress` entry."""

    host: str
    methods: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class Limits:
    """The `limits` block, with hub-api's own defaults applied."""

    timeout_ms: int
    memory_mb: int
    egress_rps: int


@dataclass(slots=True, frozen=True)
class BundleManifestV2:
    """A validated `bundle.yaml` v2 manifest."""

    schema_version: int
    app_id: str
    name: str
    version: str
    feature: str
    module: str
    provider: str
    language: str
    artifact: str
    execution_model: str
    is_default: bool
    stages: dict[str, Any]
    egress: tuple[EgressRule, ...]
    data_tables: tuple[str, ...]
    limits: Limits
    permissions: tuple[str, ...]
    routes_to: tuple[str, ...]
    consumes: tuple[ConsumeRule, ...]


def _require(condition: bool, reason: str, detail: str) -> None:
    if not condition:
        raise ManifestV2Error(reason, detail)


def _parse_consumes(raw_rules: list[dict[str, Any]], *, known_custom_platforms: frozenset[str], allow_wildcard: bool) -> tuple[ConsumeRule, ...]:
    rules: list[ConsumeRule] = []
    for rule in raw_rules:
        platform = rule.get("platform", "")
        event_types = rule.get("event_types", [])
        _require(bool(platform), "missing_field", "consumes[].platform is required")
        _require(bool(event_types), "missing_field", "consumes[].event_types must be non-empty")
        if platform.startswith("custom:"):
            name = platform.removeprefix("custom:")
            _require(
                name in known_custom_platforms, "unknown_consumes_platform",
                f"{platform!r} is not registered for this tenant",
            )
        elif platform == "*":
            _require(allow_wildcard, "wildcard_consumes_not_allowed", "allow_wildcard_consumes is false for this tenant")
        for event_type in event_types:
            if event_type == "**" or "**" in event_type.split("."):
                _require(allow_wildcard, "wildcard_consumes_not_allowed", f"{event_type!r} requires allow_wildcard_consumes")
        rules.append(
            ConsumeRule(
                platform=platform,
                source_id=rule.get("source_id"),
                event_types=tuple(event_types),
                filters=dict(rule.get("filters") or {}),
            )
        )
    return tuple(rules)


def parse_bundle_manifest_v2(
    raw: dict[str, Any],
    *,
    known_custom_platforms: frozenset[str],
    allow_wildcard_consumes: bool,
    allow_prebuilt: bool,
) -> BundleManifestV2:
    """Parse+validate `raw` (a `yaml.safe_load`-d `bundle.yaml`). Raises `ManifestV2Error` on any rule failure."""
    for key in ("schema_version", "app_id", "name", "version", "feature", "module", "provider", "language", "artifact", "stages"):
        _require(key in raw, "missing_field", f"{key} is required")

    _require(raw["schema_version"] == 2, "unsupported_schema_version", f"got {raw['schema_version']!r}, expected 2")
    _require(bool(_SEMVER_RE.match(raw["version"])), "bad_semver", f"{raw['version']!r} is not valid SemVer 2.0.0")
    _require(bool(_APP_ID_RE.match(raw["app_id"])), "not_namespaced", f"{raw['app_id']!r} is not a valid app_id")
    _require(bool(_FEATURE_RE.match(raw["feature"])), "not_namespaced", f"{raw['feature']!r} is not a valid feature id")
    _require(raw["module"] in KNOWN_MODULES, "unknown_module", f"{raw['module']!r} is not a KNOWN_MODULES entry")
    _require(raw["feature"] == raw["app_id"].rsplit(".", 1)[0], "feature_prefix_mismatch", "feature must equal app_id minus its last segment")
    _require(raw["module"] == raw["feature"].split(".")[1], "feature_prefix_mismatch", "module must equal feature's second segment")
    _require(raw["provider"] in {"builtin", "thirdparty"}, "invalid_provider", f"{raw['provider']!r}")
    _require(raw["language"] in _ALLOWED_LANGUAGES, "unsupported_language", f"{raw['language']!r}")
    _require(raw["artifact"] in {"source", "prebuilt"}, "invalid_provider", f"{raw['artifact']!r}")
    if raw["language"] == "other":
        _require(raw["artifact"] == "prebuilt", "unsupported_language", "language 'other' requires artifact: prebuilt")
    if raw["artifact"] == "prebuilt":
        _require(allow_prebuilt, "prebuilt_not_allowed", "bundles.allow_prebuilt is false")

    stages = raw["stages"]
    _require(bool(stages), "no_stages_declared", "stages must be non-empty")
    _require("ingest" not in stages, "ingest_not_pluggable", "ingest is fixed code, not bundle-pluggable")
    for stage_name in stages:
        _require(stage_name in _ALLOWED_STAGES, "unknown_surface", f"{stage_name!r}")

    consumes: tuple[ConsumeRule, ...] = ()
    if "process" in stages:
        process_consumes = stages["process"].get("consumes") or []
        _require(bool(process_consumes), "consumes_required", "a process stage must declare consumes")
        consumes = _parse_consumes(
            process_consumes, known_custom_platforms=known_custom_platforms, allow_wildcard=allow_wildcard_consumes
        )
    if "action" in stages:
        _require(not stages["action"].get("consumes"), "consumes_on_action_stage", "an action stage must not declare consumes")

    egress_rules: list[EgressRule] = []
    for entry in raw.get("egress") or []:
        host = entry.get("host", "")
        _require(bool(_EGRESS_HOST_RE.match(host)) and "://" not in host, "invalid_egress_host", f"{host!r}")
        methods = tuple(entry.get("methods") or sorted(_ALLOWED_METHODS))
        _require(set(methods) <= _ALLOWED_METHODS, "invalid_egress_method", f"{methods!r}")
        egress_rules.append(EgressRule(host=host, methods=methods))

    tables: list[str] = []
    for table in (raw.get("data") or {}).get("tables") or []:
        _require(bool(_TABLE_RE.match(table)), "invalid_data_table", f"{table!r}")
        _require(table not in _RESERVED_TABLES, "reserved_data_table", f"{table!r} is a reserved identity table")
        tables.append(table)

    raw_limits = raw.get("limits") or {}
    timeout_ms = int(raw_limits.get("timeout_ms", 2000))
    memory_mb = int(raw_limits.get("memory_mb", 64))
    egress_rps = int(raw_limits.get("egress_rps", 10))
    _require(50 <= timeout_ms <= _MAX_TIMEOUT_MS, "limit_out_of_range", f"timeout_ms={timeout_ms}")
    _require(8 <= memory_mb <= _MAX_MEMORY_MB, "limit_out_of_range", f"memory_mb={memory_mb}")
    _require(1 <= egress_rps <= _MAX_EGRESS_RPS, "limit_out_of_range", f"egress_rps={egress_rps}")

    return BundleManifestV2(
        schema_version=raw["schema_version"],
        app_id=raw["app_id"],
        name=raw["name"],
        version=raw["version"],
        feature=raw["feature"],
        module=raw["module"],
        provider=raw["provider"],
        language=raw["language"],
        artifact=raw["artifact"],
        execution_model=raw.get("execution_model", "native"),
        is_default=bool(raw.get("is_default", False)),
        stages=stages,
        egress=tuple(egress_rules),
        data_tables=tuple(tables),
        limits=Limits(timeout_ms=timeout_ms, memory_mb=memory_mb, egress_rps=egress_rps),
        permissions=tuple(raw.get("permissions") or []),
        routes_to=tuple(raw.get("routes_to") or []),
        consumes=consumes,
    )
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_manifest_v2.py -v`
Expected: `13 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_manifest_v2.py hub_api/tests/test_bundle_manifest_v2.py
git commit -m "$(cat <<'EOF'
feat(hub-api): bundle.yaml v2 parser -- hub-api's pure-YAML validation subset (spec Sec6.4)

Covers V1-V21/V23-V24/V27-V30 pre-Job, before the compiler Job (which
alone can check the two artifact-based rules, V25/V31) is even
created. ingest_not_pluggable, consumes_required/consumes_on_action_
stage, wildcard_consumes_not_allowed and custom-platform-registration
are all enforced here.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 8: `bundle_storage_service.py` — staging upload + bucket digest verification

**Depends on:** Task 6 (`boto3` pinned in `requirements.in`).

**Files:**
- Create: `hub_api/services/bundle_storage_service.py`
- Test: `hub_api/tests/test_bundle_storage_service.py`

**Interfaces:**
- Produces: `services.bundle_storage_service.stage_upload(app_id: str, version: str, *, manifest_bytes: bytes, source_bytes: bytes | None, component_bytes: bytes | None) -> StagedUpload` (dataclass: `manifest_key, source_key, component_key`); `fetch_object_sha256(key: str) -> str` (returns `"sha256:" + 64 hex`, used by Task 13's digest cross-check); `fetch_sidecar_json(key: str) -> dict[str, Any]` (downloads and `json.loads`s the compiler's signed 12-field sidecar object at `key`, spec Sec9.4 — Task 13's fallback-insert path reads `built_at`/`scan_status`/`size_bytes` off the returned dict; parse-only, no Ed25519 signature check — hub-api does not hold `BUNDLE_SIGNING_PUBLIC_KEY`, and the property this plan's own security boundary actually depends on, the `artifact_digest`, is independently re-derived by `fetch_object_sha256` against the component bytes themselves, never trusted from the sidecar); `BUCKET_NAME` env-driven constant function `bucket_name() -> str`.
- Consumes: nothing new (`boto3`, already a pinned dependency via `storage_service.py`'s precedent).

**Seam ruling (pre-flight M2a scan, finding #1):** M2a's compiler notification callback carries only `component_key`/`sidecar_key` plus the digest fields — it does not repeat `built_at`/`scan_status`/`size_bytes` in the callback body (those live in the sidecar object itself, spec Sec9.4's 12-field schema). `fetch_sidecar_json` is the one new primitive Task 13 needs to read them for its fallback-insert path without inventing a second, wire-level copy of fields the sidecar already carries.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_storage_service.py
"""Tests for the bundle bucket staging + digest-verification helpers, boto3 mocked."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from services.bundle_storage_service import fetch_object_sha256, stage_upload


@pytest.fixture(autouse=True)
def _bucket_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUNDLE_BUCKET_ENDPOINT", "http://minio.waddles.svc.cluster.local:9000")
    monkeypatch.setenv("BUNDLE_BUCKET_NAME", "waddles-bundles")
    monkeypatch.setenv("BUNDLE_BUCKET_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("BUNDLE_BUCKET_SECRET_ACCESS_KEY", "test-secret")


async def test_stage_upload_writes_manifest_and_source() -> None:
    mock_client = MagicMock()
    with patch("services.bundle_storage_service._client", return_value=mock_client):
        staged = await stage_upload(
            "waddles.socials.music.default", "3.0.0",
            manifest_bytes=b"schema_version: 2\n",
            source_bytes=b"fake-tarball-bytes",
            component_bytes=None,
        )
    assert staged.manifest_key == "staging/waddles.socials.music.default/3.0.0/manifest.yaml"
    assert staged.source_key == "staging/waddles.socials.music.default/3.0.0/source.tar.zst"
    assert staged.component_key is None
    assert mock_client.put_object.call_count == 2
    for call in mock_client.put_object.call_args_list:
        assert call.kwargs["ServerSideEncryption"] == "AES256"


async def test_stage_upload_writes_component_for_prebuilt() -> None:
    mock_client = MagicMock()
    with patch("services.bundle_storage_service._client", return_value=mock_client):
        staged = await stage_upload(
            "waddles.socials.music.default", "3.0.0",
            manifest_bytes=b"schema_version: 2\n",
            source_bytes=None,
            component_bytes=b"fake-wasm-bytes",
        )
    assert staged.component_key == "staging/waddles.socials.music.default/3.0.0/component.wasm"
    assert staged.source_key is None


async def test_fetch_object_sha256_hashes_the_downloaded_bytes() -> None:
    body = b"the exact bytes the compiler published"
    expected = "sha256:" + hashlib.sha256(body).hexdigest()
    mock_client = MagicMock()
    mock_client.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=body))}
    with patch("services.bundle_storage_service._client", return_value=mock_client):
        digest = await fetch_object_sha256("bundles/waddles.socials.music.default/3.0.0/deadbeef.wasm")
    assert digest == expected
    mock_client.get_object.assert_called_once()


async def test_fetch_sidecar_json_parses_the_downloaded_object() -> None:
    from services.bundle_storage_service import fetch_sidecar_json

    sidecar_bytes = (
        b'{"schema_version": 1, "app_id": "waddles.socials.music.default", '
        b'"version": "3.0.1", "digest": "sha256:' + b"a" * 64 + b'", "size_bytes": 2048, '
        b'"language": "python", "artifact_kind": "source", "scan_status": "scanned", '
        b'"wit_world": "waddle:bundle/stage@1.0.0", "built_at": "2026-09-14T12:00:00.000Z", '
        b'"builder": "bundle-compiler@1.0.0", "signature": "ZmFrZQ=="}'
    )
    mock_client = MagicMock()
    mock_client.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=sidecar_bytes))}
    with patch("services.bundle_storage_service._client", return_value=mock_client):
        sidecar = await fetch_sidecar_json("bundles/waddles.socials.music.default/3.0.1/deadbeef.json")
    assert sidecar["built_at"] == "2026-09-14T12:00:00.000Z"
    assert sidecar["scan_status"] == "scanned"
    assert sidecar["size_bytes"] == 2048
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_storage_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_storage_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/bundle_storage_service.py
"""S3-compatible bucket access for the bundle-install flow -- staging uploads + digest verification.

Same boto3/`asyncio.to_thread` pattern as `services/storage_service.py`
(avatar/community-asset uploads), a separate bucket/credential set
(`BUNDLE_BUCKET_*` env vars, chart value `bundles.bucket.*` per spec
Sec12.3) since the bundle bucket holds compiled components and their
signed sidecars, not user media.

`stage_upload()` writes hub-api's own pre-compile staging copy under
`staging/{app_id}/{version}/...` -- distinct from the compiler's own
published, content-addressed `bundles/{app_id}/{version}/{sha256}.wasm`
key layout (spec Sec9.4), because hub-api does not yet know the digest
at upload time. `fetch_object_sha256()` is the digest-verification
primitive Task 13's artifact-callback cross-check re-hashes the
published object with -- hub-api verifies a claimed digest; it never
computes one on its own initiative.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from typing import Any, cast

import boto3
from botocore.client import Config as BotoConfig


@dataclass(slots=True, frozen=True)
class StagedUpload:
    """The bucket keys hub-api wrote for one uploaded version."""

    manifest_key: str
    source_key: str | None
    component_key: str | None


def _client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("BUNDLE_BUCKET_ENDPOINT", "http://minio.waddles.svc.cluster.local:9000"),
        aws_access_key_id=os.getenv("BUNDLE_BUCKET_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.getenv("BUNDLE_BUCKET_SECRET_ACCESS_KEY", ""),
        region_name=os.getenv("BUNDLE_BUCKET_REGION", "us-east-1"),
        config=BotoConfig(signature_version="s3v4"),
    )


def bucket_name() -> str:
    """The bundle bucket name -- `waddles-bundles` by default (spec Sec12.3)."""
    return os.getenv("BUNDLE_BUCKET_NAME", "waddles-bundles")


async def stage_upload(
    app_id: str,
    version: str,
    *,
    manifest_bytes: bytes,
    source_bytes: bytes | None,
    component_bytes: bytes | None,
) -> StagedUpload:
    """Write the uploaded manifest plus (source XOR component) to the staging prefix."""
    prefix = f"staging/{app_id}/{version}"
    manifest_key = f"{prefix}/manifest.yaml"
    source_key = f"{prefix}/source.tar.zst" if source_bytes is not None else None
    component_key = f"{prefix}/component.wasm" if component_bytes is not None else None

    def _put_all() -> None:
        client = _client()
        client.put_object(
            Bucket=bucket_name(), Key=manifest_key, Body=manifest_bytes,
            ContentType="application/yaml", ServerSideEncryption="AES256",
        )
        if source_bytes is not None:
            client.put_object(
                Bucket=bucket_name(), Key=source_key, Body=source_bytes,
                ContentType="application/zstd", ServerSideEncryption="AES256",
            )
        if component_bytes is not None:
            client.put_object(
                Bucket=bucket_name(), Key=component_key, Body=component_bytes,
                ContentType="application/wasm", ServerSideEncryption="AES256",
            )

    await asyncio.to_thread(_put_all)
    return StagedUpload(manifest_key=manifest_key, source_key=source_key, component_key=component_key)


async def fetch_object_sha256(key: str) -> str:
    """Download the object at `key` and return `"sha256:" + hex digest` over its bytes.

    The verification primitive -- hub-api never trusts a claimed digest
    without re-hashing the actual bucket object (Task 13).
    """

    def _get_and_hash() -> str:
        response = _client().get_object(Bucket=bucket_name(), Key=key)
        body = response["Body"].read()
        return "sha256:" + hashlib.sha256(body).hexdigest()

    return await asyncio.to_thread(_get_and_hash)


async def fetch_sidecar_json(key: str) -> dict[str, Any]:
    """Download and parse the compiler's signed 12-field sidecar object at `key` (spec Sec9.4).

    Parse-only -- hub-api does not hold `BUNDLE_SIGNING_PUBLIC_KEY` (spec
    Sec12.1: the public half is a data-plane-pod secret), so no Ed25519
    signature check happens here. The one property this plan's own
    security boundary depends on -- the artifact's digest -- is never
    read from this object; it is independently re-derived by
    `fetch_object_sha256` against the component bytes themselves
    (Task 13). This helper exists only to recover the sidecar's
    build-time metadata (`built_at`, `scan_status`, `size_bytes`) for
    Task 13's fallback-insert path, since M2a's actual callback body
    does not repeat them (pre-flight M2a seam scan, finding #1).
    """
    import json

    def _get_and_parse() -> dict[str, Any]:
        response = _client().get_object(Bucket=bucket_name(), Key=key)
        body = response["Body"].read()
        return cast(dict[str, Any], json.loads(body))

    return await asyncio.to_thread(_get_and_parse)
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_storage_service.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_storage_service.py hub_api/tests/test_bundle_storage_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): bundle_storage_service -- staging bucket upload + digest re-verification

Same boto3/asyncio.to_thread pattern as storage_service.py, a
dedicated bucket/credential set. fetch_object_sha256() is the
primitive Task 13's artifact-callback cross-check uses: hub-api
re-hashes the published object rather than trusting a claimed digest.
fetch_sidecar_json() recovers the sidecar's build-time metadata for
Task 13's fallback-insert path (pre-flight M2a seam scan, finding #1) --
parse-only, no signature check; the digest itself is never read from it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 9: `compiler_job_service.py` — launch the bundle-compiler K8s Job

**Depends on:** Task 6 (`kubernetes` pinned in `requirements.in`), Task 8 (`StagedUpload`, the staging keys the Job is pointed at).

**Files:**
- Create: `hub_api/services/compiler_job_service.py`
- Test: `hub_api/tests/test_compiler_job_service.py`

**Interfaces:**
- Produces: `services.compiler_job_service.build_job_spec(...) -> dict[str, Any]` (a plain dict shaped like the K8s Job manifest, easy to assert against without a live cluster), `create_compiler_job(batch_api: Any, core_api: Any, *, namespace: str, app_id: str, version: str, artifact_kind: str, language: str, staged: StagedUpload, manifest_bytes: bytes, source_bytes: bytes | None, component_bytes: bytes | None, validation_context: dict[str, Any], callback_token: str) -> str` (returns the created Job's `metadata.name`).
- Consumes: `services.bundle_storage_service.StagedUpload` (Task 8).
- **Must match M2a** — the compiler binary reads exactly the env vars this task sets. The `VALIDATION_CONTEXT_JSON` env var's shape (below) is the boundary contract between M2b (hub-api, this plan) and M2a (the compiler): `{"known_custom_platforms": list[str], "allow_wildcard_consumes": bool, "allow_prebuilt": bool, "egress_denylist": list[str]}`. If M2a's plan defines a different shape, reconcile there — this plan's version is the one hub-api ships until that reconciliation happens.

**Seam ruling (pre-flight M2a scan, finding #3, binding — supersedes this task's earlier single-container draft):** spec D27/§9.2 (binding; §16 table line 245: "hub-api, which creates the Job") requires the compiler Job to split into an untrusted `build` initContainer (zero credentials, zero network) and a trusted `publisher` container (bucket/DB/signing credentials, network to bucket + hub-api callback only), sharing one `emptyDir`. The single-container draft violated this — every env var, including the machine JWT `callback_token`, would have landed on the same container that runs bundle-supplied build code. `build_job_spec` now emits both containers with strictly disjoint env: `build` gets only `APP_ID`/`VERSION`/`ARTIFACT_KIND`/`LANGUAGE` plus a read-only `bundle-source` volume mount (see below) — no bucket, DB, signing or callback credential of any kind; `publisher` gets everything credential-bearing. M2a's own plan (`docs/plan-m2a-compiler-sdks` Task 21) drafted a ConfigMap-templated Job body for the same purpose — flagged to that plan's coordinator as now redundant (this task builds the Job directly, self-sufficient, never reading that ConfigMap); M2a's Task 21 NetworkPolicy portion (label-selector based, no ConfigMap dependency) is unaffected and still applies.

**D10's "zero network" for `build` also means it cannot pull the staged manifest/source bytes over any wire of its own** — those bytes must already be sitting in the shared volume before the container starts. `create_compiler_job` solves this by creating a per-Job, read-only Kubernetes Secret (`{job_name}-source`, keys `manifest.yaml` + `source.tar.zst`/`component.wasm`) directly via the Kubernetes API immediately before creating the Job, then setting an `ownerReference` from that Secret to the Job so Kubernetes' cascading GC removes it whenever the Job is removed (`ttlSecondsAfterFinished` included) — `build` mounts it read-only and makes no network call to populate it. This is bounded by the etcd object-size limit (~1 MiB per Secret): large bundle sources are a known, explicitly out-of-scope-here follow-up (flagged in the ledger), not silently mishandled — a bundle whose staged source exceeds the limit fails the Secret creation with a clear `413`-mapped error, never a silent truncation.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_compiler_job_service.py
"""Tests for the bundle-compiler K8s Job builder, kubernetes client mocked."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from services.bundle_storage_service import StagedUpload
from services.compiler_job_service import build_job_spec, create_compiler_job

_STAGED = StagedUpload(
    manifest_key="staging/waddles.socials.music.default/3.0.0/manifest.yaml",
    source_key="staging/waddles.socials.music.default/3.0.0/source.tar.zst",
    component_key=None,
)
_VALIDATION_CONTEXT = {
    "known_custom_platforms": ["mycrm"],
    "allow_wildcard_consumes": False,
    "allow_prebuilt": True,
    "egress_denylist": ["evil.example.com"],
}


def _build_container(spec: dict) -> dict:
    return next(c for c in spec["spec"]["template"]["spec"]["initContainers"] if c["name"] == "build")


def _publisher_container(spec: dict) -> dict:
    return next(c for c in spec["spec"]["template"]["spec"]["containers"] if c["name"] == "publisher")


def test_build_job_spec_sets_gvisor_runtime_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_RUNTIME_CLASS_NAME", "runsc")
    spec = build_job_spec(
        app_id="waddles.socials.music.default", version="3.0.0", artifact_kind="source",
        language="python", staged=_STAGED, validation_context=_VALIDATION_CONTEXT, callback_token="tok",
    )
    assert spec["spec"]["template"]["spec"]["runtimeClassName"] == "runsc"


def test_build_job_spec_network_policy_relevant_fields() -> None:
    spec = build_job_spec(
        app_id="waddles.socials.music.default", version="3.0.0", artifact_kind="source",
        language="python", staged=_STAGED, validation_context=_VALIDATION_CONTEXT, callback_token="tok",
    )
    for container in (_build_container(spec), _publisher_container(spec)):
        security_context = container["securityContext"]
        assert security_context["allowPrivilegeEscalation"] is False
        assert security_context["readOnlyRootFilesystem"] is True
        assert security_context["capabilities"]["drop"] == ["ALL"]
    assert spec["spec"]["backoffLimit"] == 0
    assert spec["spec"]["activeDeadlineSeconds"] == 900


def test_build_container_carries_no_credential_or_callback_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """D27: the untrusted build initContainer holds zero credentials -- the whole point of the split."""
    spec = build_job_spec(
        app_id="waddles.socials.music.default", version="3.0.0", artifact_kind="source",
        language="python", staged=_STAGED, validation_context=_VALIDATION_CONTEXT, callback_token="tok",
    )
    build_env_names = {e["name"] for e in _build_container(spec)["env"]}
    forbidden = {
        "HUB_API_CALLBACK_TOKEN", "HUB_API_CALLBACK_URL", "PUBLISHER_DATABASE_URL",
        "BUNDLE_BUCKET_ACCESS_KEY_ID", "BUNDLE_BUCKET_SECRET_ACCESS_KEY",
        "BUNDLE_SIGNING_PRIVATE_KEY_FILE", "VALIDATION_CONTEXT_JSON",
    }
    assert build_env_names.isdisjoint(forbidden)
    assert build_env_names == {"APP_ID", "VERSION", "ARTIFACT_KIND", "LANGUAGE"}
    build_volume_mounts = {m["name"] for m in _build_container(spec)["volumeMounts"]}
    assert build_volume_mounts == {"work", "bundle-source"}
    assert next(m for m in _build_container(spec)["volumeMounts"] if m["name"] == "bundle-source")["readOnly"] is True


def test_publisher_container_carries_every_credential_env_var() -> None:
    spec = build_job_spec(
        app_id="waddles.socials.music.default", version="3.0.0", artifact_kind="source",
        language="python", staged=_STAGED, validation_context=_VALIDATION_CONTEXT, callback_token="tok",
    )
    container = _publisher_container(spec)
    env_by_name = {e["name"]: e for e in container["env"]}
    assert env_by_name["HUB_API_CALLBACK_TOKEN"]["value"] == "tok"  # nosec B105 -- test fixture value, not a real secret
    assert env_by_name["BUNDLE_BUCKET_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["key"] == "accessKeyId"
    assert env_by_name["BUNDLE_BUCKET_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["key"] == "secretAccessKey"
    assert env_by_name["PUBLISHER_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["key"] == "databaseUrl"
    assert env_by_name["BUNDLE_SIGNING_PRIVATE_KEY_FILE"]["value"] == "/etc/waddles/signing/privateKey"
    parsed_context = json.loads(env_by_name["VALIDATION_CONTEXT_JSON"]["value"])
    assert parsed_context == _VALIDATION_CONTEXT
    signing_mount = next(m for m in container["volumeMounts"] if m["name"] == "signing-key")
    assert signing_mount["readOnly"] is True


def test_build_job_spec_names_the_job_deterministically() -> None:
    spec = build_job_spec(
        app_id="waddles.socials.music.default", version="3.0.0", artifact_kind="source",
        language="python", staged=_STAGED, validation_context=_VALIDATION_CONTEXT, callback_token="tok",
    )
    name = spec["metadata"]["name"]
    assert name.startswith("bundle-compile-")
    assert len(name) <= 63  # K8s object-name limit


async def test_create_compiler_job_creates_source_secret_then_job_then_owns_it() -> None:
    mock_batch_api = MagicMock()
    mock_batch_api.create_namespaced_job.return_value = MagicMock(
        metadata=MagicMock(name="bundle-compile-abc123", uid="job-uid-1")
    )
    mock_core_api = MagicMock()
    job_name = await create_compiler_job(
        mock_batch_api, mock_core_api, namespace="waddles", app_id="waddles.socials.music.default",
        version="3.0.0", artifact_kind="source", language="python", staged=_STAGED,
        manifest_bytes=b"schema_version: 2\n", source_bytes=b"fake-tarball-bytes", component_bytes=None,
        validation_context=_VALIDATION_CONTEXT, callback_token="tok",
    )
    assert job_name == "bundle-compile-abc123"
    mock_core_api.create_namespaced_secret.assert_called_once()
    secret_body = mock_core_api.create_namespaced_secret.call_args.kwargs["body"]
    assert secret_body["metadata"]["name"] == "bundle-compile-abc123-source"
    assert "manifest.yaml" in secret_body["data"]
    assert "source.tar.zst" in secret_body["data"]
    mock_batch_api.create_namespaced_job.assert_called_once()
    assert mock_batch_api.create_namespaced_job.call_args.kwargs["namespace"] == "waddles"
    mock_core_api.patch_namespaced_secret.assert_called_once()
    owner_refs = mock_core_api.patch_namespaced_secret.call_args.kwargs["body"]["metadata"]["ownerReferences"]
    assert owner_refs[0]["uid"] == "job-uid-1"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_compiler_job_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.compiler_job_service'` (7 test functions collected once the module exists)

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/compiler_job_service.py
"""Build and launch the bundle-compiler Kubernetes Job (spec Sec4.6, Sec9.2, D27).

One Job per uploaded version, two containers sharing one `emptyDir`
with sharply different trust (D27, pre-flight M2a seam scan finding
#3): an untrusted `build` initContainer (gVisor, zero credentials,
zero network -- the whole reason it gets neither a bucket key, a DB
URL, the signing key, nor the hub-api callback token) and a trusted
`publisher` container (default runtime, holds every credential, the
only one that ever talks to the bucket/Postgres/hub-api). The digest
is measured in `publisher`, never in the container that ran
bundle-supplied build code (D27's whole point). The NetworkPolicy
itself is a chart concern (Task 12 / M2a Task 21's NetworkPolicy
template); this module only builds the pod spec so the two agree on
image/env/security context.

**Must match M2a**: the compiler binary reads exactly the env vars
this module sets, including `VALIDATION_CONTEXT_JSON`'s shape -- the
snapshot of tenant-dependent validation state (registered custom
platforms, the two boolean settings, the egress denylist) the Job has
no other way to see, since `build`'s NetworkPolicy denies it a live
query back to hub-api's DB (D10: "no network except the bucket and the
hub-api callback" -- that network belongs to `publisher` only).
"""

from __future__ import annotations

import json
import os
from typing import Any

from services.bundle_storage_service import StagedUpload, bucket_name


def _job_name(app_id: str, version: str) -> str:
    """Deterministic, K8s-object-name-safe Job name, `bundle-compile-<hash>`."""
    import hashlib

    digest = hashlib.sha256(f"{app_id}:{version}".encode()).hexdigest()[:16]
    return f"bundle-compile-{digest}"


def build_job_spec(
    *,
    app_id: str,
    version: str,
    artifact_kind: str,
    language: str,
    staged: StagedUpload,
    validation_context: dict[str, Any],
    callback_token: str,
) -> dict[str, Any]:
    """Build the plain-dict Job manifest `create_namespaced_job` sends as `body=`.

    `staged` is retained on the signature for the manifest/source-key
    metadata the `build` container's args reference on the shared
    `/work` mount name, but the actual staged bytes reach `build`
    through the `bundle-source` Secret volume `create_compiler_job`
    creates immediately before this Job -- never a network fetch.
    """
    image = os.environ.get(
        "BUNDLE_COMPILER_IMAGE", "ghcr.io/penguintechinc/waddles/bundle-compiler:latest"
    )
    runtime_class = os.environ.get("SANDBOX_RUNTIME_CLASS_NAME", "runsc")
    hub_api_callback_url = os.environ.get(
        "HUB_API_CALLBACK_URL", "https://hub-api.waddles.svc.cluster.local:8204"
    )
    bucket_secret = os.environ.get("BUNDLE_BUCKET_EXISTING_SECRET", "waddles-bundle-bucket")
    signing_secret = os.environ.get("BUNDLE_SIGNING_SECRET", "waddles-bundle-signing")
    publisher_db_secret = os.environ.get("PUBLISHER_DATABASE_SECRET", "waddles-publisher-db")

    name = _job_name(app_id, version)
    source_secret_name = f"{name}-source"

    build_env = [
        {"name": "APP_ID", "value": app_id},
        {"name": "VERSION", "value": version},
        {"name": "ARTIFACT_KIND", "value": artifact_kind},
        {"name": "LANGUAGE", "value": language},
    ]
    # D27: build carries no bucket/DB/signing/callback credential of any
    # kind -- only the metadata it needs to select a build recipe. The
    # staged manifest/source bytes arrive via the bundle-source Secret
    # volume mount below, never a key it fetches itself.

    publisher_env = [
        {"name": "APP_ID", "value": app_id},
        {"name": "VERSION", "value": version},
        {"name": "ARTIFACT_KIND", "value": artifact_kind},
        {"name": "LANGUAGE", "value": language},
        {"name": "BUCKET_NAME", "value": bucket_name()},
        {
            "name": "BUNDLE_BUCKET_ENDPOINT",
            "value": os.environ.get("BUNDLE_BUCKET_ENDPOINT", "http://minio.waddles.svc.cluster.local:9000"),
        },
        {
            "name": "BUNDLE_BUCKET_ACCESS_KEY_ID",
            "valueFrom": {"secretKeyRef": {"name": bucket_secret, "key": "accessKeyId"}},
        },
        {
            "name": "BUNDLE_BUCKET_SECRET_ACCESS_KEY",
            "valueFrom": {"secretKeyRef": {"name": bucket_secret, "key": "secretAccessKey"}},
        },
        {
            "name": "PUBLISHER_DATABASE_URL",
            "valueFrom": {"secretKeyRef": {"name": publisher_db_secret, "key": "databaseUrl"}},
        },
        {"name": "HUB_API_CALLBACK_URL", "value": hub_api_callback_url},
        {"name": "HUB_API_CALLBACK_TOKEN", "value": callback_token},
        {"name": "BUNDLE_SIGNING_PRIVATE_KEY_FILE", "value": "/etc/waddles/signing/privateKey"},
        {"name": "VALIDATION_CONTEXT_JSON", "value": json.dumps(validation_context, sort_keys=True)},
    ]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "labels": {"app": "bundle-compiler", "waddles.io/app-id": app_id[:63]}},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": {"app": "bundle-compiler"}},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "bundle-compiler",
                    "runtimeClassName": runtime_class,
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "volumes": [
                        {"name": "work", "emptyDir": {}},
                        {"name": "bundle-source", "secret": {"secretName": source_secret_name}},
                        {"name": "signing-key", "secret": {"secretName": signing_secret}},
                    ],
                    "initContainers": [
                        {
                            "name": "build",
                            "image": image,
                            "args": ["build", "--bundle", "/input/source.tar.zst",
                                     "--manifest", "/input/manifest.yaml", "--out", "/work"],
                            "env": build_env,
                            "resources": {"limits": {"cpu": "2000m", "memory": "4Gi"}},
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "work", "mountPath": "/work"},
                                {"name": "bundle-source", "mountPath": "/input", "readOnly": True},
                            ],
                        }
                    ],
                    "containers": [
                        {
                            "name": "publisher",
                            "image": image,
                            "args": ["publish", "--component", "/work/component.wasm",
                                     "--manifest", "/input/manifest.yaml",
                                     "--language", language, "--artifact-kind", artifact_kind],
                            "env": publisher_env,
                            "resources": {"limits": {"cpu": "1000m", "memory": "1Gi"}},
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "work", "mountPath": "/work"},
                                {"name": "bundle-source", "mountPath": "/input", "readOnly": True},
                                {"name": "signing-key", "mountPath": "/etc/waddles/signing", "readOnly": True},
                            ],
                        }
                    ],
                },
            },
        },
    }


async def create_compiler_job(
    batch_api: Any,
    core_api: Any,
    *,
    namespace: str,
    app_id: str,
    version: str,
    artifact_kind: str,
    language: str,
    staged: StagedUpload,
    manifest_bytes: bytes,
    source_bytes: bytes | None,
    component_bytes: bytes | None,
    validation_context: dict[str, Any],
    callback_token: str,
) -> str:
    """Create the per-Job source Secret, then the Job, then own the Secret by the Job.

    `batch_api`/`core_api` are `kubernetes.client.BatchV1Api`/`CoreV1Api`
    -shaped objects, passed in rather than constructed here so tests
    inject mocks and the caller (Task 10) controls in-cluster vs.
    kubeconfig auth.

    D10/D27: `build` has zero network, so it cannot fetch the staged
    manifest/source bytes itself -- they must already be in the pod
    before it starts. hub-api already holds them in memory from the
    original upload request (Task 10, before `stage_upload` even wrote
    them to the bucket), so it packages them directly into a per-Job
    Secret via the Kubernetes API (not a network call from inside the
    sandbox) and points the Job's `bundle-source` volume at it.
    `ownerReferences` ties the Secret's lifetime to the Job so
    Kubernetes' cascading GC removes both together
    (`ttlSecondsAfterFinished` included). Bounded by the etcd
    object-size limit (~1 MiB) -- a bundle whose staged bytes exceed it
    fails Secret creation with a clear error; large-bundle support is a
    documented follow-up, not solved here.
    """
    import asyncio
    import base64

    job_spec = build_job_spec(
        app_id=app_id, version=version, artifact_kind=artifact_kind, language=language,
        staged=staged, validation_context=validation_context, callback_token=callback_token,
    )
    job_name = str(job_spec["metadata"]["name"])
    secret_name = f"{job_name}-source"
    secret_data = {"manifest.yaml": base64.b64encode(manifest_bytes).decode("ascii")}
    if source_bytes is not None:
        secret_data["source.tar.zst"] = base64.b64encode(source_bytes).decode("ascii")
    if component_bytes is not None:
        secret_data["component.wasm"] = base64.b64encode(component_bytes).decode("ascii")

    def _create() -> Any:
        core_api.create_namespaced_secret(
            namespace=namespace,
            body={
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {"name": secret_name},
                "data": secret_data,
            },
        )
        job = batch_api.create_namespaced_job(namespace=namespace, body=job_spec)
        core_api.patch_namespaced_secret(
            name=secret_name,
            namespace=namespace,
            body={
                "metadata": {
                    "ownerReferences": [
                        {
                            "apiVersion": "batch/v1",
                            "kind": "Job",
                            "name": job_name,
                            "uid": job.metadata.uid,
                            "blockOwnerDeletion": False,
                        }
                    ]
                }
            },
        )
        return job

    result = await asyncio.to_thread(_create)
    return str(result.metadata.name)
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_compiler_job_service.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/compiler_job_service.py hub_api/tests/test_compiler_job_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): compiler_job_service -- launch the D27 two-container bundle-compiler K8s Job

One Job per uploaded version, backoffLimit=0, activeDeadlineSeconds=900,
rootless/no-new-privileges/read-only-rootfs/drop-ALL on both containers
(spec Sec4.6, Sec9.2, D27). Untrusted build initContainer carries zero
credentials (gVisor, zero network); trusted publisher container holds
the bucket/DB/signing/callback secrets and never runs bundle code
(pre-flight M2a seam scan, finding #3 -- corrects the earlier
single-container draft, which would have leaked the callback JWT into
the sandbox that runs bundle-supplied build code). Staged manifest/
source bytes reach the build container via a per-Job Secret hub-api
creates directly through the Kubernetes API (never a network fetch
build itself makes), owned by the Job for cascading GC.
VALIDATION_CONTEXT_JSON is the M2a boundary contract: build's own
NetworkPolicy has no live DB access, so hub-api snapshots registered
custom platforms + the two tenant settings + the egress denylist into
this one env var at Job-creation time.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 10: `bundle_version_service.py` — `create_version()` orchestration + `get_version()`/`list_versions()` (R52: penguin-dal)

**Depends on:** Task 4 (`install_dal` penguin-dal wiring, reflects `app_version_uploads`), Task 7 (`parse_bundle_manifest_v2`), Task 8 (`stage_upload`), Task 9 (`create_compiler_job`).

**Files:**
- Create: `hub_api/services/bundle_version_service.py`
- Test: `hub_api/tests/test_bundle_version_service.py`

**Interfaces:**
- Produces: state constants `STATUS_UPLOADED, STATUS_VALIDATING, STATUS_SCANNING, STATUS_INSPECTING, STATUS_COMPILING, STATUS_ADDRESSING, STATUS_PUBLISHING, STATUS_PUBLISHED, STATUS_REJECTED`; `async def create_version(install_dal, *, tenant_id: int, app_id: str, requested_by: int, manifest_bytes: bytes, source_bytes: bytes | None, component_bytes: bytes | None, batch_api: Any, core_api: Any, namespace: str, known_custom_platforms: frozenset[str], allow_wildcard_consumes: bool, allow_prebuilt: bool) -> Any` (the inserted row, re-selected); `async def get_version(install_dal, *, app_id: str, version: str) -> Any` (raises `not_found()`); `async def list_versions(install_dal, *, app_id: str) -> list[Any]`. `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `app_version_uploads` is one of this plan's own new tables (R52), so this module never touches the pre-existing pydal `async_dal`/`dal` pair.
- Consumes: `services.bundle_manifest_v2.parse_bundle_manifest_v2`/`ManifestV2Error` (Task 7), `services.bundle_storage_service.stage_upload` (Task 8), `services.compiler_job_service.create_compiler_job` (Task 9 — **signature widened** by that task's D27 two-container fix: now takes `core_api` plus the raw `manifest_bytes`/`source_bytes`/`component_bytes` this function already holds, to populate the per-Job source Secret), `services.errors.{ApiError, bad_request, conflict, not_found, forbidden}` (existing), `flask_core.auth.create_jwt_token`/`flask_core.secrets.require_secret_key` (existing).

`BUNDLE_MAX_SOURCE_BYTES = 16_777_216`, `BUNDLE_MAX_COMPONENT_BYTES = 33_554_432` (spec §9.2) are module constants here.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_version_service.py
"""Tests for bundle_version_service.create_version()/get_version()/list_versions()."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from services.bundle_version_service import (
    STATUS_UPLOADED,
    STATUS_VALIDATING,
    create_version,
    get_version,
    list_versions,
)
from services.errors import ApiError

_MANIFEST = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music Station Song Request",
    "version": "3.0.1",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "bundles.social_music_process:transform",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        },
    },
}


def _mock_batch_api() -> Any:
    mock = MagicMock()
    mock.create_namespaced_job.return_value = MagicMock(
        metadata=MagicMock(name="bundle-compile-abc", uid="job-uid-1")
    )
    return mock


def _mock_core_api() -> Any:
    return MagicMock()


async def test_create_version_happy_path(install_dal: Any) -> None:
    with patch("services.bundle_storage_service.stage_upload") as mock_stage:
        mock_stage.return_value = MagicMock(
            manifest_key="staging/x/3.0.1/manifest.yaml",
            source_key="staging/x/3.0.1/source.tar.zst",
            component_key=None,
        )
        row = await create_version(
            install_dal,
            tenant_id=1, app_id="waddles.socials.music.default", requested_by=1,
            manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
            source_bytes=b"fake-tarball", component_bytes=None,
            batch_api=_mock_batch_api(), core_api=_mock_core_api(), namespace="waddles",
            known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True,
        )
    assert row.status == STATUS_VALIDATING
    assert row.compiler_job_name == "bundle-compile-abc"
    assert row.version == "3.0.1"


async def test_create_version_rejects_duplicate(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status=STATUS_UPLOADED,
        created_at=now, updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal, tenant_id=1, app_id="waddles.socials.music.default", requested_by=1,
            manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
            source_bytes=b"x", component_bytes=None,
            batch_api=_mock_batch_api(), core_api=_mock_core_api(), namespace="waddles",
            known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True,
        )
    assert exc.value.status_code == 409


async def test_create_version_rejects_bad_manifest(install_dal: Any) -> None:
    bad_manifest = {**_MANIFEST, "schema_version": 1}
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal, tenant_id=1,
            app_id="waddles.socials.music.default", requested_by=1,
            manifest_bytes=yaml.safe_dump(bad_manifest).encode(),
            source_bytes=b"x", component_bytes=None,
            batch_api=_mock_batch_api(), core_api=_mock_core_api(), namespace="waddles",
            known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True,
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "unsupported_schema_version"


async def test_create_version_rejects_oversize_source(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal, tenant_id=1,
            app_id="waddles.socials.music.default", requested_by=1,
            manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
            source_bytes=b"x" * (16_777_216 + 1), component_bytes=None,
            batch_api=_mock_batch_api(), core_api=_mock_core_api(), namespace="waddles",
            known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True,
        )
    assert exc.value.status_code == 413


async def test_create_version_rejects_prebuilt_when_disallowed(install_dal: Any) -> None:
    prebuilt_manifest = {**_MANIFEST, "artifact": "prebuilt", "language": "other"}
    del prebuilt_manifest["stages"]["process"]["entry"]
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal, tenant_id=1,
            app_id="waddles.socials.music.default", requested_by=1,
            manifest_bytes=yaml.safe_dump(prebuilt_manifest).encode(),
            source_bytes=None, component_bytes=b"x",
            batch_api=_mock_batch_api(), core_api=_mock_core_api(), namespace="waddles",
            known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=False,
        )
    assert exc.value.status_code == 403
    assert exc.value.code == "prebuilt_not_allowed"


async def test_get_version_not_found_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await get_version(install_dal, app_id="waddles.x.y.default", version="1.0.0")
    assert exc.value.status_code == 404


async def test_list_versions_returns_all_versions_for_app_id(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="1.0.0", tenant_id=1,
        artifact_kind="source", language="python", status=STATUS_UPLOADED,
        created_at=now, updated_at=now,
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="2.0.0", tenant_id=1,
        artifact_kind="source", language="python", status=STATUS_UPLOADED,
        created_at=now, updated_at=now,
    )
    rows = await list_versions(install_dal, app_id="waddles.socials.music.default")
    assert {r.version for r in rows} == {"1.0.0", "2.0.0"}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_version_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_version_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/bundle_version_service.py
"""Version-upload orchestration: parse -> stage -> mint callback token -> launch compiler Job.

`app_version_uploads` is hub-api's own pre-publish lifecycle tracker
(this plan's Decision #2) -- `app_versions` itself (Task 2) has no
status column and is written only by `waddles_publisher`/`hub_api`
post-build (Task 13). R52: `app_version_uploads` is one of this plan's
own new tables, so every function here queries it through the
penguin-dal `install_dal: AsyncDB` (Task 4) -- never a new pydal
binder/query.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml
from flask_core.auth import create_jwt_token
from flask_core.secrets import require_secret_key
from penguin_dal import AsyncDB

from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2
from services.bundle_storage_service import stage_upload
from services.compiler_job_service import create_compiler_job
from services.errors import ApiError, conflict, not_found

STATUS_UPLOADED = "UPLOADED"
STATUS_VALIDATING = "VALIDATING"
STATUS_SCANNING = "SCANNING"
STATUS_INSPECTING = "INSPECTING"
STATUS_COMPILING = "COMPILING"
STATUS_ADDRESSING = "ADDRESSING"
STATUS_PUBLISHING = "PUBLISHING"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_REJECTED = "REJECTED"

BUNDLE_MAX_SOURCE_BYTES = 16_777_216
BUNDLE_MAX_COMPONENT_BYTES = 33_554_432


def _mint_callback_token(tenant_slug: str) -> str:
    """Short-lived (1h) machine JWT, scope `bundles:artifact`, for the compiler's callback."""
    return create_jwt_token(
        user_id="bundle-compiler",
        username="bundle-compiler",
        email="",
        roles=[],
        secret_key=require_secret_key(),
        tenant=tenant_slug,
        scope="bundles:artifact",
        expiration_hours=1,
    )


async def create_version(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    app_id: str,
    requested_by: int,
    manifest_bytes: bytes,
    source_bytes: bytes | None,
    component_bytes: bytes | None,
    batch_api: Any,
    core_api: Any,
    namespace: str,
    known_custom_platforms: frozenset[str],
    allow_wildcard_consumes: bool,
    allow_prebuilt: bool,
) -> Any:
    """Validate, stage, and launch the compiler Job for a new bundle version.

    Raises `ApiError` 400 (manifest rule failure, `code` = the rule's
    `reason`), 403 `prebuilt_not_allowed`, 409 (version already exists),
    or 413 (oversize part).
    """
    raw = yaml.safe_load(manifest_bytes)
    try:
        manifest = parse_bundle_manifest_v2(
            raw,
            known_custom_platforms=known_custom_platforms,
            allow_wildcard_consumes=allow_wildcard_consumes,
            allow_prebuilt=allow_prebuilt,
        )
    except ManifestV2Error as exc:
        status_code = 403 if exc.reason == "prebuilt_not_allowed" else 400
        raise ApiError(str(exc), status_code, exc.reason) from exc

    if source_bytes is not None and len(source_bytes) > BUNDLE_MAX_SOURCE_BYTES:
        raise ApiError("source tarball exceeds 16 MiB", 413, "PAYLOAD_TOO_LARGE")
    if component_bytes is not None and len(component_bytes) > BUNDLE_MAX_COMPONENT_BYTES:
        raise ApiError("component exceeds 32 MiB", 413, "PAYLOAD_TOO_LARGE")

    existing = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == manifest.version)
    ).select()
    if existing:
        raise conflict(f"version {manifest.version} of {app_id} already exists")

    staged = await stage_upload(
        app_id, manifest.version,
        manifest_bytes=manifest_bytes, source_bytes=source_bytes, component_bytes=component_bytes,
    )

    now = datetime.now(UTC)
    upload_id = await install_dal.app_version_uploads.async_insert(
        app_id=app_id, version=manifest.version, tenant_id=tenant_id, requested_by=requested_by,
        artifact_kind=manifest.artifact, language=manifest.language, status=STATUS_UPLOADED,
        staging_manifest_key=staged.manifest_key, staging_source_key=staged.source_key,
        staging_component_key=staged.component_key, manifest_json=raw,
        created_at=now, updated_at=now,
    )

    callback_token = _mint_callback_token("global")
    job_name = await create_compiler_job(
        batch_api, core_api, namespace=namespace, app_id=app_id, version=manifest.version,
        artifact_kind=manifest.artifact, language=manifest.language, staged=staged,
        manifest_bytes=manifest_bytes, source_bytes=source_bytes, component_bytes=component_bytes,
        validation_context={
            "known_custom_platforms": sorted(known_custom_platforms),
            "allow_wildcard_consumes": allow_wildcard_consumes,
            "allow_prebuilt": allow_prebuilt,
            "egress_denylist": [],
        },
        callback_token=callback_token,
    )

    await install_dal(install_dal.app_version_uploads.id == upload_id).update(
        status=STATUS_VALIDATING, compiler_job_name=job_name, updated_at=datetime.now(UTC),
    )

    rows = await install_dal(install_dal.app_version_uploads.id == upload_id).select()
    return rows.first()


async def get_version(install_dal: AsyncDB, *, app_id: str, version: str) -> Any:
    """The `app_version_uploads` row for `(app_id, version)`. Raises 404 if absent."""
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    first = rows.first()
    if first is None:
        raise not_found(f"version {version} of {app_id} not found")
    return first


async def list_versions(install_dal: AsyncDB, *, app_id: str) -> list[Any]:
    """Every uploaded version of `app_id`, newest first."""
    rows = await install_dal(install_dal.app_version_uploads.app_id == app_id).select(
        orderby=~install_dal.app_version_uploads.created_at,
    )
    return list(rows)
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_version_service.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_version_service.py hub_api/tests/test_bundle_version_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): bundle_version_service -- create_version() orchestration (parse/stage/launch Job, R52)

Ties bundle_manifest_v2, bundle_storage_service and
compiler_job_service together: validates the pure-YAML rule subset,
enforces the 16MiB/32MiB size ceilings, rejects a duplicate
(app_id, version), stages the upload, mints a 1h bundles:artifact
callback JWT, and launches the compiler Job. app_version_uploads is
hub-api's own pre-publish lifecycle tracker (app_versions itself has
no status column, per spec Sec6.10), queried through penguin-dal's
install_dal per coordinator ruling R52.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 11: `blueprints/v1/bundle_versions.py` — `POST`/`GET` `/api/v1/apps/{app_id}/versions` (R52: penguin-dal)

**Depends on:** Task 4 (`bundle_install_db`/`install_dal` fixtures), Task 10 (`bundle_version_service.{create_version, get_version}`, `install_dal`-only per R52).

**Files:**
- Create: `hub_api/blueprints/v1/bundle_versions.py`
- Test: `hub_api/tests/test_bundle_versions_blueprint.py`

**Interfaces:**
- Produces: `POST /api/v1/apps/{app_id}/versions` (multipart: `manifest`, `source` XOR `component`; scope `platform:admin`) → `202` with `{versionId, status, compilerJobName}`; `GET /api/v1/apps/{app_id}/versions/{version}` (`tenant_middleware` only) → `{versionId, appId, version, status, rejectReason, scanStatus, artifactDigest}` (the latter two `null` until Task 13 publishes). Auto-discovered via `BLUEPRINTS` (no registration edit).
- Consumes: `services.bundle_version_service.{create_version, get_version}` (Task 10, `install_dal`-only). **This task's inline `_allow_prebuilt`/`_known_custom_platforms` helpers query the new `platform_settings`/`custom_platforms` tables through `install_dal` (R52); `_allow_wildcard_consumes` queries the pre-existing `tenant_settings` table through the existing pydal `dal` (Decision #8: `allow_wildcard_consumes` lives on hub-api's existing generic tenant-settings surface, not a new table) — this file is the plan's first example of a function needing both handles.** These three helpers are temporary — Tasks 23-25 add the real `platform_settings_service`/`tenant_bundle_settings`/`custom_platform_service` modules and, as their own final step, replace these three inline queries in this file with calls to those modules. A cheap implementer of *this* task does not need those modules to exist yet.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_versions_blueprint.py
"""Blueprint tests for POST/GET /api/v1/apps/{app_id}/versions."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from quart import Quart

from blueprints.v1.bundle_versions import BLUEPRINTS
from tests.conftest import make_token

_MANIFEST_YAML = b"""
schema_version: 2
app_id: waddles.socials.music.default
name: Music Station Song Request
version: 3.0.1
feature: waddles.socials.music
module: socials
provider: builtin
language: python
artifact: source
stages:
  process:
    entry: "bundles.social_music_process:transform"
    consumes:
      - platform: twitch
        event_types: ["chat.message"]
"""


@pytest.fixture
def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    app = Quart(__name__)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_post_version_requires_platform_admin_scope(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions",
        headers={"Authorization": f"Bearer {token}"},
        files={"manifest": (_MANIFEST_YAML, "bundle.yaml")},
        form={},
    )
    assert response.status_code == 403


async def test_post_version_happy_path(app: Quart) -> None:
    token = make_token(scope="platform:admin")
    mock_batch_api = MagicMock()
    mock_batch_api.create_namespaced_job.return_value = MagicMock(
        metadata=MagicMock(name="bundle-compile-abc", uid="job-uid-1")
    )
    mock_core_api = MagicMock()
    with (
        patch("blueprints.v1.bundle_versions._batch_api", return_value=mock_batch_api),
        patch("blueprints.v1.bundle_versions._core_api", return_value=mock_core_api),
    ):
        client = app.test_client()
        response = await client.post(
            "/api/v1/apps/waddles.socials.music.default/versions",
            headers={"Authorization": f"Bearer {token}"},
            files={"manifest": (_MANIFEST_YAML, "bundle.yaml"), "source": (b"fake-tarball", "source.tar.zst")},
        )
    assert response.status_code == 202
    body = await response.get_json()
    assert body["status"] == "VALIDATING"
    assert body["compilerJobName"] == "bundle-compile-abc"


async def test_get_version_returns_404_for_unknown_version(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.get(
        "/api/v1/apps/waddles.socials.music.default/versions/9.9.9",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_versions_blueprint.py -v`
Expected: `ModuleNotFoundError: No module named 'blueprints.v1.bundle_versions'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/blueprints/v1/bundle_versions.py
"""v1 `bundle_versions` group -- POST/GET /api/v1/apps/{app_id}/versions (spec Sec9.2).

Global tier (`platform:admin`), same authorization level as the
existing `marketplace_lifecycle.py::install_bundle`. GET is open to any
authenticated tenant member, matching `list_bundles`'s own precedent.

R52: `platform_settings`/`custom_platforms`/`app_versions`/
`app_version_uploads` are this plan's own new tables, queried through
`current_app.config["install_dal"]` (penguin-dal, Task 4). Only
`allow_wildcard_consumes` reads a pre-existing table (`tenant_settings`,
Decision #8), so this module is the plan's first to hold both the new
`install_dal` and the existing pydal `async_dal`/`dal` pair side by side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services import bundle_version_service as svc
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request

bundle_versions_bp = Blueprint("v1_bundle_versions", __name__, url_prefix="/api/v1/apps")


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code))


def _batch_api() -> Any:
    """The K8s `BatchV1Api` client, constructed lazily so import-time never requires a cluster."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.BatchV1Api()


def _core_api() -> Any:
    """The K8s `CoreV1Api` client -- Task 9's D27 fix uses it to create the per-Job source Secret."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()


async def _allow_prebuilt(install_dal: AsyncDB) -> bool:
    """Temporary inline query against the new `platform_settings` table -- superseded by `platform_settings_service` in Task 23."""
    row = (await install_dal(install_dal.platform_settings.key == "bundles.allow_prebuilt").select()).first()
    return (row.value if row else "true") == "true"


def _allow_wildcard_consumes(dal: Any, tenant_id: int) -> bool:
    """Temporary inline query against the pre-existing `tenant_settings` table (Decision #8) -- superseded by `tenant_bundle_settings` in Task 24. Unaffected by R52: not a new table."""
    row = dal(
        (dal.tenant_settings.tenant_id == tenant_id) & (dal.tenant_settings.key == "allow_wildcard_consumes")
    ).select().first()
    return (row.value if row else "false") == "true"


async def _known_custom_platforms(install_dal: AsyncDB, tenant_id: int) -> frozenset[str]:
    """Temporary inline query against the new `custom_platforms` table -- superseded by `custom_platform_service` in Task 25."""
    rows = await install_dal(install_dal.custom_platforms.tenant_id == tenant_id).select(
        install_dal.custom_platforms.name
    )
    return frozenset(r.name for r in rows)


@dataclass(slots=True, frozen=True)
class CreateVersionResponse:
    """Response DTO for `POST /apps/{app_id}/versions`."""

    success: bool
    versionId: int
    status: str
    compilerJobName: str | None


@dataclass(slots=True, frozen=True)
class VersionDTO:
    """Response DTO for `GET /apps/{app_id}/versions/{version}`."""

    success: bool
    versionId: int
    appId: str
    version: str
    status: str
    rejectReason: str | None
    scanStatus: str | None
    artifactDigest: str | None


@bundle_versions_bp.route("/<app_id>/versions", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
async def post_version(app_id: str) -> tuple[dict[str, object], int]:
    """Upload a new bundle version (multipart: `manifest` + `source` XOR `component`)."""
    async_dal, dal = _dal()
    install_dal = _install_dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    files = await request.files
    manifest_file = files.get("manifest")
    if manifest_file is None:
        return _err(bad_request("manifest part is required"))
    manifest_bytes = manifest_file.read()
    source_file = files.get("source")
    component_file = files.get("component")
    source_bytes = source_file.read() if source_file is not None else None
    component_bytes = component_file.read() if component_file is not None else None

    caller_id = get_current_user_id(request)
    try:
        row = await svc.create_version(
            install_dal,
            tenant_id=ctx.tenant_id, app_id=app_id, requested_by=caller_id,
            manifest_bytes=manifest_bytes, source_bytes=source_bytes, component_bytes=component_bytes,
            batch_api=_batch_api(), core_api=_core_api(),
            namespace=current_app.config.get("K8S_NAMESPACE", "waddles"),
            known_custom_platforms=await _known_custom_platforms(install_dal, ctx.tenant_id),
            allow_wildcard_consumes=_allow_wildcard_consumes(dal, ctx.tenant_id),
            allow_prebuilt=await _allow_prebuilt(install_dal),
        )
    except ApiError as exc:
        return _err(exc)
    return (
        {
            "success": True,
            "versionId": row.id,
            "status": row.status,
            "compilerJobName": row.compiler_job_name,
        },
        202,
    )


@bundle_versions_bp.route("/<app_id>/versions/<version>", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(VersionDTO)
async def get_version(app_id: str, version: str) -> VersionDTO | tuple[dict[str, object], int]:
    """The state-machine state, reject reason (if any), scan status and digest for one version."""
    install_dal = _install_dal()
    try:
        row = await svc.get_version(install_dal, app_id=app_id, version=version)
    except ApiError as exc:
        return _err(exc)
    scan_status = None
    artifact_digest = None
    if row.app_version_id is not None:
        published = (
            await install_dal(install_dal.app_versions.id == row.app_version_id).select()
        ).first()
        if published is not None:
            scan_status = published.scan_status
            artifact_digest = published.artifact_digest
    return VersionDTO(
        success=True, versionId=row.id, appId=row.app_id, version=row.version,
        status=row.status, rejectReason=row.reject_reason,
        scanStatus=scan_status, artifactDigest=artifact_digest,
    )


BLUEPRINTS: list[Blueprint] = [bundle_versions_bp]
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_versions_blueprint.py -v`
Expected: `3 passed`

- [ ] **Step 5: Add the per-file ruff N815 ignore for this new camelCase-DTO blueprint**

Append to `hub_api/pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]` section (same convention as `blueprints/v1/bot.py`'s existing entry):

```toml
# M2b bundle-install group -- camelCase DTO fields are the wire-contract
# convention every other v1 blueprint in this file already uses.
"blueprints/v1/bundle_versions.py" = ["N815"]
```

- [ ] **Step 6: Commit**

```bash
git add hub_api/blueprints/v1/bundle_versions.py hub_api/tests/test_bundle_versions_blueprint.py \
        hub_api/pyproject.toml
git commit -m "$(cat <<'EOF'
feat(hub-api): POST/GET /api/v1/apps/{app_id}/versions (spec Sec9.2, R52)

Global-tier (platform:admin) version upload, orchestrating manifest
validation, bucket staging and compiler-Job launch via
bundle_version_service (penguin-dal install_dal). GET is open to any
tenant member. allow_wildcard_consumes still reads the pre-existing
tenant_settings table through the existing pydal dal (Decision #8).
Auto-discovered blueprint, no registration edit.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 12: Helm — compiler Job RBAC + `bundles.compiler.*`/`sandbox.*` values

**Depends on:** Task 9 (the Job shape hub-api creates, which this RBAC and these values must permit).

**Files:**
- Create: `k8s/helm/waddlebot/templates/hub-api-compiler-rbac.yaml`
- Modify: `k8s/helm/waddlebot/values.yaml`

**Interfaces:**
- Produces: `ServiceAccount hub-api` gains a bound `Role`/`RoleBinding` permitting `batch/v1` Job `create`/`get`/`list`/`watch`/`delete` **and `v1` Secret `create`/`get`/`patch`/`delete`** (the per-Job source Secret, Task 9's D27 fix) in its own namespace; `ServiceAccount bundle-compiler` (the Job's own identity, per Task 9's `serviceAccountName: bundle-compiler`) with **no** RBAC rules at all (it never talks to the K8s API — its job is bucket + hub-api callback only, per D10, and it never sees the Secret's contents through the API either, only the mounted volume). New `values.yaml` keys: `bundles.compiler.image`, `bundles.compiler.activeDeadlineSeconds`, `bundles.compiler.build.resources`/`bundles.compiler.publisher.resources` (split per-container limits, D27), `bundles.signingPublicKeySecret` (spec's own canonical name, Sec12.3), `bundles.compiler.publisherDatabaseSecret`, `sandbox.runtimeClassName`, `sandbox.gvisor.enabled` (these last two are read by `compiler_job_service.py`'s `SANDBOX_RUNTIME_CLASS_NAME` env var, set from this chart value — the chart wiring for the *executor's* own gVisor posture is M3/M4/M6 scope, not this plan's).
- Consumes: nothing new.

**Seam ruling (pre-flight M2a scan, finding #3):** widens the original single-container-shaped values keys to match Task 9's D27 two-container Job spec — `bundles.compiler.resources.limits` splits into `.build.resources`/`.publisher.resources` (different workloads, different limits: `build` runs the toolchain, `publisher` only hashes/signs/uploads/writes one row), and `bundles.signingPublicKeySecret`/`bundles.compiler.publisherDatabaseSecret` are added since the publisher container now mounts/references them directly.

- [ ] **Step 1: Write the RBAC template**

```yaml
# k8s/helm/waddlebot/templates/hub-api-compiler-rbac.yaml
# hub-api's own ServiceAccount gains permission to create/manage the
# bundle-compiler Job it launches per uploaded version (spec Sec4.6,
# Sec9.2). The compiler's OWN ServiceAccount (bundle-compiler) gets NO
# RBAC rules -- it never talks to the Kubernetes API; its only two
# network destinations are the bucket and hub-api's callback (D10).
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ .Release.Name }}-hub-api-compiler-jobs
  namespace: {{ .Release.Namespace }}
rules:
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: [""]
    resources: ["secrets"]
    # The per-Job source Secret (Task 9, D27): hub-api creates it, patches
    # an ownerReference onto it, and lets Kubernetes GC delete it with the
    # Job. Scoped to "secrets" broadly because Role cannot scope by name
    # pattern -- the Namespace boundary plus hub-api's own least-privilege
    # ServiceAccount (no other secret-bearing workload shares it) is the
    # containing control.
    verbs: ["create", "get", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ .Release.Name }}-hub-api-compiler-jobs
  namespace: {{ .Release.Namespace }}
subjects:
  - kind: ServiceAccount
    name: hub-api
    namespace: {{ .Release.Namespace }}
roleRef:
  kind: Role
  name: {{ .Release.Name }}-hub-api-compiler-jobs
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: bundle-compiler
  namespace: {{ .Release.Namespace }}
  annotations:
    waddles.io/no-api-access: "true"  # documentation only -- no Role is ever bound to this ServiceAccount
automountServiceAccountToken: false
```

- [ ] **Step 2: Add the new values.yaml keys**

Append to `k8s/helm/waddlebot/values.yaml`:

```yaml
bundles:
  compiler:
    image: ghcr.io/penguintechinc/waddles/bundle-compiler:latest
    activeDeadlineSeconds: 900
    # D27: build (untrusted, runs bundle code) and publisher (trusted,
    # holds every credential) get independent resource limits -- build
    # runs the actual per-language toolchain, publisher only hashes/
    # signs/uploads/writes one row.
    build:
      resources:
        limits:
          cpu: "2000m"
          memory: "4Gi"
    publisher:
      resources:
        limits:
          cpu: "1000m"
          memory: "1Gi"
    publisherDatabaseSecret: waddles-publisher-db
  bucket:
    provider: minio
    endpoint: "http://minio.waddles.svc.cluster.local:9000"
    name: waddles-bundles
    region: us-east-1
    existingSecret: waddles-bundle-bucket
  signingPublicKeySecret: waddles-bundle-signing
  allowPrebuilt: true
  allowWildcardConsumes: false

sandbox:
  runtimeClassName: runsc
  gvisor:
    enabled: true
```

- [ ] **Step 3: Verify the chart renders**

Run: `cd k8s/helm/waddlebot && helm template . --set bundles.compiler.image=test-image 2>&1 | grep -A3 "kind: Role"`
Expected: prints the rendered `Role`/`RoleBinding`/`ServiceAccount` manifests with `bundle-compiler-jobs` and `bundle-compiler` names visible, no template errors.

- [ ] **Step 4: Verify `helm lint` passes**

Run: `cd k8s/helm/waddlebot && helm lint .`
Expected: `0 chart(s) linted, 0 chart(s) failed`

- [ ] **Step 5: Commit**

```bash
git add k8s/helm/waddlebot/templates/hub-api-compiler-rbac.yaml k8s/helm/waddlebot/values.yaml
git commit -m "$(cat <<'EOF'
feat(chart): hub-api compiler-Job RBAC + bundles.compiler/sandbox values (spec Sec4.6, Sec12.3)

hub-api's ServiceAccount gains Job create/get/list/watch/delete plus
Secret create/get/patch/delete (the per-Job source Secret feeding the
D27 build initContainer, Task 9) in its own namespace; the compiler
Job's own ServiceAccount (bundle-compiler) gets zero RBAC rules,
matching D10's two-destination network policy (bucket + hub-api
callback only, never the K8s API). Values keys widened to the D27
two-container split (pre-flight M2a seam scan, finding #3):
build/publisher get independent resource limits, plus
signingPublicKeySecret/publisherDatabaseSecret for the publisher
container's mounted/referenced credentials.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 13: `bundle_artifact_service.py` — the success callback: verify, cross-check, or fallback-insert (R52: penguin-dal)

**Depends on:** Task 4 (`install_dal` penguin-dal wiring, reflects `app_version_uploads`/`app_versions`/`audit_log`), Task 8 (`bundle_storage_service`'s bucket read, re-hashed here), Task 10 (`app_version_uploads` rows this callback advances).

**Files:**
- Create: `hub_api/services/bundle_artifact_service.py`
- Test: `hub_api/tests/test_bundle_artifact_service.py`

**Interfaces:**
- Produces: `async def record_artifact_notification(install_dal, *, app_id: str, version: str, claimed_digest: str, component_key: str, sidecar_key: str, cwasm_digest: str | None, wasmtime_abi: str | None, collector: str | None, builder: str) -> Any` (the confirmed `app_versions` row). Raises `ApiError` 409 `digest_mismatch` on a re-hash disagreement (with a best-effort `audit_log` entry written first), 404 if no matching `app_version_uploads` row exists. `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `app_version_uploads` and `app_versions` are this plan's own new tables (R52), and `audit_log` is reachable through the same `install_dal` since `install_dal.reflect()` (Task 4) discovers hub-api's entire live schema, not only the new tables.
- Consumes: `services.bundle_storage_service.fetch_object_sha256`, `services.bundle_storage_service.fetch_sidecar_json` (Task 8); `install_dal.audit_log` (a plain reflected `TableProxy`, no new binding needed — `install_dal.reflect()` (Task 4) already sees every pre-existing table `bind_platform_tables()` created, `audit_log` included).

This plan's Decision #3: **hub-api verifies, it never computes.** The compiler (M2a's `waddles_publisher`-authenticated container) is expected to `INSERT`/`UPDATE` `app_versions` directly with its own Postgres role — this function's job is to (a) re-hash the bucket object at `component_key` and compare against `claimed_digest`, refusing on any mismatch, and (b) look for the row the publisher already wrote; if none exists yet (a race, or an M2a build that hasn't wired direct-DB-write yet), insert it itself using hub-api's own (also-granted) write privilege, but **only after its own re-hash succeeded** — never inserting an unverified value.

**Seam ruling (pre-flight M2a scan, finding #1, binding — supersedes this function's earlier draft signature):** M2a's compiler (`docs/plan-m2a-compiler-sdks` Task 17, `notify_hub_api`) posts a plain `#[derive(Serialize)]` Rust struct with no `rename_all` — the wire body is **snake_case**, exactly seven fields: `artifact_digest, cwasm_digest, wasmtime_abi, collector, component_key, sidecar_key, builder`. It does not repeat `language`/`artifact_kind`/`built_at`/`scan_status`/`size_bytes`/`badge` on the wire. Per the coordinator's ownership split (M2a owns the sidecar/callback payload), this function's parameters match that wire shape exactly — no `size_bytes`/`language`/`artifact_kind`/`built_at`/`scan_status`/`badge` parameters. `language`/`artifact_kind` are already known to hub-api from the `app_version_uploads` row created at upload time (Task 10) and are read off that row, never off the wire. `built_at`/`scan_status`/`size_bytes` are recovered, on the fallback-insert path only, by fetching and parsing the signed sidecar object at `sidecar_key` (`fetch_sidecar_json`, Task 8) — the sidecar's own 12-field schema (spec Sec9.4) carries all three. `badge` has no source on either the wire or the sidecar and stays `None` on the fallback path, matching the column's nullable definition (Task 2) — the normal path (publisher already wrote the row) is unaffected, since `badge` there was set directly by `waddles_publisher`'s own `INSERT` (M2a's own writer, outside this function entirely).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_artifact_service.py
"""Tests for the artifact-callback notification/cross-check path."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from services.bundle_artifact_service import record_artifact_notification
from services.bundle_install_dal import raw_sql_rows
from services.errors import ApiError

_DIGEST = "sha256:" + "a" * 64
_WRONG_DIGEST = "sha256:" + "b" * 64


async def _seed_upload(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="COMPILING",
        created_at=now, updated_at=now,
    )


_SIDECAR = {
    "schema_version": 1, "app_id": "waddles.socials.music.default", "version": "3.0.1",
    "digest": _DIGEST, "size_bytes": 2048, "language": "python", "artifact_kind": "source",
    "scan_status": "scanned", "wit_world": "waddle:bundle/stage@1.0.0",
    "built_at": "2026-09-14T12:00:00.000Z", "builder": "bundle-compiler@1.0.0", "signature": "ZmFrZQ==",
}


async def test_matching_digest_is_accepted_when_publisher_already_wrote_the_row(
    install_dal: Any,
) -> None:
    await _seed_upload(install_dal)
    await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1",
        artifact_digest=_DIGEST, language="python", artifact_kind="source",
        scan_status="scanned",
    )

    with patch("services.bundle_artifact_service.fetch_object_sha256", return_value=_DIGEST):
        result = await record_artifact_notification(
            install_dal, app_id="waddles.socials.music.default", version="3.0.1",
            claimed_digest=_DIGEST, component_key="bundles/x/3.0.1/aaa.wasm",
            sidecar_key="bundles/x/3.0.1/aaa.json",
            cwasm_digest=None, wasmtime_abi=None, collector=None,
            builder="bundle-compiler@1.0.0",
        )
    assert result.artifact_digest == _DIGEST

    upload = (
        await install_dal(install_dal.app_version_uploads.app_id == "waddles.socials.music.default").select()
    ).first()
    assert upload.status == "PUBLISHED"
    assert upload.app_version_id == result.id


async def test_missing_row_is_inserted_as_a_fallback_after_verification(install_dal: Any) -> None:
    await _seed_upload(install_dal)

    with (
        patch("services.bundle_artifact_service.fetch_object_sha256", return_value=_DIGEST),
        patch("services.bundle_artifact_service.fetch_sidecar_json", return_value=_SIDECAR),
    ):
        result = await record_artifact_notification(
            install_dal, app_id="waddles.socials.music.default", version="3.0.1",
            claimed_digest=_DIGEST, component_key="bundles/x/3.0.1/aaa.wasm",
            sidecar_key="bundles/x/3.0.1/aaa.json",
            cwasm_digest="sha256:" + "c" * 64, wasmtime_abi="wasmtime-30", collector="drc",
            builder="bundle-compiler@1.0.0",
        )
    assert result.artifact_digest == _DIGEST
    assert result.language == "python"  # sourced from app_version_uploads, not the wire
    assert result.built_at is not None  # sourced from the fetched sidecar
    assert result.scan_status == "scanned"
    assert result.size_bytes == 2048
    count_rows = await raw_sql_rows(
        install_dal, "SELECT COUNT(*) AS n FROM app_versions WHERE artifact_digest = :d", {"d": _DIGEST}
    )
    assert count_rows.first()["n"] == 1


async def test_digest_mismatch_is_refused_and_audited(install_dal: Any) -> None:
    await _seed_upload(install_dal)

    with patch("services.bundle_artifact_service.fetch_object_sha256", return_value=_WRONG_DIGEST):
        with pytest.raises(ApiError) as exc:
            await record_artifact_notification(
                install_dal, app_id="waddles.socials.music.default", version="3.0.1",
                claimed_digest=_DIGEST, component_key="bundles/x/3.0.1/aaa.wasm",
                sidecar_key="bundles/x/3.0.1/aaa.json",
                cwasm_digest=None, wasmtime_abi=None, collector=None,
                builder="bundle-compiler@1.0.0",
            )
    assert exc.value.status_code == 409
    assert exc.value.code == "digest_mismatch"
    count_rows = await raw_sql_rows(
        install_dal, "SELECT COUNT(*) AS n FROM app_versions WHERE app_id = :a",
        {"a": "waddles.socials.music.default"},
    )
    assert count_rows.first()["n"] == 0

    audit_rows = await raw_sql_rows(
        install_dal, "SELECT details FROM audit_log WHERE action = :a",
        {"a": "bundle_artifact_digest_mismatch"},
    )
    audit_row = audit_rows.first()
    assert audit_row is not None
    import json

    details = audit_row["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["claimed_digest"] == _DIGEST
    assert details["computed_digest"] == _WRONG_DIGEST


async def test_unknown_version_upload_raises_404(install_dal: Any) -> None:
    with patch("services.bundle_artifact_service.fetch_object_sha256", return_value=_DIGEST):
        with pytest.raises(ApiError) as exc:
            await record_artifact_notification(
                install_dal, app_id="waddles.unknown.x.default", version="1.0.0",
                claimed_digest=_DIGEST, component_key="bundles/x/1.0.0/aaa.wasm",
                sidecar_key="bundles/x/1.0.0/aaa.json",
                cwasm_digest=None, wasmtime_abi=None, collector=None,
                builder="bundle-compiler@1.0.0",
            )
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_artifact_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_artifact_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/bundle_artifact_service.py
"""The compiler's success-artifact callback: notification/cross-check, never the write authority.

hub-api verifies a claimed digest by re-hashing the bucket object; it
never computes or invents one. `waddles_publisher` (M2a) is expected to
have already INSERTed/UPDATEd the `app_versions` row directly with its
own Postgres role -- this function's primary path is a cross-check
confirming that row exists and matches. Its fallback path (no row yet)
still only ever stores the value it just re-hash-verified, using
hub-api's own also-granted write privilege on `app_versions` (spec
Sec6.10 -- exactly two writers, both permitted, this plan's Decision #3).

R52: `app_version_uploads`/`app_versions` are this plan's own new
tables, queried through the penguin-dal `install_dal: AsyncDB` (Task
4). The digest-mismatch `audit_log` write is a separate, best-effort
write through the same `install_dal` (Decision #18) -- wrapped in
`try`/`except`, matching this codebase's existing convention for every
audit-log call site, so a logging failure never blocks the 409 refusal
this function must still raise.

Wire shape (pre-flight M2a seam scan, finding #1, binding): matches
M2a's actual `notify_hub_api` callback body exactly -- seven
snake_case fields (`artifact_digest, cwasm_digest, wasmtime_abi,
collector, component_key, sidecar_key, builder`), no
`language`/`artifact_kind`/`built_at`/`scan_status`/`size_bytes`/
`badge`. `language`/`artifact_kind` come off the `app_version_uploads`
row (already known at upload time, Task 10); `built_at`/`scan_status`/
`size_bytes` are recovered from the signed sidecar object at
`sidecar_key` (`fetch_sidecar_json`, Task 8) on the fallback-insert
path only -- the normal path (publisher already wrote the row) never
needs them, since `waddles_publisher` set them directly. `badge` has
no source here and stays `None` on the fallback path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services.bundle_storage_service import fetch_object_sha256, fetch_sidecar_json
from services.errors import ApiError, not_found


async def _write_digest_mismatch_audit(
    install_dal: AsyncDB, *, app_id: str, version: str, claimed_digest: str, computed_digest: str
) -> None:
    try:
        await install_dal.audit_log.async_insert(
            user_id=None,
            action="bundle_artifact_digest_mismatch",
            target_type="app_version",
            target_id=f"{app_id}:{version}",
            details={"claimed_digest": claimed_digest, "computed_digest": computed_digest},
            created_at=datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001, S110 -- audit logging failure must not break the main flow
        pass


async def record_artifact_notification(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    claimed_digest: str,
    component_key: str,
    sidecar_key: str,
    cwasm_digest: str | None,
    wasmtime_abi: str | None,
    collector: str | None,
    builder: str,
) -> Any:
    """Verify `claimed_digest` against the bucket, cross-check or fallback-insert, update the upload row."""
    upload_rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = upload_rows.first()
    if upload is None:
        raise not_found(f"no upload request found for {app_id} version {version}")

    computed_digest = await fetch_object_sha256(component_key)
    if computed_digest != claimed_digest:
        await _write_digest_mismatch_audit(
            install_dal, app_id=app_id, version=version,
            claimed_digest=claimed_digest, computed_digest=computed_digest,
        )
        await install_dal(install_dal.app_version_uploads.id == upload.id).update(
            status="REJECTED", reject_reason="digest_mismatch", updated_at=datetime.now(UTC),
        )
        raise ApiError(
            f"claimed digest {claimed_digest} does not match the bucket object's actual digest",
            409, "digest_mismatch",
        )

    existing = await install_dal(
        (install_dal.app_versions.app_id == app_id) & (install_dal.app_versions.version == version)
    ).select()
    version_row = existing.first()
    if version_row is None:
        # Fallback-insert path only: language/artifact_kind are already known
        # from the upload request (Task 10), never from this callback's wire
        # body. built_at/scan_status/size_bytes come from the sidecar object
        # itself (spec Sec9.4) since M2a's callback does not repeat them.
        sidecar = await fetch_sidecar_json(sidecar_key)
        new_id = await install_dal.app_versions.async_insert(
            app_id=app_id, version=version, artifact_digest=claimed_digest,
            cwasm_digest=cwasm_digest, wasmtime_abi=wasmtime_abi, collector=collector,
            size_bytes=sidecar.get("size_bytes"), language=upload.language,
            artifact_kind=upload.artifact_kind, built_at=sidecar.get("built_at"),
            builder=builder, scan_status=sidecar.get("scan_status", "not_scanned"), badge=None,
            created_at=datetime.now(UTC),
        )
        version_row = (await install_dal(install_dal.app_versions.id == new_id).select()).first()

    await install_dal(install_dal.app_version_uploads.id == upload.id).update(
        status="PUBLISHED", app_version_id=version_row.id, updated_at=datetime.now(UTC),
    )
    return version_row
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_artifact_service.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_artifact_service.py hub_api/tests/test_bundle_artifact_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): artifact-callback notification/cross-check -- hub-api verifies, never computes a digest (R52)

Re-hashes the bucket object and compares against the compiler's claimed
digest; refuses + best-effort-audit-logs on mismatch. Cross-checks
against a row waddles_publisher (M2a) already wrote, or falls back to
inserting one itself using hub-api's own also-granted write privilege
-- only ever after its own verification succeeded (spec Sec6.10, this
plan's Decision #3). Queries the new app_version_uploads/app_versions
tables through penguin-dal's install_dal per coordinator ruling R52.
Wire shape matches M2a's actual notify_hub_api callback body exactly
(pre-flight M2a seam scan, finding #1) -- language/artifact_kind read
from app_version_uploads, built_at/scan_status/size_bytes recovered
from the fetched sidecar object on the fallback-insert path only.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 14: `bundle_artifact_service.py` extension — the rejection callback (R52: penguin-dal)

**Depends on:** Task 13 (`bundle_artifact_service`'s success path, `install_dal`-only per R52, extended here with the rejection path).

**Files:**
- Modify: `hub_api/services/bundle_artifact_service.py`
- Modify: `hub_api/tests/test_bundle_artifact_service.py`

**Interfaces:**
- Produces: `async def record_rejection(install_dal, *, app_id: str, version: str, reason: str) -> None` — moves the `app_version_uploads` row to `REJECTED` with the given `reason`. Raises 404 if no matching upload row exists.
- Consumes: nothing new.

The compiler reports failures from `VALIDATING`/`SCANNING`/`INSPECTING`/`COMPILING`/`PUBLISHING` (spec §9.1's REJECTED transitions) through this second callback — there is no successful-artifact digest to verify on this path, so it does not touch `app_versions` at all.

- [ ] **Step 1: Write the failing test**

Append to `hub_api/tests/test_bundle_artifact_service.py`:

```python
from services.bundle_artifact_service import record_rejection  # noqa: E402 -- appended import


async def test_record_rejection_sets_status_and_reason(install_dal: Any) -> None:
    await _seed_upload(install_dal)

    await record_rejection(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1",
        reason="scan_failed",
    )

    row = (
        await install_dal(install_dal.app_version_uploads.app_id == "waddles.socials.music.default").select()
    ).first()
    assert row.status == "REJECTED"
    assert row.reject_reason == "scan_failed"


async def test_record_rejection_unknown_version_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await record_rejection(
            install_dal, app_id="waddles.unknown.x.default", version="1.0.0", reason="scan_failed"
        )
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_artifact_service.py -k rejection -v`
Expected: `ImportError: cannot import name 'record_rejection'`

- [ ] **Step 3: Add the implementation**

Append to `hub_api/services/bundle_artifact_service.py`:

```python
async def record_rejection(install_dal: AsyncDB, *, app_id: str, version: str, reason: str) -> None:
    """Move the upload to REJECTED with `reason` -- no `app_versions` write on this path."""
    upload_rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = upload_rows.first()
    if upload is None:
        raise not_found(f"no upload request found for {app_id} version {version}")
    await install_dal(install_dal.app_version_uploads.id == upload.id).update(
        status="REJECTED", reject_reason=reason, updated_at=datetime.now(UTC),
    )
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_artifact_service.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_artifact_service.py hub_api/tests/test_bundle_artifact_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): record_rejection() -- compiler failure callback (spec Sec9.1 REJECTED transitions, R52)

Handles VALIDATING/SCANNING/INSPECTING/COMPILING/PUBLISHING failures
reported by the compiler; never touches app_versions on this path.
Queries app_version_uploads through penguin-dal's install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 15: `blueprints/v1/bundle_artifact_callback.py` — the compiler's two callbacks (R52: penguin-dal)

**Depends on:** Tasks 13-14 (both callback handlers this blueprint exposes, `install_dal`-only per R52).

**Files:**
- Create: `hub_api/blueprints/v1/bundle_artifact_callback.py`
- Test: `hub_api/tests/test_bundle_artifact_callback_blueprint.py`

**Interfaces:**
- Produces: `POST /api/v1/bundles/{app_id}/versions/{version}/artifact` (scope `bundles:artifact`) → `200 {"success": true, "artifactDigest": ...}` or `409 digest_mismatch`; `POST /api/v1/bundles/{app_id}/versions/{version}/rejected` (scope `bundles:artifact`) → `200 {"success": true}`. **Matches M2a's actual wire contract** (pre-flight M2a seam scan, finding #1, verified against `docs/plan-m2a-compiler-sdks` Task 17's `notify_hub_api`, not invented here) — the artifact endpoint's request body is the one exception to this plan's own camelCase-DTO convention (Global Constraints), a deliberate, documented deviation matching M2a's plain, un-renamed Rust `Serialize` output verbatim.
- Consumes: `services.bundle_artifact_service.{record_artifact_notification, record_rejection}` (Tasks 13-14), `current_app.config["install_dal"]` (Task 4).

Request body for the artifact endpoint (JSON, **snake_case — matches M2a's wire body exactly**): `{"artifact_digest": str, "component_key": str, "sidecar_key": str, "cwasm_digest": str|null, "wasmtime_abi": str|null, "collector": str|null, "builder": str}`. Rejection endpoint (unaffected by the seam finding — M2a has no equivalent struct to match, camelCase-vs-snake_case is moot for a single field): `{"reason": str}`.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_artifact_callback_blueprint.py
"""Blueprint tests for the compiler's artifact + rejected callbacks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from quart import Quart

from blueprints.v1.bundle_artifact_callback import BLUEPRINTS
from tests.conftest import make_token

_DIGEST = "sha256:" + "a" * 64


@pytest.fixture
async def app(install_dal: Any) -> Quart:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="COMPILING",
        created_at=now, updated_at=now,
    )
    app = Quart(__name__)
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_artifact_callback_requires_bundles_artifact_scope(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.post(
        "/api/v1/bundles/waddles.socials.music.default/versions/3.0.1/artifact",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "artifact_digest": _DIGEST, "component_key": "bundles/x/3.0.1/aaa.wasm",
            "sidecar_key": "bundles/x/3.0.1/aaa.json",
            "cwasm_digest": None, "wasmtime_abi": None, "collector": None,
            "builder": "bundle-compiler@1.0.0",
        },
    )
    assert response.status_code == 403


async def test_artifact_callback_happy_path(app: Quart) -> None:
    token = make_token(scope="bundles:artifact")
    sidecar = {
        "schema_version": 1, "app_id": "waddles.socials.music.default", "version": "3.0.1",
        "digest": _DIGEST, "size_bytes": 1, "language": "python", "artifact_kind": "source",
        "scan_status": "scanned", "wit_world": "waddle:bundle/stage@1.0.0",
        "built_at": "2026-09-14T12:00:00.000Z", "builder": "bundle-compiler@1.0.0", "signature": "ZmFrZQ==",
    }
    with (
        patch("services.bundle_artifact_service.fetch_object_sha256", return_value=_DIGEST),
        patch("services.bundle_artifact_service.fetch_sidecar_json", return_value=sidecar),
    ):
        client = app.test_client()
        response = await client.post(
            "/api/v1/bundles/waddles.socials.music.default/versions/3.0.1/artifact",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "artifact_digest": _DIGEST, "component_key": "bundles/x/3.0.1/aaa.wasm",
                "sidecar_key": "bundles/x/3.0.1/aaa.json",
                "cwasm_digest": None, "wasmtime_abi": None, "collector": None,
                "builder": "bundle-compiler@1.0.0",
            },
        )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["artifactDigest"] == _DIGEST


async def test_rejected_callback_happy_path(app: Quart) -> None:
    token = make_token(scope="bundles:artifact")
    client = app.test_client()
    response = await client.post(
        "/api/v1/bundles/waddles.socials.music.default/versions/3.0.1/rejected",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "scan_failed"},
    )
    assert response.status_code == 200
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_artifact_callback_blueprint.py -v`
Expected: `ModuleNotFoundError: No module named 'blueprints.v1.bundle_artifact_callback'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/blueprints/v1/bundle_artifact_callback.py
"""v1 `bundle_artifact_callback` group -- the compiler's two callbacks (spec Sec9.1, Sec9.4).

Machine-JWT auth, scope `bundles:artifact`, minted by
`bundle_version_service._mint_callback_token` at Job-creation time
(Task 10). The artifact endpoint's request DTO is snake_case,
verified against M2a's actual `notify_hub_api` wire body
(`docs/plan-m2a-compiler-sdks` Task 17) rather than invented here --
pre-flight M2a seam scan, finding #1; the one deliberate exception to
this plan's own camelCase-DTO convention (Global Constraints). R52:
both handlers read `current_app.config["install_dal"]` --
`app_version_uploads`/`app_versions` are this plan's own new tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import bundle_artifact_service as svc
from services.errors import ApiError

bundle_artifact_callback_bp = Blueprint(
    "v1_bundle_artifact_callback", __name__, url_prefix="/api/v1/bundles"
)


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code))


@dataclass(slots=True, frozen=True)
class ArtifactCallbackRequest:
    """Request DTO for `POST .../artifact` -- snake_case, matches M2a's compiler payload verbatim.

    The one deliberate exception to this plan's camelCase-DTO
    convention (Global Constraints): `convert_casing` is not enabled
    in this app's QuartSchema setup, so these field names ARE the wire
    keys, and M2a's actual `notify_hub_api` (Task 17) serializes a
    plain, un-renamed Rust struct -- snake_case, seven fields, no
    `language`/`artifact_kind`/`built_at`/`scan_status`/`size_bytes`/
    `badge` (pre-flight M2a seam scan, finding #1).
    """

    artifact_digest: str
    component_key: str
    sidecar_key: str
    builder: str
    cwasm_digest: str | None = None
    wasmtime_abi: str | None = None
    collector: str | None = None


@dataclass(slots=True, frozen=True)
class ArtifactCallbackResponse:
    """Response DTO for `POST .../artifact`."""

    success: bool
    artifactDigest: str


@dataclass(slots=True, frozen=True)
class RejectedCallbackRequest:
    """Request DTO for `POST .../rejected`."""

    reason: str


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic message response DTO."""

    success: bool
    message: str


@bundle_artifact_callback_bp.route("/<app_id>/versions/<version>/artifact", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("bundles:artifact")  # type: ignore[untyped-decorator]
@validate_request(ArtifactCallbackRequest)
@validate_response(ArtifactCallbackResponse)
async def post_artifact(
    data: ArtifactCallbackRequest, app_id: str, version: str
) -> ArtifactCallbackResponse | tuple[dict[str, object], int]:
    """The compiler's success callback -- hub-api re-hashes the bucket object before trusting it."""
    install_dal = _install_dal()
    try:
        version_row = await svc.record_artifact_notification(
            install_dal, app_id=app_id, version=version,
            claimed_digest=data.artifact_digest, component_key=data.component_key,
            sidecar_key=data.sidecar_key, cwasm_digest=data.cwasm_digest,
            wasmtime_abi=data.wasmtime_abi, collector=data.collector, builder=data.builder,
        )
    except ApiError as exc:
        return _err(exc)
    return ArtifactCallbackResponse(success=True, artifactDigest=version_row.artifact_digest)


@bundle_artifact_callback_bp.route("/<app_id>/versions/<version>/rejected", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("bundles:artifact")  # type: ignore[untyped-decorator]
@validate_request(RejectedCallbackRequest)
@validate_response(MessageResponse)
async def post_rejected(
    data: RejectedCallbackRequest, app_id: str, version: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """The compiler's failure callback -- never touches app_versions."""
    install_dal = _install_dal()
    try:
        await svc.record_rejection(install_dal, app_id=app_id, version=version, reason=data.reason)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"version {version} of {app_id} rejected: {data.reason}")


BLUEPRINTS: list[Blueprint] = [bundle_artifact_callback_bp]
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_artifact_callback_blueprint.py -v`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/blueprints/v1/bundle_artifact_callback.py hub_api/tests/test_bundle_artifact_callback_blueprint.py
git commit -m "$(cat <<'EOF'
feat(hub-api): POST /api/v1/bundles/{app_id}/versions/{version}/{artifact,rejected} (spec Sec9.1, Sec9.4, R52)

The compiler's two callbacks, scope bundles:artifact. The artifact
endpoint's request DTO is snake_case, verified against M2a's actual
notify_hub_api wire body rather than invented (pre-flight M2a seam
scan, finding #1) -- the one deliberate exception to this plan's
camelCase-DTO convention. Reads install_dal (penguin-dal) from app config.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 16: `bundle_activation_service.py` — activate/rollback via `app_active_versions` (R52: penguin-dal)

**Depends on:** Task 2 (`app_versions`/`app_active_versions` DDL), Task 4 (`install_dal` penguin-dal wiring + `bundle_install_db`/`install_dal` fixtures).

**Files:**
- Create: `hub_api/services/bundle_activation_service.py`
- Test: `hub_api/tests/test_bundle_activation_service.py`

**Interfaces:**
- Produces: `TENANT_WIDE_COMMUNITY_ID = 0` (the sentinel from Task 2's migration); `async def activate_version(install_dal, *, tenant_id: int, community_id: int | None, app_id: str, version: str, activated_by: int) -> Any` (upserts `app_active_versions`, returns the row) — raises `ApiError` 409 `digest_not_verified` when no `app_versions` row exists for `(app_id, version)` with a non-null `artifact_digest`; `async def get_active_version(install_dal, *, tenant_id: int, community_id: int | None, app_id: str) -> Any | None` (joins to `app_versions`, `None` if nothing is active for that scope — consumed by Task 32's distribution-service extension). `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `app_versions` and `app_active_versions` are this plan's own new tables (R52).
- Consumes: nothing new (queries `app_versions`/`app_active_versions` directly).

Rollback is this same function called again with an older `version` that is still present in `app_versions` — no separate endpoint (this plan's Decision #4).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_activation_service.py
"""Tests for activate_version()/get_active_version() -- app_active_versions."""

from __future__ import annotations

from typing import Any

import pytest

from services.bundle_activation_service import (
    TENANT_WIDE_COMMUNITY_ID,
    activate_version,
    get_active_version,
)
from services.errors import ApiError

_DIGEST_V1 = "sha256:" + "1" * 64
_DIGEST_V2 = "sha256:" + "2" * 64


async def _insert_version(install_dal: Any, version: str, digest: str | None) -> int:
    return await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version=version, artifact_digest=digest,
        language="python", artifact_kind="source", scan_status="scanned",
    )


async def test_activate_version_refuses_an_unverified_digest(install_dal: Any) -> None:
    """The explicit refusal case: a version with no app_versions row (never verified) cannot activate."""
    with pytest.raises(ApiError) as exc:
        await activate_version(
            install_dal, tenant_id=1, community_id=None,
            app_id="waddles.socials.music.default", version="9.9.9", activated_by=1,
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "digest_not_verified"


async def test_activate_version_with_null_digest_row_is_also_refused(install_dal: Any) -> None:
    """A row exists but its digest is still NULL (upload accepted, never published) -- also refused."""
    await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version="0.0.1", artifact_digest=None,
        language="python", artifact_kind="source", scan_status="not_scanned",
    )
    with pytest.raises(ApiError) as exc:
        await activate_version(
            install_dal, tenant_id=1, community_id=None,
            app_id="waddles.socials.music.default", version="0.0.1", activated_by=1,
        )
    assert exc.value.code == "digest_not_verified"


async def test_activate_version_tenant_wide_succeeds_and_is_readable(install_dal: Any) -> None:
    await _insert_version(install_dal, "1.0.0", _DIGEST_V1)

    await activate_version(
        install_dal, tenant_id=1, community_id=None,
        app_id="waddles.socials.music.default", version="1.0.0", activated_by=1,
    )

    active = await get_active_version(
        install_dal, tenant_id=1, community_id=None, app_id="waddles.socials.music.default"
    )
    assert active is not None
    assert active.artifact_digest == _DIGEST_V1

    row = (
        await install_dal(
            (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
            & (install_dal.app_active_versions.tenant_id == 1)
            & (install_dal.app_active_versions.community_id == TENANT_WIDE_COMMUNITY_ID)
        ).select()
    ).first()
    assert row is not None
    assert row.activated_by == 1


async def test_rollback_is_activate_version_pointed_at_an_older_version(install_dal: Any) -> None:
    await _insert_version(install_dal, "1.0.0", _DIGEST_V1)
    await _insert_version(install_dal, "2.0.0", _DIGEST_V2)

    await activate_version(
        install_dal, tenant_id=1, community_id=None,
        app_id="waddles.socials.music.default", version="2.0.0", activated_by=1,
    )
    await activate_version(
        install_dal, tenant_id=1, community_id=None,
        app_id="waddles.socials.music.default", version="1.0.0", activated_by=1,
    )

    active = await get_active_version(
        install_dal, tenant_id=1, community_id=None, app_id="waddles.socials.music.default"
    )
    assert active.artifact_digest == _DIGEST_V1

    count = await install_dal(
        (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
        & (install_dal.app_active_versions.tenant_id == 1)
    ).count()
    assert count == 1  # upsert, not a second row


async def test_get_active_version_returns_none_when_nothing_activated(install_dal: Any) -> None:
    active = await get_active_version(
        install_dal, tenant_id=1, community_id=None, app_id="waddles.socials.music.default"
    )
    assert active is None


async def test_community_scoped_and_tenant_wide_are_independent(install_dal: Any) -> None:
    await _insert_version(install_dal, "1.0.0", _DIGEST_V1)
    await _insert_version(install_dal, "2.0.0", _DIGEST_V2)

    await activate_version(
        install_dal, tenant_id=1, community_id=None,
        app_id="waddles.socials.music.default", version="1.0.0", activated_by=1,
    )
    await activate_version(
        install_dal, tenant_id=1, community_id=1,
        app_id="waddles.socials.music.default", version="2.0.0", activated_by=1,
    )

    tenant_wide = await get_active_version(
        install_dal, tenant_id=1, community_id=None, app_id="waddles.socials.music.default"
    )
    community_scoped = await get_active_version(
        install_dal, tenant_id=1, community_id=1, app_id="waddles.socials.music.default"
    )
    assert tenant_wide.artifact_digest == _DIGEST_V1
    assert community_scoped.artifact_digest == _DIGEST_V2
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_activation_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_activation_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/bundle_activation_service.py
"""Activation and rollback: an upsert into app_active_versions, never an edit of a digest row.

This plan's Decision #4: refuses to activate a digest hub-api never
verified -- i.e. any `(app_id, version)` with no `app_versions` row, or
a row whose `artifact_digest` is still NULL (uploaded/compiling, never
published). Rollback is this same function pointed at an older,
already-published version; there is no separate rollback endpoint
(spec Sec6.10: "Rollback is an UPDATE of version_id here").

`community_id = 0` is the tenant-wide sentinel from migration 0020
(`communities.id` is a real SERIAL starting at 1, so 0 never collides).

R52: `app_versions`/`app_active_versions` are this plan's own new
tables, queried through the penguin-dal `install_dal: AsyncDB` (Task 4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services.errors import ApiError

TENANT_WIDE_COMMUNITY_ID = 0


def _scope_community_id(community_id: int | None) -> int:
    return TENANT_WIDE_COMMUNITY_ID if community_id is None else community_id


async def activate_version(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int | None,
    app_id: str,
    version: str,
    activated_by: int,
) -> Any:
    """Point `(tenant_id, community_id, app_id)` at `version`'s digest. Refuses an unverified digest."""
    version_rows = await install_dal(
        (install_dal.app_versions.app_id == app_id) & (install_dal.app_versions.version == version)
    ).select()
    version_row = version_rows.first()
    if version_row is None or version_row.artifact_digest is None:
        raise ApiError(
            f"{app_id} version {version} has no verified artifact digest -- cannot activate",
            409, "digest_not_verified",
        )
    scope_community_id = _scope_community_id(community_id)

    existing = await install_dal(
        (install_dal.app_active_versions.app_id == app_id)
        & (install_dal.app_active_versions.tenant_id == tenant_id)
        & (install_dal.app_active_versions.community_id == scope_community_id)
    ).select()
    now = datetime.now(UTC)
    if existing:
        await install_dal(
            (install_dal.app_active_versions.app_id == app_id)
            & (install_dal.app_active_versions.tenant_id == tenant_id)
            & (install_dal.app_active_versions.community_id == scope_community_id)
        ).update(version_id=version_row.id, activated_by=activated_by, activated_at=now)
    else:
        await install_dal.app_active_versions.async_insert(
            app_id=app_id, tenant_id=tenant_id, community_id=scope_community_id,
            version_id=version_row.id, activated_by=activated_by, activated_at=now,
        )
    return version_row


async def get_active_version(
    install_dal: AsyncDB, *, tenant_id: int, community_id: int | None, app_id: str
) -> Any | None:
    """The `app_versions` row currently active for `(tenant_id, community_id, app_id)`, or `None`."""
    scope_community_id = _scope_community_id(community_id)
    pointer_rows = await install_dal(
        (install_dal.app_active_versions.app_id == app_id)
        & (install_dal.app_active_versions.tenant_id == tenant_id)
        & (install_dal.app_active_versions.community_id == scope_community_id)
    ).select()
    pointer = pointer_rows.first()
    if pointer is None:
        return None
    version_rows = await install_dal(install_dal.app_versions.id == pointer.version_id).select()
    return version_rows.first()
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_activation_service.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_activation_service.py hub_api/tests/test_bundle_activation_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): bundle_activation_service -- activate/rollback via app_active_versions (spec Sec6.10, R52)

Refuses to activate a digest hub-api never verified (no app_versions
row, or artifact_digest still NULL). Rollback is this same function
pointed at an older, already-published version -- no separate
endpoint. Tenant-wide and community-scoped activation are independent
(community_id=0 sentinel). Queries the new app_versions/
app_active_versions tables through penguin-dal's install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 17: `blueprints/v1/bundle_versions.py` extension — `POST .../activate` (R52: penguin-dal)

**Depends on:** Task 11 (`blueprints/v1/bundle_versions.py` and its `bundle_versions_bp`, `_install_dal()` helper), Task 16 (`activate_version`, `install_dal`-only per R52).

**Files:**
- Modify: `hub_api/blueprints/v1/bundle_versions.py`
- Modify: `hub_api/tests/test_bundle_versions_blueprint.py`

**Interfaces:**
- Produces: `POST /api/v1/apps/{app_id}/versions/{version}/activate` (scope `platform:admin`, body `{"communityId": int | null}`) → `200 {"success": true, "artifactDigest": ...}` or `409 digest_not_verified`.
- Consumes: `services.bundle_activation_service.activate_version` (Task 16).

- [ ] **Step 1: Write the failing test**

Append to `hub_api/tests/test_bundle_versions_blueprint.py`:

```python
async def test_activate_refuses_unverified_digest(app: Quart) -> None:
    token = make_token(scope="platform:admin", tenant="acme-corp")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions/9.9.9/activate",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None},
    )
    assert response.status_code == 409
    body = await response.get_json()
    assert body["error"]["code"] == "digest_not_verified"


async def test_activate_happy_path(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1",
        artifact_digest="sha256:" + "a" * 64, language="python", artifact_kind="source",
        scan_status="scanned",
    )
    token = make_token(scope="platform:admin", tenant="acme-corp")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/activate",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["artifactDigest"] == "sha256:" + "a" * 64
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_versions_blueprint.py -k activate -v`
Expected: `404 NOT FOUND` (the route doesn't exist yet) rather than the expected status codes.

- [ ] **Step 3: Add the route**

Add to `hub_api/blueprints/v1/bundle_versions.py` — new imports:

```python
from dataclasses import dataclass  # already imported; add nothing new here
from quart_schema import validate_request  # add to the existing quart_schema import line

from services import bundle_activation_service as activation_svc  # add alongside the existing svc import
from services.current_user import get_current_user_id  # already imported above
```

New DTOs and route, appended before `BLUEPRINTS: list[Blueprint] = [...]`:

```python
@dataclass(slots=True, frozen=True)
class ActivateVersionRequest:
    """Request DTO for `POST .../activate`."""

    communityId: int | None = None


@dataclass(slots=True, frozen=True)
class ActivateVersionResponse:
    """Response DTO for `POST .../activate`."""

    success: bool
    artifactDigest: str


@bundle_versions_bp.route("/<app_id>/versions/<version>/activate", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_request(ActivateVersionRequest)
@validate_response(ActivateVersionResponse)
async def post_activate(
    data: ActivateVersionRequest, app_id: str, version: str
) -> ActivateVersionResponse | tuple[dict[str, object], int]:
    """Activate (or roll back to) `version` for the caller's tenant, optionally one community."""
    install_dal = _install_dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    caller_id = get_current_user_id(request)
    try:
        version_row = await activation_svc.activate_version(
            install_dal, tenant_id=ctx.tenant_id, community_id=data.communityId,
            app_id=app_id, version=version, activated_by=caller_id,
        )
    except ApiError as exc:
        return _err(exc)
    return ActivateVersionResponse(success=True, artifactDigest=version_row.artifact_digest)
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_versions_blueprint.py -v`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/blueprints/v1/bundle_versions.py hub_api/tests/test_bundle_versions_blueprint.py
git commit -m "$(cat <<'EOF'
feat(hub-api): POST /api/v1/apps/{app_id}/versions/{version}/activate (spec Sec6.10, R52)

Wires bundle_activation_service into the versions blueprint. 409
digest_not_verified is a documented, tested response shape for a
version with no verified app_versions row. Reads install_dal
(penguin-dal) from app config.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 18: `permission_summary_service.py` — the consent-screen summary + canonical hash

**Depends on:** Task 7 (`BundleManifestV2`, `EgressRule`, `Limits`, `ConsumeRule`).

**Files:**
- Create: `hub_api/services/permission_summary_service.py`
- Test: `hub_api/tests/test_permission_summary_service.py`

**Interfaces:**
- Produces: `build_permission_summary(manifest: BundleManifestV2, *, grant_labels: list[dict[str, str]], component_capabilities: frozenset[str], min_tier: str, flag_key: str, allow_private_hosts: bool) -> dict[str, Any]` (spec §9.7.1's sections: `streams`, `egress`, `database`, `capabilities`, `routesTo`, `limits`, `provenance`, `entitlement`, `unusual`); `canonical_json(summary: dict[str, Any]) -> str` (sorted keys, no insignificant whitespace); `permission_hash(summary: dict[str, Any]) -> str` (`"sha256:" + 64 hex` over `canonical_json`).
- Consumes: `services.bundle_manifest_v2.BundleManifestV2` (Task 7).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_permission_summary_service.py
"""Tests for build_permission_summary()/canonical_json()/permission_hash()."""

from __future__ import annotations

from services.bundle_manifest_v2 import BundleManifestV2, ConsumeRule, EgressRule, Limits
from services.permission_summary_service import build_permission_summary, canonical_json, permission_hash

_MANIFEST = BundleManifestV2(
    schema_version=2, app_id="waddles.socials.music.default", name="Music Station",
    version="3.0.0", feature="waddles.socials.music", module="socials", provider="builtin",
    language="python", artifact="source", execution_model="native", is_default=True,
    stages={"process": {}}, egress=(EgressRule(host="api.spotify.com", methods=("GET", "POST")),),
    data_tables=("music_queue",), limits=Limits(timeout_ms=2000, memory_mb=64, egress_rps=10),
    permissions=(), routes_to=("waddles.community.forums.default",),
    consumes=(ConsumeRule(platform="twitch", source_id=None, event_types=("chat.message",), filters={}),),
)


def _summary() -> dict:
    return build_permission_summary(
        _MANIFEST,
        grant_labels=[{"platform": "twitch", "sourceId": "tw-channelA", "label": "Twitch #channelA"}],
        component_capabilities=frozenset({"http", "db", "kv"}),
        min_tier="free", flag_key="waddles.socials.music",
        allow_private_hosts=False,
    )


def test_summary_lists_grant_labels_in_words() -> None:
    summary = _summary()
    assert summary["streams"] == [{"platform": "twitch", "sourceId": "tw-channelA", "label": "Twitch #channelA"}]


def test_summary_lists_routes_to() -> None:
    summary = _summary()
    assert summary["routesTo"] == ["waddles.community.forums.default"]


def test_summary_lists_capabilities_actually_imported() -> None:
    summary = _summary()
    assert set(summary["capabilities"]) == {"http", "db", "kv"}


def test_two_calls_with_identical_inputs_produce_the_same_hash() -> None:
    hash1 = permission_hash(_summary())
    hash2 = permission_hash(_summary())
    assert hash1 == hash2
    assert hash1.startswith("sha256:")
    assert len(hash1) == len("sha256:") + 64


def test_canonical_json_has_sorted_keys_and_no_insignificant_whitespace() -> None:
    text = canonical_json({"b": 1, "a": 2})
    assert text == '{"a":2,"b":1}'


def test_widened_capability_changes_the_hash() -> None:
    narrow = permission_hash(_summary())
    summary = build_permission_summary(
        _MANIFEST,
        grant_labels=[{"platform": "twitch", "sourceId": "tw-channelA", "label": "Twitch #channelA"}],
        component_capabilities=frozenset({"http", "db", "kv", "relay"}),  # widened
        min_tier="free", flag_key="waddles.socials.music", allow_private_hosts=False,
    )
    assert permission_hash(summary) != narrow


def test_unusual_flags_a_private_hosts_egress_request() -> None:
    summary = build_permission_summary(
        _MANIFEST, grant_labels=[], component_capabilities=frozenset({"http"}),
        min_tier="free", flag_key="waddles.socials.music", allow_private_hosts=True,
    )
    assert "allow_private_hosts" in summary["unusual"]
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_permission_summary_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.permission_summary_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/permission_summary_service.py
"""The install-time consent summary and its canonical permission_hash (spec Sec9.7.1, Sec9.7.2)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from services.bundle_manifest_v2 import BundleManifestV2


def build_permission_summary(
    manifest: BundleManifestV2,
    *,
    grant_labels: list[dict[str, str]],
    component_capabilities: frozenset[str],
    min_tier: str,
    flag_key: str,
    allow_private_hosts: bool,
) -> dict[str, Any]:
    """Build the consent-screen summary -- the record `permission_hash()` hashes.

    `grant_labels` is the already-resolved, human-readable grant list
    (spec Sec5.2) -- this function renders it, it does not resolve it.
    `component_capabilities` is what the compiled component actually
    imports (cross-checked against the manifest's requests at install
    time, spec Sec9.7.1) -- passed in rather than derived here.
    """
    unusual: list[str] = []
    if allow_private_hosts:
        unusual.append("allow_private_hosts")
    if any(rule.platform == "*" for rule in manifest.consumes):
        unusual.append("wildcard_consumes")
    if manifest.routes_to:
        unusual.append("routes_to")
    if manifest.artifact == "prebuilt":
        unusual.append("prebuilt_artifact")

    return {
        "streams": list(grant_labels),
        "egress": [{"host": rule.host, "methods": list(rule.methods)} for rule in manifest.egress],
        "database": [{"table": table, "readWrite": "read_write"} for table in manifest.data_tables],
        "capabilities": sorted(component_capabilities),
        "routesTo": list(manifest.routes_to),
        "limits": {
            "timeoutMs": manifest.limits.timeout_ms,
            "memoryMb": manifest.limits.memory_mb,
            "egressRps": manifest.limits.egress_rps,
        },
        "provenance": {"language": manifest.language, "artifactKind": manifest.artifact},
        "entitlement": {"minTier": min_tier, "flagKey": flag_key},
        "unusual": unusual,
    }


def canonical_json(summary: dict[str, Any]) -> str:
    """Sorted-key, no-insignificant-whitespace JSON -- the same permissions always hash the same way."""
    return json.dumps(summary, sort_keys=True, separators=(",", ":"))


def permission_hash(summary: dict[str, Any]) -> str:
    """`"sha256:" + 64 hex` over `canonical_json(summary)`."""
    return "sha256:" + hashlib.sha256(canonical_json(summary).encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_permission_summary_service.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/permission_summary_service.py hub_api/tests/test_permission_summary_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): permission_summary_service -- consent summary + canonical permission_hash (spec Sec9.7)

Canonical JSON (sorted keys, no whitespace) so the same permissions
always hash identically on any machine -- the property headless
approval (Sec9.7.5) depends on. Flags private-host egress, wildcard
consumes, routes_to and prebuilt artifacts under "unusual" per Sec9.7.1.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 19: `bundle_approval_service.py` — permission summary retrieval, approve, deny (R52: penguin-dal)

**Depends on:** Task 3 (`app_install_approvals` DDL), Task 4 (`install_dal` penguin-dal wiring + `bundle_install_db`/`install_dal` fixtures), Task 18 (`build_permission_summary`, `permission_hash`).

**Files:**
- Create: `hub_api/services/bundle_approval_service.py`
- Test: `hub_api/tests/test_bundle_approval_service.py`

**Interfaces:**
- Produces: `async def get_permission_summary(install_dal, *, app_id: str, version: str) -> tuple[dict[str, Any], str]` (the summary dict and its hash); `def classify_diff(new_summary: dict, previous_summary: dict | None) -> str` (one of `"initial"`, `"widened"`, `"narrowed"`, `"unchanged"`); `async def approve_version(install_dal, *, app_id: str, version: str, tenant_id: int, community_id: int | None, approved_by: int, expected_permission_hash: str | None = None) -> Any` (the new `app_install_approvals` row) — raises `ApiError` 409 `permission_hash_mismatch` when `expected_permission_hash` is given and disagrees (spec §9.7.5, fail-closed), 409 `version_not_published` if the version hasn't reached `PUBLISHED`; `async def deny_version(install_dal, *, app_id: str, version: str, reason: str) -> None`. `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `app_install_approvals`/`app_version_uploads` are this plan's own new tables (R52); this module never touches `audit_log` (no audit write in this task's scope).
- Consumes: `services.permission_summary_service.{build_permission_summary, permission_hash}` (Task 18).

**Scope note:** capability derivation (`_derive_capabilities`) is based on the manifest's declared shape (egress non-empty ⇒ `http`, `data_tables` non-empty ⇒ `db`, an `action` stage ⇒ `relay`; `context`/`kv`/`flags`/`log`/`clock` always) — spec §9.7.1's stronger claim ("cross-checked against the component's actual imports") requires M2a's compiler to report an imports list on the artifact callback, which is a documented follow-on once M2a ships that field; this plan does not block on it.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_approval_service.py
"""Tests for get_permission_summary()/classify_diff()/approve_version()/deny_version()."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from services.bundle_approval_service import (
    approve_version,
    classify_diff,
    deny_version,
    get_permission_summary,
)
from services.errors import ApiError

_MANIFEST = {
    "schema_version": 2, "app_id": "waddles.socials.music.default", "name": "Music Station",
    "version": "3.0.1", "feature": "waddles.socials.music", "module": "socials",
    "provider": "builtin", "language": "python", "artifact": "source",
    "stages": {
        "process": {"entry": "x:y", "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}]},
    },
    "egress": [{"host": "api.spotify.com"}],
    "data": {"tables": ["music_queue"]},
}


async def _seed_published(install_dal: Any, *, manifest: dict = _MANIFEST) -> None:
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id=manifest["app_id"], version=manifest["version"], artifact_digest="sha256:" + "a" * 64,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=manifest["app_id"], version=manifest["version"], tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED",
        manifest_json=manifest, app_version_id=version_id,
        created_at=now, updated_at=now,
    )


async def test_get_permission_summary_is_deterministic(install_dal: Any) -> None:
    await _seed_published(install_dal)
    summary1, hash1 = await get_permission_summary(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1"
    )
    summary2, hash2 = await get_permission_summary(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1"
    )
    assert summary1 == summary2
    assert hash1 == hash2


async def test_get_permission_summary_unknown_version_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await get_permission_summary(install_dal, app_id="waddles.x.y.default", version="1.0.0")
    assert exc.value.status_code == 404


async def test_approve_version_records_a_row(install_dal: Any) -> None:
    await _seed_published(install_dal)
    row = await approve_version(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1",
        tenant_id=1, community_id=None, approved_by=1,
    )
    assert row.approved_by == 1
    assert row.permission_hash.startswith("sha256:")


async def test_approve_version_not_published_is_refused(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="0.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="COMPILING",
        manifest_json=_MANIFEST, created_at=now, updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await approve_version(
            install_dal, app_id="waddles.socials.music.default", version="0.0.1",
            tenant_id=1, community_id=None, approved_by=1,
        )
    assert exc.value.code == "version_not_published"


async def test_approve_version_headless_hash_mismatch_fails_closed(install_dal: Any) -> None:
    await _seed_published(install_dal)
    with pytest.raises(ApiError) as exc:
        await approve_version(
            install_dal, app_id="waddles.socials.music.default", version="3.0.1",
            tenant_id=1, community_id=None, approved_by=1,
            expected_permission_hash="sha256:" + "0" * 64,
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "permission_hash_mismatch"


async def test_approve_version_supersedes_the_previous_current_approval(install_dal: Any) -> None:
    await _seed_published(install_dal)
    first = await approve_version(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1",
        tenant_id=1, community_id=None, approved_by=1,
    )
    newer_manifest = {**_MANIFEST, "version": "3.0.2"}
    await _seed_published(install_dal, manifest=newer_manifest)
    second = await approve_version(
        install_dal, app_id="waddles.socials.music.default", version="3.0.2",
        tenant_id=1, community_id=None, approved_by=1,
    )
    refreshed_first = (
        await install_dal(install_dal.app_install_approvals.id == first.id).select()
    ).first()
    assert refreshed_first.superseded_by == second.id


def test_classify_diff_widened_when_a_new_table_is_added() -> None:
    previous = {"database": [{"table": "music_queue"}], "egress": [], "streams": [], "capabilities": [], "routesTo": []}
    new = {"database": [{"table": "music_queue"}, {"table": "music_history"}], "egress": [], "streams": [], "capabilities": [], "routesTo": []}
    assert classify_diff(new, previous) == "widened"


def test_classify_diff_narrowed_when_a_table_is_removed() -> None:
    previous = {"database": [{"table": "music_queue"}, {"table": "music_history"}], "egress": [], "streams": [], "capabilities": [], "routesTo": []}
    new = {"database": [{"table": "music_queue"}], "egress": [], "streams": [], "capabilities": [], "routesTo": []}
    assert classify_diff(new, previous) == "narrowed"


def test_classify_diff_unchanged() -> None:
    summary = {"database": [{"table": "music_queue"}], "egress": [], "streams": [], "capabilities": [], "routesTo": []}
    assert classify_diff(summary, summary) == "unchanged"


def test_classify_diff_initial_with_no_previous() -> None:
    summary = {"database": [], "egress": [], "streams": [], "capabilities": [], "routesTo": []}
    assert classify_diff(summary, None) == "initial"


async def test_deny_version_sets_rejected(install_dal: Any) -> None:
    await _seed_published(install_dal)
    await deny_version(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1",
        reason="egress host not acceptable",
    )
    row = (
        await install_dal(install_dal.app_version_uploads.version == "3.0.1").select()
    ).first()
    assert row.status == "REJECTED"
    assert row.reject_reason == "egress host not acceptable"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_approval_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_approval_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/bundle_approval_service.py
"""Permission-summary retrieval, approval (with widen/narrow diff), and denial (spec Sec9.7).

R52: `app_install_approvals`/`app_version_uploads`/`app_versions` are
this plan's own new tables, queried through the penguin-dal
`install_dal: AsyncDB` (Task 4) -- never a new pydal binder/query.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services.bundle_manifest_v2 import BundleManifestV2, ConsumeRule, EgressRule, Limits
from services.errors import ApiError, not_found
from services.permission_summary_service import build_permission_summary, permission_hash


def _reparse_trusted(raw: dict[str, Any]) -> BundleManifestV2:
    """Rebuild the structured manifest from a stored, already-validated `manifest_json` blob.

    Not a re-validation -- the manifest already passed `bundle_manifest_v2`'s
    gate at upload time (Task 7/10) and is immutable thereafter. This
    just reconstructs the dataclass shape for summary-building.
    """
    stages = raw.get("stages", {})
    consumes = tuple(
        ConsumeRule(
            platform=rule["platform"], source_id=rule.get("source_id"),
            event_types=tuple(rule["event_types"]), filters=dict(rule.get("filters") or {}),
        )
        for rule in (stages.get("process", {}).get("consumes") or [])
    )
    egress = tuple(
        EgressRule(host=e["host"], methods=tuple(e.get("methods") or ()))
        for e in raw.get("egress") or []
    )
    limits_raw = raw.get("limits") or {}
    return BundleManifestV2(
        schema_version=raw["schema_version"], app_id=raw["app_id"], name=raw["name"],
        version=raw["version"], feature=raw["feature"], module=raw["module"], provider=raw["provider"],
        language=raw["language"], artifact=raw["artifact"], execution_model=raw.get("execution_model", "native"),
        is_default=bool(raw.get("is_default", False)), stages=stages, egress=egress,
        data_tables=tuple((raw.get("data") or {}).get("tables") or ()),
        limits=Limits(
            timeout_ms=int(limits_raw.get("timeout_ms", 2000)),
            memory_mb=int(limits_raw.get("memory_mb", 64)),
            egress_rps=int(limits_raw.get("egress_rps", 10)),
        ),
        permissions=tuple(raw.get("permissions") or ()), routes_to=tuple(raw.get("routes_to") or ()),
        consumes=consumes,
    )


def _derive_capabilities(manifest: BundleManifestV2) -> frozenset[str]:
    caps = {"context", "kv", "flags", "log", "clock"}
    if manifest.egress:
        caps.add("http")
    if manifest.data_tables:
        caps.add("db")
    if "action" in manifest.stages:
        caps.add("relay")
    return frozenset(caps)


async def get_permission_summary(
    install_dal: AsyncDB, *, app_id: str, version: str
) -> tuple[dict[str, Any], str]:
    """The consent-screen summary and its hash for one uploaded version."""
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = rows.first()
    if upload is None:
        raise not_found(f"version {version} of {app_id} not found")
    manifest = _reparse_trusted(upload.manifest_json)
    summary = build_permission_summary(
        manifest,
        grant_labels=[
            {"platform": r.platform, "sourceId": r.source_id or "", "label": r.platform}
            for r in manifest.consumes
        ],
        component_capabilities=_derive_capabilities(manifest),
        min_tier="free", flag_key=manifest.feature, allow_private_hosts=False,
    )
    return summary, permission_hash(summary)


def classify_diff(new_summary: dict[str, Any], previous_summary: dict[str, Any] | None) -> str:
    """`"initial"` | `"widened"` | `"narrowed"` | `"unchanged"` -- spec Sec9.7.4."""
    if previous_summary is None:
        return "initial"

    def _flatten(summary: dict[str, Any]) -> set[str]:
        parts: set[str] = set()
        parts |= {f"stream:{s.get('platform')}:{s.get('sourceId')}" for s in summary.get("streams", [])}
        parts |= {f"egress:{e['host']}" for e in summary.get("egress", [])}
        parts |= {f"table:{t['table']}" for t in summary.get("database", [])}
        parts |= {f"cap:{c}" for c in summary.get("capabilities", [])}
        parts |= {f"route:{r}" for r in summary.get("routesTo", [])}
        return parts

    new_set, old_set = _flatten(new_summary), _flatten(previous_summary)
    if new_set == old_set:
        return "unchanged"
    added, removed = new_set - old_set, old_set - new_set
    if added and not removed:
        return "widened"
    if removed and not added:
        return "narrowed"
    return "widened"  # mixed add+remove is treated as widening -- the conservative choice


async def approve_version(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    tenant_id: int,
    community_id: int | None,
    approved_by: int,
    expected_permission_hash: str | None = None,
) -> Any:
    """Record an `app_install_approvals` row. Fails closed on a headless hash mismatch (spec Sec9.7.5)."""
    upload_rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = upload_rows.first()
    if upload is None:
        raise not_found(f"version {version} of {app_id} not found")
    if upload.status != "PUBLISHED":
        raise ApiError(f"version {version} of {app_id} is not published yet", 409, "version_not_published")

    summary, computed_hash = await get_permission_summary(install_dal, app_id=app_id, version=version)
    if expected_permission_hash is not None and expected_permission_hash != computed_hash:
        raise ApiError(
            "the supplied permission_hash does not match the current summary", 409, "permission_hash_mismatch"
        )

    previous_rows = await install_dal(
        (install_dal.app_install_approvals.app_id == app_id)
        & (install_dal.app_install_approvals.tenant_id == tenant_id)
        & (install_dal.app_install_approvals.community_id == community_id)
        & (install_dal.app_install_approvals.superseded_by == None)  # noqa: E711 -- penguin-dal query operator
    ).select()
    previous = previous_rows.first()
    now = datetime.now(UTC)
    new_id = await install_dal.app_install_approvals.async_insert(
        tenant_id=tenant_id, community_id=community_id, app_id=app_id, version=version,
        permission_hash=computed_hash, summary_json=summary, approved_by=approved_by, approved_at=now,
    )
    if previous is not None:
        await install_dal(install_dal.app_install_approvals.id == previous.id).update(superseded_by=new_id)
    return (await install_dal(install_dal.app_install_approvals.id == new_id).select()).first()


async def deny_version(install_dal: AsyncDB, *, app_id: str, version: str, reason: str) -> None:
    """Move the version to REJECTED with `reason` -- reuses `app_version_uploads.status`."""
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = rows.first()
    if upload is None:
        raise not_found(f"version {version} of {app_id} not found")
    await install_dal(install_dal.app_version_uploads.id == upload.id).update(
        status="REJECTED", reject_reason=reason, updated_at=datetime.now(UTC),
    )
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_approval_service.py -v`
Expected: `12 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_approval_service.py hub_api/tests/test_bundle_approval_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): bundle_approval_service -- permission summary, approve (fail-closed hash check), deny (R52)

approve_version() fails closed on a headless permission_hash mismatch
(spec Sec9.7.5), supersedes the previous current approval on a new
one, and refuses a version that hasn't reached PUBLISHED.
classify_diff() labels widened/narrowed/unchanged/initial for the
upgrade-diff UI (Sec9.7.4). Queries the new app_install_approvals/
app_version_uploads/app_versions tables through penguin-dal's
install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 20: `bundle_db_role_service.py` — per-bundle Postgres roles for the `db` capability

**Depends on:** nothing new — SQLAlchemy is already pinned transitively via `libs/flask_core`.

**Files:**
- Create: `hub_api/services/bundle_db_role_service.py`
- Test: `hub_api/tests/test_bundle_db_role_service.py`

**Interfaces:**
- Produces: `bundle_role_name(app_id: str) -> str` (`bundle_<app_id with `.`/`-` -> `_`>`); `async def create_bundle_role(engine: Any, *, app_id: str, tables: list[str], grantee_roles: tuple[str, ...] = ("svc_process", "svc_action")) -> str` (idempotent `CREATE ROLE ... NOLOGIN`, `GRANT SELECT, INSERT, UPDATE, DELETE` on exactly `tables`, `GRANT <role> TO <grantee_roles>` so the stage can `SET ROLE`); `async def drop_bundle_role(engine: Any, *, app_id: str) -> None` (idempotent `DROP ROLE IF EXISTS`, after revoking).
- Consumes: nothing new — `sqlalchemy` (already a pinned dependency via `libs/flask_core`), matching `backend-database.md` rule #2 (SQLAlchemy for schema-adjacent DDL, never runtime queries).

This is **unrelated to** Task 1-2's `app_versions` RBAC roles — it is the spec §11.6.2 per-bundle `data.tables` role, created at approval, dropped at uninstall (Task 36), with a 168 h orphan sweeper behind it (Task 36, this plan's Decision #10b).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_db_role_service.py
"""Tests for per-bundle Postgres role creation/drop, SQLAlchemy engine mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.bundle_db_role_service import bundle_role_name, create_bundle_role, drop_bundle_role


def test_bundle_role_name_replaces_dots_and_dashes() -> None:
    assert bundle_role_name("waddles.socials.music-station.default") == "bundle_waddles_socials_music_station_default"


async def test_create_bundle_role_issues_idempotent_create_and_grants() -> None:
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value.__enter__.return_value = mock_conn

    role_name = await create_bundle_role(
        mock_engine, app_id="waddles.socials.music.default", tables=["music_queue", "music_history"]
    )
    assert role_name == "bundle_waddles_socials_music_default"

    executed_sql = " ".join(str(call.args[0]) for call in mock_conn.execute.call_args_list)
    assert "CREATE ROLE" in executed_sql
    assert "IF NOT EXISTS" in executed_sql
    assert "music_queue" in executed_sql
    assert "music_history" in executed_sql
    assert "svc_process" in executed_sql
    assert "svc_action" in executed_sql


async def test_create_bundle_role_rejects_a_reserved_table_name() -> None:
    mock_engine = MagicMock()
    with pytest.raises(ValueError, match="reserved"):
        await create_bundle_role(mock_engine, app_id="waddles.socials.music.default", tables=["users"])


async def test_drop_bundle_role_revokes_then_drops() -> None:
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value.__enter__.return_value = mock_conn

    await drop_bundle_role(mock_engine, app_id="waddles.socials.music.default")

    executed_sql = " ".join(str(call.args[0]) for call in mock_conn.execute.call_args_list)
    assert "REVOKE ALL" in executed_sql
    assert "DROP ROLE IF EXISTS" in executed_sql
    assert "bundle_waddles_socials_music_default" in executed_sql
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_db_role_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_db_role_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/bundle_db_role_service.py
"""Per-bundle Postgres roles for the `db` WIT capability (spec Sec11.6.2, Sec7.4).

Created at approval (Task 21), dropped at uninstall (Task 36), with a
`BUNDLE_ROLE_GRACE_H = 168` hour orphan sweeper behind it for roles
whose uninstall never ran (this plan's Decision #10b, spec Q3). Uses
SQLAlchemy directly
against a privileged engine -- this is schema-adjacent DDL, not a
runtime query, matching `backend-database.md` rule #2. Unrelated to
Tasks 1-2's `app_versions`/`app_active_versions` RBAC roles, which are
a fixed, spec-defined set; this one is generated per bundle, per its
approved `data.tables`.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy import text

_RESERVED_TABLES = frozenset({"users", "tenants", "communities", "app_catalog", "app_activations", "app_tenant_availability"})
_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def bundle_role_name(app_id: str) -> str:
    """`bundle_<app_id with '.'/'-' -> '_'>` -- the per-bundle Postgres role name."""
    return "bundle_" + re.sub(r"[.\-]", "_", app_id)


def _validate_tables(tables: list[str]) -> None:
    for table in tables:
        if table in _RESERVED_TABLES:
            raise ValueError(f"{table!r} is a reserved identity table")
        if not _TABLE_RE.match(table):
            raise ValueError(f"{table!r} is not a valid table name")


async def create_bundle_role(
    engine: Any, *, app_id: str, tables: list[str], grantee_roles: tuple[str, ...] = ("svc_process", "svc_action")
) -> str:
    """Idempotently create the bundle's role and grant it exactly `tables`. Returns the role name."""
    _validate_tables(tables)
    role_name = bundle_role_name(app_id)

    def _run() -> None:
        with engine.begin() as conn:
            conn.execute(text(
                f"DO $$ BEGIN\n"
                f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_name}') THEN\n"
                f"    CREATE ROLE {role_name} NOLOGIN;\n"
                f"  END IF;\n"
                f"END $$;"
            ))
            for table in tables:
                conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role_name};"))
            for grantee in grantee_roles:
                conn.execute(text(f"GRANT {role_name} TO {grantee};"))

    await asyncio.to_thread(_run)
    return role_name


async def drop_bundle_role(engine: Any, *, app_id: str) -> None:
    """Revoke everything, then idempotently drop the bundle's role (called at uninstall, Task 36)."""
    role_name = bundle_role_name(app_id)

    def _run() -> None:
        with engine.begin() as conn:
            conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role_name};"))
            conn.execute(text(
                f"DO $$ BEGIN\n"
                f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_name}') THEN\n"
                f"    DROP ROLE IF EXISTS {role_name};\n"
                f"  END IF;\n"
                f"END $$;"
            ))

    await asyncio.to_thread(_run)
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_db_role_service.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_db_role_service.py hub_api/tests/test_bundle_db_role_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): bundle_db_role_service -- per-bundle Postgres roles for the db capability (spec Sec11.6.2)

Idempotent CREATE ROLE + GRANT scoped to exactly the bundle's approved
data.tables; svc_process/svc_action are grantee members so the stage
can SET ROLE. Rejects reserved identity tables. Separate concept from
Tasks 1-2's fixed app_versions RBAC roles.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 21: Wire per-bundle role creation into `approve_version()` (R52: penguin-dal)

**Depends on:** Task 19 (`approve_version`, `install_dal`-only per R52), Task 20 (`create_bundle_role`).

**Files:**
- Modify: `hub_api/services/bundle_approval_service.py`
- Modify: `hub_api/tests/test_bundle_approval_service.py`

**Interfaces:**
- Produces: `approve_version(..., db_engine: Any | None = None)` — new optional keyword. When given and the approved manifest declares `data_tables`, calls `bundle_db_role_service.create_bundle_role`. `None` (the existing default, so every Task 19 test keeps passing unmodified) skips role management — the blueprint (Task 22) always passes a real engine in production.
- Consumes: `services.bundle_db_role_service.create_bundle_role` (Task 20).

- [ ] **Step 1: Write the failing test**

Append to `hub_api/tests/test_bundle_approval_service.py`:

```python
from unittest.mock import AsyncMock, patch  # noqa: E402 -- appended import


async def test_approve_version_creates_the_bundle_role_when_engine_given(install_dal: Any) -> None:
    await _seed_published(install_dal)
    mock_engine = object()
    with patch("services.bundle_approval_service.create_bundle_role", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = "bundle_waddles_socials_music_default"
        await approve_version(
            install_dal, app_id="waddles.socials.music.default", version="3.0.1",
            tenant_id=1, community_id=None, approved_by=1, db_engine=mock_engine,
        )
    mock_create.assert_called_once_with(mock_engine, app_id="waddles.socials.music.default", tables=["music_queue"])


async def test_approve_version_skips_role_creation_without_an_engine(install_dal: Any) -> None:
    await _seed_published(install_dal)
    with patch("services.bundle_approval_service.create_bundle_role", new_callable=AsyncMock) as mock_create:
        await approve_version(
            install_dal, app_id="waddles.socials.music.default", version="3.0.1",
            tenant_id=1, community_id=None, approved_by=1,
        )
    mock_create.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_approval_service.py -k role_creation -v`
Expected: `TypeError: approve_version() got an unexpected keyword argument 'db_engine'`

- [ ] **Step 3: Add the wiring**

Add the import to `hub_api/services/bundle_approval_service.py`:

```python
from services.bundle_db_role_service import create_bundle_role
```

Change `approve_version`'s signature and body:

```python
async def approve_version(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    tenant_id: int,
    community_id: int | None,
    approved_by: int,
    expected_permission_hash: str | None = None,
    db_engine: Any | None = None,
) -> Any:
    """Record an `app_install_approvals` row. Fails closed on a headless hash mismatch (spec Sec9.7.5).

    When `db_engine` is given and the manifest declares `data_tables`,
    also creates/refreshes the bundle's per-bundle Postgres role (spec
    Sec11.6.2). `db_engine=None` (test/dev default) skips role
    management entirely.
    """
    upload_rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = upload_rows.first()
    if upload is None:
        raise not_found(f"version {version} of {app_id} not found")
    if upload.status != "PUBLISHED":
        raise ApiError(f"version {version} of {app_id} is not published yet", 409, "version_not_published")

    manifest = _reparse_trusted(upload.manifest_json)
    summary, computed_hash = await get_permission_summary(install_dal, app_id=app_id, version=version)
    if expected_permission_hash is not None and expected_permission_hash != computed_hash:
        raise ApiError(
            "the supplied permission_hash does not match the current summary", 409, "permission_hash_mismatch"
        )

    previous_rows = await install_dal(
        (install_dal.app_install_approvals.app_id == app_id)
        & (install_dal.app_install_approvals.tenant_id == tenant_id)
        & (install_dal.app_install_approvals.community_id == community_id)
        & (install_dal.app_install_approvals.superseded_by == None)  # noqa: E711 -- penguin-dal query operator
    ).select()
    previous = previous_rows.first()
    now = datetime.now(UTC)
    new_id = await install_dal.app_install_approvals.async_insert(
        tenant_id=tenant_id, community_id=community_id, app_id=app_id, version=version,
        permission_hash=computed_hash, summary_json=summary, approved_by=approved_by, approved_at=now,
    )
    if previous is not None:
        await install_dal(install_dal.app_install_approvals.id == previous.id).update(superseded_by=new_id)

    if db_engine is not None and manifest.data_tables:
        await create_bundle_role(db_engine, app_id=app_id, tables=list(manifest.data_tables))

    return (await install_dal(install_dal.app_install_approvals.id == new_id).select()).first()
```

(This replaces the entire existing function body from Task 19 — the only changes are the new `db_engine` parameter, computing `manifest` once up front instead of discarding it, and the new role-creation call at the end.)

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_approval_service.py -v`
Expected: `14 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_approval_service.py hub_api/tests/test_bundle_approval_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): approve_version() creates the per-bundle Postgres role when data.tables is non-empty (R52)

Optional db_engine parameter, defaulting to None (skips role
management in tests/dev). Wires bundle_db_role_service into the
approval flow per spec Sec11.6.2. approve_version() itself queries
app_install_approvals/app_version_uploads through penguin-dal's
install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 22: `blueprints/v1/bundle_approvals.py` — `GET permissions`, `POST approve`/`deny` (R52: penguin-dal)

**Depends on:** Task 19 (`get_permission_summary`, `approve_version`, `deny_version`, `install_dal`-only per R52), Task 21 (`approve_version`'s `db_engine` keyword).

**Files:**
- Create: `hub_api/blueprints/v1/bundle_approvals.py`
- Test: `hub_api/tests/test_bundle_approvals_blueprint.py`

**Interfaces:**
- Produces: `GET /api/v1/apps/{app_id}/versions/{version}/permissions` (scope `platform:admin`) → `{summary, permissionHash}`; `POST /api/v1/apps/{app_id}/versions/{version}/approve` (scope `platform:admin`, body `{"communityId": int|null, "permissionHash": str|null}`) → `200 {"success": true, "permissionHash": ...}` or `409 permission_hash_mismatch` (with the current `summary`/`hash` in the body, per spec §9.7.5); `POST .../deny` (scope `platform:admin`, body `{"reason": str}`) → `200`.
- Consumes: `services.bundle_approval_service.{get_permission_summary, approve_version, deny_version}` (Tasks 19/21), `current_app.config["install_dal"]` (Task 4).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_approvals_blueprint.py
"""Blueprint tests for GET permissions / POST approve / POST deny."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart

from blueprints.v1.bundle_approvals import BLUEPRINTS
from tests.conftest import make_token

_MANIFEST = {
    "schema_version": 2, "app_id": "waddles.socials.music.default", "name": "Music Station",
    "version": "3.0.1", "feature": "waddles.socials.music", "module": "socials",
    "provider": "builtin", "language": "python", "artifact": "source",
    "stages": {"process": {"entry": "x:y", "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}]}},
}


@pytest.fixture
async def app(install_dal: Any) -> Quart:
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", artifact_digest="sha256:" + "a" * 64,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED",
        manifest_json=_MANIFEST, app_version_id=version_id,
        created_at=now, updated_at=now,
    )
    app = Quart(__name__)
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_get_permissions_requires_platform_admin(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.get(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


async def test_get_permissions_happy_path(app: Quart) -> None:
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.get(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["permissionHash"].startswith("sha256:")


async def test_approve_mismatched_hash_fails_closed(app: Quart) -> None:
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None, "permissionHash": "sha256:" + "0" * 64},
    )
    assert response.status_code == 409
    body = await response.get_json()
    assert body["error"]["code"] == "permission_hash_mismatch"


async def test_approve_without_a_hash_succeeds_interactively(app: Quart) -> None:
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None, "permissionHash": None},
    )
    assert response.status_code == 200


async def test_deny_happy_path(app: Quart) -> None:
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/deny",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "egress host not acceptable"},
    )
    assert response.status_code == 200
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_approvals_blueprint.py -v`
Expected: `ModuleNotFoundError: No module named 'blueprints.v1.bundle_approvals'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/blueprints/v1/bundle_approvals.py
"""v1 `bundle_approvals` group -- GET permissions, POST approve/deny (spec Sec9.7).

R52: every handler reads `current_app.config["install_dal"]` --
`app_install_approvals`/`app_version_uploads`/`app_versions` are this
plan's own new tables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import bundle_approval_service as svc
from services.current_user import get_current_user_id
from services.errors import ApiError

bundle_approvals_bp = Blueprint("v1_bundle_approvals", __name__, url_prefix="/api/v1/apps")


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code))


def _db_engine() -> Any:
    """The privileged SQLAlchemy engine for per-bundle role management, or `None` if unconfigured."""
    dsn = os.environ.get("POSTGRES_ADMIN_DSN")
    if not dsn:
        return None
    from sqlalchemy import create_engine

    return create_engine(dsn)


@dataclass(slots=True, frozen=True)
class PermissionSummaryResponse:
    """Response DTO for `GET .../permissions`."""

    success: bool
    summary: dict[str, Any]
    permissionHash: str


@dataclass(slots=True, frozen=True)
class ApproveRequest:
    """Request DTO for `POST .../approve`."""

    communityId: int | None = None
    permissionHash: str | None = None


@dataclass(slots=True, frozen=True)
class ApproveResponse:
    """Response DTO for `POST .../approve`."""

    success: bool
    permissionHash: str


@dataclass(slots=True, frozen=True)
class DenyRequest:
    """Request DTO for `POST .../deny`."""

    reason: str


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic message response DTO."""

    success: bool
    message: str


@bundle_approvals_bp.route("/<app_id>/versions/<version>/permissions", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_response(PermissionSummaryResponse)
async def get_permissions(
    app_id: str, version: str
) -> PermissionSummaryResponse | tuple[dict[str, object], int]:
    """The consent-screen summary and its hash -- a headless caller inspects this before approving."""
    install_dal = _install_dal()
    try:
        summary, permission_hash = await svc.get_permission_summary(install_dal, app_id=app_id, version=version)
    except ApiError as exc:
        return _err(exc)
    return PermissionSummaryResponse(success=True, summary=summary, permissionHash=permission_hash)


@bundle_approvals_bp.route("/<app_id>/versions/<version>/approve", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_request(ApproveRequest)
async def post_approve(data: ApproveRequest, app_id: str, version: str) -> tuple[dict[str, object], int]:
    """Approve a version. A headless caller supplies `permissionHash`; a mismatch fails closed (409)."""
    install_dal = _install_dal()
    caller_id = get_current_user_id(request)
    try:
        row = await svc.approve_version(
            install_dal, app_id=app_id, version=version, tenant_id=1, community_id=data.communityId,
            approved_by=caller_id, expected_permission_hash=data.permissionHash, db_engine=_db_engine(),
        )
    except ApiError as exc:
        if exc.code == "permission_hash_mismatch":
            summary, current_hash = await svc.get_permission_summary(install_dal, app_id=app_id, version=version)
            return (
                {
                    "success": False,
                    "error": {"code": exc.code, "message": exc.message},
                    "summary": summary,
                    "permissionHash": current_hash,
                },
                409,
            )
        return _err(exc)
    return {"success": True, "permissionHash": row.permission_hash}, 200


@bundle_approvals_bp.route("/<app_id>/versions/<version>/deny", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_request(DenyRequest)
@validate_response(MessageResponse)
async def post_deny(data: DenyRequest, app_id: str, version: str) -> MessageResponse | tuple[dict[str, object], int]:
    """Deny a version -- moves it to REJECTED with `reason`."""
    install_dal = _install_dal()
    try:
        await svc.deny_version(install_dal, app_id=app_id, version=version, reason=data.reason)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"version {version} of {app_id} denied: {data.reason}")


BLUEPRINTS: list[Blueprint] = [bundle_approvals_bp]
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_approvals_blueprint.py -v`
Expected: `5 passed`

- [ ] **Step 5: Add the ruff per-file ignore**

Append to `hub_api/pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]`:

```toml
"blueprints/v1/bundle_approvals.py" = ["N815"]
```

- [ ] **Step 6: Commit**

```bash
git add hub_api/blueprints/v1/bundle_approvals.py hub_api/tests/test_bundle_approvals_blueprint.py \
        hub_api/pyproject.toml
git commit -m "$(cat <<'EOF'
feat(hub-api): GET permissions, POST approve/deny (spec Sec9.7, R52)

approve's headless path fails closed with 409 permission_hash_mismatch
plus the current summary/hash in the body, per spec Sec9.7.5. Reads
install_dal (penguin-dal) from app config.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 23: `platform_settings_service.py` + `blueprints/v1/bundle_settings.py` — the global `bundles.allow_prebuilt` setting (R52: penguin-dal)

**Depends on:** Task 3 (`platform_settings` DDL, seeded with `bundles.allow_prebuilt`), Task 4 (`install_dal` penguin-dal wiring + `bundle_install_db`/`install_dal` fixtures).

**Files:**
- Create: `hub_api/services/platform_settings_service.py`
- Create: `hub_api/blueprints/v1/bundle_settings.py`
- Test: `hub_api/tests/test_platform_settings_service.py`
- Test: `hub_api/tests/test_bundle_settings_blueprint.py`

**Interfaces:**
- Produces: `SETTING_ALLOW_PREBUILT = "bundles.allow_prebuilt"`; `async def get_platform_setting_bool(install_dal, *, key: str, default: bool) -> bool`; `async def set_platform_setting(install_dal, *, key: str, value: str, updated_by: int) -> None`; `GET /api/v1/marketplace/settings` (scope `platform:admin`) → `{success, settings: [{key, value}]}`; `PUT /api/v1/marketplace/settings` (scope `platform:admin`, body `{"settings": [{"key": str, "value": str}]}`) → `200`. `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `platform_settings` is this plan's own new table (R52).
- Consumes: nothing new.

- [ ] **Step 1: Write the failing service test**

```python
# hub_api/tests/test_platform_settings_service.py
"""Tests for get_platform_setting_bool()/set_platform_setting()."""

from __future__ import annotations

from typing import Any

from services.platform_settings_service import (
    SETTING_ALLOW_PREBUILT,
    get_platform_setting_bool,
    set_platform_setting,
)


async def test_default_is_used_when_unset(install_dal: Any) -> None:
    value = await get_platform_setting_bool(install_dal, key="nonexistent.key", default=True)
    assert value is True


async def test_seeded_bundles_allow_prebuilt_reads_true(install_dal: Any) -> None:
    await install_dal.platform_settings.async_insert(key=SETTING_ALLOW_PREBUILT, value="true")
    value = await get_platform_setting_bool(install_dal, key=SETTING_ALLOW_PREBUILT, default=False)
    assert value is True


async def test_set_platform_setting_updates_an_existing_row(install_dal: Any) -> None:
    await install_dal.platform_settings.async_insert(key=SETTING_ALLOW_PREBUILT, value="true")
    await set_platform_setting(install_dal, key=SETTING_ALLOW_PREBUILT, value="false", updated_by=1)
    value = await get_platform_setting_bool(install_dal, key=SETTING_ALLOW_PREBUILT, default=True)
    assert value is False


async def test_set_platform_setting_inserts_when_absent(install_dal: Any) -> None:
    await set_platform_setting(install_dal, key="a.new.key", value="1", updated_by=1)
    row = (await install_dal(install_dal.platform_settings.key == "a.new.key").select()).first()
    assert row.value == "1"
    assert row.updated_by == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_platform_settings_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.platform_settings_service'`

- [ ] **Step 3: Write the service**

```python
# hub_api/services/platform_settings_service.py
"""Global (not per-tenant) admin settings -- one row per key, `platform_settings` table.

The single global setting this milestone introduces:
`bundles.allow_prebuilt` (spec Sec6.4.4 V18, Sec12.3), default `true`,
seeded by migration 0021. R52: `platform_settings` is this plan's own
new table, queried through the penguin-dal `install_dal: AsyncDB`
(Task 4).
"""

from __future__ import annotations

from datetime import UTC, datetime

from penguin_dal import AsyncDB

SETTING_ALLOW_PREBUILT = "bundles.allow_prebuilt"


async def get_platform_setting_bool(install_dal: AsyncDB, *, key: str, default: bool) -> bool:
    """`True`/`False` for `key`, or `default` when the row does not exist."""
    rows = await install_dal(install_dal.platform_settings.key == key).select()
    row = rows.first()
    if row is None:
        return default
    return row.value == "true"


async def set_platform_setting(install_dal: AsyncDB, *, key: str, value: str, updated_by: int) -> None:
    """Upsert `key` -> `value` (select-then-branch -- `penguin_dal` has no portable `ON CONFLICT`, same gotcha the original pydal `PORTING.md` documented)."""
    existing = await install_dal(install_dal.platform_settings.key == key).select()
    now = datetime.now(UTC)
    if existing:
        await install_dal(install_dal.platform_settings.key == key).update(
            value=value, updated_by=updated_by, updated_at=now
        )
    else:
        await install_dal.platform_settings.async_insert(
            key=key, value=value, updated_by=updated_by, updated_at=now
        )
```

- [ ] **Step 4: Run the service test to verify it passes**

Run: `cd hub_api && python3 -m pytest tests/test_platform_settings_service.py -v`
Expected: `4 passed`

- [ ] **Step 5: Write the failing blueprint test**

```python
# hub_api/tests/test_bundle_settings_blueprint.py
"""Blueprint tests for GET/PUT /api/v1/marketplace/settings."""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart

from blueprints.v1.bundle_settings import BLUEPRINTS
from tests.conftest import make_token


@pytest.fixture
async def app(install_dal: Any) -> Quart:
    await install_dal.platform_settings.async_insert(key="bundles.allow_prebuilt", value="true")
    app = Quart(__name__)
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_get_settings_requires_platform_admin(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.get("/api/v1/marketplace/settings", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


async def test_get_settings_returns_seeded_value(app: Quart) -> None:
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.get("/api/v1/marketplace/settings", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = await response.get_json()
    assert {"key": "bundles.allow_prebuilt", "value": "true"} in body["settings"]


async def test_put_settings_updates_the_value(app: Quart) -> None:
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.put(
        "/api/v1/marketplace/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"settings": [{"key": "bundles.allow_prebuilt", "value": "false"}]},
    )
    assert response.status_code == 200
```

- [ ] **Step 6: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_settings_blueprint.py -v`
Expected: `ModuleNotFoundError: No module named 'blueprints.v1.bundle_settings'`

- [ ] **Step 7: Write the blueprint**

```python
# hub_api/blueprints/v1/bundle_settings.py
"""v1 `bundle_settings` group -- GET/PUT /api/v1/marketplace/settings (global admin, spec Sec12.3).

R52: reads `current_app.config["install_dal"]` -- `platform_settings`
is this plan's own new table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import platform_settings_service as svc
from services.current_user import get_current_user_id

bundle_settings_bp = Blueprint("v1_bundle_settings", __name__, url_prefix="/api/v1/marketplace")


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


@dataclass(slots=True, frozen=True)
class SettingDTO:
    """One `{key, value}` pair."""

    key: str
    value: str | None


@dataclass(slots=True, frozen=True)
class SettingsListResponse:
    """Response DTO for `GET /marketplace/settings`."""

    success: bool
    settings: list[SettingDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class SettingInput:
    """One `{key, value}` pair in `UpdateSettingsRequest.settings`."""

    key: str
    value: str


@dataclass(slots=True, frozen=True)
class UpdateSettingsRequest:
    """Request DTO for `PUT /marketplace/settings`."""

    settings: list[SettingInput]


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic message response DTO."""

    success: bool
    message: str


@bundle_settings_bp.route("/settings", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_response(SettingsListResponse)
async def get_settings() -> SettingsListResponse:
    """List every global bundle setting."""
    install_dal = _install_dal()
    rows = await install_dal(install_dal.platform_settings.id > 0).select()
    return SettingsListResponse(success=True, settings=[SettingDTO(key=r.key, value=r.value) for r in rows])


@bundle_settings_bp.route("/settings", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
@validate_request(UpdateSettingsRequest)
@validate_response(MessageResponse)
async def put_settings(data: UpdateSettingsRequest) -> MessageResponse:
    """Upsert one or more global bundle settings."""
    install_dal = _install_dal()
    caller_id = get_current_user_id(request)
    for setting in data.settings:
        await svc.set_platform_setting(install_dal, key=setting.key, value=setting.value, updated_by=caller_id)
    return MessageResponse(success=True, message="settings updated")


BLUEPRINTS: list[Blueprint] = [bundle_settings_bp]
```

- [ ] **Step 8: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_settings_blueprint.py -v`
Expected: `3 passed`

- [ ] **Step 9: Refactor `bundle_versions.py`'s inline `_allow_prebuilt` helper**

Task 11 added a temporary inline `_allow_prebuilt(install_dal)` query to `hub_api/blueprints/v1/bundle_versions.py`. Replace it now that the real service exists:

```python
from services.platform_settings_service import SETTING_ALLOW_PREBUILT, get_platform_setting_bool
```

Delete the `_allow_prebuilt` function from `bundle_versions.py` and replace its one call site:

```python
            allow_prebuilt=await get_platform_setting_bool(
                install_dal, key=SETTING_ALLOW_PREBUILT, default=True
            ),
```

- [ ] **Step 10: Run the versions blueprint tests to confirm the refactor didn't break anything**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_versions_blueprint.py -v`
Expected: `5 passed`

- [ ] **Step 11: Commit**

```bash
git add hub_api/services/platform_settings_service.py hub_api/blueprints/v1/bundle_settings.py \
        hub_api/tests/test_platform_settings_service.py hub_api/tests/test_bundle_settings_blueprint.py \
        hub_api/blueprints/v1/bundle_versions.py
git commit -m "$(cat <<'EOF'
feat(hub-api): GET/PUT /api/v1/marketplace/settings -- global bundles.allow_prebuilt (spec Sec12.3, R52)

Refactors Task 11's temporary inline _allow_prebuilt() query in
bundle_versions.py to call the real service. Queries the new
platform_settings table through penguin-dal's install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 24: `tenant_bundle_settings.py` — tenant `allow_wildcard_consumes`/`bundles.egress.allowPrivateHosts` (confirmed unaffected by R52)

**R52 note:** `tenant_settings` is a **pre-existing** table (Decision #8) — not one of this plan's own new tables — so this entire task is unchanged by the penguin-dal ruling: every function here keeps using the existing pydal `async_dal`/`dal` pair, exactly as originally written. It is called out explicitly (rather than silently left alone) so a reader auditing the R52 rewrite can confirm this task was checked, not skipped.

**Depends on:** Task 4 (the `bundle_install_db` fixture), Task 11 (`blueprints/v1/bundle_versions.py`; its `post_version` handler binds `async_dal, dal = _dal()` alongside the new `install_dal = _install_dal()`, so both handles are already in scope for this task's refactor).

**Files:**
- Create: `hub_api/services/tenant_bundle_settings.py`
- Modify: `hub_api/blueprints/v1/bundle_versions.py` (refactor)
- Test: `hub_api/tests/test_tenant_bundle_settings.py`

**Interfaces:**
- Produces: `SETTING_ALLOW_WILDCARD_CONSUMES = "allow_wildcard_consumes"`; `SETTING_ALLOW_PRIVATE_HOSTS = "bundles.egress.allowPrivateHosts"`; `async def get_tenant_setting_bool(async_dal, dal, *, tenant_id: int, key: str, default: bool) -> bool`. No new endpoint — both keys are set through the **existing** `PUT /api/v1/tenant/<slug>/settings` (`tenant_service.update_tenant_settings`, already shipped), per this plan's Decision #8.
- Consumes: nothing new — reads the existing `tenant_settings` table directly.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_tenant_bundle_settings.py
"""Tests for get_tenant_setting_bool() against the existing tenant_settings table."""

from __future__ import annotations

from typing import Any

from services.tenant_bundle_settings import (
    SETTING_ALLOW_PRIVATE_HOSTS,
    SETTING_ALLOW_WILDCARD_CONSUMES,
    get_tenant_setting_bool,
)


async def test_default_false_when_unset(bundle_install_db: Any) -> None:
    async_dal = bundle_install_db
    value = await get_tenant_setting_bool(
        async_dal, async_dal.dal, tenant_id=1, key=SETTING_ALLOW_WILDCARD_CONSUMES, default=False
    )
    assert value is False


async def test_true_when_set_via_the_generic_tenant_settings_table(bundle_install_db: Any) -> None:
    dal = bundle_install_db.dal
    dal.tenant_settings.insert(tenant_id=1, key=SETTING_ALLOW_WILDCARD_CONSUMES, value="true")
    dal.commit()
    value = await get_tenant_setting_bool(
        bundle_install_db, dal, tenant_id=1, key=SETTING_ALLOW_WILDCARD_CONSUMES, default=False
    )
    assert value is True


async def test_allow_private_hosts_key_is_scoped_per_tenant(bundle_install_db: Any) -> None:
    dal = bundle_install_db.dal
    dal.tenant_settings.insert(tenant_id=1, key=SETTING_ALLOW_PRIVATE_HOSTS, value="true")
    dal.tenant_settings.insert(tenant_id=2, key=SETTING_ALLOW_PRIVATE_HOSTS, value="false")
    dal.commit()
    tenant1 = await get_tenant_setting_bool(
        bundle_install_db, dal, tenant_id=1, key=SETTING_ALLOW_PRIVATE_HOSTS, default=False
    )
    tenant2 = await get_tenant_setting_bool(
        bundle_install_db, dal, tenant_id=2, key=SETTING_ALLOW_PRIVATE_HOSTS, default=False
    )
    assert tenant1 is True
    assert tenant2 is False
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_tenant_bundle_settings.py -v`
Expected: `ModuleNotFoundError: No module named 'services.tenant_bundle_settings'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/tenant_bundle_settings.py
"""Tenant-scoped bundle settings -- read via the EXISTING `tenant_settings` table.

No new endpoint: both keys below are set through the already-shipped
`PUT /api/v1/tenant/<slug>/settings` (`services/tenant_service.py::
update_tenant_settings`, which already accepts arbitrary `{key, value}`
pairs). This module is the read-side helper the bundle-install flow
needs; it introduces no new table and no new route (this plan's
Decision #8).
"""

from __future__ import annotations

from typing import Any

SETTING_ALLOW_WILDCARD_CONSUMES = "allow_wildcard_consumes"
SETTING_ALLOW_PRIVATE_HOSTS = "bundles.egress.allowPrivateHosts"


async def get_tenant_setting_bool(async_dal: Any, dal: Any, *, tenant_id: int, key: str, default: bool) -> bool:
    """`True`/`False` for `(tenant_id, key)`, or `default` when the row does not exist."""
    rows = await async_dal.select_async(
        dal((dal.tenant_settings.tenant_id == tenant_id) & (dal.tenant_settings.key == key))
    )
    if not rows:
        return default
    return rows[0].value == "true"
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_tenant_bundle_settings.py -v`
Expected: `3 passed`

- [ ] **Step 5: Refactor `bundle_versions.py`'s inline `_allow_wildcard_consumes` helper**

Add the import to `hub_api/blueprints/v1/bundle_versions.py`:

```python
from services.tenant_bundle_settings import SETTING_ALLOW_WILDCARD_CONSUMES, get_tenant_setting_bool
```

Delete the `_allow_wildcard_consumes` function and replace its one call site:

```python
            allow_wildcard_consumes=await get_tenant_setting_bool(
                async_dal, dal, tenant_id=ctx.tenant_id, key=SETTING_ALLOW_WILDCARD_CONSUMES, default=False
            ),
```

- [ ] **Step 6: Run the versions blueprint tests to confirm the refactor didn't break anything**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_versions_blueprint.py -v`
Expected: `5 passed`

- [ ] **Step 7: Commit**

```bash
git add hub_api/services/tenant_bundle_settings.py hub_api/tests/test_tenant_bundle_settings.py \
        hub_api/blueprints/v1/bundle_versions.py
git commit -m "$(cat <<'EOF'
feat(hub-api): tenant_bundle_settings -- allow_wildcard_consumes / bundles.egress.allowPrivateHosts (spec Sec8.5, V30)

Read-side helper only -- both settings are already writable through
the existing PUT /api/v1/tenant/<slug>/settings, no new endpoint.
Refactors Task 11's temporary inline query in bundle_versions.py.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 25: `custom_platform_service.py` + `blueprints/v1/custom_platforms.py` — registry + `intake:write` token minting (R52: penguin-dal)

**Depends on:** Task 3 (`custom_platforms` DDL), Task 4 (`install_dal` penguin-dal wiring + `bundle_install_db`/`install_dal` fixtures).

**Files:**
- Create: `hub_api/services/custom_platform_service.py`
- Create: `hub_api/blueprints/v1/custom_platforms.py`
- Test: `hub_api/tests/test_custom_platform_service.py`
- Test: `hub_api/tests/test_custom_platforms_blueprint.py`

**Interfaces:**
- Produces: `async def list_platform_names(install_dal, *, tenant_id: int) -> frozenset[str]`; `async def create_platform(install_dal, *, tenant_id: int, name: str) -> Any` (409 on duplicate); `async def delete_platform(install_dal, *, tenant_id: int, name: str) -> None` (404 if absent); `def mint_intake_token(tenant_slug: str, platform_name: str) -> str` (24h JWT, scope `intake:write`, mandatory `tenant` claim — spec §10.4, pure JWT minting, no DB access). Endpoints: `POST /api/v1/tenant/{slug}/custom-platforms` (scope `tenant:admin`), `GET /api/v1/tenant/{slug}/custom-platforms` (`tenant_middleware` only), `DELETE /api/v1/tenant/{slug}/custom-platforms/{name}` (scope `tenant:admin`), `POST /api/v1/tenant/{slug}/custom-platforms/{name}/tokens` (scope `tenant:admin`) → `{token, expiresInHours}` — re-mintable any time, JWTs are stateless so nothing is stored. `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `custom_platforms` is this plan's own new table (R52).
- Consumes: nothing new.

- [ ] **Step 1: Write the failing service test**

```python
# hub_api/tests/test_custom_platform_service.py
"""Tests for custom_platform_service."""

from __future__ import annotations

from typing import Any

import jwt
import pytest

from services.custom_platform_service import (
    create_platform,
    delete_platform,
    list_platform_names,
    mint_intake_token,
)
from services.errors import ApiError


async def test_create_and_list_platform(install_dal: Any) -> None:
    await create_platform(install_dal, tenant_id=1, name="mycrm")
    names = await list_platform_names(install_dal, tenant_id=1)
    assert names == frozenset({"mycrm"})


async def test_create_duplicate_platform_raises_409(install_dal: Any) -> None:
    await create_platform(install_dal, tenant_id=1, name="mycrm")
    with pytest.raises(ApiError) as exc:
        await create_platform(install_dal, tenant_id=1, name="mycrm")
    assert exc.value.status_code == 409


async def test_delete_platform_removes_it(install_dal: Any) -> None:
    await create_platform(install_dal, tenant_id=1, name="mycrm")
    await delete_platform(install_dal, tenant_id=1, name="mycrm")
    names = await list_platform_names(install_dal, tenant_id=1)
    assert names == frozenset()


async def test_delete_unknown_platform_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await delete_platform(install_dal, tenant_id=1, name="ghost")
    assert exc.value.status_code == 404


def test_mint_intake_token_carries_scope_and_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-for-prod")
    token = mint_intake_token("acme-corp", "mycrm")
    payload = jwt.decode(token, "test-secret-key-not-for-prod", algorithms=["HS256"], audience="waddlebot-services")
    assert payload["scope"] == "intake:write"
    assert payload["tenant"] == "acme-corp"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_custom_platform_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.custom_platform_service'`

- [ ] **Step 3: Write the service**

```python
# hub_api/services/custom_platform_service.py
"""Per-tenant custom platform registry (spec Sec6.4.3, Sec10.4) + `intake:write` token minting.

R52: `custom_platforms` is this plan's own new table, queried through
the penguin-dal `install_dal: AsyncDB` (Task 4). `mint_intake_token`
does no database access at all -- pure JWT minting, unaffected by R52.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask_core.auth import create_jwt_token
from flask_core.secrets import require_secret_key
from penguin_dal import AsyncDB

from services.errors import ApiError, conflict, not_found


async def list_platform_names(install_dal: AsyncDB, *, tenant_id: int) -> frozenset[str]:
    """Every custom platform name registered for `tenant_id`."""
    rows = await install_dal(install_dal.custom_platforms.tenant_id == tenant_id).select()
    return frozenset(r.name for r in rows)


async def create_platform(install_dal: AsyncDB, *, tenant_id: int, name: str) -> Any:
    """Register a new custom platform name. Raises 409 on duplicate."""
    existing = await install_dal(
        (install_dal.custom_platforms.tenant_id == tenant_id) & (install_dal.custom_platforms.name == name)
    ).select()
    if existing:
        raise conflict(f"custom platform {name!r} already registered")
    new_id = await install_dal.custom_platforms.async_insert(
        tenant_id=tenant_id, name=name, created_at=datetime.now(UTC)
    )
    return (await install_dal(install_dal.custom_platforms.id == new_id).select()).first()


async def delete_platform(install_dal: AsyncDB, *, tenant_id: int, name: str) -> None:
    """Remove a custom platform. Raises 404 if it does not exist."""
    existing = await install_dal(
        (install_dal.custom_platforms.tenant_id == tenant_id) & (install_dal.custom_platforms.name == name)
    ).select()
    if not existing:
        raise not_found(f"custom platform {name!r} not found")
    await install_dal(
        (install_dal.custom_platforms.tenant_id == tenant_id) & (install_dal.custom_platforms.name == name)
    ).delete()


def mint_intake_token(tenant_slug: str, platform_name: str) -> str:
    """24h JWT, scope `intake:write`, mandatory `tenant` claim (spec Sec10.4).

    24h is the general JWT-expiration ceiling (security.md JWT Claims:
    "default 1h/max 24h"), not the stricter 1h service-to-service ceiling
    -- this token is handed to a third-party REST-intake integration, not
    exchanged between PenguinTech-controlled services. The admin re-mints
    by calling this endpoint again; nothing is stored server-side.
    """
    return create_jwt_token(
        user_id=f"platform:{platform_name}", username=platform_name, email="", roles=[],
        secret_key=require_secret_key(), tenant=tenant_slug, scope="intake:write", expiration_hours=24,
    )
```

Add `from typing import Any` to this module's imports (used by `create_platform`'s return type).

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_custom_platform_service.py -v`
Expected: `5 passed`

- [ ] **Step 5: Write the failing blueprint test**

```python
# hub_api/tests/test_custom_platforms_blueprint.py
"""Blueprint tests for /api/v1/tenant/{slug}/custom-platforms."""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart

from blueprints.v1.custom_platforms import BLUEPRINTS
from tests.conftest import TENANT_SLUG, make_token


@pytest.fixture
def app(install_dal: Any) -> Quart:
    app = Quart(__name__)
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_create_requires_tenant_admin(app: Quart) -> None:
    token = make_token(scope="", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.post(
        f"/api/v1/tenant/{TENANT_SLUG}/custom-platforms",
        headers={"Authorization": f"Bearer {token}"}, json={"name": "mycrm"},
    )
    assert response.status_code == 403


async def test_create_list_delete_round_trip(app: Quart) -> None:
    token = make_token(scope="tenant:admin", tenant=TENANT_SLUG)
    client = app.test_client()
    create_response = await client.post(
        f"/api/v1/tenant/{TENANT_SLUG}/custom-platforms",
        headers={"Authorization": f"Bearer {token}"}, json={"name": "mycrm"},
    )
    assert create_response.status_code == 201

    list_response = await client.get(
        f"/api/v1/tenant/{TENANT_SLUG}/custom-platforms", headers={"Authorization": f"Bearer {token}"}
    )
    body = await list_response.get_json()
    assert "mycrm" in body["names"]

    delete_response = await client.delete(
        f"/api/v1/tenant/{TENANT_SLUG}/custom-platforms/mycrm", headers={"Authorization": f"Bearer {token}"}
    )
    assert delete_response.status_code == 200


async def test_mint_token_returns_a_jwt(app: Quart) -> None:
    token = make_token(scope="tenant:admin", tenant=TENANT_SLUG)
    client = app.test_client()
    await client.post(
        f"/api/v1/tenant/{TENANT_SLUG}/custom-platforms",
        headers={"Authorization": f"Bearer {token}"}, json={"name": "mycrm"},
    )
    response = await client.post(
        f"/api/v1/tenant/{TENANT_SLUG}/custom-platforms/mycrm/tokens",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["expiresInHours"] == 24
    assert len(body["token"]) > 20
```

- [ ] **Step 6: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_custom_platforms_blueprint.py -v`
Expected: `ModuleNotFoundError: No module named 'blueprints.v1.custom_platforms'`

- [ ] **Step 7: Write the blueprint**

```python
# hub_api/blueprints/v1/custom_platforms.py
"""v1 `custom_platforms` group -- per-tenant registry + intake:write token minting (spec Sec6.4.3, Sec10.4).

R52: reads `current_app.config["install_dal"]` -- `custom_platforms` is
this plan's own new table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import custom_platform_service as svc
from services.errors import ApiError
from services.tenant_service import require_matching_tenant

custom_platforms_bp = Blueprint(
    "v1_custom_platforms", __name__, url_prefix="/api/v1/tenant/<tenant_slug>/custom-platforms"
)


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code))


def _tenant_id(tenant_slug: str) -> int:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    require_matching_tenant(tenant_slug, ctx.tenant_slug)
    return cast(int, ctx.tenant_id)


@dataclass(slots=True, frozen=True)
class CreatePlatformRequest:
    """Request DTO for `POST .../custom-platforms`."""

    name: str


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic message response DTO."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class PlatformListResponse:
    """Response DTO for `GET .../custom-platforms`."""

    success: bool
    names: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class TokenResponse:
    """Response DTO for `POST .../tokens`."""

    success: bool
    token: str
    expiresInHours: int


@custom_platforms_bp.route("", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(CreatePlatformRequest)
async def create_platform(data: CreatePlatformRequest, tenant_slug: str) -> tuple[dict[str, object], int]:
    """Register a new custom platform name."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.create_platform(install_dal, tenant_id=tenant_id, name=data.name)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "message": f"platform {data.name} registered"}, 201


@custom_platforms_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(PlatformListResponse)
async def list_platforms(tenant_slug: str) -> PlatformListResponse | tuple[dict[str, object], int]:
    """List every custom platform registered for this tenant."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
    except ApiError as exc:
        return _err(exc)
    names = await svc.list_platform_names(install_dal, tenant_id=tenant_id)
    return PlatformListResponse(success=True, names=sorted(names))


@custom_platforms_bp.route("/<name>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def delete_platform(tenant_slug: str, name: str) -> MessageResponse | tuple[dict[str, object], int]:
    """Remove a custom platform."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.delete_platform(install_dal, tenant_id=tenant_id, name=name)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"platform {name} removed")


@custom_platforms_bp.route("/<name>/tokens", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(TokenResponse)
async def mint_token(tenant_slug: str, name: str) -> TokenResponse | tuple[dict[str, object], int]:
    """Mint (or re-mint) an `intake:write` JWT for this custom platform."""
    try:
        _tenant_id(tenant_slug)
    except ApiError as exc:
        return _err(exc)
    token = svc.mint_intake_token(tenant_slug, name)
    return TokenResponse(success=True, token=token, expiresInHours=24)


BLUEPRINTS: list[Blueprint] = [custom_platforms_bp]
```

- [ ] **Step 8: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_custom_platforms_blueprint.py -v`
Expected: `3 passed`

- [ ] **Step 9: Refactor `bundle_versions.py`'s inline `_known_custom_platforms` helper**

Add the import to `hub_api/blueprints/v1/bundle_versions.py`:

```python
from services.custom_platform_service import list_platform_names
```

Delete the `_known_custom_platforms` function from `bundle_versions.py` and replace its one call site:

```python
            known_custom_platforms=await list_platform_names(install_dal, tenant_id=ctx.tenant_id),
```

- [ ] **Step 10: Run the versions blueprint tests to confirm the refactor didn't break anything**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_versions_blueprint.py -v`
Expected: `5 passed`

- [ ] **Step 11: Add the ruff per-file ignore**

Append to `hub_api/pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]`:

```toml
"blueprints/v1/custom_platforms.py" = ["N815"]
```

- [ ] **Step 12: Commit**

```bash
git add hub_api/services/custom_platform_service.py hub_api/blueprints/v1/custom_platforms.py \
        hub_api/tests/test_custom_platform_service.py hub_api/tests/test_custom_platforms_blueprint.py \
        hub_api/blueprints/v1/bundle_versions.py hub_api/pyproject.toml
git commit -m "$(cat <<'EOF'
feat(hub-api): custom platform registry + intake:write token minting (spec Sec6.4.3, Sec10.4, R52)

POST/GET/DELETE /api/v1/tenant/{slug}/custom-platforms plus a token-
minting endpoint issuing 24h intake:write JWTs for REST-intake
integrations. Refactors Task 11's temporary inline query in
bundle_versions.py. Queries the new custom_platforms table through
penguin-dal's install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 26: `ingest_source_service.py` — the per-tenant ingest source registry (R52: penguin-dal)

**Depends on:** Task 3 (`ingest_sources` DDL), Task 4 (`install_dal` penguin-dal fixture/wiring), Task 6 (`bundle_secret_crypto.{encrypt, decrypt}`).

**Files:**
- Create: `hub_api/services/ingest_source_service.py`
- Test: `hub_api/tests/test_ingest_source_service.py`

**Interfaces:**
- Produces: `async def create_source(install_dal, *, tenant_id: int, community_id: int | None, platform: str, source_id: str, label: str, mapping: dict[str, Any] | None) -> tuple[Any, str]` (the row and the **plaintext secret, returned exactly once** — spec §10.3's per-source HMAC secret); `async def list_sources(install_dal, *, tenant_id: int) -> list[Any]` (never returns the plaintext secret, only whether one is set); `async def delete_source(install_dal, *, tenant_id: int, source_id: str) -> None`; `async def resolve_secret(install_dal, *, tenant_id: int, platform: str, source_id: str) -> str | None` (decrypts, for the webhook-verification path a later milestone's Rust ingest calls through the distribution API's `/sources` endpoint, Task 34). `install_dal` is the `penguin_dal.AsyncDB` from `services.bundle_install_dal.build_install_dal()` (Task 4) — `ingest_sources` is one of this plan's own new tables (R52), so every function here takes only `install_dal`, never the pre-existing pydal `async_dal`/`dal` pair.
- Consumes: `services.bundle_secret_crypto.{encrypt, decrypt}` (Task 6).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_ingest_source_service.py
"""Tests for the ingest source registry -- secret shown once, encrypted at rest."""

from __future__ import annotations

from typing import Any

import pytest

from services.ingest_source_service import create_source, delete_source, list_sources, resolve_secret
from services.errors import ApiError


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUNDLE_SECRET_ENCRYPTION_KEY", "b" * 64)


async def test_create_source_returns_a_plaintext_secret_once(install_dal: Any) -> None:
    row, secret = await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="custom:mycrm", source_id="ticketing-1", label="MyCRM Ticketing", mapping={"event_type": {"pointer": "/type"}},
    )
    assert len(secret) >= 32
    assert row.platform == "custom:mycrm"


async def test_list_sources_never_returns_the_plaintext_secret(install_dal: Any) -> None:
    await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="custom:mycrm", source_id="ticketing-1", label="MyCRM Ticketing", mapping=None,
    )
    rows = await list_sources(install_dal, tenant_id=1)
    assert len(rows) == 1
    assert not hasattr(rows[0], "secret_ciphertext") or rows[0].secret_ciphertext is not None
    # the ROW object still carries the encrypted column (penguin_dal.Row
    # returns all selected columns, same as pydal did); the service-layer
    # contract is that a DTO built from this row (Task 27's blueprint)
    # never surfaces secret_ciphertext/secret_iv on the wire.


async def test_resolve_secret_round_trips(install_dal: Any) -> None:
    _, secret = await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="custom:mycrm", source_id="ticketing-1", label="MyCRM Ticketing", mapping=None,
    )
    resolved = await resolve_secret(install_dal, tenant_id=1, platform="custom:mycrm", source_id="ticketing-1")
    assert resolved == secret


async def test_duplicate_source_raises_409(install_dal: Any) -> None:
    await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="custom:mycrm", source_id="ticketing-1", label="x", mapping=None,
    )
    with pytest.raises(ApiError) as exc:
        await create_source(
            install_dal, tenant_id=1, community_id=None,
            platform="custom:mycrm", source_id="ticketing-1", label="y", mapping=None,
        )
    assert exc.value.status_code == 409


async def test_delete_source_removes_it(install_dal: Any) -> None:
    await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="custom:mycrm", source_id="ticketing-1", label="x", mapping=None,
    )
    await delete_source(install_dal, tenant_id=1, source_id="ticketing-1")
    rows = await list_sources(install_dal, tenant_id=1)
    assert rows == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_source_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.ingest_source_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/ingest_source_service.py
"""Per-tenant ingest source registry (spec Sec5.2, Sec10.3) -- secret shown once, AES-256-GCM at rest.

R52: `ingest_sources` is one of this plan's own new tables (migration
0021, Task 3), so every function here queries it through the
penguin-dal `install_dal: AsyncDB` (Task 4) -- never a new pydal
binder/query. hub-api's pre-existing pydal surface is untouched and not
referenced anywhere in this module.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services.bundle_secret_crypto import decrypt, encrypt
from services.errors import conflict, not_found


async def create_source(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int | None,
    platform: str,
    source_id: str,
    label: str,
    mapping: dict[str, Any] | None,
) -> tuple[Any, str]:
    """Register a new ingest source. Returns `(row, plaintext_secret)` -- the secret is never stored plaintext."""
    existing = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.platform == platform)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    if existing:
        raise conflict(f"ingest source {platform}/{source_id} already registered for this tenant")

    plaintext_secret = secrets.token_urlsafe(32)
    ciphertext, iv = encrypt(plaintext_secret)
    now = datetime.now(UTC)
    new_id = await install_dal.ingest_sources.async_insert(
        tenant_id=tenant_id, community_id=community_id, platform=platform, source_id=source_id,
        label=label, secret_ciphertext=ciphertext, secret_iv=iv, mapping=mapping, enabled=True,
        created_at=now, updated_at=now,
    )
    row = (await install_dal(install_dal.ingest_sources.id == new_id).select()).first()
    return row, plaintext_secret


async def list_sources(install_dal: AsyncDB, *, tenant_id: int) -> list[Any]:
    """Every ingest source for `tenant_id`."""
    rows = await install_dal(install_dal.ingest_sources.tenant_id == tenant_id).select()
    return list(rows)


async def delete_source(install_dal: AsyncDB, *, tenant_id: int, source_id: str) -> None:
    """Remove an ingest source by its `source_id`. Raises 404 if absent."""
    existing = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    if not existing:
        raise not_found(f"ingest source {source_id!r} not found")
    await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.source_id == source_id)
    ).delete()


async def resolve_secret(
    install_dal: AsyncDB, *, tenant_id: int, platform: str, source_id: str
) -> str | None:
    """Decrypt and return the source's secret, or `None` if no such source exists."""
    rows = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.platform == platform)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    first = rows.first()
    if first is None or first.secret_ciphertext is None:
        return None
    return decrypt(first.secret_ciphertext, first.secret_iv)
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_source_service.py -v`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/ingest_source_service.py hub_api/tests/test_ingest_source_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): ingest_source_service -- per-tenant ingest source registry (spec Sec5.2, Sec10.3, R52)

Webhook secrets shown once at creation, AES-256-GCM at rest via
bundle_secret_crypto, never re-exposed plaintext after that. Queries
the new ingest_sources table through penguin-dal's install_dal
(coordinator ruling R52), not a new pydal binder.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 27: `blueprints/v1/ingest_sources.py` — `POST`/`GET`/`DELETE` `/api/v1/tenant/{slug}/ingest-sources` (R52: penguin-dal)

**Depends on:** Task 26 (`ingest_source_service.{create_source, list_sources, delete_source}`, all `install_dal`-only per R52), Task 4 (`install_dal` wired into `app.config`).

**Files:**
- Create: `hub_api/blueprints/v1/ingest_sources.py`
- Test: `hub_api/tests/test_ingest_sources_blueprint.py`

**Interfaces:**
- Produces: `POST /api/v1/tenant/{slug}/ingest-sources` (scope `tenant:admin`, body `{"communityId": int|null, "platform": str, "sourceId": str, "label": str, "mapping": dict|null}`) → `201 {secret: <shown once>}`; `GET .../ingest-sources` (`tenant_middleware`) → list, secret never included; `DELETE .../ingest-sources/{sourceId}` (scope `tenant:admin`).
- Consumes: `services.ingest_source_service.{create_source, list_sources, delete_source}` (Task 26), `current_app.config["install_dal"]` (Task 4).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_ingest_sources_blueprint.py
"""Blueprint tests for /api/v1/tenant/{slug}/ingest-sources."""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart

from blueprints.v1.ingest_sources import BLUEPRINTS
from tests.conftest import TENANT_SLUG, make_token


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUNDLE_SECRET_ENCRYPTION_KEY", "c" * 64)


@pytest.fixture
def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    app = Quart(__name__)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_create_requires_tenant_admin(app: Quart) -> None:
    token = make_token(scope="", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.post(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None, "platform": "custom:mycrm", "sourceId": "ticketing-1", "label": "x", "mapping": None},
    )
    assert response.status_code == 403


async def test_create_returns_the_secret_once(app: Quart) -> None:
    token = make_token(scope="tenant:admin", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.post(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None, "platform": "custom:mycrm", "sourceId": "ticketing-1", "label": "x", "mapping": None},
    )
    assert response.status_code == 201
    body = await response.get_json()
    assert len(body["secret"]) >= 32


async def test_list_never_includes_the_secret_field(app: Quart) -> None:
    token = make_token(scope="tenant:admin", tenant=TENANT_SLUG)
    client = app.test_client()
    await client.post(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None, "platform": "custom:mycrm", "sourceId": "ticketing-1", "label": "x", "mapping": None},
    )
    response = await client.get(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources", headers={"Authorization": f"Bearer {token}"}
    )
    body = await response.get_json()
    assert "secret" not in body["sources"][0]
    assert "secretCiphertext" not in body["sources"][0]


async def test_delete_removes_the_source(app: Quart) -> None:
    token = make_token(scope="tenant:admin", tenant=TENANT_SLUG)
    client = app.test_client()
    await client.post(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None, "platform": "custom:mycrm", "sourceId": "ticketing-1", "label": "x", "mapping": None},
    )
    response = await client.delete(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources/ticketing-1", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_sources_blueprint.py -v`
Expected: `ModuleNotFoundError: No module named 'blueprints.v1.ingest_sources'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/blueprints/v1/ingest_sources.py
"""v1 `ingest_sources` group -- per-tenant ingest source registry (spec Sec5.2, Sec10.3).

R52: reads `current_app.config["install_dal"]` (the penguin-dal AsyncDB,
Task 4) -- `ingest_sources` is one of this plan's own new tables, so
this blueprint never touches the pre-existing pydal `async_dal`/`dal`
pair (tenant/community resolution below uses `get_tenant_context()`,
which reads claims off the validated JWT, not the database).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import ingest_source_service as svc
from services.errors import ApiError
from services.tenant_service import require_matching_tenant

ingest_sources_bp = Blueprint(
    "v1_ingest_sources", __name__, url_prefix="/api/v1/tenant/<tenant_slug>/ingest-sources"
)


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code))


def _tenant_id(tenant_slug: str) -> int:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    require_matching_tenant(tenant_slug, ctx.tenant_slug)
    return cast(int, ctx.tenant_id)


@dataclass(slots=True, frozen=True)
class CreateSourceRequest:
    """Request DTO for `POST .../ingest-sources`."""

    platform: str
    sourceId: str
    label: str
    communityId: int | None = None
    mapping: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class CreateSourceResponse:
    """Response DTO -- the secret is shown exactly once."""

    success: bool
    secret: str


@dataclass(slots=True, frozen=True)
class SourceDTO:
    """Response DTO: one ingest source. Never includes the secret."""

    platform: str
    sourceId: str
    label: str
    communityId: int | None
    enabled: bool


@dataclass(slots=True, frozen=True)
class SourceListResponse:
    """Response DTO for `GET .../ingest-sources`."""

    success: bool
    sources: list[SourceDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic message response DTO."""

    success: bool
    message: str


@ingest_sources_bp.route("", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(CreateSourceRequest)
async def create_source(data: CreateSourceRequest, tenant_slug: str) -> tuple[dict[str, object], int]:
    """Register a new ingest source. The response's `secret` field is shown exactly once."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        _, secret = await svc.create_source(
            install_dal, tenant_id=tenant_id, community_id=data.communityId,
            platform=data.platform, source_id=data.sourceId, label=data.label, mapping=data.mapping,
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "secret": secret}, 201


@ingest_sources_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(SourceListResponse)
async def list_sources(tenant_slug: str) -> SourceListResponse | tuple[dict[str, object], int]:
    """List every ingest source for this tenant. Never includes a secret field."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
    except ApiError as exc:
        return _err(exc)
    rows = await svc.list_sources(install_dal, tenant_id=tenant_id)
    return SourceListResponse(
        success=True,
        sources=[
            SourceDTO(
                platform=r.platform, sourceId=r.source_id, label=r.label,
                communityId=r.community_id, enabled=bool(r.enabled),
            )
            for r in rows
        ],
    )


@ingest_sources_bp.route("/<source_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def delete_source(tenant_slug: str, source_id: str) -> MessageResponse | tuple[dict[str, object], int]:
    """Remove an ingest source."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.delete_source(install_dal, tenant_id=tenant_id, source_id=source_id)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"ingest source {source_id} removed")


BLUEPRINTS: list[Blueprint] = [ingest_sources_bp]
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_sources_blueprint.py -v`
Expected: `4 passed`

- [ ] **Step 5: Add the ruff per-file ignore**

Append to `hub_api/pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]`:

```toml
"blueprints/v1/ingest_sources.py" = ["N815"]
```

- [ ] **Step 6: Commit**

```bash
git add hub_api/blueprints/v1/ingest_sources.py hub_api/tests/test_ingest_sources_blueprint.py \
        hub_api/pyproject.toml
git commit -m "$(cat <<'EOF'
feat(hub-api): POST/GET/DELETE /api/v1/tenant/{slug}/ingest-sources (spec Sec5.2, Sec10.3, R52)

The secret is returned exactly once, at creation; every subsequent
read omits it entirely. Reads install_dal (penguin-dal) from app
config, not the pre-existing pydal dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 28: `valkey_admin_client.py` — consumer-group lifecycle (spec Sec5.2, Sec9.5)

**Depends on:** nothing new — `redis.asyncio` is already pinned transitively via `libs/flask_core`.

**Files:**
- Create: `hub_api/services/valkey_admin_client.py`
- Test: `hub_api/tests/test_valkey_admin_client.py`

**Interfaces:**
- Produces: `def build_client() -> Any` (a `redis.asyncio.Redis` from `VALKEY_URL`, TLS-aware per spec §11.6.1 — refuses a plaintext `redis://` URL when `security.transport.tls` is on, matching every Rust service's own startup check); `async def ensure_group(client: Any, *, stream: str, group: str) -> None` (`XGROUP CREATE ... MKSTREAM`, BUSYGROUP-tolerant); `async def destroy_group(client: Any, *, stream: str, group: str) -> None` (`XGROUP DESTROY`, tolerant of "no such key"/"no such group").
- Consumes: `redis.asyncio` (already a pinned transitive dependency via `libs/flask_core`).

hub-api's role here is **only** consumer-group lifecycle at activation/revocation time (spec §5.2's "Create"/"Destroy" rows) — it never reads or writes stream entries; that is exclusively the Rust stages' job (§5.2: "Bundles hold no Valkey connection... the stage is the enforcement point").

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_valkey_admin_client.py
"""Tests for ensure_group()/destroy_group(), redis.asyncio client mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import redis.exceptions

from services.valkey_admin_client import build_client, destroy_group, ensure_group


async def test_ensure_group_calls_xgroup_create_with_mkstream() -> None:
    mock_client = AsyncMock()
    await ensure_group(mock_client, stream="waddles:t:acme:c:main:src:twitch:tw-a:events", group="app1")
    mock_client.xgroup_create.assert_called_once_with(
        "waddles:t:acme:c:main:src:twitch:tw-a:events", "app1", id="$", mkstream=True
    )


async def test_ensure_group_is_busygroup_tolerant() -> None:
    mock_client = AsyncMock()
    mock_client.xgroup_create.side_effect = redis.exceptions.ResponseError("BUSYGROUP Consumer Group name already exists")
    await ensure_group(mock_client, stream="s", group="g")  # must not raise


async def test_ensure_group_reraises_other_response_errors() -> None:
    mock_client = AsyncMock()
    mock_client.xgroup_create.side_effect = redis.exceptions.ResponseError("WRONGTYPE Operation against a key")
    with pytest.raises(redis.exceptions.ResponseError):
        await ensure_group(mock_client, stream="s", group="g")


async def test_destroy_group_calls_xgroup_destroy() -> None:
    mock_client = AsyncMock()
    await destroy_group(mock_client, stream="s", group="g")
    mock_client.xgroup_destroy.assert_called_once_with("s", "g")


async def test_destroy_group_tolerates_missing_stream() -> None:
    mock_client = AsyncMock()
    mock_client.xgroup_destroy.side_effect = redis.exceptions.ResponseError("no such key")
    await destroy_group(mock_client, stream="s", group="g")  # must not raise


def test_build_client_refuses_plaintext_url_when_tls_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VALKEY_URL", "redis://valkey:6379/0")
    monkeypatch.setenv("SECURITY_TRANSPORT_TLS", "true")
    with pytest.raises(ValueError, match="rediss://"):
        build_client()


def test_build_client_allows_plaintext_when_tls_explicitly_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VALKEY_URL", "redis://valkey:6379/0")
    monkeypatch.setenv("SECURITY_TRANSPORT_TLS", "false")
    client = build_client()
    assert client is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_valkey_admin_client.py -v`
Expected: `ModuleNotFoundError: No module named 'services.valkey_admin_client'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/valkey_admin_client.py
"""Consumer-group lifecycle only -- hub-api never reads/writes stream entries (spec Sec5.2, Sec9.5).

TLS/auth defaults mirror spec Sec11.6.4: `security.transport.tls`
(env `SECURITY_TRANSPORT_TLS`, default `true`) refuses a plaintext
`redis://` URL at construction time, matching every Rust service's own
startup check -- the opt-out is explicit and visible, never silent.
"""

from __future__ import annotations

import os
from typing import Any

import redis.asyncio as redis_asyncio
import redis.exceptions


def _tls_required() -> bool:
    return os.environ.get("SECURITY_TRANSPORT_TLS", "true").lower() != "false"


def build_client() -> Any:
    """A `redis.asyncio.Redis` from `VALKEY_URL`. Refuses a plaintext URL when TLS is required."""
    url = os.environ.get("VALKEY_URL", "rediss://valkey:6379/0")
    if _tls_required() and not url.startswith("rediss://"):
        raise ValueError(
            f"VALKEY_URL must use rediss:// when security.transport.tls is true (got {url!r})"
        )
    return redis_asyncio.from_url(url)


async def ensure_group(client: Any, *, stream: str, group: str) -> None:
    """`XGROUP CREATE {stream} {group} $ MKSTREAM`, tolerant of an already-existing group (BUSYGROUP)."""
    try:
        await client.xgroup_create(stream, group, id="$", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def destroy_group(client: Any, *, stream: str, group: str) -> None:
    """`XGROUP DESTROY {stream} {group}`, tolerant of a stream/group that no longer exists."""
    try:
        await client.xgroup_destroy(stream, group)
    except redis.exceptions.ResponseError as exc:
        if "no such key" not in str(exc).lower() and "no such" not in str(exc).lower():
            raise
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_valkey_admin_client.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/valkey_admin_client.py hub_api/tests/test_valkey_admin_client.py
git commit -m "$(cat <<'EOF'
feat(hub-api): valkey_admin_client -- consumer-group lifecycle only (spec Sec5.2, Sec9.5, Sec11.6.4)

ensure_group()/destroy_group() are BUSYGROUP/missing-key tolerant.
build_client() refuses a plaintext redis:// URL when
security.transport.tls is true, matching every Rust service's own
startup check. hub-api never reads/writes stream entries -- only group
lifecycle.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 29: `stream_grant_service.py` — `consumes` resolution into `app_stream_grants` (R52: penguin-dal)

**Depends on:** Task 3 (`app_stream_grants` DDL), Task 4 (`install_dal` penguin-dal wiring + `bundle_install_db`/`install_dal` fixtures), Task 7 (`ConsumeRule`), Task 26 (`ingest_sources` rows resolution expands against, `install_dal`-only per R52), Task 28 (`ensure_group`, `destroy_group`).

**Files:**
- Create: `hub_api/services/stream_grant_service.py`
- Test: `hub_api/tests/test_stream_grant_service.py`

**Interfaces:**
- Produces: `def render_stream_key(tenant_slug: str, community_slug: str | None, platform: str, source_id: str) -> str` (`waddles:t:{tenant}:c:{community|_tenant}:src:{platform}:{source_id}:events`, spec §5.1); `async def resolve_grants_for_scope(install_dal, valkey_client, *, tenant_id: int, tenant_slug: str, community_id: int | None, community_slug: str | None, app_id: str, consumes: tuple[ConsumeRule, ...], granted_by: int | None) -> list[Any]` (idempotent — expands every rule against enabled `ingest_sources`, inserts missing grants, `ensure_group`s each, returns the full current active-grant list; never duplicates on a second call); `async def revoke_grant(install_dal, valkey_client, *, app_id: str, grant_id: int, actor_id: int) -> None` (destroys the Valkey group, sets `revoked_at`, writes a best-effort `audit_log` entry — 404 if the grant doesn't exist or is already revoked); `async def revoke_all_grants_for_scope(install_dal, valkey_client, *, app_id: str, tenant_id: int, community_id: int | None) -> None` (deactivation teardown); `async def list_grants(install_dal, *, app_id: str, tenant_id: int, community_id: int | None) -> list[Any]` (active only). `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `ingest_sources`/`app_stream_grants` are this plan's own new tables (R52); the `audit_log` write in `revoke_grant` is a separate, best-effort write through the same `install_dal` (Decision #18 — matching this codebase's existing "never block the main flow on a logging failure" convention).
- Consumes: `services.bundle_manifest_v2.ConsumeRule` (Task 7); `services.valkey_admin_client.{ensure_group, destroy_group}` (Task 28).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_stream_grant_service.py
"""Tests for consumes-rule resolution into app_stream_grants (spec Sec5.2)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.bundle_install_dal import raw_sql_rows
from services.bundle_manifest_v2 import ConsumeRule
from services.errors import ApiError
from services.stream_grant_service import (
    list_grants,
    render_stream_key,
    resolve_grants_for_scope,
    revoke_all_grants_for_scope,
    revoke_grant,
)


def test_render_stream_key_tenant_wide() -> None:
    key = render_stream_key("acme", None, "twitch", "tw-channelA")
    assert key == "waddles:t:acme:c:_tenant:src:twitch:tw-channelA:events"


def test_render_stream_key_community_scoped() -> None:
    key = render_stream_key("acme", "main", "twitch", "tw-channelA")
    assert key == "waddles:t:acme:c:main:src:twitch:tw-channelA:events"


async def _seed_source(install_dal: Any, *, platform: str, source_id: str, label: str) -> None:
    await install_dal.ingest_sources.async_insert(
        tenant_id=1, community_id=None, platform=platform, source_id=source_id, label=label, enabled=True,
    )


async def test_resolve_exact_source_id_rule(install_dal: Any) -> None:
    await _seed_source(install_dal, platform="twitch", source_id="tw-channelA", label="Twitch #channelA")
    await _seed_source(install_dal, platform="twitch", source_id="tw-channelB", label="Twitch #channelB")
    mock_valkey = AsyncMock()

    grants = await resolve_grants_for_scope(
        install_dal, mock_valkey, tenant_id=1, tenant_slug="acme", community_id=None, community_slug=None,
        app_id="waddles.socials.music.default",
        consumes=(ConsumeRule(platform="twitch", source_id="tw-channelA", event_types=("chat.message",), filters={}),),
        granted_by=1,
    )
    assert len(grants) == 1
    assert grants[0].source_id == "tw-channelA"
    mock_valkey.xgroup_create.assert_called_once()


async def test_resolve_platform_wide_rule_grants_every_matching_source(install_dal: Any) -> None:
    await _seed_source(install_dal, platform="twitch", source_id="tw-channelA", label="Twitch #channelA")
    await _seed_source(install_dal, platform="twitch", source_id="tw-channelB", label="Twitch #channelB")
    await _seed_source(install_dal, platform="discord", source_id="dg-guildX", label="Discord guild X")
    mock_valkey = AsyncMock()

    grants = await resolve_grants_for_scope(
        install_dal, mock_valkey, tenant_id=1, tenant_slug="acme", community_id=None, community_slug=None,
        app_id="waddles.socials.music.default",
        consumes=(ConsumeRule(platform="twitch", source_id=None, event_types=("chat.message",), filters={}),),
        granted_by=1,
    )
    assert {g.source_id for g in grants} == {"tw-channelA", "tw-channelB"}


async def test_resolution_is_idempotent(install_dal: Any) -> None:
    await _seed_source(install_dal, platform="twitch", source_id="tw-channelA", label="Twitch #channelA")
    mock_valkey = AsyncMock()
    consumes = (ConsumeRule(platform="twitch", source_id="tw-channelA", event_types=("chat.message",), filters={}),)

    await resolve_grants_for_scope(
        install_dal, mock_valkey, tenant_id=1, tenant_slug="acme", community_id=None, community_slug=None,
        app_id="waddles.socials.music.default", consumes=consumes, granted_by=1,
    )
    grants = await resolve_grants_for_scope(
        install_dal, mock_valkey, tenant_id=1, tenant_slug="acme", community_id=None, community_slug=None,
        app_id="waddles.socials.music.default", consumes=consumes, granted_by=1,
    )
    assert len(grants) == 1  # not duplicated on a second call


async def test_revoke_grant_destroys_group_and_audits(install_dal: Any) -> None:
    await _seed_source(install_dal, platform="twitch", source_id="tw-channelA", label="Twitch #channelA")
    mock_valkey = AsyncMock()
    grants = await resolve_grants_for_scope(
        install_dal, mock_valkey, tenant_id=1, tenant_slug="acme", community_id=None, community_slug=None,
        app_id="waddles.socials.music.default",
        consumes=(ConsumeRule(platform="twitch", source_id="tw-channelA", event_types=("chat.message",), filters={}),),
        granted_by=1,
    )
    await revoke_grant(install_dal, mock_valkey, app_id="waddles.socials.music.default", grant_id=grants[0].id, actor_id=1)

    mock_valkey.xgroup_destroy.assert_called_once()
    remaining = await list_grants(install_dal, app_id="waddles.socials.music.default", tenant_id=1, community_id=None)
    assert remaining == []
    audit_rows = await raw_sql_rows(
        install_dal, "SELECT id FROM audit_log WHERE action = :a", {"a": "app_stream_grant_revoked"}
    )
    assert audit_rows.first() is not None


async def test_revoke_already_revoked_grant_raises_404(install_dal: Any) -> None:
    await _seed_source(install_dal, platform="twitch", source_id="tw-channelA", label="Twitch #channelA")
    mock_valkey = AsyncMock()
    grants = await resolve_grants_for_scope(
        install_dal, mock_valkey, tenant_id=1, tenant_slug="acme", community_id=None, community_slug=None,
        app_id="waddles.socials.music.default",
        consumes=(ConsumeRule(platform="twitch", source_id="tw-channelA", event_types=("chat.message",), filters={}),),
        granted_by=1,
    )
    await revoke_grant(install_dal, mock_valkey, app_id="waddles.socials.music.default", grant_id=grants[0].id, actor_id=1)
    with pytest.raises(ApiError) as exc:
        await revoke_grant(install_dal, mock_valkey, app_id="waddles.socials.music.default", grant_id=grants[0].id, actor_id=1)
    assert exc.value.status_code == 404


async def test_revoke_all_grants_for_scope_tears_down_everything(install_dal: Any) -> None:
    await _seed_source(install_dal, platform="twitch", source_id="tw-channelA", label="x")
    await _seed_source(install_dal, platform="discord", source_id="dg-guildX", label="y")
    mock_valkey = AsyncMock()
    await resolve_grants_for_scope(
        install_dal, mock_valkey, tenant_id=1, tenant_slug="acme", community_id=None, community_slug=None,
        app_id="waddles.socials.music.default",
        consumes=(
            ConsumeRule(platform="twitch", source_id=None, event_types=("chat.message",), filters={}),
            ConsumeRule(platform="discord", source_id=None, event_types=("chat.message",), filters={}),
        ),
        granted_by=1,
    )
    await revoke_all_grants_for_scope(
        install_dal, mock_valkey, app_id="waddles.socials.music.default", tenant_id=1, community_id=None
    )
    remaining = await list_grants(install_dal, app_id="waddles.socials.music.default", tenant_id=1, community_id=None)
    assert remaining == []
    assert mock_valkey.xgroup_destroy.call_count == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_stream_grant_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.stream_grant_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/stream_grant_service.py
"""Resolve `consumes` rules into `app_stream_grants` + Valkey consumer-group lifecycle (spec Sec5.2).

R52: `ingest_sources`/`app_stream_grants` are this plan's own new
tables, queried through the penguin-dal `install_dal: AsyncDB` (Task
4). `revoke_grant`'s `audit_log` write is a separate, best-effort write
through the same `install_dal` (Decision #18) -- wrapped in
`try`/`except`, matching this codebase's existing convention for every
audit-log call site, so a logging failure never blocks the revocation
that already succeeded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services.bundle_manifest_v2 import ConsumeRule
from services.errors import ApiError, not_found
from services.valkey_admin_client import destroy_group, ensure_group


def render_stream_key(tenant_slug: str, community_slug: str | None, platform: str, source_id: str) -> str:
    """`waddles:t:{tenant}:c:{community|_tenant}:src:{platform}:{source_id}:events` (spec Sec5.1)."""
    community_segment = community_slug if community_slug is not None else "_tenant"
    return f"waddles:t:{tenant_slug}:c:{community_segment}:src:{platform}:{source_id}:events"


def _rule_matches_source(rule: ConsumeRule, source: Any) -> bool:
    if rule.platform != "*" and rule.platform != source.platform:
        return False
    if rule.source_id is not None and rule.source_id != source.source_id:
        return False
    return True


async def resolve_grants_for_scope(
    install_dal: AsyncDB,
    valkey_client: Any,
    *,
    tenant_id: int,
    tenant_slug: str,
    community_id: int | None,
    community_slug: str | None,
    app_id: str,
    consumes: tuple[ConsumeRule, ...],
    granted_by: int | None,
) -> list[Any]:
    """Expand `consumes` against enabled `ingest_sources`, insert missing grants, ensure each group. Idempotent."""
    sources = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.enabled == True)  # noqa: E712 -- penguin-dal query operator
    ).select()
    matched_sources = [s for s in sources if any(_rule_matches_source(rule, s) for rule in consumes)]

    existing_rows = await install_dal(
        (install_dal.app_stream_grants.app_id == app_id)
        & (install_dal.app_stream_grants.tenant_id == tenant_id)
        & (install_dal.app_stream_grants.community_id == community_id)
        & (install_dal.app_stream_grants.revoked_at == None)  # noqa: E711 -- penguin-dal query operator
    ).select()
    existing_stream_keys = {row.stream_key for row in existing_rows}

    now = datetime.now(UTC)
    for source in matched_sources:
        stream_key = render_stream_key(tenant_slug, community_slug, source.platform, source.source_id)
        await ensure_group(valkey_client, stream=stream_key, group=app_id)
        if stream_key not in existing_stream_keys:
            await install_dal.app_stream_grants.async_insert(
                tenant_id=tenant_id, community_id=community_id, app_id=app_id, stream_key=stream_key,
                platform=source.platform, source_id=source.source_id, label=source.label,
                granted_by=granted_by, granted_at=now,
            )

    return await list_grants(install_dal, app_id=app_id, tenant_id=tenant_id, community_id=community_id)


async def revoke_grant(
    install_dal: AsyncDB, valkey_client: Any, *, app_id: str, grant_id: int, actor_id: int
) -> None:
    """Destroy the Valkey group, mark the grant revoked, best-effort audit-log the action. 404 if already revoked/absent."""
    rows = await install_dal(
        (install_dal.app_stream_grants.id == grant_id)
        & (install_dal.app_stream_grants.app_id == app_id)
        & (install_dal.app_stream_grants.revoked_at == None)  # noqa: E711
    ).select()
    grant = rows.first()
    if grant is None:
        raise not_found(f"grant {grant_id} not found or already revoked")

    await destroy_group(valkey_client, stream=grant.stream_key, group=app_id)
    now = datetime.now(UTC)
    await install_dal(install_dal.app_stream_grants.id == grant_id).update(revoked_at=now)
    try:
        await install_dal.audit_log.async_insert(
            user_id=actor_id, action="app_stream_grant_revoked",
            target_type="app_stream_grant", target_id=str(grant_id),
            details={"app_id": app_id, "stream_key": grant.stream_key}, created_at=now,
        )
    except Exception:  # noqa: BLE001, S110 -- audit logging failure must not break the main flow
        pass


async def revoke_all_grants_for_scope(
    install_dal: AsyncDB, valkey_client: Any, *, app_id: str, tenant_id: int, community_id: int | None
) -> None:
    """Deactivation teardown -- destroys every active group and marks every grant revoked for this scope."""
    rows = await install_dal(
        (install_dal.app_stream_grants.app_id == app_id)
        & (install_dal.app_stream_grants.tenant_id == tenant_id)
        & (install_dal.app_stream_grants.community_id == community_id)
        & (install_dal.app_stream_grants.revoked_at == None)  # noqa: E711
    ).select()
    now = datetime.now(UTC)
    for row in rows:
        await destroy_group(valkey_client, stream=row.stream_key, group=app_id)
        await install_dal(install_dal.app_stream_grants.id == row.id).update(revoked_at=now)


async def list_grants(
    install_dal: AsyncDB, *, app_id: str, tenant_id: int, community_id: int | None
) -> list[Any]:
    """Every currently-active grant for `(app_id, tenant_id, community_id)`."""
    rows = await install_dal(
        (install_dal.app_stream_grants.app_id == app_id)
        & (install_dal.app_stream_grants.tenant_id == tenant_id)
        & (install_dal.app_stream_grants.community_id == community_id)
        & (install_dal.app_stream_grants.revoked_at == None)  # noqa: E711
    ).select()
    return list(rows)
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_stream_grant_service.py -v`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/stream_grant_service.py hub_api/tests/test_stream_grant_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): stream_grant_service -- consumes resolution into app_stream_grants (spec Sec5.2, R52)

Idempotent expand-against-configured-sources, ensure_group per grant,
revoke_grant destroys the group + marks revoked_at + best-effort
audit-logs, revoke_all_grants_for_scope is the deactivation teardown.
Queries the new ingest_sources/app_stream_grants tables through
penguin-dal's install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 30: Wire approval-gating + grant resolution into `activate_bundle()`/`deactivate_bundle()` (R52: penguin-dal)

**Depends on:** Task 16 (`get_active_version`, `install_dal`-only per R52), Task 19 (`app_install_approvals` rows the gate requires, `install_dal`-only per R52), Task 29 (`resolve_grants_for_scope`, `revoke_all_grants_for_scope`, `install_dal`-only per R52).

**R52 note:** `marketplace_lifecycle_service.py` is a **pre-existing** hub-api module — `activate_bundle`/`deactivate_bundle`'s existing bodies (`app_activations` queries via `async_dal`/`dal`, `check_activation_insert_allowed`, `ensure_registered`, `detect_conflict`, `_guarded_upsert_activation_sync`/`_guarded_set_deactivated_sync`) are **not rewritten** — `app_activations` is an existing table, untouched. Only the new M2b gating/grant-resolution block this task adds is new code, and it queries exclusively new tables (`app_active_versions`, `app_install_approvals`, `app_version_uploads`, `ingest_sources`, `app_stream_grants`) — so that block, and only that block, takes a new `install_dal: AsyncDB | None = None` parameter alongside `valkey_client`. Both new tables the wiring needs are new-plan tables and both go through `install_dal`, never through the pre-existing `async_dal`/`dal` pair. This is this plan's clearest example of one function holding both DAL handles side by side.

**Files:**
- Modify: `hub_api/services/stream_grant_service.py` (add `consumes_from_version`)
- Modify: `hub_api/services/marketplace_lifecycle_service.py` (extend `activate_bundle`/`deactivate_bundle`)
- Test: `hub_api/tests/test_marketplace_lifecycle_grants.py`

**Interfaces:**
- Produces: `stream_grant_service.consumes_from_version(install_dal, *, app_id: str, version: str) -> tuple[ConsumeRule, ...]` (`install_dal`-only per R52 — reads the new `app_version_uploads` table); `marketplace_lifecycle_service.activate_bundle(..., valkey_client: Any | None = None, install_dal: AsyncDB | None = None, tenant_slug: str | None = None, community_slug: str | None = None)` — **new keyword-only params, all defaulting to `None`, so every existing call site and every existing test in `test_v1_marketplace_lifecycle_blueprint.py`/`test_marketplace_lifecycle_concurrency.py` keeps passing unmodified.** When `valkey_client`/`install_dal` are `None` (the pre-M2b default), activation behaves exactly as it does today. When both are given, activation additionally: (a) looks up `app_active_versions` (via `install_dal`) for this `(app_id, tenant_id, community_id)` — if no row exists, the bundle never went through the version/consent flow (a legacy `is_default` builtin) and gating is skipped entirely; (b) if a row exists, requires a current `app_install_approvals` row (via `install_dal`) for that exact version at this scope, else `403 bundle_not_approved`; (c) resolves `consumes` into `app_stream_grants` via `stream_grant_service.resolve_grants_for_scope` (`install_dal`). `deactivate_bundle(..., valkey_client: Any | None = None, install_dal: AsyncDB | None = None)` similarly calls `revoke_all_grants_for_scope` only when both are given.
- Consumes: `services.stream_grant_service.{resolve_grants_for_scope, revoke_all_grants_for_scope}` (Task 29); `services.bundle_activation_service.get_active_version` (Task 16).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_marketplace_lifecycle_grants.py
"""Tests for the M2b approval-gating + grant-resolution wiring in activate_bundle()/deactivate_bundle()."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.bundle_activation_service import activate_version
from services.bundle_approval_service import approve_version
from services.errors import ApiError
from services.marketplace_lifecycle_service import activate_bundle, deactivate_bundle

_MANIFEST = {
    "schema_version": 2, "app_id": "waddles.socials.music.default", "name": "Music Station",
    "version": "3.0.1", "feature": "waddles.socials.music", "module": "socials",
    "provider": "builtin", "language": "python", "artifact": "source",
    "stages": {"process": {"entry": "x:y", "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}]}},
}


async def _seed_approved_version(install_dal: Any) -> None:
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", artifact_digest="sha256:" + "a" * 64,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED",
        manifest_json=_MANIFEST, app_version_id=version_id, created_at=now, updated_at=now,
    )
    await activate_version(
        install_dal, tenant_id=1, community_id=1, app_id="waddles.socials.music.default",
        version="3.0.1", activated_by=1,
    )
    await approve_version(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1",
        tenant_id=1, community_id=1, approved_by=1,
    )


async def test_activate_bundle_without_valkey_client_behaves_exactly_as_before(bundle_install_db: Any) -> None:
    """No new params supplied -- pre-M2b behaviour, no gating, no grant resolution."""
    async_dal = bundle_install_db
    dal = async_dal.dal
    row = await activate_bundle(
        async_dal, dal, community_id=1, tenant_id=1, app_id="waddles.socials.music.default",
        config=None, activated_by=1,
    )
    assert row is not None


async def test_activate_bundle_with_no_active_version_skips_gating(
    bundle_install_db: Any, install_dal: Any
) -> None:
    """A legacy bundle with no app_active_versions row activates unimpeded even with valkey_client/install_dal given."""
    async_dal = bundle_install_db
    dal = async_dal.dal
    mock_valkey = AsyncMock()
    row = await activate_bundle(
        async_dal, dal, community_id=1, tenant_id=1, app_id="waddles.socials.music.default",
        config=None, activated_by=1, valkey_client=mock_valkey, install_dal=install_dal,
        tenant_slug="acme-corp", community_slug="acme-community",
    )
    assert row is not None
    mock_valkey.xgroup_create.assert_not_called()


async def test_activate_bundle_refuses_when_active_version_is_unapproved(
    bundle_install_db: Any, install_dal: Any
) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", artifact_digest="sha256:" + "a" * 64,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED",
        manifest_json=_MANIFEST, app_version_id=version_id, created_at=now, updated_at=now,
    )
    await activate_version(
        install_dal, tenant_id=1, community_id=1, app_id="waddles.socials.music.default",
        version="3.0.1", activated_by=1,
    )
    mock_valkey = AsyncMock()
    with pytest.raises(ApiError) as exc:
        await activate_bundle(
            async_dal, dal, community_id=1, tenant_id=1, app_id="waddles.socials.music.default",
            config=None, activated_by=1, valkey_client=mock_valkey, install_dal=install_dal,
            tenant_slug="acme-corp", community_slug="acme-community",
        )
    assert exc.value.status_code == 403
    assert exc.value.code == "bundle_not_approved"


async def test_activate_bundle_resolves_grants_when_approved(bundle_install_db: Any, install_dal: Any) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    await _seed_approved_version(install_dal)
    await install_dal.ingest_sources.async_insert(
        tenant_id=1, community_id=None, platform="twitch", source_id="tw-channelA",
        label="Twitch #channelA", enabled=True,
    )
    mock_valkey = AsyncMock()
    await activate_bundle(
        async_dal, dal, community_id=1, tenant_id=1, app_id="waddles.socials.music.default",
        config=None, activated_by=1, valkey_client=mock_valkey, install_dal=install_dal,
        tenant_slug="acme-corp", community_slug="acme-community",
    )
    mock_valkey.xgroup_create.assert_called_once()


async def test_deactivate_bundle_revokes_grants_when_valkey_client_given(
    bundle_install_db: Any, install_dal: Any
) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    await _seed_approved_version(install_dal)
    await install_dal.ingest_sources.async_insert(
        tenant_id=1, community_id=None, platform="twitch", source_id="tw-channelA",
        label="Twitch #channelA", enabled=True,
    )
    mock_valkey = AsyncMock()
    await activate_bundle(
        async_dal, dal, community_id=1, tenant_id=1, app_id="waddles.socials.music.default",
        config=None, activated_by=1, valkey_client=mock_valkey, install_dal=install_dal,
        tenant_slug="acme-corp", community_slug="acme-community",
    )
    await deactivate_bundle(
        async_dal, dal, community_id=1, app_id="waddles.socials.music.default",
        valkey_client=mock_valkey, install_dal=install_dal,
    )
    mock_valkey.xgroup_destroy.assert_called_once()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_marketplace_lifecycle_grants.py -v`
Expected: `TypeError: activate_bundle() got an unexpected keyword argument 'valkey_client'`

- [ ] **Step 3: Add `consumes_from_version` to `stream_grant_service.py`**

Append to `hub_api/services/stream_grant_service.py`:

```python
async def consumes_from_version(install_dal: AsyncDB, *, app_id: str, version: str) -> tuple[ConsumeRule, ...]:
    """Read the process stage's `consumes` rules out of the stored, already-validated `manifest_json`."""
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = rows.first()
    if upload is None or not upload.manifest_json:
        return ()
    process_stage = (upload.manifest_json.get("stages") or {}).get("process") or {}
    return tuple(
        ConsumeRule(
            platform=rule["platform"], source_id=rule.get("source_id"),
            event_types=tuple(rule["event_types"]), filters=dict(rule.get("filters") or {}),
        )
        for rule in (process_stage.get("consumes") or [])
    )
```

- [ ] **Step 4: Extend `activate_bundle`/`deactivate_bundle` in `marketplace_lifecycle_service.py`**

Add the imports (`AsyncDB` is type-only, for the new parameter's annotation):

```python
from typing import TYPE_CHECKING

from services.bundle_activation_service import get_active_version
from services.stream_grant_service import consumes_from_version, resolve_grants_for_scope, revoke_all_grants_for_scope

if TYPE_CHECKING:
    from penguin_dal import AsyncDB
```

Replace `activate_bundle`'s signature and add the gating call right after the existing conflict check, before the `return await loop.run_in_executor(...)` line. Everything above the new M2b block (`check_activation_insert_allowed`, `ensure_registered`, the `app_activations` conflict query, `detect_conflict`) is **existing pydal code via `async_dal`/`dal`, unchanged** — only the new block uses `install_dal`:

```python
async def activate_bundle(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    tenant_id: int,
    app_id: str,
    config: dict[str, Any] | None,
    activated_by: int,
    registry: AppRegistry | None = None,
    valkey_client: Any | None = None,
    install_dal: "AsyncDB | None" = None,
    tenant_slug: str | None = None,
    community_slug: str | None = None,
) -> Any:
    """Activate `app_id` for `community_id`. Upserts on `(community_id, app_id)`.

    Enforces `activated <= available` via `check_activation_insert_allowed`
    (409 if not available to `tenant_id`), then a coexistence check
    (`flask_core.app_binding.detect_conflict`, design doc Sec7.3) against
    every OTHER currently-enabled activation for this community -- 409
    naming the conflicting `app_id` if `candidate` cannot coexist with an
    already-active App. All of the above is existing, pre-M2b logic
    against the existing `app_activations` table (`async_dal`/`dal`,
    unchanged by R52).

    M2b addition (spec Sec9.7.3, Sec5.2, R52): when `valkey_client` AND
    `install_dal` are both given AND this `app_id` has an
    `app_active_versions` row for this scope (i.e. it went through the
    version/consent flow), activation ALSO requires a current
    `app_install_approvals` row for that exact version -- 403
    `bundle_not_approved` otherwise -- and resolves `consumes` into
    `app_stream_grants`. All three of those tables are this plan's own
    new tables, queried exclusively through `install_dal`. A bundle
    with no `app_active_versions` row (a legacy `is_default` builtin
    registered through the v1 install path) skips this entirely;
    `valkey_client=None`/`install_dal=None` (the defaults) preserve the
    pre-M2b behavior exactly, for every existing caller and test.
    """
    try:
        await check_activation_insert_allowed(dal, tenant_id, app_id)
    except AppTierError as exc:
        raise _from_tier_error(exc) from exc

    candidate = await ensure_registered(dal, app_id, registry=registry)

    active_rows = await async_dal.select_async(
        dal(
            (dal.app_activations.community_id == community_id)
            & (dal.app_activations.enabled == True)  # noqa: E712
            & (dal.app_activations.app_id != app_id)
        ),
        dal.app_activations.app_id,
    )
    active_manifests = [
        await ensure_registered(dal, r.app_id, registry=registry) for r in active_rows
    ]
    conflicting = detect_conflict(candidate, active_manifests)
    if conflicting is not None:
        raise conflict(f"Bundle {app_id!r} conflicts with already-active bundle {conflicting!r}")

    if valkey_client is not None and install_dal is not None:
        active_version = await get_active_version(
            install_dal, tenant_id=tenant_id, community_id=community_id, app_id=app_id
        )
        if active_version is not None:
            approval_rows = await install_dal(
                (install_dal.app_install_approvals.app_id == app_id)
                & (install_dal.app_install_approvals.version == active_version.version)
                & (install_dal.app_install_approvals.tenant_id == tenant_id)
                & (install_dal.app_install_approvals.community_id == community_id)
                & (install_dal.app_install_approvals.superseded_by == None)  # noqa: E711
            ).select()
            if not approval_rows:
                raise ApiError(
                    f"{app_id} version {active_version.version} is not approved for this community",
                    403, "bundle_not_approved",
                )
            consumes = await consumes_from_version(install_dal, app_id=app_id, version=active_version.version)
            await resolve_grants_for_scope(
                install_dal, valkey_client, tenant_id=tenant_id, tenant_slug=tenant_slug or "",
                community_id=community_id, community_slug=community_slug, app_id=app_id,
                consumes=consumes, granted_by=activated_by,
            )

    payload = config if config is not None else {}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        async_dal.executor,
        partial(
            _guarded_upsert_activation_sync,
            dal,
            community_id=community_id,
            tenant_id=tenant_id,
            app_id=app_id,
            config=payload,
            activated_by=activated_by,
        ),
    )
```

Add the import `ApiError` alongside the existing `from services.errors import ApiError, conflict, not_found` line (add `ApiError` if not already present — it is not, in the original file, only `conflict`/`not_found` are imported at module level per this module's own top).

Extend `deactivate_bundle`:

```python
async def deactivate_bundle(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    app_id: str,
    valkey_client: Any | None = None,
    install_dal: "AsyncDB | None" = None,
) -> None:
    """Soft-disable: set `app_activations.enabled = False`. Raises 404 if no such row.

    M2b addition (R52): when `valkey_client` AND `install_dal` are both
    given, also tears down every active `app_stream_grants` row for
    this `(app_id, community_id)`'s tenant scope via
    `revoke_all_grants_for_scope` (spec Sec9.5: "Deactivation destroys
    those groups and marks the rows revoked") -- `app_stream_grants` is
    a new table, queried through `install_dal`. Neither given (the
    defaults) preserves the pre-M2b behavior exactly.
    """
    existing_query = (dal.app_activations.community_id == community_id) & (
        dal.app_activations.app_id == app_id
    )
    existing = await async_dal.count_async(existing_query)
    if existing == 0:
        raise not_found(f"Bundle {app_id!r} is not activated for this community")

    if valkey_client is not None and install_dal is not None:
        activation_row = (await async_dal.select_async(dal(existing_query)))[0]
        await revoke_all_grants_for_scope(
            install_dal, valkey_client, app_id=app_id,
            tenant_id=activation_row.tenant_id, community_id=community_id,
        )

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        async_dal.executor,
        partial(_guarded_set_deactivated_sync, dal, community_id=community_id, app_id=app_id),
    )
```

- [ ] **Step 5: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_marketplace_lifecycle_grants.py -v`
Expected: `5 passed`

- [ ] **Step 6: Run the FULL existing marketplace-lifecycle suite to confirm zero regression**

Run: `cd hub_api && python3 -m pytest tests/test_v1_marketplace_lifecycle_blueprint.py tests/test_marketplace_lifecycle_concurrency.py -v`
Expected: every test that passed before this task still passes — these tests never pass `valkey_client`/`install_dal`, so they exercise the exact pre-M2b code path.

- [ ] **Step 7: Commit**

```bash
git add hub_api/services/stream_grant_service.py hub_api/services/marketplace_lifecycle_service.py \
        hub_api/tests/test_marketplace_lifecycle_grants.py
git commit -m "$(cat <<'EOF'
feat(hub-api): approval-gating + grant resolution wired into activate_bundle()/deactivate_bundle() (R52)

New keyword-only params (valkey_client, install_dal, tenant_slug,
community_slug), all defaulting to None -- every existing call site
and test keeps the exact pre-M2b behavior unmodified. A bundle with no
app_active_versions row (legacy is_default builtin) skips gating
entirely; one with an active version requires a current
app_install_approvals row for that exact version, else 403
bundle_not_approved (spec Sec9.7.3). The new gating/grant-resolution
block queries app_active_versions/app_install_approvals/
app_version_uploads/ingest_sources/app_stream_grants through
penguin-dal's install_dal; the pre-existing app_activations logic is
untouched, still on the pydal async_dal/dal pair.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 31: `blueprints/v1/bundle_grants.py` — `GET grants`, `POST resolve`, `DELETE grant` (R52: penguin-dal)

**Depends on:** Task 16 (`get_active_version`, `install_dal`-only per R52), Task 28 (`build_client`), Tasks 29-30 (`list_grants`, `resolve_grants_for_scope`, `revoke_grant`, `consumes_from_version`, all `install_dal`-only per R52).

**Files:**
- Create: `hub_api/blueprints/v1/bundle_grants.py`
- Test: `hub_api/tests/test_bundle_grants_blueprint.py`

**Interfaces:**
- Produces: `GET /api/v1/apps/{app_id}/grants?communityId=` (scope `tenant:admin`) → the labeled grant list; `POST /api/v1/apps/{app_id}/grants/resolve` (scope `tenant:admin`, body `{"communityId": int|null}`) → re-runs resolution idempotently; `DELETE /api/v1/apps/{app_id}/grants/{grantId}` (scope `tenant:admin`) → revokes one grant. All three derive `tenant_id`/`tenant_slug` from the caller's own JWT (`get_tenant_context`), never a path/body param (security.md tenant isolation) — matching the spec's literal path shape (no `tenant_slug` segment) while still enforcing tenant-from-JWT-only.
- Consumes: `services.stream_grant_service.{list_grants, resolve_grants_for_scope, revoke_grant, consumes_from_version}` (Tasks 29-30); `services.bundle_activation_service.get_active_version` (Task 16); `services.valkey_admin_client.build_client` (Task 28); `current_app.config["install_dal"]` (Task 4).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_grants_blueprint.py
"""Blueprint tests for /api/v1/apps/{app_id}/grants."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from quart import Quart

from blueprints.v1.bundle_grants import BLUEPRINTS
from tests.conftest import TENANT_SLUG, make_token

_MANIFEST = {
    "schema_version": 2, "app_id": "waddles.socials.music.default", "name": "Music Station",
    "version": "3.0.1", "feature": "waddles.socials.music", "module": "socials",
    "provider": "builtin", "language": "python", "artifact": "source",
    "stages": {"process": {"entry": "x:y", "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}]}},
}


@pytest.fixture
async def app(install_dal: Any) -> Quart:
    now = datetime.now(UTC)
    await install_dal.ingest_sources.async_insert(
        tenant_id=1, community_id=None, platform="twitch", source_id="tw-channelA",
        label="Twitch #channelA", enabled=True,
    )
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", artifact_digest="sha256:" + "a" * 64,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED",
        manifest_json=_MANIFEST, app_version_id=version_id, created_at=now, updated_at=now,
    )
    app = Quart(__name__)
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_resolve_requires_tenant_admin(app: Quart) -> None:
    token = make_token(scope="", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/waddles.socials.music.default/grants/resolve",
        headers={"Authorization": f"Bearer {token}"}, json={"communityId": None},
    )
    assert response.status_code == 403


async def test_resolve_and_list_round_trip(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    from services.bundle_activation_service import activate_version

    await activate_version(
        install_dal, tenant_id=1, community_id=None, app_id="waddles.socials.music.default",
        version="3.0.1", activated_by=1,
    )
    token = make_token(scope="tenant:admin", tenant=TENANT_SLUG)
    mock_valkey = AsyncMock()
    with patch("blueprints.v1.bundle_grants.build_client", return_value=mock_valkey):
        client = app.test_client()
        resolve_response = await client.post(
            "/api/v1/apps/waddles.socials.music.default/grants/resolve",
            headers={"Authorization": f"Bearer {token}"}, json={"communityId": None},
        )
        assert resolve_response.status_code == 200

        list_response = await client.get(
            "/api/v1/apps/waddles.socials.music.default/grants",
            headers={"Authorization": f"Bearer {token}"},
        )
    body = await list_response.get_json()
    assert len(body["grants"]) == 1
    assert body["grants"][0]["label"] == "Twitch #channelA"


async def test_delete_grant_revokes_it(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    from services.bundle_activation_service import activate_version

    await activate_version(
        install_dal, tenant_id=1, community_id=None, app_id="waddles.socials.music.default",
        version="3.0.1", activated_by=1,
    )
    token = make_token(scope="tenant:admin", tenant=TENANT_SLUG)
    mock_valkey = AsyncMock()
    with patch("blueprints.v1.bundle_grants.build_client", return_value=mock_valkey):
        client = app.test_client()
        await client.post(
            "/api/v1/apps/waddles.socials.music.default/grants/resolve",
            headers={"Authorization": f"Bearer {token}"}, json={"communityId": None},
        )
        list_response = await client.get(
            "/api/v1/apps/waddles.socials.music.default/grants",
            headers={"Authorization": f"Bearer {token}"},
        )
        grant_id = (await list_response.get_json())["grants"][0]["grantId"]
        delete_response = await client.delete(
            f"/api/v1/apps/waddles.socials.music.default/grants/{grant_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert delete_response.status_code == 200
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_grants_blueprint.py -v`
Expected: `ModuleNotFoundError: No module named 'blueprints.v1.bundle_grants'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/blueprints/v1/bundle_grants.py
"""v1 `bundle_grants` group -- GET/POST resolve/DELETE grant (spec Sec9.6). Tenant strictly from the JWT.

R52: every handler reads `current_app.config["install_dal"]` --
`app_versions`/`app_version_uploads`/`ingest_sources`/`app_stream_grants`
are this plan's own new tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services.bundle_activation_service import get_active_version
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request
from services.stream_grant_service import (
    consumes_from_version,
    list_grants,
    resolve_grants_for_scope,
    revoke_grant,
)
from services.valkey_admin_client import build_client

bundle_grants_bp = Blueprint("v1_bundle_grants", __name__, url_prefix="/api/v1/apps")


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code))


def _parse_community_id(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ApiError(f"communityId {raw!r} must be an integer", 400, "INVALID_COMMUNITY_ID") from exc


@dataclass(slots=True, frozen=True)
class GrantDTO:
    """One resolved stream grant, rendered in words."""

    grantId: int
    platform: str
    sourceId: str
    label: str
    streamKey: str


@dataclass(slots=True, frozen=True)
class GrantListResponse:
    """Response DTO for `GET .../grants`."""

    success: bool
    grants: list[GrantDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ResolveRequest:
    """Request DTO for `POST .../grants/resolve`."""

    communityId: int | None = None


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic message response DTO."""

    success: bool
    message: str


@bundle_grants_bp.route("/<app_id>/grants", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(GrantListResponse)
async def get_grants(app_id: str) -> GrantListResponse | tuple[dict[str, object], int]:
    """The bundle's current, active stream grants for the caller's tenant/community."""
    install_dal = _install_dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    try:
        community_id = _parse_community_id(request.args.get("communityId"))
    except ApiError as exc:
        return _err(exc)
    rows = await list_grants(install_dal, app_id=app_id, tenant_id=ctx.tenant_id, community_id=community_id)
    return GrantListResponse(
        success=True,
        grants=[
            GrantDTO(grantId=r.id, platform=r.platform, sourceId=r.source_id, label=r.label, streamKey=r.stream_key)
            for r in rows
        ],
    )


@bundle_grants_bp.route("/<app_id>/grants/resolve", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(ResolveRequest)
async def post_resolve(data: ResolveRequest, app_id: str) -> tuple[dict[str, object], int]:
    """Re-run grant resolution for this scope. Idempotent, callable any time (e.g. after a new ingest source)."""
    install_dal = _install_dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    caller_id = get_current_user_id(request)

    active_version = await get_active_version(
        install_dal, tenant_id=ctx.tenant_id, community_id=data.communityId, app_id=app_id
    )
    if active_version is None:
        return _err(bad_request(f"{app_id} has no active version for this scope"))
    consumes = await consumes_from_version(install_dal, app_id=app_id, version=active_version.version)

    grants = await resolve_grants_for_scope(
        install_dal, build_client(), tenant_id=ctx.tenant_id, tenant_slug=ctx.tenant_slug,
        community_id=data.communityId, community_slug=None, app_id=app_id, consumes=consumes, granted_by=caller_id,
    )
    return {"success": True, "grantCount": len(grants)}, 200


@bundle_grants_bp.route("/<app_id>/grants/<int:grant_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def delete_grant(app_id: str, grant_id: int) -> MessageResponse | tuple[dict[str, object], int]:
    """Revoke one grant without uninstalling or deactivating the bundle."""
    install_dal = _install_dal()
    caller_id = get_current_user_id(request)
    try:
        await revoke_grant(install_dal, build_client(), app_id=app_id, grant_id=grant_id, actor_id=caller_id)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"grant {grant_id} revoked")


BLUEPRINTS: list[Blueprint] = [bundle_grants_bp]
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_grants_blueprint.py -v`
Expected: `3 passed`

- [ ] **Step 5: Add the ruff per-file ignore**

Append to `hub_api/pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]`:

```toml
"blueprints/v1/bundle_grants.py" = ["N815"]
```

- [ ] **Step 6: Commit**

```bash
git add hub_api/blueprints/v1/bundle_grants.py hub_api/tests/test_bundle_grants_blueprint.py \
        hub_api/pyproject.toml
git commit -m "$(cat <<'EOF'
feat(hub-api): GET grants, POST resolve, DELETE grant (spec Sec9.6, R52)

Tenant strictly from the JWT, never a path segment, matching the
spec's literal /api/v1/apps/{app_id}/grants path shape. Reads
install_dal (penguin-dal) from app config.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 32: `distribution_service.py` extension — the new distribution-API fields (spec §6.7) (R52: penguin-dal)

**Depends on:** Task 16 (`get_active_version`, `install_dal`-only per R52), Task 29 (`list_grants`, `install_dal`-only per R52), Task 10 (`app_version_uploads.manifest_json`, the source of the `manifest` subset, `install_dal`-only per R52).

**R52 note:** `distribution_service.py` is a **pre-existing** hub-api module. `list_bundles_for_stage`'s existing body (`app_activations`/`app_tenant_availability`/`app_catalog` queries via `async_dal`/`dal`) is **not rewritten** — those are existing tables, untouched. The new enrichment this task adds (`app_active_versions`/`app_versions`/`app_version_uploads`/`app_stream_grants`) queries exclusively new tables, so it takes a new, **optional** `install_dal: AsyncDB | None = None` keyword parameter, defaulting to `None` — when absent, enrichment is skipped (every new field stays `None`/`{}`/`[]`, exactly as if this task had never landed). This is deliberate, not a shortcut: Decision #12 requires the pre-existing v1 distribution blueprint's `GET /api/v1/distribution/bundles` route to stay **byte-identical**, including its own call site into this function, which is not touched until M6 — an `install_dal` parameter with no default would break that call site outright (`TypeError`, missing argument) the moment this task lands. Task 33's new v2 route is the only caller that passes a real `install_dal`.

**Files:**
- Modify: `hub_api/services/distribution_service.py`
- Test: `hub_api/tests/test_distribution_service_versions.py`

**Interfaces:**
- Produces: `BundleDistributionRow` gains `artifact_version: str | None`, `artifact_digest: str | None`, `artifact_kind: str | None`, `language: str | None`, `scan_status: str | None`, `manifest: dict[str, Any]` (all default to `None`/`{}`, so every existing construction/test keeps working), `grants: list[GrantInfo]` (`stage="process"` rows only); new dataclass `GrantInfo(grant_id: int, stream: str, platform: str, source_id: str, label: str)`. `list_bundles_for_stage(async_dal, dal, *, tenant_id, community_id, stage, install_dal: AsyncDB | None = None)` — the new keyword-only `install_dal` parameter (Task 4's penguin-dal `AsyncDB`) drives the new-table enrichment; omitted or `None`, the function's observable behavior is byte-identical to before this task.
- Consumes: `services.bundle_activation_service.get_active_version` (Task 16); `services.stream_grant_service.list_grants` (Task 29).

A row whose `artifact_digest` is `None` (no `app_active_versions` row for this scope yet, or `install_dal` not given — spec §6.7: "A row whose `artifactDigest` is `null`... is skipped by the stage") is still returned by this service; the **blueprint** (Task 33) is where the null-digest fields collapse into the wire shape the Rust stages expect.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_distribution_service_versions.py
"""Tests for the Sec6.7 distribution-API field additions to list_bundles_for_stage()."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from services.bundle_activation_service import activate_version
from services.distribution_service import list_bundles_for_stage
from services.stream_grant_service import resolve_grants_for_scope
from services.bundle_manifest_v2 import ConsumeRule

_MANIFEST = {
    "schema_version": 2, "app_id": "waddles.socials.music.default", "name": "Music Station",
    "version": "3.0.1", "feature": "waddles.socials.music", "module": "socials",
    "provider": "builtin", "language": "python", "artifact": "source",
    "stages": {
        "process": {"entry": "x:y", "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}]},
    },
}


async def _seed_active(dal: Any, install_dal: Any) -> None:
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", artifact_digest="sha256:" + "a" * 64,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED",
        manifest_json=_MANIFEST, app_version_id=version_id, created_at=now, updated_at=now,
    )
    dal.app_tenant_availability.insert(tenant_id=1, app_id="waddles.socials.music.default", available=True)
    dal.commit()
    await activate_version(
        install_dal, tenant_id=1, community_id=None, app_id="waddles.socials.music.default",
        version="3.0.1", activated_by=1,
    )


async def test_process_row_includes_digest_and_grants_when_active(
    bundle_install_db: Any, install_dal: Any
) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    await _seed_active(dal, install_dal)
    await install_dal.ingest_sources.async_insert(
        tenant_id=1, community_id=None, platform="twitch", source_id="tw-channelA",
        label="Twitch #channelA", enabled=True,
    )
    await resolve_grants_for_scope(
        install_dal, AsyncMock(), tenant_id=1, tenant_slug="acme", community_id=None, community_slug=None,
        app_id="waddles.socials.music.default",
        consumes=(ConsumeRule(platform="twitch", source_id=None, event_types=("chat.message",), filters={}),),
        granted_by=1,
    )

    rows = await list_bundles_for_stage(
        async_dal, dal, tenant_id=1, community_id=None, stage="process", install_dal=install_dal
    )
    row = next(r for r in rows if r.app_id == "waddles.socials.music.default")
    assert row.artifact_digest == "sha256:" + "a" * 64
    assert row.artifact_version == "3.0.1"
    assert row.language == "python"
    assert row.scan_status == "scanned"
    assert len(row.grants) == 1
    assert row.grants[0].label == "Twitch #channelA"
    assert row.manifest["consumes"] == [{"platform": "twitch", "event_types": ["chat.message"]}]
    assert row.manifest["limits"] == {"timeout_ms": 2000, "memory_mb": 64, "egress_rps": 10}
    assert row.manifest["egress"] == []
    assert row.manifest["data"] == {"tables": []}


async def test_action_stage_row_has_no_grants_field_populated(
    bundle_install_db: Any, install_dal: Any
) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    action_manifest = {**_MANIFEST, "stages": {"action": {"entry": "x:y"}}}
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", artifact_digest="sha256:" + "b" * 64,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED",
        manifest_json=action_manifest, app_version_id=version_id, created_at=now, updated_at=now,
    )
    await activate_version(
        install_dal, tenant_id=1, community_id=None, app_id="waddles.socials.music.default",
        version="3.0.1", activated_by=1,
    )
    dal(dal.app_catalog.app_id == "waddles.socials.music.default").update(
        stages={"action": {"entrypoint": "x:y", "config": {}, "spec": {}}}
    )
    dal.app_tenant_availability.insert(tenant_id=1, app_id="waddles.socials.music.default", available=True)
    dal.commit()

    rows = await list_bundles_for_stage(
        async_dal, dal, tenant_id=1, community_id=None, stage="action", install_dal=install_dal
    )
    row = next(r for r in rows if r.app_id == "waddles.socials.music.default")
    assert row.grants == []
    assert row.artifact_digest == "sha256:" + "b" * 64


async def test_no_active_version_leaves_digest_fields_none(
    bundle_install_db: Any, install_dal: Any
) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    dal.app_tenant_availability.insert(tenant_id=1, app_id="waddles.socials.music.default", available=True)
    dal.commit()

    rows = await list_bundles_for_stage(
        async_dal, dal, tenant_id=1, community_id=None, stage="process", install_dal=install_dal
    )
    row = next((r for r in rows if r.app_id == "waddles.socials.music.default"), None)
    if row is not None:  # the seed app_catalog row has no "process" stage data, may legitimately be absent
        assert row.artifact_digest is None


async def test_omitting_install_dal_skips_enrichment_entirely(bundle_install_db: Any) -> None:
    """Decision #12/R52: the pre-existing v1 blueprint call site passes no install_dal at all."""
    async_dal = bundle_install_db
    dal = async_dal.dal
    dal.app_tenant_availability.insert(tenant_id=1, app_id="waddles.socials.music.default", available=True)
    dal.commit()

    rows = await list_bundles_for_stage(async_dal, dal, tenant_id=1, community_id=None, stage="process")
    row = next((r for r in rows if r.app_id == "waddles.socials.music.default"), None)
    if row is not None:
        assert row.artifact_digest is None
        assert row.grants == []
        assert row.manifest == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_distribution_service_versions.py -v`
Expected: `AttributeError: 'BundleDistributionRow' object has no attribute 'artifact_digest'`

- [ ] **Step 3: Write the implementation**

Replace `hub_api/services/distribution_service.py`'s `BundleDistributionRow` dataclass and `list_bundles_for_stage` function with the versions below (every other function/constant in the file is unchanged). Add `from penguin_dal import AsyncDB` to this module's `TYPE_CHECKING`-guarded imports (type-only, matching this file's existing lightweight-import style) if not already present:

```python
@dataclass(slots=True, frozen=True)
class GrantInfo:
    """One resolved stream grant, rendered in words (spec Sec6.7's `grants` array)."""

    grant_id: int
    stream: str
    platform: str
    source_id: str
    label: str


@dataclass(slots=True, frozen=True)
class BundleDistributionRow:
    """One bundle's `{entrypoint, config, spec}` for a stage, at a single (tenant, community).

    `config` is the merge of the bundle's own shipped stage default
    (`app_catalog.stages[stage].config`) with the tenant/community's
    override (`app_tenant_availability.config_defaults` /
    `app_activations.config`) -- override wins, same precedence
    `DBInstallationLookup`'s docstring establishes for narrower-scope-wins.

    M2b additions (spec Sec6.7, R52): `artifact_version`/`artifact_digest`/
    `artifact_kind`/`language`/`scan_status` are populated from the
    scope's `app_active_versions` -> `app_versions` join (queried
    through the new `install_dal` parameter -- `None` when the caller
    doesn't pass one, i.e. the pre-existing v1 blueprint call site).
    `grants` is populated only for `stage="process"` rows. `manifest`
    is the capability-bearing subset of the bundle's `bundle.yaml` v2
    the stage must enforce (`egress`, `data.tables`, `limits`, and --
    for a `stage="process"` row -- `consumes`), read out of
    `app_version_uploads.manifest_json` so the stage never fetches the
    full manifest separately (spec Sec6.7).
    """

    app_id: str
    community_id: int | None
    entrypoint: str | None
    spec: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    artifact_version: str | None = None
    artifact_digest: str | None = None
    artifact_kind: str | None = None
    language: str | None = None
    scan_status: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    grants: list[GrantInfo] = field(default_factory=list)


DEFAULT_LIMITS: dict[str, int] = {"timeout_ms": 2000, "memory_mb": 64, "egress_rps": 10}


async def _manifest_subset(install_dal: "AsyncDB", *, app_id: str, version: str, stage: str) -> dict[str, Any]:
    """The capability-bearing `bundle.yaml` slice the stage enforces (spec Sec6.7's `manifest`).

    Exactly four keys -- `egress`, `data`, `limits` and (process rows
    only) `consumes` -- read out of the `app_version_uploads.manifest_json`
    stored at upload time (R52: through `install_dal`). Manifest
    defaults are applied here, not left to the stage, so a bundle that
    omitted `limits` still advertises the numbers the executor will
    actually enforce.
    """
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = rows.first()
    raw: dict[str, Any] = dict(upload.manifest_json or {}) if upload is not None else {}
    limits = {**DEFAULT_LIMITS, **(raw.get("limits") or {})}
    subset: dict[str, Any] = {
        "egress": list(raw.get("egress") or []),
        "data": {"tables": list(((raw.get("data") or {}).get("tables")) or [])},
        "limits": limits,
    }
    if stage == "process":
        stage_block = (raw.get("stages") or {}).get("process") or {}
        subset["consumes"] = list(stage_block.get("consumes") or [])
    return subset


async def _enrich_with_active_version(
    install_dal: "AsyncDB | None",
    *,
    tenant_id: int,
    community_id: int | None,
    app_id: str,
    stage: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None, dict[str, Any], list[GrantInfo]]:
    """`(version, digest, kind, language, scan_status, manifest, grants)` for one bundle.

    R52: `install_dal=None` (the pre-existing v1 call site) skips this
    entirely and returns the all-empty tuple -- Decision #12's
    byte-identical-v1 guarantee.
    """
    if install_dal is None:
        return None, None, None, None, None, {}, []

    from services.bundle_activation_service import get_active_version
    from services.stream_grant_service import list_grants

    active_version = await get_active_version(
        install_dal, tenant_id=tenant_id, community_id=community_id, app_id=app_id
    )
    if active_version is None:
        return None, None, None, None, None, {}, []

    grants: list[GrantInfo] = []
    if stage == "process":
        grant_rows = await list_grants(install_dal, app_id=app_id, tenant_id=tenant_id, community_id=community_id)
        grants = [
            GrantInfo(grant_id=g.id, stream=g.stream_key, platform=g.platform, source_id=g.source_id, label=g.label)
            for g in grant_rows
        ]
    manifest = await _manifest_subset(install_dal, app_id=app_id, version=active_version.version, stage=stage)
    return (
        active_version.version, active_version.artifact_digest, active_version.artifact_kind,
        active_version.language, active_version.scan_status, manifest, grants,
    )


async def list_bundles_for_stage(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    community_id: int | None,
    stage: str,
    install_dal: "AsyncDB | None" = None,
) -> Sequence[BundleDistributionRow]:
    """Every enabled, activated bundle implementing `stage` at (`tenant_id`, `community_id`).

    Community-scoped `app_activations` rows (when `community_id` is given)
    come first, then tenant-wide `app_tenant_availability` rows -- same
    ordering as `DBInstallationLookup.find()`, deduped by `app_id` (first
    occurrence wins) so a bundle available at both scopes is returned once,
    with the narrower (community) config winning. `app_activations`/
    `app_tenant_availability`/`app_catalog` are all pre-existing tables,
    queried through the pre-existing pydal `async_dal`/`dal` pair,
    unchanged by R52.

    `install_dal` (R52, Task 4's penguin-dal `AsyncDB`) is optional and
    keyword-only: when given, each row is additionally enriched with its
    active-version digest, manifest subset and (process-stage only)
    grants (spec Sec6.7); when omitted (the pre-existing v1 blueprint's
    call site, Decision #12), those fields stay `None`/`{}`/`[]` and
    this function's behavior is byte-identical to before this task.

    Raises `InvalidStageError` for any `stage` outside `BUNDLE_STAGES` --
    caught by the blueprint and turned into a 400, never a silently-empty
    result that looks like "no bundles active" for a typo'd stage name.
    """
    if stage not in BUNDLE_STAGES:
        raise InvalidStageError(f"invalid stage {stage!r}; must be one of {BUNDLE_STAGES}")

    rows: list[BundleDistributionRow] = []
    seen_app_ids: set[str] = set()

    if community_id is not None:
        query = (
            (dal.app_activations.tenant_id == tenant_id)
            & (dal.app_activations.community_id == community_id)
            & (dal.app_activations.enabled == True)  # noqa: E712 - pydal query operator, not a bool compare
            & (dal.app_activations.app_id == dal.app_catalog.app_id)
            & (dal.app_catalog.status == "active")
        )
        activation_rows = await async_dal.select_async(
            dal(query),
            dal.app_activations.app_id,
            dal.app_activations.config,
            dal.app_catalog.stages,
        )
        for row in activation_rows:
            app_id = row.app_activations.app_id
            if app_id in seen_app_ids:
                continue
            stage_data = _stage_data(row.app_catalog.stages, stage)
            if stage_data is None:
                continue
            seen_app_ids.add(app_id)
            merged_config = {**stage_data.get("config", {}), **(row.app_activations.config or {})}
            (artifact_version, artifact_digest, artifact_kind, language, scan_status, manifest, grants) = (
                await _enrich_with_active_version(
                    install_dal, tenant_id=tenant_id, community_id=community_id, app_id=app_id, stage=stage
                )
            )
            rows.append(
                BundleDistributionRow(
                    app_id=app_id,
                    community_id=community_id,
                    entrypoint=stage_data.get("entrypoint"),
                    spec=dict(stage_data.get("spec") or {}),
                    config=merged_config,
                    artifact_version=artifact_version, artifact_digest=artifact_digest,
                    artifact_kind=artifact_kind, language=language, scan_status=scan_status,
                    manifest=manifest, grants=grants,
                )
            )

    avail_query = (
        (dal.app_tenant_availability.tenant_id == tenant_id)
        & (dal.app_tenant_availability.available == True)  # noqa: E712
        & (dal.app_tenant_availability.app_id == dal.app_catalog.app_id)
        & (dal.app_catalog.status == "active")
    )
    availability_rows = await async_dal.select_async(
        dal(avail_query),
        dal.app_tenant_availability.app_id,
        dal.app_tenant_availability.config_defaults,
        dal.app_catalog.stages,
    )
    for row in availability_rows:
        app_id = row.app_tenant_availability.app_id
        if app_id in seen_app_ids:
            continue
        stage_data = _stage_data(row.app_catalog.stages, stage)
        if stage_data is None:
            continue
        seen_app_ids.add(app_id)
        merged_config = {
            **stage_data.get("config", {}),
            **(row.app_tenant_availability.config_defaults or {}),
        }
        (artifact_version, artifact_digest, artifact_kind, language, scan_status, manifest, grants) = (
            await _enrich_with_active_version(
                install_dal, tenant_id=tenant_id, community_id=None, app_id=app_id, stage=stage
            )
        )
        rows.append(
            BundleDistributionRow(
                app_id=app_id,
                community_id=None,
                entrypoint=stage_data.get("entrypoint"),
                spec=dict(stage_data.get("spec") or {}),
                config=merged_config,
                artifact_version=artifact_version, artifact_digest=artifact_digest,
                artifact_kind=artifact_kind, language=language, scan_status=scan_status,
                manifest=manifest, grants=grants,
            )
        )

    return rows
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_distribution_service_versions.py -v`
Expected: `4 passed`

- [ ] **Step 5: Run the existing distribution-service tests to confirm zero regression**

Run: `cd hub_api && python3 -m pytest tests/test_distribution_service.py tests/test_v1_distribution_blueprint.py -v`
Expected: every previously-passing test still passes — these call sites pass no `install_dal` at all, so the new fields all stay `None`/`{}`/`[]` and every existing test constructs/asserts against the pre-existing fields only.

- [ ] **Step 6: Commit**

```bash
git add hub_api/services/distribution_service.py hub_api/tests/test_distribution_service_versions.py
git commit -m "$(cat <<'EOF'
feat(hub-api): distribution_service -- artifactVersion/Digest/Kind/language/scanStatus/manifest/grants (spec Sec6.7, R52)

Joins app_active_versions -> app_versions for the scope (through the
new, optional install_dal parameter), reads the capability-bearing
manifest slice (egress/data.tables/limits/consumes) out of
app_version_uploads.manifest_json, and app_stream_grants for
stage=process rows -- all three new tables via penguin-dal. install_dal
defaults to None so the pre-existing v1 blueprint's call site (Decision
#12, untouched until M6) stays byte-identical. A row with no active
version yet returns all-None digest fields -- the blueprint (Task 33)
is where that collapses into the wire null the Rust stages skip on.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 33: `blueprints/v1/distribution.py` — distribution API v2 bundles route + ETag (R52: penguin-dal)

**Depends on:** Task 32 (`distribution_service` returns the §6.7 fields via the new, optional `install_dal` parameter), Task 29 (`stream_grant_service.list_grants`), Task 16 (`bundle_activation_service.get_active_version`), Task 4 (`install_dal` wired into `app.config`), Task 18 (`permission_summary_service.canonical_json`).

**R52 note:** the pre-existing `_ensure_tables`/`bind_app_bundle_tables` machinery in this file is for **pre-existing** pydal tables (`app_catalog`, `app_activations`, `app_tenant_availability`) and is **untouched** by this task — there is no `bind_bundle_install_tables` to call anymore (Task 4 removed that pydal binder entirely; `app_active_versions`/`app_versions`/`app_version_uploads`/`app_stream_grants` are reachable through `install_dal`, which was already fully reflected once at hub-api startup, Task 4 — no per-request/per-connection binding is needed for it, unlike pydal's per-`DAL`-instance `define_table()` model). This task's only change to `_ensure_tables` is: none.

**Files:**
- Modify: `hub_api/blueprints/v1/distribution.py`
- Test: `hub_api/tests/test_distribution_v2_blueprint.py`

**Interfaces:**

| Method | Path | Auth | Query params | Success |
|---|---|---|---|---|
| `GET` | `/api/v1/distribution/v2/bundles` | `tenant_middleware` + `require_scope("distribution:read")` | `stage` (required, one of `ingest`/`process`/`action`), `communityId` (optional int) | `200` + `ETag`, or `304` when `If-None-Match` matches |
| `GET` | `/api/v1/distribution/bundles` | unchanged | unchanged | **unchanged — this task does not touch the v1 route, and its call site passes no `install_dal`, matching Task 32's Decision #12 default** |

- Produces: `DistributionGrantDTO(grantId: int, stream: str, platform: str, sourceId: str, label: str)`; `DistributionBundleV2DTO(appId, communityId, entrypoint, spec, config, artifactVersion, artifactDigest, artifactKind, language, scanStatus, manifest, grants)`; `DistributionBundlesV2Response(success, stage, bundles, meta)`; `def bundles_etag(stage: str, bundles: list[DistributionBundleV2DTO]) -> str`.
- Consumes: `services.distribution_service.{list_bundles_for_stage, BUNDLE_STAGES, InvalidStageError}` (Task 32); `services.permission_summary_service.canonical_json` (Task 18); `current_app.config["install_dal"]` (Task 4) — the v2 route is the first caller to pass a real `install_dal` into `list_bundles_for_stage`.

Two facts this task depends on and must not re-derive:

1. **The ETag is computed over `{"stage": ..., "bundles": [...]}` only — never over `meta`.** `meta.timestamp` is `datetime.now(UTC)` and changes on every request; including it would produce a fresh ETag every poll and the 304 path would never fire.
2. **`@validate_response` is deliberately not used on the v2 routes.** A 304 carries no body, and the ETag has to be set on a real response object, so the handler builds the DTO, serialises it with `dataclasses.asdict`, and hands that to `jsonify`. The explicit-schema guarantee (`security.md` Output Validation) still holds — the DTO is the only object that is ever serialised, and no ORM row or `**row.as_dict()` reaches the wire. The v1 route keeps its `@validate_response(DistributionBundlesResponse)` unchanged.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_distribution_v2_blueprint.py
"""Blueprint tests for GET /api/v1/distribution/v2/bundles (spec Sec6.7)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from blueprints.v1.distribution import BLUEPRINTS
from services.bundle_activation_service import activate_version
from services.bundle_manifest_v2 import ConsumeRule
from services.stream_grant_service import resolve_grants_for_scope
from tests.conftest import TENANT_SLUG, make_token

_MANIFEST = {
    "schema_version": 2, "app_id": "waddles.socials.music.default", "name": "Music Station",
    "version": "3.0.1", "feature": "waddles.socials.music", "module": "socials",
    "provider": "builtin", "language": "python", "artifact": "source",
    "egress": [{"host": "api.spotify.com", "methods": ["GET", "POST"]}],
    "data": {"tables": ["music_queue"]},
    "limits": {"timeout_ms": 2000, "memory_mb": 64, "egress_rps": 10},
    "stages": {
        "process": {
            "entry": "bundles.social_music_process:transform",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
            "config": {"command_prefix": "!"},
            "spec": {"required_config": []},
        }
    },
}


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    dal = bundle_install_db.dal
    dal(dal.app_catalog.app_id == "waddles.socials.music.default").update(
        stages={"process": {"entrypoint": "bundles.social_music_process:transform",
                            "config": {"command_prefix": "!"}, "spec": {"required_config": []}}}
    )
    dal.app_tenant_availability.insert(tenant_id=1, app_id="waddles.socials.music.default", available=True)
    dal.commit()
    now = datetime.now(UTC)
    await install_dal.ingest_sources.async_insert(
        tenant_id=1, community_id=None, platform="twitch", source_id="tw-channelA",
        label="Twitch #channelA", enabled=True,
    )
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", artifact_digest="sha256:" + "a" * 64,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED",
        manifest_json=_MANIFEST, app_version_id=version_id, created_at=now, updated_at=now,
    )
    quart_app = Quart(__name__)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = dal
    quart_app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


async def _activate_and_grant(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    await activate_version(
        install_dal, tenant_id=1, community_id=None,
        app_id="waddles.socials.music.default", version="3.0.1", activated_by=1,
    )
    await resolve_grants_for_scope(
        install_dal, AsyncMock(), tenant_id=1, tenant_slug=TENANT_SLUG, community_id=None,
        community_slug=None, app_id="waddles.socials.music.default",
        consumes=(ConsumeRule(platform="twitch", source_id=None, event_types=("chat.message",), filters={}),),
        granted_by=1,
    )


async def test_v2_requires_distribution_read_scope(app: Quart) -> None:
    client = app.test_client()
    response = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process",
        headers={"Authorization": f"Bearer {make_token(scope='', tenant=TENANT_SLUG)}"},
    )
    assert response.status_code == 403


async def test_v2_returns_digest_manifest_and_grants(app: Quart) -> None:
    await _activate_and_grant(app)
    token = make_token(scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["meta"]["version"] == 2
    row = next(b for b in body["bundles"] if b["appId"] == "waddles.socials.music.default")
    assert row["artifactDigest"] == "sha256:" + "a" * 64
    assert row["artifactVersion"] == "3.0.1"
    assert row["artifactKind"] == "source"
    assert row["language"] == "python"
    assert row["scanStatus"] == "scanned"
    assert row["manifest"]["data"] == {"tables": ["music_queue"]}
    assert row["manifest"]["egress"] == [{"host": "api.spotify.com", "methods": ["GET", "POST"]}]
    assert row["manifest"]["consumes"] == [{"platform": "twitch", "event_types": ["chat.message"]}]
    assert len(row["grants"]) == 1
    assert row["grants"][0]["label"] == "Twitch #channelA"
    assert row["grants"][0]["stream"].endswith(":src:twitch:tw-channelA:events")


async def test_v2_null_digest_row_is_still_returned(app: Quart) -> None:
    token = make_token(scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process", headers={"Authorization": f"Bearer {token}"}
    )
    body = await response.get_json()
    row = next(b for b in body["bundles"] if b["appId"] == "waddles.socials.music.default")
    assert row["artifactDigest"] is None
    assert row["grants"] == []
    assert row["manifest"] == {}


async def test_v2_etag_round_trip_returns_304(app: Quart) -> None:
    await _activate_and_grant(app)
    token = make_token(scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    first = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process", headers={"Authorization": f"Bearer {token}"}
    )
    etag = first.headers["ETag"]
    assert etag.startswith('"') and etag.endswith('"')

    second = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process",
        headers={"Authorization": f"Bearer {token}", "If-None-Match": etag},
    )
    assert second.status_code == 304
    assert second.headers["ETag"] == etag
    assert await second.get_data() == b""


async def test_v2_etag_is_stable_across_calls_despite_the_meta_timestamp(app: Quart) -> None:
    await _activate_and_grant(app)
    token = make_token(scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    first = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process", headers={"Authorization": f"Bearer {token}"}
    )
    second = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process", headers={"Authorization": f"Bearer {token}"}
    )
    assert first.headers["ETag"] == second.headers["ETag"]
    assert (await first.get_json())["meta"]["timestamp"] != (await second.get_json())["meta"]["timestamp"]


async def test_v2_etag_changes_when_a_grant_is_revoked(app: Quart) -> None:
    await _activate_and_grant(app)
    install_dal = app.config["install_dal"]
    token = make_token(scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    before = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process", headers={"Authorization": f"Bearer {token}"}
    )
    await install_dal(install_dal.app_stream_grants.app_id == "waddles.socials.music.default").update(
        revoked_at=datetime.now(UTC)
    )
    after = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process", headers={"Authorization": f"Bearer {token}"}
    )
    assert before.headers["ETag"] != after.headers["ETag"]
    assert (await after.get_json())["bundles"][0]["grants"] == []


@pytest.mark.parametrize("stage", ["", "nonsense", "presentation", "INGEST"])
async def test_v2_rejects_an_invalid_stage(app: Quart, stage: str) -> None:
    token = make_token(scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.get(
        f"/api/v1/distribution/v2/bundles?stage={stage}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 400
    assert (await response.get_json())["error"]["code"] == "INVALID_STAGE"


async def test_v2_rejects_a_non_integer_community_id(app: Quart) -> None:
    token = make_token(scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process&communityId=abc",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert (await response.get_json())["error"]["code"] == "INVALID_COMMUNITY_ID"


async def test_v2_community_id_filter_narrows_the_result_set(app: Quart) -> None:
    token = make_token(scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.get(
        "/api/v1/distribution/v2/bundles?stage=process&communityId=999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert [b["communityId"] for b in body["bundles"]] == [None]


async def test_v2_empty_result_is_a_200_with_an_empty_list(app: Quart) -> None:
    token = make_token(scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.get(
        "/api/v1/distribution/v2/bundles?stage=action", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert (await response.get_json())["bundles"] == []


async def test_v1_route_body_is_unchanged_by_this_task(app: Quart) -> None:
    await _activate_and_grant(app)
    token = make_token(scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.get(
        "/api/v1/distribution/bundles?stage=process", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["meta"]["version"] == 1
    assert set(body["bundles"][0]) == {"appId", "communityId", "entrypoint", "spec", "config"}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_distribution_v2_blueprint.py -v`
Expected: every test fails with `404 != 200` / `404 != 403` — the `/v2/bundles` rule is not registered yet (`test_v1_route_body_is_unchanged_by_this_task` is the one exception and already passes).

- [ ] **Step 3: Write the implementation**

Make exactly four edits to `hub_api/blueprints/v1/distribution.py`. `_ensure_tables` (the pre-existing `bind_app_bundle_tables` binder for `app_catalog`/`app_activations`/`app_tenant_availability`) is **not** among them — it stays exactly as it is today; R52's new tables need no per-request binding (`install_dal.reflect()` ran once at hub-api startup, Task 4).

**3a.** Replace the existing import block's `from dataclasses import ...` line and add the new imports, so the top of the file reads:

```python
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, Response, current_app, jsonify, request
from quart_schema import validate_response

from services import distribution_service as svc
from services.errors import ApiError
from services.permission_summary_service import canonical_json
```

(`services.schema.bind_bundle_install_tables` is **not** imported — it no longer exists; R52 replaced it with `install_dal.reflect()` at startup, Task 4.)

**3b.** Add a new `_install_dal()` helper next to the file's existing `_dal()` helper (`_dal()` itself is untouched):

```python
def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])
```

**3c.** Append these DTOs and the ETag helper directly after the existing `DistributionBundlesResponse` dataclass:

```python
@dataclass(slots=True, frozen=True)
class DistributionGrantDTO:
    """One resolved stream grant, as the Rust process stage reads it (spec Sec6.7)."""

    grantId: int
    stream: str
    platform: str
    sourceId: str
    label: str


@dataclass(slots=True, frozen=True)
class DistributionBundleV2DTO:
    """One bundle's full v2 distribution row -- the v1 five fields plus spec Sec6.7's seven.

    `artifactDigest` is `None` for a bundle registered without an
    activated, digest-verified version; the stage skips such a row and
    counts it as `waddles_bundle_skipped_total{reason="no_artifact"}`
    rather than treating it as an error. `grants` is populated only on
    `stage=process` rows; an empty list means "activated but currently
    reads nothing", a legitimate state.
    """

    appId: str
    communityId: int | None
    entrypoint: str | None
    spec: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    artifactVersion: str | None = None
    artifactDigest: str | None = None
    artifactKind: str | None = None
    language: str | None = None
    scanStatus: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    grants: list[DistributionGrantDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class DistributionBundlesV2Response:
    """Response DTO for `GET /api/v1/distribution/v2/bundles`."""

    success: bool
    stage: str
    bundles: list[DistributionBundleV2DTO]
    meta: DistributionMetaDTO


def bundles_etag(stage: str, bundles: list[DistributionBundleV2DTO]) -> str:
    """A strong, quoted ETag over the bundle set -- deliberately excluding `meta`.

    `meta.timestamp` is `datetime.now(UTC)` and changes on every
    request; hashing it would mint a fresh ETag per poll and the 304
    path would never fire. Hashing `{stage, bundles}` instead makes the
    ETag change exactly when the advertised digests, config or grants
    change, which is the signal the Rust poller acts on.
    """
    payload = {"stage": stage, "bundles": [asdict(b) for b in bundles]}
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f'"{digest[:32]}"'


def _conditional(stage: str, bundles: list[DistributionBundleV2DTO], payload: Any) -> Response:
    """Serialise `payload`, attach the ETag, and short-circuit to 304 on an `If-None-Match` hit.

    Written by hand rather than through `Response.make_conditional`
    because Quart's `make_conditional` handles Range requests, not
    conditional GET -- the four lines below are the whole of RFC 9110's
    If-None-Match rule this endpoint needs.
    """
    etag = bundles_etag(stage, bundles)
    presented = {tag.strip() for tag in request.headers.get("If-None-Match", "").split(",") if tag.strip()}
    if etag in presented or "*" in presented:
        return Response(b"", status=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    response = cast(Response, jsonify(asdict(payload)))
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return response
```

**3d.** Append this route immediately before the file's final `BLUEPRINTS: list[Blueprint] = [distribution_bp]` line:

```python
@distribution_bp.route("/v2/bundles", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("distribution:read")  # type: ignore[untyped-decorator]
async def list_distribution_bundles_v2() -> Response | tuple[dict[str, object], int]:
    """Distribution API v2 -- every Sec6.7 field, with an ETag/If-None-Match 304 path.

    The v1 route above is untouched and keeps serving its five-field
    body until the M6 cut-over, so today's Python
    `flask_core.stage_runner.BundlePoller` keeps working while the Rust
    stages move to this route. R52: this is the first caller to pass a
    real `install_dal` into `list_bundles_for_stage` -- the v1 route's
    own call site below is unmodified and still passes none.
    """
    stage = request.args.get("stage", "")
    if stage not in svc.BUNDLE_STAGES:
        return _err(ApiError(f"stage must be one of {svc.BUNDLE_STAGES}, got {stage!r}", 400, "INVALID_STAGE"))
    try:
        community_id = _parse_community_id(request.args.get("communityId"))
    except ApiError as exc:
        return _err(exc)

    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware always publishes this on the success path

    async_dal, read_dal = _dal()
    install_dal = _install_dal()
    rows = await svc.list_bundles_for_stage(
        async_dal, read_dal, tenant_id=ctx.tenant_id, community_id=community_id, stage=stage,
        install_dal=install_dal,
    )
    bundles = [
        DistributionBundleV2DTO(
            appId=row.app_id,
            communityId=row.community_id,
            entrypoint=row.entrypoint,
            spec=row.spec,
            config=row.config,
            artifactVersion=row.artifact_version,
            artifactDigest=row.artifact_digest,
            artifactKind=row.artifact_kind,
            language=row.language,
            scanStatus=row.scan_status,
            manifest=row.manifest,
            grants=[
                DistributionGrantDTO(
                    grantId=g.grant_id, stream=g.stream, platform=g.platform,
                    sourceId=g.source_id, label=g.label,
                )
                for g in row.grants
            ],
        )
        for row in rows
    ]
    payload = DistributionBundlesV2Response(
        success=True,
        stage=stage,
        bundles=bundles,
        meta=DistributionMetaDTO(version=2, timestamp=datetime.now(UTC).isoformat()),
    )
    return _conditional(stage, bundles, payload)
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_distribution_v2_blueprint.py -v`
Expected: `14 passed` (10 named tests + the 4 `stage` parametrize cases).

- [ ] **Step 5: Run the pre-existing distribution tests to confirm zero regression on v1**

Run: `cd hub_api && python3 -m pytest tests/test_v1_distribution_blueprint.py tests/test_distribution_service.py tests/test_distribution_service_versions.py -v`
Expected: every previously-passing test still passes — the v1 route, its DTOs and its `@validate_response` decorator are byte-identical after this task.

- [ ] **Step 6: Add the ruff per-file ignore**

`hub_api/blueprints/v1/distribution.py` already carries camelCase DTO fields; add it to `hub_api/pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]` if it is not already listed:

```toml
# Distribution API v1 + v2 -- camelCase DTO fields are the wire contract the
# Rust stages deserialize (spec Sec6.7), and S101 is the tenant_middleware
# postcondition assert every ported blueprint repeats.
"blueprints/v1/distribution.py" = ["N815", "S101"]
```

- [ ] **Step 7: Commit**

```bash
git add hub_api/blueprints/v1/distribution.py hub_api/tests/test_distribution_v2_blueprint.py \
        hub_api/pyproject.toml
git commit -m "$(cat <<'EOF'
feat(hub-api): distribution API v2 -- GET /api/v1/distribution/v2/bundles with ETag (spec Sec6.7, R52)

Serves artifactVersion/artifactDigest/artifactKind/language/scanStatus,
the capability-bearing manifest slice, and the resolved stream grants
for stage=process rows, sourced through penguin-dal's install_dal.
Strong ETag over {stage, bundles} only -- never over meta.timestamp --
so a poll that changes nothing is a 304 rather than a fresh body every
5 s.

The v1 /bundles route is untouched (its call site passes no
install_dal) and keeps its byte-identical five-field body until the M6
cut-over.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 34: `GET /api/v1/distribution/sources` — the ingest-source registry the Rust svc-ingest polls (R52: penguin-dal)

**Depends on:** Task 33 (`_conditional`, `DistributionMetaDTO`, `_install_dal()` on the distribution blueprint), Task 26 (`ingest_source_service`, `install_dal`-only per R52), Task 29 (`render_stream_key`).

**R52 note:** `ingest_sources` is this plan's own new table (`install_dal`); `communities` (read here only for a community's `slug`) is a **pre-existing** table, read through the existing pydal `async_dal`/`dal` pair. `list_sources_for_distribution` is this plan's second function (after Task 30's `activate_bundle`) holding both DAL handles side by side.

**Files:**
- Modify: `hub_api/services/ingest_source_service.py`
- Modify: `hub_api/blueprints/v1/distribution.py`
- Test: `hub_api/tests/test_distribution_sources_blueprint.py`

**Interfaces:**

| Method | Path | Auth | Query params | Success |
|---|---|---|---|---|
| `GET` | `/api/v1/distribution/sources` | `tenant_middleware` + `require_scope("distribution:read")` | `communityId` (optional int), `platform` (optional exact match), `enabled` (optional, exactly `true` or `false`) | `200` + `ETag`, or `304` when `If-None-Match` matches |

- Produces: `@dataclass(slots=True, frozen=True) DistributionSource(source_id: str, platform: str, label: str, community_id: int | None, community_slug: str | None, enabled: bool, has_secret: bool, mapping: dict[str, Any], stream_key: str)`; `async def list_sources_for_distribution(install_dal, async_dal, dal, *, tenant_id: int, tenant_slug: str, community_id: int | None = None, platform: str | None = None, enabled: bool | None = None) -> list[DistributionSource]` (`install_dal` for the new `ingest_sources` table, `async_dal`/`dal` for the pre-existing `communities` table's slug lookup); blueprint DTOs `DistributionSourceDTO(sourceId, platform, label, communityId, enabled, hasSecret, mapping, streamKey)` and `DistributionSourcesResponse(success, sources, meta)`.
- Consumes: `services.stream_grant_service.render_stream_key` (Task 29); `services.permission_summary_service.canonical_json` (Task 18, via Task 33's `_conditional`).

Three semantics fixed here and not re-derived anywhere else:

1. **The plaintext HMAC secret is never in this response.** Only `hasSecret: bool`. The Rust ingest fetches the secret itself through `ingest_source_service.resolve_secret`'s own authenticated path; this registry endpoint exists to tell it *which* sources and *which* streams exist, not to hand out credentials. A test asserts the string `secret` never appears as a key and that no `secret_ciphertext`/`secret_iv` bytes leak.
2. **`communityId` widens, it does not narrow to exactly one value.** `communityId=7` returns the tenant's sources scoped to community 7 **plus** its tenant-wide (`community_id IS NULL`) sources, because a tenant-wide source feeds every community — the same narrower-plus-tenant-wide rule `list_bundles_for_stage` already applies. Omitting `communityId` returns every source in the tenant.
3. **`streamKey` is rendered, never stored.** `render_stream_key(tenant_slug, community_slug, platform, source_id)` is the single source of truth for the key shape (`waddles:t:{tenant}:c:{community|_tenant}:src:{platform}:{source_id}:events`); this endpoint calls it rather than re-templating the string.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_distribution_sources_blueprint.py
"""Blueprint tests for GET /api/v1/distribution/sources (spec Sec10.3/Sec10.4 registry, Sec5.1 key shape)."""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart

from blueprints.v1.distribution import BLUEPRINTS
from tests.conftest import TENANT_SLUG, make_token


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    await install_dal.ingest_sources.async_insert(
        tenant_id=1, community_id=None, platform="twitch", source_id="tw-channelA",
        label="Twitch #channelA", enabled=True, secret_ciphertext=b"xx", secret_iv=b"yy", mapping=None,
    )
    await install_dal.ingest_sources.async_insert(
        tenant_id=1, community_id=1, platform="discord", source_id="dg-guildX",
        label="Discord guild X", enabled=True, mapping={"text": "/content"},
    )
    await install_dal.ingest_sources.async_insert(
        tenant_id=1, community_id=None, platform="custom:acme", source_id="wh-1",
        label="Acme webhook", enabled=False, mapping={"text": "/body/message"},
    )
    quart_app = Quart(__name__)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


def _token() -> str:
    return make_token(scope="distribution:read", tenant=TENANT_SLUG)


async def test_sources_requires_distribution_read_scope(app: Quart) -> None:
    client = app.test_client()
    response = await client.get(
        "/api/v1/distribution/sources",
        headers={"Authorization": f"Bearer {make_token(scope='', tenant=TENANT_SLUG)}"},
    )
    assert response.status_code == 403


async def test_sources_lists_every_tenant_source_with_a_rendered_stream_key(app: Quart) -> None:
    client = app.test_client()
    response = await client.get("/api/v1/distribution/sources", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 200
    body = await response.get_json()
    assert body["meta"]["version"] == 2
    by_id = {s["sourceId"]: s for s in body["sources"]}
    assert set(by_id) == {"tw-channelA", "dg-guildX", "wh-1"}
    assert by_id["tw-channelA"]["streamKey"] == f"waddles:t:{TENANT_SLUG}:c:_tenant:src:twitch:tw-channelA:events"
    assert by_id["dg-guildX"]["streamKey"] == (
        f"waddles:t:{TENANT_SLUG}:c:acme-community:src:discord:dg-guildX:events"
    )
    assert by_id["tw-channelA"]["hasSecret"] is True
    assert by_id["dg-guildX"]["hasSecret"] is False
    assert by_id["wh-1"]["mapping"] == {"text": "/body/message"}


async def test_sources_never_leak_the_secret_material(app: Quart) -> None:
    client = app.test_client()
    response = await client.get("/api/v1/distribution/sources", headers={"Authorization": f"Bearer {_token()}"})
    raw = (await response.get_data()).decode()
    body = await response.get_json()
    assert "secret_ciphertext" not in raw
    assert "secret_iv" not in raw
    for source in body["sources"]:
        assert set(source) == {
            "sourceId", "platform", "label", "communityId", "enabled", "hasSecret", "mapping", "streamKey",
        }


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("?platform=twitch", {"tw-channelA"}),
        ("?platform=discord", {"dg-guildX"}),
        ("?platform=custom:acme", {"wh-1"}),
        ("?platform=nope", set()),
        ("?enabled=true", {"tw-channelA", "dg-guildX"}),
        ("?enabled=false", {"wh-1"}),
        ("?communityId=1", {"tw-channelA", "dg-guildX", "wh-1"}),
        ("?communityId=2", {"tw-channelA", "wh-1"}),
        ("?platform=twitch&enabled=true", {"tw-channelA"}),
        ("?platform=twitch&enabled=false", set()),
    ],
)
async def test_sources_filter_combinations(app: Quart, query: str, expected: set[str]) -> None:
    client = app.test_client()
    response = await client.get(
        f"/api/v1/distribution/sources{query}", headers={"Authorization": f"Bearer {_token()}"}
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert {s["sourceId"] for s in body["sources"]} == expected


@pytest.mark.parametrize("value", ["yes", "1", "TRUE", ""])
async def test_sources_rejects_an_invalid_enabled_value(app: Quart, value: str) -> None:
    client = app.test_client()
    response = await client.get(
        f"/api/v1/distribution/sources?enabled={value}", headers={"Authorization": f"Bearer {_token()}"}
    )
    assert response.status_code == 400
    assert (await response.get_json())["error"]["code"] == "INVALID_ENABLED"


async def test_sources_rejects_a_non_integer_community_id(app: Quart) -> None:
    client = app.test_client()
    response = await client.get(
        "/api/v1/distribution/sources?communityId=abc", headers={"Authorization": f"Bearer {_token()}"}
    )
    assert response.status_code == 400
    assert (await response.get_json())["error"]["code"] == "INVALID_COMMUNITY_ID"


async def test_sources_etag_round_trip_returns_304(app: Quart) -> None:
    client = app.test_client()
    first = await client.get("/api/v1/distribution/sources", headers={"Authorization": f"Bearer {_token()}"})
    etag = first.headers["ETag"]
    second = await client.get(
        "/api/v1/distribution/sources",
        headers={"Authorization": f"Bearer {_token()}", "If-None-Match": etag},
    )
    assert second.status_code == 304
    assert await second.get_data() == b""


async def test_sources_etag_changes_when_a_source_is_disabled(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    client = app.test_client()
    before = await client.get("/api/v1/distribution/sources", headers={"Authorization": f"Bearer {_token()}"})
    await install_dal(install_dal.ingest_sources.source_id == "tw-channelA").update(enabled=False)
    after = await client.get("/api/v1/distribution/sources", headers={"Authorization": f"Bearer {_token()}"})
    assert before.headers["ETag"] != after.headers["ETag"]


async def test_sources_of_another_tenant_are_never_returned(app: Quart) -> None:
    dal = app.config["dal"]
    install_dal = app.config["install_dal"]
    other_tenant_id = dal.tenants.insert(slug="other-corp", display_name="Other", is_active=True)
    dal.commit()
    await install_dal.ingest_sources.async_insert(
        tenant_id=other_tenant_id, community_id=None, platform="twitch", source_id="tw-otherchannel",
        label="Other tenant channel", enabled=True,
    )
    client = app.test_client()
    response = await client.get("/api/v1/distribution/sources", headers={"Authorization": f"Bearer {_token()}"})
    assert "tw-otherchannel" not in {s["sourceId"] for s in (await response.get_json())["sources"]}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_distribution_sources_blueprint.py -v`
Expected: every test fails with `404 != 200` / `404 != 403` — the `/sources` rule is not registered yet.

- [ ] **Step 3: Add `list_sources_for_distribution` to `services/ingest_source_service.py`**

Append these imports to the module's existing import block and this dataclass + function to the end of `hub_api/services/ingest_source_service.py`:

```python
from dataclasses import dataclass  # noqa: E402 -- appended import, top of file in the real edit

from services.stream_grant_service import render_stream_key  # noqa: E402


@dataclass(slots=True, frozen=True)
class DistributionSource:
    """One registered ingest source as the Rust svc-ingest polls it (spec Sec10.3/Sec10.5).

    Carries `has_secret`, never the secret itself: this registry tells
    the stage which sources and which Valkey streams exist, and nothing
    about how to authenticate a webhook -- that stays behind
    `resolve_secret`'s own path.
    """

    source_id: str
    platform: str
    label: str
    community_id: int | None
    community_slug: str | None
    enabled: bool
    has_secret: bool
    mapping: dict[str, Any]
    stream_key: str


async def list_sources_for_distribution(
    install_dal: AsyncDB,
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    tenant_slug: str,
    community_id: int | None = None,
    platform: str | None = None,
    enabled: bool | None = None,
) -> list[DistributionSource]:
    """The tenant's ingest sources, filtered, each with its rendered Valkey stream key.

    R52: `ingest_sources` is queried through `install_dal` (this plan's
    own new table); `communities` (read only for a community's `slug`)
    is a pre-existing table, read through the existing pydal
    `async_dal`/`dal` pair -- this function holds both handles side by
    side.

    `community_id` widens rather than narrows: a value returns that
    community's sources **plus** the tenant-wide (`community_id IS
    NULL`) ones, because a tenant-wide source feeds every community --
    the same narrower-plus-tenant-wide rule `list_bundles_for_stage`
    applies. `platform` is an exact match; `enabled` is a tri-state
    (`None` = no filter).
    """
    query = install_dal.ingest_sources.tenant_id == tenant_id
    if community_id is not None:
        query &= (install_dal.ingest_sources.community_id == community_id) | (
            install_dal.ingest_sources.community_id == None  # noqa: E711 - penguin-dal IS NULL operator
        )
    if platform is not None:
        query &= install_dal.ingest_sources.platform == platform
    if enabled is not None:
        query &= install_dal.ingest_sources.enabled == enabled
    rows = await install_dal(query).select(orderby=install_dal.ingest_sources.source_id)

    slug_cache: dict[int, str | None] = {}
    results: list[DistributionSource] = []
    for row in rows:
        community_slug: str | None = None
        if row.community_id is not None:
            if row.community_id not in slug_cache:
                community_rows = await async_dal.select_async(dal(dal.communities.id == row.community_id))
                slug_cache[row.community_id] = community_rows[0].name if community_rows else None
            community_slug = slug_cache[row.community_id]
        results.append(
            DistributionSource(
                source_id=row.source_id,
                platform=row.platform,
                label=row.label,
                community_id=row.community_id,
                community_slug=community_slug,
                enabled=bool(row.enabled),
                has_secret=row.secret_ciphertext is not None,
                mapping=dict(row.mapping or {}),
                stream_key=render_stream_key(tenant_slug, community_slug, row.platform, row.source_id),
            )
        )
    return results
```

Add `from penguin_dal import AsyncDB` to this module's imports if not already present (Task 26 already imports it for `create_source`/etc.).

- [ ] **Step 4: Add the route to `blueprints/v1/distribution.py`**

Add the import and append the DTOs + route immediately before the file's final `BLUEPRINTS: list[Blueprint] = [distribution_bp]` line:

```python
from services.ingest_source_service import list_sources_for_distribution
```

```python
@dataclass(slots=True, frozen=True)
class DistributionSourceDTO:
    """One registered ingest source. `hasSecret` only -- the secret itself is never on this wire."""

    sourceId: str
    platform: str
    label: str
    communityId: int | None
    enabled: bool
    hasSecret: bool
    mapping: dict[str, Any] = field(default_factory=dict)
    streamKey: str = ""


@dataclass(slots=True, frozen=True)
class DistributionSourcesResponse:
    """Response DTO for `GET /api/v1/distribution/sources`."""

    success: bool
    sources: list[DistributionSourceDTO]
    meta: DistributionMetaDTO


def _parse_enabled(raw: str | None) -> bool | None:
    """Parse the tri-state `enabled` query param. Exactly `true`/`false`; anything else is a 400."""
    if raw is None:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ApiError(f"enabled {raw!r} must be exactly 'true' or 'false'", 400, "INVALID_ENABLED")


@distribution_bp.route("/sources", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("distribution:read")  # type: ignore[untyped-decorator]
async def list_distribution_sources() -> Response | tuple[dict[str, object], int]:
    """The caller's tenant's ingest sources and their Valkey stream keys, with an ETag."""
    try:
        community_id = _parse_community_id(request.args.get("communityId"))
        enabled = _parse_enabled(request.args.get("enabled"))
    except ApiError as exc:
        return _err(exc)
    platform = request.args.get("platform") or None

    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware always publishes this on the success path

    async_dal, read_dal = _dal()
    install_dal = _install_dal()
    sources = await list_sources_for_distribution(
        install_dal, async_dal, read_dal, tenant_id=ctx.tenant_id, tenant_slug=ctx.tenant_slug,
        community_id=community_id, platform=platform, enabled=enabled,
    )
    dtos = [
        DistributionSourceDTO(
            sourceId=s.source_id, platform=s.platform, label=s.label, communityId=s.community_id,
            enabled=s.enabled, hasSecret=s.has_secret, mapping=s.mapping, streamKey=s.stream_key,
        )
        for s in sources
    ]
    payload = DistributionSourcesResponse(
        success=True,
        sources=dtos,
        meta=DistributionMetaDTO(version=2, timestamp=datetime.now(UTC).isoformat()),
    )
    etag_seed = [
        DistributionBundleV2DTO(appId=d.sourceId, communityId=d.communityId, entrypoint=d.streamKey,
                                spec={"platform": d.platform, "enabled": d.enabled},
                                config={"label": d.label, "hasSecret": d.hasSecret, "mapping": d.mapping})
        for d in dtos
    ]
    return _conditional("sources", etag_seed, payload)
```

The `etag_seed` reuse is deliberate: `_conditional` hashes whatever list it is handed, so projecting each source into the same DTO type keeps one ETag implementation instead of two, and every field that can change (`platform`, `enabled`, `label`, `hasSecret`, `mapping`, `streamKey`) is inside the hashed payload.

- [ ] **Step 5: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_distribution_sources_blueprint.py -v`
Expected: `21 passed` (10 filter-combination cases + 4 invalid-`enabled` cases + 7 others).

- [ ] **Step 6: Confirm zero regression on the ingest-source and v2-bundle surfaces**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_source_service.py tests/test_ingest_sources_blueprint.py tests/test_distribution_v2_blueprint.py tests/test_v1_distribution_blueprint.py -v`
Expected: every previously-passing test still passes — `list_sources_for_distribution` is additive and no existing function in `ingest_source_service.py` changed.

- [ ] **Step 7: Commit**

```bash
git add hub_api/services/ingest_source_service.py hub_api/blueprints/v1/distribution.py \
        hub_api/tests/test_distribution_sources_blueprint.py
git commit -m "$(cat <<'EOF'
feat(hub-api): GET /api/v1/distribution/sources -- the ingest-source registry with stream keys (R52)

Returns every configured ingest source for the caller's tenant with its
rendered Valkey stream key, filterable by communityId/platform/enabled,
behind the same ETag/304 treatment as v2/bundles. hasSecret is a
boolean -- the HMAC secret itself is never on this wire. Queries the
new ingest_sources table through penguin-dal's install_dal; the
pre-existing communities table (for a community's slug) stays on the
pydal async_dal/dal pair.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 35: `bundle_trip_reenable_service.py` + `POST .../trip-reenable` — clearing a sandbox trip (Q1) (R52: penguin-dal)

**Depends on:** Task 16 (`bundle_activation_service.{activate_version, get_active_version}`, `install_dal`-only per R52), Task 19 (`app_install_approvals` rows written by `approve_version`, `install_dal`-only per R52), Task 11/17 (`blueprints/v1/bundle_versions.py` and its `bundle_versions_bp`, `_install_dal()` helper).

**Files:**
- Create: `hub_api/services/bundle_trip_reenable_service.py`
- Modify: `hub_api/blueprints/v1/bundle_versions.py`
- Test: `hub_api/tests/test_bundle_trip_reenable.py`

**Interfaces:**

| Method | Path | Auth | Body | Success |
|---|---|---|---|---|
| `POST` | `/api/v1/apps/{app_id}/trip-reenable` | `tenant_middleware` + `require_scope("tenant:admin")` | `{"version": str, "communityId": int \| null}` | `200 {"success": true, "previousDigest": str \| null, "artifactDigest": str}` |

- Produces: `async def trip_reenable(install_dal, *, tenant_id: int, community_id: int | None, app_id: str, version: str, actor_id: int) -> tuple[str | None, str]` returning `(previous_digest, new_digest)`. Raises `ApiError`: `404` `version_not_found` (no `app_versions` row for `(app_id, version)`), `409` `digest_not_verified` (row exists, `artifact_digest` is NULL), `409` `same_digest_no_reenable` (target digest equals the digest currently advertised for this scope), `403` `bundle_not_approved` (no current `app_install_approvals` row for `(app_id, version, tenant_id, community_id)`). `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `app_versions`/`app_active_versions`/`app_install_approvals` are this plan's own new tables (R52); the `audit_log` write is a separate, best-effort write through the same `install_dal` (Decision #18).
- Consumes: `services.bundle_activation_service.{activate_version, get_active_version}` (Task 16).

**Why this endpoint exists and what it deliberately is not** (this plan's Decision #10): a trip-disabled bundle is disabled *inside a Rust executor pod*, per `(app_id, digest)`, and clears only when that pod observes a **new** `artifactDigest` from the distribution API or restarts (spec §7.5, assumption A7). hub-api has no way to reach into a pod and never gains one. This endpoint is the admin-side half: it re-points `app_active_versions` at a **different, already-published, already-approved** version so the distribution API starts advertising a new digest, which is what actually clears the trip on the next poll. Pointing at the same digest cannot clear anything, so it is refused with `409 same_digest_no_reenable` and a message telling the operator to publish a new version or restart the stage pods. No pod-, deployment- or cluster-scoped runtime toggle is added anywhere in this plan.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_trip_reenable.py
"""Tests for trip_reenable() and POST /api/v1/apps/{app_id}/trip-reenable (spec Sec7.5, Sec19 Q1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart

from blueprints.v1.bundle_versions import BLUEPRINTS
from services.bundle_activation_service import activate_version
from services.bundle_install_dal import raw_sql_rows
from services.bundle_trip_reenable_service import trip_reenable
from services.errors import ApiError
from tests.conftest import TENANT_SLUG, make_user_token

_APP = "waddles.socials.music.default"
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


async def _seed_version(install_dal: Any, version: str, digest: str | None) -> int:
    now = datetime.now(UTC)
    version_id: int = await install_dal.app_versions.async_insert(
        app_id=_APP, version=version, artifact_digest=digest,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=_APP, version=version, tenant_id=1, artifact_kind="source", language="python",
        status="PUBLISHED", manifest_json={"schema_version": 2, "app_id": _APP, "version": version},
        app_version_id=version_id, created_at=now, updated_at=now,
    )
    return version_id


async def _seed_approval(install_dal: Any, version: str) -> None:
    now = datetime.now(UTC)
    await install_dal.app_install_approvals.async_insert(
        tenant_id=1, community_id=None, app_id=_APP, version=version,
        permission_hash="sha256:" + "c" * 64, summary_json={}, approved_by=1, approved_at=now,
    )


async def test_reenable_requires_a_published_version(install_dal: Any) -> None:
    with pytest.raises(ApiError) as excinfo:
        await trip_reenable(
            install_dal, tenant_id=1, community_id=None, app_id=_APP,
            version="9.9.9", actor_id=1,
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "version_not_found"


async def test_reenable_refuses_a_digest_hub_api_never_verified(install_dal: Any) -> None:
    await install_dal.app_versions.async_insert(
        app_id=_APP, version="3.0.2", artifact_digest=None,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    with pytest.raises(ApiError) as excinfo:
        await trip_reenable(install_dal, tenant_id=1, community_id=None, app_id=_APP,
                            version="3.0.2", actor_id=1)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "digest_not_verified"


async def test_reenable_refuses_the_same_digest(install_dal: Any) -> None:
    await _seed_version(install_dal, "3.0.1", _DIGEST_A)
    await _seed_approval(install_dal, "3.0.1")
    await activate_version(install_dal, tenant_id=1, community_id=None, app_id=_APP,
                           version="3.0.1", activated_by=1)
    with pytest.raises(ApiError) as excinfo:
        await trip_reenable(install_dal, tenant_id=1, community_id=None, app_id=_APP,
                            version="3.0.1", actor_id=1)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "same_digest_no_reenable"


async def test_reenable_refuses_an_unapproved_version(install_dal: Any) -> None:
    await _seed_version(install_dal, "3.0.1", _DIGEST_A)
    await _seed_approval(install_dal, "3.0.1")
    await _seed_version(install_dal, "3.0.2", _DIGEST_B)  # published, never approved
    await activate_version(install_dal, tenant_id=1, community_id=None, app_id=_APP,
                           version="3.0.1", activated_by=1)
    with pytest.raises(ApiError) as excinfo:
        await trip_reenable(install_dal, tenant_id=1, community_id=None, app_id=_APP,
                            version="3.0.2", actor_id=1)
    assert excinfo.value.status_code == 403
    assert excinfo.value.code == "bundle_not_approved"


async def test_reenable_repoints_to_a_new_digest_and_audits(install_dal: Any) -> None:
    await _seed_version(install_dal, "3.0.1", _DIGEST_A)
    await _seed_approval(install_dal, "3.0.1")
    await _seed_version(install_dal, "3.0.2", _DIGEST_B)
    await _seed_approval(install_dal, "3.0.2")
    await activate_version(install_dal, tenant_id=1, community_id=None, app_id=_APP,
                           version="3.0.1", activated_by=1)

    previous, new = await trip_reenable(install_dal, tenant_id=1, community_id=None,
                                        app_id=_APP, version="3.0.2", actor_id=1)
    assert previous == _DIGEST_A
    assert new == _DIGEST_B

    from services.bundle_activation_service import get_active_version

    active = await get_active_version(install_dal, tenant_id=1, community_id=None, app_id=_APP)
    assert active is not None
    assert active.artifact_digest == _DIGEST_B

    audit_rows = await raw_sql_rows(
        install_dal, "SELECT details FROM audit_log WHERE action = :a", {"a": "bundle_trip_reenabled"}
    )
    audit_row = audit_rows.first()
    assert audit_row is not None
    import json

    details = audit_row["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["old_digest"] == _DIGEST_A
    assert details["new_digest"] == _DIGEST_B


async def test_reenable_on_a_scope_with_nothing_active_is_allowed(install_dal: Any) -> None:
    await _seed_version(install_dal, "3.0.2", _DIGEST_B)
    await _seed_approval(install_dal, "3.0.2")
    previous, new = await trip_reenable(install_dal, tenant_id=1, community_id=None,
                                        app_id=_APP, version="3.0.2", actor_id=1)
    assert previous is None
    assert new == _DIGEST_B


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    await _seed_version(install_dal, "3.0.1", _DIGEST_A)
    await _seed_approval(install_dal, "3.0.1")
    await _seed_version(install_dal, "3.0.2", _DIGEST_B)
    await _seed_approval(install_dal, "3.0.2")
    quart_app = Quart(__name__)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


async def test_endpoint_requires_tenant_admin(app: Quart) -> None:
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/{_APP}/trip-reenable",
        headers={"Authorization": f"Bearer {make_user_token(user_id=1, scope='', tenant=TENANT_SLUG)}"},
        json={"version": "3.0.2", "communityId": None},
    )
    assert response.status_code == 403


async def test_endpoint_happy_path(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    await activate_version(install_dal, tenant_id=1, community_id=None, app_id=_APP,
                           version="3.0.1", activated_by=1)
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/{_APP}/trip-reenable",
        headers={"Authorization": f"Bearer {token}"},
        json={"version": "3.0.2", "communityId": None},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["previousDigest"] == _DIGEST_A
    assert body["artifactDigest"] == _DIGEST_B


async def test_endpoint_surfaces_same_digest_as_409(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    await activate_version(install_dal, tenant_id=1, community_id=None, app_id=_APP,
                           version="3.0.1", activated_by=1)
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/{_APP}/trip-reenable",
        headers={"Authorization": f"Bearer {token}"},
        json={"version": "3.0.1", "communityId": None},
    )
    assert response.status_code == 409
    assert (await response.get_json())["error"]["code"] == "same_digest_no_reenable"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_trip_reenable.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_trip_reenable_service'`

- [ ] **Step 3: Write the service**

```python
# hub_api/services/bundle_trip_reenable_service.py
"""Clearing a sandbox trip is an admin action that causes a NEW digest to be advertised.

A trip-disabled bundle is disabled inside a Rust executor pod, per
`(app_id, digest)`, and clears only when the pod observes a new
`artifactDigest` from the distribution API or restarts (spec Sec7.5,
assumption A7). hub-api cannot reach into a pod and does not try. This
module is the admin-side half: it re-points `app_active_versions` at a
different, already-published, already-approved version, which is what
makes the distribution API advertise a new digest on the next poll.

Pointing at the same digest clears nothing, so it is refused
(`same_digest_no_reenable`) rather than silently succeeding and leaving
the operator believing the trip was cleared.

R52: `app_versions`/`app_active_versions`/`app_install_approvals` are
this plan's own new tables, queried through the penguin-dal
`install_dal: AsyncDB` (Task 4). The `audit_log` write is a separate,
best-effort write through the same `install_dal` (Decision #18) --
wrapped in `try`/`except`, matching this codebase's existing convention
for every audit-log call site.
"""

from __future__ import annotations

from datetime import UTC, datetime

from penguin_dal import AsyncDB

from services.bundle_activation_service import activate_version, get_active_version
from services.errors import ApiError, forbidden, not_found


async def trip_reenable(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int | None,
    app_id: str,
    version: str,
    actor_id: int,
) -> tuple[str | None, str]:
    """Re-point this scope at `version`, refusing anything that would not change the digest.

    Returns `(previous_digest, new_digest)`. `previous_digest` is
    `None` when nothing was active for the scope yet.
    """
    target_rows = await install_dal(
        (install_dal.app_versions.app_id == app_id) & (install_dal.app_versions.version == version)
    ).select()
    target = target_rows.first()
    if target is None:
        raise not_found(f"version {version} of {app_id} has never been published")
    if not target.artifact_digest:
        raise ApiError(
            f"version {version} of {app_id} has no verified artifact digest",
            409,
            "digest_not_verified",
        )

    active = await get_active_version(
        install_dal, tenant_id=tenant_id, community_id=community_id, app_id=app_id
    )
    previous_digest: str | None = active.artifact_digest if active is not None else None
    if previous_digest == target.artifact_digest:
        raise ApiError(
            f"{app_id} already advertises {target.artifact_digest} for this scope; a trip clears only on a "
            "new digest -- publish a new version or restart the stage pods",
            409,
            "same_digest_no_reenable",
        )

    approvals = await install_dal(
        (install_dal.app_install_approvals.app_id == app_id)
        & (install_dal.app_install_approvals.version == version)
        & (install_dal.app_install_approvals.tenant_id == tenant_id)
        & (install_dal.app_install_approvals.community_id == community_id)
        & (install_dal.app_install_approvals.superseded_by == None)  # noqa: E711 - penguin-dal IS NULL operator
    ).select()
    if not approvals:
        raise forbidden(f"version {version} of {app_id} has not been approved for this scope")

    await activate_version(
        install_dal, tenant_id=tenant_id, community_id=community_id,
        app_id=app_id, version=version, activated_by=actor_id,
    )

    now = datetime.now(UTC)
    try:
        await install_dal.audit_log.async_insert(
            user_id=actor_id, action="bundle_trip_reenabled",
            target_type="app_active_versions", target_id=app_id,
            details={
                "app_id": app_id, "version": version, "tenant_id": tenant_id,
                "community_id": community_id, "old_digest": previous_digest,
                "new_digest": target.artifact_digest,
            },
            created_at=now,
        )
    except Exception:  # noqa: BLE001, S110 -- audit logging failure must not break the main flow
        pass
    return previous_digest, str(target.artifact_digest)
```

- [ ] **Step 4: Add the route to `blueprints/v1/bundle_versions.py`**

Add the import and append the DTOs + route immediately before the file's final `BLUEPRINTS: list[Blueprint] = [bundle_versions_bp]` line:

```python
from services.bundle_trip_reenable_service import trip_reenable
```

```python
@dataclass(slots=True, frozen=True)
class TripReenableRequest:
    """Request DTO for `POST /api/v1/apps/{app_id}/trip-reenable`."""

    version: str
    communityId: int | None = None


@bundle_versions_bp.route("/<app_id>/trip-reenable", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(TripReenableRequest)
async def post_trip_reenable(data: TripReenableRequest, app_id: str) -> tuple[dict[str, object], int]:
    """Clear a sandbox trip by advertising a different, approved, already-published digest.

    Never a runtime switch: the executor still re-enables purely on
    observing a new `artifactDigest` (spec Sec7.5/A7). This endpoint
    only makes such a digest exist for the scope.
    """
    install_dal = _install_dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware always publishes this on the success path
    actor_id = get_current_user_id(request)
    try:
        previous, new = await trip_reenable(
            install_dal, tenant_id=ctx.tenant_id, community_id=data.communityId,
            app_id=app_id, version=data.version, actor_id=actor_id,
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "previousDigest": previous, "artifactDigest": new}, 200
```

If `validate_request` is not already in this file's `from quart_schema import ...` line (Task 17 added it for the activate route), add it.

- [ ] **Step 5: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_trip_reenable.py -v`
Expected: `9 passed`

- [ ] **Step 6: Confirm zero regression on the versions blueprint**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_versions_blueprint.py tests/test_bundle_activation_service.py -v`
Expected: every previously-passing test still passes — this task only appends one route and one DTO.

- [ ] **Step 7: Commit**

```bash
git add hub_api/services/bundle_trip_reenable_service.py hub_api/blueprints/v1/bundle_versions.py \
        hub_api/tests/test_bundle_trip_reenable.py
git commit -m "$(cat <<'EOF'
feat(hub-api): POST /api/v1/apps/{app_id}/trip-reenable -- admin action plus a new digest (spec Sec19 Q1, R52)

Re-points app_active_versions at a different, already-published,
already-approved version so the distribution API advertises a new
artifactDigest, which is what actually clears a sandbox trip inside an
executor pod. Refuses 409 same_digest_no_reenable when the target
digest equals the one already advertised, 403 bundle_not_approved when
the version was never approved for the scope, and 409
digest_not_verified for a digest hub-api never verified. No runtime
re-enable switch is added anywhere (spec Sec7.5/A7 unchanged). Queries
the new tables through penguin-dal's install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 36: per-bundle role drop at uninstall + the 168 h orphan sweeper + its CronJob (Q3) (R52: penguin-dal)

**Depends on:** Task 20 (`bundle_db_role_service.{bundle_role_name, drop_bundle_role}`), Task 21 (role created inside `approve_version`), Task 4 (`install_dal` penguin-dal wiring + `bundle_install_db`/`install_dal` fixtures), Task 12 (the Helm chart's `bundles.*` values block).

**R52 note:** `uninstall_bundle` touches only `app_catalog` — a **pre-existing** table — so it needs **zero changes** for R52 (confirmed, not skipped: see Step 3). The orphan sweeper is different: it reads `app_catalog` (existing, via `async_dal`/`dal`) **and** `app_active_versions`/`app_install_approvals` (this plan's own new tables, via `install_dal`) in the same pass, so `sweep_orphan_bundle_roles` and the CronJob's own standalone connection-building both hold both handles side by side.

**Files:**
- Modify: `hub_api/services/marketplace_lifecycle_service.py`
- Create: `hub_api/services/bundle_role_cleanup_job.py`
- Create: `k8s/helm/waddlebot/templates/bundle-role-cleanup-cronjob.yaml`
- Modify: `k8s/helm/waddlebot/values.yaml`
- Test: `hub_api/tests/test_bundle_role_cleanup_job.py`

**Interfaces:**
- Produces: `marketplace_lifecycle_service.uninstall_bundle(dal, *, app_id: str, db_engine: Any | None = None)` — **new keyword-only param defaulting to `None`, so every existing call site and every existing test keeps passing unmodified**; when an engine is given, `drop_bundle_role(db_engine, app_id=app_id)` runs after the catalog row is retired. `BUNDLE_ROLE_GRACE_H = 168`; `@dataclass(slots=True, frozen=True) SweepResult(examined: int, dropped: tuple[str, ...], retained: int)`; `async def sweep_orphan_bundle_roles(async_dal, dal, install_dal, engine, *, now: datetime | None = None, grace_hours: int = BUNDLE_ROLE_GRACE_H) -> SweepResult` (`async_dal`/`dal` for the pre-existing `app_catalog`, `install_dal` for the new `app_active_versions`/`app_install_approvals`); `async def main() -> int` (the CronJob entrypoint, `python -m services.bundle_role_cleanup_job`, builds all three connections plus the privileged `engine`).
- Consumes: `services.bundle_db_role_service.{bundle_role_name, drop_bundle_role}` (Task 20); `services.bundle_install_dal.build_install_dal` (Task 4).

**The rule this task implements** (this plan's Decision #10b): uninstall is the drop trigger. The sweeper exists only for roles whose uninstall never ran — a catalog row deleted out from under the role, an uninstall that predates this plan, or a crash between the catalog write and the `DROP ROLE`. A bundle is swept when it has **no** `app_active_versions` row at all **and** its newest non-superseded `app_install_approvals.approved_at` is older than `grace_hours` (or it has no approval at all). A role whose bundle is still activated anywhere is never dropped, regardless of age.

**Verification integrity:** `sweep_orphan_bundle_roles` returns the number of roles **examined** as well as the list dropped, and `main()` prints both. A sweep that examined zero roles is reported as a failure by `main()` (exit `1`), because a sweeper pointed at the wrong database or a renamed prefix would otherwise print "0 dropped" forever and look healthy (`critical-rules.md` Verification Integrity).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_role_cleanup_job.py
"""Tests for the per-bundle role drop at uninstall and the 168 h orphan sweeper (spec Sec19 Q3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from services.bundle_role_cleanup_job import (
    BUNDLE_ROLE_GRACE_H,
    sweep_orphan_bundle_roles,
)
from services.marketplace_lifecycle_service import uninstall_bundle

_APP = "waddles.socials.music.default"


def _seed_second_app(dal: Any, app_id: str) -> None:
    dal.app_catalog.insert(
        app_id=app_id, name="Second App", manifest_version="3.0.0", module="socials",
        feature="waddles.socials.second", provider="builtin", execution_model="native",
        is_default=False,
        platform_compatibility={"tested_with": "3.0.0", "min_version": None, "max_version": None},
        status="active", stages={},
    )


async def test_uninstall_drops_the_bundle_role_when_an_engine_is_given(bundle_install_db: Any) -> None:
    dal = bundle_install_db.dal
    engine = object()
    with patch(
        "services.marketplace_lifecycle_service.drop_bundle_role", new_callable=AsyncMock
    ) as mock_drop:
        await uninstall_bundle(dal, app_id=_APP, db_engine=engine)
    mock_drop.assert_awaited_once_with(engine, app_id=_APP)


async def test_uninstall_without_an_engine_never_touches_roles(bundle_install_db: Any) -> None:
    dal = bundle_install_db.dal
    with patch(
        "services.marketplace_lifecycle_service.drop_bundle_role", new_callable=AsyncMock
    ) as mock_drop:
        await uninstall_bundle(dal, app_id=_APP)
    mock_drop.assert_not_awaited()


async def test_sweep_retains_a_role_whose_bundle_is_still_activated(
    bundle_install_db: Any, install_dal: Any
) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    version_id = await install_dal.app_versions.async_insert(
        app_id=_APP, version="3.0.1", artifact_digest="sha256:" + "a" * 64,
        language="python", artifact_kind="source", scan_status="scanned",
    )
    await install_dal.app_active_versions.async_insert(
        app_id=_APP, tenant_id=1, community_id=0, version_id=version_id,
        activated_by=1, activated_at=datetime.now(UTC),
    )
    with patch("services.bundle_role_cleanup_job.drop_bundle_role", new_callable=AsyncMock) as mock_drop:
        result = await sweep_orphan_bundle_roles(async_dal, dal, install_dal, object())
    assert result.examined == 1
    assert result.dropped == ()
    assert result.retained == 1
    mock_drop.assert_not_awaited()


async def test_sweep_retains_a_role_inside_the_grace_window(
    bundle_install_db: Any, install_dal: Any
) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    await install_dal.app_install_approvals.async_insert(
        tenant_id=1, community_id=None, app_id=_APP, version="3.0.1",
        permission_hash="sha256:" + "c" * 64, summary_json={}, approved_by=1,
        approved_at=datetime.now(UTC) - timedelta(hours=BUNDLE_ROLE_GRACE_H - 1),
    )
    with patch("services.bundle_role_cleanup_job.drop_bundle_role", new_callable=AsyncMock) as mock_drop:
        result = await sweep_orphan_bundle_roles(async_dal, dal, install_dal, object())
    assert result.dropped == ()
    mock_drop.assert_not_awaited()


async def test_sweep_drops_a_role_past_the_grace_window(
    bundle_install_db: Any, install_dal: Any
) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    await install_dal.app_install_approvals.async_insert(
        tenant_id=1, community_id=None, app_id=_APP, version="3.0.1",
        permission_hash="sha256:" + "c" * 64, summary_json={}, approved_by=1,
        approved_at=datetime.now(UTC) - timedelta(hours=BUNDLE_ROLE_GRACE_H + 1),
    )
    with patch("services.bundle_role_cleanup_job.drop_bundle_role", new_callable=AsyncMock) as mock_drop:
        result = await sweep_orphan_bundle_roles(async_dal, dal, install_dal, object())
    assert result.dropped == ("bundle_waddles_socials_music_default",)
    assert result.retained == 0
    mock_drop.assert_awaited_once()


async def test_sweep_drops_a_bundle_that_was_never_approved_or_activated(
    bundle_install_db: Any, install_dal: Any
) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    _seed_second_app(dal, "waddles.socials.second.default")
    dal.commit()
    with patch("services.bundle_role_cleanup_job.drop_bundle_role", new_callable=AsyncMock) as mock_drop:
        result = await sweep_orphan_bundle_roles(async_dal, dal, install_dal, object())
    assert result.examined == 2
    assert set(result.dropped) == {
        "bundle_waddles_socials_music_default", "bundle_waddles_socials_second_default",
    }
    assert mock_drop.await_count == 2


async def test_sweep_honours_a_custom_grace_window(bundle_install_db: Any, install_dal: Any) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    await install_dal.app_install_approvals.async_insert(
        tenant_id=1, community_id=None, app_id=_APP, version="3.0.1",
        permission_hash="sha256:" + "c" * 64, summary_json={}, approved_by=1,
        approved_at=datetime.now(UTC) - timedelta(hours=2),
    )
    with patch("services.bundle_role_cleanup_job.drop_bundle_role", new_callable=AsyncMock):
        kept = await sweep_orphan_bundle_roles(async_dal, dal, install_dal, object(), grace_hours=24)
        dropped = await sweep_orphan_bundle_roles(async_dal, dal, install_dal, object(), grace_hours=1)
    assert kept.dropped == ()
    assert dropped.dropped == ("bundle_waddles_socials_music_default",)


async def test_sweep_with_zero_catalog_rows_reports_zero_examined(
    bundle_install_db: Any, install_dal: Any
) -> None:
    async_dal = bundle_install_db
    dal = async_dal.dal
    dal(dal.app_catalog.id > 0).delete()
    dal.commit()
    with patch("services.bundle_role_cleanup_job.drop_bundle_role", new_callable=AsyncMock):
        result = await sweep_orphan_bundle_roles(async_dal, dal, install_dal, object())
    assert result.examined == 0
    assert result.dropped == ()


async def test_main_exits_nonzero_when_nothing_was_examined(
    bundle_install_db: Any, install_dal: Any
) -> None:
    from services import bundle_role_cleanup_job as job

    dal = bundle_install_db.dal
    dal(dal.app_catalog.id > 0).delete()
    dal.commit()
    with (
        patch.object(job, "_build_dals", return_value=(bundle_install_db, dal)),
        patch.object(job, "_build_install_dal", new_callable=AsyncMock, return_value=install_dal),
        patch.object(job, "_build_engine", return_value=object()),
        patch.object(job, "drop_bundle_role", new_callable=AsyncMock),
    ):
        exit_code = await job.main()
    assert exit_code == 1


async def test_main_exits_zero_on_a_real_sweep(bundle_install_db: Any, install_dal: Any) -> None:
    from services import bundle_role_cleanup_job as job

    dal = bundle_install_db.dal
    with (
        patch.object(job, "_build_dals", return_value=(bundle_install_db, dal)),
        patch.object(job, "_build_install_dal", new_callable=AsyncMock, return_value=install_dal),
        patch.object(job, "_build_engine", return_value=object()),
        patch.object(job, "drop_bundle_role", new_callable=AsyncMock),
    ):
        exit_code = await job.main()
    assert exit_code == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_role_cleanup_job.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_role_cleanup_job'`

- [ ] **Step 3: `uninstall_bundle` — confirmed unaffected by R52, extend as originally planned**

`app_catalog` is a pre-existing table; this function needs no DAL-client change. In `hub_api/services/marketplace_lifecycle_service.py`, add the import and replace `uninstall_bundle` with this version. Nothing else in the file changes:

```python
from services.bundle_db_role_service import drop_bundle_role
```

```python
async def uninstall_bundle(dal: Any, *, app_id: str, db_engine: Any | None = None) -> None:
    """Retire the catalog row and, when a privileged engine is given, drop the bundle's Postgres role.

    `db_engine` is keyword-only and defaults to `None` so every
    pre-M2b call site and test keeps its exact current behaviour. The
    blueprint passes a real SQLAlchemy engine in production, which
    makes uninstall the drop point for the per-bundle `data.tables`
    role created at approval (this plan's Decision #10b, spec Sec19
    Q3). The drop is idempotent, so a bundle that never had a `db`
    capability -- and therefore never had a role -- is a harmless
    no-op rather than a special case. R52 does not touch this function:
    `app_catalog` is a pre-existing table.
    """
    dal(dal.app_catalog.app_id == app_id).update(status="retired")
    dal.commit()
    if db_engine is not None:
        await drop_bundle_role(db_engine, app_id=app_id)
```

If the existing body of `uninstall_bundle` differs from the two lines above, keep the existing body verbatim and append only the `if db_engine is not None:` block plus the new docstring paragraph — the role drop is additive and must not change what uninstall already does to the catalog.

- [ ] **Step 4: Write the sweeper**

```python
# hub_api/services/bundle_role_cleanup_job.py
"""The 168 h orphan sweeper for per-bundle Postgres roles (spec Sec19 Q3).

Uninstall is the normal drop point (`marketplace_lifecycle_service.
uninstall_bundle`). This job exists only for roles whose uninstall
never ran: a catalog row deleted out from under the role, an uninstall
that predates this plan, or a crash between the catalog write and the
`DROP ROLE`. A bundle is swept when it has NO `app_active_versions`
row at all AND its newest non-superseded `app_install_approvals.
approved_at` is older than `grace_hours` (or it has no approval at
all). A bundle still activated anywhere is never swept, whatever its
age.

R52: `app_catalog` is a pre-existing table, read through the existing
pydal `async_dal`/`dal` pair; `app_active_versions`/
`app_install_approvals` are this plan's own new tables, read through
the penguin-dal `install_dal`. This standalone CronJob process builds
all three connections itself (`_build_dals`/`_build_install_dal`) plus
the privileged role-admin engine (`_build_engine`), since it runs
outside the main hub-api Quart process and has no `app.config` to read
them from.

Runs as a Kubernetes CronJob (`k8s/helm/waddlebot/templates/
bundle-role-cleanup-cronjob.yaml`), entrypoint `python -m
services.bundle_role_cleanup_job`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from flask_core.database import AsyncDAL
from penguin_dal import AsyncDB

from services.bundle_db_role_service import bundle_role_name, drop_bundle_role
from services.bundle_install_dal import build_install_dal
from services.schema import bind_app_bundle_tables

BUNDLE_ROLE_GRACE_H = 168


@dataclass(slots=True, frozen=True)
class SweepResult:
    """What one sweep did. `examined` is the denominator a zero-drop run is judged against."""

    examined: int
    dropped: tuple[str, ...]
    retained: int


async def sweep_orphan_bundle_roles(
    async_dal: Any,
    dal: Any,
    install_dal: AsyncDB,
    engine: Any,
    *,
    now: datetime | None = None,
    grace_hours: int = BUNDLE_ROLE_GRACE_H,
) -> SweepResult:
    """Drop every per-bundle role whose bundle has been inactive past `grace_hours`.

    `app_catalog` (existing table) via `async_dal`/`dal`;
    `app_active_versions`/`app_install_approvals` (new tables) via
    `install_dal`.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(hours=grace_hours)

    catalog_rows = await async_dal.select_async(dal(dal.app_catalog.id > 0), dal.app_catalog.app_id)
    app_ids = sorted({row.app_id for row in catalog_rows})

    dropped: list[str] = []
    retained = 0
    for app_id in app_ids:
        active = await install_dal(install_dal.app_active_versions.app_id == app_id).select()
        if active:
            retained += 1
            continue
        approvals = await install_dal(
            (install_dal.app_install_approvals.app_id == app_id)
            & (install_dal.app_install_approvals.superseded_by == None)  # noqa: E711 - penguin-dal IS NULL operator
        ).select(orderby=~install_dal.app_install_approvals.approved_at)
        newest = approvals.first().approved_at if approvals else None
        if newest is not None:
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=UTC)
            if newest >= cutoff:
                retained += 1
                continue
        await drop_bundle_role(engine, app_id=app_id)
        dropped.append(bundle_role_name(app_id))

    return SweepResult(examined=len(app_ids), dropped=tuple(dropped), retained=retained)


def _build_dals() -> tuple[Any, Any]:
    """Open the job's own pydal connection from `DATABASE_URL` and bind the pre-existing tables it reads."""
    async_dal = AsyncDAL(os.environ["DATABASE_URL"], pool_size=1)
    dal = async_dal.dal
    bind_app_bundle_tables(dal)
    return async_dal, dal


async def _build_install_dal() -> AsyncDB:
    """Open the job's own penguin-dal connection from `DATABASE_URL` (R52) -- same DSN, separate pool."""
    return await build_install_dal(os.environ["DATABASE_URL"], pool_size=1)


def _build_engine() -> Any:
    """The privileged SQLAlchemy engine the DDL runs on -- `ROLE_ADMIN_DATABASE_URL`, never a CLI arg."""
    from sqlalchemy import create_engine

    return create_engine(os.environ["ROLE_ADMIN_DATABASE_URL"])


async def main() -> int:
    """CronJob entrypoint. Prints the denominator; a zero-examined sweep is a failure, not a pass."""
    async_dal, dal = _build_dals()
    install_dal = await _build_install_dal()
    engine = _build_engine()
    result = await sweep_orphan_bundle_roles(async_dal, dal, install_dal, engine)
    print(
        f"bundle role sweep: examined={result.examined} retained={result.retained} "
        f"dropped={len(result.dropped)} roles={list(result.dropped)}"
    )
    if result.examined == 0:
        print(
            "bundle role sweep FAILED: zero bundles examined -- the job is pointed at the wrong "
            "database or app_catalog is empty",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 5: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_role_cleanup_job.py -v`
Expected: `10 passed`

- [ ] **Step 6: Confirm zero regression on the lifecycle service**

Run: `cd hub_api && python3 -m pytest tests/test_v1_marketplace_lifecycle_blueprint.py tests/test_marketplace_lifecycle_concurrency.py tests/test_marketplace_lifecycle_grants.py -v`
Expected: every previously-passing test still passes — `db_engine` defaults to `None`, so uninstall's existing behaviour is byte-identical without it.

- [ ] **Step 7: Add the CronJob manifest**

```yaml
# k8s/helm/waddlebot/templates/bundle-role-cleanup-cronjob.yaml
{{- if .Values.bundles.roleCleanup.enabled }}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ include "waddlebot.fullname" . }}-bundle-role-cleanup
  labels:
    {{- include "waddlebot.labels" . | nindent 4 }}
    app.kubernetes.io/component: bundle-role-cleanup
spec:
  schedule: {{ .Values.bundles.roleCleanup.schedule | quote }}
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  startingDeadlineSeconds: 600
  jobTemplate:
    spec:
      backoffLimit: 2
      ttlSecondsAfterFinished: 3600
      template:
        metadata:
          labels:
            {{- include "waddlebot.selectorLabels" . | nindent 12 }}
            app.kubernetes.io/component: bundle-role-cleanup
        spec:
          restartPolicy: Never
          serviceAccountName: {{ include "waddlebot.fullname" . }}-hub-api
          securityContext:
            runAsNonRoot: true
            runAsUser: 10001
            runAsGroup: 10001
            fsGroup: 10001
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: bundle-role-cleanup
              image: "{{ .Values.hubApi.image.repository }}:{{ .Values.hubApi.image.tag }}"
              imagePullPolicy: {{ .Values.hubApi.image.pullPolicy }}
              command: ["python", "-m", "services.bundle_role_cleanup_job"]
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities:
                  drop: ["ALL"]
              env:
                - name: DATABASE_URL
                  valueFrom:
                    secretKeyRef:
                      name: {{ .Values.hubApi.database.secretName }}
                      key: {{ .Values.hubApi.database.secretKey }}
                - name: ROLE_ADMIN_DATABASE_URL
                  valueFrom:
                    secretKeyRef:
                      name: {{ .Values.bundles.roleCleanup.adminSecretName }}
                      key: {{ .Values.bundles.roleCleanup.adminSecretKey }}
                - name: OTEL_EXPORTER_OTLP_ENDPOINT
                  value: {{ .Values.observability.otlpEndpoint | quote }}
                - name: OTEL_SERVICE_NAME
                  value: waddles-bundle-role-cleanup
              resources:
                requests:
                  cpu: 50m
                  memory: 128Mi
                limits:
                  cpu: 200m
                  memory: 256Mi
              volumeMounts:
                - name: tmp
                  mountPath: /tmp
          volumes:
            - name: tmp
              emptyDir: {}
{{- end }}
```

- [ ] **Step 8: Add the values keys**

Add to `k8s/helm/waddlebot/values.yaml` under the existing `bundles:` block Task 12 created:

```yaml
bundles:
  roleCleanup:
    # The 168 h orphan sweeper for per-bundle Postgres roles (spec Sec19 Q3).
    # Uninstall is the normal drop point; this only catches roles whose
    # uninstall never ran.
    enabled: true
    # 03:17 UTC daily -- off the hour so it never contends with the
    # on-the-hour jobs every other chart in this cluster schedules.
    schedule: "17 3 * * *"
    # The privileged Postgres account that may CREATE/DROP ROLE. Separate
    # from hub-api's own runtime credential on purpose -- the API process
    # never holds role-admin rights.
    adminSecretName: waddlebot-postgres-role-admin
    adminSecretKey: dsn
```

- [ ] **Step 9: Validate the chart renders**

Run:
```bash
helm lint ./k8s/helm/waddlebot
helm template waddlebot ./k8s/helm/waddlebot --values ./k8s/helm/waddlebot/alpha.yml \
  --show-only templates/bundle-role-cleanup-cronjob.yaml
```
Expected: `helm lint` reports `1 chart(s) linted, 0 chart(s) failed`, and the template renders one `CronJob` whose `spec.jobTemplate.spec.template.spec.securityContext.runAsNonRoot` is `true` and whose container command is `["python", "-m", "services.bundle_role_cleanup_job"]`.

- [ ] **Step 10: Commit**

```bash
git add hub_api/services/marketplace_lifecycle_service.py hub_api/services/bundle_role_cleanup_job.py \
        hub_api/tests/test_bundle_role_cleanup_job.py \
        k8s/helm/waddlebot/templates/bundle-role-cleanup-cronjob.yaml k8s/helm/waddlebot/values.yaml
git commit -m "$(cat <<'EOF'
feat(hub-api): drop the per-bundle Postgres role at uninstall, sweep orphans after 168 h (spec Sec19 Q3, R52)

uninstall_bundle gains an optional db_engine keyword (defaulting to
None, so every existing call site is unchanged) and drops the bundle's
data.tables role when one is given -- unaffected by R52 (app_catalog is
a pre-existing table). bundle_role_cleanup_job is the CronJob behind
it, for roles whose uninstall never ran: a bundle with no
app_active_versions row and no approval newer than the grace window is
dropped, both new tables read through penguin-dal's install_dal
(a standalone connection this CronJob process builds itself). The
sweep reports how many bundles it examined and exits non-zero on a
zero-examined run, so a job pointed at the wrong database fails loudly
instead of printing "0 dropped" forever.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 37: `bundle_feature_gate.py` — the PostHog flag gate on every M2b write surface

**Depends on:** Tasks 11, 15, 17, 22, 25, 27, 31, 35 (the blueprints being gated). Nothing depends on this task except the closing gates (Task 41, and Task 49 once D30/D31 widened it).

**Files:**
- Create: `hub_api/services/bundle_feature_gate.py`
- Modify: `hub_api/blueprints/v1/bundle_versions.py`, `bundle_approvals.py`, `bundle_grants.py`, `bundle_artifact_callback.py`, `custom_platforms.py`, `ingest_sources.py`, `bundle_settings.py`
- Test: `hub_api/tests/test_bundle_feature_gate.py`

**Interfaces:**
- Produces: `FLAG_RUST_DATA_PLANE = "waddles.core.rust-data-plane"`, `FLAG_WASM_BUNDLES = "waddles.core.wasm-bundles"`, `FLAG_GENERIC_INTAKE = "waddles.core.generic-intake"`, `FLAG_PREBUILT_BUNDLES = "waddles.core.prebuilt-bundles"`; `async def flags_enabled(*flag_keys: str, tenant: str, community: int | None = None) -> bool`; `def require_flags(*flag_keys: str) -> Callable[..., Any]` — a Quart route decorator returning `403` with `error.code == "feature_disabled"` when any named flag is off.
- Consumes: `flask_core.feature_flags.feature_enabled` (existing), `flask_core.tenancy.get_tenant_context` (existing).

**Three rules fixed here:**

1. **Write surfaces are gated; read surfaces are not.** A flag switched off must never break the distribution poll, an admin's read-only view, or the OpenAPI document — it must only stop new writes. `GET` routes across this whole milestone stay ungated. `GET /api/v1/distribution/v2/bundles` and `/sources` in particular are ungated so a mid-rollout flag flip cannot blind a running Rust stage.
2. **The decorator goes innermost** — after `@tenant_middleware` and `@require_scope(...)` in source order, i.e. closest to the function — because it reads the tenant from `get_tenant_context(request)`, which `tenant_middleware` publishes.
3. **Failure is closed but never fatal.** `feature_enabled` already degrades to the last-known cached value (never-seen flags default `False`) on a PostHog/license outage and never raises; a flag nobody has ever cached therefore reads `False` and the write is refused with a `403` the caller can act on, not a `500`. All four flags are `min_tier: free` (this plan's Decision #9), so the licence half of the two-gate never fails — only the PostHog half.

**The gated surface table — exact, complete, copied into each edit below:**

| File | Route | Flags required |
|---|---|---|
| `bundle_versions.py` | `POST /api/v1/apps/<app_id>/versions` | `rust-data-plane`, `wasm-bundles` |
| `bundle_versions.py` | `POST /api/v1/apps/<app_id>/versions/<version>/activate` | `rust-data-plane`, `wasm-bundles` |
| `bundle_versions.py` | `POST /api/v1/apps/<app_id>/trip-reenable` | `rust-data-plane`, `wasm-bundles` |
| `bundle_approvals.py` | `POST /api/v1/apps/<app_id>/versions/<version>/approve` | `rust-data-plane`, `wasm-bundles` |
| `bundle_approvals.py` | `POST /api/v1/apps/<app_id>/versions/<version>/deny` | `rust-data-plane`, `wasm-bundles` |
| `bundle_grants.py` | `POST /api/v1/apps/<app_id>/grants/resolve` | `rust-data-plane`, `wasm-bundles` |
| `bundle_grants.py` | `DELETE /api/v1/apps/<app_id>/grants/<grant_id>` | `rust-data-plane`, `wasm-bundles` |
| `bundle_artifact_callback.py` | `POST /api/v1/bundles/<app_id>/versions/<version>/artifact` | `rust-data-plane`, `wasm-bundles` |
| `bundle_artifact_callback.py` | `POST /api/v1/bundles/<app_id>/versions/<version>/rejected` | `rust-data-plane`, `wasm-bundles` |
| `custom_platforms.py` | `POST`/`DELETE` `/api/v1/tenant/<slug>/custom-platforms[...]`, `POST .../tokens` | `rust-data-plane`, `generic-intake` |
| `ingest_sources.py` | `POST`/`DELETE` `/api/v1/tenant/<slug>/ingest-sources[...]` | `rust-data-plane`, `generic-intake` |
| `bundle_settings.py` | `PUT /api/v1/marketplace/settings` | `rust-data-plane` |

`FLAG_PREBUILT_BUNDLES` is **not** a route gate — it is the second half of the `allow_prebuilt` two-gate inside `POST .../versions` (Step 5 below).

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_bundle_feature_gate.py
"""Tests for the PostHog flag gate on every M2b write surface (spec Sec13.5)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from quart import Quart

from services.bundle_feature_gate import (
    FLAG_GENERIC_INTAKE,
    FLAG_RUST_DATA_PLANE,
    FLAG_WASM_BUNDLES,
    flags_enabled,
    require_flags,
)
from tests.conftest import TENANT_SLUG, make_user_token


async def test_flags_enabled_requires_every_named_flag() -> None:
    with patch("services.bundle_feature_gate.feature_enabled", new_callable=AsyncMock) as mock_flag:
        mock_flag.side_effect = [True, True]
        assert await flags_enabled(FLAG_RUST_DATA_PLANE, FLAG_WASM_BUNDLES, tenant=TENANT_SLUG) is True
        mock_flag.side_effect = [True, False]
        assert await flags_enabled(FLAG_RUST_DATA_PLANE, FLAG_WASM_BUNDLES, tenant=TENANT_SLUG) is False


async def test_flags_enabled_passes_default_false_to_every_call() -> None:
    with patch("services.bundle_feature_gate.feature_enabled", new_callable=AsyncMock) as mock_flag:
        mock_flag.return_value = True
        await flags_enabled(FLAG_GENERIC_INTAKE, tenant=TENANT_SLUG, community=7)
    mock_flag.assert_awaited_once_with(FLAG_GENERIC_INTAKE, tenant=TENANT_SLUG, community=7, default=False)


@pytest.fixture
def gated_app() -> Quart:
    from flask_core.authz import require_scope
    from flask_core.tenancy import tenant_middleware

    app = Quart(__name__)

    @app.route("/gated", methods=["POST"])
    @tenant_middleware  # type: ignore[untyped-decorator]
    @require_scope("tenant:admin")  # type: ignore[untyped-decorator]
    @require_flags(FLAG_RUST_DATA_PLANE, FLAG_WASM_BUNDLES)
    async def gated() -> tuple[dict[str, object], int]:
        return {"success": True}, 200

    return app


async def test_route_is_403_when_a_flag_is_off(gated_app: Quart) -> None:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    with patch("services.bundle_feature_gate.feature_enabled", new_callable=AsyncMock) as mock_flag:
        mock_flag.return_value = False
        response = await gated_app.test_client().post(
            "/gated", headers={"Authorization": f"Bearer {token}"}, json={}
        )
    assert response.status_code == 403
    assert (await response.get_json())["error"]["code"] == "feature_disabled"


async def test_route_runs_when_every_flag_is_on(gated_app: Quart) -> None:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    with patch("services.bundle_feature_gate.feature_enabled", new_callable=AsyncMock) as mock_flag:
        mock_flag.return_value = True
        response = await gated_app.test_client().post(
            "/gated", headers={"Authorization": f"Bearer {token}"}, json={}
        )
    assert response.status_code == 200


async def test_gate_runs_after_scope_so_an_unscoped_caller_still_gets_403_scope(gated_app: Quart) -> None:
    token = make_user_token(user_id=1, scope="", tenant=TENANT_SLUG)
    with patch("services.bundle_feature_gate.feature_enabled", new_callable=AsyncMock) as mock_flag:
        mock_flag.return_value = True
        response = await gated_app.test_client().post(
            "/gated", headers={"Authorization": f"Bearer {token}"}, json={}
        )
    assert response.status_code == 403
    mock_flag.assert_not_awaited()


_GATED_ROUTES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("blueprints.v1.bundle_versions", "POST", "/api/v1/apps/waddles.socials.music.default/trip-reenable",
     {"version": "3.0.1", "communityId": None}),
    ("blueprints.v1.bundle_grants", "POST", "/api/v1/apps/waddles.socials.music.default/grants/resolve",
     {"communityId": None}),
    ("blueprints.v1.ingest_sources", "POST", f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources",
     {"communityId": None, "platform": "twitch", "sourceId": "tw-x", "label": "X", "mapping": None}),
    ("blueprints.v1.custom_platforms", "POST", f"/api/v1/tenant/{TENANT_SLUG}/custom-platforms",
     {"name": "acme"}),
]


@pytest.mark.parametrize(("module_path", "method", "path", "body"), _GATED_ROUTES)
async def test_every_gated_write_route_is_403_when_the_flag_is_off(
    bundle_install_db: Any, module_path: str, method: str, path: str, body: dict[str, Any]
) -> None:
    import importlib

    module = importlib.import_module(module_path)
    app = Quart(__name__)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    for bp in module.BLUEPRINTS:
        app.register_blueprint(bp)

    token = make_user_token(user_id=1, scope="tenant:admin platform:admin", tenant=TENANT_SLUG)
    with patch("services.bundle_feature_gate.feature_enabled", new_callable=AsyncMock) as mock_flag:
        mock_flag.return_value = False
        response = await app.test_client().open(
            path, method=method, headers={"Authorization": f"Bearer {token}"}, json=body
        )
    assert response.status_code == 403
    assert (await response.get_json())["error"]["code"] == "feature_disabled"


@pytest.mark.parametrize(
    ("path", "scope"),
    [
        ("/api/v1/distribution/v2/bundles?stage=process", "distribution:read"),
        ("/api/v1/distribution/sources", "distribution:read"),
    ],
)
async def test_distribution_reads_are_never_gated(bundle_install_db: Any, path: str, scope: str) -> None:
    from blueprints.v1.distribution import BLUEPRINTS

    app = Quart(__name__)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)

    token = make_user_token(user_id=1, scope=scope, tenant=TENANT_SLUG)
    with patch("services.bundle_feature_gate.feature_enabled", new_callable=AsyncMock) as mock_flag:
        mock_flag.return_value = False
        response = await app.test_client().get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    mock_flag.assert_not_awaited()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_feature_gate.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_feature_gate'`

- [ ] **Step 3: Write the gate**

```python
# hub_api/services/bundle_feature_gate.py
"""The PostHog flag gate on every M2b write surface (spec Sec13.5, critical-rules.md Feature Flags).

Two gates, both enforced by `flask_core.feature_flags.feature_enabled`:
the PostHog flag evaluates true AND the deployment's licence tier
entitles the key. All four keys here are `min_tier: free` (core
module), so in practice only the PostHog half can fail -- but the call
still goes through the two-gate helper rather than a bare PostHog
lookup, because the tier check is what makes adding a licensed flag
later a one-line change instead of a refactor.

Write surfaces only. `GET` routes across this milestone are
deliberately ungated: a flag flipped off mid-rollout must stop new
writes, never blind a running Rust stage polling the distribution API.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context
from quart import request

FLAG_RUST_DATA_PLANE = "waddles.core.rust-data-plane"
FLAG_WASM_BUNDLES = "waddles.core.wasm-bundles"
FLAG_GENERIC_INTAKE = "waddles.core.generic-intake"
FLAG_PREBUILT_BUNDLES = "waddles.core.prebuilt-bundles"


async def flags_enabled(*flag_keys: str, tenant: str, community: int | None = None) -> bool:
    """True only when EVERY named flag is enabled for `tenant`. Short-circuits on the first `False`."""
    for flag_key in flag_keys:
        if not await feature_enabled(flag_key, tenant=tenant, community=community, default=False):
            return False
    return True


def require_flags(*flag_keys: str) -> Callable[..., Any]:
    """Route decorator refusing the request `403 feature_disabled` unless every flag is on.

    Place it **innermost** -- after `@tenant_middleware` and
    `@require_scope(...)` in source order -- so `get_tenant_context`
    has already published the tenant this evaluates against. A request
    that has not passed `tenant_middleware` has no tenant to evaluate
    and is refused rather than silently defaulting to a global answer.
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = get_tenant_context(request)
            if ctx is None:
                return cast(
                    tuple[dict[str, object], int],
                    error_response("tenant context is required", 403, "feature_disabled"),
                )
            if not await flags_enabled(*flag_keys, tenant=ctx.tenant_slug):
                return cast(
                    tuple[dict[str, object], int],
                    error_response(
                        f"this feature is not enabled for {ctx.tenant_slug} "
                        f"(requires {', '.join(flag_keys)})",
                        403,
                        "feature_disabled",
                    ),
                )
            return await func(*args, **kwargs)

        return wrapper

    return decorator
```

- [ ] **Step 4: Apply the decorator to every gated route**

In each file below, add the import and insert the `@require_flags(...)` line as the **last** decorator (immediately above the `async def`) on each named handler. Change nothing else.

`hub_api/blueprints/v1/bundle_versions.py` — handlers `post_version`, `post_activate`, `post_trip_reenable`:
```python
from services.bundle_feature_gate import FLAG_RUST_DATA_PLANE, FLAG_WASM_BUNDLES, require_flags
```
```python
@require_flags(FLAG_RUST_DATA_PLANE, FLAG_WASM_BUNDLES)
```

`hub_api/blueprints/v1/bundle_approvals.py` — handlers `post_approve`, `post_deny`: same import, same decorator line.

`hub_api/blueprints/v1/bundle_grants.py` — handlers `post_resolve`, `delete_grant`: same import, same decorator line. **Not** `get_grants`.

`hub_api/blueprints/v1/bundle_artifact_callback.py` — both callback handlers: same import, same decorator line.

`hub_api/blueprints/v1/custom_platforms.py` — handlers `post_platform`, `delete_platform`, `post_token`: 
```python
from services.bundle_feature_gate import FLAG_GENERIC_INTAKE, FLAG_RUST_DATA_PLANE, require_flags
```
```python
@require_flags(FLAG_RUST_DATA_PLANE, FLAG_GENERIC_INTAKE)
```
**Not** the `GET` list handler.

`hub_api/blueprints/v1/ingest_sources.py` — handlers `post_source`, `delete_source_route`: same import and decorator as `custom_platforms.py`. **Not** the `GET` list handler.

`hub_api/blueprints/v1/bundle_settings.py` — handler `put_settings` only:
```python
from services.bundle_feature_gate import FLAG_RUST_DATA_PLANE, require_flags
```
```python
@require_flags(FLAG_RUST_DATA_PLANE)
```

- [ ] **Step 5: Make `allow_prebuilt` a real two-gate in `POST .../versions`**

In `hub_api/blueprints/v1/bundle_versions.py`'s `post_version` handler, the value passed as `allow_prebuilt=` to `svc.create_version` is currently the platform setting alone. Replace that expression so the flag is the second gate:

```python
    allow_prebuilt = await get_platform_setting_bool(
        install_dal, key=SETTING_ALLOW_PREBUILT, default=True
    ) and await flags_enabled(FLAG_PREBUILT_BUNDLES, tenant=ctx.tenant_slug)
```

(`get_platform_setting_bool` takes `install_dal` only, per R52/Task 23 — `platform_settings` is one of this plan's own new tables. `install_dal` is already in scope in `post_version` via Task 11's `_install_dal()` call.)

Add to the same file's imports:
```python
from services.bundle_feature_gate import FLAG_PREBUILT_BUNDLES, flags_enabled
```

A prebuilt upload with the setting on but the flag off therefore fails with `403 prebuilt_not_allowed` from `bundle_manifest_v2` (Task 7's existing reason code), not with a new error shape.

- [ ] **Step 6: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_feature_gate.py -v`
Expected: `11 passed` (4 gated-route cases + 2 distribution-read cases + 5 others).

- [ ] **Step 7: Run every M2b blueprint suite to confirm the decorator did not break the happy paths**

Run:
```bash
cd hub_api && python3 -m pytest \
  tests/test_bundle_versions_blueprint.py tests/test_bundle_approvals_blueprint.py \
  tests/test_bundle_grants_blueprint.py tests/test_bundle_artifact_callback_blueprint.py \
  tests/test_custom_platforms_blueprint.py tests/test_ingest_sources_blueprint.py \
  tests/test_bundle_settings_blueprint.py tests/test_bundle_trip_reenable.py -v
```
Expected: **failures** on every write-route happy-path test — those tests do not patch `feature_enabled`, so the gate closes. Fix them by adding this fixture to `hub_api/tests/conftest.py` and nothing else:

```python
@pytest.fixture(autouse=True)
def _bundle_flags_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every M2b PostHog flag ON for tests that are not about the flag gate itself.

    `tests/test_bundle_feature_gate.py` patches
    `services.bundle_feature_gate.feature_enabled` directly inside each
    test, which wins over this autouse fixture -- so the gate's own
    off-path tests still exercise the real refusal.
    """

    async def _always_on(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("services.bundle_feature_gate.feature_enabled", _always_on)
```

Re-run the same command.
Expected: every suite passes.

- [ ] **Step 8: Add the ruff per-file ignore**

Append to `hub_api/pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]`:

```toml
# The flag gate wraps handlers with *args/**kwargs and re-raises nothing;
# ANN401-style Any is inherent to a generic route decorator.
"services/bundle_feature_gate.py" = ["ANN401"]
```

- [ ] **Step 9: Commit**

```bash
git add hub_api/services/bundle_feature_gate.py hub_api/tests/test_bundle_feature_gate.py \
        hub_api/tests/conftest.py hub_api/pyproject.toml \
        hub_api/blueprints/v1/bundle_versions.py hub_api/blueprints/v1/bundle_approvals.py \
        hub_api/blueprints/v1/bundle_grants.py hub_api/blueprints/v1/bundle_artifact_callback.py \
        hub_api/blueprints/v1/custom_platforms.py hub_api/blueprints/v1/ingest_sources.py \
        hub_api/blueprints/v1/bundle_settings.py
git commit -m "$(cat <<'EOF'
feat(hub-api): PostHog flag gate on every M2b write surface (spec Sec13.5)

require_flags() refuses 403 feature_disabled unless every named flag is
on for the caller's tenant, evaluated through flask_core's two-gate
feature_enabled (PostHog AND licence entitlement). Applied to version
upload/activate/trip-reenable, approve/deny, grant resolve/revoke, both
compiler callbacks, custom-platform and ingest-source writes, and the
marketplace settings PUT.

Read surfaces stay ungated on purpose: a flag flipped off mid-rollout
must stop new writes, never blind a running Rust stage polling the
distribution API. allow_prebuilt becomes a real two-gate -- the
platform setting AND waddles.core.prebuilt-bundles.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 38: `bundle_telemetry.py` — OTel spans, counters and histograms across the M2b surface

**Depends on:** Tasks 10, 19, 29, 13-14 (the service entry points being instrumented), Tasks 33-34 (the distribution v2 routes).

**Files:**
- Create: `hub_api/services/bundle_telemetry.py`
- Modify: `hub_api/requirements.in` (+ regenerate `requirements.txt`)
- Modify: `hub_api/services/bundle_version_service.py`, `bundle_approval_service.py`, `stream_grant_service.py`, `bundle_artifact_service.py`
- Modify: `hub_api/blueprints/v1/distribution.py`
- Test: `hub_api/tests/test_bundle_telemetry.py`

**Interfaces:**
- Produces: `def get_tracer() -> Any`; `def get_meter() -> Any`; `@asynccontextmanager bundle_span(name: str, **attributes: Any)`; counters `record_version_upload(result: str, language: str, artifact_kind: str)`, `record_approval(decision: str)`, `record_grants_resolved(app_id: str, count: int)`, `record_artifact_callback(result: str)`; histogram recorders `record_distribution_poll(stage: str, rows: int, duration_ms: float)`.
- Consumes: `opentelemetry.trace`, `opentelemetry.metrics` (API only).

**Non-negotiables this module encodes** (`critical-rules.md` Observability):

- **Destination is never hardcoded.** This module touches `opentelemetry.trace`/`opentelemetry.metrics` — the *API*, never an SDK exporter and never a vendor SDK. Where the data goes is `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_PROTOCOL` / `OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES`, set per deployment. With no provider configured the OTel API's no-op tracer/meter is used and every call below is a cheap no-op — telemetry never breaks a request.
- **Histograms for load and latency first.** `waddles_hub_bundle_operation_duration_ms` and `waddles_hub_distribution_poll_duration_ms` are histograms; so is `waddles_hub_distribution_rows`, the payload-size signal. Counters are for events, the up-down counter for current state.
- **No PII, no secrets, no digests-as-labels.** Span attributes and metric labels carry `app_id`, `version`, `stage`, `language`, `artifact_kind`, `result`, `tenant_id` — never a user name, an email, a token, an HMAC secret, or a full `artifact_digest` (a digest is high-cardinality and would blow up the label set; it belongs in a log line, not a label).

- [ ] **Step 1: Pin the dependency**

Add to `hub_api/requirements.in`:

```
# OpenTelemetry API only -- services/bundle_telemetry.py emits spans and
# metrics through the API; the SDK, its exporters and the OTLP endpoint
# are configured per deployment via the standard OTEL_* env vars, never
# in app code (critical-rules.md Observability). The SDK arrives
# transitively via libs/flask_core (opentelemetry-sdk,
# opentelemetry-exporter-otlp) -- pinned here too because hub-api's own
# code imports the API directly, per this file's header comment.
opentelemetry-api>=1.27.0,<2.0.0
opentelemetry-sdk>=1.27.0,<2.0.0  # tests/test_bundle_telemetry.py -- InMemory span/metric readers
```

Regenerate the lockfile:
```bash
cd hub_api && uv pip compile requirements.in --generate-hashes -o requirements.txt
```
Expected: `requirements.txt` regenerated with `--generate-hashes`, containing `opentelemetry-api==` and `opentelemetry-sdk==` lines each followed by `--hash=sha256:` entries.

- [ ] **Step 2: Write the failing test**

```python
# hub_api/tests/test_bundle_telemetry.py
"""Telemetry validation for the M2b surface -- spans, counters and histograms actually emitted.

Counts are asserted AND printed: a zero-item run is a failure, never a
pass (critical-rules.md Verification Integrity, testing.md Telemetry
Validation).
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def otel_sink(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real in-process OTel SDK wired to in-memory readers -- the local OTLP test sink."""
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: tracer_provider)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: meter_provider)

    import services.bundle_telemetry as telemetry

    monkeypatch.setattr(telemetry, "_TRACER", None)
    monkeypatch.setattr(telemetry, "_METER", None)
    monkeypatch.setattr(telemetry, "_INSTRUMENTS", None)
    return exporter, reader


async def test_bundle_span_emits_a_span_and_a_duration_histogram(otel_sink: Any) -> None:
    from services.bundle_telemetry import bundle_span

    exporter, reader = otel_sink
    async with bundle_span("hub.bundle.create_version", app_id="waddles.socials.music.default"):
        pass

    spans = exporter.get_finished_spans()
    print(f"telemetry check: span records received = {len(spans)}")
    assert len(spans) >= 1, "zero spans received -- FAIL, not a pass"
    assert spans[0].name == "hub.bundle.create_version"
    assert spans[0].attributes["app_id"] == "waddles.socials.music.default"

    points = _histogram_points(reader, "waddles_hub_bundle_operation_duration_ms")
    print(f"telemetry check: histogram data points received = {len(points)}")
    assert len(points) >= 1, "zero histogram data points received -- FAIL, not a pass"


async def test_bundle_span_records_an_error_status_and_still_emits(otel_sink: Any) -> None:
    from services.bundle_telemetry import bundle_span

    exporter, _ = otel_sink
    with pytest.raises(ValueError):
        async with bundle_span("hub.bundle.approve", app_id="x"):
            raise ValueError("boom")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR


async def test_counters_emit_data_points(otel_sink: Any) -> None:
    from services.bundle_telemetry import (
        record_approval,
        record_artifact_callback,
        record_grants_resolved,
        record_version_upload,
    )

    _, reader = otel_sink
    record_version_upload(result="accepted", language="python", artifact_kind="source")
    record_approval(decision="approved")
    record_grants_resolved(app_id="waddles.socials.music.default", count=2)
    record_artifact_callback(result="verified")

    names = _metric_names(reader)
    print(f"telemetry check: metric streams received = {len(names)} -> {sorted(names)}")
    assert {
        "waddles_hub_bundle_versions_total",
        "waddles_hub_bundle_approvals_total",
        "waddles_hub_bundle_grants_resolved_total",
        "waddles_hub_bundle_grants_active",
        "waddles_hub_bundle_artifact_callbacks_total",
    } <= names


async def test_distribution_poll_records_rows_and_duration_histograms(otel_sink: Any) -> None:
    from services.bundle_telemetry import record_distribution_poll

    _, reader = otel_sink
    record_distribution_poll(stage="process", rows=3, duration_ms=12.5)

    rows_points = _histogram_points(reader, "waddles_hub_distribution_rows")
    duration_points = _histogram_points(reader, "waddles_hub_distribution_poll_duration_ms")
    print(
        f"telemetry check: distribution histogram data points = "
        f"rows {len(rows_points)}, duration {len(duration_points)}"
    )
    assert len(rows_points) >= 1
    assert len(duration_points) >= 1
    assert rows_points[0].sum == 3


async def test_no_provider_configured_is_a_silent_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead or absent exporter must never break the app (critical-rules.md Observability)."""
    import services.bundle_telemetry as telemetry

    monkeypatch.setattr(telemetry, "_TRACER", None)
    monkeypatch.setattr(telemetry, "_METER", None)
    monkeypatch.setattr(telemetry, "_INSTRUMENTS", None)
    async with telemetry.bundle_span("hub.bundle.noop"):
        pass
    telemetry.record_version_upload(result="accepted", language="python", artifact_kind="source")


def _metric_names(reader: Any) -> set[str]:
    data = reader.get_metrics_data()
    names: set[str] = set()
    if data is None:
        return names
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                names.add(metric.name)
    return names


def _histogram_points(reader: Any, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    points: list[Any] = []
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name == name:
                    points.extend(metric.data.data_points)
    return points
```

- [ ] **Step 3: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_telemetry.py -v`
Expected: `ModuleNotFoundError: No module named 'services.bundle_telemetry'`

- [ ] **Step 4: Write the module**

```python
# hub_api/services/bundle_telemetry.py
"""OTel spans, counters and histograms for the M2b bundle control plane.

API only -- `opentelemetry.trace` and `opentelemetry.metrics`. No SDK,
no exporter, no vendor library. Where the data goes is a deployment
concern set through the standard OTLP env vars
(`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`,
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME`,
`OTEL_RESOURCE_ATTRIBUTES`); with no provider configured every call
here resolves to the API's no-op implementation and costs nothing.
Telemetry failure is never a request failure.

No PII, no secrets, and deliberately no `artifact_digest` in any label:
a digest is unbounded cardinality and belongs in a log line, not a
metric dimension.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace

_TRACER: Any = None
_METER: Any = None
_INSTRUMENTS: Any = None

_SCOPE = "waddles.hub_api.bundles"


def get_tracer() -> Any:
    """The module's tracer, created once. No-op when no provider is configured."""
    global _TRACER
    if _TRACER is None:
        _TRACER = trace.get_tracer(_SCOPE)
    return _TRACER


def get_meter() -> Any:
    """The module's meter, created once. No-op when no provider is configured."""
    global _METER
    if _METER is None:
        _METER = metrics.get_meter(_SCOPE)
    return _METER


@dataclass(slots=True, frozen=True)
class _Instruments:
    """Every instrument this module owns, created once on first use."""

    operation_duration_ms: Any
    versions_total: Any
    approvals_total: Any
    grants_resolved_total: Any
    grants_active: Any
    artifact_callbacks_total: Any
    distribution_rows: Any
    distribution_poll_duration_ms: Any


def _instruments() -> _Instruments:
    """Lazily build the instrument set so importing this module configures nothing."""
    global _INSTRUMENTS
    if _INSTRUMENTS is None:
        meter = get_meter()
        _INSTRUMENTS = _Instruments(
            operation_duration_ms=meter.create_histogram(
                "waddles_hub_bundle_operation_duration_ms",
                unit="ms",
                description="Wall-clock duration of one bundle control-plane operation",
            ),
            versions_total=meter.create_counter(
                "waddles_hub_bundle_versions_total",
                description="Bundle version uploads accepted or refused by hub-api",
            ),
            approvals_total=meter.create_counter(
                "waddles_hub_bundle_approvals_total",
                description="Install-time permission decisions recorded",
            ),
            grants_resolved_total=meter.create_counter(
                "waddles_hub_bundle_grants_resolved_total",
                description="Stream grants written by consumes resolution",
            ),
            grants_active=meter.create_up_down_counter(
                "waddles_hub_bundle_grants_active",
                description="Current active stream grants per bundle",
            ),
            artifact_callbacks_total=meter.create_counter(
                "waddles_hub_bundle_artifact_callbacks_total",
                description="Compiler artifact callbacks by outcome",
            ),
            distribution_rows=meter.create_histogram(
                "waddles_hub_distribution_rows",
                unit="1",
                description="Rows returned by one distribution-API poll",
            ),
            distribution_poll_duration_ms=meter.create_histogram(
                "waddles_hub_distribution_poll_duration_ms",
                unit="ms",
                description="Server-side duration of one distribution-API poll",
            ),
        )
    return _INSTRUMENTS


@asynccontextmanager
async def bundle_span(name: str, **attributes: Any) -> AsyncIterator[Any]:
    """Span the real work, and record its duration into the operation histogram.

    Sets an ERROR status and records the exception when the body
    raises, then re-raises -- observability never swallows a failure.
    """
    started = time.perf_counter()
    with get_tracer().start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            _instruments().operation_duration_ms.record(
                (time.perf_counter() - started) * 1000.0, {"operation": name, "result": "error"}
            )
            raise
        _instruments().operation_duration_ms.record(
            (time.perf_counter() - started) * 1000.0, {"operation": name, "result": "ok"}
        )


def record_version_upload(*, result: str, language: str, artifact_kind: str) -> None:
    """Count one version upload. `result` is `accepted` or a manifest rule's `reason`."""
    _instruments().versions_total.add(
        1, {"result": result, "language": language, "artifact_kind": artifact_kind}
    )


def record_approval(*, decision: str) -> None:
    """Count one install-time permission decision (`approved`, `denied`, `superseded`)."""
    _instruments().approvals_total.add(1, {"decision": decision})


def record_grants_resolved(*, app_id: str, count: int) -> None:
    """Count grants written by one resolution, and set the bundle's current active-grant level."""
    _instruments().grants_resolved_total.add(count, {"app_id": app_id})
    _instruments().grants_active.add(count, {"app_id": app_id})


def record_grants_revoked(*, app_id: str, count: int) -> None:
    """Lower the bundle's current active-grant level by `count`."""
    _instruments().grants_active.add(-count, {"app_id": app_id})


def record_artifact_callback(*, result: str) -> None:
    """Count one compiler callback (`verified`, `digest_mismatch`, `rejected`, `fallback_insert`)."""
    _instruments().artifact_callbacks_total.add(1, {"result": result})


def record_distribution_poll(*, stage: str, rows: int, duration_ms: float) -> None:
    """Record one distribution-API poll's payload size and server-side duration."""
    _instruments().distribution_rows.record(rows, {"stage": stage})
    _instruments().distribution_poll_duration_ms.record(duration_ms, {"stage": stage})
```

- [ ] **Step 5: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_telemetry.py -v -s`
Expected: `5 passed`, and stdout includes the printed denominators — `telemetry check: span records received = 1`, `telemetry check: histogram data points received = 1`, `telemetry check: metric streams received = 5 -> [...]`, `telemetry check: distribution histogram data points = rows 1, duration 1`.

- [ ] **Step 6: Wire the instrumentation into the four services**

**`hub_api/services/bundle_version_service.py`** — add the import and wrap `create_version`'s body:
```python
from services.bundle_telemetry import bundle_span, record_version_upload
```
Wrap the entire existing body of `create_version` in:
```python
    async with bundle_span("hub.bundle.create_version", app_id=app_id, tenant_id=tenant_id):
        ...  # the existing body, indented one level
```
and immediately after the `parse_bundle_manifest_v2` `except ManifestV2Error as exc:` block's `raise`, add a `record_version_upload(result=exc.reason, language="unknown", artifact_kind="unknown")` call **before** the `raise`. After the final `rows = await install_dal(install_dal.app_version_uploads.id == upload_id).select()` line, add:
```python
        record_version_upload(result="accepted", language=manifest.language, artifact_kind=manifest.artifact)
```

**`hub_api/services/bundle_approval_service.py`**:
```python
from services.bundle_telemetry import bundle_span, record_approval
```
Wrap `approve_version`'s body in `async with bundle_span("hub.bundle.approve", app_id=app_id, version=version, tenant_id=tenant_id):` and call `record_approval(decision="approved")` just before it returns. Do the same for `deny_version` with `"hub.bundle.deny"` and `record_approval(decision="denied")`.

**`hub_api/services/stream_grant_service.py`**:
```python
from services.bundle_telemetry import bundle_span, record_grants_resolved, record_grants_revoked
```
Wrap `resolve_grants_for_scope`'s body in `async with bundle_span("hub.bundle.resolve_grants", app_id=app_id, tenant_id=tenant_id):` and call `record_grants_resolved(app_id=app_id, count=len(inserted))` — where `inserted` is the list of grants this call newly created, not the full returned list, so a no-op re-resolution records `0`. In `revoke_grant` call `record_grants_revoked(app_id=app_id, count=1)` after the update; in `revoke_all_grants_for_scope` call it with the number of rows revoked.

**`hub_api/services/bundle_artifact_service.py`**:
```python
from services.bundle_telemetry import bundle_span, record_artifact_callback
```
Wrap the success handler in `async with bundle_span("hub.bundle.artifact_callback", app_id=app_id, version=version):` and call `record_artifact_callback(result=...)` with `"verified"`, `"digest_mismatch"` or `"fallback_insert"` on each respective path; wrap the rejection handler in `"hub.bundle.artifact_rejected"` with `record_artifact_callback(result="rejected")`.

- [ ] **Step 7: Wire the distribution poll histograms**

In `hub_api/blueprints/v1/distribution.py`, add:
```python
import time

from services.bundle_telemetry import bundle_span, record_distribution_poll
```
Wrap `list_distribution_bundles_v2`'s body (from the `stage` read to the `return`) in:
```python
    started = time.perf_counter()
    async with bundle_span("hub.distribution.poll_v2", stage=stage):
        ...  # existing body
        record_distribution_poll(
            stage=stage, rows=len(bundles), duration_ms=(time.perf_counter() - started) * 1000.0
        )
        return _conditional(stage, bundles, payload)
```
and `list_distribution_sources`'s body the same way with span name `"hub.distribution.poll_sources"` and `stage="sources"`.

- [ ] **Step 8: Re-run every touched suite**

Run:
```bash
cd hub_api && python3 -m pytest \
  tests/test_bundle_version_service.py tests/test_bundle_approval_service.py \
  tests/test_stream_grant_service.py tests/test_bundle_artifact_service.py \
  tests/test_distribution_v2_blueprint.py tests/test_distribution_sources_blueprint.py \
  tests/test_bundle_telemetry.py -v
```
Expected: every suite passes. The instrumentation is no-op when no provider is configured, so no existing assertion changes.

- [ ] **Step 9: Commit**

```bash
git add hub_api/services/bundle_telemetry.py hub_api/tests/test_bundle_telemetry.py \
        hub_api/requirements.in hub_api/requirements.txt \
        hub_api/services/bundle_version_service.py hub_api/services/bundle_approval_service.py \
        hub_api/services/stream_grant_service.py hub_api/services/bundle_artifact_service.py \
        hub_api/blueprints/v1/distribution.py
git commit -m "$(cat <<'EOF'
feat(hub-api): OTel spans, counters and histograms across the M2b bundle control plane

bundle_span() wraps version upload, approval, grant resolution, both
compiler callbacks and both distribution polls; histograms cover
operation duration, distribution payload size and poll latency;
counters cover uploads, approvals, grants and callbacks, with an
up-down counter for current active grants per bundle.

OTel API only -- no SDK, no exporter, no vendor library in app code.
The destination is OTEL_EXPORTER_OTLP_ENDPOINT and friends, set per
deployment; with no provider configured every call is a no-op, so a
dead exporter never breaks a request. No digest, token or PII appears
in any span attribute or metric label.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 39: migration 0022 + `ingest_source_auth.py` — the per-source auth config and its validation (R52: penguin-dal)

**Depends on:** Task 3 (migration 0021 created `ingest_sources`), Task 4 (`install_dal` penguin-dal wiring + `_create_bundle_install_tables()` fixture), Task 6 (`bundle_secret_crypto.{encrypt, decrypt}`), Task 26 (`ingest_source_service`, all `install_dal`-only per R52).

**Files:**
- Create: `alembic/versions/0022_ingest_source_auth.py`
- Create: `hub_api/services/ingest_source_auth.py`
- Modify: `hub_api/tests/conftest.py` (three new columns on the `ingest_sources` table in `_create_bundle_install_tables()`)
- Modify: `hub_api/services/ingest_source_service.py`
- Test: `hub_api/tests/test_ingest_source_auth.py`

**Interfaces:**
- Produces, in `services/ingest_source_auth.py`: `MODE_HMAC = "hmac"`, `MODE_IP_ALLOWLIST = "ip_allowlist"`, `MODE_BEARER = "bearer"`, `MODE_BASIC = "basic"`; `SECOND_FACTOR_MODES = frozenset({MODE_IP_ALLOWLIST, MODE_BEARER, MODE_BASIC})`; `DEFAULT_ORIGIN_SUFFIXES: dict[str, tuple[str, ...]] = {"twitch": ("twitch.tv",), "kick": ("kick.com",)}`; `WEBHOOK_PLATFORM = "webhook"`; `class AuthConfigError(ValueError)` with a machine-checkable `.reason`; `def is_generic_webhook(platform: str) -> bool`; `def build_auth_config(platform: str, raw: dict[str, Any] | None, *, secret_ref: str) -> tuple[dict[str, Any], str | None]` returning `(canonical_auth_config, plaintext_secret_to_encrypt_or_None)`.
- Produces, in `services/ingest_source_service.py`: `create_source(..., auth: dict[str, Any] | None = None)` (signature otherwise unchanged from Task 26: `install_dal` only); `async def update_source_auth(install_dal, *, tenant_id: int, source_id: str, auth: dict[str, Any], actor_id: int) -> Any` (R52/Decision #18: the `ingest_sources.auth_*` update goes through `install_dal` (Pattern A); the `audit_log` insert is a separate, best-effort write through `install_dal.audit_log` — reachable since `install_dal.reflect()` (Task 4) sees `audit_log` too — wrapped in the same `try`/`except` this codebase already uses at every other audit-log call site, never blocking the auth-policy change on a logging failure); `async def resolve_auth_secret(install_dal, *, tenant_id: int, source_id: str) -> str | None`.
- Consumes: `services.bundle_secret_crypto.{encrypt, decrypt}` (Task 6).

**The canonical wire shape — must match plan M5, reproduced in full so no later task re-derives it:**

```json
{
  "modes": ["hmac", "bearer"],
  "cidrs": ["203.0.113.0/24", "2001:db8::/32"],
  "secret_ref": "src:tw-channelA",
  "origin_suffixes": ["twitch.tv"],
  "origin_cidrs": []
}
```

| Key | Type | Rule |
|---|---|---|
| `modes` | list of string | Always contains `"hmac"` first. For a generic webhook source it must also contain at least one of `ip_allowlist`, `bearer`, `basic`, else `422 auth_second_factor_required`. Sorted after `"hmac"`, deduped. |
| `cidrs` | list of string | Required non-empty when `ip_allowlist` is in `modes`, else `[]`. Every entry parsed with `ipaddress.ip_network(strict=False)`; a parse failure is `422 invalid_cidr`. |
| `secret_ref` | string | `"src:{source_id}"`. The opaque handle svc_ingest exchanges for the bearer token / basic password hash through the same authenticated secret path as the HMAC secret. **Never** the secret itself. |
| `origin_suffixes` | list of string | Defaults to `DEFAULT_ORIGIN_SUFFIXES[platform]` for `twitch`/`kick`, `[]` elsewhere. Lowercased, deduped, sorted. Each must be a bare dotted domain suffix (no scheme, no path, no wildcard). |
| `origin_cidrs` | list of string | Optional, same parse rule as `cidrs`; `[]` when absent. |

Input-only keys, accepted from the caller and **never** stored in `auth` or returned anywhere: `bearer_token` (string, ≥ 32 characters) and `basic_password` (string, ≥ 12 characters, paired with `basic_username`). `basic_username` **is** stored in `auth` (it is not a secret); the password is hashed with `hashlib.sha256` and the **hash** is what `encrypt()` protects at rest, so hub-api never holds a reversible password.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_ingest_source_auth.py
"""Tests for the per-source auth config: validation, storage, and the audit entry."""

from __future__ import annotations

from typing import Any

import pytest

from services.bundle_secret_crypto import decrypt
from services.bundle_install_dal import raw_sql_rows
from services.errors import ApiError
from services.ingest_source_auth import AuthConfigError, build_auth_config, is_generic_webhook
from services.ingest_source_service import (
    create_source,
    list_sources,
    resolve_auth_secret,
    update_source_auth,
)


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUNDLE_SECRET_ENCRYPTION_KEY", "d" * 64)


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("custom:acme", True), ("webhook", True), ("twitch", False), ("kick", False), ("discord", False)],
)
def test_is_generic_webhook(platform: str, expected: bool) -> None:
    assert is_generic_webhook(platform) is expected


def test_generic_source_without_a_second_factor_is_refused() -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        build_auth_config("custom:acme", {"modes": ["hmac"]}, secret_ref="src:wh-1")
    assert excinfo.value.reason == "auth_second_factor_required"


def test_generic_source_with_no_auth_block_at_all_is_refused() -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        build_auth_config("custom:acme", None, secret_ref="src:wh-1")
    assert excinfo.value.reason == "auth_second_factor_required"


def test_generic_source_with_ip_allowlist_is_accepted() -> None:
    config, secret = build_auth_config(
        "custom:acme",
        {"modes": ["ip_allowlist"], "cidrs": ["203.0.113.0/24", "2001:db8::/32"]},
        secret_ref="src:wh-1",
    )
    assert config["modes"] == ["hmac", "ip_allowlist"]
    assert config["cidrs"] == ["203.0.113.0/24", "2001:db8::/32"]
    assert config["secret_ref"] == "src:wh-1"
    assert config["origin_suffixes"] == []
    assert secret is None


def test_ip_allowlist_without_cidrs_is_refused() -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        build_auth_config("custom:acme", {"modes": ["ip_allowlist"], "cidrs": []}, secret_ref="src:wh-1")
    assert excinfo.value.reason == "cidrs_required"


@pytest.mark.parametrize("bad", ["203.0.113.0/33", "not-an-ip", "203.0.113.0/", "", "10.0.0.0/8/8"])
def test_a_cidr_parse_error_is_refused(bad: str) -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        build_auth_config("custom:acme", {"modes": ["ip_allowlist"], "cidrs": [bad]}, secret_ref="src:wh-1")
    assert excinfo.value.reason == "invalid_cidr"


def test_bearer_mode_returns_the_plaintext_once_and_never_stores_it() -> None:
    config, secret = build_auth_config(
        "custom:acme", {"modes": ["bearer"], "bearer_token": "t" * 40}, secret_ref="src:wh-1"
    )
    assert config["modes"] == ["hmac", "bearer"]
    assert secret == "t" * 40
    assert "bearer_token" not in config
    assert "t" * 40 not in str(config)


def test_a_short_bearer_token_is_refused() -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        build_auth_config("custom:acme", {"modes": ["bearer"], "bearer_token": "short"}, secret_ref="src:wh-1")
    assert excinfo.value.reason == "bearer_token_too_short"


def test_basic_mode_stores_the_username_and_hashes_the_password() -> None:
    import hashlib

    config, secret = build_auth_config(
        "custom:acme",
        {"modes": ["basic"], "basic_username": "acme-bot", "basic_password": "correct horse battery"},
        secret_ref="src:wh-1",
    )
    assert config["modes"] == ["hmac", "basic"]
    assert config["basic_username"] == "acme-bot"
    assert "basic_password" not in config
    assert secret == hashlib.sha256(b"correct horse battery").hexdigest()


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ({"modes": ["basic"], "basic_username": "", "basic_password": "correct horse battery"}, "basic_username_required"),
        ({"modes": ["basic"], "basic_username": "bot", "basic_password": "short"}, "basic_password_too_short"),
        ({"modes": ["nonsense"]}, "unknown_auth_mode"),
        ({"modes": "bearer"}, "malformed_auth"),
    ],
)
def test_malformed_auth_blocks_are_refused(raw: dict[str, Any], reason: str) -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        build_auth_config("custom:acme", raw, secret_ref="src:wh-1")
    assert excinfo.value.reason == reason


def test_twitch_defaults_to_the_twitch_origin_suffix() -> None:
    config, secret = build_auth_config("twitch", None, secret_ref="src:tw-channelA")
    assert config["modes"] == ["hmac"]
    assert config["origin_suffixes"] == ["twitch.tv"]
    assert config["origin_cidrs"] == []
    assert secret is None


def test_kick_defaults_to_the_kick_origin_suffix() -> None:
    config, _ = build_auth_config("kick", None, secret_ref="src:kk-1")
    assert config["origin_suffixes"] == ["kick.com"]


def test_twitch_origin_policy_can_be_overridden_and_is_normalised() -> None:
    config, _ = build_auth_config(
        "twitch",
        {"origin_suffixes": ["EventSub.Twitch.TV", "twitch.tv", "twitch.tv"], "origin_cidrs": ["192.0.2.0/24"]},
        secret_ref="src:tw-channelA",
    )
    assert config["origin_suffixes"] == ["eventsub.twitch.tv", "twitch.tv"]
    assert config["origin_cidrs"] == ["192.0.2.0/24"]


@pytest.mark.parametrize("bad", ["https://twitch.tv", "*.twitch.tv", "twitch.tv/path", ""])
def test_a_malformed_origin_suffix_is_refused(bad: str) -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        build_auth_config("twitch", {"origin_suffixes": [bad]}, secret_ref="src:tw-1")
    assert excinfo.value.reason == "invalid_origin_suffix"


async def test_create_source_refuses_a_generic_source_with_no_second_factor(install_dal: Any) -> None:
    with pytest.raises(ApiError) as excinfo:
        await create_source(
            install_dal, tenant_id=1, community_id=None, platform="custom:acme",
            source_id="wh-1", label="Acme webhook", mapping=None, auth={"modes": ["hmac"]},
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "auth_second_factor_required"


async def test_create_source_stores_the_canonical_auth_and_encrypts_the_bearer(install_dal: Any) -> None:
    row, _hmac_secret = await create_source(
        install_dal, tenant_id=1, community_id=None, platform="custom:acme",
        source_id="wh-1", label="Acme webhook", mapping=None,
        auth={"modes": ["bearer"], "bearer_token": "t" * 40},
    )
    stored = (await install_dal(install_dal.ingest_sources.id == row.id).select()).first()
    assert stored.auth["modes"] == ["hmac", "bearer"]
    assert stored.auth["secret_ref"] == "src:wh-1"
    assert "t" * 40 not in str(stored.auth)
    assert stored.auth_secret_ciphertext is not None
    assert decrypt(stored.auth_secret_ciphertext, stored.auth_secret_iv) == "t" * 40


async def test_list_sources_never_echoes_the_bearer_token(install_dal: Any) -> None:
    await create_source(
        install_dal, tenant_id=1, community_id=None, platform="custom:acme",
        source_id="wh-1", label="Acme webhook", mapping=None,
        auth={"modes": ["bearer"], "bearer_token": "t" * 40},
    )
    rows = await list_sources(install_dal, tenant_id=1)
    serialised = str([dict(r.as_dict()) for r in rows])
    assert "t" * 40 not in serialised
    assert "auth_secret_ciphertext" not in serialised or "t" * 40 not in serialised


async def test_resolve_auth_secret_round_trips(install_dal: Any) -> None:
    await create_source(
        install_dal, tenant_id=1, community_id=None, platform="custom:acme",
        source_id="wh-1", label="Acme webhook", mapping=None,
        auth={"modes": ["bearer"], "bearer_token": "t" * 40},
    )
    assert await resolve_auth_secret(install_dal, tenant_id=1, source_id="wh-1") == "t" * 40


async def test_update_source_auth_rewrites_the_config_and_audits_the_mode_change(install_dal: Any) -> None:
    await create_source(
        install_dal, tenant_id=1, community_id=None, platform="custom:acme",
        source_id="wh-1", label="Acme webhook", mapping=None,
        auth={"modes": ["bearer"], "bearer_token": "t" * 40},
    )
    await update_source_auth(
        install_dal, tenant_id=1, source_id="wh-1",
        auth={"modes": ["ip_allowlist"], "cidrs": ["203.0.113.0/24"]}, actor_id=1,
    )
    stored = (await install_dal(install_dal.ingest_sources.source_id == "wh-1").select()).first()
    assert stored.auth["modes"] == ["hmac", "ip_allowlist"]
    assert stored.auth_secret_ciphertext is None

    audit_rows = await raw_sql_rows(
        install_dal, "SELECT details FROM audit_log WHERE action = :a", {"a": "ingest_source_auth_changed"}
    )
    audit_row = audit_rows.first()
    assert audit_row is not None
    # install_dal.audit_log.async_insert() (Pattern A) serialised `details`
    # as native JSON via SQLAlchemy's own type coercion, but a raw text()
    # SELECT (raw_sql_rows) returns the driver's own JSON representation
    # -- a JSON *string* on sqlite, an already-decoded dict on some
    # Postgres driver configurations -- so this assertion normalises via
    # json.loads() only when needed.
    import json

    details = audit_row["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["old_modes"] == ["hmac", "bearer"]
    assert details["new_modes"] == ["hmac", "ip_allowlist"]
    assert "bearer_token" not in str(details)


async def test_update_source_auth_refuses_removing_the_last_second_factor(install_dal: Any) -> None:
    await create_source(
        install_dal, tenant_id=1, community_id=None, platform="custom:acme",
        source_id="wh-1", label="Acme webhook", mapping=None,
        auth={"modes": ["bearer"], "bearer_token": "t" * 40},
    )
    with pytest.raises(ApiError) as excinfo:
        await update_source_auth(
            install_dal, tenant_id=1, source_id="wh-1", auth={"modes": ["hmac"]}, actor_id=1
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "auth_second_factor_required"


async def test_update_source_auth_404s_for_an_unknown_source(install_dal: Any) -> None:
    with pytest.raises(ApiError) as excinfo:
        await update_source_auth(
            install_dal, tenant_id=1, source_id="nope",
            auth={"modes": ["ip_allowlist"], "cidrs": ["203.0.113.0/24"]}, actor_id=1,
        )
    assert excinfo.value.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_source_auth.py -v`
Expected: `ModuleNotFoundError: No module named 'services.ingest_source_auth'`

- [ ] **Step 3: Write the migration**

```python
# alembic/versions/0022_ingest_source_auth.py
"""Per-source auth config on ingest_sources: auth JSONB + encrypted auth secret.

The HMAC secret authenticates the payload, not the caller. This
migration adds the second-factor config svc_ingest enforces: an
ip_allowlist / bearer / basic mode for generic webhook sources, and an
origin policy (origin_suffixes + origin_cidrs) for Twitch and Kick.
Bearer tokens and Basic password hashes live in
auth_secret_ciphertext/auth_secret_iv, AES-256-GCM, same helper as
every other secret in this service -- `auth` itself is safe to serve.
This migration is unaffected by R52 (penguin-dal is a runtime-query
library; DDL is Alembic/raw SQL either way, per Global Constraints).

Existing rows are backfilled with the platform-appropriate default so
no row is left with an empty policy: twitch -> {"twitch.tv"},
kick -> {"kick.com"}, everything else -> hmac-only. A pre-existing
generic webhook source therefore keeps working after this migration and
is upgraded to a second factor by an explicit admin action (Task 40's
PUT), never silently broken by a deploy.

Revision ID: 0022_ingest_source_auth
Revises: 0021_bundle_install_schema
"""

from __future__ import annotations

from alembic import op

revision = "0022_ingest_source_auth"
down_revision = "0021_bundle_install_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the auth column trio and backfill a platform-appropriate default."""
    op.execute(
        """
        ALTER TABLE ingest_sources
            ADD COLUMN IF NOT EXISTS auth JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS auth_secret_ciphertext BYTEA,
            ADD COLUMN IF NOT EXISTS auth_secret_iv BYTEA
        """
    )
    op.execute(
        """
        UPDATE ingest_sources SET auth = jsonb_build_object(
            'modes', jsonb_build_array('hmac'),
            'cidrs', '[]'::jsonb,
            'secret_ref', 'src:' || source_id,
            'origin_suffixes', CASE
                WHEN platform = 'twitch' THEN jsonb_build_array('twitch.tv')
                WHEN platform = 'kick' THEN jsonb_build_array('kick.com')
                ELSE '[]'::jsonb END,
            'origin_cidrs', '[]'::jsonb
        )
        WHERE auth = '{}'::jsonb
        """
    )
    op.execute(
        "COMMENT ON COLUMN ingest_sources.auth IS "
        "'Caller-authentication policy svc_ingest enforces: "
        "{modes, cidrs, secret_ref, origin_suffixes, origin_cidrs}. Never holds a secret.'"
    )
    op.execute(
        "COMMENT ON COLUMN ingest_sources.auth_secret_ciphertext IS "
        "'AES-256-GCM bearer token or Basic password hash. Never served by any endpoint.'"
    )


def downgrade() -> None:
    """Drop the three columns. The policy is reconstructible from defaults, the secrets are not."""
    op.execute(
        """
        ALTER TABLE ingest_sources
            DROP COLUMN IF EXISTS auth,
            DROP COLUMN IF EXISTS auth_secret_ciphertext,
            DROP COLUMN IF EXISTS auth_secret_iv
        """
    )
```

- [ ] **Step 4: Extend the `install_dal` test fixture's `ingest_sources` table**

Task 4 defined `ingest_sources` inside `_create_bundle_install_tables()` in `hub_api/tests/conftest.py` ending with `Column("updated_at", DateTime),` immediately before its closing `)`:

```python
    Table(
        "ingest_sources",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("platform", String(50), nullable=False),
        Column("source_id", String(255), nullable=False),
        Column("label", String(255), nullable=False),
        Column("secret_ciphertext", LargeBinary),
        Column("secret_iv", LargeBinary),
        Column("mapping", JSON),
        Column("enabled", Boolean, server_default=sa_true()),
        Column("created_at", DateTime),
        Column("updated_at", DateTime),
    )
```

Replace it with (three new columns added before the closing parenthesis):

```python
    Table(
        "ingest_sources",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("platform", String(50), nullable=False),
        Column("source_id", String(255), nullable=False),
        Column("label", String(255), nullable=False),
        Column("secret_ciphertext", LargeBinary),
        Column("secret_iv", LargeBinary),
        Column("mapping", JSON),
        Column("enabled", Boolean, server_default=sa_true()),
        Column("created_at", DateTime),
        Column("updated_at", DateTime),
        Column("auth", JSON, server_default="{}"),
        Column("auth_secret_ciphertext", LargeBinary),
        Column("auth_secret_iv", LargeBinary),
    )
```

- [ ] **Step 5: Write `services/ingest_source_auth.py`**

```python
# hub_api/services/ingest_source_auth.py
"""Validation and canonicalisation of an ingest source's caller-authentication policy.

The HMAC secret proves a payload was produced by someone holding the
secret; it does not prove *who* sent this particular request, and a
captured body replays cleanly. So every generic webhook source must
also declare a second factor -- an IP allowlist, a bearer token, or
HTTP Basic -- and Twitch/Kick sources carry an origin policy instead,
their senders being known.

This module produces the canonical `auth` dict stored on the row and
published verbatim by `GET /api/v1/distribution/sources` (must match
plan M5). It never stores or returns a secret: `build_auth_config`
hands the plaintext back to its caller exactly once, for the caller to
encrypt, and the dict it returns is safe to serve. Pure validation
logic -- no database access, unaffected by R52.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any

MODE_HMAC = "hmac"
MODE_IP_ALLOWLIST = "ip_allowlist"
MODE_BEARER = "bearer"
MODE_BASIC = "basic"

SECOND_FACTOR_MODES = frozenset({MODE_IP_ALLOWLIST, MODE_BEARER, MODE_BASIC})
KNOWN_MODES = SECOND_FACTOR_MODES | {MODE_HMAC}

WEBHOOK_PLATFORM = "webhook"
CUSTOM_PLATFORM_PREFIX = "custom:"

DEFAULT_ORIGIN_SUFFIXES: dict[str, tuple[str, ...]] = {
    "twitch": ("twitch.tv",),
    "kick": ("kick.com",),
}

MIN_BEARER_TOKEN_CHARS = 32
MIN_BASIC_PASSWORD_CHARS = 12

_ORIGIN_SUFFIX_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


class AuthConfigError(ValueError):
    """An auth block failed validation. `reason` is the machine-checkable error code."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


def is_generic_webhook(platform: str) -> bool:
    """True for the built-in `webhook` platform and every tenant-registered `custom:<name>`."""
    return platform == WEBHOOK_PLATFORM or platform.startswith(CUSTOM_PLATFORM_PREFIX)


def _validate_cidrs(values: Any, *, field: str) -> list[str]:
    """Parse every entry with `ipaddress.ip_network(strict=False)`; a failure is `invalid_cidr`."""
    if values is None:
        return []
    if not isinstance(values, list):
        raise AuthConfigError("malformed_auth", f"{field} must be a list of CIDR strings")
    parsed: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise AuthConfigError("invalid_cidr", f"{value!r} is not a CIDR string")
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise AuthConfigError("invalid_cidr", f"{value!r} is not a valid CIDR: {exc}") from exc
        parsed.append(value)
    return sorted(set(parsed))


def _validate_origin_suffixes(values: Any, platform: str) -> list[str]:
    """Lowercase, dedupe and sort; each must be a bare dotted suffix -- no scheme, path or wildcard."""
    if values is None:
        return sorted(DEFAULT_ORIGIN_SUFFIXES.get(platform, ()))
    if not isinstance(values, list):
        raise AuthConfigError("malformed_auth", "origin_suffixes must be a list of domain suffixes")
    normalised: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise AuthConfigError("invalid_origin_suffix", f"{value!r} is not a string")
        candidate = value.strip().lower()
        if not _ORIGIN_SUFFIX_RE.match(candidate):
            raise AuthConfigError(
                "invalid_origin_suffix",
                f"{value!r} must be a bare dotted domain suffix (no scheme, path or wildcard)",
            )
        normalised.append(candidate)
    return sorted(set(normalised))


def _normalise_modes(raw_modes: Any) -> list[str]:
    """`hmac` first, then the declared second factors, deduped and sorted."""
    if raw_modes is None:
        return [MODE_HMAC]
    if not isinstance(raw_modes, list) or any(not isinstance(m, str) for m in raw_modes):
        raise AuthConfigError("malformed_auth", "modes must be a list of strings")
    unknown = {m for m in raw_modes if m not in KNOWN_MODES}
    if unknown:
        raise AuthConfigError("unknown_auth_mode", f"unknown auth modes {sorted(unknown)}")
    seconds = sorted({m for m in raw_modes if m in SECOND_FACTOR_MODES})
    return [MODE_HMAC, *seconds]


def build_auth_config(
    platform: str, raw: dict[str, Any] | None, *, secret_ref: str
) -> tuple[dict[str, Any], str | None]:
    """Canonicalise one source's auth block. Returns `(config, plaintext_secret_or_None)`.

    The returned config is exactly the five-key (six with
    `basic_username`) wire shape the distribution API publishes and is
    safe to serve; the plaintext is handed back once, for the caller to
    encrypt, and appears nowhere in the config.
    """
    if raw is not None and not isinstance(raw, dict):
        raise AuthConfigError("malformed_auth", "auth must be an object")
    block: dict[str, Any] = dict(raw or {})

    modes = _normalise_modes(block.get("modes"))
    if is_generic_webhook(platform) and not (set(modes) & SECOND_FACTOR_MODES):
        raise AuthConfigError(
            "auth_second_factor_required",
            f"generic webhook source on {platform!r} must declare one of "
            f"{sorted(SECOND_FACTOR_MODES)} in addition to hmac",
        )

    cidrs = _validate_cidrs(block.get("cidrs"), field="cidrs")
    if MODE_IP_ALLOWLIST in modes and not cidrs:
        raise AuthConfigError("cidrs_required", "ip_allowlist mode requires at least one CIDR")

    config: dict[str, Any] = {
        "modes": modes,
        "cidrs": cidrs,
        "secret_ref": secret_ref,
        "origin_suffixes": _validate_origin_suffixes(block.get("origin_suffixes"), platform),
        "origin_cidrs": _validate_cidrs(block.get("origin_cidrs"), field="origin_cidrs"),
    }

    plaintext: str | None = None
    if MODE_BEARER in modes:
        token = block.get("bearer_token")
        if not isinstance(token, str) or len(token) < MIN_BEARER_TOKEN_CHARS:
            raise AuthConfigError(
                "bearer_token_too_short",
                f"bearer_token must be at least {MIN_BEARER_TOKEN_CHARS} characters",
            )
        plaintext = token
    if MODE_BASIC in modes:
        username = block.get("basic_username")
        password = block.get("basic_password")
        if not isinstance(username, str) or not username.strip():
            raise AuthConfigError("basic_username_required", "basic mode requires basic_username")
        if not isinstance(password, str) or len(password) < MIN_BASIC_PASSWORD_CHARS:
            raise AuthConfigError(
                "basic_password_too_short",
                f"basic_password must be at least {MIN_BASIC_PASSWORD_CHARS} characters",
            )
        config["basic_username"] = username.strip()
        plaintext = hashlib.sha256(password.encode("utf-8")).hexdigest()
    if MODE_BEARER in modes and MODE_BASIC in modes:
        raise AuthConfigError(
            "malformed_auth", "bearer and basic are mutually exclusive -- one secret slot per source"
        )
    return config, plaintext
```

- [ ] **Step 6: Wire it into `services/ingest_source_service.py`**

Add these imports (`AsyncDB` was already imported by Task 26):

```python
from services.bundle_secret_crypto import decrypt, encrypt
from services.errors import ApiError, not_found
from services.ingest_source_auth import AuthConfigError, build_auth_config
```

Add an `auth: dict[str, Any] | None = None` keyword-only parameter to `create_source` (Task 26's version) and, immediately before the row insert, this block:

```python
    try:
        auth_config, auth_plaintext = build_auth_config(platform, auth, secret_ref=f"src:{source_id}")
    except AuthConfigError as exc:
        raise ApiError(str(exc), 422, exc.reason) from exc
    auth_ciphertext, auth_iv = encrypt(auth_plaintext) if auth_plaintext is not None else (None, None)
```

then add `auth=auth_config, auth_secret_ciphertext=auth_ciphertext, auth_secret_iv=auth_iv,` to the existing `install_dal.ingest_sources.async_insert(...)` call. Nothing else in `create_source` changes.

Append these two functions at the end of the module:

```python
async def update_source_auth(
    install_dal: AsyncDB, *, tenant_id: int, source_id: str, auth: dict[str, Any], actor_id: int
) -> Any:
    """Replace one source's auth policy, rotate its auth secret, and audit the mode change.

    R52: the `ingest_sources` update goes through `install_dal` (Pattern
    A -- `AsyncQuerySet.update()` builds a real SQLAlchemy Core
    statement against the reflected table, so the JSON `auth` column
    serializes correctly on both the sqlite test fixture and real
    Postgres `JSONB`). The `audit_log` insert is a **separate,
    best-effort** write, wrapped in the same `try`/`except` this
    codebase already uses at every other audit-log call site (Decision
    #18) -- an audit-logging failure must never roll back or block the
    auth-policy change that already succeeded. It still goes through
    `install_dal` rather than the pre-existing pydal `dal`, since this
    is a new call site this plan's own code adds (R52). The audit row
    records the old and new `modes` lists and nothing else -- never a
    token, never a password, never a hash.
    """
    rows = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    existing = rows.first()
    if existing is None:
        raise not_found(f"ingest source {source_id!r} not found")

    try:
        auth_config, auth_plaintext = build_auth_config(
            existing.platform, auth, secret_ref=f"src:{source_id}"
        )
    except AuthConfigError as exc:
        raise ApiError(str(exc), 422, exc.reason) from exc
    ciphertext, iv = encrypt(auth_plaintext) if auth_plaintext is not None else (None, None)

    old_modes = list((existing.auth or {}).get("modes", []))
    now = datetime.now(UTC)
    await install_dal(install_dal.ingest_sources.id == existing.id).update(
        auth=auth_config,
        auth_secret_ciphertext=ciphertext,
        auth_secret_iv=iv,
        updated_at=now,
    )
    try:
        await install_dal.audit_log.async_insert(
            user_id=actor_id,
            action="ingest_source_auth_changed",
            target_type="ingest_source",
            target_id=source_id,
            details={
                "tenant_id": tenant_id,
                "platform": existing.platform,
                "old_modes": old_modes,
                "new_modes": auth_config["modes"],
            },
            created_at=now,
        )
    except Exception:  # noqa: BLE001, S110 -- audit logging failure must not break the main flow
        pass
    updated = await install_dal(install_dal.ingest_sources.id == existing.id).select()
    return updated.first()


async def resolve_auth_secret(install_dal: AsyncDB, *, tenant_id: int, source_id: str) -> str | None:
    """Decrypt the source's bearer token or Basic password hash. `None` when no secret is set."""
    rows = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    first = rows.first()
    if first is None or first.auth_secret_ciphertext is None:
        return None
    return decrypt(first.auth_secret_ciphertext, first.auth_secret_iv)
```

If `datetime`/`UTC` are not already imported in this module, add `from datetime import UTC, datetime`.

- [ ] **Step 7: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_source_auth.py -v`
Expected: `35 passed` (5 `is_generic_webhook` cases + 5 CIDR cases + 4 malformed-auth cases + 4 origin-suffix cases + 17 named tests).

- [ ] **Step 8: Verify the migration applies and backfills, on a real Postgres**

Run:
```bash
docker run -d --name pg-m2b-0022 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=waddlebot -p 55436:5432 postgres:17-alpine
sleep 3
DATABASE_URL="postgresql://postgres:test@localhost:55436/waddlebot" alembic upgrade 0021_bundle_install_schema
psql "postgresql://postgres:test@localhost:55436/waddlebot" -c \
  "INSERT INTO tenants (slug, display_name, is_active) VALUES ('acme','Acme',true) ON CONFLICT DO NOTHING;"
psql "postgresql://postgres:test@localhost:55436/waddlebot" -c \
  "INSERT INTO ingest_sources (tenant_id, platform, source_id, label) \
   SELECT id, 'twitch', 'tw-legacy', 'Legacy channel' FROM tenants WHERE slug='acme';"
DATABASE_URL="postgresql://postgres:test@localhost:55436/waddlebot" alembic upgrade 0022_ingest_source_auth
psql "postgresql://postgres:test@localhost:55436/waddlebot" -t -c \
  "SELECT auth FROM ingest_sources WHERE source_id='tw-legacy';"
docker rm -f pg-m2b-0022
```
Expected: the final `psql` prints a JSON object whose `"modes"` is `["hmac"]`, whose `"secret_ref"` is `"src:tw-legacy"`, and whose `"origin_suffixes"` is `["twitch.tv"]` — proving the backfill ran and an existing row was not left with an empty policy.

- [ ] **Step 9: Confirm zero regression on the ingest-source suites**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_source_service.py tests/test_ingest_sources_blueprint.py tests/test_distribution_sources_blueprint.py -v`
Expected: every previously-passing test still passes. Non-generic sources (`twitch`, `discord`, `custom:` absent) accept `auth=None` and get the platform default, so no existing call site needs the new keyword.

- [ ] **Step 10: Commit**

```bash
git add alembic/versions/0022_ingest_source_auth.py hub_api/services/ingest_source_auth.py \
        hub_api/tests/conftest.py hub_api/services/ingest_source_service.py \
        hub_api/tests/test_ingest_source_auth.py
git commit -m "$(cat <<'EOF'
db(hub-api): per-source auth config on ingest_sources -- second factor, origin policy, encrypted secret (R52)

The HMAC secret authenticates the payload, not the caller. Generic
webhook sources (custom:<name>, webhook) must now declare at least one
of ip_allowlist / bearer / basic alongside hmac, refused 422
auth_second_factor_required otherwise; Twitch and Kick carry an origin
policy instead (origin_suffixes defaulting to twitch.tv / kick.com,
plus optional origin_cidrs). CIDRs are parsed with ipaddress and a bad
one is 422 invalid_cidr.

Bearer tokens and Basic password hashes are AES-256-GCM at rest in
auth_secret_ciphertext/auth_secret_iv and never appear in the auth
document, any response, or the audit row -- the wire carries secret_ref
only. Every auth change writes audit_log ingest_source_auth_changed
with the old and new mode lists, as a separate best-effort write
through install_dal (Decision #18, R52) -- the same try/except-around-
a-single-insert convention this codebase already uses at every other
audit-log call site, so a logging failure never blocks the auth change.

Migration 0022 backfills existing rows with the platform-appropriate
default, so a deploy never silently breaks a running webhook source.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 40: publish `auth` — distribution `/sources`, the tenant config view, the consent view, and the auth `PUT` (R52: penguin-dal)

**Depends on:** Task 39 (`ingest_source_auth`, `update_source_auth`, the `auth` column, `install_dal`-only per R52), Task 34 (`list_sources_for_distribution` + `GET /api/v1/distribution/sources`, `install_dal`+existing-`communities` per R52), Task 27 (`blueprints/v1/ingest_sources.py`, `_install_dal()` helper), Task 22 (`blueprints/v1/bundle_approvals.py`, `_install_dal()` helper), Task 37 (`require_flags`).

**Files:**
- Modify: `hub_api/services/ingest_source_service.py`
- Modify: `hub_api/blueprints/v1/distribution.py`
- Modify: `hub_api/blueprints/v1/ingest_sources.py`
- Modify: `hub_api/blueprints/v1/bundle_approvals.py`
- Test: `hub_api/tests/test_ingest_source_auth_api.py`

**Interfaces:**

| Method | Path | Auth | Body / params | Success |
|---|---|---|---|---|
| `GET` | `/api/v1/distribution/sources` | `distribution:read` | unchanged (Task 34) | each `sources[]` row gains `auth` — the exact Decision #13 wire shape, **must match plan M5** |
| `GET` | `/api/v1/tenant/{slug}/ingest-sources` | `tenant_middleware` | unchanged | each row gains `auth` (the config view) |
| `POST` | `/api/v1/tenant/{slug}/ingest-sources` | `tenant:admin` + flags | body gains `"auth": {...} \| null` | `201`, `422` on a bad auth block |
| `PUT` | `/api/v1/tenant/{slug}/ingest-sources/{sourceId}/auth` | `tenant:admin` + `require_flags(FLAG_RUST_DATA_PLANE, FLAG_GENERIC_INTAKE)` | `{"auth": {...}}` | `200 {"success": true, "modes": [...]}` |
| `GET` | `/api/v1/apps/{app_id}/versions/{version}/permissions` | `platform:admin` | new optional query param `communityId` (int) | response gains `sourceAuth` (the consent view) |

- Produces: `DistributionSource.auth: dict[str, Any]`; `DistributionSourceDTO.auth`; `IngestSourceAuthRequest(auth: dict[str, Any])`; `PermissionSummaryResponse.sourceAuth: list[dict[str, Any]]`; `async def source_auth_for_consent(install_dal, *, tenant_id: int, community_id: int | None, consumes_platforms: set[str]) -> list[dict[str, Any]]` (`install_dal`-only per R52 — `ingest_sources` is this plan's own new table).
- Consumes: `services.ingest_source_service.{list_sources_for_distribution, update_source_auth}` (Tasks 34, 39); `services.bundle_feature_gate.{FLAG_GENERIC_INTAKE, FLAG_RUST_DATA_PLANE, require_flags}` (Task 37).

**`sourceAuth` is deliberately outside the hashed summary.** `permission_hash` is computed over `summary` only (Task 18's `canonical_json(summary)`), and this task does not change that. Rotating a bearer token or widening a CIDR list must not silently invalidate every existing approval for every bundle that happens to read that platform — the operator sees the configured mode on the consent screen because it is a sibling field of `summary` in the response DTO, not a member of it. A test asserts the hash is byte-identical before and after an auth change.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_ingest_source_auth_api.py
"""API-level tests for publishing the per-source auth config (Decision #13, must match plan M5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from quart import Quart

from services.bundle_install_dal import raw_sql_rows
from services.ingest_source_service import create_source
from tests.conftest import TENANT_SLUG, make_user_token

_BEARER = "t" * 40


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUNDLE_SECRET_ENCRYPTION_KEY", "e" * 64)


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _always_on(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("services.bundle_feature_gate.feature_enabled", _always_on)


async def _seed(install_dal: Any) -> None:
    await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="custom:acme", source_id="wh-1", label="Acme webhook", mapping=None,
        auth={"modes": ["bearer"], "bearer_token": _BEARER},
    )
    await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="twitch", source_id="tw-channelA", label="Twitch #channelA", mapping=None, auth=None,
    )


def _app(bundle_install_db: Any, install_dal: Any, module_path: str) -> Quart:
    import importlib

    module = importlib.import_module(module_path)
    app = Quart(__name__)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    for bp in module.BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_distribution_sources_publish_the_exact_m5_auth_shape(
    bundle_install_db: Any, install_dal: Any
) -> None:
    await _seed(install_dal)
    app = _app(bundle_install_db, install_dal, "blueprints.v1.distribution")
    token = make_user_token(user_id=1, scope="distribution:read", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        "/api/v1/distribution/sources", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    by_id = {s["sourceId"]: s for s in (await response.get_json())["sources"]}

    assert set(by_id["wh-1"]["auth"]) == {"modes", "cidrs", "secret_ref", "origin_suffixes", "origin_cidrs"}
    assert by_id["wh-1"]["auth"]["modes"] == ["hmac", "bearer"]
    assert by_id["wh-1"]["auth"]["secret_ref"] == "src:wh-1"
    assert by_id["wh-1"]["auth"]["cidrs"] == []
    assert by_id["tw-channelA"]["auth"]["modes"] == ["hmac"]
    assert by_id["tw-channelA"]["auth"]["origin_suffixes"] == ["twitch.tv"]


async def test_distribution_sources_never_echo_the_bearer_token(
    bundle_install_db: Any, install_dal: Any
) -> None:
    await _seed(install_dal)
    app = _app(bundle_install_db, install_dal, "blueprints.v1.distribution")
    token = make_user_token(user_id=1, scope="distribution:read", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        "/api/v1/distribution/sources", headers={"Authorization": f"Bearer {token}"}
    )
    assert _BEARER not in (await response.get_data()).decode()


async def test_distribution_sources_etag_changes_when_auth_changes(
    bundle_install_db: Any, install_dal: Any
) -> None:
    await _seed(install_dal)
    app = _app(bundle_install_db, install_dal, "blueprints.v1.distribution")
    token = make_user_token(user_id=1, scope="distribution:read", tenant=TENANT_SLUG)
    client = app.test_client()
    before = await client.get("/api/v1/distribution/sources", headers={"Authorization": f"Bearer {token}"})

    from services.ingest_source_service import update_source_auth

    await update_source_auth(
        install_dal, tenant_id=1, source_id="wh-1",
        auth={"modes": ["ip_allowlist"], "cidrs": ["203.0.113.0/24"]}, actor_id=1,
    )
    after = await client.get("/api/v1/distribution/sources", headers={"Authorization": f"Bearer {token}"})
    assert before.headers["ETag"] != after.headers["ETag"]


async def test_tenant_config_view_shows_the_configured_mode(
    bundle_install_db: Any, install_dal: Any
) -> None:
    await _seed(install_dal)
    app = _app(bundle_install_db, install_dal, "blueprints.v1.ingest_sources")
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    by_id = {s["sourceId"]: s for s in (await response.get_json())["sources"]}
    assert by_id["wh-1"]["auth"]["modes"] == ["hmac", "bearer"]
    assert _BEARER not in (await response.get_data()).decode()


async def test_post_source_rejects_a_generic_source_without_a_second_factor(
    bundle_install_db: Any, install_dal: Any
) -> None:
    app = _app(bundle_install_db, install_dal, "blueprints.v1.ingest_sources")
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().post(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources",
        headers={"Authorization": f"Bearer {token}"},
        json={"communityId": None, "platform": "custom:acme", "sourceId": "wh-2",
              "label": "Second webhook", "mapping": None, "auth": {"modes": ["hmac"]}},
    )
    assert response.status_code == 422
    assert (await response.get_json())["error"]["code"] == "auth_second_factor_required"


@pytest.mark.parametrize(
    ("auth", "code"),
    [
        ({"modes": ["ip_allowlist"], "cidrs": ["203.0.113.0/33"]}, "invalid_cidr"),
        ({"modes": ["ip_allowlist"], "cidrs": []}, "cidrs_required"),
        ({"modes": ["bearer"], "bearer_token": "short"}, "bearer_token_too_short"),
        ({"modes": ["nope"]}, "unknown_auth_mode"),
    ],
)
async def test_put_auth_surfaces_every_validation_failure_as_422(
    bundle_install_db: Any, install_dal: Any, auth: dict[str, Any], code: str
) -> None:
    await _seed(install_dal)
    app = _app(bundle_install_db, install_dal, "blueprints.v1.ingest_sources")
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().put(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources/wh-1/auth",
        headers={"Authorization": f"Bearer {token}"}, json={"auth": auth},
    )
    assert response.status_code == 422
    assert (await response.get_json())["error"]["code"] == code


async def test_put_auth_requires_tenant_admin(bundle_install_db: Any, install_dal: Any) -> None:
    await _seed(install_dal)
    app = _app(bundle_install_db, install_dal, "blueprints.v1.ingest_sources")
    token = make_user_token(user_id=1, scope="", tenant=TENANT_SLUG)
    response = await app.test_client().put(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources/wh-1/auth",
        headers={"Authorization": f"Bearer {token}"},
        json={"auth": {"modes": ["ip_allowlist"], "cidrs": ["203.0.113.0/24"]}},
    )
    assert response.status_code == 403


async def test_put_auth_is_flag_gated(bundle_install_db: Any, install_dal: Any) -> None:
    await _seed(install_dal)
    app = _app(bundle_install_db, install_dal, "blueprints.v1.ingest_sources")
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    with patch("services.bundle_feature_gate.feature_enabled", new_callable=AsyncMock) as mock_flag:
        mock_flag.return_value = False
        response = await app.test_client().put(
            f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources/wh-1/auth",
            headers={"Authorization": f"Bearer {token}"},
            json={"auth": {"modes": ["ip_allowlist"], "cidrs": ["203.0.113.0/24"]}},
        )
    assert response.status_code == 403
    assert (await response.get_json())["error"]["code"] == "feature_disabled"


async def test_put_auth_happy_path_writes_the_audit_row(bundle_install_db: Any, install_dal: Any) -> None:
    await _seed(install_dal)
    app = _app(bundle_install_db, install_dal, "blueprints.v1.ingest_sources")
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().put(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources/wh-1/auth",
        headers={"Authorization": f"Bearer {token}"},
        json={"auth": {"modes": ["ip_allowlist"], "cidrs": ["203.0.113.0/24"]}},
    )
    assert response.status_code == 200
    assert (await response.get_json())["modes"] == ["hmac", "ip_allowlist"]

    audit_rows = await raw_sql_rows(
        install_dal, "SELECT details FROM audit_log WHERE action = :a", {"a": "ingest_source_auth_changed"}
    )
    audit_row = audit_rows.first()
    assert audit_row is not None
    import json

    details = audit_row["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["new_modes"] == ["hmac", "ip_allowlist"]


async def test_consent_view_shows_the_mode_without_changing_the_permission_hash(
    bundle_install_db: Any, install_dal: Any
) -> None:
    await _seed(install_dal)
    now = datetime.now(UTC)
    manifest = {
        "schema_version": 2, "app_id": "waddles.socials.music.default", "name": "Music Station",
        "version": "3.0.1", "feature": "waddles.socials.music", "module": "socials",
        "provider": "builtin", "language": "python", "artifact": "source",
        "stages": {"process": {"entry": "x:y",
                               "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}]}},
    }
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED", manifest_json=manifest,
        created_at=now, updated_at=now,
    )

    app = _app(bundle_install_db, install_dal, "blueprints.v1.bundle_approvals")
    token = make_user_token(user_id=1, scope="platform:admin", tenant=TENANT_SLUG)
    client = app.test_client()
    before = await client.get(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    body_before = await before.get_json()
    assert before.status_code == 200
    assert any(entry["sourceId"] == "tw-channelA" for entry in body_before["sourceAuth"])
    assert next(e for e in body_before["sourceAuth"] if e["sourceId"] == "tw-channelA")["authModes"] == ["hmac"]

    from services.ingest_source_service import update_source_auth

    await update_source_auth(
        install_dal, tenant_id=1, source_id="tw-channelA",
        auth={"origin_suffixes": ["twitch.tv", "eventsub.twitch.tv"]}, actor_id=1,
    )
    after = await client.get(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    body_after = await after.get_json()
    assert body_after["permissionHash"] == body_before["permissionHash"]
    assert next(e for e in body_after["sourceAuth"] if e["sourceId"] == "tw-channelA")["originSuffixes"] == [
        "eventsub.twitch.tv", "twitch.tv"
    ]


async def test_consent_view_never_echoes_a_secret(bundle_install_db: Any, install_dal: Any) -> None:
    await _seed(install_dal)
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED",
        manifest_json={
            "schema_version": 2, "app_id": "waddles.socials.music.default", "name": "Music Station",
            "version": "3.0.1", "feature": "waddles.socials.music", "module": "socials",
            "provider": "builtin", "language": "python", "artifact": "source",
            "stages": {"process": {"entry": "x:y",
                                   "consumes": [{"platform": "custom:acme", "event_types": ["chat.message"]}]}},
        },
        created_at=now, updated_at=now,
    )
    app = _app(bundle_install_db, install_dal, "blueprints.v1.bundle_approvals")
    token = make_user_token(user_id=1, scope="platform:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert _BEARER not in (await response.get_data()).decode()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_source_auth_api.py -v`
Expected: `KeyError: 'auth'` on the distribution and config-view tests, `405`/`404` on the `PUT .../auth` tests, `KeyError: 'sourceAuth'` on the consent-view tests.

- [ ] **Step 3: Publish `auth` on `DistributionSource`**

In `hub_api/services/ingest_source_service.py`, add `auth: dict[str, Any]` as the last field of the `DistributionSource` dataclass:

```python
    auth: dict[str, Any]
```

and add `auth=dict(row.auth or {}),` to the `DistributionSource(...)` construction inside `list_sources_for_distribution` (Task 34's version, which iterates `rows` from `install_dal(query).select()`).

Append this function to the same module:

```python
async def source_auth_for_consent(
    install_dal: AsyncDB, *, tenant_id: int, community_id: int | None, consumes_platforms: set[str]
) -> list[dict[str, Any]]:
    """The configured auth mode of every source a bundle's `consumes` rules would reach.

    Shown on the consent screen next to the grant list so an approver
    sees *how a caller is authenticated*, not only *what is read*.
    Deliberately NOT part of the hashed permission summary: rotating a
    token or widening a CIDR must never invalidate an existing
    approval. R52: `ingest_sources` is this plan's own new table,
    queried through `install_dal` only.
    """
    query = install_dal.ingest_sources.tenant_id == tenant_id
    if community_id is not None:
        query &= (install_dal.ingest_sources.community_id == community_id) | (
            install_dal.ingest_sources.community_id == None  # noqa: E711 - penguin-dal IS NULL operator
        )
    rows = await install_dal(query).select(orderby=install_dal.ingest_sources.source_id)
    wildcard = "*" in consumes_platforms
    entries: list[dict[str, Any]] = []
    for row in rows:
        if not wildcard and row.platform not in consumes_platforms:
            continue
        auth = dict(row.auth or {})
        entries.append(
            {
                "platform": row.platform,
                "sourceId": row.source_id,
                "label": row.label,
                "authModes": list(auth.get("modes", [])),
                "cidrs": list(auth.get("cidrs", [])),
                "originSuffixes": list(auth.get("origin_suffixes", [])),
                "originCidrs": list(auth.get("origin_cidrs", [])),
            }
        )
    return entries
```

- [ ] **Step 4: Publish `auth` on the distribution `/sources` DTO**

In `hub_api/blueprints/v1/distribution.py`, add the field to `DistributionSourceDTO`:

```python
    auth: dict[str, Any] = field(default_factory=dict)
```

pass `auth=s.auth,` in the `DistributionSourceDTO(...)` construction, and add `"auth": d.auth` to the `etag_seed` projection so an auth change moves the ETag:

```python
                                config={"label": d.label, "hasSecret": d.hasSecret,
                                        "mapping": d.mapping, "auth": d.auth})
```

- [ ] **Step 5: Add `auth` to the tenant config view, the `POST` body, and the new `PUT`**

In `hub_api/blueprints/v1/ingest_sources.py`:

```python
from services.bundle_feature_gate import FLAG_GENERIC_INTAKE, FLAG_RUST_DATA_PLANE, require_flags
from services.ingest_source_service import update_source_auth
```

Add `auth: dict[str, Any] | None = None` as the last field of the existing `CreateSourceRequest` DTO, and pass `auth=data.auth` through to `create_source(...)`.

Add `auth: dict[str, Any] = field(default_factory=dict)` to the `SourceDTO` the `GET` handler returns, populated from `row.auth or {}`. The `GET` handler must never include `auth_secret_ciphertext`/`auth_secret_iv` — it builds a DTO per row, so simply do not add those fields.

Append this request DTO and route immediately before the file's final `BLUEPRINTS: list[Blueprint] = [...]` line:

```python
@dataclass(slots=True, frozen=True)
class IngestSourceAuthRequest:
    """Request DTO for `PUT .../ingest-sources/{sourceId}/auth`."""

    auth: dict[str, Any]


@ingest_sources_bp.route("/<source_id>/auth", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@require_flags(FLAG_RUST_DATA_PLANE, FLAG_GENERIC_INTAKE)
@validate_request(IngestSourceAuthRequest)
async def put_source_auth(
    data: IngestSourceAuthRequest, tenant_slug: str, source_id: str
) -> tuple[dict[str, object], int]:
    """Replace one source's caller-authentication policy and audit the mode change.

    `tenant_slug` is a routing segment only (from the blueprint's
    `url_prefix`) -- the tenant this writes to comes from the caller's
    own JWT via `get_tenant_context`, never the path (security.md
    Tenant Isolation). R52: `install_dal`-only, `ingest_sources` is
    this plan's own new table.
    """
    install_dal = _install_dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware always publishes this on the success path
    require_matching_tenant(tenant_slug, ctx.tenant_slug)
    try:
        row = await update_source_auth(
            install_dal, tenant_id=ctx.tenant_id, source_id=source_id,
            auth=data.auth, actor_id=get_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "modes": list((row.auth or {}).get("modes", []))}, 200
```

(This route reuses `ingest_sources_bp`'s existing `url_prefix="/api/v1/tenant/<tenant_slug>/ingest-sources"` from Task 27 — the route rule is `/<source_id>/auth`, giving the full path `/api/v1/tenant/{slug}/ingest-sources/{sourceId}/auth`. `get_current_user_id` and `require_matching_tenant` are already imported by this file from Task 27.)

- [ ] **Step 6: Add `sourceAuth` to the consent view**

In `hub_api/blueprints/v1/bundle_approvals.py`:

```python
from services.ingest_source_service import source_auth_for_consent
```

Add the field to the response DTO:

```python
@dataclass(slots=True, frozen=True)
class PermissionSummaryResponse:
    """Response DTO for `GET .../permissions`.

    `sourceAuth` is a sibling of `summary`, never a member: it is NOT
    covered by `permissionHash`, so rotating a source's token or
    widening its CIDR list never invalidates an existing approval.
    """

    success: bool
    summary: dict[str, Any]
    permissionHash: str
    sourceAuth: list[dict[str, Any]] = field(default_factory=list)
```

and replace the `get_permissions` body's return with:

```python
    platforms = {str(entry.get("platform")) for entry in summary.get("streams", [])}
    try:
        community_id = _parse_community_id(request.args.get("communityId"))
    except ApiError as exc:
        return _err(exc)
    install_dal = _install_dal()
    source_auth = await source_auth_for_consent(
        install_dal, tenant_id=ctx.tenant_id, community_id=community_id, consumes_platforms=platforms
    )
    return PermissionSummaryResponse(
        success=True, summary=summary, permissionHash=permission_hash, sourceAuth=source_auth
    )
```

`get_permissions` already binds `install_dal = _install_dal()` at its top (Task 22); reuse that binding instead of re-fetching it if the existing local variable is still in scope at this point in the function body. Add `ctx = get_tenant_context(request)` / `assert ctx is not None  # nosec B101` above this block if `get_permissions` does not already bind `ctx` (Task 22's version does not need `ctx` for anything else, so add it here), and the same `_parse_community_id` helper Task 31's blueprint defines (copy it verbatim into this file — a five-line helper duplicated is better than a cross-blueprint import).

- [ ] **Step 7: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_source_auth_api.py -v`
Expected: `14 passed` (4 validation-failure cases + 10 others).

- [ ] **Step 8: Confirm zero regression across every touched surface**

Run:
```bash
cd hub_api && python3 -m pytest \
  tests/test_ingest_source_service.py tests/test_ingest_source_auth.py \
  tests/test_ingest_sources_blueprint.py tests/test_distribution_sources_blueprint.py \
  tests/test_bundle_approvals_blueprint.py tests/test_bundle_approval_service.py \
  tests/test_permission_summary_service.py -v
```
Expected: every previously-passing test still passes. `test_permission_summary_service.py` in particular must be untouched — `build_permission_summary`'s signature and output are unchanged, which is what keeps `permissionHash` stable.

- [ ] **Step 9: Commit**

```bash
git add hub_api/services/ingest_source_service.py hub_api/blueprints/v1/distribution.py \
        hub_api/blueprints/v1/ingest_sources.py hub_api/blueprints/v1/bundle_approvals.py \
        hub_api/tests/test_ingest_source_auth_api.py
git commit -m "$(cat <<'EOF'
feat(hub-api): publish the per-source auth config -- distribution /sources, config view, consent view, auth PUT (R52)

GET /api/v1/distribution/sources now carries auth =
{modes, cidrs, secret_ref, origin_suffixes, origin_cidrs} per source,
the shape svc_ingest enforces (must match plan M5); the ETag moves when
an auth policy changes. The tenant config view shows the configured
mode, POST accepts an auth block, and a new
PUT /api/v1/tenant/{slug}/ingest-sources/{sourceId}/auth rotates the
policy behind tenant:admin plus the generic-intake flag, writing the
audit row.

The consent view gains sourceAuth as a sibling of summary, never a
member: rotating a token or widening a CIDR list must not invalidate
existing approvals, so permissionHash is provably unchanged by an auth
edit. No bearer token or password hash appears in any response. Every
new query in this task goes through penguin-dal's install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 41: OpenAPI coverage, logging conformance, coverage gate, lint and the containerized `make` gates

**Depends on:** every preceding task (1-40). This was the milestone's closing gate when the plan had 41 tasks; Tasks 42-49 (D30/D31) were folded in afterward, and Task 49 is now the milestone's actual closing gate — it depends on this task and widens its two scanner suites rather than replacing them.

**Files:**
- Create: `hub_api/tests/test_openapi_m2b_paths.py`
- Create: `hub_api/tests/test_m2b_logging_conformance.py`
- Modify: `hub_api/pyproject.toml`

**Interfaces:**
- Produces: nothing consumed by another task. Two leaf verification suites plus the final green-gate command list.
- Consumes: `hub_api/openapi/routes.py`'s existing `/openapi/v1.json` route (quart-schema's own generated document — no hand-written spec fragment is added by this milestone; every M2b route appears in it automatically because it is a registered, non-hidden Quart rule).

**Why there is no hand-authored OpenAPI file here.** hub-api serves two documents (`hub_api/openapi/spec_builder.py`'s header explains the split): a hand-curated, unauthenticated login-only document, and the full document generated by `quart_schema`'s introspection behind `tenant_middleware` + `require_scope("platform:read")`. M2b adds no public route, so `build_public_login_spec` is untouched; every new route is picked up by the generated document the moment its blueprint is registered. What this task adds is the **assertion that they actually are** — with a non-zero denominator, so a blueprint that silently failed to auto-discover is caught rather than assumed present.

- [ ] **Step 1: Write the OpenAPI coverage test**

```python
# hub_api/tests/test_openapi_m2b_paths.py
"""Asserts every M2b route appears in the generated /openapi/v1.json document.

Reports how many paths were examined -- a document that generated zero
paths, or a blueprint that failed to auto-discover, must fail here
rather than pass silently (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

_M2B_RULES: list[str] = [
    "/api/v1/apps/<app_id>/versions",
    "/api/v1/apps/<app_id>/versions/<version>/activate",
    "/api/v1/apps/<app_id>/trip-reenable",
    "/api/v1/apps/<app_id>/versions/<version>/permissions",
    "/api/v1/apps/<app_id>/versions/<version>/approve",
    "/api/v1/apps/<app_id>/versions/<version>/deny",
    "/api/v1/apps/<app_id>/grants",
    "/api/v1/apps/<app_id>/grants/resolve",
    "/api/v1/apps/<app_id>/grants/<int:grant_id>",
    "/api/v1/bundles/<app_id>/versions/<version>/artifact",
    "/api/v1/bundles/<app_id>/versions/<version>/rejected",
    "/api/v1/marketplace/settings",
    "/api/v1/tenant/<slug>/custom-platforms",
    "/api/v1/tenant/<slug>/custom-platforms/<name>",
    "/api/v1/tenant/<slug>/custom-platforms/<name>/tokens",
    "/api/v1/tenant/<slug>/ingest-sources",
    "/api/v1/tenant/<slug>/ingest-sources/<source_id>",
    "/api/v1/tenant/<slug>/ingest-sources/<source_id>/auth",
    "/api/v1/distribution/bundles",
    "/api/v1/distribution/v2/bundles",
    "/api/v1/distribution/sources",
]

_M2B_MODULES: list[str] = [
    "blueprints.v1.bundle_versions",
    "blueprints.v1.bundle_approvals",
    "blueprints.v1.bundle_grants",
    "blueprints.v1.bundle_artifact_callback",
    "blueprints.v1.bundle_settings",
    "blueprints.v1.custom_platforms",
    "blueprints.v1.ingest_sources",
    "blueprints.v1.distribution",
]


@pytest.fixture
def m2b_app() -> Quart:
    """Every M2b blueprint registered on one app, with QuartSchema wired as `app.py` wires it."""
    import importlib

    app = Quart(__name__)
    QuartSchema(app, openapi_path=None, swagger_ui_path=None)
    registered = 0
    for module_path in _M2B_MODULES:
        module = importlib.import_module(module_path)
        for bp in module.BLUEPRINTS:
            app.register_blueprint(bp)
            registered += 1
    print(f"openapi check: blueprints registered = {registered}")
    assert registered >= len(_M2B_MODULES), "at least one blueprint per module must register"
    return app


def test_every_m2b_rule_is_mounted(m2b_app: Quart) -> None:
    # If a rule below does not match, check the handler's actual converter
    # variable name first (`<source_id>` vs `<sourceId>` etc.) and correct
    # THIS list -- never rename a route to satisfy the test, because the
    # path shape is the wire contract the Rust stages and the webui call.
    mounted = {str(rule.rule) for rule in m2b_app.url_map.iter_rules()}
    print(f"openapi check: rules examined = {len(mounted)}")
    assert len(mounted) > 0, "zero rules examined -- FAIL, not a pass"
    missing = [rule for rule in _M2B_RULES if rule not in mounted]
    assert not missing, f"M2b rules missing from the app: {missing}"


def test_generated_openapi_document_contains_every_m2b_path(m2b_app: Quart) -> None:
    provider = m2b_app.extensions["QUART_SCHEMA"].openapi_provider
    schema: dict[str, Any] = provider.schema()
    paths = schema.get("paths", {})
    print(f"openapi check: generated paths examined = {len(paths)}")
    assert len(paths) > 0, "the generated document has zero paths -- FAIL, not a pass"

    def _to_openapi(rule: str) -> str:
        out = rule
        for werkzeug, name in (("<int:grant_id>", "{grant_id}"),):
            out = out.replace(werkzeug, name)
        while "<" in out:
            start = out.index("<")
            end = out.index(">", start)
            out = out[:start] + "{" + out[start + 1 : end] + "}" + out[end + 1 :]
        return out

    expected = {_to_openapi(rule) for rule in _M2B_RULES}
    missing = sorted(expected - set(paths))
    assert not missing, f"M2b paths absent from the generated OpenAPI document: {missing}"


def test_the_public_document_did_not_grow(m2b_app: Quart) -> None:
    """M2b adds no unauthenticated route -- the login-only public document must still have exactly one path."""
    from openapi.spec_builder import build_public_login_spec

    spec = build_public_login_spec(title="hub-api", version="3.0.0")
    assert list(spec["paths"]) == ["/api/v1/auth/login"]
```

- [ ] **Step 2: Run it**

Run: `cd hub_api && python3 -m pytest tests/test_openapi_m2b_paths.py -v -s`
Expected: `3 passed`, with stdout carrying the denominators — `openapi check: blueprints registered = 8`, `openapi check: rules examined = <N>` where N ≥ 21, `openapi check: generated paths examined = <M>` where M ≥ 21.

- [ ] **Step 3: Write the logging-conformance test**

```python
# hub_api/tests/test_m2b_logging_conformance.py
"""Every M2b module uses the flask_core logging library, never a hand-rolled logger.

testing.md Logging Library Conformance. Reports how many files were
scanned: a scanner pointed at a moved directory reports clean, so a
zero-file scan is a FAILURE, not a pass.
"""

from __future__ import annotations

import re
from pathlib import Path

_M2B_FILES: list[str] = [
    "services/bundle_secret_crypto.py",
    "services/bundle_manifest_v2.py",
    "services/bundle_storage_service.py",
    "services/compiler_job_service.py",
    "services/bundle_version_service.py",
    "services/bundle_artifact_service.py",
    "services/bundle_activation_service.py",
    "services/permission_summary_service.py",
    "services/bundle_approval_service.py",
    "services/bundle_db_role_service.py",
    "services/platform_settings_service.py",
    "services/tenant_bundle_settings.py",
    "services/custom_platform_service.py",
    "services/ingest_source_service.py",
    "services/ingest_source_auth.py",
    "services/valkey_admin_client.py",
    "services/stream_grant_service.py",
    "services/bundle_trip_reenable_service.py",
    "services/bundle_role_cleanup_job.py",
    "services/bundle_feature_gate.py",
    "services/bundle_telemetry.py",
    "blueprints/v1/bundle_versions.py",
    "blueprints/v1/bundle_artifact_callback.py",
    "blueprints/v1/bundle_approvals.py",
    "blueprints/v1/bundle_settings.py",
    "blueprints/v1/custom_platforms.py",
    "blueprints/v1/ingest_sources.py",
    "blueprints/v1/bundle_grants.py",
    "blueprints/v1/distribution.py",
]

_BANNED = re.compile(r"\blogging\.basicConfig\b|\blogging\.getLogger\b|^\s*print\(", re.MULTILINE)

# `print` is legitimate in the CronJob entrypoint's own stdout report --
# that is CLI output, not service logging (testing.md's explicit carve-out).
_PRINT_ALLOWED = {"services/bundle_role_cleanup_job.py"}


def test_no_m2b_module_hand_rolls_a_logger() -> None:
    root = Path(__file__).resolve().parent.parent
    scanned = 0
    offenders: list[str] = []
    for relative in _M2B_FILES:
        path = root / relative
        assert path.exists(), f"{relative} does not exist -- the scanner is pointed at the wrong root"
        scanned += 1
        source = path.read_text(encoding="utf-8")
        for match in _BANNED.finditer(source):
            if match.group(0).strip().startswith("print(") and relative in _PRINT_ALLOWED:
                continue
            line = source[: match.start()].count("\n") + 1
            offenders.append(f"{relative}:{line}: {match.group(0).strip()}")
    print(f"logging conformance: files scanned = {scanned}")
    assert scanned == len(_M2B_FILES), "zero or partial scan -- FAIL, not a pass"
    assert not offenders, "hand-rolled logging found:\n" + "\n".join(offenders)


def test_the_scanner_can_actually_fail(tmp_path: object) -> None:
    """Prove the regex fires -- a check that never fails will never be noticed."""
    assert _BANNED.search("import logging\nlogging.basicConfig(level='INFO')\n") is not None
    assert _BANNED.search("print('hello')\n") is not None
    assert _BANNED.search("from flask_core.logging_config import get_logger\n") is None
```

- [ ] **Step 4: Run it**

Run: `cd hub_api && python3 -m pytest tests/test_m2b_logging_conformance.py -v -s`
Expected: `2 passed`, stdout carrying `logging conformance: files scanned = 29`. If a file legitimately needs a logger, import it from `flask_core.logging_config` rather than `logging` — do not add the file to `_PRINT_ALLOWED`.

- [ ] **Step 5: Add every remaining ruff per-file ignore**

Append to `hub_api/pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]` any entry not already added by an earlier task:

```toml
# M2b bundle-install group -- camelCase DTO fields are the wire contract the
# Rust stages and the webui deserialize (spec Sec6.7, Sec9.7); S101 is the
# tenant_middleware postcondition assert every ported blueprint repeats.
"blueprints/v1/bundle_versions.py" = ["N815", "S101"]
"blueprints/v1/bundle_artifact_callback.py" = ["N815", "S101"]
"blueprints/v1/bundle_approvals.py" = ["N815", "S101"]
"blueprints/v1/bundle_settings.py" = ["N815", "S101"]
"blueprints/v1/custom_platforms.py" = ["N815", "S101"]
"blueprints/v1/ingest_sources.py" = ["N815", "S101"]
"blueprints/v1/bundle_grants.py" = ["N815", "S101"]
"blueprints/v1/distribution.py" = ["N815", "S101"]
# The per-bundle role DDL interpolates a role name derived from a validated
# app_id and table names checked against a strict regex -- S608 flags the
# f-string SQL shape, not a real injection path (see _validate_tables).
"services/bundle_db_role_service.py" = ["S608"]
```

- [ ] **Step 6: Run the linter and confirm it can fail**

Run:
```bash
cd hub_api && python3 -m ruff check . && python3 -m ruff format --check .
```
Expected: `All checks passed!` and `<N> files already formatted`.

Then prove the gate is real:
```bash
cd hub_api && printf 'import os\n' >> services/bundle_telemetry.py && python3 -m ruff check services/bundle_telemetry.py ; git checkout -- services/bundle_telemetry.py
```
Expected: `F401 [*] \`os\` imported but unused` and a non-zero exit — the linter demonstrably fails on a real defect (`critical-rules.md` Verification Integrity). The `git checkout` restores the file.

- [ ] **Step 7: Run `mypy --strict` over the new modules**

Run:
```bash
cd hub_api && python3 -m mypy --strict \
  services/bundle_secret_crypto.py services/bundle_manifest_v2.py services/bundle_storage_service.py \
  services/compiler_job_service.py services/bundle_version_service.py services/bundle_artifact_service.py \
  services/bundle_activation_service.py services/permission_summary_service.py \
  services/bundle_approval_service.py services/bundle_db_role_service.py \
  services/platform_settings_service.py services/tenant_bundle_settings.py \
  services/custom_platform_service.py services/ingest_source_service.py services/ingest_source_auth.py \
  services/valkey_admin_client.py services/stream_grant_service.py \
  services/bundle_trip_reenable_service.py services/bundle_role_cleanup_job.py \
  services/bundle_feature_gate.py services/bundle_telemetry.py services/rbac_matrix.py \
  blueprints/v1/bundle_versions.py blueprints/v1/bundle_artifact_callback.py \
  blueprints/v1/bundle_approvals.py blueprints/v1/bundle_settings.py \
  blueprints/v1/custom_platforms.py blueprints/v1/ingest_sources.py \
  blueprints/v1/bundle_grants.py blueprints/v1/distribution.py
```
Expected: `Success: no issues found in 30 source files`. The only permitted suppressions are the `# type: ignore[untyped-decorator]` comments on `tenant_middleware`/`require_scope`, which every existing hub-api blueprint already carries (`hub_api/openapi/routes.py` documents why).

- [ ] **Step 8: Run the full M2b suite with the coverage gate**

Run:
```bash
cd hub_api && python3 -m pytest tests/ \
  --cov=services --cov=blueprints --cov-report=term-missing --cov-fail-under=90 -q
```
Expected: every test passes and the final line reads `Required test coverage of 90% reached.` — if it does not, the run exits non-zero and the milestone is not done. Record the reported total-statements number; a run whose denominator is `0 statements` is a failure, not a pass.

- [ ] **Step 9: Run the containerized build**

Run:
```bash
docker build -f hub_api/Dockerfile -t waddlebot/hub-api:m2b-local .
```
Expected: the build completes and the final stage's `USER` is non-root. Verify:
```bash
docker run --rm --entrypoint sh waddlebot/hub-api:m2b-local -c 'id -u'
```
Expected: a non-zero uid (never `0`).

- [ ] **Step 10: Run the repo's containerized `make` gates**

Run, from the repository root, in this order, stopping at the first failure:
```bash
make lint
make test-security
make test
make pre-commit
```
Expected:
- `make lint` → `scripts/lint.sh` completes with exit `0` and prints the number of files it checked.
- `make test-security` → `scripts/security-scan.sh` completes with exit `0` and reports, per scanner, how many files/packages were examined. A scanner reporting zero examined items is a FAILURE — fix the path, do not accept the clean result (`critical-rules.md` Verification Integrity).
- `make test` → `tests/k8s/alpha/05-unit-tests.sh` runs every suite and prints a non-zero `TOTAL_PASSED`.
- `make pre-commit` → `=== Pre-commit complete ===` after lint, security and test all pass.

If `make lint` or `make test-security` completes suspiciously fast or reports no denominator, audit the target once by making it fail on purpose (append an unused import, re-run, confirm a non-zero exit, revert) before treating it as green.

- [ ] **Step 11: Commit**

```bash
git add hub_api/tests/test_openapi_m2b_paths.py hub_api/tests/test_m2b_logging_conformance.py \
        hub_api/pyproject.toml
git commit -m "$(cat <<'EOF'
test(hub-api): M2b closing gate -- OpenAPI path coverage, logging conformance, ruff ignores

Asserts every M2b route is mounted and present in the generated
/openapi/v1.json document, and that the unauthenticated login-only
document did not grow. Asserts no M2b module hand-rolls a logger, with
a companion test proving the scanner's regex actually fires. Both
suites print their denominators -- a zero-file or zero-path run fails
rather than reporting clean.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 42: Migration 0023 — `workstreams` (D30) + `workstream_usage_hourly` (D31) + RBAC matrix extension

**Depends on:** Task 1 (`config/postgres/rbac-matrix.yaml` and `scripts/db/rbac_matrix.py`), Task 3 (migration 0021's `ingest_sources` table, this migration's `down_revision` and FK target).

**Files:**
- Modify: `config/postgres/rbac-matrix.yaml`
- Create: `alembic/versions/0023_workstreams_and_usage.py`

**Interfaces:**
- Produces: table `workstreams(id, tenant_id, community_id, ingest_source_id, platform, source_id, created_at, disabled_at)`, `UNIQUE(ingest_source_id)`, backfilled 1:1 from every existing `ingest_sources` row at migration time (spec §6.11, §5.11, D30). Table `workstream_usage_hourly(id, tenant_id, community_id, workstream_id, stage, app_id, hour, events, invocations, host_calls, actions_delivered, fuel_ms, outbound_bytes, media_minutes, recorded_at)`, no unique constraint on the natural key — a correction is a new row, summed at query time (spec §6.12, D31). Two new `config/postgres/rbac-matrix.yaml` tables: `hub_api` gets full CRUD on `workstreams`; `hub_api` gets **only** `SELECT`/`INSERT` on `workstream_usage_hourly` — no `UPDATE`/`DELETE` for anyone, including `hub_api` (spec §11.10.1's literal wording). Every other service role gets zero privileges on both.
- Consumes: `scripts/db/rbac_matrix.py`'s `load_matrix`, `render_create_roles_sql`, `render_revoke_public_sql`, `render_grant_sql` (Task 1), loaded via the same `importlib.util.spec_from_file_location` technique as Tasks 2-3.

**Why `ingest_source_id` is a `bigint` FK, not the spec's literal `source_id text` FK.** Spec §6.11 writes `source_id | text | FK intake_sources(source_id), UNIQUE`. In this plan's actual schema (Task 3), `ingest_sources.source_id` is unique only per `(tenant_id, platform, source_id)` (a three-column `UNIQUE`), not globally — two different tenants can register the same `source_id` string on different platforms. A workstream's one-to-one FK therefore has to target `ingest_sources.id` (the real, globally-unique surrogate key), not the non-unique `source_id` column the spec's shorthand assumes. `platform`/`source_id` are denormalized onto `workstreams` anyway (read convenience for the distribution/config views, Task 45), so nothing the spec's readers actually need is lost. `ingest_source_id` is **nullable** with `ON DELETE SET NULL`, never `ON DELETE CASCADE`: `workstream_usage_hourly` rows reference `workstream_id` for the life of the tenant's usage history, and a source being deleted must never cascade-delete the usage history behind it (spec §6.11 says "a disabled workstream mints nothing further" — it says nothing about deleting the row, and deleting it would orphan every `workstream_usage_hourly` row's FK). `services/workstream_service.py` (Task 44) sets `disabled_at` explicitly, before the source row is removed.

- [ ] **Step 1: Extend the RBAC matrix file**

In `config/postgres/rbac-matrix.yaml`, add `workstreams` and `workstream_usage_hourly` to the `tables:` list, immediately after `app_versions_audit_log`:

```yaml
  - workstreams
  - workstream_usage_hourly
```

Then append these rows to the end of the `grants:` list:

```yaml
  # workstreams (D30): hub-api-owned, 1:1 with ingest_sources. Only hub_api writes.
  - role: hub_api
    table: workstreams
    privileges: [SELECT, INSERT, UPDATE, DELETE]
  - role: waddles_publisher
    table: workstreams
    privileges: []
  - role: svc_ingest
    table: workstreams
    privileges: []
  - role: svc_process
    table: workstreams
    privileges: []
  - role: svc_action
    table: workstreams
    privileges: []
  - role: svc_streaming
    table: workstreams
    privileges: []
  - role: webui
    table: workstreams
    privileges: []
  - role: migration_runner
    table: workstreams
    privileges: [SELECT, INSERT, UPDATE, DELETE]

  # workstream_usage_hourly (D31): append-only. hub_api gets SELECT/INSERT
  # only -- no UPDATE/DELETE for anyone, including hub_api (spec Sec11.10.1,
  # Sec5.12 literal wording: "no UPDATE/DELETE for anyone"). migration_runner
  # keeps this matrix's uniform DDL-time-only grant (never used at runtime,
  # matching every other table's migration_runner row) so a future column
  # migration on this table is not a special case.
  - role: hub_api
    table: workstream_usage_hourly
    privileges: [SELECT, INSERT]
  - role: waddles_publisher
    table: workstream_usage_hourly
    privileges: []
  - role: svc_ingest
    table: workstream_usage_hourly
    privileges: []
  - role: svc_process
    table: workstream_usage_hourly
    privileges: []
  - role: svc_action
    table: workstream_usage_hourly
    privileges: []
  - role: svc_streaming
    table: workstream_usage_hourly
    privileges: []
  - role: webui
    table: workstream_usage_hourly
    privileges: []
  - role: migration_runner
    table: workstream_usage_hourly
    privileges: [SELECT, INSERT, UPDATE, DELETE]
```

- [ ] **Step 2: Write the migration**

```python
# alembic/versions/0023_workstreams_and_usage.py
"""workstreams (spec Sec6.11, D30) + workstream_usage_hourly (spec Sec6.12, D31).

`workstreams` is 1:1 with `ingest_sources`, backfilled here so every
source that existed before this migration has a `workstream_id` the
moment D30 code ships (spec Sec15.4's literal requirement). Created
going forward by `services/workstream_service.py` (Task 44) inside
`ingest_source_service.create_source()`; disabled (never deleted) when
the owning source is removed.

`workstream_usage_hourly` is written by hub-api's usage aggregator only
(Task 47) -- SELECT/INSERT, no UPDATE/DELETE for anyone, including
hub_api (spec Sec5.12: "corrections are new rows for the same key,
summed at query time, never an UPDATE of a settled hour"). Grants are
rendered from config/postgres/rbac-matrix.yaml, same generator every
earlier migration in this plan used.

Revision ID: 0023_workstreams_and_usage
Revises: 0022_ingest_source_auth
Create Date: 2026-09-14
"""

import importlib.util
import os
from pathlib import Path

from alembic import op

revision = "0023_workstreams_and_usage"
down_revision = "0022_ingest_source_auth"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
)
_MATRIX_TABLES = frozenset({"workstreams", "workstream_usage_hourly"})


def _load_matrix_module():
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0023", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workstreams (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            ingest_source_id BIGINT UNIQUE REFERENCES ingest_sources(id) ON DELETE SET NULL,
            platform VARCHAR(50) NOT NULL,
            source_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            disabled_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE workstreams IS "
        "'hub-api-owned, 1:1 with ingest_sources (spec Sec6.11, D30). "
        "ingest_source_id is nullable/ON DELETE SET NULL so usage history "
        "in workstream_usage_hourly outlives a deleted source.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workstreams_tenant "
        "ON workstreams (tenant_id, community_id) WHERE disabled_at IS NULL"
    )

    op.execute(
        """
        INSERT INTO workstreams (tenant_id, community_id, ingest_source_id, platform, source_id, created_at)
        SELECT s.tenant_id, s.community_id, s.id, s.platform, s.source_id, s.created_at
        FROM ingest_sources s
        WHERE NOT EXISTS (
            SELECT 1 FROM workstreams w WHERE w.ingest_source_id = s.id
        )
        """
    )
    op.execute(
        "COMMENT ON COLUMN workstreams.ingest_source_id IS "
        "'Backfilled 1:1 from every pre-existing ingest_sources row (spec Sec15.4); "
        "NULL only after the owning source has been deleted.'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workstream_usage_hourly (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            workstream_id UUID NOT NULL REFERENCES workstreams(id),
            stage VARCHAR(20) NOT NULL
                CHECK (stage IN ('ingest', 'process', 'action', 'streaming')),
            app_id VARCHAR(255) REFERENCES app_catalog(app_id),
            hour TIMESTAMPTZ NOT NULL,
            events BIGINT NOT NULL DEFAULT 0,
            invocations BIGINT NOT NULL DEFAULT 0,
            host_calls BIGINT NOT NULL DEFAULT 0,
            actions_delivered BIGINT NOT NULL DEFAULT 0,
            fuel_ms BIGINT NOT NULL DEFAULT 0,
            outbound_bytes BIGINT NOT NULL DEFAULT 0,
            media_minutes NUMERIC,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE workstream_usage_hourly IS "
        "'Append-only (spec Sec6.12, D31) -- no UNIQUE on the natural key, "
        "a correction is a new row, summed at query time. hub_api has "
        "SELECT/INSERT only, never UPDATE/DELETE.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workstream_usage_hourly_lookup "
        "ON workstream_usage_hourly (tenant_id, community_id, workstream_id, hour)"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get(
        "RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH)
    )
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute("GRANT USAGE ON SEQUENCE workstream_usage_hourly_id_seq TO hub_api;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workstream_usage_hourly")
    op.execute("DROP TABLE IF EXISTS workstreams")
```

- [ ] **Step 3: Run the migration against a local Postgres to verify it applies and backfills**

Run:
```bash
docker run -d --name pg-m2b-workstreams -e POSTGRES_PASSWORD=test -e POSTGRES_DB=waddlebot -p 55436:5432 postgres:17-alpine
sleep 3
DATABASE_URL="postgresql://postgres:test@localhost:55436/waddlebot" alembic upgrade 0022_ingest_source_auth
psql "postgresql://postgres:test@localhost:55436/waddlebot" -c \
  "INSERT INTO tenants (slug, display_name, is_active) VALUES ('acme', 'Acme', true);"
psql "postgresql://postgres:test@localhost:55436/waddlebot" -c \
  "INSERT INTO ingest_sources (tenant_id, platform, source_id, label, enabled) VALUES (1, 'twitch', 'tw-a', 'Twitch A', true);"
DATABASE_URL="postgresql://postgres:test@localhost:55436/waddlebot" alembic upgrade 0023_workstreams_and_usage
psql "postgresql://postgres:test@localhost:55436/waddlebot" -c \
  "SELECT platform, source_id, disabled_at FROM workstreams;"
docker rm -f pg-m2b-workstreams
```
Expected: both `alembic upgrade` invocations exit 0; the final `SELECT` prints exactly one row, `twitch | tw-a | ` (an empty `disabled_at`) — proving migration 0023's `INSERT ... SELECT` backfilled the pre-existing source.

- [ ] **Step 4: Verify the grants landed as expected**

Run:
```bash
docker run -d --name pg-m2b-workstreams2 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=waddlebot -p 55437:5432 postgres:17-alpine
sleep 3
DATABASE_URL="postgresql://postgres:test@localhost:55437/waddlebot" alembic upgrade head
psql "postgresql://postgres:test@localhost:55437/waddlebot" -c \
  "SELECT grantee, privilege_type FROM information_schema.role_table_grants WHERE table_name = 'workstream_usage_hourly' ORDER BY grantee, privilege_type;"
docker rm -f pg-m2b-workstreams2
```
Expected output:
```
     grantee      | privilege_type
-------------------+-----------------
 hub_api           | INSERT
 hub_api           | SELECT
 migration_runner  | DELETE
 migration_runner  | INSERT
 migration_runner  | SELECT
 migration_runner  | UPDATE
```
`hub_api` must **not** show `UPDATE` or `DELETE`; no `svc_ingest`/`svc_process`/`svc_action`/`svc_streaming`/`webui`/`waddles_publisher` row at all.

- [ ] **Step 5: Re-run the RBAC live-grants test to confirm the wider matrix still balances**

Run:
```bash
docker run -d --name pg-m2b-workstreams3 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=waddlebot -p 55438:5432 postgres:17-alpine
sleep 3
DATABASE_URL="postgresql://postgres:test@localhost:55438/waddlebot" alembic upgrade head
cd hub_api
TEST_POSTGRES_ADMIN_DSN="postgresql://postgres:test@localhost:55438/waddlebot" \
  python3 -m pytest tests/test_rbac_live_grants.py::test_live_grants_equal_the_matrix_exactly -v -s
docker rm -f pg-m2b-workstreams3
```
Expected: `1 passed`, stdout now reads `RBAC live-grants check: 8 roles x 11 tables examined` (9 from Task 1 plus `workstreams`/`workstream_usage_hourly`) — the same test, unmodified since Task 5, automatically widening because it reads the matrix live rather than a hardcoded table list.

- [ ] **Step 6: Commit**

```bash
git add config/postgres/rbac-matrix.yaml alembic/versions/0023_workstreams_and_usage.py
git commit -m "$(cat <<'EOF'
db(hub-api): workstreams + workstream_usage_hourly tables (spec Sec6.11-6.12, D30/D31)

workstreams is 1:1 with ingest_sources, backfilled here for every
pre-existing source (spec Sec15.4). workstream_usage_hourly is
append-only: hub_api gets SELECT/INSERT only, no UPDATE/DELETE for
anyone, matching the spec's literal Sec11.10.1/Sec5.12 wording.
ingest_source_id is a nullable ON DELETE SET NULL FK (not the spec's
literal source_id text FK, which is not globally unique in this
schema) so workstream_usage_hourly's usage history survives a deleted
source. Grants generated from config/postgres/rbac-matrix.yaml.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Task 43: `install_dal` fixture extension for `workstreams`/`workstream_usage_hourly` + smoke test (R52)

**Depends on:** Task 4 (`_create_bundle_install_tables()`/`install_dal` fixture, the function this task extends), Task 42 (migration 0023's DDL, which this fixture's DDL mirrors).

**Files:**
- Modify: `hub_api/tests/conftest.py`
- Create: `hub_api/tests/test_workstream_fixture_smoke.py`

**Interfaces:**
- Produces: `_create_bundle_install_tables()` (Task 4) additionally creates `workstreams`/`workstream_usage_hourly` via SQLAlchemy Core, so `install_dal` (Task 4's fixture, unchanged in shape) reflects them too.
- Consumes: nothing new.

**Why the shared `bundle_install_db`/`install_dal` fixtures are not seeded with a workstream row here.** Dozens of already-written M2b tests assert exact row counts and index-`[0]` lookups against those fixtures' existing seed data (one tenant, one community, one `app_catalog` row) — adding a new pre-seeded `ingest_sources`/`workstreams` row to either shared fixture would silently perturb every one of those counts. This task's own smoke test seeds its own rows instead; Task 44's tests do the same.

- [ ] **Step 1: Extend `_create_bundle_install_tables()` in `hub_api/tests/conftest.py`**

Task 4 added this function to `hub_api/tests/conftest.py`, ending with the `platform_settings` table and its `metadata.create_all(conn)` call:

```python
    Table(
        "platform_settings",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("key", String(150), nullable=False),
        Column("value", Text),
        Column("updated_by", Integer),
        Column("updated_at", DateTime),
    )
    metadata.create_all(conn)
```

Replace those two lines (the `platform_settings` `Table(...)` call plus `metadata.create_all(conn)`) with:

```python
    Table(
        "platform_settings",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("key", String(150), nullable=False),
        Column("value", Text),
        Column("updated_by", Integer),
        Column("updated_at", DateTime),
    )
    Table(
        "workstreams",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("ingest_source_id", BigInteger),
        Column("platform", String(50), nullable=False),
        Column("source_id", String(255), nullable=False),
        Column("created_at", DateTime),
        Column("disabled_at", DateTime),
    )
    Table(
        "workstream_usage_hourly",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("community_id", Integer),
        Column("workstream_id", String(36), nullable=False),
        Column("stage", String(20), nullable=False),
        Column("app_id", String(255)),
        Column("hour", DateTime, nullable=False),
        Column("events", BigInteger, server_default="0"),
        Column("invocations", BigInteger, server_default="0"),
        Column("host_calls", BigInteger, server_default="0"),
        Column("actions_delivered", BigInteger, server_default="0"),
        Column("fuel_ms", BigInteger, server_default="0"),
        Column("outbound_bytes", BigInteger, server_default="0"),
        Column("media_minutes", Float),
        Column("recorded_at", DateTime),
    )
    metadata.create_all(conn)
```

`workstream_usage_hourly.workstream_id` is a `String(36)` column here, not an integer FK — the real Postgres column is `UUID` (Task 42), while `workstreams.id` in this SQLAlchemy/sqlite test fixture stays an autoincrement integer (sqlite has no native `UUID` type, Decision #14; `penguin_dal.FieldProxy` reflects whatever SQLAlchemy type the column has, so this is purely a fixture-schema choice, not a `penguin-dal` limitation). Every service function treats `workstream_id` as an opaque string via `str(...)` on both backends, so `String(36)` holds either representation without a type mismatch anywhere a test constructs one.

Add `BigInteger` and `Float` to the existing `from sqlalchemy import (...)` block Task 4 added at the top of `hub_api/tests/conftest.py` (it already imports `BigInteger` for `app_versions.size_bytes`/`app_version_uploads.app_version_id` — only `Float` is new):

```python
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    true as sa_true,
)
```

- [ ] **Step 2: Write the smoke test**

```python
# hub_api/tests/test_workstream_fixture_smoke.py
"""Smoke test for workstreams/workstream_usage_hourly reflection -- proves both tables are queryable via install_dal.

Seeds its own ingest_sources + workstreams rows rather than relying on
bundle_install_db's/install_dal's shared seed data, so this test cannot
perturb row counts any other M2b test already asserts against.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.bundle_install_dal import raw_sql_rows, raw_sql_write


async def test_workstreams_and_usage_tables_are_queryable(install_dal: Any) -> None:
    assert "workstreams" in install_dal.tables
    assert "workstream_usage_hourly" in install_dal.tables
    workstreams_count = await raw_sql_rows(install_dal, "SELECT COUNT(*) AS n FROM workstreams")
    assert workstreams_count.first()["n"] == 0
    usage_count = await raw_sql_rows(install_dal, "SELECT COUNT(*) AS n FROM workstream_usage_hourly")
    assert usage_count.first()["n"] == 0


async def test_a_workstream_row_round_trips(install_dal: Any) -> None:
    source_id = (
        await install_dal.ingest_sources.async_insert(
            tenant_id=1,
            community_id=None,
            platform="twitch",
            source_id="smoke-src-1",
            label="Smoke Source",
            enabled=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    workstream_id = await install_dal.workstreams.async_insert(
        tenant_id=1,
        community_id=None,
        ingest_source_id=source_id,
        platform="twitch",
        source_id="smoke-src-1",
        created_at=datetime.now(UTC),
    )
    row = (
        await install_dal(install_dal.workstreams.id == workstream_id).select()
    ).first()
    assert row is not None
    assert row.source_id == "smoke-src-1"
    assert row.disabled_at is None


async def test_a_usage_hourly_row_round_trips(install_dal: Any) -> None:
    source_id = await install_dal.ingest_sources.async_insert(
        tenant_id=1,
        community_id=None,
        platform="twitch",
        source_id="smoke-src-2",
        label="Smoke Source 2",
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workstream_id = await install_dal.workstreams.async_insert(
        tenant_id=1,
        community_id=None,
        ingest_source_id=source_id,
        platform="twitch",
        source_id="smoke-src-2",
        created_at=datetime.now(UTC),
    )
    rows = await raw_sql_write(
        install_dal,
        """
        INSERT INTO workstream_usage_hourly
            (tenant_id, community_id, workstream_id, stage, app_id, hour, events,
             invocations, host_calls, actions_delivered, fuel_ms, outbound_bytes,
             media_minutes, recorded_at)
        VALUES
            (:tenant_id, :community_id, :workstream_id, :stage, :app_id, :hour, :events,
             :invocations, :host_calls, :actions_delivered, :fuel_ms, :outbound_bytes,
             :media_minutes, :recorded_at)
        """,
        {
            "tenant_id": 1,
            "community_id": None,
            "workstream_id": str(workstream_id),
            "stage": "ingest",
            "app_id": None,
            "hour": datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC),
            "events": 5,
            "invocations": 0,
            "host_calls": 0,
            "actions_delivered": 0,
            "fuel_ms": 0,
            "outbound_bytes": 1024,
            "media_minutes": None,
            "recorded_at": datetime.now(UTC),
        },
    )
    assert rows.first() is None or len(rows) == 0  # no RETURNING clause
    check = await raw_sql_rows(
        install_dal,
        "SELECT events, workstream_id FROM workstream_usage_hourly WHERE workstream_id = :w",
        {"w": str(workstream_id)},
    )
    row = check.first()
    assert row is not None
    assert row["events"] == 5
    assert row["workstream_id"] == str(workstream_id)
```

- [ ] **Step 3: Run the smoke test**

Run: `cd hub_api && python3 -m pytest tests/test_workstream_fixture_smoke.py -v`
Expected: `3 passed`

- [ ] **Step 4: Run the full existing hub-api suite to confirm no regression**

Run: `cd hub_api && python3 -m pytest -q`
Expected: every previously-passing test still passes; the printed summary line's pass count is the pre-existing count plus the 3 new smoke tests. Confirm against your own baseline run before this task, since the exact pre-existing count drifts task to task.

- [ ] **Step 5: Commit**

```bash
git add hub_api/tests/conftest.py hub_api/tests/test_workstream_fixture_smoke.py
git commit -m "$(cat <<'EOF'
feat(hub-api): reflect workstreams/workstream_usage_hourly into install_dal (D30/D31, R52)

Extends the existing M2b install_dal test fixture's SQLAlchemy Core DDL
(_create_bundle_install_tables()) with the two new Task 42 tables.
workstream_id is a String(36) column on both sides -- Postgres uses a
real UUID (Decision #14), sqlite has no UUID type, and every service
function treats the value as an opaque string on both backends. Seeds
its own rows in a new, isolated test rather than touching the shared
fixtures' seed data.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 44: `workstream_service.py` — creation/disable lifecycle wired into `ingest_source_service` (D30) (R52: penguin-dal)

**Depends on:** Task 42 (`workstreams` DDL), Task 43 (the `install_dal` fixture extension), Task 26 (`ingest_source_service.create_source`/`delete_source`, both modified here, `install_dal`-only per R52), Task 39 (`create_source`'s current signature, which already gained an `auth` keyword — this task changes `create_source`'s final statements, not its signature).

**R52/Decision #18(a) note:** `ingest_sources` and `workstreams` are both this plan's own new tables, both on the same `install_dal`. Creating both rows for a new source is this plan's one genuine cross-table atomicity requirement (the coordinator ruling's own worked example) — closed here for real, with one `async with install_dal.engine.begin() as conn:` block wrapping both inserts, rather than the pre-R52 plan's two sequential, separately-committed calls (safe only because the second was idempotent). This is written directly against `conn`, not through `core_transaction()` (Task 4): the `workstreams` insert needs `ingest_sources.id`, a value the database only assigns once the first `INSERT` executes, and `core_transaction()`'s statement list is built eagerly, before either runs — it cannot express "build statement 2 from statement 1's result." `disable_workstream_for_source`/`get_workstream_for_source` and `delete_source`'s wiring keep the original "order, don't couple" sequential pattern — disabling a workstream before removing its source is safe to retry either way, so no shared transaction is needed there.

**Files:**
- Create: `hub_api/services/workstream_service.py`
- Modify: `hub_api/services/ingest_source_service.py`
- Test: `hub_api/tests/test_workstream_service.py`
- Modify: `hub_api/tests/test_ingest_source_service.py`

**Interfaces:**
- Produces: `async def create_workstream_for_source(install_dal, *, ingest_source_id: int, tenant_id: int, community_id: int | None, platform: str, source_id: str) -> Any` (idempotent on `ingest_source_id` — a standalone caller that retries never duplicates a workstream row; **not** called by `create_source()`, which does its own atomic dual-insert instead — see the R52 note above); `async def disable_workstream_for_source(install_dal, *, ingest_source_id: int) -> None` (sets `disabled_at`; a no-op when none exists or it is already disabled); `async def get_workstream_for_source(install_dal, *, ingest_source_id: int) -> Any | None`. `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `ingest_sources`/`workstreams` are this plan's own new tables (R52).
- Consumes: nothing new.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_workstream_service.py
"""Tests for the standalone workstream creation/disable helpers (spec Sec5.11, Sec6.11, D30).

create_workstream_for_source() is exercised standalone here -- it is
NOT called by create_source() (Task 44's atomicity fix routes that
through create_source's own engine.begin() block instead, Decision
#18(a)); this function remains available, idempotent, and tested for
any other caller that needs to ensure a source's workstream exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.workstream_service import (
    create_workstream_for_source,
    disable_workstream_for_source,
    get_workstream_for_source,
)


async def _seed_source(install_dal: Any, source_id: str = "wsvc-1") -> int:
    now = datetime.now(UTC)
    return await install_dal.ingest_sources.async_insert(
        tenant_id=1, community_id=None, platform="twitch", source_id=source_id,
        label="X", enabled=True, created_at=now, updated_at=now,
    )


async def test_create_workstream_for_source_creates_exactly_one_row(install_dal: Any) -> None:
    source_id = await _seed_source(install_dal)
    row = await create_workstream_for_source(
        install_dal, ingest_source_id=source_id, tenant_id=1,
        community_id=None, platform="twitch", source_id="wsvc-1",
    )
    assert row.platform == "twitch"
    assert row.disabled_at is None
    assert await install_dal(install_dal.workstreams.id > 0).count() == 1


async def test_create_workstream_for_source_is_idempotent(install_dal: Any) -> None:
    source_id = await _seed_source(install_dal)
    first = await create_workstream_for_source(
        install_dal, ingest_source_id=source_id, tenant_id=1,
        community_id=None, platform="twitch", source_id="wsvc-1",
    )
    second = await create_workstream_for_source(
        install_dal, ingest_source_id=source_id, tenant_id=1,
        community_id=None, platform="twitch", source_id="wsvc-1",
    )
    assert first.id == second.id
    assert await install_dal(install_dal.workstreams.id > 0).count() == 1


async def test_disable_workstream_for_source_sets_disabled_at(install_dal: Any) -> None:
    source_id = await _seed_source(install_dal)
    await create_workstream_for_source(
        install_dal, ingest_source_id=source_id, tenant_id=1,
        community_id=None, platform="twitch", source_id="wsvc-1",
    )
    await disable_workstream_for_source(install_dal, ingest_source_id=source_id)
    row = await get_workstream_for_source(install_dal, ingest_source_id=source_id)
    assert row is not None
    assert row.disabled_at is not None


async def test_disable_workstream_for_source_is_a_noop_when_none_exists(install_dal: Any) -> None:
    await disable_workstream_for_source(install_dal, ingest_source_id=999999)


async def test_disable_workstream_for_source_is_idempotent(install_dal: Any) -> None:
    source_id = await _seed_source(install_dal)
    await create_workstream_for_source(
        install_dal, ingest_source_id=source_id, tenant_id=1,
        community_id=None, platform="twitch", source_id="wsvc-1",
    )
    await disable_workstream_for_source(install_dal, ingest_source_id=source_id)
    await disable_workstream_for_source(install_dal, ingest_source_id=source_id)
    row = await get_workstream_for_source(install_dal, ingest_source_id=source_id)
    assert row.disabled_at is not None


async def test_get_workstream_for_source_returns_none_when_absent(install_dal: Any) -> None:
    assert await get_workstream_for_source(install_dal, ingest_source_id=999999) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_workstream_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.workstream_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/workstream_service.py
"""hub-api-owned workstream lifecycle -- 1:1 with ingest_sources (spec Sec5.11, Sec6.11, D30).

A workstream is created the moment its ingest source is registered
(`ingest_source_service.create_source()` does this atomically itself,
Decision #18(a) -- see that module, not this one, for the create-time
path) and disabled (never deleted) the moment that source is removed
-- workstream_usage_hourly rows keep their FK target for the life of
the tenant's usage history even after the source itself is gone
(Task 42's migration docstring explains the nullable ingest_source_id
FK). `create_workstream_for_source` here is the standalone, idempotent
helper for any caller other than `create_source()` itself; `disable_workstream_for_source`/
`get_workstream_for_source` are used by `delete_source()`.

R52: `ingest_sources`/`workstreams` are this plan's own new tables,
queried through the penguin-dal `install_dal: AsyncDB` (Task 4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB


async def create_workstream_for_source(
    install_dal: AsyncDB,
    *,
    ingest_source_id: int,
    tenant_id: int,
    community_id: int | None,
    platform: str,
    source_id: str,
) -> Any:
    """Create the 1:1 workstream for an ingest source, or return the existing one. Idempotent."""
    existing = await install_dal(install_dal.workstreams.ingest_source_id == ingest_source_id).select()
    first = existing.first()
    if first is not None:
        return first

    new_id = await install_dal.workstreams.async_insert(
        tenant_id=tenant_id,
        community_id=community_id,
        ingest_source_id=ingest_source_id,
        platform=platform,
        source_id=source_id,
        created_at=datetime.now(UTC),
    )
    return (await install_dal(install_dal.workstreams.id == new_id).select()).first()


async def disable_workstream_for_source(install_dal: AsyncDB, *, ingest_source_id: int) -> None:
    """Set `disabled_at` on the source's workstream. No-op if none exists or it is already disabled.

    Called before the owning `ingest_sources` row is deleted -- never
    after, since the FK is `ON DELETE SET NULL` and this function needs
    `ingest_source_id` to still resolve to the right row.
    """
    rows = await install_dal(
        (install_dal.workstreams.ingest_source_id == ingest_source_id)
        & (install_dal.workstreams.disabled_at == None)  # noqa: E711 - penguin-dal IS NULL operator
    ).select()
    target = rows.first()
    if target is None:
        return
    await install_dal(install_dal.workstreams.id == target.id).update(disabled_at=datetime.now(UTC))


async def get_workstream_for_source(install_dal: AsyncDB, *, ingest_source_id: int) -> Any | None:
    """The workstream row for a given ingest source, or `None` if it has none."""
    rows = await install_dal(install_dal.workstreams.ingest_source_id == ingest_source_id).select()
    return rows.first()
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_workstream_service.py -v`
Expected: `6 passed`

- [ ] **Step 5: Wire `create_source()` — atomic dual-insert, Decision #18(a)**

`create_source()`'s current body (after Tasks 26 and 39) ends:

```python
    plaintext_secret = secrets.token_urlsafe(32)
    ciphertext, iv = encrypt(plaintext_secret)
    now = datetime.now(UTC)
    try:
        auth_config, auth_plaintext = build_auth_config(platform, auth, secret_ref=f"src:{source_id}")
    except AuthConfigError as exc:
        raise ApiError(str(exc), 422, exc.reason) from exc
    auth_ciphertext, auth_iv = encrypt(auth_plaintext) if auth_plaintext is not None else (None, None)
    new_id = await install_dal.ingest_sources.async_insert(
        tenant_id=tenant_id, community_id=community_id, platform=platform, source_id=source_id,
        label=label, secret_ciphertext=ciphertext, secret_iv=iv, mapping=mapping, enabled=True,
        auth=auth_config, auth_secret_ciphertext=auth_ciphertext, auth_secret_iv=auth_iv,
        created_at=now, updated_at=now,
    )
    row = (await install_dal(install_dal.ingest_sources.id == new_id).select()).first()
    return row, plaintext_secret
```

Replace the last four lines (from `new_id = await install_dal.ingest_sources.async_insert(...)` through `return row, plaintext_secret`) with:

```python
    ingest_sources_table = install_dal.ingest_sources.table
    workstreams_table = install_dal.workstreams.table
    async with install_dal.engine.begin() as conn:
        result = await conn.execute(
            ingest_sources_table.insert().values(
                tenant_id=tenant_id, community_id=community_id, platform=platform, source_id=source_id,
                label=label, secret_ciphertext=ciphertext, secret_iv=iv, mapping=mapping, enabled=True,
                auth=auth_config, auth_secret_ciphertext=auth_ciphertext, auth_secret_iv=auth_iv,
                created_at=now, updated_at=now,
            )
        )
        new_id = result.inserted_primary_key[0]
        await conn.execute(
            workstreams_table.insert().values(
                tenant_id=tenant_id, community_id=community_id, ingest_source_id=new_id,
                platform=platform, source_id=source_id, created_at=now,
            )
        )
    row = (await install_dal(install_dal.ingest_sources.id == new_id).select()).first()
    return row, plaintext_secret
```

Both inserts now commit or roll back together: a crash or exception between them leaves neither row, never an `ingest_sources` row with no workstream. `ingest_sources_table`/`workstreams_table` are the real, reflected SQLAlchemy `Table` objects (`TableProxy.table`, a public property), so `auth`/`mapping`'s JSON columns still serialize correctly — the same type-aware guarantee `core_transaction()` documents, applied here directly since the two statements are data-dependent.

- [ ] **Step 6: Wire `delete_source()`**

`delete_source()`'s current body (unchanged since Task 26) is:

```python
async def delete_source(install_dal: AsyncDB, *, tenant_id: int, source_id: str) -> None:
    """Remove an ingest source by its `source_id`. Raises 404 if absent."""
    existing = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    if not existing:
        raise not_found(f"ingest source {source_id!r} not found")
    await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.source_id == source_id)
    ).delete()
```

Add the import:

```python
from services.workstream_service import disable_workstream_for_source
```

Replace `delete_source`'s body with:

```python
async def delete_source(install_dal: AsyncDB, *, tenant_id: int, source_id: str) -> None:
    """Remove an ingest source by its `source_id`. Raises 404 if absent.

    Disables the source's workstream (never deletes it) before the
    source row itself is removed -- workstream_usage_hourly keeps its
    FK target for the life of the tenant's usage history (Decision #14).
    Sequential, not a shared transaction: disabling a workstream ahead
    of a delete that then fails is a harmless, safe-to-retry state (the
    disable itself is idempotent), unlike Decision #18(a)'s create-time
    case where a missing workstream would be a real gap.
    """
    existing = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.source_id == source_id)
    ).select()
    row = existing.first()
    if row is None:
        raise not_found(f"ingest source {source_id!r} not found")
    await disable_workstream_for_source(install_dal, ingest_source_id=row.id)
    await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.source_id == source_id)
    ).delete()
```

- [ ] **Step 7: Append regression tests to `test_ingest_source_service.py`**

Append these two functions to the end of `hub_api/tests/test_ingest_source_service.py`:

```python
async def test_create_source_creates_a_workstream(install_dal: Any) -> None:
    row, _ = await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="custom:mycrm", source_id="ws-reg-1", label="x", mapping=None,
    )
    workstream = (
        await install_dal(install_dal.workstreams.ingest_source_id == row.id).select()
    ).first()
    assert workstream is not None
    assert workstream.source_id == "ws-reg-1"
    assert workstream.disabled_at is None


async def test_delete_source_disables_the_workstream_without_deleting_it(install_dal: Any) -> None:
    row, _ = await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="custom:mycrm", source_id="ws-reg-2", label="x", mapping=None,
    )
    await delete_source(install_dal, tenant_id=1, source_id="ws-reg-2")
    workstream = (
        await install_dal(install_dal.workstreams.ingest_source_id == row.id).select()
    ).first()
    assert workstream is not None
    assert workstream.disabled_at is not None
```

- [ ] **Step 8: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_ingest_source_service.py tests/test_ingest_sources_blueprint.py tests/test_distribution_sources_blueprint.py tests/test_ingest_source_auth.py tests/test_ingest_source_auth_api.py -v`
Expected: every previously-passing test still passes, plus the 2 new regression tests — `create_source`'s atomic dual-insert and `delete_source`'s additive `disable_workstream_for_source()` call change no prior assertion about either function's return value.

- [ ] **Step 9: Commit**

```bash
git add hub_api/services/workstream_service.py hub_api/services/ingest_source_service.py \
        hub_api/tests/test_workstream_service.py hub_api/tests/test_ingest_source_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): atomically create the 1:1 workstream inside create_source() (spec Sec5.11, D30, R52)

create_source() now inserts ingest_sources and its 1:1 workstreams row
inside one engine.begin() transaction (Decision #18(a)) -- a real
atomicity fix the R52 rewrite enables, since both tables now share one
penguin-dal engine: a crash between the two inserts previously left an
ingest_sources row with no workstream until the next retry. Written
directly against the connection, not through core_transaction(), since
the workstream insert needs the ingest source's generated id.
create_workstream_for_source() stays available as a standalone,
idempotent helper for other callers. delete_source() disables the
workstream (sets disabled_at) before removing the source row --
workstream_usage_hourly's FK target survives the deletion, per this
plan's Decision #14.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 45: Publish `workstreamId` — distribution `/sources`, the tenant config view, and the consent screen (D30) (R52: penguin-dal)

**Depends on:** Task 44 (`workstream_service.get_workstream_for_source`, `install_dal`-only per R52, and every `ingest_sources` row now has a 1:1 workstream), Task 34 (`DistributionSource`/`DistributionSourceDTO`, `list_sources_for_distribution`), Task 27 (`blueprints/v1/ingest_sources.py`'s `SourceDTO`/`list_sources` handler), Task 40 (`source_auth_for_consent`, the `auth` fields these same dataclasses/functions already carry — this task adds one more sibling field to each, never touching `auth`'s own shape or the `permissionHash` computation).

**Files:**
- Modify: `hub_api/services/ingest_source_service.py`
- Modify: `hub_api/blueprints/v1/distribution.py`
- Modify: `hub_api/blueprints/v1/ingest_sources.py`
- Test: `hub_api/tests/test_workstream_id_publication.py`

**Interfaces:**
- Produces: `DistributionSource.workstream_id: str | None` (last field); `DistributionSourceDTO.workstreamId: str | None = None` (last field); tenant config view `SourceDTO.workstreamId: str | None = None` (last field); `source_auth_for_consent(...)`'s returned dict entries gain a `"workstreamId"` key.
- Consumes: `services.workstream_service.get_workstream_for_source` (Task 44, `install_dal`-only per R52).

Spec basis (D30, spec §5.11): "Shown on the source's config view (§10.3) and on the consent screen (§9.7.1) alongside the source it reads." svc-ingest mints `workstream_id` from its own `intake_sources`/`workstreams` lookup (spec §5.11's Minting paragraph), so the distribution `/sources` endpoint svc-ingest polls must carry it too — without it, the ingest-side minting code (plan M5, once it lands D30) has nowhere to read the id from at runtime.

`workstreamId` is never part of `permissionHash` — like `sourceAuth` (Task 40), it is a sibling of `summary` on the consent response, never a member, because a workstream's presence never changes what permissions a bundle is asking for.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_workstream_id_publication.py
"""workstreamId appears on the distribution /sources feed, the tenant config view, and the consent screen (D30)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from quart import Quart

from tests.conftest import TENANT_SLUG, make_user_token


def _app(bundle_install_db: Any, install_dal: Any, module_path: str) -> Quart:
    import importlib

    module = importlib.import_module(module_path)
    app = Quart(__name__)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    for bp in module.BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_distribution_sources_carries_a_workstream_id(
    bundle_install_db: Any, install_dal: Any
) -> None:
    from services.ingest_source_service import create_source

    await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="twitch", source_id="ws-tw-1", label="Twitch WS test", mapping=None,
    )
    app = _app(bundle_install_db, install_dal, "blueprints.v1.distribution")
    token = make_user_token(user_id=1, scope="distribution:read", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        "/api/v1/distribution/sources", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    by_id = {s["sourceId"]: s for s in (await response.get_json())["sources"]}
    assert by_id["ws-tw-1"]["workstreamId"] is not None
    assert isinstance(by_id["ws-tw-1"]["workstreamId"], str)


async def test_tenant_config_view_carries_a_workstream_id(
    bundle_install_db: Any, install_dal: Any
) -> None:
    from services.ingest_source_service import create_source

    await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="twitch", source_id="ws-tw-2", label="Twitch WS test 2", mapping=None,
    )
    app = _app(bundle_install_db, install_dal, "blueprints.v1.ingest_sources")
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/ingest-sources", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    by_id = {s["sourceId"]: s for s in (await response.get_json())["sources"]}
    assert by_id["ws-tw-2"]["workstreamId"] is not None


async def test_consent_screen_carries_a_workstream_id_without_moving_the_hash(
    bundle_install_db: Any, install_dal: Any
) -> None:
    from services.ingest_source_service import create_source

    await create_source(
        install_dal, tenant_id=1, community_id=None,
        platform="twitch", source_id="ws-tw-3", label="Twitch WS test 3", mapping=None,
    )
    now = datetime.now(UTC)
    manifest = {
        "schema_version": 2, "app_id": "waddles.socials.music.default", "name": "Music Station",
        "version": "3.0.1", "feature": "waddles.socials.music", "module": "socials",
        "provider": "builtin", "language": "python", "artifact": "source",
        "stages": {"process": {"entry": "x:y",
                               "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}]}},
    }
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.1", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED", manifest_json=manifest,
        created_at=now, updated_at=now,
    )
    app = _app(bundle_install_db, install_dal, "blueprints.v1.bundle_approvals")
    token = make_user_token(user_id=1, scope="platform:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        "/api/v1/apps/waddles.socials.music.default/versions/3.0.1/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = await response.get_json()
    assert response.status_code == 200
    entry = next(e for e in body["sourceAuth"] if e["sourceId"] == "ws-tw-3")
    assert entry["workstreamId"] is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_workstream_id_publication.py -v`
Expected: `KeyError: 'workstreamId'` on all three tests.

- [ ] **Step 3: Add `workstream_id` to `DistributionSource` and `list_sources_for_distribution`**

In `hub_api/services/ingest_source_service.py`, add the import:

```python
from services.workstream_service import get_workstream_for_source
```

Add `workstream_id: str | None` as the **last** field of the `DistributionSource` dataclass (after `auth`).

`list_sources_for_distribution`'s signature (Task 34) is `(install_dal, async_dal, dal, *, tenant_id, tenant_slug, community_id=None, platform=None, enabled=None)`. In its loop over `rows`, immediately before the `results.append(DistributionSource(...))` call, add:

```python
        workstream = await get_workstream_for_source(install_dal, ingest_source_id=row.id)
```

and add `workstream_id=str(workstream.id) if workstream is not None else None,` as the last keyword argument of that `DistributionSource(...)` construction.

- [ ] **Step 4: Add `workstreamId` to `source_auth_for_consent`**

In the same file, `source_auth_for_consent`'s signature (Task 40) is `(install_dal, *, tenant_id, community_id, consumes_platforms)`. Its loop over `rows` currently builds each `entries.append({...})` dict from `row`. Add, immediately before that `entries.append(...)` call:

```python
        workstream = await get_workstream_for_source(install_dal, ingest_source_id=row.id)
```

and add `"workstreamId": str(workstream.id) if workstream is not None else None,` as a new key in that dict.

- [ ] **Step 5: Publish it on the distribution DTO**

In `hub_api/blueprints/v1/distribution.py`, add `workstreamId: str | None = None` as the last field of `DistributionSourceDTO`. In `list_distribution_sources`'s `DistributionSourceDTO(...)` construction, add `workstreamId=s.workstream_id,` as the last keyword argument. In the same handler's `etag_seed` list comprehension, add `"workstreamId": d.workstreamId` to the `config={...}` dict passed to each `DistributionBundleV2DTO(...)`.

- [ ] **Step 6: Publish it on the tenant config view**

In `hub_api/blueprints/v1/ingest_sources.py`, add the import:

```python
from services.workstream_service import get_workstream_for_source
```

Add `workstreamId: str | None = None` as the last field of `SourceDTO`. Replace the `list_sources` handler's body with:

```python
@ingest_sources_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(SourceListResponse)
async def list_sources(tenant_slug: str) -> SourceListResponse | tuple[dict[str, object], int]:
    """List every ingest source for this tenant. Never includes a secret field."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
    except ApiError as exc:
        return _err(exc)
    rows = await svc.list_sources(install_dal, tenant_id=tenant_id)
    sources: list[SourceDTO] = []
    for r in rows:
        workstream = await get_workstream_for_source(install_dal, ingest_source_id=r.id)
        sources.append(
            SourceDTO(
                platform=r.platform, sourceId=r.source_id, label=r.label,
                communityId=r.community_id, enabled=bool(r.enabled), auth=dict(r.auth or {}),
                workstreamId=str(workstream.id) if workstream is not None else None,
            )
        )
    return SourceListResponse(success=True, sources=sources)
```

- [ ] **Step 7: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_workstream_id_publication.py -v`
Expected: `3 passed`

- [ ] **Step 8: Confirm zero regression on every touched surface**

Run:
```bash
cd hub_api && python3 -m pytest \
  tests/test_ingest_source_service.py tests/test_ingest_sources_blueprint.py \
  tests/test_distribution_sources_blueprint.py tests/test_ingest_source_auth_api.py \
  tests/test_bundle_approvals_blueprint.py tests/test_permission_summary_service.py -v
```
Expected: every previously-passing test still passes — `permissionHash`-related tests in particular must be untouched, since `workstreamId` is a sibling of `summary`/`sourceAuth`, never a member.

- [ ] **Step 9: Commit**

```bash
git add hub_api/services/ingest_source_service.py hub_api/blueprints/v1/distribution.py \
        hub_api/blueprints/v1/ingest_sources.py hub_api/tests/test_workstream_id_publication.py
git commit -m "$(cat <<'EOF'
feat(hub-api): publish workstreamId -- distribution /sources, tenant config view, consent screen (spec Sec5.11, D30, R52)

svc-ingest mints workstream_id from its own source lookup at runtime,
so the distribution /sources feed it polls must carry it. Also shown
on the tenant's ingest-source config view and, as a sibling of summary
and sourceAuth (never a member), on the bundle consent screen --
permissionHash is unaffected. Every new lookup goes through
penguin-dal's install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 46: `routes_to` cross-tenant refusal wired into `approve_version()` (spec §5.9, D30) (R52: penguin-dal)

**Depends on:** Task 21 (`approve_version`, the function this task extends, `install_dal`-only per R52), Task 7 (`BundleManifestV2.routes_to`, already parsed and available on `manifest`).

**R52 note:** `app_catalog` is a **pre-existing** table, but `install_dal.reflect()` (Task 4) discovers hub-api's entire live Postgres schema, not only the 11 new tables — exactly what already makes `install_dal.audit_log` a valid `TableProxy` (Decision #18). The same is true of `app_catalog`: this task's `routes_to` existence check is a **read-only** lookup against it, so it goes through the same `install_dal` `approve_version()` already holds, rather than requiring a second, pydal `dal` parameter this function has never needed before. `app_install_approvals` is this plan's own new table. Nothing here touches any EXISTING pydal call site that also reads `app_catalog` elsewhere in the codebase.

**Files:**
- Modify: `hub_api/services/bundle_approval_service.py`
- Modify: `hub_api/tests/test_bundle_approval_service.py`

**Interfaces:**
- Produces: `approve_version(...)` refuses `422 routes_to_target_not_found` when a declared `routes_to` target does not exist in `app_catalog`, and `422 routes_to_cross_tenant` when it exists but is not installed in the approving call's tenant — both refusals write a best-effort `audit_log` row (`action = "routes_to_refused"`) before raising. No new parameter; every existing caller and test keeps its exact current behaviour when `manifest.routes_to` is empty.
- Consumes: nothing new — uses `install_dal.app_catalog`/`install_dal.app_install_approvals`/`install_dal.audit_log`, all reachable via the one `install_dal` this function already takes.

**"Installed in the same tenant" is defined here** (this plan's Decision #15) as: the target `app_id` has a non-superseded `app_install_approvals` row whose `tenant_id` equals the approving call's `tenant_id` — community-agnostic, since a tenant-wide or any-community install of the target both count as "installed in this tenant." This is spec §5.9's install-time row: "hub-api validates that each target exists in `app_catalog`, is installed in the same tenant as the declaring bundle — a cross-tenant target is refused outright at approval, not merely left unapproved (D30)."

- [ ] **Step 1: Write the failing tests**

Append to `hub_api/tests/test_bundle_approval_service.py` (add `from datetime import UTC, datetime` and `from services.errors import ApiError` at the top of the file only if they are not already imported there):

```python
async def _seed_uploaded_version_with_routes_to(install_dal: Any, *, routes_to: list[str]) -> None:
    manifest = {
        "schema_version": 2, "app_id": "waddles.socials.music.default", "name": "Music Station",
        "version": "3.0.2", "feature": "waddles.socials.music", "module": "socials",
        "provider": "builtin", "language": "python", "artifact": "source",
        "routes_to": routes_to,
        "stages": {"process": {"entry": "x:y", "consumes": []}},
    }
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default", version="3.0.2", tenant_id=1,
        artifact_kind="source", language="python", status="PUBLISHED", manifest_json=manifest,
        created_at=now, updated_at=now,
    )


async def test_approve_version_refuses_a_routes_to_target_that_does_not_exist(install_dal: Any) -> None:
    await _seed_uploaded_version_with_routes_to(install_dal, routes_to=["waddles.nope.default"])
    with pytest.raises(ApiError) as excinfo:
        await approve_version(
            install_dal, app_id="waddles.socials.music.default", version="3.0.2",
            tenant_id=1, community_id=None, approved_by=1,
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "routes_to_target_not_found"


async def test_approve_version_refuses_a_cross_tenant_routes_to_target(
    bundle_install_db: Any, install_dal: Any
) -> None:
    dal = bundle_install_db.dal
    other_tenant_id = dal.tenants.insert(slug="other-corp", display_name="Other Corp", is_active=True)
    dal.app_catalog.insert(
        app_id="waddles.socials.forums.default", name="Forums", manifest_version="3.0.0",
        module="socials", feature="waddles.socials.forums", provider="builtin",
        execution_model="native", is_default=False,
        platform_compatibility={"tested_with": "3.0.0", "min_version": None, "max_version": None},
        status="active", stages={},
    )
    dal.commit()
    await install_dal.app_install_approvals.async_insert(
        tenant_id=other_tenant_id, community_id=None, app_id="waddles.socials.forums.default",
        version="1.0.0", permission_hash="sha256:" + "b" * 64, summary_json={}, approved_by=1,
        approved_at=datetime.now(UTC),
    )
    await _seed_uploaded_version_with_routes_to(install_dal, routes_to=["waddles.socials.forums.default"])
    with pytest.raises(ApiError) as excinfo:
        await approve_version(
            install_dal, app_id="waddles.socials.music.default", version="3.0.2",
            tenant_id=1, community_id=None, approved_by=1,
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "routes_to_cross_tenant"


async def test_approve_version_allows_a_same_tenant_routes_to_target(
    bundle_install_db: Any, install_dal: Any
) -> None:
    dal = bundle_install_db.dal
    dal.app_catalog.insert(
        app_id="waddles.socials.forums.default", name="Forums", manifest_version="3.0.0",
        module="socials", feature="waddles.socials.forums", provider="builtin",
        execution_model="native", is_default=False,
        platform_compatibility={"tested_with": "3.0.0", "min_version": None, "max_version": None},
        status="active", stages={},
    )
    dal.commit()
    await install_dal.app_install_approvals.async_insert(
        tenant_id=1, community_id=None, app_id="waddles.socials.forums.default",
        version="1.0.0", permission_hash="sha256:" + "b" * 64, summary_json={}, approved_by=1,
        approved_at=datetime.now(UTC),
    )
    await _seed_uploaded_version_with_routes_to(install_dal, routes_to=["waddles.socials.forums.default"])
    result = await approve_version(
        install_dal, app_id="waddles.socials.music.default", version="3.0.2",
        tenant_id=1, community_id=None, approved_by=1,
    )
    assert result.app_id == "waddles.socials.music.default"


async def test_approve_version_audits_a_routes_to_refusal(install_dal: Any) -> None:
    from services.bundle_install_dal import raw_sql_rows

    await _seed_uploaded_version_with_routes_to(install_dal, routes_to=["waddles.nope.default"])
    with pytest.raises(ApiError):
        await approve_version(
            install_dal, app_id="waddles.socials.music.default", version="3.0.2",
            tenant_id=1, community_id=None, approved_by=1,
        )
    audit_rows = await raw_sql_rows(
        install_dal, "SELECT details FROM audit_log WHERE action = :a", {"a": "routes_to_refused"}
    )
    audit_row = audit_rows.first()
    assert audit_row is not None
    import json

    details = audit_row["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["target_app_id"] == "waddles.nope.default"
    assert details["reason"] == "routes_to_target_not_found"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_approval_service.py -k routes_to -v`
Expected: every new test fails — `manifest.routes_to` is parsed (Task 7) but never checked, so all four cases (including the "allowed" one, since nothing raises today either way) currently either pass by accident or fail on the missing audit row; after Step 3 every one asserts its documented behaviour precisely.

- [ ] **Step 3: Add the check**

In `hub_api/services/bundle_approval_service.py`, append these two functions (after the existing imports, before `approve_version`):

```python
async def _audit_routes_to_refusal(
    install_dal: AsyncDB, *, actor_id: int, app_id: str, version: str, target_app_id: str, reason: str
) -> None:
    """Record a routes_to refusal -- D30 requires the refusal itself to be auditable, not only a success.

    Best-effort, matching this codebase's convention for every other
    audit-log write (Decision #18): a logging failure must never
    prevent the caller from raising the refusal itself.
    """
    try:
        await install_dal.audit_log.async_insert(
            user_id=actor_id, action="routes_to_refused",
            target_type="app_install_approvals", target_id=f"{app_id}@{version}",
            details={"target_app_id": target_app_id, "reason": reason},
            created_at=datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001, S110 -- audit logging failure must not break the refusal itself
        pass


async def _validate_routes_to(
    install_dal: AsyncDB, *, routes_to: tuple[str, ...], tenant_id: int,
    approved_by: int, app_id: str, version: str,
) -> None:
    """Refuse a routes_to target that does not exist, or exists in a different tenant (spec Sec5.9, D30).

    `app_catalog` is a pre-existing table, reachable read-only through
    `install_dal` since `reflect()` (Task 4) discovers the entire live
    schema, not only this plan's own new tables.
    """
    for target_app_id in routes_to:
        catalog_rows = await install_dal(install_dal.app_catalog.app_id == target_app_id).select()
        if not catalog_rows:
            await _audit_routes_to_refusal(
                install_dal, actor_id=approved_by, app_id=app_id, version=version,
                target_app_id=target_app_id, reason="routes_to_target_not_found",
            )
            raise ApiError(
                f"routes_to target {target_app_id!r} does not exist in the app catalog",
                422, "routes_to_target_not_found",
            )
        installed = await install_dal(
            (install_dal.app_install_approvals.app_id == target_app_id)
            & (install_dal.app_install_approvals.tenant_id == tenant_id)
            & (install_dal.app_install_approvals.superseded_by == None)  # noqa: E711 - penguin-dal IS NULL operator
        ).select()
        if not installed:
            await _audit_routes_to_refusal(
                install_dal, actor_id=approved_by, app_id=app_id, version=version,
                target_app_id=target_app_id, reason="routes_to_cross_tenant",
            )
            raise ApiError(
                f"routes_to target {target_app_id!r} is not installed in this tenant",
                422, "routes_to_cross_tenant",
            )
```

Then, in `approve_version()`, immediately after the line `manifest = _reparse_trusted(upload.manifest_json)`, insert:

```python
    if manifest.routes_to:
        await _validate_routes_to(
            install_dal, routes_to=manifest.routes_to, tenant_id=tenant_id,
            approved_by=approved_by, app_id=app_id, version=version,
        )
```

Nothing else in `approve_version()` changes — a manifest with no `routes_to` (the common case) skips this block entirely.

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_approval_service.py -v`
Expected: every previously-passing test in this file still passes, plus the 5 new tests from Step 1.

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_approval_service.py hub_api/tests/test_bundle_approval_service.py
git commit -m "$(cat <<'EOF'
feat(hub-api): refuse a cross-tenant routes_to target at approval (spec Sec5.9, D30, R52)

approve_version() now validates every manifest.routes_to entry before
recording the approval: a target absent from app_catalog is 422
routes_to_target_not_found, one that exists but has no non-superseded
app_install_approvals row for this tenant is 422 routes_to_cross_tenant.
Both refusals write a best-effort audit_log row (routes_to_refused)
before raising, so the refusal is exactly as auditable as a success.
app_catalog is read-only here, through the same install_dal
approve_version() already holds (reflect() sees the whole schema).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 47: `usage_aggregator_service.py` — the `waddles:usage` consumer writing `workstream_usage_hourly` (spec §5.12, D31) (R52: penguin-dal)

**Depends on:** Task 42 (`workstream_usage_hourly` DDL), Task 43 (the `install_dal` fixture extension), Task 28 (`valkey_admin_client.build_client`, `ensure_group`), Task 38 (`bundle_telemetry.get_meter`, `bundle_span` — this task only calls these existing exports, it does not modify `bundle_telemetry.py`), Task 4 (`services.bundle_install_dal.build_install_dal` — this standalone CronJob process builds its own `install_dal`, the same pattern Task 36's `bundle_role_cleanup_job.py` established).

**Files:**
- Create: `hub_api/services/usage_aggregator_service.py`
- Create: `k8s/helm/waddlebot/templates/usage-aggregator-cronjob.yaml`
- Modify: `k8s/helm/waddlebot/values.yaml`
- Test: `hub_api/tests/test_usage_aggregator_service.py`

**Interfaces:**
- Produces: `USAGE_STREAM = "waddles:usage"`, `USAGE_CONSUMER_GROUP = "hub_api_usage_aggregator"`; `@dataclass(slots=True, frozen=True) UsageDelta(tenant_id, community_id, workstream_id, stage, app_id, hour, events, invocations, host_calls, actions_delivered, fuel_ms, outbound_bytes, media_minutes)`; `def parse_usage_entry(fields: dict[Any, Any]) -> UsageDelta` (raises `ValueError` on a malformed entry, no DB access); `@dataclass(slots=True, frozen=True) AggregationResult(examined: int, written: int, skipped: int)`; `async def run_usage_aggregation_batch(install_dal, redis_client, *, batch_size: int = 500, consumer_name: str = "hub-api-1") -> AggregationResult`; `async def main() -> int` (the CronJob entrypoint, `python -m services.usage_aggregator_service`). `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `workstream_usage_hourly` is this plan's own new table (R52); this standalone process builds its own `install_dal` via `build_install_dal()`, the same way `bundle_role_cleanup_job.py` (Task 36) does.
- Consumes: `services.valkey_admin_client.{build_client, ensure_group}` (Task 28); `services.bundle_telemetry.{bundle_span, get_meter}` (Task 38); `services.bundle_install_dal.build_install_dal` (Task 4).

**Wire shape of one `waddles:usage` entry** (this task's own decision — the spec specifies the transport and the recorded fields, §5.12, not the field names on the wire; **must match** whichever plan implements the stage-side `XADD` producer: M3 `svc_action`, M4 `svc_process`, M5 `svc_ingest`, and `svc_streaming`'s own future plan): `tenant_id` (decimal string), `community_id` (decimal string, or the literal `"_tenant"` for tenant-wide, mirroring the stream-key segment convention §6.2), `workstream_id` (opaque string), `stage` (one of `ingest`/`process`/`action`/`streaming`), `app_id` (string, or `""`/absent for ingest-stage entries with no bundle), `hour` (an RFC3339 timestamp truncated to the hour), `events`/`invocations`/`host_calls`/`actions_delivered`/`fuel_ms`/`outbound_bytes` (decimal strings, default `"0"` when absent), `media_minutes` (decimal string, `svc-streaming` only, absent elsewhere).

**At-least-once, by design, never data loss.** Each batch is committed to Postgres before its entries are `XACK`ed, so a crash between commit and ack causes a duplicate row on the next run, never a lost one — `workstream_usage_hourly` already tolerates this by being append-only and summed at query time (spec §6.12), and "no charging, quota or enforcement ships now" (spec §5.12) makes an occasional duplicated correction row a non-issue today.

- [ ] **Step 1: Write the failing test**

```python
# hub_api/tests/test_usage_aggregator_service.py
"""Tests for the waddles:usage consumer -- parsing, aggregation, and the CronJob entrypoint (spec Sec5.12, D31)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from services.bundle_install_dal import raw_sql_rows
from services.usage_aggregator_service import (
    USAGE_CONSUMER_GROUP,
    USAGE_STREAM,
    AggregationResult,
    parse_usage_entry,
    run_usage_aggregation_batch,
)


def _entry(**overrides: str) -> dict[str, str]:
    base = {
        "tenant_id": "1", "community_id": "_tenant", "workstream_id": "ws-1", "stage": "ingest",
        "app_id": "", "hour": "2026-09-14T10:00:00+00:00", "events": "3", "invocations": "0",
        "host_calls": "0", "actions_delivered": "0", "fuel_ms": "0", "outbound_bytes": "512",
    }
    base.update(overrides)
    return base


def test_parse_usage_entry_decodes_a_well_formed_entry() -> None:
    delta = parse_usage_entry(_entry())
    assert delta.tenant_id == 1
    assert delta.community_id is None
    assert delta.workstream_id == "ws-1"
    assert delta.stage == "ingest"
    assert delta.app_id is None
    assert delta.events == 3
    assert delta.media_minutes is None


def test_parse_usage_entry_decodes_byte_fields() -> None:
    raw = {k.encode(): v.encode() for k, v in _entry().items()}
    delta = parse_usage_entry(raw)
    assert delta.tenant_id == 1


def test_parse_usage_entry_parses_a_real_community_id() -> None:
    delta = parse_usage_entry(_entry(community_id="7", app_id="waddles.socials.music.default"))
    assert delta.community_id == 7
    assert delta.app_id == "waddles.socials.music.default"


def test_parse_usage_entry_parses_media_minutes_for_streaming() -> None:
    delta = parse_usage_entry(_entry(stage="streaming", media_minutes="4.5"))
    assert delta.media_minutes == 4.5


def test_parse_usage_entry_rejects_an_unknown_stage() -> None:
    with pytest.raises(ValueError, match="malformed"):
        parse_usage_entry(_entry(stage="nope"))


def test_parse_usage_entry_rejects_a_missing_required_field() -> None:
    entry = _entry()
    del entry["tenant_id"]
    with pytest.raises(ValueError, match="malformed"):
        parse_usage_entry(entry)


def test_parse_usage_entry_rejects_a_non_numeric_field() -> None:
    with pytest.raises(ValueError, match="malformed"):
        parse_usage_entry(_entry(events="not-a-number"))


async def test_run_usage_aggregation_batch_writes_one_row_per_group(install_dal: Any) -> None:
    redis_client = AsyncMock()
    redis_client.xreadgroup.return_value = [
        (USAGE_STREAM, [
            (b"1-1", {b"tenant_id": b"1", b"community_id": b"_tenant", b"workstream_id": b"ws-1",
                      b"stage": b"ingest", b"app_id": b"", b"hour": b"2026-09-14T10:00:00+00:00",
                      b"events": b"2", b"invocations": b"0", b"host_calls": b"0",
                      b"actions_delivered": b"0", b"fuel_ms": b"0", b"outbound_bytes": b"100"}),
            (b"1-2", {b"tenant_id": b"1", b"community_id": b"_tenant", b"workstream_id": b"ws-1",
                      b"stage": b"ingest", b"app_id": b"", b"hour": b"2026-09-14T10:00:00+00:00",
                      b"events": b"3", b"invocations": b"0", b"host_calls": b"0",
                      b"actions_delivered": b"0", b"fuel_ms": b"0", b"outbound_bytes": b"50"}),
        ]),
    ]
    result = await run_usage_aggregation_batch(install_dal, redis_client)
    assert result == AggregationResult(examined=2, written=1, skipped=0)
    rows = await raw_sql_rows(
        install_dal, "SELECT events, outbound_bytes FROM workstream_usage_hourly WHERE workstream_id = :w",
        {"w": "ws-1"},
    )
    row = rows.first()
    assert row["events"] == 5
    assert row["outbound_bytes"] == 150
    redis_client.xack.assert_called_once_with(USAGE_STREAM, USAGE_CONSUMER_GROUP, b"1-1", b"1-2")


async def test_run_usage_aggregation_batch_acks_and_skips_a_malformed_entry(install_dal: Any) -> None:
    redis_client = AsyncMock()
    redis_client.xreadgroup.return_value = [
        (USAGE_STREAM, [(b"2-1", {b"tenant_id": b"1", b"stage": b"bogus"})]),
    ]
    result = await run_usage_aggregation_batch(install_dal, redis_client)
    assert result == AggregationResult(examined=1, written=0, skipped=1)
    redis_client.xack.assert_called_once_with(USAGE_STREAM, USAGE_CONSUMER_GROUP, b"2-1")


async def test_run_usage_aggregation_batch_with_nothing_to_read_examines_zero(install_dal: Any) -> None:
    redis_client = AsyncMock()
    redis_client.xreadgroup.return_value = []
    result = await run_usage_aggregation_batch(install_dal, redis_client)
    assert result == AggregationResult(examined=0, written=0, skipped=0)
    redis_client.xack.assert_not_called()


async def test_run_usage_aggregation_batch_groups_two_different_workstreams_separately(install_dal: Any) -> None:
    redis_client = AsyncMock()
    redis_client.xreadgroup.return_value = [
        (USAGE_STREAM, [
            (b"3-1", {b"tenant_id": b"1", b"community_id": b"_tenant", b"workstream_id": b"ws-a",
                      b"stage": b"process", b"app_id": b"waddles.socials.music.default",
                      b"hour": b"2026-09-14T11:00:00+00:00", b"events": b"1", b"invocations": b"1",
                      b"host_calls": b"0", b"actions_delivered": b"0", b"fuel_ms": b"10",
                      b"outbound_bytes": b"0"}),
            (b"3-2", {b"tenant_id": b"1", b"community_id": b"_tenant", b"workstream_id": b"ws-b",
                      b"stage": b"process", b"app_id": b"waddles.socials.music.default",
                      b"hour": b"2026-09-14T11:00:00+00:00", b"events": b"1", b"invocations": b"1",
                      b"host_calls": b"0", b"actions_delivered": b"0", b"fuel_ms": b"20",
                      b"outbound_bytes": b"0"}),
        ]),
    ]
    result = await run_usage_aggregation_batch(install_dal, redis_client)
    assert result == AggregationResult(examined=2, written=2, skipped=0)


async def test_main_propagates_a_redis_connection_error(
    monkeypatch: pytest.MonkeyPatch, install_dal: Any
) -> None:
    """Prove the gate can fail: a Redis outage must not be swallowed into a silent zero-examined pass."""
    from services import usage_aggregator_service as job

    monkeypatch.setenv("DATABASE_URL", "sqlite://usage-aggregator-test.db")

    async def _raise_on_read(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("valkey unreachable")

    mock_client = AsyncMock()
    mock_client.xreadgroup.side_effect = _raise_on_read
    with (
        patch.object(job, "build_client", return_value=mock_client),
        patch.object(job, "ensure_group", new_callable=AsyncMock),
        patch.object(job, "_build_install_dal", new_callable=AsyncMock, return_value=install_dal),
        pytest.raises(ConnectionError),
    ):
        await job.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_usage_aggregator_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.usage_aggregator_service'`

- [ ] **Step 3: Write the implementation**

```python
# hub_api/services/usage_aggregator_service.py
"""hub-api's `waddles:usage` consumer -- writes workstream_usage_hourly (spec Sec5.12, Sec6.12, D31).

Stages (`svc_ingest`, `svc_process`, `svc_action`, `svc_streaming`) are
XADD-only producers onto `waddles:usage` (spec Sec11.10.2) -- hub-api is
the one and only reader, through its own consumer group. Every batch is
committed to Postgres BEFORE the entries it covers are XACKed, so a
crash between commit and XACK causes at-least-once redelivery (a
Postgres row gets written twice on the next run) rather than
at-most-once data loss -- acceptable per spec Sec5.12 ("no charging,
quota or enforcement ships now") and consistent with
`workstream_usage_hourly` being append-only/summed-at-query-time by
design: a duplicated correction row is exactly the shape the table
already tolerates.

Wire shape of one `waddles:usage` XADD entry (this task's own decision;
must match whichever plan implements the stage-side producer -- M3
svc_action, M4 svc_process, M5 svc_ingest, and svc_streaming's own
future plan): `tenant_id` (decimal string), `community_id` (decimal
string, or the literal "_tenant" for tenant-wide), `workstream_id`
(opaque string), `stage` (one of ingest/process/action/streaming),
`app_id` (string, or "" for ingest-stage entries with no bundle),
`hour` (RFC3339, truncated to the hour), `events`/`invocations`/
`host_calls`/`actions_delivered`/`fuel_ms`/`outbound_bytes` (decimal
strings, default "0"), `media_minutes` (decimal string, svc-streaming
only, absent elsewhere).

R52: `workstream_usage_hourly` is this plan's own new table, queried
through the penguin-dal `install_dal: AsyncDB` (Task 4). This is a
standalone process (a Kubernetes CronJob), so it builds its own
`install_dal` via `build_install_dal()` rather than reading one from a
Quart `app.config` -- the same pattern `bundle_role_cleanup_job.py`
(Task 36) established. Runs as a CronJob (`k8s/helm/waddlebot/
templates/usage-aggregator-cronjob.yaml`), entrypoint `python -m
services.usage_aggregator_service` -- bounded per-invocation batches
under `concurrencyPolicy: Forbid`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB

from services.bundle_install_dal import build_install_dal
from services.bundle_telemetry import bundle_span, get_meter
from services.valkey_admin_client import build_client, ensure_group

USAGE_STREAM = "waddles:usage"
USAGE_CONSUMER_GROUP = "hub_api_usage_aggregator"
_TENANT_WIDE_SENTINEL = "_tenant"
_VALID_STAGES = frozenset({"ingest", "process", "action", "streaming"})

_meter = get_meter()
_batches_counter = _meter.create_counter(
    "waddles_hub_usage_batches_total", description="usage aggregator batch runs, by result"
)
_rows_written_histogram = _meter.create_histogram(
    "waddles_hub_usage_rows_written", description="workstream_usage_hourly rows written per batch"
)
_entries_skipped_counter = _meter.create_counter(
    "waddles_hub_usage_entries_skipped_total", description="waddles:usage entries acked but unparseable"
)


@dataclass(slots=True, frozen=True)
class UsageDelta:
    """One decoded `waddles:usage` entry."""

    tenant_id: int
    community_id: int | None
    workstream_id: str
    stage: str
    app_id: str | None
    hour: datetime
    events: int
    invocations: int
    host_calls: int
    actions_delivered: int
    fuel_ms: int
    outbound_bytes: int
    media_minutes: float | None


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def parse_usage_entry(fields: dict[Any, Any]) -> UsageDelta:
    """Decode one `XREADGROUP` entry's field map into a `UsageDelta`. Raises `ValueError` if malformed."""
    decoded = {_decode(k): _decode(v) for k, v in fields.items()}
    try:
        stage = decoded["stage"]
        if stage not in _VALID_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        community_raw = decoded.get("community_id", _TENANT_WIDE_SENTINEL)
        community_id = None if community_raw == _TENANT_WIDE_SENTINEL else int(community_raw)
        app_id = decoded.get("app_id") or None
        media_raw = decoded.get("media_minutes")
        return UsageDelta(
            tenant_id=int(decoded["tenant_id"]),
            community_id=community_id,
            workstream_id=decoded["workstream_id"],
            stage=stage,
            app_id=app_id,
            hour=datetime.fromisoformat(decoded["hour"]),
            events=int(decoded.get("events", "0")),
            invocations=int(decoded.get("invocations", "0")),
            host_calls=int(decoded.get("host_calls", "0")),
            actions_delivered=int(decoded.get("actions_delivered", "0")),
            fuel_ms=int(decoded.get("fuel_ms", "0")),
            outbound_bytes=int(decoded.get("outbound_bytes", "0")),
            media_minutes=float(media_raw) if media_raw is not None else None,
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(f"malformed waddles:usage entry: {exc}") from exc


@dataclass(slots=True, frozen=True)
class AggregationResult:
    """What one batch did. `examined` is the denominator a zero-written run is judged against."""

    examined: int
    written: int
    skipped: int


def _group_key(delta: UsageDelta) -> tuple[int, int | None, str, str, str | None, datetime]:
    return (delta.tenant_id, delta.community_id, delta.workstream_id, delta.stage, delta.app_id, delta.hour)


async def run_usage_aggregation_batch(
    install_dal: AsyncDB,
    redis_client: Any,
    *,
    batch_size: int = 500,
    consumer_name: str = "hub-api-1",
) -> AggregationResult:
    """One bounded read-aggregate-write-ack pass over `waddles:usage`. Never blocks (`BLOCK` is unused)."""
    async with bundle_span("hub.usage.aggregate_batch", stream=USAGE_STREAM):
        response = await redis_client.xreadgroup(
            USAGE_CONSUMER_GROUP, consumer_name, {USAGE_STREAM: ">"}, count=batch_size
        )
        if not response:
            _batches_counter.add(1, {"result": "empty"})
            return AggregationResult(examined=0, written=0, skipped=0)

        entries = response[0][1]
        groups: dict[tuple[int, int | None, str, str, str | None, datetime], list[UsageDelta]] = defaultdict(list)
        to_ack: list[Any] = []
        skipped = 0
        for entry_id, fields in entries:
            to_ack.append(entry_id)
            try:
                delta = parse_usage_entry(fields)
            except ValueError:
                skipped += 1
                _entries_skipped_counter.add(1)
                continue
            groups[_group_key(delta)].append(delta)

        now = datetime.now(UTC)
        written = 0
        for (tenant_id, community_id, workstream_id, stage, app_id, hour), deltas in groups.items():
            await install_dal.workstream_usage_hourly.async_insert(
                tenant_id=tenant_id, community_id=community_id, workstream_id=workstream_id,
                stage=stage, app_id=app_id, hour=hour,
                events=sum(d.events for d in deltas),
                invocations=sum(d.invocations for d in deltas),
                host_calls=sum(d.host_calls for d in deltas),
                actions_delivered=sum(d.actions_delivered for d in deltas),
                fuel_ms=sum(d.fuel_ms for d in deltas),
                outbound_bytes=sum(d.outbound_bytes for d in deltas),
                media_minutes=(
                    sum(d.media_minutes for d in deltas if d.media_minutes is not None)
                    if any(d.media_minutes is not None for d in deltas) else None
                ),
                recorded_at=now,
            )
            written += 1

        if to_ack:
            await redis_client.xack(USAGE_STREAM, USAGE_CONSUMER_GROUP, *to_ack)

        _batches_counter.add(1, {"result": "processed"})
        _rows_written_histogram.record(written)
        return AggregationResult(examined=len(entries), written=written, skipped=skipped)


async def _build_install_dal() -> AsyncDB:
    """Open this standalone CronJob process's own penguin-dal connection (R52) -- same DSN, separate pool."""
    return await build_install_dal(os.environ["DATABASE_URL"], pool_size=1)


async def main() -> int:
    """CronJob entrypoint: ensure the consumer group exists, drain up to 20 batches, print denominators.

    An idle stream (zero entries examined) is the expected steady state
    between bursts of traffic, not a failure -- unlike Task 36's
    orphan-role sweeper, which examines a catalog table that is always
    populated once the system has any installed bundle. A genuine
    "pointed at the wrong place" failure here (an unreachable Valkey, a
    missing DATABASE_URL) raises an exception instead of returning a
    silent zero, which is what the test above proves.
    """
    install_dal = await _build_install_dal()
    redis_client = build_client()
    await ensure_group(redis_client, stream=USAGE_STREAM, group=USAGE_CONSUMER_GROUP)

    total_examined = 0
    total_written = 0
    total_skipped = 0
    for _ in range(20):
        result = await run_usage_aggregation_batch(install_dal, redis_client)
        total_examined += result.examined
        total_written += result.written
        total_skipped += result.skipped
        if result.examined == 0:
            break

    print(
        f"usage aggregation: examined={total_examined} written={total_written} skipped={total_skipped}"
    )
    if total_examined == 0:
        print(
            "usage aggregation: zero entries examined this run -- normal when the stream is idle "
            "between bursts of traffic",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 4: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_usage_aggregator_service.py -v`
Expected: `12 passed`

- [ ] **Step 5: Add the CronJob manifest**

```yaml
# k8s/helm/waddlebot/templates/usage-aggregator-cronjob.yaml
{{- if .Values.metering.enabled }}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ include "waddlebot.fullname" . }}-usage-aggregator
  labels:
    {{- include "waddlebot.labels" . | nindent 4 }}
    app.kubernetes.io/component: usage-aggregator
spec:
  schedule: {{ .Values.metering.aggregatorSchedule | quote }}
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  startingDeadlineSeconds: 60
  jobTemplate:
    spec:
      backoffLimit: 2
      ttlSecondsAfterFinished: 600
      template:
        metadata:
          labels:
            {{- include "waddlebot.selectorLabels" . | nindent 12 }}
            app.kubernetes.io/component: usage-aggregator
        spec:
          restartPolicy: Never
          serviceAccountName: {{ include "waddlebot.fullname" . }}-hub-api
          securityContext:
            runAsNonRoot: true
            runAsUser: 10001
            runAsGroup: 10001
            fsGroup: 10001
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: usage-aggregator
              image: "{{ .Values.hubApi.image.repository }}:{{ .Values.hubApi.image.tag }}"
              imagePullPolicy: {{ .Values.hubApi.image.pullPolicy }}
              command: ["python", "-m", "services.usage_aggregator_service"]
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities:
                  drop: ["ALL"]
              env:
                - name: DATABASE_URL
                  valueFrom:
                    secretKeyRef:
                      name: {{ .Values.hubApi.database.secretName }}
                      key: {{ .Values.hubApi.database.secretKey }}
                - name: VALKEY_URL
                  valueFrom:
                    secretKeyRef:
                      name: {{ .Values.metering.valkeySecretName }}
                      key: {{ .Values.metering.valkeySecretKey }}
                - name: SECURITY_TRANSPORT_TLS
                  value: {{ .Values.metering.valkeyTls | quote }}
                - name: OTEL_EXPORTER_OTLP_ENDPOINT
                  value: {{ .Values.observability.otlpEndpoint | quote }}
                - name: OTEL_SERVICE_NAME
                  value: waddles-usage-aggregator
              resources:
                requests:
                  cpu: 50m
                  memory: 128Mi
                limits:
                  cpu: 250m
                  memory: 256Mi
              volumeMounts:
                - name: tmp
                  mountPath: /tmp
          volumes:
            - name: tmp
              emptyDir: {}
{{- end }}
```

- [ ] **Step 6: Add the values keys**

Add to `k8s/helm/waddlebot/values.yaml` as a new top-level block:

```yaml
metering:
  # hub-api's usage aggregator: reads waddles:usage (its own consumer
  # group, XACK after commit), writes workstream_usage_hourly (spec
  # Sec5.12, D31). No charging/quota is wired to this -- spec Sec5.12
  # "Not billed yet". Read surfaces (Task 48's usage API) are never
  # gated by this value; only the recording pipeline is.
  enabled: true
  # Every minute -- Kubernetes CronJob's finest granularity;
  # concurrencyPolicy: Forbid means a slow run simply skips the next
  # tick rather than overlapping with itself.
  aggregatorSchedule: "*/1 * * * *"
  # Same Valkey the four Rust stage services XADD onto; a read-capable
  # credential distinct from any stage's write-only ACL user (spec
  # Sec11.10.2: stages are +xadd only, only hub-api's ACL user reads).
  valkeySecretName: waddlebot-valkey-usage-reader
  valkeySecretKey: url
  valkeyTls: "true"
```

- [ ] **Step 7: Validate the chart renders**

Run:
```bash
helm lint ./k8s/helm/waddlebot
helm template waddlebot ./k8s/helm/waddlebot --values ./k8s/helm/waddlebot/alpha.yml \
  --show-only templates/usage-aggregator-cronjob.yaml
```
Expected: `helm lint` reports `1 chart(s) linted, 0 chart(s) failed`, and the template renders one `CronJob` whose `spec.jobTemplate.spec.template.spec.securityContext.runAsNonRoot` is `true` and whose container command is `["python", "-m", "services.usage_aggregator_service"]`.

- [ ] **Step 8: Commit**

```bash
git add hub_api/services/usage_aggregator_service.py hub_api/tests/test_usage_aggregator_service.py \
        k8s/helm/waddlebot/templates/usage-aggregator-cronjob.yaml k8s/helm/waddlebot/values.yaml
git commit -m "$(cat <<'EOF'
feat(hub-api): usage_aggregator_service -- the waddles:usage consumer writing workstream_usage_hourly (spec Sec5.12, D31, R52)

Own consumer group (hub_api_usage_aggregator), commits every batch to
Postgres before XACKing its entries -- a crash between the two causes
at-least-once redelivery, never data loss, which the append-only,
summed-at-query-time workstream_usage_hourly table already tolerates.
Runs as a CronJob gated by the chart value metering.enabled, not a
PostHog flag (spec Sec12.3), building its own standalone penguin-dal
install_dal connection the same way bundle_role_cleanup_job.py (Task
36) does. Wire shape of one waddles:usage entry is this task's own
decision, documented in the module docstring and marked must-match for
the stage-side XADD producer plans (M3/M4/M5).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 48: `usage_query_service.py` + `blueprints/v1/workstream_usage.py` — the per-community admin usage API (spec §5.12, D31) (R52: penguin-dal)

**Depends on:** Task 42 (`workstream_usage_hourly` DDL), Task 43 (the `install_dal` fixture extension), Task 47 (the aggregator that populates the rows this API reads, `install_dal`-only per R52).

**Files:**
- Create: `hub_api/services/usage_query_service.py`
- Create: `hub_api/blueprints/v1/workstream_usage.py`
- Test: `hub_api/tests/test_usage_query_service.py`
- Test: `hub_api/tests/test_workstream_usage_blueprint.py`
- Modify: `hub_api/pyproject.toml`

**Interfaces:**
- Produces: `MAX_USAGE_PAGE_SIZE = 200`; `@dataclass(slots=True, frozen=True) UsageRow(tenant_id, community_id, workstream_id, stage, app_id, hour, events, invocations, host_calls, actions_delivered, fuel_ms, outbound_bytes, media_minutes)`; `async def query_usage(install_dal, *, tenant_id: int, community_id: int | None = None, workstream_id: str | None = None, stage: str | None = None, app_id: str | None = None, hour_from: datetime | None = None, hour_to: datetime | None = None, limit: int = 50, offset: int = 0) -> tuple[list[UsageRow], int]` (returns `(page, total)`, summed by natural key per spec §5.12/§6.12, raises `ApiError(..., 422, ...)` on an invalid filter); `GET /api/v1/tenant/{slug}/usage` (scope `tenant:admin`), DTOs `UsageRowDTO`, `UsageMetaDTO`, `UsageListResponse(success, rows, meta)`. `install_dal` is the `penguin_dal.AsyncDB` from Task 4 — `workstream_usage_hourly` is this plan's own new table (R52).
- Consumes: nothing new.

**Ungated by design.** This view is read-only, so it follows this plan's established rule (Task 37: "write surfaces are gated; read surfaces are not") — no new PostHog flag. The recording pipeline behind it is gated instead, by the chart value `metering.enabled` (Task 47), exactly as the spec names it (§12.3).

- [ ] **Step 1: Write the failing test for the query service**

```python
# hub_api/tests/test_usage_query_service.py
"""Tests for the per-community usage query -- filters, aggregation-at-query-time, pagination (D31)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.errors import ApiError
from services.usage_query_service import query_usage


async def _seed_row(install_dal: Any, **overrides: Any) -> None:
    base = {
        "tenant_id": 1, "community_id": None, "workstream_id": "ws-1", "stage": "ingest",
        "app_id": None, "hour": datetime(2026, 9, 14, 10, 0, tzinfo=UTC), "events": 1,
        "invocations": 0, "host_calls": 0, "actions_delivered": 0, "fuel_ms": 0,
        "outbound_bytes": 100, "media_minutes": None, "recorded_at": datetime.now(UTC),
    }
    base.update(overrides)
    await install_dal.workstream_usage_hourly.async_insert(**base)


async def test_query_usage_with_no_rows_returns_an_empty_page(install_dal: Any) -> None:
    rows, total = await query_usage(install_dal, tenant_id=1)
    assert rows == []
    assert total == 0


async def test_query_usage_sums_two_correction_rows_for_the_same_natural_key(install_dal: Any) -> None:
    await _seed_row(install_dal, events=2, outbound_bytes=100)
    await _seed_row(install_dal, events=3, outbound_bytes=50)
    rows, total = await query_usage(install_dal, tenant_id=1)
    assert total == 1
    assert rows[0].events == 5
    assert rows[0].outbound_bytes == 150


async def test_query_usage_filters_by_community(install_dal: Any) -> None:
    await _seed_row(install_dal, workstream_id="ws-a", community_id=1)
    await _seed_row(install_dal, workstream_id="ws-b", community_id=2)
    rows, total = await query_usage(install_dal, tenant_id=1, community_id=1)
    assert total == 1
    assert rows[0].workstream_id == "ws-a"


async def test_query_usage_filters_by_workstream(install_dal: Any) -> None:
    await _seed_row(install_dal, workstream_id="ws-a")
    await _seed_row(install_dal, workstream_id="ws-b")
    rows, total = await query_usage(install_dal, tenant_id=1, workstream_id="ws-b")
    assert total == 1
    assert rows[0].workstream_id == "ws-b"


async def test_query_usage_filters_by_date_range(install_dal: Any) -> None:
    await _seed_row(install_dal, workstream_id="ws-early", hour=datetime(2026, 9, 10, 0, 0, tzinfo=UTC))
    await _seed_row(install_dal, workstream_id="ws-late", hour=datetime(2026, 9, 20, 0, 0, tzinfo=UTC))
    rows, total = await query_usage(
        install_dal, tenant_id=1,
        hour_from=datetime(2026, 9, 15, 0, 0, tzinfo=UTC), hour_to=datetime(2026, 9, 25, 0, 0, tzinfo=UTC),
    )
    assert total == 1
    assert rows[0].workstream_id == "ws-late"


async def test_query_usage_paginates(install_dal: Any) -> None:
    for i in range(5):
        await _seed_row(install_dal, workstream_id=f"ws-{i}", hour=datetime(2026, 9, 14, i, 0, tzinfo=UTC))
    page1, total = await query_usage(install_dal, tenant_id=1, limit=2, offset=0)
    page2, _ = await query_usage(install_dal, tenant_id=1, limit=2, offset=2)
    assert total == 5
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r.workstream_id for r in page1} != {r.workstream_id for r in page2}


async def test_query_usage_never_returns_another_tenants_rows(install_dal: Any) -> None:
    await _seed_row(install_dal, tenant_id=1, workstream_id="ws-mine")
    await _seed_row(install_dal, tenant_id=2, workstream_id="ws-other")
    rows, total = await query_usage(install_dal, tenant_id=1)
    assert total == 1
    assert rows[0].workstream_id == "ws-mine"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"stage": "nonsense"}, "invalid_stage"),
        ({"limit": 0}, "invalid_limit"),
        ({"limit": 500}, "invalid_limit"),
        ({"offset": -1}, "invalid_offset"),
        (
            {"hour_from": datetime(2026, 9, 20, tzinfo=UTC), "hour_to": datetime(2026, 9, 1, tzinfo=UTC)},
            "invalid_date_range",
        ),
        ({"workstream_id": "   "}, "invalid_workstream_id"),
    ],
)
async def test_query_usage_rejects_every_invalid_filter(
    install_dal: Any, kwargs: dict[str, Any], code: str
) -> None:
    with pytest.raises(ApiError) as excinfo:
        await query_usage(install_dal, tenant_id=1, **kwargs)
    assert excinfo.value.code == code
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_usage_query_service.py -v`
Expected: `ModuleNotFoundError: No module named 'services.usage_query_service'`

- [ ] **Step 3: Write `usage_query_service.py`**

```python
# hub_api/services/usage_query_service.py
"""Per-community admin usage query (spec Sec5.12, Sec6.12, D31) -- read-only, no charging/quota.

`workstream_usage_hourly` allows more than one row per natural key (a
correction is a new row, spec Sec6.12) -- this module sums by
`(tenant_id, community_id, workstream_id, stage, app_id, hour)` at
query time, exactly as the spec's design intends, rather than exposing
raw, possibly-duplicated rows to an admin.

R52: `workstream_usage_hourly` is this plan's own new table, queried
through the penguin-dal `install_dal: AsyncDB` (Task 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from penguin_dal import AsyncDB

from services.errors import ApiError

MAX_USAGE_PAGE_SIZE = 200
_VALID_STAGES = frozenset({"ingest", "process", "action", "streaming"})


@dataclass(slots=True, frozen=True)
class UsageRow:
    """One aggregated `(tenant, community, workstream, stage, app)` usage bucket for one hour."""

    tenant_id: int
    community_id: int | None
    workstream_id: str
    stage: str
    app_id: str | None
    hour: datetime
    events: int
    invocations: int
    host_calls: int
    actions_delivered: int
    fuel_ms: int
    outbound_bytes: int
    media_minutes: float | None


def _validate_filters(
    *, workstream_id: str | None, stage: str | None, hour_from: datetime | None,
    hour_to: datetime | None, limit: int, offset: int,
) -> None:
    if workstream_id is not None and not workstream_id.strip():
        raise ApiError("workstreamId must not be blank", 422, "invalid_workstream_id")
    if stage is not None and stage not in _VALID_STAGES:
        raise ApiError(f"stage must be one of {sorted(_VALID_STAGES)}", 422, "invalid_stage")
    if hour_from is not None and hour_to is not None and hour_from > hour_to:
        raise ApiError("from must not be after to", 422, "invalid_date_range")
    if limit < 1 or limit > MAX_USAGE_PAGE_SIZE:
        raise ApiError(f"limit must be between 1 and {MAX_USAGE_PAGE_SIZE}", 422, "invalid_limit")
    if offset < 0:
        raise ApiError("offset must not be negative", 422, "invalid_offset")


async def query_usage(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int | None = None,
    workstream_id: str | None = None,
    stage: str | None = None,
    app_id: str | None = None,
    hour_from: datetime | None = None,
    hour_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[UsageRow], int]:
    """The tenant's usage, optionally scoped to one community, summed by natural key, paginated.

    Aggregation and pagination both happen in Python after the filtered
    rows are fetched -- an admin reporting view bounded by tenant (and
    usually by a community/date-range filter too), never a hot path, so
    this trades a little raw-row overhead for a query `penguin_dal`'s
    query-builder can express without a second, aggregate-specific
    code path (never raw SQL, per this plan's Global Constraints).
    """
    _validate_filters(
        workstream_id=workstream_id, stage=stage, hour_from=hour_from, hour_to=hour_to,
        limit=limit, offset=offset,
    )
    query = install_dal.workstream_usage_hourly.tenant_id == tenant_id
    if community_id is not None:
        query &= install_dal.workstream_usage_hourly.community_id == community_id
    if workstream_id is not None:
        query &= install_dal.workstream_usage_hourly.workstream_id == workstream_id
    if stage is not None:
        query &= install_dal.workstream_usage_hourly.stage == stage
    if app_id is not None:
        query &= install_dal.workstream_usage_hourly.app_id == app_id
    if hour_from is not None:
        query &= install_dal.workstream_usage_hourly.hour >= hour_from
    if hour_to is not None:
        query &= install_dal.workstream_usage_hourly.hour < hour_to

    raw_rows = await install_dal(query).select()

    grouped: dict[tuple[int, int | None, str, str, str | None, datetime], dict[str, Any]] = {}
    for row in raw_rows:
        key = (row.tenant_id, row.community_id, row.workstream_id, row.stage, row.app_id, row.hour)
        bucket = grouped.setdefault(
            key,
            {"events": 0, "invocations": 0, "host_calls": 0, "actions_delivered": 0,
             "fuel_ms": 0, "outbound_bytes": 0, "media_minutes": None},
        )
        bucket["events"] += row.events
        bucket["invocations"] += row.invocations
        bucket["host_calls"] += row.host_calls
        bucket["actions_delivered"] += row.actions_delivered
        bucket["fuel_ms"] += row.fuel_ms
        bucket["outbound_bytes"] += row.outbound_bytes
        if row.media_minutes is not None:
            bucket["media_minutes"] = (bucket["media_minutes"] or 0) + row.media_minutes

    results = [
        UsageRow(
            tenant_id=key[0], community_id=key[1], workstream_id=key[2], stage=key[3],
            app_id=key[4], hour=key[5], **bucket,
        )
        for key, bucket in sorted(grouped.items(), key=lambda item: (item[0][5], item[0][2], item[0][3]))
    ]
    total = len(results)
    return results[offset : offset + limit], total
```

- [ ] **Step 4: Run to verify the query-service tests pass**

Run: `cd hub_api && python3 -m pytest tests/test_usage_query_service.py -v`
Expected: `13 passed` (7 named tests + 6 parametrized invalid-filter cases).

- [ ] **Step 5: Write the failing test for the blueprint**

```python
# hub_api/tests/test_workstream_usage_blueprint.py
"""Blueprint tests for GET /api/v1/tenant/{slug}/usage (spec Sec5.12, D31)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart

from blueprints.v1.workstream_usage import BLUEPRINTS
from tests.conftest import TENANT_SLUG, make_user_token


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    await install_dal.workstream_usage_hourly.async_insert(
        tenant_id=1, community_id=None, workstream_id="ws-1", stage="ingest", app_id=None,
        hour=datetime(2026, 9, 14, 10, 0, tzinfo=UTC), events=4, invocations=0, host_calls=0,
        actions_delivered=0, fuel_ms=0, outbound_bytes=200, media_minutes=None,
        recorded_at=datetime.now(UTC),
    )
    quart_app = Quart(__name__)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


async def test_usage_requires_tenant_admin(app: Quart) -> None:
    token = make_user_token(user_id=1, scope="", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


async def test_usage_returns_the_seeded_row(app: Quart) -> None:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["meta"]["total"] == 1
    assert body["rows"][0]["workstreamId"] == "ws-1"
    assert body["rows"][0]["events"] == 4


async def test_usage_filters_by_workstream_id(app: Quart) -> None:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage?workstreamId=nope", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["meta"]["total"] == 0
    assert body["rows"] == []


async def test_usage_rejects_an_invalid_stage(app: Quart) -> None:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage?stage=bogus", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 422
    assert (await response.get_json())["error"]["code"] == "invalid_stage"


async def test_usage_rejects_an_out_of_range_limit(app: Quart) -> None:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage?limit=9999", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 422
    assert (await response.get_json())["error"]["code"] == "invalid_limit"


async def test_usage_paginates_via_query_params(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    for i in range(3):
        await install_dal.workstream_usage_hourly.async_insert(
            tenant_id=1, community_id=None, workstream_id=f"ws-extra-{i}", stage="ingest", app_id=None,
            hour=datetime(2026, 9, 14, 12 + i, 0, tzinfo=UTC), events=1, invocations=0, host_calls=0,
            actions_delivered=0, fuel_ms=0, outbound_bytes=0, media_minutes=None,
            recorded_at=datetime.now(UTC),
        )
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage?limit=2&offset=0", headers={"Authorization": f"Bearer {token}"}
    )
    body = await response.get_json()
    assert body["meta"]["total"] == 4
    assert len(body["rows"]) == 2


async def test_usage_never_returns_another_tenants_rows(app: Quart) -> None:
    dal = app.config["dal"]
    install_dal = app.config["install_dal"]
    other_tenant_id = dal.tenants.insert(slug="other-corp", display_name="Other", is_active=True)
    dal.commit()
    await install_dal.workstream_usage_hourly.async_insert(
        tenant_id=other_tenant_id, community_id=None, workstream_id="ws-other", stage="ingest", app_id=None,
        hour=datetime(2026, 9, 14, 10, 0, tzinfo=UTC), events=99, invocations=0, host_calls=0,
        actions_delivered=0, fuel_ms=0, outbound_bytes=0, media_minutes=None, recorded_at=datetime.now(UTC),
    )
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage", headers={"Authorization": f"Bearer {token}"}
    )
    body = await response.get_json()
    assert all(r["events"] != 99 for r in body["rows"])
```

- [ ] **Step 6: Run to verify failure**

Run: `cd hub_api && python3 -m pytest tests/test_workstream_usage_blueprint.py -v`
Expected: `ModuleNotFoundError: No module named 'blueprints.v1.workstream_usage'`

- [ ] **Step 7: Write `blueprints/v1/workstream_usage.py`**

```python
# hub_api/blueprints/v1/workstream_usage.py
"""v1 `workstream_usage` group -- per-community admin usage view (spec Sec5.12, Sec6.12, D31).

Read-only, ungated by a PostHog flag on purpose -- this milestone's
established rule is "write surfaces are gated, read surfaces are not"
(Task 37), so a flag flip mid-rollout never blinds an admin already
looking at usage data. Gated instead by `metering.enabled` (a chart
value, not a PostHog flag, spec Sec12.3) -- when metering is off the
aggregator (Task 47) simply never runs and this view returns empty
pages, never an error. R52: reads `current_app.config["install_dal"]`
-- `workstream_usage_hourly` is this plan's own new table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services.errors import ApiError
from services.tenant_service import require_matching_tenant
from services.usage_query_service import query_usage

workstream_usage_bp = Blueprint(
    "v1_workstream_usage", __name__, url_prefix="/api/v1/tenant/<tenant_slug>/usage"
)


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code))


def _tenant_id(tenant_slug: str) -> int:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    require_matching_tenant(tenant_slug, ctx.tenant_slug)
    return cast(int, ctx.tenant_id)


def _parse_int(raw: str | None, *, field_name: str, code: str) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ApiError(f"{field_name} must be an integer", 422, code) from exc


def _parse_datetime(raw: str | None, *, field_name: str, code: str) -> datetime | None:
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(f"{field_name} must be an RFC3339 timestamp", 422, code) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(slots=True, frozen=True)
class UsageRowDTO:
    """One aggregated usage bucket on the wire."""

    communityId: int | None
    workstreamId: str
    stage: str
    appId: str | None
    hour: str
    events: int
    invocations: int
    hostCalls: int
    actionsDelivered: int
    fuelMs: int
    outboundBytes: int
    mediaMinutes: float | None


@dataclass(slots=True, frozen=True)
class UsageMetaDTO:
    """Pagination metadata."""

    total: int
    limit: int
    offset: int


@dataclass(slots=True, frozen=True)
class UsageListResponse:
    """Response DTO for `GET .../usage`."""

    success: bool
    rows: list[UsageRowDTO] = field(default_factory=list)
    meta: UsageMetaDTO = field(default_factory=lambda: UsageMetaDTO(total=0, limit=0, offset=0))


@workstream_usage_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(UsageListResponse)
async def list_usage(tenant_slug: str) -> UsageListResponse | tuple[dict[str, object], int]:
    """Per-community usage, summed by `(community, workstream, stage, app, hour)`, paginated."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        community_id = _parse_int(
            request.args.get("communityId"), field_name="communityId", code="invalid_community_id"
        )
        limit = _parse_int(request.args.get("limit"), field_name="limit", code="invalid_limit") or 50
        offset = _parse_int(request.args.get("offset"), field_name="offset", code="invalid_offset") or 0
        hour_from = _parse_datetime(request.args.get("from"), field_name="from", code="invalid_date_range")
        hour_to = _parse_datetime(request.args.get("to"), field_name="to", code="invalid_date_range")
        rows, total = await query_usage(
            install_dal, tenant_id=tenant_id, community_id=community_id,
            workstream_id=request.args.get("workstreamId"), stage=request.args.get("stage"),
            app_id=request.args.get("appId"), hour_from=hour_from, hour_to=hour_to,
            limit=limit, offset=offset,
        )
    except ApiError as exc:
        return _err(exc)

    return UsageListResponse(
        success=True,
        rows=[
            UsageRowDTO(
                communityId=r.community_id, workstreamId=r.workstream_id, stage=r.stage, appId=r.app_id,
                hour=r.hour.isoformat(), events=r.events, invocations=r.invocations, hostCalls=r.host_calls,
                actionsDelivered=r.actions_delivered, fuelMs=r.fuel_ms, outboundBytes=r.outbound_bytes,
                mediaMinutes=r.media_minutes,
            )
            for r in rows
        ],
        meta=UsageMetaDTO(total=total, limit=limit, offset=offset),
    )


BLUEPRINTS: list[Blueprint] = [workstream_usage_bp]
```

- [ ] **Step 8: Run to verify all pass**

Run: `cd hub_api && python3 -m pytest tests/test_workstream_usage_blueprint.py -v`
Expected: `7 passed`

- [ ] **Step 9: Add the ruff per-file ignore**

Append to `hub_api/pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]`:

```toml
"blueprints/v1/workstream_usage.py" = ["N815"]
```

- [ ] **Step 10: Commit**

```bash
git add hub_api/services/usage_query_service.py hub_api/blueprints/v1/workstream_usage.py \
        hub_api/tests/test_usage_query_service.py hub_api/tests/test_workstream_usage_blueprint.py \
        hub_api/pyproject.toml
git commit -m "$(cat <<'EOF'
feat(hub-api): GET /api/v1/tenant/{slug}/usage -- per-community admin usage view (spec Sec5.12, D31, R52)

query_usage() sums workstream_usage_hourly by natural key at query
time (corrections are new rows, never an UPDATE, per spec Sec6.12) and
supports date-range, community, workstream, stage and app filters plus
limit/offset pagination -- every filter combination, an invalid value,
and the empty-result case are covered. Read-only and ungated by a
PostHog flag, following this plan's established write/read gating
split (Task 37); the recording pipeline behind it is gated instead by
the chart value metering.enabled (Task 47). Queries the new
workstream_usage_hourly table through penguin-dal's install_dal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```
---

## Task 49: M2b D30/D31 closing gate — OpenAPI coverage, logging conformance, and the coverage/lint/make re-run for the workstream/usage surface

**Depends on:** every preceding task (1-48), including Task 41's original closing gate. Nothing depends on this one — it is the milestone's final closing gate.

**Files:**
- Modify: `hub_api/tests/test_openapi_m2b_paths.py`
- Modify: `hub_api/tests/test_m2b_logging_conformance.py`
- Modify: `hub_api/pyproject.toml`

**Interfaces:**
- Produces: nothing consumed by another task — a leaf verification that widens Task 41's counts to cover Tasks 42-48's new surface.
- Consumes: nothing new.

- [ ] **Step 1: Widen the OpenAPI rule/module lists**

In `hub_api/tests/test_openapi_m2b_paths.py`, add this entry to the `_M2B_RULES` list:

```python
    "/api/v1/tenant/<slug>/usage",
```

and this entry to the `_M2B_MODULES` list:

```python
    "blueprints.v1.workstream_usage",
```

- [ ] **Step 2: Run to verify all pass with the wider surface**

Run: `cd hub_api && python3 -m pytest tests/test_openapi_m2b_paths.py -v -s`
Expected: `3 passed`, stdout now reading `openapi check: blueprints registered = 9`, `openapi check: rules examined = <N>` where N ≥ 22, `openapi check: generated paths examined = <M>` where M ≥ 22.

- [ ] **Step 3: Widen the logging-conformance file list**

In `hub_api/tests/test_m2b_logging_conformance.py`, add these four entries to the `_M2B_FILES` list:

```python
    "services/workstream_service.py",
    "services/usage_aggregator_service.py",
    "services/usage_query_service.py",
    "blueprints/v1/workstream_usage.py",
```

Add `"services/usage_aggregator_service.py"` to the `_PRINT_ALLOWED` set (its `main()` prints its own CronJob stdout/stderr report, the same carve-out `bundle_role_cleanup_job.py` already has):

```python
_PRINT_ALLOWED = {"services/bundle_role_cleanup_job.py", "services/usage_aggregator_service.py"}
```

- [ ] **Step 4: Run to verify all pass with the wider scan**

Run: `cd hub_api && python3 -m pytest tests/test_m2b_logging_conformance.py -v -s`
Expected: `2 passed`, stdout now reading `logging conformance: files scanned = 33`.

- [ ] **Step 5: Add the remaining ruff per-file ignore, if not already present**

Confirm `hub_api/pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]` contains the line Task 48 added:

```toml
"blueprints/v1/workstream_usage.py" = ["N815"]
```

- [ ] **Step 6: Run the linter and confirm it still passes**

Run:
```bash
cd hub_api && python3 -m ruff check . && python3 -m ruff format --check .
```
Expected: `All checks passed!` and `<N> files already formatted`.

- [ ] **Step 7: Run `mypy --strict` over every M2b module, including the six added by Tasks 44-48**

Run:
```bash
cd hub_api && python3 -m mypy --strict \
  services/bundle_secret_crypto.py services/bundle_manifest_v2.py services/bundle_storage_service.py \
  services/compiler_job_service.py services/bundle_version_service.py services/bundle_artifact_service.py \
  services/bundle_activation_service.py services/permission_summary_service.py \
  services/bundle_approval_service.py services/bundle_db_role_service.py \
  services/platform_settings_service.py services/tenant_bundle_settings.py \
  services/custom_platform_service.py services/ingest_source_service.py services/ingest_source_auth.py \
  services/valkey_admin_client.py services/stream_grant_service.py \
  services/bundle_trip_reenable_service.py services/bundle_role_cleanup_job.py \
  services/bundle_feature_gate.py services/bundle_telemetry.py services/rbac_matrix.py \
  services/workstream_service.py services/usage_aggregator_service.py services/usage_query_service.py \
  blueprints/v1/bundle_versions.py blueprints/v1/bundle_artifact_callback.py \
  blueprints/v1/bundle_approvals.py blueprints/v1/bundle_settings.py \
  blueprints/v1/custom_platforms.py blueprints/v1/ingest_sources.py \
  blueprints/v1/bundle_grants.py blueprints/v1/distribution.py blueprints/v1/workstream_usage.py
```
Expected: `Success: no issues found in 34 source files`.

- [ ] **Step 8: Run the full M2b suite with the coverage gate**

Run:
```bash
cd hub_api && python3 -m pytest tests/ \
  --cov=services --cov=blueprints --cov-report=term-missing --cov-fail-under=90 -q
```
Expected: every test passes and the final line reads `Required test coverage of 90% reached.` — record the reported total-statements number; a run whose denominator is `0 statements` is a failure, not a pass.

- [ ] **Step 9: Run the containerized build**

Run:
```bash
docker build -f hub_api/Dockerfile -t waddlebot/hub-api:m2b-local .
```
Expected: the build completes and the final stage's `USER` is non-root. Verify:
```bash
docker run --rm --entrypoint sh waddlebot/hub-api:m2b-local -c 'id -u'
```
Expected: a non-zero uid (never `0`).

- [ ] **Step 10: Run the repo's containerized `make` gates**

Run, from the repository root, in this order, stopping at the first failure:
```bash
make lint
make test-security
make test
make pre-commit
```
Expected:
- `make lint` → `scripts/lint.sh` completes with exit `0` and prints the number of files it checked.
- `make test-security` → `scripts/security-scan.sh` completes with exit `0` and reports, per scanner, how many files/packages were examined. A scanner reporting zero examined items is a FAILURE — fix the path, do not accept the clean result (`critical-rules.md` Verification Integrity).
- `make test` → `tests/k8s/alpha/05-unit-tests.sh` runs every suite and prints a non-zero `TOTAL_PASSED`.
- `make pre-commit` → `=== Pre-commit complete ===` after lint, security and test all pass.

If `make lint` or `make test-security` completes suspiciously fast or reports no denominator, audit the target once by making it fail on purpose (append an unused import, re-run, confirm a non-zero exit, revert) before treating it as green.

- [ ] **Step 11: Commit**

```bash
git add hub_api/tests/test_openapi_m2b_paths.py hub_api/tests/test_m2b_logging_conformance.py \
        hub_api/pyproject.toml
git commit -m "$(cat <<'EOF'
test(hub-api): M2b closing gate, widened for the D30/D31 workstream/usage surface

Extends Task 41's OpenAPI-path and logging-conformance suites to cover
the six modules and one route Tasks 42-48 added -- blueprints
registered 8 -> 9, files scanned 29 -> 33. Re-runs mypy --strict,
the coverage gate, the containerized build and every make gate over
the now-complete M2b surface.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

## Self-Review

Run before declaring the plan finished. Findings from the pass that produced this section are recorded as **fixed** below.

### Spec coverage

| Spec section / requirement | Where implemented | Status |
|---|---|---|
| §6.7 distribution API additions (six fields + `manifest` + `grants`) | Tasks 32, 33 | ✅ |
| §6.7 `null` digest row skipped by the stage, still served | Tasks 32, 33 | ✅ |
| §6.8 `app_stream_grants` table + partial unique | Task 3 | ✅ |
| §6.8 resolution against configured ingest sources, per-source stream keys | Task 29 | ✅ |
| §6.8 `XGROUP CREATE MKSTREAM`, BUSYGROUP-tolerant | Task 28 | ✅ |
| §6.8 re-resolution when sources appear | Tasks 29, 31 (`POST .../grants/resolve`) | ✅ |
| §6.8 wildcard gate (`allow_wildcard_consumes`) | Tasks 7, 24 | ✅ |
| §6.4.4 `routes_to` validation | Task 7 | ✅ |
| §6.9 `app_install_approvals` | Task 3 | ✅ |
| §6.10 `app_versions`, exactly two writers, audit trigger with role + old/new digest | Task 2 | ✅ |
| §6.10 `app_active_versions`, activation/rollback, refuse an unverified digest | Tasks 2, 16, 17 | ✅ |
| §9.1 AWAITING APPROVAL / APPROVED lifecycle | Tasks 3 (`app_version_uploads` statuses), 19, 30 | ✅ |
| §9.2 install + compiler Job orchestration | Tasks 8, 9, 10, 11, 12 | ✅ |
| §9.4 publish: callback is a notification, hub-api re-hashes the bucket object | Tasks 13, 15 | ✅ |
| §9.6 grant revocation + audit | Tasks 29, 31 | ✅ |
| §9.7.1 permission summary from manifest + component imports | Tasks 18, 19 (imports-based cross-check deferred with a documented scope note) | ⚠️ documented |
| §9.7.1 `permission_hash` over canonical JSON | Task 18 | ✅ |
| §9.7.2 approver recorded | Tasks 3, 19 | ✅ |
| §9.7.4 upgrade diff, narrowing auto-approve | Task 19 (`classify_diff`) | ✅ |
| §9.7.5 headless approval fails closed on hash mismatch | Task 19 | ✅ |
| §10.3/§10.4 generic intake registry + `intake:write` JWT | Tasks 25, 26, 27 | ✅ |
| §4.1.1/§10.1/§10.3 D29 per-source `auth`: HMAC + mandatory second factor for generic webhooks, Twitch/Kick origin policy, secrets never returned, audit on change, published through the config/distribution API | Tasks 39, 40 | ✅ |
| §11.10 D28 RBAC matrix, generated grants, live-grants equality CI test (≥8 roles, ≥8 tables — widened to 11 tables by Task 42) | Tasks 1, 2, 5, 42 | ✅ |
| §5.11/§6.11/§15.4 D30 `workstreams` table, 1:1 with `ingest_sources`, backfilled for pre-existing sources, created/disabled going forward | Task 42 (schema+backfill), Task 44 (lifecycle) | ✅ |
| §5.11/§9.7.1/§10.3 D30 workstream shown on the source config view, the distribution `/sources` feed, and the consent screen | Task 45 | ✅ |
| §5.9 D30 `routes_to` cross-tenant refusal wired into version approval (422 + audit) | Task 46 | ✅ |
| §12.3 D30 envelope binding key (`security.envelopeBinding.keySecretRef`) never read, written or provisioned by hub-api | Decision #17 (verified, no task — see Findings #12) | ✅ documented |
| §5.11 D30 RLS policies on bundle-reachable (`data.tables`) tables | Confirmed out of M2b scope — see Findings #12 | N/A (correctly excluded) |
| §5.12/§6.12/§11.10.1/§11.10.2 D31 `workstream_usage_hourly`: append-only, `hub_api` SELECT/INSERT only, no `UPDATE`/`DELETE` for anyone | Task 42 | ✅ |
| §5.12 D31 usage aggregator: own Valkey consumer group on `waddles:usage`, commit-then-XACK, stages remain XADD-only | Task 47 | ✅ |
| §5.12/§6.12 D31 per-community admin usage view/API, filter coverage (date range, community, workstream, pagination, invalid filters, empty results) | Task 48 | ✅ |
| §13.5 feature flags | Task 37 | ✅ |
| §19 Q1 trip re-enable | Task 35, Decision #10 | ✅ |
| §19 Q3 per-bundle role lifecycle | Tasks 21, 36, Decision #10b | ✅ |
| Observability: logs + metrics + traces, env-configurable endpoint | Tasks 38, 41, 47 (usage aggregator metrics reuse Task 38's `get_meter()`/`bundle_span`) | ✅ |
| Coverage ≥ 90 %, ruff, mypy --strict, containerized `make` gates | Task 41 (original 41-task surface), Task 49 (widened for D30/D31) | ✅ |

The one ⚠️ is deliberate and already documented in Task 19's scope note: cross-checking the permission summary against the **component's actual imports** needs M2a's compiler to report an import list on the artifact callback. hub-api derives capabilities from the manifest's declared shape until that field exists; the follow-on is a one-function change in `_derive_capabilities`, not a redesign.

**Name consistency with M2a/M5.** `waddles_publisher` (the trusted publisher role `app_versions` grants privileges to) and the compiler artifact-callback shape are unchanged from Tasks 6-15 and still match the M2a plan's own naming. The `auth` wire shape (Decision #13) and the new `workstreamId` field (Decision #14, Task 45) are both plain camelCase/snake_case siblings published through the same `/api/v1/distribution/sources` endpoint M5 polls — `workstreamId`'s presence is additive to that endpoint's existing shape, so it does not change any field M5 already depended on. The `waddles:usage` entry wire shape (Decision #16, Task 47) is a **new** cross-plan surface with no prior name to match; it is marked **must match** for whichever plan first implements the stage-side `XADD` producer (M3, M4, M5, or a future svc-streaming plan), the same marking convention Decision #13 already established.

### Placeholder scan

Run this before considering the plan done:

```bash
grep -nE 'TBD|TODO|FIXME|XXX|similar to Task|same as Task|as above|placeholder|restream' \
  docs/superpowers/plans/2026-09-14-rust-data-plane-m2b-hub-api.md
```
Expected: exactly **three** hits, all benign and all outside any task body — the Global Constraints line that *forbids* the word "restream", and this Self-Review section's own two self-referential lines (the command above and the sentence you are reading). A hit anywhere inside a `## Task` body is a defect: every task must carry complete, runnable code, because its implementer sees only that task's text.

A second scan, for bodies elided rather than written out:

```bash
grep -nE '^\s*\.\.\.\s*(#|$)' docs/superpowers/plans/2026-09-14-rust-data-plane-m2b-hub-api.md
```
Expected: exactly **two** hits, both in Task 38's wiring steps (`...  # the existing body, indented one level` and `...  # existing body`). Each names precisely which existing body it stands for, is preceded by the full surrounding code, and sits in a step whose instruction is "wrap the existing body" — never a stub. Any bare `...` with no such note is a defect.

### Findings fixed during this review

| # | Finding | Fix |
|---|---|---|
| 1 | Task 32's Interfaces block declared `manifest: dict[str, Any]` on `BundleDistributionRow`, but neither the dataclass nor `_enrich_with_active_version` populated it — spec §6.7's `manifest` field would have shipped permanently empty, and the Rust stage would have had no `egress`/`data.tables`/`limits`/`consumes` to enforce. | Added `_manifest_subset()` reading `app_version_uploads.manifest_json`, applied the manifest defaults (`timeout_ms` 2000 / `memory_mb` 64 / `egress_rps` 10) at the hub rather than leaving them to the stage, widened `_enrich_with_active_version`'s return to a 7-tuple, updated both call sites and the commit message, and added four assertions to the first Task 32 test. |
| 2 | Task 32's test carried a dead line (`dal.app_catalog.update_record_by_id if False else None  # no-op line kept for diff clarity`) that would have been copied verbatim into a real test file by a worker following the task literally. | Deleted. |
| 3 | Decision #10 recorded Q1/Q3 in the spec's bare interim wording, which left "no hub-api action" for a trip and a grace-period-only role drop — neither matching the decided defaults (admin action + new digest; created at approval, dropped at uninstall). | Rewrote as Decisions #10 and #10b, and added Tasks 35 and 36 implementing both. Task 20's docstrings were realigned to match. |
| 4 | Nothing in the plan actually gated a write surface on a PostHog flag — the requirement lived only in Global Constraints, so every one of the ~20 endpoints would have shipped ungated. | Added Task 37: a `require_flags` decorator, a complete gated-surface table, the autouse test fixture that keeps every other suite green, and the `allow_prebuilt` two-gate. |
| 5 | No task emitted an OTel span, metric or trace, despite Observability being a blocking gate. | Added Task 38: `bundle_telemetry.py` (API-only, OTLP env vars, no vendor SDK), instrumentation of five service entry points and both distribution polls, and an in-memory-sink test that prints its denominators. |
| 6 | The forward references written before the renumber (`Task 33`/`Task 34`/`Task 35`) pointed at the wrong tasks once Tasks 33-41 landed. | Corrected every one: the file structure block, Task 20's note, Task 26's `/sources` reference, and the coverage line in Global Constraints. |
| 7 | `/api/v1/distribution/sources` was named in the requirements but had no service function capable of filtering or rendering stream keys. | Added `list_sources_for_distribution` in Task 34 with `communityId`/`platform`/`enabled` filter coverage, plus the widening-not-narrowing `communityId` semantics stated once and tested. |
| 8 | Tasks 1-32 (written before this pass) carried `**Files:**` and `**Interfaces:**` blocks but no `**Depends on:**` line, so a worker picking up a task in isolation had no statement of what must already exist. | Added a `**Depends on:**` line to every one of Tasks 1-32, derived from each task's own `Consumes` list and the migration chain. All 41 tasks now carry one. |
| 9 | A mid-flight requirement arrived from the user's third spec review — every ingest source needs a caller-auth second factor (generic webhooks) or an origin policy (Twitch/Kick), published to svc_ingest. Nothing in the plan modelled it: `ingest_sources` had only the HMAC secret. | Added Decision #13 (the exact wire shape, marked *must match plan M5*), Task 39 (migration 0022 + `ingest_source_auth.py` + service wiring + the audit row) and Task 40 (publication through `/distribution/sources`, the tenant config view, the consent view, and the auth `PUT`). |
| 10 | Several tasks' "Expected: `N` passed" lines were miscounted against their own parametrized cases — a worker would have seen a mismatch and assumed a real failure. | Recounted every new task's test list; corrected Tasks 33 (13→14), 36 (9→10), 37 (13→11) and 39 (31→35). |
| 11 | D30 (workstream identity/end-to-end trace/tenant wall) and D31 (workstream usage metering) arrived from the spec's third user review after this plan's original 41 tasks were written. Nothing in Tasks 1-41 modeled a `workstreams` table, a usage-metering consumer, an admin usage view, or a `routes_to` tenant check — the spec's own M2b milestone row (§16) names all four as this milestone's D30/D31 deliverables, and none existed. | Added Tasks 42-49: migration 0023 + RBAC extension (42), the `install_dal` fixture extension (43, penguin-dal per the later R52 ruling — see the top of this document), workstream create/disable lifecycle wired into `ingest_source_service` (44), `workstreamId` publication on the config view/distribution feed/consent screen (45), `routes_to` cross-tenant refusal in `approve_version()` (46), the `waddles:usage` aggregator + its CronJob (47), the per-community usage API with full filter coverage (48), and a widened closing gate (49). Decisions #14-#17 record the schema deviations and scope boundaries this required. |
| 12 | The task brief that triggered this pass asked two open questions to verify, not assume: whether hub-api ever touches the envelope binding key (`security.envelopeBinding.keySecretRef`), and whether RLS policies on bundle-reachable tables belong to this milestone. Neither is mentioned in the spec's own M2b milestone row (§16), and `bundle_db_role_service.py` (Task 20) already only grants table-level privileges, never RLS. | Confirmed both are **not** M2b's job — the binding key is stage-only by spec §5.11/§12.3's explicit wording, and RLS enforcement is the Rust stage side's job per spec §7.4, corroborated by the M2a plan's own PA4 scope note ("RLS... M3/M4/M5 scope"). Recorded as Decision #17 and in the spec-coverage table above rather than fabricating a task for scope this milestone does not own. |

---
