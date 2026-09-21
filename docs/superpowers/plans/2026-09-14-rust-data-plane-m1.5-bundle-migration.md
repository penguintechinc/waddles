# Milestone M1.5 — Bundle DAL Migration + `consumes` v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate every existing Python App Bundle's database access from `flask_core.database.AsyncDAL` (a pydal wrapper) to `penguin-dal`; migrate today's ingest-stage `consumes` tags into the new `bundle.yaml` v2 `consumes` contract; add the Python side of the shared golden-fixture contract tests — all verified by each service's own pytest suite, natively, before any WASM compilation work begins (D21a).

**Architecture:** `flask_core.bundle_runtime.get_bundle_dal()`/`set_bundle_dal()` keep their exact names and calling convention but are retyped from `flask_core.database.AsyncDAL` to `penguin_dal.AsyncDB` — bundle call SITES change (the query APIs differ), the get/set/bind mechanism does not. `svc_process`/`svc_action`'s own startup construct `penguin_dal.AsyncDB` and call `await dal.reflect()` once, which discovers the **entire real Postgres schema** via SQLAlchemy `MetaData.reflect()` — this makes every bundle's hand-rolled `_ensure_*_tables()` pydal-stub-definition helper (and `services/reference_tables.bind_minimal_reference_tables`, `services/activity_feed.init_live_activity_events_table`) dead code, deleted as each bundle touching them is migrated. Two new small helpers in `flask_core.bundle_runtime` (`raw_sql_rows`/`raw_sql_write`) give bundles a `penguin-dal`-native escape hatch (`AsyncDB.engine` + `sqlalchemy.text()`, wrapped back into `penguin_dal.Row`/`Rows`) for the joins/`GROUP BY`/`ON CONFLICT`/`RANDOM()` queries penguin-dal's single-table `Query` builder cannot express — every bundle keeps its exact SQL logic, only the placeholder syntax (`$1` → `:name`) and the calling convention change. `consumes` v2 is added as a new, idempotent Alembic migration (`app_catalog.stages.process.consumes`), since no `bundle.yaml` file exists anywhere in this repo yet (that file lands in M2/M6's directory move) — `app_catalog.stages` is today's only bundle registration surface, exactly as the milestone table permits ("`app_catalog.stages` migration rows / `bundle.yaml` where one exists").

**Tech Stack:** Python 3.13, Quart, `penguin-dal` 0.4.0 (SQLAlchemy-async wrapper), `asyncpg` (async Postgres driver), `aiosqlite` (test-only, in-memory SQLite harness), Alembic, pytest + pytest-asyncio + pytest-cov, `uv` for dependency compilation.

**Spec:** `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` (branch `docs/rust-data-plane-spec`) — §2 Decisions D7/D21/D21a/D21b/D23/D24, §6.1 Data contracts (`PlatformEvent.source`), §6.4 `bundle.yaml` v2 (`consumes` contract, V27–V31), §14.1 Shared golden fixtures, §16 Milestones M1.5.

## Global Constraints

- Python 3.13.x minimum (`general.md` Language Selection) — every new/edited file targets `py313`.
- `penguin-dal==0.4.0` exact pin in every touched `requirements.in`; recompiled `requirements.txt` via `uv pip compile requirements.in --generate-hashes -o requirements.txt`, run inside each service's own containerized toolchain (never host `pip`) — `critical-rules.md` Dependency Pinning.
- Coverage ≥ 90% (lines/branches/functions/statements) on every service touched — `critical-rules.md` Coverage. Existing bundle test coverage must not regress.
- No `|| true` on any lint/test/gate command; every gate reports the count of items it examined, never a bare "no findings" — `critical-rules.md` Verification Integrity. The bundle-DAL-import gate script asserts `files_scanned > 0`.
- Secrets/tokens: none introduced by this milestone; `DATABASE_URL`/`DB_PASS` stay env-sourced exactly as today — `critical-rules.md` Token & Secret Hygiene.
- Naming: the product is **Waddles**; never write "restream". `waddlebot` survives only in the specific legacy identifiers D22 lists (the Postgres `DB_NAME` default `waddlebot`, the Helm chart directory `k8s/helm/waddlebot`, the unused `waddlebot:stream:*`/`waddlebot:dlq:*` key prefixes, Python package paths) — none of those are touched by this plan.
- Every class/function gets a 2–3 line doc comment (`general.md` Code Documentation); no ASCII-art section dividers.
- D7: bundle **logic** and **entrypoint signatures** are otherwise byte-for-byte unchanged — only DB-access lines move. No behavior change beyond the DB call shape and (Task 20 only) the new declarative `consumes` metadata, which changes no runtime code path yet (M4/M5 wire the Rust fan-out against it later).
- D21a: migration happens **natively in Python**, no WASM/`componentize-py` involved anywhere in this plan.
- Every bundle-DAL-migration task removes any `asyncio.to_thread(...)`/`loop.run_in_executor(...)` wrapping a DB call and replaces it with a plain `await` — the WASM runtime has no thread pool (spec Assumption A20).
- Branching: this plan executes on a `feature/*` branch cut from `release/v3.0.X`, inside a worktree (`devops.md`); every merge is a PR into the release branch.

---

## File Structure

```
libs/flask_core/flask_core/
  bundle_runtime.py            MODIFY — retype get_bundle_dal/set_bundle_dal to penguin_dal.AsyncDB;
                                add raw_sql_rows()/raw_sql_write() helpers
  app_manifest.py              MODIFY — v2 consumes schema: ConsumeRule dataclass, parse/validate (V27-V31)
  stream_pipeline.py           MODIFY — PlatformEvent gains `source: SourceRef | None`
tests/ (libs/flask_core/tests/)
  test_bundle_runtime.py       MODIFY — retyped fixture, raw_sql_rows/raw_sql_write tests
  test_app_manifest.py         MODIFY — v2 consumes validation tests
  test_stream_pipeline.py      MODIFY — PlatformEvent.source round-trip tests
  fixtures/spine/               CREATE — golden fixture JSON (Task 23)
  test_golden_fixtures.py       CREATE — generator + round-trip assertions (Task 23)

core/svc_process/
  requirements.in               MODIFY — +penguin-dal, +asyncpg, +aiosqlite (test)
  requirements.txt              REGENERATE — uv pip compile --generate-hashes
  app.py                        MODIFY — construct penguin_dal.AsyncDB, await dal.reflect(), drop
                                 init_live_activity_events_table call
  services/activity_feed.py     MODIFY — init_live_activity_events_table() deleted (dead: reflect() covers it)
  bundles/
    community_announcements_process.py   MODIFY (Task 15)
    community_chat_process.py            MODIFY (Task 10)
    community_loyalty_process.py         MODIFY (Task 11)
    community_polls_process.py           MODIFY (Task 9)
    community_reputation_process.py      MODIFY (Task 7)
    inventory_process.py                 MODIFY (Task 8)
    social_alias_process.py              MODIFY (Task 12)
    social_music_process.py              MODIFY (Task 13)
    social_quote_process.py              MODIFY (Task 6)
    social_shoutout_process.py           MODIFY (Task 14)
    social_welcome_process.py            MODIFY (Task 5)
  tests/  (one test_bundles_*.py per bundle above)   MODIFY

core/svc_action/
  requirements.in                MODIFY — +penguin-dal, +asyncpg, +aiosqlite (test)
  requirements.txt               REGENERATE
  app.py                         MODIFY — construct penguin_dal.AsyncDB, await dal.reflect(), drop
                                  bind_minimal_reference_tables call
  services/reference_tables.py   MODIFY — bind_minimal_reference_tables() deleted (dead: reflect() covers it)
  bundles/
    community_announcements_action.py    MODIFY (Task 16)
    community_forums_action.py           MODIFY (Task 17)
    social_quote_action.py               MODIFY (Task 6)
    streaming_stream_action.py           MODIFY (Task 18)
    twitch_shoutout_action.py            MODIFY (Task 19)
  tests/  (one test_bundles_*.py per bundle above)   MODIFY

core/svc_ingest/bundles/
  twitch_ingest.py, twitch_eventsub_ingest.py, discord_ingest.py,
  slack_ingest.py, youtube_live_ingest.py, kick_ingest.py, echo_ingest.py    MODIFY (Task 22)

scripts/ci/
  check_bundle_dal_imports.sh   CREATE (Task 1) — fails on flask_core.database/pydal under bundle dirs
Makefile                        MODIFY (Task 25) — `make check-bundle-dal-imports` target
tests/k8s/alpha/05-unit-tests.sh MODIFY (Task 25) — wire the gate into the unit-test step

alembic/versions/
  0020_bundle_consumes_v2.py    CREATE (Task 20)
alembic/tests/
  test_0020_bundle_consumes_v2.py  CREATE (Task 20)

docs/APP_BUNDLE_AUTHORING.md    MODIFY (Task 24) — DAL section, consumes v2, ingest-not-pluggable note
```

---

## Shared Reference: penguin-dal Translation Idiom (read before Tasks 5–19)

Every bundle-migration task below applies one of two mechanical patterns. Both are established once in Task 3 and only *used*, never re-derived, in Tasks 5–19.

**Pattern A — single-table filter/insert/update/delete/count** (penguin-dal's native `Query`/`TableProxy` builder):

| Old (`flask_core.database.AsyncDAL`) | New (`penguin_dal.AsyncDB`) |
|---|---|
| `await dal.select_async(dal.dal(query))` or `dal.select(query)` | `await dal(query).select()` |
| `await dal.insert_async(dal.table, **fields)` | `await dal.table.async_insert(**fields)` |
| `await dal.update_async(query, **fields)` | `await dal(query).update(**fields)` |
| `await dal.delete_async(query)` | `await dal(query).delete()` |
| `await dal.count_async(query)` | `await dal(query).count()` |
| `dal.table.field == value` (comparison) | unchanged — `FieldProxy.__eq__` exists identically in both |
| `dal.table.field.belongs([...])` | unchanged — `FieldProxy.belongs()` exists identically in both |
| `rows.first()` | unchanged — `Rows.first()` exists identically in both |
| `dal.define_table("name", dal.Field(...), migrate=False)` (pydal stub) | **deleted** — `await dal.reflect()` at service startup (Task 4) already discovers the real table from the live schema; reference the real columns directly as `dal.<table>.<column>` |

**Pattern B — raw/complex SQL** (joins, `GROUP BY`, `ON CONFLICT`, `RANDOM()`, dynamic column names) that penguin-dal's single-table `Query` builder cannot express — uses the two helpers added to `flask_core.bundle_runtime` in Task 3:

```python
from flask_core.bundle_runtime import raw_sql_rows, raw_sql_write

# read (SELECT) — returns a penguin_dal.Rows, same .first()/iteration/len() as Pattern A
rows = await raw_sql_rows(dal, "SELECT id FROM t WHERE x = :x LIMIT 1", {"x": value})

# write (INSERT/UPDATE/DELETE, optional RETURNING) — commits, returns Rows (empty if no RETURNING)
rows = await raw_sql_write(dal, "INSERT INTO t (a) VALUES (:a) RETURNING id", {"a": value})
```

The only per-bundle work under Pattern B is: rewrite `$1, $2, $3` positional placeholders to named `:param` placeholders (matching the existing positional-list argument order), and change the params argument from a `list` to a `dict`. The SQL text itself — including every `JOIN`, `GROUP BY`, `ON CONFLICT`, and `ORDER BY RANDOM()` — is preserved verbatim; that SQL was already correct and is not being redesigned by this milestone.

---

### Task 1: Bundle DAL inventory + import gate script

**Files:**
- Create: `scripts/ci/check_bundle_dal_imports.sh`
- Test: `scripts/ci/tests/test_check_bundle_dal_imports.sh` (a small bats-free shell self-test)

**Interfaces:**
- Produces: `scripts/ci/check_bundle_dal_imports.sh` — exit 0 with `files_scanned=N legacy_hits=0` printed when clean; exit 1 and print every offending `path:line` when not. Scans exactly `core/svc_process/bundles`, `core/svc_action/bundles`, `core/svc_ingest/bundles` (today's real bundle locations — `bundles/python/` does not exist until M2/M6's directory move).

This task also fixes the milestone's own inventory number: a naive `grep -rl "get_bundle_dal"` over the bundle directories returns 17 files, but `core/svc_process/bundles/community_context_process.py` only *mentions* `get_bundle_dal()` inside a docstring comparing itself to a sibling bundle (line 12: "`community_reputation_process` calling `get_bundle_dal()` directly instead") — it never imports or calls it. The real, verified count is **16 bundles** (11 in `core/svc_process/bundles/`, 5 in `core/svc_action/bundles/`, 0 in `core/svc_ingest/bundles/`):

| # | Bundle | Service | Legacy call sites |
|---|---|---|---|
| 1 | `social_welcome_process.py` | svc_process | 2 (`dal.execute`) |
| 2 | `social_quote_process.py` | svc_process | 2 (`dal.execute`) |
| 3 | `community_reputation_process.py` | svc_process | 4 (`dal.execute`) |
| 4 | `inventory_process.py` | svc_process | 4 (`dal.execute`) |
| 5 | `community_polls_process.py` | svc_process | 11 (`dal.execute`) |
| 6 | `community_chat_process.py` | svc_process | 2 (`dal.execute`) |
| 7 | `community_loyalty_process.py` | svc_process | 2 (`dal.execute`, shared permission pattern) |
| 8 | `social_alias_process.py` | svc_process | 2 (`dal.execute`) + `dal.select`/`dal.update`/`dal.insert_async` wrapped in `asyncio.to_thread` |
| 9 | `social_music_process.py` | svc_process | 2 (`dal.execute`, shared permission pattern) |
| 10 | `social_shoutout_process.py` | svc_process | 3 (`dal.execute`) |
| 11 | `community_announcements_process.py` | svc_process | 1 (`select_async` + `_ensure_announcements_table`) |
| 12 | `community_announcements_action.py` | svc_action | `select_async`/`insert_async` + `_ensure_announcement_tables` |
| 13 | `community_forums_action.py` | svc_action | 3 `select_async` + 2 `insert_async` + 1 `update_async` + `_ensure_forum_tables` |
| 14 | `social_quote_action.py` | svc_action | 1 (`dal.execute`) |
| 15 | `streaming_stream_action.py` | svc_action | 3 `select_async` (two are JOINs) + `_ensure_streaming_tables` |
| 16 | `twitch_shoutout_action.py` | svc_action | 2 `select_async` + 1 `insert_async` + `_ensure_shoutout_tables` |

- [ ] **Step 1: Write the gate script**

```bash
cat > scripts/ci/check_bundle_dal_imports.sh <<'SCRIPT_EOF'
#!/usr/bin/env bash
# Fails if any App Bundle file imports the legacy flask_core.database/pydal
# surface instead of penguin-dal (D21b). Scans the three real bundle
# directories in today's repo layout -- `bundles/python/` does not exist
# until the M2/M6 directory move.
set -euo pipefail

BUNDLE_DIRS=(
    "core/svc_process/bundles"
    "core/svc_action/bundles"
    "core/svc_ingest/bundles"
)

files_scanned=0
legacy_hits=0
findings=()

for dir in "${BUNDLE_DIRS[@]}"; do
    if [ ! -d "$dir" ]; then
        echo "check_bundle_dal_imports: expected directory missing: $dir" >&2
        exit 1
    fi
    while IFS= read -r -d '' file; do
        files_scanned=$((files_scanned + 1))
        while IFS= read -r hit; do
            [ -z "$hit" ] && continue
            legacy_hits=$((legacy_hits + 1))
            findings+=("$hit")
        done < <(grep -nE '(^|[^.[:alnum:]_])(from flask_core\.database import|import flask_core\.database|from pydal import|^import pydal([^.[:alnum:]_]|$))' "$file" || true)
    done < <(find "$dir" -maxdepth 1 -name '*.py' -print0)
done

if [ "$files_scanned" -eq 0 ]; then
    echo "check_bundle_dal_imports: FAIL -- zero files scanned (path moved?)" >&2
    exit 1
fi

echo "check_bundle_dal_imports: files_scanned=$files_scanned legacy_hits=$legacy_hits"

if [ "$legacy_hits" -gt 0 ]; then
    echo "check_bundle_dal_imports: FAIL -- legacy flask_core.database/pydal import(s) found in bundle code:" >&2
    printf '  %s\n' "${findings[@]}" >&2
    echo "  -> use penguin_dal (see docs/APP_BUNDLE_AUTHORING.md 'Accessing the database')." >&2
    exit 1
fi

exit 0
SCRIPT_EOF
chmod +x scripts/ci/check_bundle_dal_imports.sh
```

- [ ] **Step 2: Run it now to prove it fails loud on a moved path (verification-integrity self-check)**

Run: `mv core/svc_process/bundles /tmp/bd-moved-check && bash scripts/ci/check_bundle_dal_imports.sh; echo "exit=$?"; mv /tmp/bd-moved-check core/svc_process/bundles`
Expected: `check_bundle_dal_imports: expected directory missing: core/svc_process/bundles` then `exit=1`.

- [ ] **Step 3: Run it against the current (unmigrated) tree — expect exactly 16 legacy files**

Run: `bash scripts/ci/check_bundle_dal_imports.sh; echo "exit=$?"`
Expected: `check_bundle_dal_imports: files_scanned=<N> legacy_hits=0` — **note**: the grep pattern above matches `from flask_core.database import`/`import flask_core.database`/`from pydal import`/bare `import pydal`, none of which any of the 16 bundles use today (they call `get_bundle_dal()`/`select_async`/etc., which are `flask_core` top-level re-exports, not `flask_core.database` imports) — so this gate starts **green** even before migration; it exists to catch a *regression* (a bundle importing the legacy module directly), not to detect today's `get_bundle_dal()`-based usage (Task 1's own file-by-file inventory table above is the record of that). Confirm `exit=0`.

- [ ] **Step 4: Commit**

```bash
git add scripts/ci/check_bundle_dal_imports.sh
git commit -m "$(cat <<'EOF'
test(bundles): add flask_core.database/pydal import gate for App Bundles

Scans core/{svc_process,svc_action,svc_ingest}/bundles for a direct
flask_core.database or pydal import (D21b) -- the real bundle locations in
today's repo layout, before the M2/M6 bundles/python/ directory move.
Inventory: 16 bundles reach the legacy AsyncDAL via get_bundle_dal(); this
gate specifically guards against a NEW direct flask_core.database/pydal
import creeping back in.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 2: Pin `penguin-dal` + async driver in both services

**Files:**
- Modify: `core/svc_process/requirements.in`
- Modify: `core/svc_action/requirements.in`
- Regenerate: `core/svc_process/requirements.txt`, `core/svc_action/requirements.txt`

**Interfaces:**
- Produces: `penguin_dal` importable in both services' containerized toolchain, at exact version `0.4.0`. `asyncpg` (async Postgres driver `penguin_dal.AsyncDB` requires — `AsyncDB.__init__` calls `ensure_async_uri()`, which maps `postgresql://` → `postgresql+asyncpg://`). `aiosqlite` (test-only async SQLite driver the bundle test harness uses, per `penguin_dal`'s own `tests/test_async_db.py`).

- [ ] **Step 1: Edit `core/svc_process/requirements.in`**

Add these three lines directly under the existing `# Database` comment block, keeping `pydal`/`psycopg2-binary` in place (hub-api and svc-action's `flask_core.database` module still use pydal; only bundle code migrates in this milestone):

```
# Database (flask_core.AsyncDAL wraps pydal directly)
pydal>=20240906.1,<20250101
psycopg2-binary>=2.9.9

# penguin-dal (App Bundle DB access, D21a) -- async Postgres via asyncpg;
# aiosqlite is test-only (in-memory bundle test harness, penguin-dal's own
# tests/test_async_db.py fixture pattern).
penguin-dal==0.4.0
asyncpg==0.30.0
aiosqlite==0.20.0
```

- [ ] **Step 2: Edit `core/svc_action/requirements.in`** with the identical three-line block (same placement, under its own `# Database` comment).

- [ ] **Step 3: Recompile both lockfiles inside the containerized Python 3.13 toolchain**

Run: `docker run --rm -v "$(pwd)/core/svc_process:/work" -w /work python:3.13-slim bash -c "pip install -q uv && uv pip compile requirements.in --generate-hashes -o requirements.txt --python-platform x86_64-manylinux_2_28"`
Expected: exits 0, `requirements.txt` rewritten with `penguin-dal==0.4.0 \` and its hash lines, plus `asyncpg==0.30.0` and `aiosqlite==0.20.0` blocks (each with `--hash=sha256:...` lines, matching the file's existing autogenerated format — see the header comment `# This file was autogenerated by uv via the following command: uv pip compile requirements.in --generate-hashes -o requirements.txt`).

Repeat identically for `core/svc_action`:
Run: `docker run --rm -v "$(pwd)/core/svc_action:/work" -w /work python:3.13-slim bash -c "pip install -q uv && uv pip compile requirements.in --generate-hashes -o requirements.txt --python-platform x86_64-manylinux_2_28"`

- [ ] **Step 4: Verify the new packages actually install cleanly together (no dependency conflict) inside the same containerized toolchain**

Run: `docker run --rm -v "$(pwd)/core/svc_process:/work" -w /work python:3.13-slim bash -c "pip install -q -r requirements.txt && python3 -c 'import penguin_dal, asyncpg, aiosqlite; print(penguin_dal.__name__, asyncpg.__version__, aiosqlite.__version__)'"`
Expected: prints `penguin_dal <asyncpg-version> <aiosqlite-version>` with no `pip install` error. Repeat for `core/svc_action`.

- [ ] **Step 5: Commit**

```bash
git add core/svc_process/requirements.in core/svc_process/requirements.txt \
        core/svc_action/requirements.in core/svc_action/requirements.txt
git commit -m "$(cat <<'EOF'
chore(deps): pin penguin-dal 0.4.0 + asyncpg/aiosqlite for svc_process/svc_action

Adds the penguin-dal public API (D21) plus its async Postgres driver and a
test-only async SQLite driver to both bundle-hosting services, exact-pinned
with hashes via uv pip compile, ahead of the bundle DAL migration (D21a).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 3: Retype `get_bundle_dal()`/`set_bundle_dal()` to `penguin_dal.AsyncDB` + add raw-SQL helpers

**Files:**
- Modify: `libs/flask_core/flask_core/bundle_runtime.py`
- Modify: `libs/flask_core/tests/test_bundle_runtime.py`

**Interfaces:**
- Consumes: `penguin_dal.AsyncDB`, `penguin_dal.Row`, `penguin_dal.Rows` (Task 2's new dependency).
- Produces: `flask_core.bundle_runtime.get_bundle_dal() -> AsyncDB` (was `-> AsyncDAL`), `set_bundle_dal(dal: AsyncDB) -> None` (was `dal: AsyncDAL`) — **same names, same zero/one-arg calling convention**, this is the "call-compatible facade" decision (M1.5's DAL API decision: **call-compatible**, not a full bundle-call-site rewrite of the accessor itself — only the query methods *inside* each bundle change, per Pattern A/B above). Also produces two new functions: `raw_sql_rows(dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None) -> Rows` and `raw_sql_write(dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None) -> Rows`.

- [ ] **Step 1: Write the failing tests**

```python
# libs/flask_core/tests/test_bundle_runtime.py -- add these, keep existing tests unchanged
import pytest
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, text

from flask_core.bundle_runtime import (
    get_bundle_dal,
    raw_sql_rows,
    raw_sql_write,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from penguin_dal import AsyncDB


def _create_widgets_table(conn):
    metadata = MetaData()
    Table(
        "widgets",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("name", String(255), nullable=False),
        Column("active", Boolean, default=True),
    )
    metadata.create_all(conn)


@pytest.fixture
async def dal():
    """An in-memory penguin_dal.AsyncDB with one seeded `widgets` table."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.run_sync(_create_widgets_table)
        await conn.execute(
            text("INSERT INTO widgets (name, active) VALUES ('a', 1), ('b', 0)")
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()


class TestGetSetBundleDalRetyped:
    async def test_get_bundle_dal_returns_asyncdb(self, dal):
        assert get_bundle_dal() is dal
        assert isinstance(get_bundle_dal(), AsyncDB)

    async def test_bundle_can_query_via_penguin_dal_query_builder(self, dal):
        rows = await get_bundle_dal()(get_bundle_dal().widgets.active == True).select()  # noqa: E712
        assert len(rows) == 1
        assert rows.first().name == "a"


class TestRawSqlRows:
    async def test_returns_rows_object_with_first_and_dict_access(self, dal):
        rows = await raw_sql_rows(dal, "SELECT id, name FROM widgets WHERE name = :n", {"n": "a"})
        assert len(rows) == 1
        row = rows.first()
        assert row is not None
        assert row["name"] == "a"
        assert row.name == "a"

    async def test_empty_result_returns_empty_rows(self, dal):
        rows = await raw_sql_rows(dal, "SELECT id FROM widgets WHERE name = :n", {"n": "nope"})
        assert len(rows) == 0
        assert rows.first() is None

    async def test_no_params_defaults_to_empty_dict(self, dal):
        rows = await raw_sql_rows(dal, "SELECT id FROM widgets")
        assert len(rows) == 2


class TestRawSqlWrite:
    async def test_insert_commits_and_returns_empty_rows_without_returning(self, dal):
        rows = await raw_sql_write(
            dal, "INSERT INTO widgets (name, active) VALUES (:n, 1)", {"n": "c"}
        )
        assert len(rows) == 0
        check = await raw_sql_rows(dal, "SELECT COUNT(*) AS n FROM widgets")
        assert check.first()["n"] == 3

    async def test_write_persists_across_a_new_connection(self, dal):
        await raw_sql_write(dal, "UPDATE widgets SET active = 0 WHERE name = :n", {"n": "a"})
        rows = await raw_sql_rows(dal, "SELECT active FROM widgets WHERE name = :n", {"n": "a"})
        assert rows.first()["active"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd libs/flask_core && python3 -m pytest tests/test_bundle_runtime.py -v 2>&1 | tail -30`
Expected: `ImportError: cannot import name 'raw_sql_rows' from 'flask_core.bundle_runtime'` (or `ModuleNotFoundError: No module named 'penguin_dal'` if Task 2 hasn't installed it into this environment yet — install first: `pip install penguin-dal==0.4.0 asyncpg==0.30.0 aiosqlite==0.20.0`).

- [ ] **Step 3: Implement**

Replace the `TYPE_CHECKING` import block and add the two helpers. Edit `libs/flask_core/flask_core/bundle_runtime.py`:

```python
# Replace this block:
#     if TYPE_CHECKING:
#         from .database import AsyncDAL
# with:

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only -- penguin_dal is a real runtime import (Task 2 pin), but
    # kept TYPE_CHECKING-guarded here too, matching this module's existing
    # "stay a lightweight leaf module" rationale (module docstring).
    from penguin_dal import AsyncDB


# Replace every `AsyncDAL` type annotation in this file with `AsyncDB`:
#   _dal: AsyncDB | None = None
#   def set_bundle_dal(dal: AsyncDB) -> None: ...
#   def get_bundle_dal() -> AsyncDB: ...
# (docstrings' prose mentions of "AsyncDAL"/"flask_core.AsyncDAL" become
# "AsyncDB"/"penguin_dal.AsyncDB" -- e.g. get_bundle_dal()'s Returns line
# becomes "The `penguin_dal.AsyncDB` instance the current stage runner
# bound at startup.")
```

Then append these two new functions at the end of the file:

```python
async def raw_sql_rows(
    dal: "AsyncDB", sql: str, params: "Mapping[str, Any] | None" = None
) -> "Rows":
    """Run a read-only raw SQL query, for the joins/`GROUP BY`/`RANDOM()` cases
    `penguin_dal`'s single-table `Query` builder cannot express.

    `penguin_dal.AsyncDB.engine` is a public SQLAlchemy async engine; this
    wraps `sqlalchemy.text()` execution back into `penguin_dal.Row`/`Rows`
    so callers get the exact same `row["x"]`/`row.x`/`rows.first()`
    ergonomics as `dal(query).select()` (Pattern A), regardless of which
    path produced the rows -- see docs/APP_BUNDLE_AUTHORING.md's DAL
    section for when to reach for this over the query builder.

    Args:
        dal: The bound `penguin_dal.AsyncDB` (from `get_bundle_dal()`).
        sql: SQL text with named `:param` placeholders.
        params: Bind parameter values, or `None` for a parameterless query.

    Returns:
        A `penguin_dal.Rows` of the result set (empty if no rows matched).
    """
    async with dal.engine.connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return Rows([Row(dict(mapping)) for mapping in result.mappings().all()])


async def raw_sql_write(
    dal: "AsyncDB", sql: str, params: "Mapping[str, Any] | None" = None
) -> "Rows":
    """Run a raw SQL write (INSERT/UPDATE/DELETE, optionally `RETURNING`) in a
    committed transaction, for the `ON CONFLICT`/multi-row cases
    `penguin_dal`'s single-table `Query` builder cannot express.

    Commits on success (via `AsyncDB.engine.begin()`'s own transaction
    scope) and rolls back on any exception raised inside the block. A
    statement with no `RETURNING` clause returns an empty `Rows`, never
    raises for that reason alone.

    Args:
        dal: The bound `penguin_dal.AsyncDB` (from `get_bundle_dal()`).
        sql: SQL text with named `:param` placeholders.
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
```

Add the two new imports at the top of the file (alongside the existing `contextvars`/`dataclasses` imports):

```python
from collections.abc import Mapping
from typing import Any

from penguin_dal import Row, Rows
from sqlalchemy import text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd libs/flask_core && python3 -m pytest tests/test_bundle_runtime.py -v 2>&1 | tail -30`
Expected: `9 passed` (or however many the file now totals — every new test above plus the pre-existing `set_bundle_dal`/`get_bundle_dal`/`bundle_context` tests, all green).

- [ ] **Step 5: Run the full flask_core suite + coverage to confirm nothing else broke**

Run: `cd libs/flask_core && python3 -m pytest --cov=flask_core.bundle_runtime --cov-report=term-missing --cov-fail-under=90 tests/test_bundle_runtime.py`
Expected: `Required test coverage of 90% reached` and all tests pass.

- [ ] **Step 6: Commit**

```bash
git add libs/flask_core/flask_core/bundle_runtime.py libs/flask_core/tests/test_bundle_runtime.py
git commit -m "$(cat <<'EOF'
refactor(flask-core): retype get_bundle_dal()/set_bundle_dal() to penguin_dal.AsyncDB

Call-compatible facade decision (M1.5): get_bundle_dal()/set_bundle_dal()
keep their exact names and zero/one-arg calling convention, retyped from
flask_core.database.AsyncDAL to penguin_dal.AsyncDB -- only each bundle's
own query call sites change (Tasks 5-19), never the accessor. Adds
raw_sql_rows()/raw_sql_write(), the sanctioned escape hatch for the
joins/GROUP BY/ON CONFLICT/RANDOM() queries penguin-dal's single-table
Query builder can't express, over AsyncDB's own public .engine property.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 4: Wire `penguin_dal.AsyncDB` into `svc_process`/`svc_action` startup

**Files:**
- Modify: `core/svc_process/app.py`
- Modify: `core/svc_process/services/activity_feed.py`
- Modify: `core/svc_process/tests/test_app.py`
- Modify: `core/svc_action/app.py`
- Modify: `core/svc_action/services/reference_tables.py`
- Modify: `core/svc_action/tests/test_app.py`

**Interfaces:**
- Consumes: `penguin_dal.AsyncDB`, `flask_core.bundle_runtime.set_bundle_dal` (Task 3).
- Produces: both services' `startup()` hook binds a fully-reflected `AsyncDB` via `set_bundle_dal()` before the poller/runner starts — every bundle migrated in Tasks 5–19 depends on this being in place. **Depends on: Task 3.**

- [ ] **Step 1: `core/svc_process/app.py` — replace the DAL construction in `startup()`**

Old code (imports near the top, body inside `startup()`):

```python
from flask_core import (
    AsyncDAL,
    create_health_blueprint,
    install_security_headers,
    set_bundle_dal,
    setup_aaa_logging,
)
...
from services.activity_feed import init_live_activity_events_table
...
    async_dal = AsyncDAL(Config.DATABASE_URL, pool_size=Config.DB_POOL_SIZE, migrate=False)
    set_bundle_dal(async_dal)
    init_live_activity_events_table(async_dal.dal)
```

New code:

```python
from flask_core import (
    create_health_blueprint,
    install_security_headers,
    set_bundle_dal,
    setup_aaa_logging,
)
from penguin_dal import AsyncDB
...
    # penguin-dal (D21a): AsyncDB.reflect() discovers the entire live Postgres
    # schema via SQLAlchemy MetaData.reflect() -- this supersedes the old
    # per-table init_live_activity_events_table()/pydal-stub-definition
    # pattern outright; every real table (live_activity_events included) is
    # already present as dal.<table_name> after one reflect() call.
    async_dal = AsyncDB(Config.DATABASE_URL, pool_size=Config.DB_POOL_SIZE)
    await async_dal.reflect()
    set_bundle_dal(async_dal)
```

Remove the now-unused `from services.activity_feed import init_live_activity_events_table` line entirely.

- [ ] **Step 2: `core/svc_process/services/activity_feed.py` — delete `init_live_activity_events_table`**

Run: `grep -rn "init_live_activity_events_table" core/svc_process/`
Expected after Step 1: only the definition itself in `services/activity_feed.py` and its own test — no other caller. Delete the function definition and its dedicated test (`test_init_live_activity_events_table` or similar, if one exists in `core/svc_process/tests/test_activity_feed.py`); leave `record_activity()` and every other function in that module untouched.

- [ ] **Step 3: `core/svc_action/app.py` — identical replacement**

Old code:

```python
from flask_core import (
    AsyncDAL,
    ...
    set_bundle_dal,
    ...
)
...
from services.reference_tables import bind_minimal_reference_tables
...
    async_dal = AsyncDAL(_config.database_url, pool_size=_config.db_pool_size, migrate=False)
    bind_minimal_reference_tables(async_dal.dal)
```

New code:

```python
from flask_core import (
    ...
    set_bundle_dal,
    ...
)
from penguin_dal import AsyncDB
...
    # penguin-dal (D21a): reflect() discovers the whole live schema, so the
    # old bind_minimal_reference_tables() pydal-stub pattern (tenants/
    # communities/app_catalog/action_dispatch_log) is redundant -- delete it.
    async_dal = AsyncDB(_config.database_url, pool_size=_config.db_pool_size)
    await async_dal.reflect()
```

Remove `from services.reference_tables import bind_minimal_reference_tables`. `set_bundle_dal(async_dal)` (a few lines further down) stays unchanged.

- [ ] **Step 4: `core/svc_action/services/reference_tables.py` — delete `bind_minimal_reference_tables`**

Run: `grep -rn "bind_minimal_reference_tables" core/svc_action/`
Expected after Step 3: only the definition and its own dedicated test remain. Delete the function and that test; leave every other export of `reference_tables.py` (if any) untouched.

- [ ] **Step 5: Update each service's startup test to assert the new construction**

Add to `core/svc_process/tests/test_app.py` (adapt the mock-injection mechanics to whatever pattern this file already uses for `httpx.AsyncClient`/`redis.from_url` — it already mocks `startup()`'s other dependencies the same way; this test follows the identical shape for the DAL line):

```python
async def test_startup_binds_reflected_asyncdb(monkeypatch):
    """startup() constructs penguin_dal.AsyncDB, calls reflect(), and binds it via set_bundle_dal()."""
    from penguin_dal import AsyncDB

    bound: list[AsyncDB] = []
    reflected: list[bool] = []

    class _FakeAsyncDB(AsyncDB):
        def __init__(self, *a, **kw):
            super().__init__("sqlite://", pool_size=1)

        async def reflect(self):
            reflected.append(True)
            return await super().reflect()

    monkeypatch.setattr("app.AsyncDB", _FakeAsyncDB)
    monkeypatch.setattr("app.set_bundle_dal", lambda dal: bound.append(dal))
    # ... exercise app.startup() per this file's existing dependency-mocking
    # convention, then:
    assert reflected == [True]
    assert len(bound) == 1
```

Add the mirrored test to `core/svc_action/tests/test_app.py`.

- [ ] **Step 6: Run both services' full suites**

Run: `env -C core/svc_process python3 -m pytest -v 2>&1 | tail -40`
Expected: all tests pass, EXCEPT any bundle test still exercising the legacy `AsyncDAL` shape directly — that is expected and is exactly what Tasks 5–19 fix next. If a test unrelated to a to-be-migrated bundle fails, stop and investigate before proceeding.

Run: `env -C core/svc_action python3 -m pytest -v 2>&1 | tail -40`
Expected: same.

- [ ] **Step 7: Commit**

```bash
git add core/svc_process/app.py core/svc_process/services/activity_feed.py \
        core/svc_process/tests/test_app.py \
        core/svc_action/app.py core/svc_action/services/reference_tables.py \
        core/svc_action/tests/test_app.py
git commit -m "$(cat <<'EOF'
refactor(bundles): construct penguin_dal.AsyncDB at svc_process/svc_action startup

Both services now build a penguin_dal.AsyncDB and call await dal.reflect()
once before binding it via set_bundle_dal() -- reflect() discovers the
entire live Postgres schema via SQLAlchemy, making the old per-table pydal
stub helpers (init_live_activity_events_table, bind_minimal_reference_tables)
dead code, deleted here. Bundle-side query call sites migrate in
subsequent commits (D21a); this is the shared wiring they depend on.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

**Tasks 5–19 below are independent of each other and may run in parallel — each depends only on Task 4.**

### Task 5: Migrate `social_welcome_process.py` (establishes the Pattern B upsert idiom)

**Files:**
- Modify: `core/svc_process/bundles/social_welcome_process.py`
- Modify: `core/svc_process/tests/test_bundles_social_welcome_process.py`

**Interfaces:**
- Consumes: `flask_core.bundle_runtime.raw_sql_rows`/`raw_sql_write` (Task 3), `penguin_dal.AsyncDB` reflected at startup (Task 4).
- Depends on: Task 4.

- [ ] **Step 1: Edit `core/svc_process/bundles/social_welcome_process.py` imports**

Old: `from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal`
New:
```python
from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal
from flask_core.bundle_runtime import raw_sql_rows, raw_sql_write
```

- [ ] **Step 2: Replace `_is_first_time`'s body**

Old:
```python
    ctx = get_bundle_context()
    dal = get_bundle_dal()
    rows = await dal.execute(
        "SELECT id FROM activity_message_events "
        "WHERE community_id = $1 AND platform = $2 AND platform_user_id = $3 "
        "LIMIT 1",
        [int(ctx.community) if ctx.community else None, platform, platform_user_id],
    )
    return len(rows) == 0
```

New:
```python
    ctx = get_bundle_context()
    dal = get_bundle_dal()
    rows = await raw_sql_rows(
        dal,
        "SELECT id FROM activity_message_events "
        "WHERE community_id = :community_id AND platform = :platform "
        "AND platform_user_id = :platform_user_id LIMIT 1",
        {
            "community_id": int(ctx.community) if ctx.community else None,
            "platform": platform,
            "platform_user_id": platform_user_id,
        },
    )
    return len(rows) == 0
```

- [ ] **Step 3: Replace `_try_mark_welcomed`'s body**

Old:
```python
    ctx = get_bundle_context()
    dal = get_bundle_dal()
    rows = await dal.execute(
        "INSERT INTO community_welcomed_users "
        "(community_id, platform, platform_user_id) "
        "VALUES ($1, $2, $3) "
        "ON CONFLICT (community_id, platform, platform_user_id) DO NOTHING "
        "RETURNING id",
        [int(ctx.community) if ctx.community else None, platform, platform_user_id],
    )
    return len(rows) == 1
```

New:
```python
    ctx = get_bundle_context()
    dal = get_bundle_dal()
    rows = await raw_sql_write(
        dal,
        "INSERT INTO community_welcomed_users "
        "(community_id, platform, platform_user_id) "
        "VALUES (:community_id, :platform, :platform_user_id) "
        "ON CONFLICT (community_id, platform, platform_user_id) DO NOTHING "
        "RETURNING id",
        {
            "community_id": int(ctx.community) if ctx.community else None,
            "platform": platform,
            "platform_user_id": platform_user_id,
        },
    )
    return len(rows) == 1
```

- [ ] **Step 4: Rewrite the test file's DAL fixture from an `AsyncMock`-based fake executor to a real in-memory `penguin_dal.AsyncDB`**

Replace the `_mock_executor` helper and every `set_bundle_dal(_mock_executor(...))`/`set_bundle_dal(AsyncMock())` call in `core/svc_process/tests/test_bundles_social_welcome_process.py` with this fixture (delete `_mock_executor` and the `from unittest.mock import AsyncMock` import — no longer used):

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with the two real tables this bundle touches."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE activity_message_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "community_id INTEGER, platform TEXT, platform_user_id TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE community_welcomed_users ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "community_id INTEGER, platform TEXT, platform_user_id TEXT, "
                "UNIQUE(community_id, platform, platform_user_id))"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Replace `TestIsFirstTime` with:

```python
class TestIsFirstTime:
    """Tests for _is_first_time."""

    async def test_returns_true_if_no_prior_events(self, dal) -> None:
        """User with no prior activity_message_events is a first-timer."""
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _is_first_time("discord", "user123")
        assert result is True

    async def test_returns_false_if_prior_events_exist(self, dal) -> None:
        """User with prior events is not a first-timer."""
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO activity_message_events "
                    "(community_id, platform, platform_user_id) VALUES (42, 'discord', 'user123')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _is_first_time("discord", "user123")
        assert result is False

    async def test_scopes_by_community_platform_and_user(self, dal) -> None:
        """A row for a different community/platform/user must not count as a prior event."""
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO activity_message_events "
                    "(community_id, platform, platform_user_id) VALUES (99, 'twitch', 'someone_else')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _is_first_time("discord", "user123")
        assert result is True
```

Replace `TestTryMarkWelcomed` with:

```python
class TestTryMarkWelcomed:
    """Tests for _try_mark_welcomed."""

    async def test_returns_true_if_insert_succeeded(self, dal) -> None:
        """Returning a row means this call won the race."""
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _try_mark_welcomed("discord", "user123")
        assert result is True

    async def test_returns_false_if_conflict_prevented_insert(self, dal) -> None:
        """No returned row means a concurrent insert already claimed the welcome."""
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_welcomed_users "
                    "(community_id, platform, platform_user_id) VALUES (42, 'discord', 'user123')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _try_mark_welcomed("discord", "user123")
        assert result is False

    async def test_scopes_conflict_by_community_platform_and_user(self, dal) -> None:
        """A conflicting row for a DIFFERENT community/platform/user must not block this insert."""
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_welcomed_users "
                    "(community_id, platform, platform_user_id) VALUES (99, 'twitch', 'someone_else')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _try_mark_welcomed("discord", "user123")
        assert result is True
```

In `TestTransform`, replace every `set_bundle_dal(_mock_executor())` with the `dal` fixture as a test parameter (e.g. `async def test_missing_text_raises_valueerror(self, dal) -> None:`, dropping the `set_bundle_dal(...)` line — the fixture already binds it). Replace the two `side_effect`-sequenced tests:

```python
    async def test_repeat_visitor_returns_none(self, dal) -> None:
        """Event from a repeat visitor returns None (no welcome)."""
        event = _event()
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO activity_message_events "
                    "(community_id, platform, platform_user_id) VALUES (42, 'discord', 'user123')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await transform(event)
        assert result is None

    async def test_race_condition_returns_none(self, dal) -> None:
        """If another process already marked user as welcomed, return None."""
        event = _event()
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_welcomed_users "
                    "(community_id, platform, platform_user_id) VALUES (42, 'discord', 'user123')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await transform(event)
        assert result is None
```

Every other `TestTransform` test (`test_welcome_claims_and_modifies_event`, `test_non_string_text_raises_valueerror`, `test_non_string_author_id_raises_valueerror`, `test_empty_author_id_raises_valueerror`, `test_welcome_preserves_payload_fields`) keeps its exact body, only swapping `set_bundle_dal(_mock_executor())`/`set_bundle_dal(AsyncMock()); executor.execute.side_effect = [[], [{"id": 999}]]` for the `dal` fixture parameter (no pre-seeded rows needed — a fresh in-memory DB is already a first-timer with no conflict).

- [ ] **Step 5: Run the test file**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_social_welcome_process.py -v 2>&1 | tail -40`
Expected: every test passes (same test count as before the rewrite — no test was deleted, two were added: `test_scopes_by_community_platform_and_user`, `test_scopes_conflict_by_community_platform_and_user`).

- [ ] **Step 6: Run the import gate + full service suite + coverage**

Run: `bash scripts/ci/check_bundle_dal_imports.sh`
Expected: unchanged (`legacy_hits=0` — this bundle never imported `flask_core.database`/`pydal` directly; it used the `get_bundle_dal()` facade throughout, gated by the inventory table, not this script).

Run: `env -C core/svc_process python3 -m pytest --cov=bundles.social_welcome_process --cov-fail-under=90 tests/test_bundles_social_welcome_process.py`
Expected: `Required test coverage of 90% reached`.

- [ ] **Step 7: Commit**

```bash
git add core/svc_process/bundles/social_welcome_process.py \
        core/svc_process/tests/test_bundles_social_welcome_process.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate social_welcome_process to penguin-dal

Raw $N-placeholder AsyncDAL.execute() calls (including the ON CONFLICT ...
DO NOTHING RETURNING id upsert) become raw_sql_rows()/raw_sql_write() with
named :param placeholders (D21a). Test fixture moves from an AsyncMock
fake executor to a real in-memory penguin_dal.AsyncDB with the two tables
this bundle touches, seeded per test instead of stubbed via side_effect.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

**Note on Tasks 6–19 test rewrites:** every task below follows the exact same test-fixture methodology Task 5 established: delete the `AsyncMock`-based fake executor / `_mock_executor` helper, add an in-memory `penguin_dal.AsyncDB("sqlite://")` fixture (`db.engine.begin()` to `CREATE TABLE` only the columns the bundle touches, then `await db.reflect()`, then `set_bundle_dal(db)`), and replace canned `side_effect`/return-value stubs with real pre-seeded rows (INSERT via `sa_text` inside `async with dal.engine.begin() as conn:`) or an empty table for the "no matching row" case. Only the table DDL and the specific rows each test seeds differ per bundle — shown in full below for each.

### Task 6: Migrate `social_quote_process.py` + `social_quote_action.py`

**Files:**
- Modify: `core/svc_process/bundles/social_quote_process.py`
- Modify: `core/svc_process/tests/test_bundles_social_quote_process.py`
- Modify: `core/svc_action/bundles/social_quote_action.py`
- Modify: `core/svc_action/tests/test_bundles_social_quote_action.py`

**Interfaces:** Consumes `raw_sql_rows`/`raw_sql_write` (Task 3). Depends on: Task 4.

- [ ] **Step 1: `social_quote_process.py` imports** — add `from flask_core.bundle_runtime import raw_sql_rows` alongside the existing `from flask_core import PlatformEvent, get_bundle_dal`.

- [ ] **Step 2: Replace the quote-by-id lookup**

Old:
```python
        dal = get_bundle_dal()
        sql = """
            SELECT id, quote_text, quoted_username, created_at
            FROM quotes
            WHERE id = %s AND deleted_at IS NULL
        """
        result = await dal.execute(sql, [quote_id])
```

New:
```python
        dal = get_bundle_dal()
        result = await raw_sql_rows(
            dal,
            "SELECT id, quote_text, quoted_username, created_at FROM quotes "
            "WHERE id = :quote_id AND deleted_at IS NULL",
            {"quote_id": quote_id},
        )
```

Everything after (`if not result: return None`, `row = result[0]`, `row.get("quoted_username")`) is unchanged — `penguin_dal.Rows`/`Row` support the same `bool()`/indexing/`.get()` shape as the old `list[dict]`.

- [ ] **Step 3: Replace the random-quote lookup**

Old:
```python
        dal = get_bundle_dal()
        sql = """
            SELECT id, quote_text, quoted_username
            FROM quotes
            WHERE deleted_at IS NULL AND is_approved = TRUE
            ORDER BY RANDOM()
            LIMIT 1
        """
        result = await dal.execute(sql, [])
```

New:
```python
        dal = get_bundle_dal()
        result = await raw_sql_rows(
            dal,
            "SELECT id, quote_text, quoted_username FROM quotes "
            "WHERE deleted_at IS NULL AND is_approved = TRUE ORDER BY RANDOM() LIMIT 1",
        )
```

- [ ] **Step 4: `social_quote_action.py` imports** — add `from flask_core.bundle_runtime import raw_sql_write` alongside `from flask_core import StageEnvelope, get_bundle_dal`.

- [ ] **Step 5: Replace the quote-insert call**

Old:
```python
        dal = get_bundle_dal()
        sql = """
            INSERT INTO quotes
            (quote_text, quoted_username, is_approved, created_at, updated_at)
            VALUES (%s, %s, TRUE, %s, %s)
            RETURNING id
        """
        now = datetime.now(UTC).isoformat()
        result = await dal.execute(sql, [quote_text, actor or "unknown", now, now])
        if not result:
            return None
        row = result[0]
        raw_id = row.get("id") if isinstance(row, dict) else None
        return raw_id if isinstance(raw_id, int) else None
```

New:
```python
        dal = get_bundle_dal()
        now = datetime.now(UTC).isoformat()
        result = await raw_sql_write(
            dal,
            "INSERT INTO quotes (quote_text, quoted_username, is_approved, created_at, updated_at) "
            "VALUES (:quote_text, :quoted_username, TRUE, :created_at, :updated_at) RETURNING id",
            {
                "quote_text": quote_text,
                "quoted_username": actor or "unknown",
                "created_at": now,
                "updated_at": now,
            },
        )
        if not result:
            return None
        raw_id = result[0].get("id")
        return raw_id if isinstance(raw_id, int) else None
```

- [ ] **Step 6: Rewrite `test_bundles_social_quote_process.py`'s DAL fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with a minimal `quotes` table."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE quotes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, quote_text TEXT, "
                "quoted_username TEXT, is_approved BOOLEAN, deleted_at TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

For a "quote found" test, seed one row before calling the function under test:
```python
    async with dal.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "INSERT INTO quotes (id, quote_text, quoted_username, is_approved, deleted_at) "
                "VALUES (1, 'hello world', 'alice', 1, NULL)"
            )
        )
```
For a "not found"/"no quotes" test, run against the fixture's empty table with no seed. Apply the same `dal`-fixture-parameter swap (drop `set_bundle_dal(_mock_executor(...))`/`AsyncMock` usage) to every other test in the file, matching Task 5 Step 4's pattern.

- [ ] **Step 7: Rewrite `test_bundles_social_quote_action.py`'s DAL fixture** identically (same `quotes` table DDL as Step 6), and for the "insert succeeds" test assert `await send_quote(...)` (or whatever the entrypoint is named) returns an `int`, then verify via a follow-up `raw_sql_rows(dal, "SELECT * FROM quotes")`-style read (or a direct `conn.execute(sa_text("SELECT COUNT(*) FROM quotes"))`) that exactly one row now exists.

- [ ] **Step 8: Run both test files, the import gate, and coverage**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_social_quote_process.py -v 2>&1 | tail -30`
Run: `env -C core/svc_action python3 -m pytest tests/test_bundles_social_quote_action.py -v 2>&1 | tail -30`
Expected: all green in both.

Run: `env -C core/svc_process python3 -m pytest --cov=bundles.social_quote_process --cov-fail-under=90 tests/test_bundles_social_quote_process.py`
Run: `env -C core/svc_action python3 -m pytest --cov=bundles.social_quote_action --cov-fail-under=90 tests/test_bundles_social_quote_action.py`
Expected: both report `Required test coverage of 90% reached`.

- [ ] **Step 9: Commit**

```bash
git add core/svc_process/bundles/social_quote_process.py \
        core/svc_process/tests/test_bundles_social_quote_process.py \
        core/svc_action/bundles/social_quote_action.py \
        core/svc_action/tests/test_bundles_social_quote_action.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate social_quote_process/social_quote_action to penguin-dal

Raw AsyncDAL.execute() calls (including RANDOM() ordering and an
INSERT ... RETURNING id) become raw_sql_rows()/raw_sql_write() with named
:param placeholders (D21a). Test fixtures move to a real in-memory
penguin_dal.AsyncDB seeded per test.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 7: Migrate `community_reputation_process.py`

**Files:**
- Modify: `core/svc_process/bundles/community_reputation_process.py`
- Modify: `core/svc_process/tests/test_bundles_community_reputation_process.py`

**Interfaces:** Consumes `raw_sql_rows` (Task 3). Depends on: Task 4.

- [ ] **Step 1: Imports** — add `from flask_core.bundle_runtime import raw_sql_rows` alongside `from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal`.

- [ ] **Step 2: Replace the three SQL constants** (the old `_MEMBER_SQL` used a `{clause}` `.format()` placeholder to switch between two WHERE clauses with positional `$2`/`$3` — named params make that indirection unnecessary; split it into two explicit constants)

Old:
```python
_MEMBER_SQL = (
    "SELECT cm.display_name, cm.reputation, cm.user_id AS hub_user_id "
    "FROM community_members cm "
    "WHERE cm.community_id = $1 AND {clause} "
    "LIMIT 1"
)

_COMMUNITY_LABEL_SQL = (
    "SELECT COALESCE(display_name, name) AS label FROM communities WHERE id = $1 LIMIT 1"
)

_GLOBAL_SCORE_SQL = "SELECT score FROM reputation_global WHERE hub_user_id = $1"
```

New:
```python
_MEMBER_BY_PLATFORM_SQL = (
    "SELECT cm.display_name, cm.reputation, cm.user_id AS hub_user_id "
    "FROM community_members cm "
    "WHERE cm.community_id = :community_id AND cm.platform = :platform "
    "AND cm.platform_user_id = :platform_user_id LIMIT 1"
)
_MEMBER_BY_DISPLAY_NAME_SQL = (
    "SELECT cm.display_name, cm.reputation, cm.user_id AS hub_user_id "
    "FROM community_members cm "
    "WHERE cm.community_id = :community_id AND cm.display_name = :display_name LIMIT 1"
)

_COMMUNITY_LABEL_SQL = (
    "SELECT COALESCE(display_name, name) AS label FROM communities "
    "WHERE id = :community_id LIMIT 1"
)

_GLOBAL_SCORE_SQL = "SELECT score FROM reputation_global WHERE hub_user_id = :hub_user_id"
```

- [ ] **Step 3: Replace `_fetch_community_label`'s body**

Old: `rows = await dal.execute(_COMMUNITY_LABEL_SQL, [community_id])`
New: `rows = await raw_sql_rows(dal, _COMMUNITY_LABEL_SQL, {"community_id": community_id})`
(`if rows and rows[0]["label"]:` stays unchanged.)

- [ ] **Step 4: Replace `_fetch_member`'s two lookups**

Old:
```python
    if platform_user_id:
        rows = await dal.execute(
            _MEMBER_SQL.format(clause="cm.platform = $2 AND cm.platform_user_id = $3"),
            [community_id, platform, platform_user_id],
        )
        if rows:
            row = rows[0]
            reputation = row["reputation"]
            return (
                row["display_name"] or platform_user_id,
                int(reputation) if reputation is not None else _DEFAULT_SCORE,
                row["hub_user_id"],
            )

    if actor:
        rows = await dal.execute(
            _MEMBER_SQL.format(clause="cm.display_name = $2"),
            [community_id, actor],
        )
```

New:
```python
    if platform_user_id:
        rows = await raw_sql_rows(
            dal,
            _MEMBER_BY_PLATFORM_SQL,
            {"community_id": community_id, "platform": platform, "platform_user_id": platform_user_id},
        )
        if rows:
            row = rows[0]
            reputation = row["reputation"]
            return (
                row["display_name"] or platform_user_id,
                int(reputation) if reputation is not None else _DEFAULT_SCORE,
                row["hub_user_id"],
            )

    if actor:
        rows = await raw_sql_rows(
            dal, _MEMBER_BY_DISPLAY_NAME_SQL, {"community_id": community_id, "display_name": actor}
        )
```

(The rest of the `if actor:` branch's body is unchanged.)

- [ ] **Step 5: Replace `_fetch_global_score`'s body**

Old: `rows = await dal.execute(_GLOBAL_SCORE_SQL, [parsed_id])`
New: `rows = await raw_sql_rows(dal, _GLOBAL_SCORE_SQL, {"hub_user_id": parsed_id})`

- [ ] **Step 6: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with the three tables this bundle reads."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE community_members ("
                "community_id INTEGER, platform TEXT, platform_user_id TEXT, "
                "display_name TEXT, reputation INTEGER, user_id TEXT)"
            )
        )
        await conn.execute(
            sa_text("CREATE TABLE communities (id INTEGER, display_name TEXT, name TEXT)")
        )
        await conn.execute(
            sa_text("CREATE TABLE reputation_global (hub_user_id TEXT, score INTEGER)")
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed rows per test with `async with dal.engine.begin() as conn: await conn.execute(sa_text("INSERT INTO community_members (...) VALUES (...)"))`, matching each existing test's scenario (member found by platform id, member found by display name, member not found → defaults to 600, community label found/missing, global score found/missing). Swap every `set_bundle_dal(_mock_executor(...))`/`AsyncMock` call for the `dal` fixture parameter, per Task 5's established pattern.

- [ ] **Step 7: Run tests, gate, coverage**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_community_reputation_process.py -v 2>&1 | tail -30`
Expected: all green, including the byte-identical-tier-table test (`test_tier_table_matches_hub_api`, untouched — it parses `hub_api`'s source file directly, no DAL involved).

Run: `env -C core/svc_process python3 -m pytest --cov=bundles.community_reputation_process --cov-fail-under=90 tests/test_bundles_community_reputation_process.py`

- [ ] **Step 8: Commit**

```bash
git add core/svc_process/bundles/community_reputation_process.py \
        core/svc_process/tests/test_bundles_community_reputation_process.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate community_reputation_process to penguin-dal

Splits the old .format()-templated _MEMBER_SQL (positional $2/$3 swapped
by clause) into two explicit named-parameter queries -- raw_sql_rows()
makes the dynamic-clause indirection unnecessary. D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 8: Migrate `inventory_process.py`

**Files:**
- Modify: `core/svc_process/bundles/inventory_process.py`
- Modify: `core/svc_process/tests/test_bundles_inventory_process.py`

**Interfaces:** Consumes `raw_sql_rows`/`raw_sql_write` (Task 3). Depends on: Task 4.

- [ ] **Step 1: Imports** — add `from flask_core.bundle_runtime import raw_sql_rows, raw_sql_write` (the `import json` this file already has for `_coerce_metadata` stays, now also used to serialize the `metadata` param).

- [ ] **Step 2: Replace `_handle_add`'s two DB calls**

Old:
```python
        dal = get_bundle_dal()
        existing = await dal.execute(
            "SELECT id FROM inventory_items "
            "WHERE community_id = $1 AND name = $2 AND deleted_at IS NULL "
            "LIMIT 1",
            [community_id, name],
        )
        if existing:
            reply_text = f"'{name}' already exists in inventory."
        else:
            metadata = {"tags": flags.get("-t"), "owner": flags.get("-o")}
            await dal.execute(
                "INSERT INTO inventory_items "
                "(community_id, name, item_type, quantity, available_quantity, "
                "metadata, created_at, updated_at) "
                "VALUES ($1, $2, 'general', 1, 1, $3::jsonb, NOW(), NOW())",
                [community_id, name, metadata],
            )
            reply_text = f"\U0001f4e6 added '{name}' to inventory."
```

New:
```python
        dal = get_bundle_dal()
        existing = await raw_sql_rows(
            dal,
            "SELECT id FROM inventory_items "
            "WHERE community_id = :community_id AND name = :name AND deleted_at IS NULL LIMIT 1",
            {"community_id": community_id, "name": name},
        )
        if existing:
            reply_text = f"'{name}' already exists in inventory."
        else:
            metadata = {"tags": flags.get("-t"), "owner": flags.get("-o")}
            await raw_sql_write(
                dal,
                "INSERT INTO inventory_items "
                "(community_id, name, item_type, quantity, available_quantity, "
                "metadata, created_at, updated_at) "
                "VALUES (:community_id, :name, 'general', 1, 1, CAST(:metadata AS jsonb), NOW(), NOW())",
                {"community_id": community_id, "name": name, "metadata": json.dumps(metadata)},
            )
            reply_text = f"\U0001f4e6 added '{name}' to inventory."
```

(`asyncpg` binds a plain string for `:metadata`; `CAST(:metadata AS jsonb)` performs the same server-side cast the old `$3::jsonb` shorthand did — `json.dumps(metadata)` mirrors what `flask_core.database.AsyncDAL.execute()`'s own dict/list-to-JSON-string conversion already did for psycopg2, so the on-the-wire behavior is unchanged.)

- [ ] **Step 3: Replace `_handle_remove`'s DB call**

Old:
```python
        dal = get_bundle_dal()
        result = await dal.execute(
            "UPDATE inventory_items SET deleted_at = NOW(), updated_at = NOW() "
            "WHERE community_id = $1 AND name = $2 AND deleted_at IS NULL "
            "RETURNING id",
            [community_id, name],
        )
```

New:
```python
        dal = get_bundle_dal()
        result = await raw_sql_write(
            dal,
            "UPDATE inventory_items SET deleted_at = NOW(), updated_at = NOW() "
            "WHERE community_id = :community_id AND name = :name AND deleted_at IS NULL "
            "RETURNING id",
            {"community_id": community_id, "name": name},
        )
```

- [ ] **Step 4: Replace `_handle_list`'s DB call**

Old:
```python
        dal = get_bundle_dal()
        rows = await dal.execute(
            "SELECT name, quantity, available_quantity, metadata FROM inventory_items "
            "WHERE community_id = $1 AND deleted_at IS NULL "
            "ORDER BY name LIMIT 20",
            [community_id],
        )
```

New:
```python
        dal = get_bundle_dal()
        rows = await raw_sql_rows(
            dal,
            "SELECT name, quantity, available_quantity, metadata FROM inventory_items "
            "WHERE community_id = :community_id AND deleted_at IS NULL ORDER BY name LIMIT 20",
            {"community_id": community_id},
        )
```

`_format_list(rows)` iterates `for row in rows: ... row.get("metadata") ...` — unchanged, `Rows` is iterable and `Row.get()` matches the old dict shape.

- [ ] **Step 5: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with a minimal `inventory_items` table."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE inventory_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, name TEXT, "
                "item_type TEXT, quantity INTEGER, available_quantity INTEGER, "
                "metadata TEXT, deleted_at TEXT, created_at TEXT, updated_at TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

(SQLite has no native `jsonb`/`NOW()` — the fixture's `metadata` column is plain `TEXT` and any test seeding a row uses a literal timestamp string instead of `NOW()`; this is a test-only accommodation, the bundle's own SQL text sent to the real Postgres driver in production is unchanged.) Seed/assert per existing test scenario (already-exists, not-found remove, empty list, populated list with tags/owner metadata) exactly as Task 5 Step 4 established.

- [ ] **Step 6: Run tests, gate, coverage**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_inventory_process.py -v 2>&1 | tail -30`
Run: `env -C core/svc_process python3 -m pytest --cov=bundles.inventory_process --cov-fail-under=90 tests/test_bundles_inventory_process.py`

- [ ] **Step 7: Commit**

```bash
git add core/svc_process/bundles/inventory_process.py \
        core/svc_process/tests/test_bundles_inventory_process.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate inventory_process to penguin-dal

SELECT/INSERT/UPDATE ... RETURNING raw SQL moves to raw_sql_rows()/
raw_sql_write() with named :param placeholders; the $3::jsonb metadata
cast becomes CAST(:metadata AS jsonb) over a json.dumps()-serialized
param, matching AsyncDAL.execute()'s own prior dict-to-JSON behavior. D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 9: Migrate `community_polls_process.py` (11 call sites — the largest single bundle)

**Files:**
- Modify: `core/svc_process/bundles/community_polls_process.py`
- Modify: `core/svc_process/tests/test_bundles_community_polls_process.py`

**Interfaces:** Consumes `raw_sql_rows`/`raw_sql_write` (Task 3). Depends on: Task 4.

- [ ] **Step 1: Imports** — add `from flask_core.bundle_runtime import raw_sql_rows, raw_sql_write` alongside `from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal`.

- [ ] **Step 2: `_handle_poll_create`** — replace both DB calls

Old:
```python
        sql = """
            INSERT INTO community_polls
            (community_id, created_by, title, is_active, created_at, updated_at)
            VALUES ($1, $2, $3, TRUE, NOW(), NOW())
            RETURNING id
        """
        creator_id = event.actor or "unknown"

        result = await dal.execute(sql, [community_id, creator_id, title])
        poll_id = result[0]["id"] if result else None

        if not poll_id:
            reply_text = "Failed to create poll."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        for idx, option_text in enumerate(options):
            opt_sql = """
                INSERT INTO poll_options (poll_id, option_text, sort_order)
                VALUES ($1, $2, $3)
            """
            await dal.execute(opt_sql, [poll_id, option_text, idx])
```

New:
```python
        creator_id = event.actor or "unknown"
        result = await raw_sql_write(
            dal,
            "INSERT INTO community_polls (community_id, created_by, title, is_active, created_at, updated_at) "
            "VALUES (:community_id, :created_by, :title, TRUE, NOW(), NOW()) RETURNING id",
            {"community_id": community_id, "created_by": creator_id, "title": title},
        )
        poll_id = result[0]["id"] if result else None

        if not poll_id:
            reply_text = "Failed to create poll."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        for idx, option_text in enumerate(options):
            await raw_sql_write(
                dal,
                "INSERT INTO poll_options (poll_id, option_text, sort_order) "
                "VALUES (:poll_id, :option_text, :sort_order)",
                {"poll_id": poll_id, "option_text": option_text, "sort_order": idx},
            )
```

- [ ] **Step 3: `_handle_poll_vote`** — replace all three DB calls

Old:
```python
        poll_sql = (
            "SELECT id, title FROM community_polls WHERE id = $1 AND community_id = $2 "
            "AND is_active = TRUE"
        )
        poll_result = await dal.execute(poll_sql, [poll_id, community_id])
        if not poll_result:
            reply_text = f"Poll {poll_id} not found or is closed."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        opts_sql = "SELECT id FROM poll_options WHERE poll_id = $1 ORDER BY sort_order"
        opts_result = await dal.execute(opts_sql, [poll_id])
        num_opts = len(opts_result) if opts_result else 0
        if not opts_result or option_number < 1 or option_number > len(opts_result):
            reply_text = f"Invalid option number. Poll {poll_id} has {num_opts} options."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        option_id = opts_result[option_number - 1]["id"]

        vote_sql = """
            INSERT INTO poll_votes (poll_id, option_id, user_id, voted_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (poll_id, option_id, user_id) DO UPDATE
            SET voted_at = NOW()
        """
        voter_id = event.actor or "unknown"

        await dal.execute(vote_sql, [poll_id, option_id, voter_id])
```

New:
```python
        poll_result = await raw_sql_rows(
            dal,
            "SELECT id, title FROM community_polls WHERE id = :poll_id AND community_id = :community_id "
            "AND is_active = TRUE",
            {"poll_id": poll_id, "community_id": community_id},
        )
        if not poll_result:
            reply_text = f"Poll {poll_id} not found or is closed."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        opts_result = await raw_sql_rows(
            dal,
            "SELECT id FROM poll_options WHERE poll_id = :poll_id ORDER BY sort_order",
            {"poll_id": poll_id},
        )
        num_opts = len(opts_result) if opts_result else 0
        if not opts_result or option_number < 1 or option_number > len(opts_result):
            reply_text = f"Invalid option number. Poll {poll_id} has {num_opts} options."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        option_id = opts_result[option_number - 1]["id"]

        voter_id = event.actor or "unknown"
        await raw_sql_write(
            dal,
            "INSERT INTO poll_votes (poll_id, option_id, user_id, voted_at) "
            "VALUES (:poll_id, :option_id, :user_id, NOW()) "
            "ON CONFLICT (poll_id, option_id, user_id) DO UPDATE SET voted_at = NOW()",
            {"poll_id": poll_id, "option_id": option_id, "user_id": voter_id},
        )
```

- [ ] **Step 4: `_handle_poll_close`** — replace all three DB calls

Old:
```python
        sql = "SELECT id, title FROM community_polls WHERE id = $1 AND community_id = $2"
        result = await dal.execute(sql, [poll_id, community_id])
        if not result:
            reply_text = f"Poll {poll_id} not found."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        poll = result[0]

        close_sql = (
            "UPDATE community_polls SET is_active = FALSE WHERE id = $1 AND community_id = $2"
        )
        await dal.execute(close_sql, [poll_id, community_id])

        results_sql = """
            SELECT po.option_text, COUNT(pv.id) as vote_count
            FROM poll_options po
            LEFT JOIN poll_votes pv ON po.id = pv.option_id
            WHERE po.poll_id = $1
            GROUP BY po.id, po.option_text
            ORDER BY po.sort_order
        """
        results = await dal.execute(results_sql, [poll_id])
```

New:
```python
        result = await raw_sql_rows(
            dal,
            "SELECT id, title FROM community_polls WHERE id = :poll_id AND community_id = :community_id",
            {"poll_id": poll_id, "community_id": community_id},
        )
        if not result:
            reply_text = f"Poll {poll_id} not found."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        poll = result[0]

        await raw_sql_write(
            dal,
            "UPDATE community_polls SET is_active = FALSE WHERE id = :poll_id AND community_id = :community_id",
            {"poll_id": poll_id, "community_id": community_id},
        )

        results = await raw_sql_rows(
            dal,
            "SELECT po.option_text, COUNT(pv.id) as vote_count "
            "FROM poll_options po LEFT JOIN poll_votes pv ON po.id = pv.option_id "
            "WHERE po.poll_id = :poll_id GROUP BY po.id, po.option_text ORDER BY po.sort_order",
            {"poll_id": poll_id},
        )
```

- [ ] **Step 5: `_handle_poll_list`** — replace the one DB call

Old:
```python
        sql = """
            SELECT id, title FROM community_polls
            WHERE community_id = $1 AND is_active = TRUE
            ORDER BY created_at DESC
            LIMIT 10
        """
        results = await dal.execute(sql, [community_id])
```

New:
```python
        results = await raw_sql_rows(
            dal,
            "SELECT id, title FROM community_polls WHERE community_id = :community_id "
            "AND is_active = TRUE ORDER BY created_at DESC LIMIT 10",
            {"community_id": community_id},
        )
```

- [ ] **Step 6: `_handle_poll_view`** — replace both DB calls

Old:
```python
        sql = "SELECT id, title, is_active FROM community_polls WHERE id = $1 AND community_id = $2"
        result = await dal.execute(sql, [poll_id, community_id])
        if not result:
            reply_text = f"Poll {poll_id} not found."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        poll = result[0]
        status = "Active" if poll["is_active"] else "Closed"

        opts_sql = """
            SELECT po.id, po.option_text, COUNT(pv.id) as vote_count
            FROM poll_options po
            LEFT JOIN poll_votes pv ON po.id = pv.option_id
            WHERE po.poll_id = $1
            GROUP BY po.id, po.option_text
            ORDER BY po.sort_order
        """
        options = await dal.execute(opts_sql, [poll_id])
```

New:
```python
        result = await raw_sql_rows(
            dal,
            "SELECT id, title, is_active FROM community_polls WHERE id = :poll_id AND community_id = :community_id",
            {"poll_id": poll_id, "community_id": community_id},
        )
        if not result:
            reply_text = f"Poll {poll_id} not found."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        poll = result[0]
        status = "Active" if poll["is_active"] else "Closed"

        options = await raw_sql_rows(
            dal,
            "SELECT po.id, po.option_text, COUNT(pv.id) as vote_count "
            "FROM poll_options po LEFT JOIN poll_votes pv ON po.id = pv.option_id "
            "WHERE po.poll_id = :poll_id GROUP BY po.id, po.option_text ORDER BY po.sort_order",
            {"poll_id": poll_id},
        )
```

Every downstream line (`row.get("vote_count", 0)`, `row['option_text']`, `poll['title']`) is unchanged — `Row` supports both `.get()` and `[...]`.

- [ ] **Step 7: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with the three tables this bundle touches."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE community_polls ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, created_by TEXT, "
                "title TEXT, is_active BOOLEAN, created_at TEXT, updated_at TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE poll_options ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, poll_id INTEGER, option_text TEXT, sort_order INTEGER)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE poll_votes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, poll_id INTEGER, option_id INTEGER, "
                "user_id TEXT, voted_at TEXT, UNIQUE(poll_id, option_id, user_id))"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed `community_polls`/`poll_options`/`poll_votes` rows per existing test scenario (poll create → assert a new row exists via a follow-up `raw_sql_rows` read; vote → seed a poll+options first, then assert a `poll_votes` row landed, including the re-vote/`ON CONFLICT` update path; close/view/list → seed accordingly), following Task 5 Step 4's pattern throughout.

- [ ] **Step 8: Run tests, gate, coverage**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_community_polls_process.py -v 2>&1 | tail -40`
Run: `env -C core/svc_process python3 -m pytest --cov=bundles.community_polls_process --cov-fail-under=90 tests/test_bundles_community_polls_process.py`

- [ ] **Step 9: Commit**

```bash
git add core/svc_process/bundles/community_polls_process.py \
        core/svc_process/tests/test_bundles_community_polls_process.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate community_polls_process to penguin-dal

All 11 raw SQL call sites (create/vote/close/list/view, including the
LEFT JOIN ... GROUP BY vote-tally queries and the ON CONFLICT DO UPDATE
re-vote path) move to raw_sql_rows()/raw_sql_write() with named :param
placeholders. Query text and semantics are unchanged. D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 10: Migrate `community_chat_process.py`

**Files:**
- Modify: `core/svc_process/bundles/community_chat_process.py`
- Modify: `core/svc_process/tests/test_bundles_community_chat_process.py`

**Interfaces:** Consumes `raw_sql_rows` (Task 3). Depends on: Task 4.

- [ ] **Step 1: Imports** — add `from flask_core.bundle_runtime import raw_sql_rows`.

- [ ] **Step 2: Replace the chat-history query**

Old:
```python
            ctx = get_bundle_context()
            dal = get_bundle_dal()

            sql = """
                SELECT id, community_id, channel_name, sender_username, message_content,
                       message_type, created_at
                FROM hub_chat_messages
                WHERE community_id = (SELECT id FROM communities WHERE id = $1 OR tenant_id = (
                    SELECT id FROM tenants WHERE id = $2))
                ORDER BY created_at DESC
                LIMIT 20
            """
            rows = await dal.execute(sql, [int(ctx.community) if ctx.community else 0, ctx.tenant])
```

New (SQL text — including the pre-existing `tenants.id = :tenant_id` comparison against a tenant *slug* — is preserved verbatim; only the placeholder syntax changes, per this milestone's "logic untouched" constraint, D7):
```python
            ctx = get_bundle_context()
            dal = get_bundle_dal()

            rows = await raw_sql_rows(
                dal,
                "SELECT id, community_id, channel_name, sender_username, message_content, "
                "message_type, created_at FROM hub_chat_messages "
                "WHERE community_id = (SELECT id FROM communities WHERE id = :community_id "
                "OR tenant_id = (SELECT id FROM tenants WHERE id = :tenant_id)) "
                "ORDER BY created_at DESC LIMIT 20",
                {
                    "community_id": int(ctx.community) if ctx.community else 0,
                    "tenant_id": ctx.tenant,
                },
            )
```

- [ ] **Step 3: Replace the channel-list query**

Old:
```python
            ctx = get_bundle_context()
            dal = get_bundle_dal()

            sql = """
                SELECT channel_name, COUNT(*) AS message_count, MAX(created_at) AS last_message_at
                FROM hub_chat_messages
                WHERE community_id = (SELECT id FROM communities WHERE id = $1 OR tenant_id = (
                    SELECT id FROM tenants WHERE id = $2))
                GROUP BY channel_name
                ORDER BY last_message_at DESC
            """
            rows = await dal.execute(sql, [int(ctx.community) if ctx.community else 0, ctx.tenant])
```

New:
```python
            ctx = get_bundle_context()
            dal = get_bundle_dal()

            rows = await raw_sql_rows(
                dal,
                "SELECT channel_name, COUNT(*) AS message_count, MAX(created_at) AS last_message_at "
                "FROM hub_chat_messages "
                "WHERE community_id = (SELECT id FROM communities WHERE id = :community_id "
                "OR tenant_id = (SELECT id FROM tenants WHERE id = :tenant_id)) "
                "GROUP BY channel_name ORDER BY last_message_at DESC",
                {
                    "community_id": int(ctx.community) if ctx.community else 0,
                    "tenant_id": ctx.tenant,
                },
            )
```

- [ ] **Step 4: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with the three tables this bundle's correlated subquery touches."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(sa_text("CREATE TABLE communities (id INTEGER, tenant_id TEXT)"))
        await conn.execute(sa_text("CREATE TABLE tenants (id TEXT)"))
        await conn.execute(
            sa_text(
                "CREATE TABLE hub_chat_messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, channel_name TEXT, "
                "sender_username TEXT, message_content TEXT, message_type TEXT, created_at TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed `communities`/`tenants`/`hub_chat_messages` rows matching each existing test's scenario, per Task 5 Step 4's pattern.

- [ ] **Step 5: Run tests, gate, coverage**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_community_chat_process.py -v 2>&1 | tail -30`
Run: `env -C core/svc_process python3 -m pytest --cov=bundles.community_chat_process --cov-fail-under=90 tests/test_bundles_community_chat_process.py`

- [ ] **Step 6: Commit**

```bash
git add core/svc_process/bundles/community_chat_process.py \
        core/svc_process/tests/test_bundles_community_chat_process.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate community_chat_process to penguin-dal

Both correlated-subquery raw SQL reads (chat-history, channel list) move
to raw_sql_rows() with named :param placeholders; SQL text and the
pre-existing tenant_id-vs-slug comparison are preserved verbatim (D7 --
only DB-access lines change). D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 11: Migrate `community_loyalty_process.py` (establishes the shared permission-check pattern)

**Files:**
- Modify: `core/svc_process/bundles/community_loyalty_process.py`
- Modify: `core/svc_process/tests/test_bundles_community_loyalty_process.py`

**Interfaces:** Consumes `raw_sql_rows` (Task 3). Depends on: Task 4. This task's `_caller_is_moderator_or_admin` translation is byte-for-byte identical to Tasks 12–14's own copies of the same helper (`social_alias_process`, `social_music_process`, `social_shoutout_process` each carry their own independently-replicated copy, per those files' own docstrings — "replicated locally rather than imported").

- [ ] **Step 1: Imports** — add `from flask_core.bundle_runtime import raw_sql_rows`.

- [ ] **Step 2: Replace the two SQL constants' placeholder syntax**

Old:
```python
_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = $1 AND platform = $2 AND platform_user_id = $3 LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members WHERE community_id = $1 AND display_name = $2 LIMIT 1"
)
```

New:
```python
_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND platform = :platform "
    "AND platform_user_id = :platform_user_id LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND display_name = :display_name LIMIT 1"
)
```

- [ ] **Step 3: Replace `_caller_is_moderator_or_admin`'s two DB calls**

Old:
```python
        if platform_user_id:
            rows = await dal.execute(
                _ROLE_BY_PLATFORM_SQL, [community_id, event.platform, platform_user_id]
            )
            if rows:
                return str(rows[0]["role"]).lower() in _ADMIN_ROLES
        if event.actor:
            rows = await dal.execute(_ROLE_BY_DISPLAY_NAME_SQL, [community_id, event.actor])
            if rows:
                return str(rows[0]["role"]).lower() in _ADMIN_ROLES
```

New:
```python
        if platform_user_id:
            rows = await raw_sql_rows(
                dal,
                _ROLE_BY_PLATFORM_SQL,
                {
                    "community_id": community_id,
                    "platform": event.platform,
                    "platform_user_id": platform_user_id,
                },
            )
            if rows:
                return str(rows[0]["role"]).lower() in _ADMIN_ROLES
        if event.actor:
            rows = await raw_sql_rows(
                dal, _ROLE_BY_DISPLAY_NAME_SQL, {"community_id": community_id, "display_name": event.actor}
            )
            if rows:
                return str(rows[0]["role"]).lower() in _ADMIN_ROLES
```

(The surrounding `try`/`except Exception` fail-closed wrapper is unchanged.)

- [ ] **Step 4: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with a minimal `community_members` table."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE community_members ("
                "community_id INTEGER, platform TEXT, platform_user_id TEXT, "
                "display_name TEXT, role TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed one `community_members` row per permission-check test scenario (admin by platform id, moderator by display name, no match → deny, DB error → deny — the last one can drop the table or point `dal` at a closed engine to force the `except Exception` branch, matching whatever the existing test already does to simulate a DB failure). Apply the same swap to every `!points`/`!top`/`!shop`/`!redeem` behavioral test in the file, per Task 5 Step 4's pattern.

- [ ] **Step 5: Run tests, gate, coverage**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_community_loyalty_process.py -v 2>&1 | tail -30`
Run: `env -C core/svc_process python3 -m pytest --cov=bundles.community_loyalty_process --cov-fail-under=90 tests/test_bundles_community_loyalty_process.py`

- [ ] **Step 6: Commit**

```bash
git add core/svc_process/bundles/community_loyalty_process.py \
        core/svc_process/tests/test_bundles_community_loyalty_process.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate community_loyalty_process to penguin-dal

_caller_is_moderator_or_admin's two role-lookup raw SQL calls move to
raw_sql_rows() with named :param placeholders -- the same translation
applied identically (each file replicates this helper locally) in
social_alias_process, social_music_process, and social_shoutout_process. D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 12: Migrate `social_alias_process.py` (also removes its `asyncio.to_thread` wrapping — spec Assumption A20)

**Files:**
- Modify: `core/svc_process/bundles/social_alias_process.py`
- Modify: `core/svc_process/tests/test_bundles_social_alias_process.py`

**Interfaces:** Consumes `raw_sql_rows` (Task 3), `penguin_dal.AsyncDB`'s native `Query`/`TableProxy` builder (Pattern A). Depends on: Task 4.

- [ ] **Step 1: Imports** — add `from flask_core.bundle_runtime import raw_sql_rows`. Remove `import asyncio` if this file's only use of it was the four `asyncio.to_thread(...)` calls this task deletes (`grep -n "asyncio\." core/svc_process/bundles/social_alias_process.py` after Steps 2–5 to confirm before removing the import).

- [ ] **Step 2: Replace the two SQL constants and `_caller_is_moderator_or_admin`'s body** — identical translation to Task 11:

Old constants:
```python
_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = $1 AND platform = $2 AND platform_user_id = $3 LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members WHERE community_id = $1 AND display_name = $2 LIMIT 1"
)
```

New constants (same as Task 11):
```python
_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND platform = :platform "
    "AND platform_user_id = :platform_user_id LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND display_name = :display_name LIMIT 1"
)
```

Old body:
```python
    dal = get_bundle_dal()
    raw_author_id = event.payload.get("author_id")
    platform_user_id = raw_author_id if isinstance(raw_author_id, str) else None

    try:
        if platform_user_id:
            rows = await dal.execute(
                _ROLE_BY_PLATFORM_SQL, [community_id, event.platform, platform_user_id]
            )
```
(and its `_ROLE_BY_DISPLAY_NAME_SQL` sibling call a few lines below)

New body:
```python
    dal = get_bundle_dal()
    raw_author_id = event.payload.get("author_id")
    platform_user_id = raw_author_id if isinstance(raw_author_id, str) else None

    try:
        if platform_user_id:
            rows = await raw_sql_rows(
                dal,
                _ROLE_BY_PLATFORM_SQL,
                {
                    "community_id": community_id,
                    "platform": event.platform,
                    "platform_user_id": platform_user_id,
                },
            )
```
with the `_ROLE_BY_DISPLAY_NAME_SQL` call replaced the same way as Task 11 Step 3.

- [ ] **Step 3: Replace `_lookup_alias`** — drop the `asyncio.to_thread`-wrapped sync closure entirely

Old:
```python
async def _lookup_alias(community_id: int, alias_name: str) -> str | None:
    """Return the active `target_command` for `alias_name`, or `None` if not found."""
    dal = get_bundle_dal()

    def _query() -> str | None:
        query = (
            (dal.command_aliases.community_id == community_id)
            & (dal.command_aliases.alias == alias_name)
            & (dal.command_aliases.deleted_at.is_null())
        )
        rows = dal.select(query)
        row = rows.first()
        return str(row.target_command) if row is not None else None

    return await asyncio.to_thread(_query)
```

New (WASI has no thread pool -- spec Assumption A20 -- so this and every remaining function in this task call `penguin_dal`'s own async query builder directly, no thread offload):
```python
async def _lookup_alias(community_id: int, alias_name: str) -> str | None:
    """Return the active `target_command` for `alias_name`, or `None` if not found."""
    dal = get_bundle_dal()
    query = (
        (dal.command_aliases.community_id == community_id)
        & (dal.command_aliases.alias == alias_name)
        & (dal.command_aliases.deleted_at == None)  # noqa: E711 -- FieldProxy.__eq__(None) emits IS NULL
    )
    rows = await dal(query).select()
    row = rows.first()
    return str(row.target_command) if row is not None else None
```

- [ ] **Step 4: Replace `_upsert_alias`**

Old:
```python
async def _upsert_alias(
    community_id: int, alias_name: str, target_command: str, created_by: str | None
) -> None:
    dal = get_bundle_dal()

    def _write() -> None:
        query = (dal.command_aliases.community_id == community_id) & (
            dal.command_aliases.alias == alias_name
        )
        rows = dal.select(query)
        existing = rows.first()
        if existing is not None:
            dal.update(
                dal.command_aliases.id == existing.id,
                target_command=target_command,
                deleted_at=None,
                created_by=created_by or "unknown",
            )
        else:
            dal.insert_async(
                dal.command_aliases,
                community_id=community_id,
                alias=alias_name,
                target_command=target_command,
                created_by=created_by or "unknown",
            )

    await asyncio.to_thread(_write)
```

New — **note for the PR description**: the old `else` branch called `dal.insert_async(...)` — an `async def` method — from inside a plain `def _write()` closure with no `await`, which only ever constructed a coroutine object and discarded it without running; the "revive a soft-deleted alias" (`UPDATE`) path worked, but "insert a brand-new alias name" silently never persisted. This migration's `await dal.command_aliases.async_insert(...)` actually awaits the insert, which is both the correct `penguin_dal` idiom AND an incidental fix — call this out explicitly when opening the PR so a reviewer isn't surprised by the behavior change:
```python
async def _upsert_alias(
    community_id: int, alias_name: str, target_command: str, created_by: str | None
) -> None:
    dal = get_bundle_dal()
    query = (dal.command_aliases.community_id == community_id) & (
        dal.command_aliases.alias == alias_name
    )
    rows = await dal(query).select()
    existing = rows.first()
    if existing is not None:
        await dal(dal.command_aliases.id == existing.id).update(
            target_command=target_command,
            deleted_at=None,
            created_by=created_by or "unknown",
        )
    else:
        await dal.command_aliases.async_insert(
            community_id=community_id,
            alias=alias_name,
            target_command=target_command,
            created_by=created_by or "unknown",
        )
```

- [ ] **Step 5: Replace `_soft_delete_alias` and `_list_aliases`**

Old:
```python
async def _soft_delete_alias(community_id: int, alias_name: str) -> bool:
    dal = get_bundle_dal()

    def _delete() -> bool:
        query = (
            (dal.command_aliases.community_id == community_id)
            & (dal.command_aliases.alias == alias_name)
            & (dal.command_aliases.deleted_at.is_null())
        )
        rows = dal.select(query)
        row = rows.first()
        if row is None:
            return False
        dal.update(dal.command_aliases.id == row.id, deleted_at=datetime.now(UTC))
        return True

    return await asyncio.to_thread(_delete)


async def _list_aliases(community_id: int) -> list[tuple[str, str]]:
    dal = get_bundle_dal()

    def _query() -> list[tuple[str, str]]:
        query = (dal.command_aliases.community_id == community_id) & (
            dal.command_aliases.deleted_at.is_null()
        )
        rows = dal.select(query)
        return sorted(
            ((str(row.alias), str(row.target_command)) for row in rows), key=lambda pair: pair[0]
        )

    return await asyncio.to_thread(_query)
```

New:
```python
async def _soft_delete_alias(community_id: int, alias_name: str) -> bool:
    dal = get_bundle_dal()
    query = (
        (dal.command_aliases.community_id == community_id)
        & (dal.command_aliases.alias == alias_name)
        & (dal.command_aliases.deleted_at == None)  # noqa: E711
    )
    rows = await dal(query).select()
    row = rows.first()
    if row is None:
        return False
    await dal(dal.command_aliases.id == row.id).update(deleted_at=datetime.now(UTC))
    return True


async def _list_aliases(community_id: int) -> list[tuple[str, str]]:
    dal = get_bundle_dal()
    query = (dal.command_aliases.community_id == community_id) & (
        dal.command_aliases.deleted_at == None  # noqa: E711
    )
    rows = await dal(query).select()
    return sorted(
        ((str(row.alias), str(row.target_command)) for row in rows), key=lambda pair: pair[0]
    )
```

- [ ] **Step 6: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with `command_aliases` + `community_members`."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE command_aliases ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, alias TEXT, "
                "target_command TEXT, created_by TEXT, deleted_at TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE community_members ("
                "community_id INTEGER, platform TEXT, platform_user_id TEXT, "
                "display_name TEXT, role TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed `command_aliases`/`community_members` rows per existing test scenario (lookup found/missing, upsert of a brand-new alias — assert the row now exists via a follow-up read, upsert reviving a soft-deleted alias, soft-delete found/missing, list sorted-by-name, and the permission-check scenarios) per Task 5 Step 4's pattern. For `test_upsert_alias_inserts_when_no_existing_row` specifically, assert the new row is actually persisted (`SELECT` it back) — this is the test that would have silently passed against the pre-migration bug (an uncalled coroutine raises nothing) but must now genuinely verify persistence.

- [ ] **Step 7: Run tests, gate, coverage**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_social_alias_process.py -v 2>&1 | tail -40`
Run: `env -C core/svc_process python3 -m pytest --cov=bundles.social_alias_process --cov-fail-under=90 tests/test_bundles_social_alias_process.py`

- [ ] **Step 8: Commit**

```bash
git add core/svc_process/bundles/social_alias_process.py \
        core/svc_process/tests/test_bundles_social_alias_process.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate social_alias_process to penguin-dal

Alias CRUD moves from asyncio.to_thread-wrapped raw pydal proxy calls to
penguin_dal's own async Query/TableProxy builder directly -- WASI has no
thread pool (spec Assumption A20). Incidentally fixes _upsert_alias's
new-alias INSERT path: the old code called AsyncDAL.insert_async() (async)
from a sync closure with no await, so it never ran; await
dal.command_aliases.async_insert(...) now actually persists it.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 13: Migrate `social_music_process.py`

**Files:**
- Modify: `core/svc_process/bundles/social_music_process.py`
- Modify: `core/svc_process/tests/test_bundles_social_music_process.py`

**Interfaces:** Consumes `raw_sql_rows` (Task 3). Depends on: Task 4. This bundle's only DB call site is its own copy of the `_caller_is_moderator_or_admin` helper (guards `!sr set youtube-labels`/`!sr pause`/`!sr resume`) — identical translation to Task 11.

- [ ] **Step 1: Imports** — add `from flask_core.bundle_runtime import raw_sql_rows`.

- [ ] **Step 2: Replace the two SQL constants**

Old:
```python
_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = $1 AND platform = $2 AND platform_user_id = $3 LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members WHERE community_id = $1 AND display_name = $2 LIMIT 1"
)
```

New: identical to Task 11's Step 2 (same two constants, same names).

- [ ] **Step 3: Replace `_caller_is_moderator_or_admin`'s body**

Old:
```python
    dal = get_bundle_dal()
    raw_author_id = event.payload.get("author_id")
    platform_user_id = raw_author_id if isinstance(raw_author_id, str) else None

    try:
        if platform_user_id:
            rows = await dal.execute(
                _ROLE_BY_PLATFORM_SQL, [community_id, event.platform, platform_user_id]
            )
            if rows:
                return str(rows[0]["role"]).lower() in _ADMIN_ROLES
        if event.actor:
            rows = await dal.execute(_ROLE_BY_DISPLAY_NAME_SQL, [community_id, event.actor])
            if rows:
                return str(rows[0]["role"]).lower() in _ADMIN_ROLES
```

New (identical shape to Task 11 Step 3):
```python
    dal = get_bundle_dal()
    raw_author_id = event.payload.get("author_id")
    platform_user_id = raw_author_id if isinstance(raw_author_id, str) else None

    try:
        if platform_user_id:
            rows = await raw_sql_rows(
                dal,
                _ROLE_BY_PLATFORM_SQL,
                {
                    "community_id": community_id,
                    "platform": event.platform,
                    "platform_user_id": platform_user_id,
                },
            )
            if rows:
                return str(rows[0]["role"]).lower() in _ADMIN_ROLES
        if event.actor:
            rows = await raw_sql_rows(
                dal, _ROLE_BY_DISPLAY_NAME_SQL, {"community_id": community_id, "display_name": event.actor}
            )
            if rows:
                return str(rows[0]["role"]).lower() in _ADMIN_ROLES
```

- [ ] **Step 4: Rewrite the test fixture** — identical `community_members` DDL to Task 11 Step 4. Seed rows per this file's own `!sr set youtube-labels`/`!sr pause`/`!sr resume` permission-check test scenarios.

- [ ] **Step 5: Run tests, gate, coverage**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_social_music_process.py -v 2>&1 | tail -30`
Run: `env -C core/svc_process python3 -m pytest --cov=bundles.social_music_process --cov-fail-under=90 tests/test_bundles_social_music_process.py`

- [ ] **Step 6: Commit**

```bash
git add core/svc_process/bundles/social_music_process.py \
        core/svc_process/tests/test_bundles_social_music_process.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate social_music_process to penguin-dal

_caller_is_moderator_or_admin's two role-lookup raw SQL calls (guarding
!sr set youtube-labels / !sr pause / !sr resume) move to raw_sql_rows()
with named :param placeholders -- identical translation to
community_loyalty_process's own copy of this helper. D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 14: Migrate `social_shoutout_process.py`

**Files:**
- Modify: `core/svc_process/bundles/social_shoutout_process.py`
- Modify: `core/svc_process/tests/test_bundles_social_shoutout_process.py`

**Interfaces:** Consumes `raw_sql_rows` (Task 3). Depends on: Task 4.

- [ ] **Step 1: Imports** — add `from flask_core.bundle_runtime import raw_sql_rows`.

- [ ] **Step 2: Replace the three SQL constants**

Old:
```python
_SHOUTOUT_CONFIG_SQL = (
    "SELECT so_permission, vso_permission FROM shoutout_config WHERE community_id = $1 LIMIT 1"
)
_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = $1 AND platform = $2 AND platform_user_id = $3 LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members WHERE community_id = $1 AND display_name = $2 LIMIT 1"
)
```

New:
```python
_SHOUTOUT_CONFIG_SQL = (
    "SELECT so_permission, vso_permission FROM shoutout_config "
    "WHERE community_id = :community_id LIMIT 1"
)
_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND platform = :platform "
    "AND platform_user_id = :platform_user_id LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = :community_id AND display_name = :display_name LIMIT 1"
)
```

- [ ] **Step 3: Replace `_shoutout_permission`'s DB call**

Old:
```python
    try:
        dal = get_bundle_dal()
        rows = await dal.execute(_SHOUTOUT_CONFIG_SQL, [community_id])
    except Exception as exc:  # noqa: BLE001 -- must never block a shoutout, only degrade
        logger.debug("social_shoutout_process.permission_lookup_failed error=%s", exc)
        return _DEFAULT_PERMISSION
```

New:
```python
    try:
        dal = get_bundle_dal()
        rows = await raw_sql_rows(dal, _SHOUTOUT_CONFIG_SQL, {"community_id": community_id})
    except Exception as exc:  # noqa: BLE001 -- must never block a shoutout, only degrade
        logger.debug("social_shoutout_process.permission_lookup_failed error=%s", exc)
        return _DEFAULT_PERMISSION
```

(`value = rows[0][column]` below, where `column` is dynamically `"so_permission"`/`"vso_permission"`, is unchanged — `Row.__getitem__` supports dynamic-key dict-style access identically to the old `dict` row.)

- [ ] **Step 4: Replace `_caller_role`'s body** (this bundle's own copy of the shared permission-lookup pattern — same shape as Task 11's `_caller_is_moderator_or_admin`, but returns the raw role string instead of a boolean):

Old:
```python
    dal = get_bundle_dal()
    raw_author_id = event.payload.get("author_id")
    platform_user_id = raw_author_id if isinstance(raw_author_id, str) else None

    try:
        if platform_user_id:
            rows = await dal.execute(
                _ROLE_BY_PLATFORM_SQL, [community_id, event.platform, platform_user_id]
            )
            if rows:
                return str(rows[0]["role"]).lower()
        if event.actor:
            rows = await dal.execute(_ROLE_BY_DISPLAY_NAME_SQL, [community_id, event.actor])
            if rows:
                return str(rows[0]["role"]).lower()
```

New:
```python
    dal = get_bundle_dal()
    raw_author_id = event.payload.get("author_id")
    platform_user_id = raw_author_id if isinstance(raw_author_id, str) else None

    try:
        if platform_user_id:
            rows = await raw_sql_rows(
                dal,
                _ROLE_BY_PLATFORM_SQL,
                {
                    "community_id": community_id,
                    "platform": event.platform,
                    "platform_user_id": platform_user_id,
                },
            )
            if rows:
                return str(rows[0]["role"]).lower()
        if event.actor:
            rows = await raw_sql_rows(
                dal, _ROLE_BY_DISPLAY_NAME_SQL, {"community_id": community_id, "display_name": event.actor}
            )
            if rows:
                return str(rows[0]["role"]).lower()
```

- [ ] **Step 5: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with `shoutout_config` + `community_members`."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE shoutout_config ("
                "community_id INTEGER, so_permission TEXT, vso_permission TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE community_members ("
                "community_id INTEGER, platform TEXT, platform_user_id TEXT, "
                "display_name TEXT, role TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed rows per existing test scenario (config row present/absent → default `"mod"`, role lookup by platform id/display name/miss). Note: `shoutout_config` is a **separate table binding local to svc_process** — `twitch_shoutout_action.py` (Task 19) binds a table of the same name on **svc_action's own, independently-connected** `AsyncDB`; both simply reference the one real Postgres table via each service's own `reflect()` call, exactly as today's two independent `_ensure_shoutout_tables()` pydal stubs did.

- [ ] **Step 6: Run tests, gate, coverage**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_social_shoutout_process.py -v 2>&1 | tail -30`
Run: `env -C core/svc_process python3 -m pytest --cov=bundles.social_shoutout_process --cov-fail-under=90 tests/test_bundles_social_shoutout_process.py`

- [ ] **Step 7: Commit**

```bash
git add core/svc_process/bundles/social_shoutout_process.py \
        core/svc_process/tests/test_bundles_social_shoutout_process.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate social_shoutout_process to penguin-dal

_shoutout_permission's dynamic-column config lookup and _caller_role's
role lookup move to raw_sql_rows() with named :param placeholders. D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 15: Migrate `community_announcements_process.py` (Pattern A — deletes its own `_ensure_announcements_table` stub)

**Files:**
- Modify: `core/svc_process/bundles/community_announcements_process.py`
- Modify: `core/svc_process/tests/test_bundles_community_announcements_process.py`

**Interfaces:** Consumes `penguin_dal.AsyncDB`'s native `Query` builder (Pattern A). Depends on: Task 4 (`await dal.reflect()` already discovers the real `announcements` table — this bundle's own pydal-stub definition is now redundant).

- [ ] **Step 1: Delete `_ensure_announcements_table` entirely**

Old (whole function, including its docstring and the `if "announcements" not in dal.tables:` guard):
```python
def _ensure_announcements_table(dal) -> None:
    """..."""
    if "announcements" not in dal.tables:
        dal.define_table(
            "announcements",
            dal.Field("community_id", "reference communities", notnull=True),
            dal.Field("title", "string", notnull=True),
            dal.Field("content", "text", notnull=True),
            dal.Field("announcement_type", "string", default="general"),
            dal.Field("status", "string", default="published"),
            dal.Field("broadcasted_platforms", "json", default=[]),
            migrate=False,
        )
```
Delete this function. `announcements` is a real table (owned by `config/postgres/migrations/090_community_announcements_bundle.sql` or wherever it was created) — `svc_process`'s startup `await dal.reflect()` (Task 4) already exposes its real columns as `dal.announcements.*`.

- [ ] **Step 2: Remove the call site and translate the query**

Old:
```python
    dal = get_bundle_dal()
    _ensure_announcements_table(dal)
    ctx = get_bundle_context()

    try:
        if ctx.community is None:
            return None  # Tenant-wide activation cannot broadcast announcements

        community_id = int(ctx.community)
        query = (dal.announcements.id == announcement_id) & (
            dal.announcements.community_id == community_id
        )
        # select_async runs query.select()/query.db.commit() directly and
        # requires a pydal Set (dal.dal(query)) -- `dal` (the AsyncDAL
        # wrapper) is not itself callable, unlike the raw pydal DAL.
        rows = await dal.select_async(dal.dal(query))
        row = rows.first() if rows else None
```

New:
```python
    dal = get_bundle_dal()
    ctx = get_bundle_context()

    try:
        if ctx.community is None:
            return None  # Tenant-wide activation cannot broadcast announcements

        community_id = int(ctx.community)
        query = (dal.announcements.id == announcement_id) & (
            dal.announcements.community_id == community_id
        )
        rows = await dal(query).select()
        row = rows.first() if rows else None
```

Everything after (`hasattr(row, "broadcasted_platforms")`, `row.broadcasted_platforms`) is unchanged — `penguin_dal.Row.__getattr__` supports the same attribute access as the old pydal row.

- [ ] **Step 3: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with the real `announcements` table's columns."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE announcements ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, title TEXT, "
                "content TEXT, announcement_type TEXT, status TEXT, broadcasted_platforms TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed an `announcements` row (id, community_id, title, etc.) per existing test scenario (found/not-found/wrong-community IDOR case), matching Task 5 Step 4's pattern. SQLite has no native array/JSON type — store `broadcasted_platforms` as a JSON-encoded string and adjust the test's assertion to `json.loads(row.broadcasted_platforms)` if the existing test round-trips a list through it; the bundle's own production code path (Postgres, real JSON column) is unaffected.

- [ ] **Step 4: Run tests, gate, coverage**

Run: `env -C core/svc_process python3 -m pytest tests/test_bundles_community_announcements_process.py -v 2>&1 | tail -30`
Run: `env -C core/svc_process python3 -m pytest --cov=bundles.community_announcements_process --cov-fail-under=90 tests/test_bundles_community_announcements_process.py`

- [ ] **Step 5: Commit**

```bash
git add core/svc_process/bundles/community_announcements_process.py \
        core/svc_process/tests/test_bundles_community_announcements_process.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate community_announcements_process to penguin-dal

Deletes _ensure_announcements_table's pydal-stub define_table() -- dead
since svc_process's own dal.reflect() (Task 4) already discovers the real
announcements table. The one select_async(dal.dal(query)) call becomes
penguin-dal's native dal(query).select(). D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 16: Migrate `community_announcements_action.py` (Pattern A — deletes its own `_ensure_announcement_tables` stub)

**Files:**
- Modify: `core/svc_action/bundles/community_announcements_action.py`
- Modify: `core/svc_action/tests/test_bundles_community_announcements_action.py`

**Interfaces:** Consumes `penguin_dal.AsyncDB`'s native `Query`/`TableProxy` builder. Depends on: Task 4.

- [ ] **Step 1: Delete `_ensure_announcement_tables` entirely** (the whole function defining `community_servers`/`announcement_broadcasts` via `dal.define_table`/`dal.Field`) — both are real tables; `svc_action`'s own `await dal.reflect()` (Task 4) already exposes them.

- [ ] **Step 2: Remove the call site and translate the select + insert**

Old:
```python
    dal = get_bundle_dal()
    _ensure_announcement_tables(dal)
    ...
    try:
        query = (
            (dal.community_servers.community_id == int(community_id))
            & (dal.community_servers.platform.belongs(target_platforms))
        )
        # select_async runs query.select()/query.db.commit() directly --
        # requires a pydal Set (dal.dal(query)), not a bare Query.
        servers = await dal.select_async(dal.dal(query))
```

New:
```python
    dal = get_bundle_dal()
    ...
    try:
        query = (
            (dal.community_servers.community_id == int(community_id))
            & (dal.community_servers.platform.belongs(target_platforms))
        )
        servers = await dal(query).select()
```

Old insert:
```python
            try:
                await dal.insert_async(
                    dal.announcement_broadcasts,
                    announcement_id=announcement_id,
                    community_server_id=server.id,
                    platform=platform,
                    status="sent" if success else "failed",
                    error_message=error,
                    broadcasted_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                )
```

New:
```python
            try:
                await dal.announcement_broadcasts.async_insert(
                    announcement_id=announcement_id,
                    community_server_id=server.id,
                    platform=platform,
                    status="sent" if success else "failed",
                    error_message=error,
                    broadcasted_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                )
```

- [ ] **Step 3: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with `community_servers` + `announcement_broadcasts`."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text("CREATE TABLE community_servers (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "community_id INTEGER, platform TEXT)")
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE announcement_broadcasts ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, announcement_id INTEGER, "
                "community_server_id INTEGER, platform TEXT, status TEXT, error_message TEXT, "
                "broadcasted_at TEXT, created_at TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed `community_servers` rows for the fan-out scenarios (multiple platforms, no matching servers → `NonRetryableTransportError`), and assert an `announcement_broadcasts` row lands per server after a successful/failed POST, per Task 5 Step 4's pattern.

- [ ] **Step 4: Run tests, gate, coverage**

Run: `env -C core/svc_action python3 -m pytest tests/test_bundles_community_announcements_action.py -v 2>&1 | tail -30`
Run: `env -C core/svc_action python3 -m pytest --cov=bundles.community_announcements_action --cov-fail-under=90 tests/test_bundles_community_announcements_action.py`

- [ ] **Step 5: Commit**

```bash
git add core/svc_action/bundles/community_announcements_action.py \
        core/svc_action/tests/test_bundles_community_announcements_action.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate community_announcements_action to penguin-dal

Deletes _ensure_announcement_tables's pydal-stub define_table() calls --
dead since svc_action's own dal.reflect() (Task 4) already discovers
community_servers/announcement_broadcasts. select_async(dal.dal(query))
becomes dal(query).select(); insert_async(table, **fields) becomes
table.async_insert(**fields). D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 17: Migrate `community_forums_action.py` (Pattern A — deletes its own `_ensure_forum_tables` stub)

**Files:**
- Modify: `core/svc_action/bundles/community_forums_action.py`
- Modify: `core/svc_action/tests/test_bundles_community_forums_action.py`

**Interfaces:** Consumes `penguin_dal.AsyncDB`'s native `Query`/`TableProxy` builder. Depends on: Task 4.

- [ ] **Step 1: Delete `_ensure_forum_tables` entirely** (defines `hub_channels`/`hub_forum_posts`/`hub_forum_replies` via `dal.define_table`/`dal.Field`) — all three are real tables, discovered by `svc_action`'s own `await dal.reflect()` (Task 4).

- [ ] **Step 2: `create_forum_post` — remove the call site, translate the select + insert**

Old:
```python
    dal = get_bundle_dal()
    _ensure_forum_tables(dal)
    try:
        channel = None
        if channel_id_int is not None:
            channel_query = dal.hub_channels.id == channel_id_int
            # select_async requires a pydal Set (dal.dal(query)), not a
            # bare Query -- see module docstring.
            channels = await dal.select_async(dal.dal(channel_query))
            channel = channels[0] if channels else None
            if not channel:
                raise NonRetryableTransportError(f"channel {channel_id_int} not found")

        post_id = await dal.insert_async(
            dal.hub_forum_posts,
            hub_channel_id=channel_id_int,
            community_id=envelope.community,
            title=title,
            body=body,
            tags=payload.get("tags") or [],
            author_hub_user_id=payload.get("author_id"),
            author_platform="hub",
            author_username=payload.get("author") or "anonymous",
            author_avatar_url=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
```

New:
```python
    dal = get_bundle_dal()
    try:
        channel = None
        if channel_id_int is not None:
            channels = await dal(dal.hub_channels.id == channel_id_int).select()
            channel = channels[0] if channels else None
            if not channel:
                raise NonRetryableTransportError(f"channel {channel_id_int} not found")

        post_id = await dal.hub_forum_posts.async_insert(
            hub_channel_id=channel_id_int,
            community_id=envelope.community,
            title=title,
            body=body,
            tags=payload.get("tags") or [],
            author_hub_user_id=payload.get("author_id"),
            author_platform="hub",
            author_username=payload.get("author") or "anonymous",
            author_avatar_url=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
```

- [ ] **Step 3: `create_forum_reply` — remove the call site, translate all four DB calls**

Old:
```python
    dal = get_bundle_dal()
    _ensure_forum_tables(dal)
    try:
        post_query = dal.hub_forum_posts.id == post_id
        # select_async requires a pydal Set (dal.dal(query)), not a bare
        # Query -- see module docstring.
        posts = await dal.select_async(dal.dal(post_query))
        post = posts[0] if posts else None
        if not post:
            raise NonRetryableTransportError(f"post {post_id} not found")
        if post.is_locked:
            raise NonRetryableTransportError(f"post {post_id} is locked")

        reply_id = await dal.insert_async(
            dal.hub_forum_replies,
            post_id=post_id,
            author_hub_user_id=payload.get("author_id"),
            author_platform="hub",
            author_username=payload.get("author") or "anonymous",
            author_avatar_url=None,
            content=content,
            created_at=datetime.now(UTC),
        )

        # Update post's reply counter and last_reply_at
        update_query = dal.hub_forum_posts.id == post_id
        await dal.update_async(
            update_query,
            reply_count=post.reply_count + 1,
            last_reply_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        # Relay to bridged channels if configured
        channel_query = dal.hub_channels.id == post.hub_channel_id
        # select_async requires a pydal Set (dal.dal(query)), not a bare
        # Query -- see module docstring.
        channels = await dal.select_async(dal.dal(channel_query))
        channel = channels[0] if channels else None
```

New:
```python
    dal = get_bundle_dal()
    try:
        posts = await dal(dal.hub_forum_posts.id == post_id).select()
        post = posts[0] if posts else None
        if not post:
            raise NonRetryableTransportError(f"post {post_id} not found")
        if post.is_locked:
            raise NonRetryableTransportError(f"post {post_id} is locked")

        reply_id = await dal.hub_forum_replies.async_insert(
            post_id=post_id,
            author_hub_user_id=payload.get("author_id"),
            author_platform="hub",
            author_username=payload.get("author") or "anonymous",
            author_avatar_url=None,
            content=content,
            created_at=datetime.now(UTC),
        )

        await dal(dal.hub_forum_posts.id == post_id).update(
            reply_count=post.reply_count + 1,
            last_reply_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        channels = await dal(dal.hub_channels.id == post.hub_channel_id).select()
        channel = channels[0] if channels else None
```

- [ ] **Step 4: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with the three real forum tables."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE hub_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "community_id INTEGER, community_server_channel_id INTEGER)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE hub_forum_posts ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, hub_channel_id INTEGER, community_id INTEGER, "
                "title TEXT, body TEXT, tags TEXT, author_hub_user_id INTEGER, author_platform TEXT, "
                "author_username TEXT, author_avatar_url TEXT, is_locked BOOLEAN DEFAULT 0, "
                "reply_count INTEGER DEFAULT 0, last_reply_at TEXT, created_at TEXT, updated_at TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE hub_forum_replies ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER, author_hub_user_id INTEGER, "
                "author_platform TEXT, author_username TEXT, author_avatar_url TEXT, "
                "content TEXT, created_at TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed `hub_channels`/`hub_forum_posts` rows per existing scenario (post creation with/without a configured channel, reply on a found/locked/missing post, reply-count increment assertion via a follow-up read), per Task 5 Step 4's pattern.

- [ ] **Step 5: Run tests, gate, coverage**

Run: `env -C core/svc_action python3 -m pytest tests/test_bundles_community_forums_action.py -v 2>&1 | tail -40`
Run: `env -C core/svc_action python3 -m pytest --cov=bundles.community_forums_action --cov-fail-under=90 tests/test_bundles_community_forums_action.py`

- [ ] **Step 6: Commit**

```bash
git add core/svc_action/bundles/community_forums_action.py \
        core/svc_action/tests/test_bundles_community_forums_action.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate community_forums_action to penguin-dal

Deletes _ensure_forum_tables's pydal-stub define_table() calls -- dead
since svc_action's own dal.reflect() (Task 4) already discovers
hub_channels/hub_forum_posts/hub_forum_replies. All four DB calls
(2 selects, 1 insert -> also a 3rd select+2nd insert+1 update in
create_forum_reply) move to penguin-dal's native Query/TableProxy
builder. D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 18: Migrate `streaming_stream_action.py` (Pattern B — the two-table JOIN queries)

**Files:**
- Modify: `core/svc_action/bundles/streaming_stream_action.py`
- Modify: `core/svc_action/tests/test_bundles_streaming_stream_action.py`

**Interfaces:** Consumes `raw_sql_rows` (Task 3) — this bundle's `coordination JOIN community_servers` filter cannot be expressed by penguin-dal's single-table `Query` builder (see the Pattern A/B table above). Depends on: Task 4.

- [ ] **Step 1: Imports** — replace `from flask_core import StageEnvelope, get_bundle_context, get_bundle_dal` + the `TYPE_CHECKING`-guarded `from flask_core import AsyncDAL` with:
```python
from flask_core import StageEnvelope, get_bundle_context, get_bundle_dal
from flask_core.bundle_runtime import raw_sql_rows

if TYPE_CHECKING:
    from penguin_dal import AsyncDB
```
(delete the `if TYPE_CHECKING: from flask_core import AsyncDAL` block).

- [ ] **Step 2: Delete `_ensure_streaming_tables` and `_build_join_query` entirely** — `community_servers`/`coordination` are real tables (`svc_action`'s `await dal.reflect()`, Task 4, already exposes them), and the join filter moves into raw SQL text (Step 3) since a two-table filter cannot be built with penguin-dal's single-table `Query`.

- [ ] **Step 3: Add the shared join-filter SQL fragment and replace all three query functions**

New shared constant (placed near `_LIVE_PLATFORM`):
```python
_JOIN_WHERE_SQL = (
    "FROM coordination c "
    "JOIN community_servers cs "
    "ON cs.platform = c.platform AND cs.platform_server_id = c.server_id "
    "WHERE cs.community_id = :community_id AND cs.status = 'approved' "
    "AND c.platform = :live_platform AND c.is_live = TRUE"
)
```

Old:
```python
async def _get_live_streams(async_dal: AsyncDAL, community_id: int) -> list[LiveStreamDTO]:
    """Port of v2 `get_live_streams` -- all live streams ordered by viewer count DESC."""
    dal = async_dal.dal
    query = _build_join_query(dal, community_id)
    rows = await async_dal.select_async(
        dal(query),
        dal.coordination.ALL,
        orderby=~dal.coordination.viewer_count,
    )
    return [_stream_dto(row) for row in rows]


async def _get_featured_streams(async_dal: AsyncDAL, community_id: int) -> list[LiveStreamDTO]:
    """Port of v2 `get_featured_streams` -- top 5 live streams by viewer count."""
    dal = async_dal.dal
    query = _build_join_query(dal, community_id)
    rows = await async_dal.select_async(
        dal(query),
        dal.coordination.ALL,
        orderby=~dal.coordination.viewer_count,
        limitby=(0, 5),
    )
    return [_stream_dto(row) for row in rows]


async def _get_stream_details(
    async_dal: AsyncDAL, community_id: int, entity_id: str
) -> StreamDetailsDTO:
    """Port of v2 `get_stream_details` -- one stream by entity_id, raises 404 if not found."""
    dal = async_dal.dal
    query = _build_join_query(dal, community_id) & (dal.coordination.entity_id == entity_id)
    rows = await async_dal.select_async(dal(query), dal.coordination.ALL)
    if not rows:
        raise NonRetryableTransportError(
            f"streaming bundle: no live stream found for entity_id={entity_id!r} "
            f"in community_id={community_id}",
            http_status=404,
        )
    row = rows.first()
```

New:
```python
async def _get_live_streams(async_dal: "AsyncDB", community_id: int) -> list[LiveStreamDTO]:
    """Port of v2 `get_live_streams` -- all live streams ordered by viewer count DESC."""
    rows = await raw_sql_rows(
        async_dal,
        f"SELECT c.* {_JOIN_WHERE_SQL} ORDER BY c.viewer_count DESC",
        {"community_id": community_id, "live_platform": _LIVE_PLATFORM},
    )
    return [_stream_dto(row) for row in rows]


async def _get_featured_streams(async_dal: "AsyncDB", community_id: int) -> list[LiveStreamDTO]:
    """Port of v2 `get_featured_streams` -- top 5 live streams by viewer count."""
    rows = await raw_sql_rows(
        async_dal,
        f"SELECT c.* {_JOIN_WHERE_SQL} ORDER BY c.viewer_count DESC LIMIT 5",
        {"community_id": community_id, "live_platform": _LIVE_PLATFORM},
    )
    return [_stream_dto(row) for row in rows]


async def _get_stream_details(
    async_dal: "AsyncDB", community_id: int, entity_id: str
) -> StreamDetailsDTO:
    """Port of v2 `get_stream_details` -- one stream by entity_id, raises 404 if not found."""
    rows = await raw_sql_rows(
        async_dal,
        f"SELECT c.* {_JOIN_WHERE_SQL} AND c.entity_id = :entity_id",
        {"community_id": community_id, "live_platform": _LIVE_PLATFORM, "entity_id": entity_id},
    )
    if not rows:
        raise NonRetryableTransportError(
            f"streaming bundle: no live stream found for entity_id={entity_id!r} "
            f"in community_id={community_id}",
            http_status=404,
        )
    row = rows.first()
```

`_stream_dto(row)` (attribute access: `row.entity_id`, `row.channel_id`, …) is unchanged — `raw_sql_rows`'s `penguin_dal.Row` supports the same attribute access the old pydal row did. `list_streams()`'s own body only needs `async_dal = get_bundle_dal()` (drop the `_ensure_streaming_tables(async_dal)` call).

- [ ] **Step 4: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with `community_servers` + `coordination`."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE community_servers (community_id INTEGER, platform TEXT, "
                "platform_server_id TEXT, status TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE coordination ("
                "entity_id TEXT, platform TEXT, server_id TEXT, channel_id TEXT, "
                "channel_name TEXT, is_live BOOLEAN, viewer_count INTEGER, live_since TEXT, "
                "stream_title TEXT, game_name TEXT, thumbnail_url TEXT, last_updated TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed matching `community_servers`/`coordination` row pairs (same `platform`/`platform_server_id`↔`server_id`) per existing test scenario (live streams list, featured top-5 ordering, stream-details found/404, a non-Twitch or non-approved-server row that must be filtered out), per Task 5 Step 4's pattern.

- [ ] **Step 5: Run tests, gate, coverage**

Run: `env -C core/svc_action python3 -m pytest tests/test_bundles_streaming_stream_action.py -v 2>&1 | tail -40`
Run: `env -C core/svc_action python3 -m pytest --cov=bundles.streaming_stream_action --cov-fail-under=90 tests/test_bundles_streaming_stream_action.py`

- [ ] **Step 6: Commit**

```bash
git add core/svc_action/bundles/streaming_stream_action.py \
        core/svc_action/tests/test_bundles_streaming_stream_action.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate streaming_stream_action to penguin-dal

The coordination JOIN community_servers filter (get_live_streams/
get_featured_streams/get_stream_details) can't be expressed by
penguin-dal's single-table Query builder -- moves to raw_sql_rows() with
an explicit SQL JOIN, named :param placeholders, preserving the exact
filter/order/limit semantics. Deletes the now-dead _ensure_streaming_tables
pydal stub (svc_action's dal.reflect(), Task 4, already discovers both
tables). D21a.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 19: Migrate `twitch_shoutout_action.py` (Pattern A — deletes its own `_ensure_shoutout_tables` stub)

**Files:**
- Modify: `core/svc_action/bundles/twitch_shoutout_action.py`
- Modify: `core/svc_action/tests/test_bundles_twitch_shoutout_action.py`

**Interfaces:** Consumes `penguin_dal.AsyncDB`'s native `Query`/`TableProxy` builder. Depends on: Task 4.

- [ ] **Step 1: Delete `_ensure_shoutout_tables` entirely** (defines `shoutout_config`/`shoutout_history` via `dal.define_table`/`dal.Field`) — both are real tables (owned by migration 046), discovered by `svc_action`'s own `await dal.reflect()` (Task 4). Delete its one call site, `_ensure_shoutout_tables(dal)`.

- [ ] **Step 2: Replace `_get_shoutout_config_row`**

Old:
```python
async def _get_shoutout_config_row(dal: Any, community_id: int) -> Any | None:
    """...
    `select_async` expects a pydal `Set` (`dal.dal(query)`), not a bare
    `Query` -- ...
    """
    query = dal.dal.shoutout_config.community_id == community_id
    rows = await dal.select_async(dal.dal(query), limitby=(0, 1))
    return rows[0] if rows else None
```

New:
```python
async def _get_shoutout_config_row(dal: Any, community_id: int) -> Any | None:
    """The community's `shoutout_config` row, or `None` if it has never been provisioned."""
    rows = await dal(dal.shoutout_config.community_id == community_id).select(limitby=(0, 1))
    return rows[0] if rows else None
```

- [ ] **Step 3: Replace `_last_shoutout_at`**

Old:
```python
    query = (
        (dal.dal.shoutout_history.community_id == community_id)
        & (dal.dal.shoutout_history.platform == platform)
        & (dal.dal.shoutout_history.target_username == target_login)
    )
    rows = await dal.select_async(
        dal.dal(query), orderby=~dal.dal.shoutout_history.created_at, limitby=(0, 1)
    )
```

New:
```python
    query = (
        (dal.shoutout_history.community_id == community_id)
        & (dal.shoutout_history.platform == platform)
        & (dal.shoutout_history.target_username == target_login)
    )
    rows = await dal(query).select(orderby=~dal.shoutout_history.created_at, limitby=(0, 1))
```

- [ ] **Step 4: Replace `_record_history`**

Old:
```python
    await dal.insert_async(
        dal.shoutout_history,
        community_id=community_id,
        platform=platform,
        target_username=target_login,
        shoutout_type=kind,
        triggered_by_username=triggered_by,
        trigger_type="manual",
    )
```

New:
```python
    await dal.shoutout_history.async_insert(
        community_id=community_id,
        platform=platform,
        target_username=target_login,
        shoutout_type=kind,
        triggered_by_username=triggered_by,
        trigger_type="manual",
    )
```

- [ ] **Step 5: Rewrite the test fixture**

```python
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with `shoutout_config` + `shoutout_history`."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE shoutout_config (community_id INTEGER, cooldown_minutes INTEGER)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE shoutout_history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, platform TEXT, "
                "target_username TEXT, shoutout_type TEXT, triggered_by_username TEXT, "
                "trigger_type TEXT, created_at TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()
```

Seed `shoutout_config`/`shoutout_history` rows per existing test scenario (default cooldown vs. configured, cooldown-active vs. expired, history write assertion via a follow-up read), per Task 5 Step 4's pattern.

- [ ] **Step 6: Run tests, gate, coverage**

Run: `env -C core/svc_action python3 -m pytest tests/test_bundles_twitch_shoutout_action.py -v 2>&1 | tail -40`
Run: `env -C core/svc_action python3 -m pytest --cov=bundles.twitch_shoutout_action --cov-fail-under=90 tests/test_bundles_twitch_shoutout_action.py`

- [ ] **Step 7: Run the full import gate + both services' complete suites — every bundle in Tasks 5–19 is now migrated**

Run: `bash scripts/ci/check_bundle_dal_imports.sh`
Expected: `check_bundle_dal_imports: files_scanned=<N> legacy_hits=0`.

Run: `grep -rln "flask_core.database\|AsyncDAL\|get_bundle_dal" core/svc_process/bundles/*.py core/svc_action/bundles/*.py | xargs grep -l "AsyncDAL\b" || echo "NONE — all 16 migrated"`
Expected: `NONE — all 16 migrated` (no bundle file references the `AsyncDAL` type name anymore; `get_bundle_dal()`/`get_bundle_context()` calls remain, which is correct — the facade name didn't change, Task 3).

Run: `env -C core/svc_process python3 -m pytest -v 2>&1 | tail -10` and `env -C core/svc_action python3 -m pytest -v 2>&1 | tail -10`
Expected: both fully green.

- [ ] **Step 8: Commit**

```bash
git add core/svc_action/bundles/twitch_shoutout_action.py \
        core/svc_action/tests/test_bundles_twitch_shoutout_action.py
git commit -m "$(cat <<'EOF'
refactor(bundles): migrate twitch_shoutout_action to penguin-dal

Deletes _ensure_shoutout_tables's pydal-stub define_table() calls -- dead
since svc_action's own dal.reflect() (Task 4) already discovers
shoutout_config/shoutout_history. All three DB calls (cooldown config
read, history read with orderby+limitby, history insert) move to
penguin-dal's native Query/TableProxy builder. D21a -- this is the 16th
and final bundle; the DAL migration is complete.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 20: Add `consumes` v2 to every process bundle's `app_catalog` registration

**Files:**
- Create: `alembic/versions/0020_bundle_consumes_v2.py`
- Create: `alembic/tests/test_0020_bundle_consumes_v2.py`

**Interfaces:** No code dependency on Tasks 1–19 — pure SQL/manifest work, runs in parallel with the DAL migration tasks. Produces: `app_catalog.stages.process.consumes` (bundle.yaml v2 shape, §6.4.3) on all 16 on-disk process bundles' registrations, either via `jsonb_set` on an existing row (13 bundles) or a brand-new row (3 bundles that shipped code with no `app_catalog` row at all: `community_reputation_process`, `community_context_process`, `inventory_process`).

**Verified inventory (16 process-bundle files on disk = 16 migrated):**

| Bundle file | `app_id`(s) | Row exists today? |
|---|---|---|
| `bot_process.py` | `waddles.bot.discord.default`, `waddles.bot.twitch.default` | yes (2 rows, one file) |
| `community_announcements_process.py` | `waddles.community.announcements.default` | yes |
| `community_chat_process.py` | `waddles.community.chat.default` | yes |
| `community_context_process.py` | `waddles.community.context.default` | **no — new row** |
| `community_forums_process.py` | `waddles.community.forums.default` | yes |
| `community_loyalty_process.py` | `waddles.community.loyalty.default` | yes |
| `community_polls_process.py` | `waddles.community.polls.default` | yes |
| `community_reputation_process.py` | `waddles.community.reputation.default` | **no — new row** |
| `echo_process.py` | `waddles.core.demo.echo` | yes |
| `inventory_process.py` | `waddles.community.inventory.default` | **no — new row** |
| `marketing_engagement_process.py` | `waddles.marketing.engagement.default` | yes |
| `social_alias_process.py` | `waddles.social.alias.default` | yes |
| `social_music_process.py` | `waddles.social.music.default` | yes |
| `social_quote_process.py` | `waddles.social.quote.default` | yes |
| `social_shoutout_process.py` | `waddles.bot.shoutout.default` | yes |
| `social_welcome_process.py` | `waddles.social.welcome.default` | yes |

The three new-row app_ids/modules/features follow the exact naming convention `0016_moderation_enforce_app`/`0017_loyalty_shoutout_apps`/`0019_kick_app` already establish for "code shipped, no catalog row" gaps.

- [ ] **Step 1: Write `alembic/versions/0020_bundle_consumes_v2.py`**

```python
cat > alembic/versions/0020_bundle_consumes_v2.py <<'MIGRATION_EOF'
"""Add v2 `consumes` metadata to every process-stage bundle (M1.5, D24 prep).

Declarative-only: adds `stages.process.consumes` (bundle.yaml v2 shape,
spec Sec6.4.3) to each process bundle's existing `app_catalog` row via
`jsonb_set`, and registers three process bundles that shipped code but no
`app_catalog` row at all (community_reputation_process,
community_context_process, inventory_process -- each dispatched today only
via bot_process.py's in-process `_FEATURE_MODULES` router, the same
"routability gap" precedent 0016/0017/0019 close for other app_ids).

Changes no runtime behavior: `core/svc_ingest/fanout.py`'s
`resolve_consuming_apps` and every stage runner keep reading
`stages.<x>.consumes` exactly as before (a flat list of legacy tags on the
INGEST stage) -- this migration writes an ADDITIONAL, unread-by-current-code
`consumes` key on the PROCESS stage for the future Rust ingest fan-out
(M4/M5) to read. Idempotent: `jsonb_set` unconditionally overwrites the key
on every re-run; the three new rows use `ON CONFLICT ... DO UPDATE`.

Command-word source: `core/svc_process/bundles/bot_process.py`'s own
`_FEATURE_MODULES` dict (the single source of truth for which `!word`
routes to which sibling bundle) plus each bundle's own module docstring for
non-command (event-type-driven) bundles. Platform reach: today's
`bot_process.py` is registered ONLY against `waddles.bot.discord.default`/
`waddles.bot.twitch.default` (migrations 083/084) -- every feature bundle
reached only via `_dispatch_feature`'s in-process call is therefore
reachable on Discord+Twitch only today, which is what these `consumes`
rules declare. Legacy-tag translation per the M1.5 mapping table:
`discord.message` -> {platform: discord, event_types: [chat.message]};
`twitch.message` (IRC) -> {platform: twitch, event_types: [chat.message]}.

Revision ID: 0020_bundle_consumes_v2
Revises: 0019_kick_app
Create Date: 2026-09-14
"""

from alembic import op

revision = "0020_bundle_consumes_v2"
down_revision = "0019_kick_app"
branch_labels = None
depends_on = None

TENANT_SLUG = "global"

# app_id -> `stages.process.consumes` JSON array (bundle.yaml v2 shape).
_CONSUMES_BY_APP_ID: dict[str, str] = {
    "waddles.bot.discord.default": (
        '[{"platform": "discord", "event_types": ["chat.message"]}]'
    ),
    "waddles.bot.twitch.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"]}]'
    ),
    "waddles.community.announcements.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!announce"]}}, '
        '{"platform": "discord", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!announce"]}}]'
    ),
    "waddles.community.chat.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!chat-history", "!channels"]}}, '
        '{"platform": "discord", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!chat-history", "!channels"]}}]'
    ),
    "waddles.community.forums.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!forum"]}}, '
        '{"platform": "discord", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!forum"]}}]'
    ),
    "waddles.community.loyalty.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!points", "!top", "!shop", "!redeem"]}}, '
        '{"platform": "discord", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!points", "!top", "!shop", "!redeem"]}}]'
    ),
    "waddles.community.polls.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!poll"]}}, '
        '{"platform": "discord", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!poll"]}}]'
    ),
    "waddles.social.alias.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!alias", "!unalias"]}}, '
        '{"platform": "discord", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!alias", "!unalias"]}}]'
    ),
    "waddles.social.music.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!sr", "!songrequest", "!sq", "!songqueue"]}}, '
        '{"platform": "discord", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!sr", "!songrequest", "!sq", "!songqueue"]}}]'
    ),
    "waddles.social.quote.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!quote"]}}, '
        '{"platform": "discord", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!quote"]}}]'
    ),
    "waddles.bot.shoutout.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!so", "!shoutout", "!vso"]}}, '
        '{"platform": "discord", "event_types": ["chat.message"], '
        '"filters": {"command_prefix": ["!so", "!shoutout", "!vso"]}}]'
    ),
    "waddles.marketing.engagement.default": (
        '[{"platform": "waddles", "event_types": ["poll_create", "poll_vote", '
        '"form_submit", "engagement"]}]'
    ),
    "waddles.social.welcome.default": (
        '[{"platform": "twitch", "event_types": ["chat.message"]}, '
        '{"platform": "discord", "event_types": ["chat.message"]}]'
    ),
    # The echo demo bundle "consumed everything" -- per the M1.5 mapping
    # table, five explicit per-platform rules, never platform: "*", so this
    # migration does not require allow_wildcard_consumes anywhere.
    "waddles.core.demo.echo": (
        '[{"platform": "twitch", "event_types": ["chat.message"]}, '
        '{"platform": "discord", "event_types": ["chat.message"]}, '
        '{"platform": "slack", "event_types": ["chat.message"]}, '
        '{"platform": "youtube", "event_types": ["chat.message"]}, '
        '{"platform": "kick", "event_types": ["chat.message"]}]'
    ),
}

# Three process bundles that ship real, tested code
# (core/svc_process/bundles/{community_reputation_process,
# community_context_process,inventory_process}.py) but never got an
# app_catalog row -- dispatched today only via bot_process.py's in-process
# _FEATURE_MODULES router. Same "close the routability gap" precedent
# 0016/0017/0019 each establish for other app_ids.
_NEW_APP_ROWS: tuple[dict[str, str], ...] = (
    {
        "app_id": "waddles.community.reputation.default",
        "module": "community",
        "feature": "waddles.community.reputation",
        "entrypoint": "bundles.community_reputation_process:transform",
        "consumes": (
            '[{"platform": "twitch", "event_types": ["chat.message"], '
            '"filters": {"command_prefix": ["!reputation", "!rep"]}}, '
            '{"platform": "discord", "event_types": ["chat.message"], '
            '"filters": {"command_prefix": ["!reputation", "!rep"]}}]'
        ),
    },
    {
        "app_id": "waddles.community.context.default",
        "module": "community",
        "feature": "waddles.community.context",
        "entrypoint": "bundles.community_context_process:transform",
        "consumes": (
            '[{"platform": "twitch", "event_types": ["chat.message"], '
            '"filters": {"command_prefix": ["!cc"]}}, '
            '{"platform": "discord", "event_types": ["chat.message"], '
            '"filters": {"command_prefix": ["!cc"]}}]'
        ),
    },
    {
        "app_id": "waddles.community.inventory.default",
        "module": "community",
        "feature": "waddles.community.inventory",
        "entrypoint": "bundles.inventory_process:transform",
        "consumes": (
            '[{"platform": "twitch", "event_types": ["chat.message"], '
            '"filters": {"command_prefix": ["!inventory"]}}, '
            '{"platform": "discord", "event_types": ["chat.message"], '
            '"filters": {"command_prefix": ["!inventory"]}}]'
        ),
    },
)


def upgrade() -> None:
    for app_id, consumes_json in _CONSUMES_BY_APP_ID.items():
        op.execute(
            f"""
            UPDATE app_catalog
            SET stages = jsonb_set(
                stages, '{{process,consumes}}', '{consumes_json}'::jsonb, true
            )
            WHERE app_id = '{app_id}' AND stages ? 'process'
            """
        )

    for row in _NEW_APP_ROWS:
        op.execute(
            f"""
            INSERT INTO app_catalog (
                app_id, manifest_version, module, feature, provider,
                execution_model, is_default, platform_compatibility,
                status, stages
            ) VALUES (
                '{row["app_id"]}',
                '1.0.0',
                '{row["module"]}',
                '{row["feature"]}',
                'builtin',
                'native',
                FALSE,
                '{{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}}'::jsonb,
                'active',
                (
                    '{{"process": {{"entrypoint": "{row["entrypoint"]}", ' ||
                    '"config": {{}}, "spec": {{"required_config": []}}, ' ||
                    '"consumes": {row["consumes"]}}}}}'
                )::jsonb
            )
            ON CONFLICT (app_id) DO UPDATE SET
                stages = jsonb_set(
                    app_catalog.stages, '{{process,consumes}}', '{row["consumes"]}'::jsonb, true
                )
            """
        )
        op.execute(
            f"""
            INSERT INTO app_tenant_availability (tenant_id, app_id, available)
            SELECT t.id, '{row["app_id"]}', TRUE
            FROM tenants t
            WHERE t.slug = '{TENANT_SLUG}'
            ON CONFLICT (tenant_id, app_id) DO NOTHING
            """
        )


def downgrade() -> None:
    for app_id in _CONSUMES_BY_APP_ID:
        op.execute(
            f"""
            UPDATE app_catalog
            SET stages = stages #- '{{process,consumes}}'
            WHERE app_id = '{app_id}'
            """
        )

    app_ids = ", ".join(f"'{row['app_id']}'" for row in _NEW_APP_ROWS)
    op.execute(f"DELETE FROM app_tenant_availability WHERE app_id IN ({app_ids})")
    op.execute(f"DELETE FROM app_catalog WHERE app_id IN ({app_ids})")
MIGRATION_EOF
```

- [ ] **Step 2: Write `alembic/tests/test_0020_bundle_consumes_v2.py`** (same mock-`op.execute`-and-assert-exact-SQL harness `test_0019_kick_app.py`/`test_0017_loyalty_shoutout_apps.py` already establish — this repo has no pytest fixture running Alembic against a real Postgres in CI):

```python
"""Regression test for 0020_bundle_consumes_v2 (M1.5 consumes migration).

Same harness convention as test_0019_kick_app.py: mocks alembic.op.execute
and asserts (a) every one of the 16 on-disk process bundles gets exactly
one consumes-bearing statement, (b) every consumes JSON blob is valid JSON
once combined, (c) the three new-row entrypoints resolve to real files
under core/svc_process/bundles/, (d) downgrade() removes exactly what
upgrade() added.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0020_bundle_consumes_v2.py"
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PROCESS_BUNDLES_DIR = _REPO_ROOT / "core" / "svc_process" / "bundles"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0020", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load_migration()


class TestProcessBundleCountMatchesDisk:
    def test_16_process_bundle_files_on_disk(self):
        """The milestone's own gate: consumes-migrated count must equal on-disk process bundle count."""
        files = sorted(
            p.stem for p in _PROCESS_BUNDLES_DIR.glob("*_process.py")
        ) + (["echo_process"] if (_PROCESS_BUNDLES_DIR / "echo_process.py").exists() else [])
        # bot_process.py maps to two app_ids but is one file/one bundle.
        assert len(set(files)) == 16, f"expected 16 process bundle files, found {sorted(set(files))}"


class TestUpgradeEmitsConsumesForEveryAppId:
    def test_every_consumes_json_blob_is_valid(self, migration):
        for app_id, blob in migration._CONSUMES_BY_APP_ID.items():
            parsed = json.loads(blob)
            assert isinstance(parsed, list) and parsed, f"{app_id}: consumes must be a non-empty list"
            for rule in parsed:
                assert "platform" in rule and "event_types" in rule, f"{app_id}: {rule}"

    def test_upgrade_issues_one_update_per_existing_app_id_plus_three_new_rows(self, migration):
        with patch("migration_0020.op.execute") as mock_execute:
            migration.upgrade()
        executed_sql = "\n".join(str(c.args[0]) for c in mock_execute.call_args_list)
        for app_id in migration._CONSUMES_BY_APP_ID:
            assert f"'{app_id}'" in executed_sql, f"missing UPDATE for {app_id}"
        for row in migration._NEW_APP_ROWS:
            assert f"'{row['app_id']}'" in executed_sql, f"missing INSERT for {row['app_id']}"

    def test_new_app_row_entrypoints_resolve_to_real_files(self, migration):
        for row in migration._NEW_APP_ROWS:
            module_path = row["entrypoint"].split(":")[0].replace("bundles.", "")
            assert (_PROCESS_BUNDLES_DIR / f"{module_path}.py").exists(), row["entrypoint"]

    def test_new_app_row_jsonb_blob_is_syntactically_valid_once_combined(self, migration):
        """Guards the exact 0014_wave1a_bundle_seeds regression: a bare `... || '...'::jsonb`
        casts only the last literal unless the whole concatenation is (...)::jsonb-wrapped."""
        for row in migration._NEW_APP_ROWS:
            combined = (
                '{"process": {"entrypoint": "' + row["entrypoint"] + '", '
                '"config": {}, "spec": {"required_config": []}, '
                '"consumes": ' + row["consumes"] + "}}"
            )
            json.loads(combined)  # raises if malformed


class TestDowngradeRemovesExactlyWhatUpgradeAdded:
    def test_downgrade_deletes_the_three_new_app_ids(self, migration):
        with patch("migration_0020.op.execute") as mock_execute:
            migration.downgrade()
        executed_sql = "\n".join(str(c.args[0]) for c in mock_execute.call_args_list)
        for row in migration._NEW_APP_ROWS:
            assert row["app_id"] in executed_sql
        for app_id in migration._CONSUMES_BY_APP_ID:
            assert app_id in executed_sql
```

- [ ] **Step 3: Run the test**

Run: `env -C alembic python3 -m pytest tests/test_0020_bundle_consumes_v2.py -v 2>&1 | tail -30` (adjust the invocation to however this repo's other `alembic/tests/test_00*.py` files are actually run — matching whatever `tests/k8s/alpha/05-unit-tests.sh`/CI does for the `alembic/` directory, if it is not already covered by that script's `libs/*` or `core/svc_*` loops, add an explicit `run_suite "alembic" env -C "$REPO_ROOT/alembic" "$PYTHON_BIN" -m pytest` line to `tests/k8s/alpha/05-unit-tests.sh` alongside the existing `libs/*`/`core/svc_*` loops if one does not already exist — check first: `grep -n "alembic" tests/k8s/alpha/05-unit-tests.sh`).
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/0020_bundle_consumes_v2.py alembic/tests/test_0020_bundle_consumes_v2.py
git commit -m "$(cat <<'EOF'
feat(catalog): add bundle.yaml v2 consumes metadata to every process bundle

Adds stages.process.consumes (spec Sec6.4.3) to all 16 on-disk process
bundles' app_catalog registrations -- 13 via jsonb_set on an existing row,
3 via a brand-new row for bundles that shipped code but were never
catalog-registered (community_reputation_process, community_context_process,
inventory_process -- same routability-gap precedent 0016/0017/0019
establish). Declarative only: no runtime fan-out reads this key yet
(M4/M5 wire the Rust ingest fanout against it). Legacy-tag mapping table
per the M1.5 spec table.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 21: Add the v2 `consumes` rule validator to `flask_core.app_manifest`

**Files:**
- Modify: `libs/flask_core/flask_core/app_manifest.py`
- Modify: `libs/flask_core/tests/test_app_manifest.py`

**Interfaces:**
- Produces: `ConsumeRule` (frozen dataclass: `platform: str`, `event_types: Tuple[str, ...]`, `filters: Mapping[str, Tuple[str, ...]]`), `parse_consumes_v2(rules: list, *, allow_wildcard: bool = False) -> Tuple[ConsumeRule, ...]`, six new `REASON_*` constants (V27–V30 from spec §6.4.4).
- **Scope note:** this task implements only the standalone `consumes` v2 validator (V27–V30) — full `bundle.yaml` v2 parsing (`schema_version`, `egress`, `data.tables`, V1–V26/V31) is M2's `penguin-bundle-host::manifest` deliverable, out of scope for M1.5 ("Python only, no WASM" — full manifest v2 wiring needs the compiler/hub-api install work M2 owns). `parse_consumes_v2` is **not yet called from `parse_manifest()`**'s main flow; it is exercised directly by its own tests and by Task 20's migration content (Step 4 below cross-validates Task 20's `_CONSUMES_BY_APP_ID` dict against it).
- Depends on: none (independent of every DAL task).

- [ ] **Step 1: Write the failing tests** — add to `libs/flask_core/tests/test_app_manifest.py`:

```python
from flask_core.app_manifest import (
    REASON_INVALID_CONSUMES_FILTER,
    REASON_INVALID_EVENT_TYPE_PATTERN,
    REASON_UNKNOWN_CONSUMES_PLATFORM,
    REASON_WILDCARD_CONSUMES_NOT_ALLOWED,
    ConsumeRule,
    ManifestError,
    parse_consumes_v2,
)


class TestParseConsumesV2:
    def test_minimal_valid_rule(self):
        rules = parse_consumes_v2(
            [{"platform": "twitch", "event_types": ["chat.message"]}]
        )
        assert rules == (ConsumeRule(platform="twitch", event_types=("chat.message",), filters={}),)

    def test_rule_with_command_prefix_filter(self):
        rules = parse_consumes_v2(
            [
                {
                    "platform": "discord",
                    "event_types": ["chat.message"],
                    "filters": {"command_prefix": ["!sr", "!songrequest"]},
                }
            ]
        )
        assert rules[0].filters == {"command_prefix": ("!sr", "!songrequest")}

    def test_custom_platform_prefix_accepted(self):
        rules = parse_consumes_v2(
            [{"platform": "custom:my-source", "event_types": ["chat.message"]}]
        )
        assert rules[0].platform == "custom:my-source"

    def test_unknown_platform_rejected(self):
        with pytest.raises(ManifestError) as exc_info:
            parse_consumes_v2([{"platform": "myspace", "event_types": ["chat.message"]}])
        assert exc_info.value.args[0] == REASON_UNKNOWN_CONSUMES_PLATFORM

    def test_missing_event_types_rejected(self):
        with pytest.raises(ManifestError) as exc_info:
            parse_consumes_v2([{"platform": "twitch"}])
        assert exc_info.value.args[0] == REASON_INVALID_EVENT_TYPE_PATTERN

    def test_empty_event_types_rejected(self):
        with pytest.raises(ManifestError) as exc_info:
            parse_consumes_v2([{"platform": "twitch", "event_types": []}])
        assert exc_info.value.args[0] == REASON_INVALID_EVENT_TYPE_PATTERN

    def test_event_type_bad_characters_rejected(self):
        with pytest.raises(ManifestError) as exc_info:
            parse_consumes_v2([{"platform": "twitch", "event_types": ["Chat.Message!"]}])
        assert exc_info.value.args[0] == REASON_INVALID_EVENT_TYPE_PATTERN

    def test_unknown_filter_key_rejected(self):
        with pytest.raises(ManifestError) as exc_info:
            parse_consumes_v2(
                [{"platform": "twitch", "event_types": ["chat.message"], "filters": {"nope": ["x"]}}]
            )
        assert exc_info.value.args[0] == REASON_INVALID_CONSUMES_FILTER

    def test_actor_roles_filter_accepted(self):
        rules = parse_consumes_v2(
            [
                {
                    "platform": "twitch",
                    "event_types": ["chat.message"],
                    "filters": {"actor_roles": ["broadcaster", "moderator"]},
                }
            ]
        )
        assert rules[0].filters["actor_roles"] == ("broadcaster", "moderator")

    def test_wildcard_platform_rejected_by_default(self):
        with pytest.raises(ManifestError) as exc_info:
            parse_consumes_v2([{"platform": "*", "event_types": ["chat.message"]}])
        assert exc_info.value.args[0] == REASON_WILDCARD_CONSUMES_NOT_ALLOWED

    def test_wildcard_platform_allowed_when_flag_set(self):
        rules = parse_consumes_v2(
            [{"platform": "*", "event_types": ["chat.message"]}], allow_wildcard=True
        )
        assert rules[0].platform == "*"

    def test_wildcard_event_type_double_star_rejected_by_default(self):
        with pytest.raises(ManifestError) as exc_info:
            parse_consumes_v2([{"platform": "twitch", "event_types": ["**"]}])
        assert exc_info.value.args[0] == REASON_WILDCARD_CONSUMES_NOT_ALLOWED

    def test_single_star_segment_glob_allowed_without_wildcard_flag(self):
        """A single-segment `*` glob (e.g. `channel.*`) is not the tenant-gated wildcard -- only
        a bare `platform: "*"` or an `event_types` entry of literal `**` is."""
        rules = parse_consumes_v2([{"platform": "twitch", "event_types": ["channel.*", "stream.*"]}])
        assert rules[0].event_types == ("channel.*", "stream.*")

    def test_multiple_rules_are_ored(self):
        rules = parse_consumes_v2(
            [
                {"platform": "twitch", "event_types": ["chat.message"]},
                {"platform": "discord", "event_types": ["chat.message"]},
            ]
        )
        assert len(rules) == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd libs/flask_core && python3 -m pytest tests/test_app_manifest.py -k ConsumesV2 -v 2>&1 | tail -30`
Expected: `ImportError: cannot import name 'ConsumeRule' from 'flask_core.app_manifest'`.

- [ ] **Step 3: Implement** — add to `libs/flask_core/flask_core/app_manifest.py` (near the existing `REASON_*` constants and `StageSpec`):

```python
# --- consumes v2 (spec Sec6.4.3, V27-V31 -- V27/V28/V31 belong to full
# bundle.yaml v2 parsing, M2 scope; only V29/V30's rule-shape validation
# is implemented here, standalone, ahead of that work). ---
REASON_UNKNOWN_CONSUMES_PLATFORM = "unknown_consumes_platform"
REASON_INVALID_EVENT_TYPE_PATTERN = "invalid_event_type_pattern"
REASON_INVALID_CONSUMES_FILTER = "invalid_consumes_filter"
REASON_WILDCARD_CONSUMES_NOT_ALLOWED = "wildcard_consumes_not_allowed"

#: Fixed platform slugs the spine ships connectors for (spec Sec6.4.3);
#: `custom:<name>` (tenant-registered generic-intake sources) and the bare
#: wildcard `*` are handled separately in `_validate_consumes_platform`.
_KNOWN_CONSUMES_PLATFORMS = frozenset(
    {"twitch", "discord", "slack", "youtube", "kick", "waddles"}
)

#: One dotted, lowercase segment, or `*`/`**` glob tokens -- e.g.
#: `chat.message`, `channel.*`, `**`. `_` and digits are legal within a
#: segment (matches PlatformEvent.event_type's own namespace convention).
_EVENT_TYPE_SEGMENT_RE = re.compile(r"^(\*\*|\*|[a-z0-9][a-z0-9_]*)$")

_KNOWN_CONSUMES_FILTER_KEYS = frozenset({"command_prefix", "actor_roles"})


@dataclass(slots=True, frozen=True)
class ConsumeRule:
    """One `consumes` rule (bundle.yaml v2, spec Sec6.4.3) -- ORed with every
    other rule on the same stage; `platform`/`event_types`/`filters` within
    one rule are ANDed. Built exclusively by :func:`parse_consumes_v2`.
    """

    platform: str
    event_types: Tuple[str, ...]
    filters: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)


def _validate_consumes_platform(platform: Any, *, allow_wildcard: bool) -> str:
    if not isinstance(platform, str) or not platform:
        raise ManifestError(REASON_UNKNOWN_CONSUMES_PLATFORM, "platform must be a non-empty string")
    if platform == "*":
        if not allow_wildcard:
            raise ManifestError(
                REASON_WILDCARD_CONSUMES_NOT_ALLOWED,
                "platform: \"*\" requires the tenant setting allow_wildcard_consumes",
            )
        return platform
    if platform.startswith("custom:") and len(platform) > len("custom:"):
        return platform
    if platform not in _KNOWN_CONSUMES_PLATFORMS:
        raise ManifestError(
            REASON_UNKNOWN_CONSUMES_PLATFORM,
            f"platform {platform!r} is not one of {sorted(_KNOWN_CONSUMES_PLATFORMS)}, "
            "a custom:<name>, or \"*\"",
        )
    return platform


def _validate_event_types(raw: Any, *, allow_wildcard: bool) -> Tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ManifestError(REASON_INVALID_EVENT_TYPE_PATTERN, "event_types must be a non-empty list")
    validated: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            raise ManifestError(REASON_INVALID_EVENT_TYPE_PATTERN, f"invalid event_types entry: {entry!r}")
        if entry == "**" and not allow_wildcard:
            raise ManifestError(
                REASON_WILDCARD_CONSUMES_NOT_ALLOWED,
                "event_types entry \"**\" requires the tenant setting allow_wildcard_consumes",
            )
        segments = entry.split(".")
        if not all(_EVENT_TYPE_SEGMENT_RE.match(seg) for seg in segments):
            raise ManifestError(REASON_INVALID_EVENT_TYPE_PATTERN, f"invalid event_types entry: {entry!r}")
        validated.append(entry)
    return tuple(validated)


def _validate_consumes_filters(raw: Any) -> Mapping[str, Tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(REASON_INVALID_CONSUMES_FILTER, "filters must be a mapping")
    validated: dict[str, Tuple[str, ...]] = {}
    for key, values in raw.items():
        if key not in _KNOWN_CONSUMES_FILTER_KEYS:
            raise ManifestError(
                REASON_INVALID_CONSUMES_FILTER,
                f"filters key {key!r} is not one of {sorted(_KNOWN_CONSUMES_FILTER_KEYS)}",
            )
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ManifestError(REASON_INVALID_CONSUMES_FILTER, f"filters[{key!r}] must be a list of strings")
        validated[key] = tuple(values)
    return validated


def parse_consumes_v2(
    rules: list, *, allow_wildcard: bool = False
) -> Tuple[ConsumeRule, ...]:
    """Validate and build a `process` stage's `consumes` v2 rule list (spec Sec6.4.3, V29/V30).

    Each rule is validated independently (`platform`, `event_types`,
    `filters`); rules are ORed at match time by the stage/hub-api, not by
    this function. `allow_wildcard` mirrors the tenant setting
    `allow_wildcard_consumes` (default `False`) gating a bare
    `platform: "*"` or an `event_types` entry of literal `**`.

    Args:
        rules: The manifest's `stages.process.consumes` list, as parsed JSON/YAML.
        allow_wildcard: Whether this tenant has `allow_wildcard_consumes` enabled.

    Returns:
        A tuple of validated `ConsumeRule`, in the same order as `rules`.

    Raises:
        ManifestError: On the first invalid rule, with the matching `REASON_*` code.
    """
    if not isinstance(rules, list) or not rules:
        raise ManifestError(REASON_INVALID_EVENT_TYPE_PATTERN, "consumes must be a non-empty list")
    compiled: list[ConsumeRule] = []
    for raw_rule in rules:
        if not isinstance(raw_rule, dict):
            raise ManifestError(REASON_UNKNOWN_CONSUMES_PLATFORM, f"invalid consumes rule: {raw_rule!r}")
        platform = _validate_consumes_platform(raw_rule.get("platform"), allow_wildcard=allow_wildcard)
        event_types = _validate_event_types(raw_rule.get("event_types"), allow_wildcard=allow_wildcard)
        filters = _validate_consumes_filters(raw_rule.get("filters"))
        compiled.append(ConsumeRule(platform=platform, event_types=event_types, filters=filters))
    return tuple(compiled)
```

Add `import re` at the top of the file if not already present (check first — `_SEGMENT`/`_APP_ID_RE` near the top already use `re.compile`, so `import re` is almost certainly already there).

- [ ] **Step 4: Cross-validate Task 20's migration content against this validator** — add to `libs/flask_core/tests/test_app_manifest.py`:

```python
import json
from pathlib import Path


class TestConsumesV2MatchesMigration0020:
    """Every consumes blob Task 20's migration writes into app_catalog must
    itself pass parse_consumes_v2 -- the declarative data and its own
    validator must never drift apart."""

    def test_every_migrated_consumes_blob_is_valid(self):
        import importlib.util

        migration_path = (
            Path(__file__).resolve().parents[3]
            / "alembic"
            / "versions"
            / "0020_bundle_consumes_v2.py"
        )
        spec = importlib.util.spec_from_file_location("migration_0020", migration_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for app_id, blob in module._CONSUMES_BY_APP_ID.items():
            rules = json.loads(blob)
            parse_consumes_v2(rules)  # raises ManifestError on drift
        for row in module._NEW_APP_ROWS:
            parse_consumes_v2(json.loads(row["consumes"]))
```

- [ ] **Step 5: Run tests, coverage**

Run: `cd libs/flask_core && python3 -m pytest tests/test_app_manifest.py -v 2>&1 | tail -40`
Expected: every new test passes, including `TestConsumesV2MatchesMigration0020` (this test only passes once Task 20 has landed — if run before Task 20, skip it or mark `pytest.mark.skipif(not migration_path.exists(), reason="Task 20 not yet merged")`).

Run: `cd libs/flask_core && python3 -m pytest --cov=flask_core.app_manifest --cov-fail-under=90 tests/test_app_manifest.py`

- [ ] **Step 6: Commit**

```bash
git add libs/flask_core/flask_core/app_manifest.py libs/flask_core/tests/test_app_manifest.py
git commit -m "$(cat <<'EOF'
feat(flask-core): add bundle.yaml v2 consumes rule validator (V29/V30)

ConsumeRule + parse_consumes_v2() validate a process stage's consumes v2
rule list (spec Sec6.4.3): known platform/custom:<name>/wildcard-gated "*",
dotted-lowercase event_type globs, command_prefix/actor_roles filter keys,
allow_wildcard_consumes gating on both platform "*" and event_types "**".
Standalone (not yet wired into parse_manifest()'s main flow -- full
bundle.yaml v2 parsing is M2 scope); cross-validated against Task 20's
own migrated consumes data.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 22: Add `PlatformEvent.source` and populate it from every `normalize()`

**Files:**
- Modify: `libs/flask_core/flask_core/stream_pipeline.py`
- Modify: `libs/flask_core/tests/test_stream_pipeline.py`
- Modify: `core/svc_ingest/bundles/twitch_ingest.py`, `core/svc_ingest/config.py`
- Modify: `core/svc_ingest/bundles/discord_ingest.py`, `core/svc_ingest/config.py`
- Modify: `core/svc_ingest/bundles/slack_ingest.py`, `youtube_live_ingest.py`, `kick_ingest.py`, `echo_ingest.py`

**Interfaces:** No dependency on Tasks 1–21. **Produces:** `flask_core.stream_pipeline.SourceRef` (frozen dataclass: `platform: str`, `account_id: str`, `channel_id: str | None`), `PlatformEvent.source: SourceRef | None = None` with strict (de)serialization (`source.platform` must equal the top-level `platform`, or `EnvelopeError`).

- [ ] **Step 1: Write the failing tests** — add to `libs/flask_core/tests/test_stream_pipeline.py`:

```python
from flask_core.stream_pipeline import EnvelopeError, PlatformEvent, SourceRef


class TestSourceRef:
    def test_round_trip(self):
        source = SourceRef(platform="twitch", account_id="waddlesbot", channel_id="12345")
        assert SourceRef.from_dict(source.to_dict()) == source

    def test_channel_id_may_be_null(self):
        source = SourceRef(platform="discord", account_id="123456789", channel_id=None)
        assert SourceRef.from_dict(source.to_dict()) == source

    def test_missing_account_id_raises(self):
        with pytest.raises(EnvelopeError):
            SourceRef.from_dict({"platform": "twitch", "channel_id": "x"})


class TestPlatformEventSource:
    def test_absent_source_deserializes_to_none(self):
        event = PlatformEvent.from_dict(
            {
                "platform": "twitch",
                "event_type": "chat.message",
                "actor": "penguin",
                "payload": {},
                "occurred_at": "2026-09-14T12:00:00.000Z",
            }
        )
        assert event.source is None

    def test_null_source_deserializes_to_none(self):
        event = PlatformEvent.from_dict(
            {
                "platform": "twitch",
                "event_type": "chat.message",
                "actor": "penguin",
                "payload": {},
                "occurred_at": "2026-09-14T12:00:00.000Z",
                "source": None,
            }
        )
        assert event.source is None

    def test_source_round_trips_through_to_dict(self):
        event = PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="penguin",
            payload={},
            occurred_at="2026-09-14T12:00:00.000Z",
            source=SourceRef(platform="twitch", account_id="waddlesbot", channel_id="12345"),
        )
        assert PlatformEvent.from_dict(event.to_dict()) == event

    def test_source_platform_mismatch_raises(self):
        with pytest.raises(EnvelopeError, match="source.platform"):
            PlatformEvent.from_dict(
                {
                    "platform": "twitch",
                    "event_type": "chat.message",
                    "actor": "penguin",
                    "payload": {},
                    "occurred_at": "2026-09-14T12:00:00.000Z",
                    "source": {"platform": "discord", "account_id": "x", "channel_id": None},
                }
            )

    def test_source_not_a_dict_raises(self):
        with pytest.raises(EnvelopeError, match="source"):
            PlatformEvent.from_dict(
                {
                    "platform": "twitch",
                    "event_type": "chat.message",
                    "actor": "penguin",
                    "payload": {},
                    "occurred_at": "2026-09-14T12:00:00.000Z",
                    "source": "not-an-object",
                }
            )
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd libs/flask_core && python3 -m pytest tests/test_stream_pipeline.py -k "SourceRef or PlatformEventSource" -v 2>&1 | tail -30`
Expected: `ImportError: cannot import name 'SourceRef' from 'flask_core.stream_pipeline'`.

- [ ] **Step 3: Implement in `libs/flask_core/flask_core/stream_pipeline.py`**

Add, directly above `PlatformEvent`:
```python
@dataclass(slots=True, frozen=True)
class SourceRef:
    """Which connection an event came in on -- distinct from tenant/community.

    `platform` mirrors the enclosing `PlatformEvent.platform` (must be
    equal, enforced by `PlatformEvent.from_dict` -- a reader consulting
    only `source` is never wrong). `account_id` is the bot account/app
    id/intake source name (stable across restarts, never a secret);
    `channel_id` is the platform's own channel/guild/room id, or `None`
    for an account-level event with no channel.
    """

    platform: str
    account_id: str
    channel_id: str | None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain, JSON-ready dict."""
        return {"platform": self.platform, "account_id": self.account_id, "channel_id": self.channel_id}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> SourceRef:
        """Deserialize from a plain dict; raises `EnvelopeError` on a bad shape."""
        return cls(
            platform=_require_str(d, "platform"),
            account_id=_require_str(d, "account_id"),
            channel_id=_optional_str(d, "channel_id"),
        )
```

Modify `PlatformEvent`:
```python
    platform: str
    event_type: str
    actor: str | None
    payload: dict[str, Any]
    occurred_at: str
    source: SourceRef | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain, JSON-ready dict."""
        d: dict[str, Any] = {
            "platform": self.platform,
            "event_type": self.event_type,
            "actor": self.actor,
            "payload": dict(self.payload),
            "occurred_at": self.occurred_at,
        }
        if self.source is not None:
            d["source"] = self.source.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> PlatformEvent:
        """Deserialize from a plain dict; raises `EnvelopeError` on a bad shape."""
        platform = _require_str(d, "platform")
        raw_source = d.get("source")
        source: SourceRef | None = None
        if raw_source is not None:
            if not isinstance(raw_source, dict):
                raise EnvelopeError(f"'source' must be a JSON object or null, got {raw_source!r}")
            source = SourceRef.from_dict(raw_source)
            if source.platform != platform:
                raise EnvelopeError(
                    f"'source.platform' ({source.platform!r}) must equal top-level "
                    f"'platform' ({platform!r})"
                )
        return cls(
            platform=platform,
            event_type=_require_str(d, "event_type"),
            actor=_optional_str(d, "actor"),
            payload=_require_object(d, "payload"),
            occurred_at=_require_str(d, "occurred_at"),
            source=source,
        )
```

- [ ] **Step 4: Run tests, coverage**

Run: `cd libs/flask_core && python3 -m pytest tests/test_stream_pipeline.py -v 2>&1 | tail -40`
Run: `cd libs/flask_core && python3 -m pytest --cov=flask_core.stream_pipeline --cov-fail-under=90 tests/test_stream_pipeline.py`

- [ ] **Step 5: Commit the flask_core change on its own** (the six `normalize()` updates below are a second, mechanical pass — commit this first so the type exists before any bundle references it):

```bash
git add libs/flask_core/flask_core/stream_pipeline.py libs/flask_core/tests/test_stream_pipeline.py
git commit -m "$(cat <<'EOF'
feat(flask-core): add PlatformEvent.source (spec Sec6.1.1)

SourceRef{platform, account_id, channel_id} identifies which connection
(bot account, intake source) an event came in on -- distinct from
tenant/community. Optional field (absent/null -> None, same convention as
target_app_id) with strict deserialization: source.platform must equal
the top-level platform, or EnvelopeError.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

- [ ] **Step 6: Populate `source` in `twitch_ingest.py`** (verified against the current file — `raw` carries no bot-account identity field, so this adds one small config constant, the same pattern `Config.YOUTUBE_CLIENT_ID_REF` already establishes in `core/svc_ingest/config.py`)

Add to `core/svc_ingest/config.py`, alongside the other platform `_REF` constants:
```python
    TWITCH_BOT_ACCOUNT_ID_REF = "TWITCH_BOT_USERNAME"
```
and, wherever `Config` reads its `_REF`-suffixed env vars into a plain attribute (matching `YOUTUBE_CLIENT_ID_REF`'s own pattern in that file), add:
```python
    TWITCH_BOT_ACCOUNT_ID = os.getenv(TWITCH_BOT_ACCOUNT_ID_REF, "waddlesbot")
```

Edit `core/svc_ingest/bundles/twitch_ingest.py`:
```python
from flask_core import PlatformEvent, SourceRef

from config import Config
```
(add both imports; `Config` is already how every other svc_ingest bundle reaches its own env-sourced settings — check `twitch_gateway_manifest.py`'s own import for the exact relative path convention already in use in this package.)

Old:
```python
    return PlatformEvent(
        platform=raw.get("platform", "twitch"),
        event_type="message",
        actor=actor,
        payload={...},
        occurred_at=raw.get("occurred_at") or datetime.now(UTC).isoformat(),
    )
```

New:
```python
    platform = raw.get("platform", "twitch")
    return PlatformEvent(
        platform=platform,
        event_type="message",
        actor=actor,
        payload={...},
        occurred_at=raw.get("occurred_at") or datetime.now(UTC).isoformat(),
        source=SourceRef(
            platform=platform,
            account_id=Config.TWITCH_BOT_ACCOUNT_ID,
            channel_id=channel_name,
        ),
    )
```
(`payload={...}` is the exact, unchanged dict literal already in the file — only the two new lines, `platform = raw.get(...)` hoisted out and the new `source=` kwarg, are added.)

- [ ] **Step 7: Populate `source` in `discord_ingest.py`** — `raw` already carries `channel_id`; add the same one new config constant pattern for the bot's own application id.

Add to `core/svc_ingest/config.py`:
```python
    DISCORD_BOT_ACCOUNT_ID_REF = "DISCORD_APPLICATION_ID"
    DISCORD_BOT_ACCOUNT_ID = os.getenv(DISCORD_BOT_ACCOUNT_ID_REF, "waddles-discord-bot")
```

Edit `core/svc_ingest/bundles/discord_ingest.py`:
```python
from flask_core import PlatformEvent, SourceRef

from config import Config
```

Old:
```python
    return PlatformEvent(
        platform=raw.get("platform", "discord"),
        event_type="message",
        actor=raw.get("author_username") or author_id,
        payload={
            "text": content.strip(),
            "guild_id": raw.get("guild_id"),
            "channel_id": raw.get("channel_id"),
            "message_id": raw.get("message_id"),
            "author_id": author_id,
        },
        occurred_at=raw.get("occurred_at") or datetime.now(UTC).isoformat(),
    )
```

New:
```python
    platform = raw.get("platform", "discord")
    return PlatformEvent(
        platform=platform,
        event_type="message",
        actor=raw.get("author_username") or author_id,
        payload={
            "text": content.strip(),
            "guild_id": raw.get("guild_id"),
            "channel_id": raw.get("channel_id"),
            "message_id": raw.get("message_id"),
            "author_id": author_id,
        },
        occurred_at=raw.get("occurred_at") or datetime.now(UTC).isoformat(),
        source=SourceRef(
            platform=platform,
            account_id=Config.DISCORD_BOT_ACCOUNT_ID,
            channel_id=raw.get("channel_id"),
        ),
    )
```

- [ ] **Step 8: Populate `source` in the remaining four ingest bundles** (`slack_ingest.py`, `youtube_live_ingest.py`, `kick_ingest.py`, `echo_ingest.py`) — same pattern as Steps 6–7, applied per-file. For each, first find the file's exact raw-event field names and its `PlatformEvent(...)` construction site:

Run (repeat for each of the four files):
```bash
grep -n "PlatformEvent(\|raw.get(" core/svc_ingest/bundles/slack_ingest.py
grep -n "PlatformEvent(\|raw.get(" core/svc_ingest/bundles/youtube_live_ingest.py
grep -n "PlatformEvent(\|raw.get(" core/svc_ingest/bundles/kick_ingest.py
grep -n "PlatformEvent(\|raw.get(" core/svc_ingest/bundles/echo_ingest.py
```

For each file: (1) import `SourceRef` from `flask_core` and `Config` from `config` (same two-line addition as Steps 6–7); (2) hoist the existing `platform=raw.get("platform", "<default>")` expression (or whichever literal the file already passes as the `platform=` kwarg) into a local `platform = ...` variable reused by both the `PlatformEvent(platform=platform, ...)` kwarg and the new `source=SourceRef(platform=platform, ...)` kwarg; (3) add a `source=SourceRef(platform=platform, account_id=Config.<PLATFORM>_BOT_ACCOUNT_ID, channel_id=<the file's own channel-identifying raw field, per the spec's own mapping table: Slack -> the channel id; YouTube -> the live chat's video/broadcast id; Kick -> the channel slug; the echo demo bundle -> `None`, it has no real channel>)` kwarg to the `PlatformEvent(...)` call, following Steps 6–7's exact shape; (4) add the matching `<PLATFORM>_BOT_ACCOUNT_ID_REF`/`<PLATFORM>_BOT_ACCOUNT_ID` pair to `core/svc_ingest/config.py`, naming the `_REF` env var after whatever credential/identity env var that platform's own receiver (`receivers/slack_socket.py`/`receivers/youtube_poll.py`/`receivers/kick_*.py`) already reads for its own app/client identity (grep `core/svc_ingest/receivers/` for the platform's existing `_REF`/`os.getenv` constants first — reuse the existing one if an app/client-id env var is already read there, rather than inventing a second name for the same value).

- [ ] **Step 9: Update each of the six ingest bundles' own test files**

Add one assertion per existing `normalize()` test: `assert event.source == SourceRef(platform="<platform>", account_id=<the value the test's own Config/env fixture sets or the default>, channel_id=<the expected channel value from that test's raw fixture>)`. `Config.<PLATFORM>_BOT_ACCOUNT_ID` reads at **import time** from an env var with a hardcoded default (Step 6/7's pattern) — tests that don't override the env var assert against that same default string, keeping them deterministic.

- [ ] **Step 10: Run every ingest bundle test file + coverage**

Run: `env -C core/svc_ingest python3 -m pytest -v 2>&1 | tail -60`
Expected: all green — six updated `normalize()` functions, six updated test files.

Run: `env -C core/svc_ingest python3 -m pytest --cov=bundles --cov-fail-under=90`

- [ ] **Step 11: Commit**

```bash
git add core/svc_ingest/bundles/twitch_ingest.py core/svc_ingest/bundles/discord_ingest.py \
        core/svc_ingest/bundles/slack_ingest.py core/svc_ingest/bundles/youtube_live_ingest.py \
        core/svc_ingest/bundles/kick_ingest.py core/svc_ingest/bundles/echo_ingest.py \
        core/svc_ingest/config.py core/svc_ingest/tests/
git commit -m "$(cat <<'EOF'
feat(ingest): populate PlatformEvent.source in every normalize()

All six normalizers (twitch/discord/slack/youtube/kick/echo) now stamp
source={platform, account_id, channel_id} per spec Sec6.1.1's mapping
table -- account_id from a new per-platform Config constant (reusing an
existing receiver identity env var where one exists), channel_id from
each raw event's own channel/guild/video/slug field.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 23: Python-side golden fixtures for the shared spine contract

**Files:**
- Create: `libs/flask_core/tests/fixtures/spine/envelopes/valid/*.json`
- Create: `libs/flask_core/tests/fixtures/spine/envelopes/invalid/*.json`
- Create: `libs/flask_core/tests/fixtures/spine/keys/*.json`
- Create: `libs/flask_core/tests/test_golden_fixtures.py`

**Interfaces:** Depends on: Task 22 (`PlatformEvent.source` must exist for the fixture set to be complete). **Canonical copy:** this repo (`waddlebot`) owns the fixtures under `libs/flask_core/tests/fixtures/spine/` — the `penguin-spine` Rust plan's own golden-test task copies these exact files into `penguin-libs` verbatim (byte-for-byte), never regenerates them independently, so both languages assert against one source of truth.

**Scope note:** per spec §14.1, the full fixture family also includes `dlq/*.json` and `entries/*.json` (stream-entry wrapping) — both require a `DLQ record`/stream-entry Python type that does not exist in this repo yet (that lands with M1's `penguin-spine`/`flask_core` alignment work, e.g. `trace_context` and a DLQ dataclass). This task ships the two families buildable **today** from what M1.5 itself adds (`envelopes/` and `keys/`); `dlq/`/`entries/` are M1's own follow-up once those types exist, tracked there rather than stubbed here with a fabricated type.

- [ ] **Step 1: Write the fixture generator + round-trip test**

```python
cat > libs/flask_core/tests/test_golden_fixtures.py <<'FIXTURES_EOF'
"""Generates + round-trips the Sec14.1 golden fixture set this repo owns canonically.

Two families, buildable from what M1.5 itself ships (StageEnvelope with
PlatformEvent.source, Task 22; the bundle_stream_key/bundle_config_key/
bundle_state_key builders): `envelopes/{valid,invalid}/*.json` and
`keys/*.json`. `dlq/*.json`/`entries/*.json` are deferred to M1, which adds
the DLQ record type and trace_context this repo does not have yet -- see
this task's own scope note.

CI fails if either side skips a fixture (spec Sec14.1): this suite asserts
`fixtures_examined == fixtures_on_disk`, a non-zero denominator, printed
every run -- never a bare "no findings".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flask_core.stream_pipeline import (
    EnvelopeError,
    PlatformEvent,
    SourceRef,
    StageEnvelope,
    bundle_config_key,
    bundle_state_key,
    bundle_stream_key,
)

_FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "spine"
_VALID_DIR = _FIXTURES_ROOT / "envelopes" / "valid"
_INVALID_DIR = _FIXTURES_ROOT / "envelopes" / "invalid"
_KEYS_DIR = _FIXTURES_ROOT / "keys"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def _base_event(**overrides) -> PlatformEvent:
    fields = {
        "platform": "twitch",
        "event_type": "chat.message",
        "actor": "penguin",
        "payload": {"text": "!songrequest foo", "channel_id": "12345"},
        "occurred_at": "2026-09-14T12:00:00.000Z",
        "source": SourceRef(platform="twitch", account_id="waddlesbot", channel_id="12345"),
    }
    fields.update(overrides)
    return PlatformEvent(**fields)


def _base_envelope(**overrides) -> StageEnvelope:
    fields = {
        "tenant": "global",
        "community": None,
        "app_id": "waddles.social.music.default",
        "stage": "process",
        "event": _base_event(),
        "ts": "2026-09-14T12:00:00.123Z",
        "target_app_id": None,
    }
    fields.update(overrides)
    return StageEnvelope(**fields)


# --- Fixture generation (idempotent -- overwrites its own output files) ---

def _generate_valid_fixtures() -> dict[str, StageEnvelope]:
    return {
        "tenant_wide": _base_envelope(),
        "community_scoped": _base_envelope(community="42"),
        "target_app_id_set": _base_envelope(target_app_id="waddles.community.forums.default"),
        "source_absent": _base_envelope(event=_base_event(source=None)),
        "empty_payload": _base_envelope(event=_base_event(payload={})),
        "unicode_payload": _base_envelope(
            event=_base_event(payload={"text": "hello \U0001f427 你好"})
        ),
        "max_length_app_id": _base_envelope(
            app_id="waddles." + "a" * 40 + "." + "b" * 40 + "." + "c" * 40
        ),
        "discord_source_null_channel": _base_envelope(
            event=_base_event(
                platform="discord",
                source=SourceRef(platform="discord", account_id="waddles-bot-app", channel_id=None),
            )
        ),
    }


def _generate_invalid_fixtures() -> dict[str, dict]:
    valid = _base_envelope().to_dict()
    return {
        "missing_tenant": {k: v for k, v in valid.items() if k != "tenant"},
        "wrong_type_stage": {**valid, "stage": 123},
        "unknown_top_level_key": {**valid, "unexpected_field": "nope"},
        "bad_stage_value": {**valid, "stage": "presentation"},
        "legacy_pre_event_shape": {
            "tenant": "global", "community": None, "app_id": valid["app_id"],
            "stage": "process", "payload": {"text": "legacy"}, "ts": valid["ts"],
        },
        "non_object_payload": {**valid, "event": {**valid["event"], "payload": "not-an-object"}},
        "empty_platform": {**valid, "event": {**valid["event"], "platform": ""}},
        "source_platform_mismatch": {
            **valid,
            "event": {
                **valid["event"],
                "source": {"platform": "discord", "account_id": "x", "channel_id": None},
            },
        },
    }


def _generate_key_fixtures() -> dict[str, dict]:
    return {
        "tenant_wide_stream_key": {
            "input": {"tenant": "global", "community": None, "app_id": "waddles.social.music.default", "stage": "process"},
            "expected": bundle_stream_key("global", None, "waddles.social.music.default", "process"),
        },
        "community_scoped_stream_key": {
            "input": {"tenant": "acme", "community": "42", "app_id": "waddles.social.music.default", "stage": "action"},
            "expected": bundle_stream_key("acme", "42", "waddles.social.music.default", "action"),
        },
        "config_key": {
            "input": {"tenant": "global", "community": None, "app_id": "waddles.social.music.default"},
            "expected": bundle_config_key("global", None, "waddles.social.music.default"),
        },
        "state_key": {
            "input": {"tenant": "global", "community": None, "app_id": "waddles.social.music.default"},
            "expected": bundle_state_key("global", None, "waddles.social.music.default"),
        },
    }


@pytest.fixture(scope="module", autouse=True)
def _write_fixtures_to_disk():
    """Regenerate every fixture file before the round-trip assertions run."""
    for name, envelope in _generate_valid_fixtures().items():
        _write_json(_VALID_DIR / f"{name}.json", envelope.to_dict())
    for name, data in _generate_invalid_fixtures().items():
        _write_json(_INVALID_DIR / f"{name}.json", data)
    for name, data in _generate_key_fixtures().items():
        _write_json(_KEYS_DIR / f"{name}.json", data)
    yield


class TestValidEnvelopeFixturesRoundTrip:
    def test_every_valid_fixture_deserializes_and_reserializes_byte_identical(self):
        files = sorted(_VALID_DIR.glob("*.json"))
        assert len(files) > 0, "zero valid envelope fixtures on disk -- generator did not run"
        examined = 0
        for path in files:
            raw_text = path.read_text()
            data = json.loads(raw_text)
            envelope = StageEnvelope.from_dict(data)
            reserialized = json.dumps(envelope.to_dict(), indent=2, sort_keys=False) + "\n"
            assert reserialized == raw_text, f"{path.name}: round-trip mismatch"
            examined += 1
        assert examined == len(files) == len(_generate_valid_fixtures())
        print(f"golden_fixtures.valid_envelopes: fixtures_examined={examined} fixtures_on_disk={len(files)}")


class TestInvalidEnvelopeFixturesAllFail:
    def test_every_invalid_fixture_raises_envelopeerror(self):
        files = sorted(_INVALID_DIR.glob("*.json"))
        assert len(files) > 0, "zero invalid envelope fixtures on disk -- generator did not run"
        examined = 0
        for path in files:
            data = json.loads(path.read_text())
            with pytest.raises(EnvelopeError):
                StageEnvelope.from_dict(data)
            examined += 1
        assert examined == len(files) == len(_generate_invalid_fixtures())
        print(f"golden_fixtures.invalid_envelopes: fixtures_examined={examined} fixtures_on_disk={len(files)}")


class TestKeyFixturesMatchBuilders:
    def test_every_key_fixture_matches_its_builder_output(self):
        files = sorted(_KEYS_DIR.glob("*.json"))
        assert len(files) > 0, "zero key fixtures on disk -- generator did not run"
        examined = 0
        for path in files:
            data = json.loads(path.read_text())
            if "stream_key" in path.stem:
                actual = bundle_stream_key(**data["input"])
            elif path.stem == "config_key":
                actual = bundle_config_key(**data["input"])
            elif path.stem == "state_key":
                actual = bundle_state_key(**data["input"])
            else:
                pytest.fail(f"unrecognized key fixture name: {path.stem}")
            assert actual == data["expected"], f"{path.name}: key builder output changed"
            examined += 1
        assert examined == len(files) == len(_generate_key_fixtures())
        print(f"golden_fixtures.keys: fixtures_examined={examined} fixtures_on_disk={len(files)}")
FIXTURES_EOF
```

- [ ] **Step 2: Run it — this both generates the fixture files on disk and asserts the round-trip**

Run: `cd libs/flask_core && python3 -m pytest tests/test_golden_fixtures.py -v -s 2>&1 | tail -40`
Expected: all three test classes pass, and each of the three printed summary lines (one per fixture family: valid envelopes, invalid envelopes, keys) shows its `fixtures_examined` count equal to its `fixtures_on_disk` count, both non-zero (8, 8, and 4 respectively).

- [ ] **Step 3: Confirm the fixture files actually landed on disk (not just in an ephemeral test run)**

Run: `find libs/flask_core/tests/fixtures/spine -name '*.json' | wc -l`
Expected: `20` (8 valid + 8 invalid + 4 keys).

- [ ] **Step 4: Commit the generator AND the generated fixture files** (the fixtures are committed artifacts, not regenerated at test time in CI — re-running the suite regenerates them byte-identically, which is exactly what the round-trip assertion proves):

```bash
git add libs/flask_core/tests/test_golden_fixtures.py libs/flask_core/tests/fixtures/spine/
git commit -m "$(cat <<'EOF'
test(flask-core): add Python-side golden fixtures (envelopes + keys)

libs/flask_core/tests/fixtures/spine/{envelopes/{valid,invalid},keys}/*.json
are the canonical copy (spec Sec14.1) -- the penguin-spine Rust plan copies
these files verbatim rather than regenerating them. dlq/entries fixture
families deferred to M1 (no DLQ record/trace_context Python type exists
yet). fixtures_examined == fixtures_on_disk asserted and printed every run.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 24: Amend `docs/APP_BUNDLE_AUTHORING.md` (DAL section, consumes v2, ingest note)

**Files:**
- Modify: `docs/APP_BUNDLE_AUTHORING.md`

**Interfaces:** Depends on: Tasks 3–22 (documents their combined output). No code produced.

- [ ] **Step 1: Locate and rewrite the "Accessing the database / shared state" section**

Run: `grep -n "^## \|Accessing the database" docs/APP_BUNDLE_AUTHORING.md`
Find that section's heading and its current content (the frozen `get_bundle_dal()`/`get_bundle_context()` API description referencing `flask_core.database.AsyncDAL`/`AsyncDAL.select_async`/etc.).

Replace its body with:

```markdown
## Accessing the database / shared state

A bundle's entrypoint signatures are frozen (`transform(event) -> PlatformEvent | None`
for process, `<name>(envelope, config, *, http_client) -> TransportResult` for
action) and carry no DAL parameter. Call `flask_core.get_bundle_dal()` from
inside your own entrypoint body to reach the process-wide database handle,
bound once at stage-runner startup — never call `set_bundle_dal()` yourself.

**The DAL is `penguin-dal`'s `AsyncDB`** (`penguin_dal.AsyncDB`), not pydal —
build queries with its native `Query`/`TableProxy` builder:

```python
from flask_core import get_bundle_dal

dal = get_bundle_dal()
rows = await dal(dal.quotes.id == quote_id).select()
pk = await dal.quotes.async_insert(quote_text="...", quoted_username="alice")
count = await dal(dal.quotes.id == quote_id).update(deleted_at=None)
```

For a join, `GROUP BY`, `ON CONFLICT`, or a DB-side function (`RANDOM()`) —
anything penguin-dal's single-table `Query` builder can't express — use
`flask_core.bundle_runtime.raw_sql_rows()`/`raw_sql_write()`, which run raw
SQL with **named** `:param` placeholders (never `$1`/`%s`) over `AsyncDB`'s
own public `.engine` and hand the result back as a `penguin_dal.Rows`:

```python
from flask_core.bundle_runtime import raw_sql_rows

rows = await raw_sql_rows(
    dal, "SELECT * FROM t WHERE community_id = :community_id", {"community_id": 42}
)
```

Scope every query by `flask_core.get_bundle_context().community`/`.tenant` —
never by anything read from `event.payload`, which is untrusted platform
data (security.md Tenant Isolation).

**No thread offload.** WASI has no thread pool: never wrap a DAL call in
`asyncio.to_thread(...)`/`loop.run_in_executor(...)` — every `penguin_dal`
call is a plain `await`, already safe to call directly from an `async def`
entrypoint.

There is exactly **one** database facade — `flask_core.database.AsyncDAL`
(pydal) is not available to bundle code; a bundle importing
`flask_core.database` or `pydal` directly fails the compiler's import check.
```

- [ ] **Step 2: Add a `consumes` v2 section** (find where `stages`/`bundle.yaml` fields are documented and add, or add a new top-level section if none exists yet):

```markdown
## Declaring what your process bundle consumes

A `process` stage bundle declares which events it wants via `consumes` —
hub-api resolves this into the granted ingest streams your bundle actually
receives; you never open a Valkey connection yourself.

```yaml
stages:
  process:
    entry: "bundles.social_music_process:transform"
    consumes:
      - platform: twitch
        event_types: ["chat.message"]
        filters:
          command_prefix: ["!sr", "!songrequest"]
      - platform: discord
        event_types: ["chat.message"]
        filters:
          command_prefix: ["!sr", "!songrequest"]
```

`platform` is one of `twitch`, `discord`, `slack`, `youtube`, `kick`,
`waddles`, `custom:<name>`, or the tenant-gated `*`. `event_types` is a list
of dotted, lowercase globs (`chat.message`, `channel.*`) matched against
`PlatformEvent.event_type`. `filters.command_prefix` narrows to messages
starting with any listed prefix — this is what keeps a `!sr`-only bundle
from waking on every chat line. Filters are an optimization, not a security
boundary: validate your own input regardless.

**Ingest is not bundle-pluggable.** Unlike `process`/`action`, you cannot
ship an `ingest` stage — the six platform normalizers (Twitch, Discord,
Slack, YouTube, Kick) and the generic webhook/REST intake are built into
the Rust ingest service; a manifest declaring an `ingest` stage is rejected.
```

- [ ] **Step 3: Add a one-paragraph note on `PlatformEvent.source`** near wherever `PlatformEvent`'s fields are documented:

```markdown
Every `PlatformEvent` your bundle receives carries an optional `source`
(`{platform, account_id, channel_id}`) identifying *which* connection to
the platform produced it — useful when a deployment has more than one bot
account or channel on the same platform. `source.platform` always equals
the event's own top-level `platform`.
```

- [ ] **Step 4: Verify no doc-reference check breaks**

Run: `bash scripts/check-doc-refs.sh` (the root Makefile's `check-docs` target)
Expected: exits 0.

- [ ] **Step 5: Commit**

```bash
git add docs/APP_BUNDLE_AUTHORING.md
git commit -m "$(cat <<'EOF'
docs(bundles): document the penguin-dal facade, consumes v2, and PlatformEvent.source

Rewrites the 'Accessing the database' section for penguin_dal.AsyncDB
(Query/TableProxy builder + raw_sql_rows/raw_sql_write, no asyncio.to_thread),
adds a consumes v2 section with the worked example, and a note on
ingest no longer being bundle-pluggable and PlatformEvent.source.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 25: Wire the bundle-DAL-import gate into `make lint`/CI

**Files:**
- Modify: `Makefile`
- Modify: `tests/k8s/alpha/05-unit-tests.sh`

**Interfaces:** Depends on: Task 1 (the script), Tasks 5–19 (nothing to regress against once they land). Produces: `make check-bundle-dal-imports`, wired into `make lint` and the CI unit-test step.

- [ ] **Step 1: Add the Makefile target**

Edit the root `Makefile` — add to the `.PHONY` line: `check-bundle-dal-imports`, and add the target itself near `lint`:

```makefile
check-bundle-dal-imports:
	@bash scripts/ci/check_bundle_dal_imports.sh
```

Edit the existing `lint:` target to also run it:

```makefile
lint:
	@bash scripts/lint.sh
	@$(MAKE) check-bundle-dal-imports
```

- [ ] **Step 2: Wire it into the CI unit-test step so a regression fails the build, not just local `make lint`**

Edit `tests/k8s/alpha/05-unit-tests.sh` — add, right after the `# --- core/svc_* ---` loop (before the final `log_pass` line):

```bash
# --- Bundle DAL import gate (D21b) -- fails if any bundle re-imports the
# legacy flask_core.database/pydal surface directly. ---
log_info "Suite: bundle-dal-import-gate"
if ! bash "$REPO_ROOT/scripts/ci/check_bundle_dal_imports.sh"; then
    log_fail "Suite failed: bundle-dal-import-gate"
    exit 1
fi
```

- [ ] **Step 3: Verify both wirings work**

Run: `make check-bundle-dal-imports`
Expected: `check_bundle_dal_imports: files_scanned=<N> legacy_hits=0` (exit 0).

Run: `make lint 2>&1 | tail -20`
Expected: `scripts/lint.sh`'s own output, followed by the same `files_scanned=<N> legacy_hits=0` line, exit 0.

Run: `bash tests/k8s/alpha/05-unit-tests.sh 2>&1 | tail -20` (full run — this also re-runs every suite from Tasks 4–23; expect it to take several minutes)
Expected: `Suite: bundle-dal-import-gate` appears in the output, followed by `Unit tests step completed -- <N> passed across all suites`.

- [ ] **Step 4: Prove the gate can actually fail (verification integrity — `critical-rules.md`)**

Run: `echo "from flask_core.database import AsyncDAL" >> core/svc_process/bundles/echo_process.py && bash scripts/ci/check_bundle_dal_imports.sh; echo "exit=$?"; git checkout -- core/svc_process/bundles/echo_process.py`
Expected: the script reports `legacy_hits=1`, prints the offending `path:line`, and `exit=1` — then the temporary line is reverted.

- [ ] **Step 5: Commit**

```bash
git add Makefile tests/k8s/alpha/05-unit-tests.sh
git commit -m "$(cat <<'EOF'
chore(ci): wire the bundle flask_core.database/pydal import gate into lint + CI

make check-bundle-dal-imports runs scripts/ci/check_bundle_dal_imports.sh
(Task 1); make lint calls it, and tests/k8s/alpha/05-unit-tests.sh gates
the full unit-test run on it too, so a regression fails CI, not just a
local lint pass. Verified the gate actually fails on a planted violation.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2rQgkHY872RubwXoBZxtE
EOF
)"
```

---

### Task 26: Final verification — full M1.5 acceptance gate

**Files:** None modified — verification only.

**Interfaces:** Depends on: every prior task (1–25).

- [ ] **Step 1: Re-run the complete inventory check and confirm the milestone's own numbers**

Run: `bash scripts/ci/check_bundle_dal_imports.sh`
Expected: `check_bundle_dal_imports: files_scanned=<N> legacy_hits=0`, `N` ≥ 32 (16 process + 18 action + 12 ingest bundle files minus `__init__.py`s — the exact count printed is the evidence, not a guessed number).

Run: `grep -rl "get_bundle_dal()" core/svc_process/bundles/*.py core/svc_action/bundles/*.py | wc -l`
Expected: `16` — the exact count Task 1's inventory established, now all migrated (verify none still reference `AsyncDAL` as a type):
Run: `grep -rl "AsyncDAL" core/svc_process/bundles/*.py core/svc_action/bundles/*.py; echo "exit=$?"`
Expected: no output, `exit=1` (grep found nothing).

- [ ] **Step 2: Confirm the `consumes` count matches the on-disk process-bundle count**

Run: `find core/svc_process/bundles -maxdepth 1 -name '*_process.py' -o -maxdepth 1 -name 'echo_process.py' | sort -u | wc -l`
Expected: `16`.

Run: `env -C alembic python3 -m pytest tests/test_0020_bundle_consumes_v2.py -v 2>&1 | tail -20`
Expected: all pass, including `test_16_process_bundle_files_on_disk`.

- [ ] **Step 3: Run every touched service's full test suite with coverage, in one pass**

Run: `env -C libs/flask_core python3 -m pytest --cov=flask_core --cov-report=term-missing --cov-fail-under=90 -v 2>&1 | tail -60`
Run: `env -C core/svc_process python3 -m pytest --cov=bundles --cov-report=term-missing --cov-fail-under=90 -v 2>&1 | tail -60`
Run: `env -C core/svc_action python3 -m pytest --cov=bundles --cov-report=term-missing --cov-fail-under=90 -v 2>&1 | tail -60`
Run: `env -C core/svc_ingest python3 -m pytest --cov=bundles --cov-report=term-missing --cov-fail-under=90 -v 2>&1 | tail -60`
Expected: all four report `Required test coverage of 90% reached` and zero failures.

- [ ] **Step 4: Run the full repo unit-test script (the actual CI entrypoint)**

Run: `bash tests/k8s/alpha/05-unit-tests.sh 2>&1 | tail -30`
Expected: `Unit tests step completed -- <N> passed across all suites`, with the `bundle-dal-import-gate` suite included in the run (Task 25).

- [ ] **Step 5: Run this repo's own lint**

Run: `bash scripts/lint.sh 2>&1 | tail -40`
Expected: exits 0 (ruff/mypy clean on every file this plan touched).

- [ ] **Step 6: Report the acceptance summary** (paste into the PR description when this branch is opened for review):

```
M1.5 acceptance:
- Bundle DAL inventory: 16 (verified — see Task 1's per-file table)
- Bundles migrated to penguin-dal: 16/16
- flask_core.database/pydal import gate: files_scanned=<N> legacy_hits=0
- consumes v2 migrated: 16/16 process bundles (13 existing rows + 3 new)
- Golden fixtures: 8 valid + 8 invalid envelopes + 4 keys = 20 files,
  fixtures_examined == fixtures_on_disk in every run
- Coverage: flask_core/svc_process/svc_action/svc_ingest all ≥ 90%
- Known incidental behavior change: social_alias_process's _upsert_alias
  new-alias INSERT path now actually persists (Task 12) — the old code
  called an async method from a sync closure with no await
```

---

## Self-Review

**1. Spec coverage** — every M1.5 deliverable row (spec §16) mapped to a task:

| Spec §16 M1.5 deliverable | Task(s) |
|---|---|
| Inventory (bundle count, non-zero denominator) | Task 1 — corrected the naive grep's 17 to the verified 16 (`community_context_process.py`'s hit was a docstring mention, not a real import) |
| Migration (rewrite DB-access lines against penguin-dal) | Tasks 3–4 (facade + wiring), Tasks 5–19 (all 16 bundles) |
| Tests (native pytest, coverage unchanged/better) | Every one of Tasks 5–19's Step "Rewrite the test fixture" + "Run tests, gate, coverage"; Task 26 Step 3 re-confirms all four services ≥ 90% |
| Gate (zero `flask_core.database`/`pydal`, files-scanned printed) | Task 1 (script), Task 25 (wired into `make lint`/CI, proven to actually fail in Step 4) |
| `consumes` migration (count == on-disk process bundle count) | Task 20 (migration + its own test asserting the 16-file count) |
| Legacy `consumes`-tag mapping table | Task 20's `_CONSUMES_BY_APP_ID`/`_NEW_APP_ROWS`, one rule per spec's mapping-table row |
| `bundle.yaml` v2 `consumes` contract (V27–V31) | Task 21 (`ConsumeRule`/`parse_consumes_v2`, V29/V30; V27/V28/V31 explicitly scoped to M2) |
| `PlatformEvent.source` + strict (de)serialization | Task 22 |
| Every `normalize()` populates `source` | Task 22 Steps 6–8 (all six ingest bundles) |
| Golden fixtures, Python side, canonical-copy statement | Task 23 |
| `docs/APP_BUNDLE_AUTHORING.md` amendment | Task 24 |
| "Ingest is not bundle-pluggable from v3" documented | Task 24 Step 2 |
| Ingest-stage bundle files not deleted | Never touched by Tasks 5–19 (DAL migration is process/action only); Task 22 only edits `normalize()` bodies, doesn't delete files |

**2. Placeholder scan** — grepped the finished plan for `TBD`/`TODO`/`similar to Task`/`fill in`/`placeholder` (case-insensitive): only legitimate SQL-`:param`-placeholder prose hits; zero forbidden patterns.

**3. Signature/type consistency** — `get_bundle_dal()`/`set_bundle_dal()` (Task 3) used identically in Tasks 4–19; `raw_sql_rows`/`raw_sql_write` (Task 3) used identically in every Pattern B task (5, 7–11, 13–14, 18) with the same `(dal, sql, params)` argument order everywhere; `ConsumeRule`/`parse_consumes_v2` (Task 21) match Task 20's data shape exactly (cross-validated by Task 21 Step 4's own test); `SourceRef` (Task 22) used identically across `stream_pipeline.py`, the six `normalize()` edits, and Task 23's fixtures; `AsyncDB` spelled consistently everywhere (no `AsyncDb`/`Asyncdb` drift found). All 26 tasks numbered 1–26 with no gaps or duplicates.

**4. Findings fixed during this review:** none outstanding — the one item worth flagging to a human reviewer (not a plan defect) is Task 12's incidental behavior fix (`_upsert_alias`'s previously-silent no-op insert), called out explicitly in that task's commit message and Task 26's acceptance summary rather than hidden.
