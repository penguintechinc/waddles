# Bundle MCP-Tools — Phase 1 (Manifest Layer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `kind: app|service` and the MCP-like **exported-tools** (service bundles) and **requested-tools** (app + service bundles) manifest blocks, with validation enforcing the invocation matrix — the foundation every later MCP-tools phase builds on.

**Architecture:** Additive fields on the existing `BundleManifestV2` (hub-api) and `flask_core.AppManifest` parsers, each a new `_require`-validated defaulted-tail field + dedicated `parse_*` helper, mirroring `parse_permission_declarations`. `kind` is inferred from the `waddles.core.*` namespace heuristic when absent (back-compat) and explicit when present. A denormalized `app_catalog.kind` **alembic** column is listing convenience; `manifest_json` stays source of truth. No new RPC/host behavior here — just the typed, validated surface.

**Tech Stack:** Python 3.13, `@dataclass(slots=True, frozen=True)`, pytest (mypy --strict, ≥90% cov), alembic.

**Spec:** `docs/superpowers/specs/2026-10-29-bundle-mcp-tools-framework.md` (§5.1–5.3); prior art `docs/superpowers/specs/2026-10-13-bundle-manifest-v-next.md` (§7 kind/requires).

## Global Constraints

- Schema stays `schema_version == 2` for P1; `kind`/tool blocks are **optional additive** (defaulted tail). Full v3 + required-kind + network block = separate v-next effort.
- **A parsed-but-unenforced field is a silent vuln** — every field added here lands WITH its enforcement in the same task (spec §3).
- **Three parsers + one reparse** must stay consistent: `hub_api BundleManifestV2`, `flask_core AppManifest`, `_reparse_trusted` (bundle_approval_service.py:138). The external penguin-libs Rust `Manifest` does NOT need `kind` in P1 (no host behavior yet) — deferred to the phase that adds `tool.invoke`.
- **Two manifest files per bundle** (`bundle.yaml` + `hub-manifest.yaml`) must carry identical `kind`/tool blocks; the seeder asserts equality.
- `kind` values: exactly `app` | `service` → else `invalid_kind`. `kind` immutable per `app_id` across versions.
- `app_catalog.kind` column is **alembic** (hub-api-owned), never `config/postgres/*.sql` (fresh-DB-only).
- New deps: none.

## Review Focus

- A manifest declaring **both** an exported-tools block **and** `kind: app` → must reject `app_cannot_export_tools` (apps never serve tools). — Task 3.
- A **requested-tool whose target is itself** (self-reference) or a **duplicate** requested tool → reject `tool_self_reference` / `duplicate_requested_tool`. — Task 4.
- An existing v2 manifest with **no `kind`** must still parse (infer from namespace) — a required-kind regression would break all 77 live bundles. — Task 1.
- A tool `name` or `target_app_id` with **delimiter characters** (dots in app_ids) must round-trip through the derived `tool.call:<target>:<tool>` id without collision. — Task 4.
- `_reparse_trusted` omitting a new field → activation silently reads empty defaults (a tool grant vanishes) — reparse must thread every field. — Task 1,3,4.

---

### Task 1: `kind` field on BundleManifestV2 (+ reparse)

**Files:**
- Modify: `hub_api/services/bundle_manifest_v2.py` (dataclass ~:182-202 defaulted tail; `parse_bundle_manifest_v2` construction :768-798; add `_infer_kind`)
- Modify: `hub_api/services/bundle_approval_service.py:138` (`_reparse_trusted`)
- Test: `hub_api/tests/test_bundle_manifest_v2.py`

**Interfaces:**
- Produces: `BundleManifestV2.kind: str` (`"app"|"service"`); `_infer_kind(app_id: str, module: str) -> str` (namespace heuristic: `waddles.core.*` / connector/economy/reputation modules → `"service"`, else `"app"`).

- [ ] **Step 1: Write failing tests** — in `test_bundle_manifest_v2.py`: `test_kind_explicit_service` (`_parse({"kind":"service"}).kind == "service"`); `test_kind_defaults_inferred` (`_parse({}).kind` equals `_infer_kind(_VALID_MANIFEST["app_id"], _VALID_MANIFEST["module"])`); `test_kind_invalid` (`_parse({"kind":"widget"})` raises `ManifestV2Error`, `exc.value.reason == "invalid_kind"`).
- [ ] **Step 2: Run** `pytest hub_api/tests/test_bundle_manifest_v2.py -k kind -v` → FAIL (`kind` not a field).
- [ ] **Step 3: Implement** — add `kind: str = ""` to the defaulted tail; in `parse_bundle_manifest_v2`, read `raw.get("kind")`, `_require(kind in (None,"app","service"), "invalid_kind", ...)`, default via `_infer_kind(app_id, module)` when absent; thread `kind=` into the construction kwargs; add `_infer_kind`. Teach `_reparse_trusted` to pass `kind=raw.get("kind") or _infer_kind(...)`.
- [ ] **Step 4: Run** the same test → PASS; run the full file to confirm no regression (`_VALID_MANIFEST` with no kind still parses).
- [ ] **Step 5: Commit** `feat(manifest): add kind field (app|service) with namespace inference`.

### Task 2: `kind` on flask_core AppManifest

**Files:**
- Modify: `libs/flask_core/flask_core/app_manifest.py` (`parse_manifest` ~:395; `KNOWN_MODULES` :71)
- Test: `libs/flask_core/tests/test_app_manifest.py` (mirror its existing pattern)

**Interfaces:** Consumes Task 1's `_infer_kind` semantics (re-implement locally; flask_core must not import hub_api). Produces `AppManifest.kind`.

- [ ] **Step 1: Write failing test** — explicit `kind` preserved + absent `kind` inferred, one invalid rejected with that lib's error type.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the field + inference + validation in `parse_manifest`, matching this parser's existing error-raising style.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(flask_core): add kind to AppManifest`.

### Task 3: Exported-tools block (service bundles only)

**Files:**
- Modify: `hub_api/services/bundle_manifest_v2.py` (new `ExportedTool` dataclass + `parse_exported_tools`; field + construction + reparse)
- Modify: `hub_api/services/bundle_approval_service.py` (`_reparse_trusted`; `_derive_capabilities:231`)
- Test: `hub_api/tests/test_bundle_manifest_v2.py`

**Interfaces:**
- Produces: `ExportedTool(name: str, input_schema: dict, output_schema: dict, access: str, risk: str)` (`access ∈ {"read","write"}`, `risk ∈ {"normal","dangerous"}`); `BundleManifestV2.exported_tools: tuple[ExportedTool,...] = ()`; `parse_exported_tools(raw_entries: list[dict]) -> tuple[ExportedTool,...]`.

- [ ] **Step 1: Write failing tests** — valid service manifest with two exported tools parses to the tuple; `test_app_cannot_export_tools` (`_parse({"kind":"app","exported_tools":[{...}]})` → `exc.value.reason == "app_cannot_export_tools"`); bad access value → `invalid_tool_access`; duplicate tool name → `duplicate_exported_tool`; missing input/output schema → `invalid_tool_schema`.
- [ ] **Step 2: Run** `-k exported_tools` → FAIL.
- [ ] **Step 3: Implement** `ExportedTool` + `parse_exported_tools` (mirror `parse_permission_declarations:424`: per-entry `_require` on name/access/risk/schemas, dedupe names). In `parse_bundle_manifest_v2`: parse the block, then `_require(not exported_tools or kind == "service", "app_cannot_export_tools", ...)`. Thread into construction + `_reparse_trusted`. In `_derive_capabilities`, surface exported tools as a derived capability (gated on `kind == "service"`).
- [ ] **Step 4: Run** → PASS; full file green.
- [ ] **Step 5: Commit** `feat(manifest): exported-tools block (service-only)`.

### Task 4: Requested-tools block (+ derived tool.call ids)

**Files:**
- Modify: `hub_api/services/bundle_manifest_v2.py` (new `RequestedTool` dataclass + `parse_requested_tools` + `requested_tool_permission_id`; field + construction + reparse)
- Modify: `hub_api/services/bundle_approval_service.py` (`_reparse_trusted`)
- Test: `hub_api/tests/test_bundle_manifest_v2.py`

**Interfaces:**
- Produces: `RequestedTool(target_app_id: str, tool: str, access: str)`; `BundleManifestV2.requested_tools: tuple[RequestedTool,...] = ()`; `requested_tool_permission_id(rt: RequestedTool) -> str` returning a delimiter-safe `tool.call:<target_app_id>:<tool>` (percent-encode the dotted app_id segment so later parsing is unambiguous).

- [ ] **Step 1: Write failing tests** — app manifest requesting two tools parses; `requested_tool_permission_id` round-trips a dotted `target_app_id` without collision (`tool.call:` + encoded target + `:` + tool); `test_tool_self_reference` (target == own app_id → `tool_self_reference`); duplicate `(target,tool)` → `duplicate_requested_tool`; bad access → `invalid_tool_access`.
- [ ] **Step 2: Run** `-k requested_tools` → FAIL.
- [ ] **Step 3: Implement** `RequestedTool` + `parse_requested_tools` (per-entry `_require`, dedupe `(target,tool)`, reject self-reference) + `requested_tool_permission_id`. Both `kind`s may request (dual-role services). Thread into construction + `_reparse_trusted`. (No grant-catalog wiring yet — that's P2; here we only produce the id string + validate shape.)
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(manifest): requested-tools block + tool.call id derivation`.

### Task 5: `app_catalog.kind` alembic column

**Files:**
- Create: `alembic/versions/<id>_app_catalog_kind.py` (down_revision = current head `0067_merge_heads_20261028`; id ≤32 chars, e.g. `0068_app_catalog_kind`)
- Modify: `hub_api/services/marketplace_lifecycle_service.py:275-294` (write `kind` on `app_catalog.insert`)
- Modify: DAL table def `schema.py:2296` (add nullable `kind` column to `app_catalog`)
- Test: `hub_api/tests/` lifecycle test mirroring the existing app_catalog insert test

**Interfaces:** Consumes `AppManifest.kind` (Task 2). Column nullable (back-compat); `manifest_json` remains source of truth.

- [ ] **Step 1: Write failing test** — seeding/inserting an app_catalog row persists `kind` and it's queryable.
- [ ] **Step 2: Run** → FAIL (no column).
- [ ] **Step 3: Implement** the alembic `add_column` (nullable `String`), the DAL field, and set `kind=manifest.kind` in the lifecycle insert.
- [ ] **Step 4: Run** `alembic upgrade head` locally + the test → PASS; confirm `alembic heads` == 1.
- [ ] **Step 5: Commit** `feat(db): denormalized app_catalog.kind (alembic)`.

### Task 6: Matrix enforcement — app→app forbidden (activation-time)

**Files:**
- Modify: the activation/requires-validation path (same place v-next checks a `requires` target resolves to `kind: service` — `bundle_approval_service.py` activation; recon §2 `requires_unsatisfied` analogue)
- Test: `hub_api/tests/` activation test

**Interfaces:** Consumes Task 4's `requested_tools` + the target's stored `kind`.

- [ ] **Step 1: Write failing test** — activating an `app` bundle whose `requested_tools` target resolves to another `app` bundle → rejected `tool_target_not_service` (app→app forbidden). A target resolving to `kind: service` → allowed. A `service` requesting a `service` tool → allowed (dual-role).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** at activation: for each `requested_tool`, resolve the target bundle's `kind`; `_require(target.kind == "service", "tool_target_not_service", ...)`. (Enforced at validate-time where the target is already known; otherwise activation-time, mirroring `requires`.)
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(manifest): enforce app→app tool-call ban (target must be service)`.

### Task 7: Seeder two-file equality + migrate core bundle manifests

**Files:**
- Modify: `hub_api/cli/seed_core_bundles.py` (assert `bundle.yaml` and `hub-manifest.yaml` agree on `kind`/tool blocks, fail loud on mismatch)
- Modify: the `service`-kind core bundles' `bundle.yaml` + `hub-manifest.yaml` (add explicit `kind: service` to connector/economy/reputation bundles; leave app bundles to inference or add `kind: app`)
- Test: `hub_api/tests/test_bundle_manifest_catalog_edge_cases.py`

**Interfaces:** Consumes Tasks 1,3,4.

- [ ] **Step 1: Write failing test** — a catalog entry whose two manifest files disagree on `kind` → `CoreBundleSeederError` with a mismatch reason; a matching pair seeds.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the equality assertion in `seed_one`; add `kind: service` to the known service bundles' BOTH manifest files.
- [ ] **Step 4: Run** the seeder test + a full `seed_core_bundles` dry pass → PASS.
- [ ] **Step 5: Commit** `feat(seed): enforce bundle.yaml/hub-manifest kind agreement + tag service bundles`.

---

## Self-Review

- **Spec coverage:** §5.1 kind (T1,T2,T5,T7) · §5.2 exported-tools (T3) · §5.3 requested-tools + tool.call id (T4) · matrix app→app-forbidden (T6) · two-file agreement (T7). Grant-catalog/host-invocation/approval/SDK are later phases — out of P1 scope by design.
- **Review Focus:** app-exports-tools (T3), self/dup requested (T4), no-kind back-compat (T1), delimiter round-trip (T4), reparse completeness (T1,3,4) — each pinned to a test above.
- **Deferred (flagged):** external penguin-libs Rust `Manifest` learns `kind` only when host `tool.invoke` lands (later phase); full schema_version-3 + network block = v-next effort; grant-time per-tool enforcement = P2.
