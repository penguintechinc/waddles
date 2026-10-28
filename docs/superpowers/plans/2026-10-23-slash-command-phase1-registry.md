# Slash-Command Phase 1: Command Registry Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the hub-api `app_commands` registry (table + manifest `commands:` field + activation-time write path + auto-inference from existing `consumes` + first-registered-owner conflict warning) so existing bundles register commands with zero manifest edits.

**Architecture:** One new Alembic table `app_commands` (rows keyed `(tenant, community, app, command)`, `community_id 0` = tenant-wide, ownership = `(registered_at, id)` order). A new service `hub_api/services/app_command_service.py` mirrors `app_source_binding_service.py`: pure functions resolve the desired rows (inferred from `consumes`, overridden/augmented by the manifest `commands:` field), and `sync_commands(conn, ...)` writes them INSIDE the existing activation `engine.begin()` transaction, returning activation warnings. Deactivation clears them in the same tx. A self-heal backfill covers already-active bundles that the seeder no-ops over.

**Tech Stack:** Python 3.13, Quart, penguin-dal `AsyncDB` + SQLAlchemy Core inside `engine.begin()` (house pattern, `bundle_approval_service.py:606`), Alembic raw-SQL migrations, pytest (`asyncio_mode=auto`), real Postgres via `alembic/tests/pg_docker.py`.

**Spec:** `docs/superpowers/specs/2026-10-23-slash-command-architecture-design.md` (branch `docs/slash-command-architecture`, not yet on `release/v3.0.X`). Sections 1, 9 (auto-infer + no-command warning) and 6 (activation-time conflict warning) ONLY. Phases 2-5 (ingest reader, active-context, routing, slash registration, token store, response ownership enforcement, watchdog) are OUT OF SCOPE; Phase 1 only WRITES the registry.

## Global Constraints

Every task's requirements implicitly include these (copied from house rules):

- Python 3.13 + Quart; every function annotated (`mypy --strict`), `@dataclass(slots=True)` (frozen for value objects), `python3` never bare `python`.
- penguin-dal parameterized queries only (no f-string SQL) + per-service DB accounts: hub-api is the only writer of `app_commands` (RBAC matrix rows for every other role, empty privileges).
- Numeric PK internal / UUID across boundaries / int enum for type: `app_commands.id BIGSERIAL`; `command_types` is an int bitmask (`CommandType` IntFlag), never a string; numeric `tenant_id`/`community_id` keys only.
- PII boundary = hub-api: registry rows hold command verbs, app ids and numeric ids only; no usernames/handles/message text in rows, logs or warnings.
- 90%+ coverage, lines AND branches (`hub_api/.coveragerc` `fail_under`); a green run reports the pytest summary denominator.
- Real-path tests: the registry write path is exercised against a live migrated Postgres (Task 8), not only the sqlite unit fixture; no mocking at the registry boundary except the one rollback fault-injection in Task 5.
- No-stubs / fail-loud: no `|| true`, no swallowed exceptions; a manifest or backfill failure is surfaced (logged WARNING + returned), never silently dropped.
- Logging via the module convention (`logging.getLogger(__name__)` + `extra=` dicts, as `app_source_binding_service.py:56`; OTel is attached by hub-api's existing logging/OTel bootstrap); never log raw user input.
- Alembic only (not `config/postgres/*.sql`: those replay on fresh DBs only); revision id <= 32 chars; single head (`scripts/ci/check-alembic-single-head.sh`).

## Review Focus

Failure modes the spec implies but a straightforward build misses (each has a pinning test in the owning task):

1. Version bump / re-sync of an ALREADY-active app must keep its `registered_at` (no re-queue, no warning against itself); only deactivate -> reactivate re-queues. (Task 4, Task 5)
2. Scope edges: tenant-wide (`community_id 0`) rows collide with community rows of the SAME tenant in both directions; the same verb in another tenant/community never conflicts. (Task 4)
3. Unusable inferred input (`!Ping`, `!`, `ping` with no prefix char, 33-char verb, rule on an unsupported platform) is skipped with a WARN, never raises and never blocks an activation that worked before; zero resulting rows on a process bundle yields a `no_routable_command` warning. (Task 3, Task 4)
4. Already-active bundles on existing DBs (seeder no-ops over them) get rows via the idempotent backfill, and re-running it never reorders owners. (Task 7)
5. Simultaneous activations/equal timestamps: owner is deterministic by `(registered_at, id)`; a race may lose a warning but never yields two owners. (Task 4, Task 8)

## Grounded Facts (read from `origin/release/v3.0.X` @ 881ac8dd)

| Need | Real location |
|---|---|
| Migration system | Alembic `alembic/versions/`, head `0061_port_legacy_sql_103_codec` (`0061...py:41`); template `0059_suppression_records.py:51-120` (RBAC matrix load + `GRANT USAGE ON SEQUENCE`) |
| Bootstrap FK tables | `app_catalog(app_id VARCHAR(255) PK)`; `app_versions` `0022...py:77`; `app_active_versions` PK `(app_id, tenant_id, community_id)` `0022...py:110`, `community_id 0` = tenant-wide sentinel |
| RBAC matrix | `config/postgres/rbac-matrix.yaml` (`tables:` list line 20-55; per-table role rows e.g. `suppression_records` at line 859); completeness test `hub_api/tests/test_rbac_matrix_loader.py:34` |
| Manifest parsing | `hub_api/services/bundle_manifest_v2.py`: `ConsumeRule` :79, `BundleManifestV2` :122, `_parse_consumes` :212, `parse_bundle_manifest_v2` :399; `ConsumeRule.filters["command_prefix"]` is a `list[str]` such as `["!about", "!bot"]` (present on 341 rules across 86 `bundles/*/*/hub-manifest.yaml`; every prefix is `!`+`[a-z0-9_-]+`; 6 bare rules: `bundles/python/greeter`, `bundles/rust/ping`) |
| Stored manifest | `app_version_uploads.manifest_json` = the raw dict (`bundle_version_service.py:362`), re-read by `bundle_approval_service._reparse_trusted` :117 |
| Activation write path | `bundle_approval_service._write_approval_and_activate` :606 and `_write_tenant_wide_approval_and_activate` :765 (single `install_dal.engine.begin()`; `sync_bindings` call is the model to copy); public `activate_for_community` :1031, `activate_tenant_wide` :893, `deactivate_for_community` :1174, `deactivate_tenant_wide` :1228 |
| Activation HTTP | `hub_api/blueprints/v1/bundle_activation.py:131` (`activate`, `MessageResponse` 201); OpenAPI export `make openapi-hubapi` -> `openapi/v1.yaml:11778` |
| Seeder | `hub_api/cli/seed_core_bundles.py`: `seed_one` :835 (routes through `activate_*`, but `_already_active` :557 makes re-runs a no-op), `_run` :1353 |
| Test fixtures | sqlite `install_dal` `hub_api/tests/conftest.py:1791`, DDL mirror `_create_bundle_install_tables` (`app_source_bindings` mirror at :1638); real PG `alembic/tests/pg_docker.py::migrated_postgres`, hub-api PG fixtures `hub_api/tests/test_seed_core_bundles_pg_integration.py` (`pg_db`, `pg_dal`) |
| Run tests | `cd hub_api && python3 -m pytest tests/<file> --no-cov -v` (omit `--no-cov` for the 90% gate); alembic: `python3 -m pytest alembic/tests/<file> --no-cov -v` |

## Deviations / decisions the real code forced (reviewers: check these)

- `platforms` is a JSONB array and `command_types` a SMALLINT bitmask (1=standard, 2=webhook), not Postgres array/enum columns, so the sqlite unit fixture mirrors the schema; overlap is computed in Python.
- Auto-infer: ANY consume rule without `command_prefix` becomes a `*` row (not only `chat.message`), otherwise event-only bundles would have no row and be dropped at the Phase 3 cutover.
- A collision requires overlapping `platforms` (a `*` platform overlaps everything); two apps registering the same verb on different platforms do not warn.
- Added a nullable `description VARCHAR(100)` column + manifest field now (Discord requires it in Phase 4; avoids a second migration).
- `_write_*_approval_and_activate` return a 4-tuple (extra warnings); public `activate_*` signatures/returns are unchanged, with new `activate_*_with_warnings` siblings for callers that want warnings.
- RBAC: `svc_ingest` gets an EMPTY row now; Phase 2 widens it (or a read-replica role) when the reader lands.
- Ownership write semantics: `registered_at` is set at first insert and preserved on upsert; deactivate DELETES rows so reactivation re-queues.

---

### Task 1: `app_commands` migration, RBAC matrix, unit-fixture mirror

**Files:**
- Create: `alembic/versions/0062_app_commands_registry.py`
- Modify: `config/postgres/rbac-matrix.yaml` (add `app_commands` to `tables:` after `suppression_records`, plus 8 role rows after line ~889)
- Modify: `hub_api/tests/conftest.py` (add sqlite mirror `Table("app_commands", ...)` inside `_create_bundle_install_tables`, after the `app_source_bindings` mirror at :1638)
- Test: `alembic/tests/test_0062_app_commands_registry.py` (real Postgres, style of `test_0059_suppression_records.py`)

**Interfaces:**
- Produces: table `app_commands(id BIGSERIAL PK, tenant_id INTEGER NOT NULL REFERENCES tenants(id), community_id INTEGER NOT NULL DEFAULT 0, app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id), command VARCHAR(32) NOT NULL, command_types SMALLINT NOT NULL DEFAULT 1, platforms JSONB NOT NULL, description VARCHAR(100), registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`; `UNIQUE (tenant_id, community_id, app_id, command)`; index `idx_app_commands_owner (tenant_id, community_id, command, registered_at, id)`.
- Checks: `command = '*' OR command ~ '^[a-z0-9][a-z0-9_-]{0,31}$'`; `command_types BETWEEN 1 AND 3`; `jsonb_typeof(platforms) = 'array' AND jsonb_array_length(platforms) > 0`.
- Revision: `revision = "0062_app_commands_registry"`, `down_revision = "0061_port_legacy_sql_103_codec"`.

- [ ] **Step 1: Write the failing real-PG test** (`requires_docker` skip marker as in test_0059). Tests, each using `migrated_postgres` and psycopg2:

```python
def test_upgrade_creates_app_commands_columns(db):   # information_schema set equality with the Interfaces column list
def test_unique_tenant_community_app_command(db):    # 2nd identical insert -> psycopg2.errors.UniqueViolation
def test_command_check_accepts_wildcard_and_verbs(db):  # '*', 'ping', 'chat-history' insert OK
@pytest.mark.parametrize("bad", ["Ping", "!ping", "a b", "", "x" * 33])
def test_command_check_rejects(db, bad):             # CheckViolation
@pytest.mark.parametrize("bad", [0, 4])
def test_command_types_check_rejects(db, bad):       # CheckViolation; 1, 2, 3 accepted
def test_platforms_must_be_nonempty_array(db):       # '[]' and '"discord"' -> CheckViolation
def test_app_id_fk_enforced(db):                     # unknown app_id -> ForeignKeyViolation
def test_grants_hub_api_only(db):                    # has_table_privilege: hub_api INSERT/SELECT/UPDATE/DELETE true; svc_ingest/svc_process/svc_action/svc_streaming/webui/waddles_publisher SELECT false
def test_round_trip_downgrade_upgrade(db):           # alembic_cli downgrade to PREVIOUS_REVISION drops table; upgrade head restores
```
PREVIOUS_REVISION = `"0061_port_legacy_sql_103_codec"`.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest alembic/tests/test_0062_app_commands_registry.py --no-cov -v`
Expected: FAIL (`relation "app_commands" does not exist`); a SKIP-all result means docker is missing and does NOT count as red/green; run where docker exists.

- [ ] **Step 3: Implement `upgrade()`/`downgrade()` in `alembic/versions/0062_app_commands_registry.py`**

Copy the 0059 structure: `CREATE TABLE IF NOT EXISTS`, `COMMENT ON TABLE` (state: hub-api-owned, `community_id 0` = tenant-wide sentinel, ownership = `(registered_at, id)` ascending, Phase 1 writer only), the owner index, matrix-driven grants (`_MATRIX_TABLES = frozenset({"app_commands"})`, `render_revoke_public_sql` + `render_grant_sql`) and `GRANT USAGE ON SEQUENCE app_commands_id_seq TO hub_api`. `downgrade()` = `DROP TABLE IF EXISTS app_commands`.

- [ ] **Step 4: Add the RBAC rows and the sqlite mirror**

`rbac-matrix.yaml`: `hub_api` and `migration_runner` = `[SELECT, INSERT, UPDATE, DELETE]`; `waddles_publisher`, `svc_ingest`, `svc_process`, `svc_action`, `svc_streaming`, `webui` = `[]` (comment: Phase 2 widens `svc_ingest`). `conftest.py` mirror: same columns with plain types (`platforms` as `JSON`, `command_types` `SmallInteger`, `id` autoincrement Integer, `registered_at` `DateTime`), no FKs.

- [ ] **Step 5: Run gates**

Run: `python3 -m pytest alembic/tests/test_0062_app_commands_registry.py --no-cov -v` -> Expected: 14 passed (0 skipped). Then `cd hub_api && python3 -m pytest tests/test_rbac_matrix_loader.py --no-cov -v` -> PASS, and `bash scripts/ci/check-alembic-single-head.sh .` -> exit 0 reporting 1 head.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0062_app_commands_registry.py alembic/tests/test_0062_app_commands_registry.py config/postgres/rbac-matrix.yaml hub_api/tests/conftest.py
git commit -m "feat(hub-api): app_commands registry table (migration 0062)"
```

### Task 2: Manifest `commands:` field

**Files:**
- Modify: `hub_api/services/bundle_manifest_v2.py` (new types after `ConsumeRule` :79; field on `BundleManifestV2` :122; call in `parse_bundle_manifest_v2` :399 after `supported_platforms` is parsed)
- Modify: `hub_api/services/bundle_approval_service.py:117` (`_reparse_trusted` re-parses `commands`)
- Test: `hub_api/tests/test_bundle_manifest_v2_commands.py`

**Interfaces:**
- Produces (in `bundle_manifest_v2`):
  - `COMMAND_WILDCARD = "*"`; `COMMAND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")`
  - `class CommandType(enum.IntFlag): STANDARD = 1; WEBHOOK = 2` (values equal the DB bitmask)
  - `@dataclass(slots=True, frozen=True) class CommandDeclaration: name: str; types: CommandType; platforms: tuple[str, ...] | None; description: str | None`
  - `def parse_command_declarations(raw_entries: Any, *, known_custom_platforms: frozenset[str], supported_platforms: tuple[str, ...] | None) -> tuple[CommandDeclaration, ...]`
  - `BundleManifestV2.command_declarations: tuple[CommandDeclaration, ...] = ()` (defaulted last field so hand-built manifests in existing tests keep working)
- Manifest shape: `commands: [{name: ping, types: [standard, webhook], platforms: [discord], description: "..."}]`; `types` default `[standard]`; `platforms` default None (resolved in Task 3); `*` is standard-only.
- Error reasons (`ManifestV2Error.reason`): `invalid_commands`, `invalid_command_name`, `duplicate_command`, `invalid_command_type`, `invalid_command_platforms`, `command_platform_unsupported`, `invalid_command_description` (1-100 chars).

- [ ] **Step 1: Write the failing tests** (build raw manifests from a minimal valid dict helper copied from `test_bundle_manifest_v2.py`):

```python
def test_manifest_without_commands_has_empty_declarations():
    assert _parse(base()).command_declarations == ()
def test_command_defaults_to_standard_no_platforms():
    m = _parse({**base(), "commands": [{"name": "ping"}]})
    assert m.command_declarations == (CommandDeclaration("ping", CommandType.STANDARD, None, None),)
def test_types_and_platforms_parsed():
    d = _parse({**base(), "commands": [{"name": "ping", "types": ["standard", "webhook"], "platforms": ["discord"], "description": "Pong"}]}).command_declarations[0]
    assert d.types == CommandType.STANDARD | CommandType.WEBHOOK and d.platforms == ("discord",) and d.description == "Pong"
def test_wildcard_is_standard_only():
    _parse({**base(), "commands": [{"name": "*"}]})                       # ok
    with pytest.raises(ManifestV2Error) as e: _parse({**base(), "commands": [{"name": "*", "types": ["webhook"]}]})
    assert e.value.reason == "invalid_command_type"
@pytest.mark.parametrize("bad", ["Ping", "!ping", "a b", "", "x" * 33, 5])
def test_bad_name(bad):  # reason == "invalid_command_name"
def test_duplicate_command():  # two {"name": "ping"} -> "duplicate_command"
@pytest.mark.parametrize("types", [["slash"], []])
def test_bad_types():  # "invalid_command_type"
def test_unknown_platform():  # platforms ["myspace"] -> "invalid_command_platforms"
def test_platform_outside_supported_platforms():  # supported_platforms ["twitch"], command platforms ["discord"] -> "command_platform_unsupported"
def test_description_too_long():  # 101 chars -> "invalid_command_description"
def test_commands_not_a_list():  # {"commands": "ping"} -> "invalid_commands"
def test_reparse_trusted_round_trips_commands():  # bundle_approval_service._reparse_trusted(raw).command_declarations == parsed
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_manifest_v2_commands.py --no-cov -v`
Expected: FAIL (`ImportError: cannot import name 'CommandDeclaration'`).

- [ ] **Step 3: Implement** the types and `parse_command_declarations` in `bundle_manifest_v2.py` (reuse `_require`; platform validation = member of `KNOWN_SUPPORTED_PLATFORMS`, or `custom:<n>` with `n in known_custom_platforms`, mirroring `_parse_supported_platforms` :173 but with the command-specific reason codes). Pass `command_declarations=` in the `BundleManifestV2(...)` constructor call at the end of `parse_bundle_manifest_v2`, and in `_reparse_trusted` call `parse_command_declarations(raw.get("commands"), known_custom_platforms=frozenset(), supported_platforms=...)` (already-validated blob: any custom platform names are accepted, so pass the declared names' own `custom:` set derived from `raw`; do not re-validate tenant registration).

- [ ] **Step 4: Run to verify it passes**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_manifest_v2_commands.py tests/test_bundle_manifest_v2.py tests/test_bundle_manifest_v2_permissions.py tests/test_bundle_approval_service.py --no-cov -q`
Expected: all PASS (existing manifest tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_manifest_v2.py hub_api/services/bundle_approval_service.py hub_api/tests/test_bundle_manifest_v2_commands.py
git commit -m "feat(hub-api): bundle manifest v2 commands: field"
```

### Task 3: Desired-command resolution (auto-infer + explicit override)

**Files:**
- Create: `hub_api/services/app_command_service.py` (pure part only in this task)
- Test: `hub_api/tests/test_app_command_inference.py`

**Interfaces:**
- Consumes: `BundleManifestV2.consumes`, `.command_declarations`, `.supported_platforms`, `.supports_platform()`, `CommandType` (Task 2).
- Produces:
  - `@dataclass(slots=True, frozen=True) class DesiredCommand: command: str; types: CommandType; platforms: tuple[str, ...]; description: str | None`
  - `def verb_from_prefix(prefix: str) -> str | None` : strip exactly one leading non-alphanumeric char; return the rest only if it matches `COMMAND_NAME_RE`, else `None`.
  - `def infer_commands(manifest: BundleManifestV2) -> tuple[DesiredCommand, ...]` : per consume rule on a supported platform, each `command_prefix` entry -> `(verb, STANDARD, (platform,))`; a rule with no/empty `command_prefix` -> `("*", STANDARD, (platform,))`; rows with the same command merge by platform union (sorted); output sorted by command. An unusable prefix is skipped with `logger.warning("app command inference: unusable command_prefix", extra={"app_id": ..., "op": "infer_commands"})` (never the prefix value if it could be user text; prefixes are manifest data, but log only `app_id` + a count).
  - `def resolve_desired_commands(manifest: BundleManifestV2) -> tuple[DesiredCommand, ...]` : inferred rows, then each explicit declaration REPLACES the inferred row of the same name (its types/platforms/description win) or is added; an explicit `platforms=None` defaults to the platforms of the consume rules on supported platforms, else `manifest.supported_platforms`, else `("*",)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_prefix_rule_infers_standard_row_per_verb():   # twitch+discord ["!about","!bot"] -> about,bot each (DISCORD,TWITCH sorted) STANDARD
def test_bare_rule_infers_wildcard():                   # greeter shape -> [DesiredCommand("*", STANDARD, ("discord","twitch"), None)]
def test_mixed_rules_per_platform():                    # discord prefix "!ping" + twitch bare -> ping:(discord,) and *:(twitch,)
def test_bare_non_chat_event_type_is_wildcard():        # event_types ("member.join",) -> "*"
def test_unsupported_platform_rule_skipped():           # supported_platforms ("discord",), twitch rule dropped
@pytest.mark.parametrize("p", ["!Ping", "!", "ping", "!a b", "!" + "x" * 33])
def test_unusable_prefix_skipped_and_warned(p, caplog):  # result == () for a lone rule; caplog has WARNING "unusable command_prefix"; no raise
def test_explicit_declaration_overrides_same_name():     # inferred ping(discord,twitch,STANDARD) + explicit ping webhook discord -> single ping row, types == STANDARD|WEBHOOK (explicit types only), platforms == ("discord",)
def test_explicit_declaration_augments():                # explicit "slashonly" added alongside inferred rows
def test_explicit_default_platforms_follow_consumes():   # explicit platforms None -> consumed platforms
def test_verb_from_prefix_cases():                       # "!ping"->"ping", "?x"->"x", "ping"->None
def test_every_core_hub_manifest_infers_a_row():         # glob bundles/*/*/hub-manifest.yaml; parse_bundle_manifest_v2(..., known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True); assert examined == 86-glob-count and examined >= 80; every manifest resolves >= 1 row; prefix rows equal that manifest's own distinct command_prefix verbs
```
(The last test is the parity/denominator guard: it fails if the glob finds zero files.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd hub_api && python3 -m pytest tests/test_app_command_inference.py --no-cov -v`
Expected: FAIL (`ModuleNotFoundError: services.app_command_service`).

- [ ] **Step 3: Implement** the four functions above in `app_command_service.py` with module docstring stating the registry contract (scope sentinel 0, ownership order, inference rules). Constant `TENANT_WIDE_COMMUNITY_SENTINEL = 0` defined locally (same circular-import reasoning as `app_source_binding_service.py:56`).

- [ ] **Step 4: Run to verify it passes**

Run: `cd hub_api && python3 -m pytest tests/test_app_command_inference.py --no-cov -v` -> Expected: all PASS, parity test reports the examined count in its assertion message.

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/app_command_service.py hub_api/tests/test_app_command_inference.py
git commit -m "feat(hub-api): infer app_commands rows from consumes + commands override"
```

### Task 4: Registry write path, ownership and conflict warnings

**Files:**
- Modify: `hub_api/services/app_command_service.py`
- Test: `hub_api/tests/test_app_command_service.py` (sqlite `install_dal` fixture; transactions via `install_dal.engine.begin()` exactly as `test_app_source_binding_service.py` does)

**Interfaces:**
- Consumes: `resolve_desired_commands`, `DesiredCommand` (Task 3); table `install_dal.metadata.tables["app_commands"]` (Task 1 mirror).
- Produces:
  - `@dataclass(slots=True, frozen=True) class ActivationWarning: code: str; app_id: str; command: str | None; owner_app_id: str | None; message: str` with `WARNING_COMMAND_CONFLICT = "command_conflict"`, `WARNING_NO_ROUTABLE_COMMAND = "no_routable_command"`.
  - `async def sync_commands(conn: Any, *, tenant_id: int, community_id: int | None, app_id: str, manifest: BundleManifestV2, commands_table: Any, now: datetime | None = None) -> tuple[ActivationWarning, ...]` : inside the caller's tx. Stored scope = `0` when `community_id is None`. Replace semantics: delete this app's rows in scope whose command is no longer desired; update changed rows keeping `registered_at`; insert new rows with `registered_at=now or datetime.now(UTC)`. Then, for each desired NON-wildcard command, load rows of OTHER apps for the same `tenant_id` and command whose scope overlaps (scope `C`: rows with `community_id in (C, 0)`; scope `0`: every community of the tenant) and whose `platforms` overlap (either side containing `"*"` overlaps); the owner is the minimum `(registered_at, id)` over those rows plus this app's own row; emit a `command_conflict` warning iff the owner is not this app. If the manifest has a `process` stage and the desired set is empty, return a single `no_routable_command` warning. `*` rows never warn.
  - `async def clear_commands(conn: Any, *, tenant_id: int, community_id: int | None, app_id: str, commands_table: Any) -> None` : deletes this app's rows in scope (`None` -> 0).
  - `async def command_owner(install_dal: AsyncDB, *, tenant_id: int, community_id: int, command: str, platform: str | None = None) -> str | None` : `app_id` of the first-registered non-wildcard row visible in that community (`community_id` or 0) overlapping `platform` (None = any); `None` if none. (Read helper for tests and Phase 2; no hot-path use.)

- [ ] **Step 1: Write the failing tests** (helper `_manifest(app_id, prefixes_by_platform)` builds a `BundleManifestV2` like `test_app_source_binding_service.py:_manifest`; `t0 < t1 < t2` fixed datetimes passed as `now`):

```python
async def test_sync_inserts_rows_tenant_wide_stored_as_zero():   # community_id=None -> rows.community_id == 0, registered_at == t0
async def test_resync_is_idempotent_and_preserves_registered_at(): # sync at t0 then t1 -> count unchanged, registered_at == t0   [Review Focus 1]
async def test_resync_drops_removed_and_keeps_kept_order():       # narrowed manifest deletes removed verb; kept verb registered_at unchanged
async def test_first_registered_owns_second_gets_warning():
    # A@t0 then B@t1 same community, same !ping -> B returns (ActivationWarning(code="command_conflict", app_id=B, command="ping", owner_app_id=A, message=...),); A re-sync at t2 returns ()
async def test_wildcard_rows_never_warn():                        # both apps bare -> () each
async def test_disjoint_platforms_do_not_conflict():              # A discord, B twitch -> ()
async def test_other_tenant_and_other_community_never_conflict(): # [Review Focus 2]
async def test_tenant_wide_and_community_conflict_both_directions(): # A community 5 then B tenant-wide warns; reversed order warns the later one
async def test_equal_registered_at_is_broken_by_id():             # same now -> owner = lower id; exactly one owner  [Review Focus 5]
async def test_clear_commands_removes_only_that_app_and_scope():
async def test_reactivation_requeues_behind_interim_app():        # A t0, B t1, clear A, sync A t2 -> A's warning names B; command_owner == B  [Review Focus 1]
async def test_no_routable_command_warning():                     # process bundle whose only rule is on an unsupported platform -> (ActivationWarning(code="no_routable_command", app_id=A, command=None, owner_app_id=None, ...),)
async def test_command_owner_picks_first_registered_and_filters_platform(): # None when empty
async def test_sync_logs_counts_without_command_text(caplog):     # INFO "app commands: synced" extra rows/warnings counts, no verb strings
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd hub_api && python3 -m pytest tests/test_app_command_service.py --no-cov -v`
Expected: FAIL (`ImportError: cannot import name 'sync_commands'`).

- [ ] **Step 3: Implement** `ActivationWarning`, `sync_commands`, `clear_commands`, `command_owner` in `app_command_service.py` using SQLAlchemy Core against the reflected table (parameter-bound, no string SQL; `platforms` stored as a sorted list). Warning `message` is a fixed template naming `owner_app_id` and the command (no user text). INFO log `extra={"app_id", "tenant_id", "community_id", "rows", "warnings"}`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd hub_api && python3 -m pytest tests/test_app_command_service.py tests/test_app_command_inference.py --no-cov -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/app_command_service.py hub_api/tests/test_app_command_service.py
git commit -m "feat(hub-api): app_commands write path, ownership + conflict warnings"
```

### Task 5: Wire into activation and deactivation (same transaction)

**Files:**
- Modify: `hub_api/services/bundle_approval_service.py` (`_write_approval_and_activate` :606 and `_write_tenant_wide_approval_and_activate` :765: call `sync_commands` right after `sync_bindings`, before the `app_active_versions` write, and return `warnings` as a 4th tuple element; `deactivate_for_community` :1174 and `deactivate_tenant_wide` :1228: `clear_commands` inside the existing `engine.begin()`; split `activate_for_community` :1031 / `activate_tenant_wide` :893 into `*_with_warnings` + unchanged wrappers; import `app_command_service`)
- Test: `hub_api/tests/test_bundle_activation_commands.py` (reuse `_install_and_make_available` and `_MANIFEST` pattern from `test_bundle_activation_service.py`; manifests carry `command_prefix` filters)

**Interfaces:**
- Consumes: `sync_commands`, `clear_commands`, `ActivationWarning` (Task 4).
- Produces:
  - `@dataclass(slots=True, frozen=True) class ActivationResult: approval: Any; warnings: tuple[ActivationWarning, ...]`
  - `async def activate_for_community_with_warnings(install_dal: AsyncDB, *, tenant_id: int, community_id: int, app_id: str, activated_by: int | None, valkey_client: Any | None = None, approval_source: str = "human") -> ActivationResult`
  - `async def activate_tenant_wide_with_warnings(install_dal: AsyncDB, *, tenant_id: int, app_id: str, activated_by: int | None, valkey_client: Any | None = None, approval_source: str = "system:core-seeder") -> ActivationResult`
  - `activate_for_community(...) -> Any` and `activate_tenant_wide(...) -> Any` keep their current signatures and return the approval row (`result.approval`).
  - Each `*_with_warnings` logs each warning at WARNING (`"bundle activation: command warning"`, extra `app_id, tenant_id, community_id, code`) AFTER commit.

- [ ] **Step 1: Write the failing tests**

```python
async def test_activate_registers_inferred_commands():          # prefix ["!ping","!pong"] -> app_commands rows ping,pong at community scope, platforms ["twitch"], types 1
async def test_tenant_wide_activation_registers_scope_zero():   # activate_tenant_wide -> community_id == 0
async def test_second_app_same_command_returns_warning_naming_first():  # two apps, both !ping, same community: activate_for_community_with_warnings(B).warnings[0].owner_app_id == A; A stays command_owner
async def test_legacy_activate_returns_approval_row_unchanged():  # activate_for_community(...).approved_by == 7 (existing contract)
async def test_version_bump_reactivation_keeps_ownership():     # activate A, activate B, re-run activate A (already active) -> A.registered_at unchanged, no warning, command_owner == A   [Review Focus 1]
async def test_deactivate_then_reactivate_requeues():           # A, B active; deactivate A (rows gone); activate A again -> warning names B
async def test_deactivate_tenant_wide_clears_scope_zero_rows():
async def test_activation_rolls_back_commands_when_a_later_write_fails(monkeypatch):
    # wrap app_command_service.sync_commands: call real then raise RuntimeError; assert no app_commands rows, no app_active_versions row
async def test_explicit_commands_field_overrides_inferred_on_activation():  # manifest commands:[{name:ping, types:[standard,webhook], platforms:[discord]}] -> row command_types == 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_activation_commands.py --no-cov -v`
Expected: FAIL (`AttributeError: ... activate_for_community_with_warnings`).

- [ ] **Step 3: Implement.** Move the existing bodies of `activate_for_community`/`activate_tenant_wide` into the `*_with_warnings` functions (return `ActivationResult(approval=..., warnings=...)`), make the originals one-line wrappers; extend the two `_write_*` return tuples with `warnings`; add the `clear_commands` calls in both deactivations. Update the module docstring (tiers section) with one paragraph on the registry write.

- [ ] **Step 4: Run to verify it passes (and nothing regressed)**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_activation_commands.py tests/test_bundle_activation_service.py tests/test_bundle_approval_service.py tests/test_tenant_app_availability_service.py tests/test_app_source_binding_service.py --no-cov -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/bundle_approval_service.py hub_api/tests/test_bundle_activation_commands.py
git commit -m "feat(hub-api): register app_commands on activation, clear on deactivation"
```

### Task 6: Activation warnings in the HTTP response

**Files:**
- Modify: `hub_api/blueprints/v1/bundle_activation.py:131` (`activate` uses `activate_for_community_with_warnings`; new response DTOs)
- Modify: `openapi/v1.yaml` (regenerate via `make openapi-hubapi`)
- Test: `hub_api/tests/test_bundle_activation_blueprint.py` (append)

**Interfaces:**
- Consumes: `svc.activate_for_community_with_warnings` (Task 5).
- Produces:
  - `@dataclass(slots=True, frozen=True) class ActivationWarningDTO: code: str; appId: str; command: str | None; ownerAppId: str | None; message: str`
  - `@dataclass(slots=True, frozen=True) class ActivateResponse: success: bool; message: str; warnings: list[ActivationWarningDTO] = field(default_factory=list)`; `POST /api/v1/apps/community/<community_id>/activation` returns `ActivateResponse`, status 201 (always 201; warnings never change the status).

- [ ] **Step 1: Write the failing tests**

```python
async def test_activate_response_has_empty_warnings_by_default():
    # 201 and set(body) == {"success", "message", "warnings"} and body["warnings"] == []   (exact field set, security.md Output Validation)
async def test_activate_conflict_warning_names_owner_and_is_still_201():
    # activate app A then app B (both !ping) as platform-admin -> second response 201,
    # body["warnings"][0] keys == {"code","appId","command","ownerAppId","message"}, code == "command_conflict", ownerAppId == A
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_activation_blueprint.py -k "warnings" --no-cov -v`
Expected: FAIL (`KeyError: 'warnings'`).

- [ ] **Step 3: Implement** the DTOs and swap `validate_response(MessageResponse, 201)` for `ActivateResponse` on `activate` only (the DELETE keeps `MessageResponse`); map `ActivationWarning` -> DTO. Run `make openapi-hubapi` and commit the regenerated `openapi/v1.yaml` (spectral must report 0 errors).

- [ ] **Step 4: Run to verify it passes**

Run: `cd hub_api && python3 -m pytest tests/test_bundle_activation_blueprint.py --no-cov -q` -> Expected: all PASS; `make openapi-hubapi` -> exit 0.

- [ ] **Step 5: Commit**

```bash
git add hub_api/blueprints/v1/bundle_activation.py hub_api/tests/test_bundle_activation_blueprint.py openapi/v1.yaml
git commit -m "feat(hub-api): return command conflict warnings from community activation"
```

### Task 7: Backfill already-active bundles + seeder wiring

**Files:**
- Create: `hub_api/services/app_command_backfill.py` (separate module: it needs `bundle_approval_service._reparse_trusted`, which already imports `app_command_service`; keeping it out avoids a circular import)
- Modify: `hub_api/cli/seed_core_bundles.py` (`_run` :1353: call the backfill after the seeding loop and before the reconcile-uninstall sweep; add its failures to `failures`; log denominators)
- Test: `hub_api/tests/test_app_command_backfill.py`; append one case to `hub_api/tests/test_seed_core_bundles_failure_paths.py`

**Interfaces:**
- Consumes: `sync_commands` (Task 4), `_reparse_trusted` (`bundle_approval_service.py:117`), `app_active_versions` + `app_versions` + `app_version_uploads.manifest_json`.
- Produces:
  - `@dataclass(slots=True, frozen=True) class BackfillResult: examined: int; synced: int; failed: tuple[str, ...]`
  - `async def backfill_active_commands(install_dal: AsyncDB, *, now: datetime | None = None) -> BackfillResult` : for every `app_active_versions` row (any tenant/community incl. sentinel 0), load the manifest of its active version, `sync_commands` in its own `engine.begin()`; one app's failure is recorded in `failed` (`"<app_id>@<community_id>: <ExcType>"`, logged WARNING with traceback) and the sweep continues.

- [ ] **Step 1: Write the failing tests**

```python
async def test_backfill_registers_commands_for_already_active_apps():  # rows inserted directly (activation without sync) -> backfill -> app_commands rows; result.examined == synced == 2
async def test_backfill_is_idempotent_and_never_reorders_owners():      # run twice, later `now` -> registered_at unchanged, command_owner unchanged   [Review Focus 4]
async def test_backfill_orders_by_activated_at_on_first_run():          # A activated_at earlier than B, both !ping -> A's registered_at < B's (initial owner = earliest activation, not backfill order)
async def test_backfill_records_failure_for_corrupt_manifest_and_continues():  # manifest_json missing "schema_version" -> failed == ("<app>@<cid>: KeyError",), others still synced
async def test_backfill_with_no_active_rows_reports_zero():             # BackfillResult(0, 0, ())
# test_seed_core_bundles_failure_paths.py
async def test_run_counts_backfill_failures_and_exits_nonzero(monkeypatch):  # patch app_command_backfill.backfill_active_commands -> BackfillResult(3,2,("x@1: KeyError",)); seeder._run(...) == 1 and logs examined/synced counts
```
(For `test_backfill_orders_by_activated_at_on_first_run` the backfill passes each row's `activated_at` as that row's `now` so a freshly-created registry reproduces the real activation order.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd hub_api && python3 -m pytest tests/test_app_command_backfill.py --no-cov -v`
Expected: FAIL (`ModuleNotFoundError: services.app_command_backfill`).

- [ ] **Step 3: Implement** `backfill_active_commands` (per-row `now = row.activated_at`; existing rows keep their `registered_at` via `sync_commands`' preserve rule) and the `_run` hook (INFO `core-bundle-seeder: command registry backfill` with `examined/synced/failed` counts).

- [ ] **Step 4: Run to verify it passes**

Run: `cd hub_api && python3 -m pytest tests/test_app_command_backfill.py tests/test_seed_core_bundles.py tests/test_seed_core_bundles_failure_paths.py --no-cov -q` -> Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hub_api/services/app_command_backfill.py hub_api/cli/seed_core_bundles.py hub_api/tests/test_app_command_backfill.py hub_api/tests/test_seed_core_bundles_failure_paths.py
git commit -m "feat(hub-api): backfill app_commands for already-active bundles in the seeder"
```

### Task 8: Real-Postgres end-to-end, docs, full gates

**Files:**
- Create: `hub_api/tests/test_app_commands_pg_integration.py` (fixtures `pg_db`/`pg_dal` imported from `test_seed_core_bundles_pg_integration.py`)
- Modify: `docs/reference/command-bundles.md` (new "Command registry" section)
- No product code.

**Interfaces:**
- Consumes: everything above, against a container migrated to `head` (so 0062's real constraints, FKs and grants apply).

- [ ] **Step 1: Write the real-path tests**

```python
async def test_seed_then_activate_registers_inferred_rows_with_fk_enforced(pg_dal):
    # real app_catalog rows; activate_for_community_with_warnings -> app_commands rows exist; inserting a row for an unknown app_id raises IntegrityError
async def test_two_bundles_same_verb_first_registered_wins_on_real_pg(pg_dal):
    # warnings name first app; command_owner == first; deactivate first + reactivate -> owner flips to second (re-queue)
async def test_tenant_wide_and_community_rows_coexist(pg_dal):
    # activate_tenant_wide (scope 0) + activate_for_community (same tenant) different apps same verb -> conflict warning crosses scope
async def test_concurrent_activations_never_produce_two_owners(pg_dal):
    # asyncio.gather(activate A, activate B) same verb -> both succeed; command_owner deterministic and unique; (a lost warning is allowed, two owners is not)   [Review Focus 5]
async def test_backfill_on_real_pg_is_idempotent(pg_dal):
    # activation rows with no app_commands -> backfill twice -> same rows, unchanged registered_at
```

- [ ] **Step 2: Run to verify behaviour on a real database**

Run: `cd hub_api && python3 -m pytest tests/test_app_commands_pg_integration.py --no-cov -v`
Expected: 5 passed, **0 skipped** (a skip means docker is absent: treat as FAIL for this gate and rerun on a docker host; do not mark the task done on a skip).

- [ ] **Step 3: Write the docs section** in `docs/reference/command-bundles.md`: the `commands:` field shape, inference rules (prefix -> standard row, bare -> `*`), ownership = first-registered, the activation warning contract, and that Phase 1 only writes the registry (no routing change).

- [ ] **Step 4: Run the full gates and report denominators**

```bash
cd hub_api && python3 -m pytest -q            # coverage gate (.coveragerc fail_under 90, lines+branches); report passed/skipped counts
ruff check hub_api alembic && ruff format --check hub_api alembic
mypy --strict hub_api/services/app_command_service.py hub_api/services/app_command_backfill.py hub_api/services/bundle_manifest_v2.py hub_api/blueprints/v1/bundle_activation.py
bandit -r hub_api/services hub_api/blueprints hub_api/cli -ll
bash scripts/ci/check-alembic-single-head.sh . && bash scripts/ci/check-no-stubs.sh
python3 -m pytest alembic/tests/test_0062_app_commands_registry.py --no-cov -q
```
Expected: pytest exit 0 with coverage >= 90%; every other command exit 0. Record each command's examined-count (tests passed, files checked).

- [ ] **Step 5: Commit**

```bash
git add hub_api/tests/test_app_commands_pg_integration.py docs/reference/command-bundles.md
git commit -m "test(hub-api): real-Postgres e2e for the command registry + docs"
```

## Self-Review (completed by plan author)

- **Spec coverage:** s1 table/columns -> T1; manifest `commands:` -> T2; write on activation/approval + seeding -> T5, T7 (seeder routes through `activate_*`, plus backfill for no-op re-runs); auto-infer (prefix -> standard, bare -> `*`, explicit overrides/augments) -> T3; activation conflict warning (first-registered owner named, wildcard never warns) -> T4/T5/T6; ownership/re-queue -> T4/T5/T8; s9 no-command warning -> T4. Phase 2-5 items intentionally absent.
- **Step scan:** every code step carries signature + file + pinned values; no TBDs; test steps list names with the asserted values.
- **Type consistency:** `CommandType`/`CommandDeclaration` (T2) -> `DesiredCommand` (T3) -> `sync_commands`/`ActivationWarning` (T4) -> `ActivationResult` (T5) -> `ActivationWarningDTO` (T6) -> `BackfillResult` (T7); `community_id None` is stored `0` everywhere; `command_types` bitmask values 1/2 match the Task 1 CHECK.
- **Review Focus:** all five map to named tests (T3-T5, T7, T8).
- **Proportion:** plan is mostly signatures, test names and assertions; no function bodies.
