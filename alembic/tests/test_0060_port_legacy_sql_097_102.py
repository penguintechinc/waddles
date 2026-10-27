"""Real-Postgres tests for 0060_port_legacy_sql_097_102 (legacy SQL 097-102 -> Alembic).

# regression: config/postgres SQL migrations 097/099/100/101/102 never reached existing DBs

`0001_baseline` replays `config/postgres/migrations/*.sql` only on a fresh database, so a
numbered SQL file added after an existing DB was baselined never ran there. 0060 is the
Alembic bridge. Three real-Postgres scenarios prove it (no mocked `op.execute`):

  - `fresh_db`: empty DB, `alembic upgrade head` -- the baseline replay already applied
    097-102, so 0060 must be a clean no-op (and still ledger them);
  - `existing_db`: an "existing DB" -- the legacy replay stops at 096 (what a DB baselined
    before 097 looks like) with representative data seeded, THEN `alembic upgrade head`;
    0060 must create the 097/100/101/102 objects, carry the data forward and ledger 097-102;
  - the minimal CI bootstrap (`migrated_postgres`): no legacy baseline at all, so only the
    self-contained DDL is applied and no ledger is invented.

Plus: the two paths end in the same schema (parity), and re-running is a no-op.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import secrets
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from unittest import mock

import psycopg2
import pytest
from pg_docker import (
    DOCKER_AVAILABLE,
    PgTestDatabase,
    alembic_cli,
    empty_postgres,
    migrated_postgres,
)

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SQL_DIR = _REPO_ROOT / "config" / "postgres" / "migrations"
_REVISION_PATH = _REPO_ROOT / "alembic" / "versions" / "0060_port_legacy_sql_097_102.py"

#: First numbered SQL file an "existing DB" has NOT seen: the legacy replay stops at 096.
_FIRST_UNSEEN_SQL = 97

#: Tables whose final shape the port owns -- compared between the two paths.
_PORTED_TABLES = (
    "reputation_tenant",
    "overlay_view_credentials",
    "overlay_images",
    "caption_events",
    "community_polls",
)

Rows = list[tuple[object, ...]]


def _load_revision() -> ModuleType:
    """Import the 0060 revision module by path (it is not an importable package)."""
    spec = importlib.util.spec_from_file_location("waddles_alembic_0060", _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REVISION = _load_revision()
_PRE_REVISION: str = _REVISION.down_revision
_LEGACY_VERSIONS: tuple[str, ...] = _REVISION.LEGACY_VERSIONS


def _query(dsn: str, sql: str, params: Sequence[object] = ()) -> Rows:
    """Run one statement (autocommit) on a short-lived connection and return every row."""
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [tuple(row) for row in cur.fetchall()]


def _execute(dsn: str, sql: str, params: Sequence[object] = ()) -> None:
    """Run one write statement on a short-lived autocommitting connection."""
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)


def _scalar(dsn: str, sql: str, params: Sequence[object] = ()) -> object:
    """Return the first column of the first row of a read query."""
    rows = _query(dsn, sql, params)
    assert rows, f"no rows for: {sql}"
    return rows[0][0]


def _exists(dsn: str, table: str) -> bool:
    """True when `public.<table>` exists."""
    return bool(_scalar(dsn, "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)))


def _ledger(dsn: str) -> set[str]:
    """Every `schema_migrations.version` currently recorded."""
    return {str(row[0]) for row in _query(dsn, "SELECT version FROM schema_migrations")}


def _snapshot(dsn: str) -> dict[str, object]:
    """Columns, indexes, constraints and sequence type of the ported tables, order-independent."""
    snap: dict[str, object] = {}
    for table in _PORTED_TABLES:
        snap[f"{table}.columns"] = _query(
            dsn,
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY column_name",
            (table,),
        )
        snap[f"{table}.indexes"] = _query(
            dsn,
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = %s ORDER BY indexname",
            (table,),
        )
        snap[f"{table}.constraints"] = _query(
            dsn,
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = %s::regclass ORDER BY conname",
            (f"public.{table}",),
        )
    snap["caption_events_id_seq"] = _query(
        dsn,
        "SELECT data_type FROM pg_sequences "
        "WHERE schemaname = 'public' AND sequencename = 'caption_events_id_seq'",
    )
    snap["reputation_global_gone"] = not _exists(dsn, "reputation_global")
    return snap


def _row_counts(dsn: str) -> dict[str, int]:
    """Row count of each data-bearing ported table (static queries, no SQL built from names)."""
    return {
        "reputation_tenant": int(str(_scalar(dsn, "SELECT count(*) FROM reputation_tenant"))),
        "overlay_view_credentials": int(
            str(_scalar(dsn, "SELECT count(*) FROM overlay_view_credentials"))
        ),
        "caption_events": int(str(_scalar(dsn, "SELECT count(*) FROM caption_events"))),
        "community_polls": int(str(_scalar(dsn, "SELECT count(*) FROM community_polls"))),
    }


@dataclass(slots=True, frozen=True)
class SeededDb:
    """An existing-DB simulation after `upgrade head`, plus what was seeded beforehand."""

    db: PgTestDatabase
    expected_reputation_pairs: frozenset[tuple[int, int]]
    expected_overlay_hashes: frozenset[tuple[int, str, str | None]]
    seeded_user_id: int
    unseeded_rep_user_id: int
    seeded_community_id: int


def _legacy_sql_before(cutoff: int, dest: Path) -> int:
    """Copy `*.sql` files numbered < `cutoff` into `dest`; return how many."""
    copied = 0
    for sql_file in sorted(_SQL_DIR.glob("*.sql")):
        if int(sql_file.name[:3]) < cutoff:
            shutil.copy(sql_file, dest / sql_file.name)
            copied += 1
    return copied


def _seed_pre_097(dsn: str) -> tuple[int, int, int]:
    """Seed rows the 097/099/100/102 ports must carry forward; return (user, no-event user, community)."""
    tenant_a = int(str(_scalar(dsn, "SELECT id FROM tenants ORDER BY id LIMIT 1")))
    tenant_b = int(
        str(
            _scalar(
                dsn,
                "INSERT INTO tenants (slug, display_name) VALUES ('port-0060-b', 'Port 0060 B') "
                "RETURNING id",
            )
        )
    )
    community_a = int(
        str(
            _scalar(
                dsn,
                "INSERT INTO communities (name, tenant_id) VALUES ('port-0060-a', %s) RETURNING id",
                (tenant_a,),
            )
        )
    )
    community_b = int(
        str(
            _scalar(
                dsn,
                "INSERT INTO communities (name, tenant_id) VALUES ('port-0060-b', %s) RETURNING id",
                (tenant_b,),
            )
        )
    )
    user_with_events = int(str(_scalar(dsn, "INSERT INTO hub_users DEFAULT VALUES RETURNING id")))
    user_no_events = int(str(_scalar(dsn, "INSERT INTO hub_users DEFAULT VALUES RETURNING id")))

    # 097: a global score with activity in two tenants (-> two tenant rows), and a global
    # row with no events at all (-> intentionally dropped, there is no tenant to derive).
    _execute(
        dsn,
        "INSERT INTO reputation_global (hub_user_id, score, total_events) VALUES (%s, 700, 3), (%s, 650, 1)",
        (user_with_events, user_no_events),
    )
    for community in (community_a, community_b):
        _execute(
            dsn,
            "INSERT INTO reputation_events (community_id, hub_user_id, platform, platform_user_id, "
            "event_type, score_before, score_after) VALUES (%s, %s, 'discord', 'u1', 'vote', 600, 700)",
            (community, user_with_events),
        )
    # 099: a poll created the pre-099 way (created_by NOT NULL, real hub_users.id).
    _execute(
        dsn,
        "INSERT INTO community_polls (community_id, created_by, title) VALUES (%s, %s, 'pre-0060 poll')",
        (community_a, user_with_events),
    )
    # 100: a legacy plaintext overlay key + its grace-window predecessor.
    _execute(
        dsn,
        "INSERT INTO community_overlay_tokens (community_id, overlay_key, previous_key) "
        "VALUES (%s, %s, %s)",
        (community_a, secrets.token_hex(16), secrets.token_hex(16)),
    )
    # 102: a 007-shaped row to keep (numeric(3,2) score) and a community-less orphan to drop.
    _execute(
        dsn,
        "INSERT INTO caption_events (community_id, platform, username, original_message, confidence_score) "
        "VALUES (%s, 'twitch', 'alice', 'hello world', 0.87), (NULL, 'twitch', 'bob', 'orphan', 0.5)",
        (community_a,),
    )
    return user_with_events, user_no_events, community_a


@pytest.fixture(scope="module")
def fresh_db() -> Iterator[PgTestDatabase]:
    """An empty DB taken through the whole chain: baseline replay applies 097-102 first."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with empty_postgres("0060-fresh") as db:
        alembic_cli("upgrade", "head", dsn=db.dsn)
        yield db


@pytest.fixture(scope="module")
def existing_db() -> Iterator[SeededDb]:
    """A DB baselined before 097 (legacy replay <= 096), seeded, then upgraded to head."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    sql_dir = Path(tempfile.mkdtemp(prefix="waddles-0060-legacy-sql-"))
    try:
        assert _legacy_sql_before(_FIRST_UNSEEN_SQL, sql_dir) > 0, "no legacy SQL copied"
        with empty_postgres("0060-existing") as db:
            with mock.patch.dict(os.environ, {"LEGACY_SQL_DIR": str(sql_dir)}):
                alembic_cli("upgrade", _PRE_REVISION, dsn=db.dsn)

            # Pre-state: this really is a DB the port has to fix.
            for table in ("reputation_tenant", "overlay_view_credentials", "overlay_images"):
                assert not _exists(db.dsn, table), f"{table} must not exist before 0060"
            assert _exists(db.dsn, "reputation_global")
            assert set(_LEGACY_VERSIONS) & _ledger(db.dsn) == set(), "097-102 must be unledgered"

            user, orphan_user, community = _seed_pre_097(db.dsn)
            pairs = frozenset(
                (int(str(t)), int(str(u)))
                for t, u in _query(
                    db.dsn,
                    "SELECT DISTINCT c.tenant_id, rg.hub_user_id FROM reputation_global rg "
                    "JOIN reputation_events re ON re.hub_user_id = rg.hub_user_id "
                    "JOIN communities c ON c.id = re.community_id",
                )
            )
            hashes = frozenset(
                (
                    int(str(cid)),
                    hashlib.sha256(str(key).encode()).hexdigest(),
                    hashlib.sha256(str(prev).encode()).hexdigest() if prev is not None else None,
                )
                for cid, key, prev in _query(
                    db.dsn,
                    "SELECT community_id, overlay_key, previous_key FROM community_overlay_tokens",
                )
            )
            assert len(pairs) == 2 and hashes, "seed produced nothing to backfill"

            alembic_cli("upgrade", "head", dsn=db.dsn)
            yield SeededDb(db, pairs, hashes, user, orphan_user, community)
    finally:
        shutil.rmtree(sql_dir, ignore_errors=True)


def test_ledger_versions_name_real_sql_files() -> None:
    """Every ledger version 0060 writes is a SQL file on disk (denominator = 6, never 0)."""
    assert len(_LEGACY_VERSIONS) == 6
    missing = [v for v in _LEGACY_VERSIONS if not (_SQL_DIR / f"{v}.sql").is_file()]
    assert missing == []
    assert len(set(_LEGACY_VERSIONS)) == len(_LEGACY_VERSIONS)


@requires_docker
def test_fresh_replay_ends_with_ported_objects_and_full_ledger(fresh_db: PgTestDatabase) -> None:
    """Fresh path: baseline applied 097-102, 0060 no-ops cleanly and every ledger row exists."""
    for table in ("reputation_tenant", "overlay_view_credentials", "overlay_images"):
        assert _exists(fresh_db.dsn, table), table
    assert not _exists(fresh_db.dsn, "reputation_global")
    assert set(_LEGACY_VERSIONS) <= _ledger(fresh_db.dsn)


@requires_docker
def test_existing_db_gets_097_100_101_102_and_ledger(existing_db: SeededDb) -> None:
    """Existing path: objects absent before 0060 now exist, the old table is gone, ledger complete."""
    dsn = existing_db.db.dsn
    for table in ("reputation_tenant", "overlay_view_credentials", "overlay_images"):
        assert _exists(dsn, table), table
    assert not _exists(dsn, "reputation_global")
    assert set(_LEGACY_VERSIONS) <= _ledger(dsn)
    assert _scalar(dsn, "SELECT count(*) FROM alembic_version") == 1


@requires_docker
def test_097_backfill_carries_tenant_scoped_rows_and_drops_unattributable(
    existing_db: SeededDb,
) -> None:
    """Per-tenant carry-forward; a global row with no events has no tenant and is dropped."""
    dsn = existing_db.db.dsn
    got = frozenset(
        (int(str(t)), int(str(u)))
        for t, u in _query(dsn, "SELECT tenant_id, hub_user_id FROM reputation_tenant")
    )
    assert got == existing_db.expected_reputation_pairs
    assert len(got) == 2
    scores = _query(
        dsn,
        "SELECT score, total_events FROM reputation_tenant WHERE hub_user_id = %s",
        (existing_db.seeded_user_id,),
    )
    assert scores == [(700, 3), (700, 3)]
    assert _scalar(
        dsn,
        "SELECT count(*) FROM reputation_tenant WHERE hub_user_id = %s",
        (existing_db.unseeded_rep_user_id,),
    ) == 0


@requires_docker
def test_097_grants_mod_core_reputation_when_role_exists(existing_db: SeededDb) -> None:
    """The role-existence-guarded GRANT reached the new table."""
    dsn = existing_db.db.dsn
    assert _scalar(dsn, "SELECT count(*) FROM pg_roles WHERE rolname = 'mod_core_reputation'") == 1
    for privilege in ("SELECT", "INSERT", "UPDATE"):
        assert _scalar(
            dsn,
            "SELECT has_table_privilege('mod_core_reputation', 'public.reputation_tenant', %s)",
            (privilege,),
        ) is True, privilege


@requires_docker
def test_099_actor_hash_poll_columns(existing_db: SeededDb) -> None:
    """created_by is nullable, created_by_hash exists, the CHECK still forbids both NULL."""
    dsn = existing_db.db.dsn
    assert _scalar(
        dsn,
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'community_polls' AND column_name = 'created_by'",
    ) == "YES"
    assert _scalar(
        dsn,
        "SELECT count(*) FROM community_polls WHERE title = 'pre-0060 poll' AND created_by IS NOT NULL",
    ) == 1
    community = existing_db.seeded_community_id
    _execute(
        dsn,
        "INSERT INTO community_polls (community_id, created_by, created_by_hash, title) "
        "VALUES (%s, NULL, %s, 'chat poll')",
        (community, "h" * 64),
    )
    with pytest.raises(psycopg2.errors.CheckViolation):
        _execute(
            dsn,
            "INSERT INTO community_polls (community_id, created_by, created_by_hash, title) "
            "VALUES (%s, NULL, NULL, 'identity-less poll')",
            (community,),
        )
    _execute(dsn, "DELETE FROM community_polls WHERE title = 'chat poll'")


@requires_docker
def test_100_overlay_credentials_backfilled_as_hashes_never_plaintext(existing_db: SeededDb) -> None:
    """Every legacy overlay key reappears only as its SHA-256 hex (and its grace predecessor)."""
    got = frozenset(
        (int(str(c)), str(k), None if p is None else str(p))
        for c, k, p in _query(
            existing_db.db.dsn,
            "SELECT community_id, key_hash, previous_key_hash FROM overlay_view_credentials",
        )
    )
    assert got == existing_db.expected_overlay_hashes
    assert _scalar(
        existing_db.db.dsn, "SELECT count(*) FROM overlay_view_credentials WHERE length(key_hash) <> 64"
    ) == 0


@requires_docker
def test_101_overlay_images_table_shape(existing_db: SeededDb) -> None:
    """overlay_images exists with a unique opaque asset_id and BIGINT community_id."""
    dsn = existing_db.db.dsn
    types = {
        str(c): str(t)
        for c, t in _query(
            dsn,
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'overlay_images'",
        )
    }
    assert types["community_id"] == "bigint" and types["asset_id"] == "uuid"
    assert _scalar(
        dsn, "SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_overlay_images_community_id'"
    ) == 1


@requires_docker
def test_102_caption_events_reconciled_to_rust_shape(existing_db: SeededDb) -> None:
    """007-shaped table widened to the Rust entity's types; orphan dropped, real row kept."""
    dsn = existing_db.db.dsn
    cols = {
        str(c): (str(t), str(n))
        for c, t, n in _query(
            dsn,
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'caption_events'",
        )
    }
    assert cols["id"] == ("bigint", "NO")
    assert cols["community_id"] == ("bigint", "NO")
    assert cols["confidence_score"] == ("double precision", "YES")
    assert cols["user_ref"] == ("uuid", "YES")
    assert cols["username"][1] == "YES"
    assert _query(dsn, "SELECT data_type FROM pg_sequences WHERE sequencename = 'caption_events_id_seq'") == [
        ("bigint",)
    ]
    assert _query(dsn, "SELECT original_message, confidence_score FROM caption_events") == [
        ("hello world", 0.87)
    ]


@requires_docker
def test_existing_and_fresh_paths_converge_on_one_schema(
    fresh_db: PgTestDatabase, existing_db: SeededDb
) -> None:
    """Columns, indexes, constraints and sequence type match between the two paths."""
    fresh = _snapshot(fresh_db.dsn)
    existing = _snapshot(existing_db.db.dsn)
    assert fresh.keys() == existing.keys() and len(fresh) == len(_PORTED_TABLES) * 3 + 2
    drift = {key: (fresh[key], existing[key]) for key in fresh if fresh[key] != existing[key]}
    assert drift == {}


@requires_docker
@pytest.mark.parametrize("which", ["fresh", "existing"])
def test_rerun_is_a_noop(
    which: str, fresh_db: PgTestDatabase, existing_db: SeededDb
) -> None:
    """Downgrade (ledger rows only) then upgrade again: schema + data unchanged, ledger restored."""
    dsn = fresh_db.dsn if which == "fresh" else existing_db.db.dsn
    schema_before, counts_before = _snapshot(dsn), _row_counts(dsn)

    alembic_cli("downgrade", _PRE_REVISION, dsn=dsn)
    assert not (set(_LEGACY_VERSIONS) & _ledger(dsn)), "downgrade must remove the ledger rows"
    assert _snapshot(dsn) == schema_before, "downgrade must leave the DDL in place"
    assert _row_counts(dsn) == counts_before, "downgrade must not touch data"

    alembic_cli("upgrade", "head", dsn=dsn)
    assert set(_LEGACY_VERSIONS) <= _ledger(dsn)
    assert _snapshot(dsn) == schema_before
    assert _row_counts(dsn) == counts_before


@requires_docker
def test_minimal_ci_bootstrap_applies_self_contained_ddl_without_inventing_a_ledger() -> None:
    """No legacy baseline (the CI/harness stamp): DDL that can apply does, no ledger is faked."""
    with migrated_postgres("0060-harness") as db:
        for table in ("reputation_tenant", "overlay_view_credentials", "overlay_images", "caption_events"):
            assert _exists(db.dsn, table), table
        assert not _exists(db.dsn, "community_polls"), "0060 must not fabricate legacy tables"
        assert not _exists(db.dsn, "schema_migrations"), "0060 must not invent a legacy ledger"
        # The PR job's downgrade/upgrade round trip must also survive without a ledger.
        alembic_cli("downgrade", _PRE_REVISION, dsn=db.dsn)
        alembic_cli("upgrade", "head", dsn=db.dsn)
        assert _exists(db.dsn, "reputation_tenant")
