"""Real-Postgres tests for 0067_marketplace_sources (marketplace Phase 1, Task 1).

Fresh replay (via `pg_docker.migrated_postgres`), CHECK constraints, the single immutable
built-in row, the audit trigger, the `source_id` FKs, an existing-DB upgrade that leaves
pre-populated rows untouched, and an explicit-target downgrade round-trip.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

PREVIOUS_REVISION = "0066_one_time_secrets"
SHA = "a" * 40

EXPECTED_COLUMNS = sorted(
    [
        "id",
        "public_id",
        "name",
        "is_builtin",
        "git_url",
        "ref",
        "pinned_commit_sha",
        "pending_commit_sha",
        "build_mode",
        "publisher_key_fingerprint",
        "publisher_public_key",
        "sandbox_ack_by",
        "sandbox_ack_at",
        "sandbox_ack_text_hash",
        "sandbox_ack_commit_sha",
        "status",
        "last_scanned_at",
        "last_scan_error",
        "created_by",
        "created_at",
        "updated_at",
    ]
)

_INSERT = (
    "INSERT INTO marketplace_sources "
    "(name, git_url, ref, build_mode, publisher_key_fingerprint, publisher_public_key, "
    "sandbox_ack_by, sandbox_ack_at, sandbox_ack_text_hash, sandbox_ack_commit_sha, "
    "pinned_commit_sha) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id"
)


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (includes 0067)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0067-marketplace-sources") as database:
        yield database


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _prebuilt(cur, url: str = "https://github.com/acme/bundles", **kw) -> int:  # type: ignore[no-untyped-def]
    cur.execute(
        _INSERT,
        (
            kw.get("name", "acme"),
            url,
            kw.get("ref", "main"),
            1,
            kw.get("fp", "fp1"),
            kw.get("key", "pk"),
            None,
            None,
            None,
            None,
            kw.get("sha"),
        ),
    )
    return int(cur.fetchone()[0])


def _sandbox(cur, **kw) -> int:  # type: ignore[no-untyped-def]
    cur.execute(
        _INSERT,
        (
            "sb",
            "https://github.com/acme/sb",
            "main",
            2,
            None,
            None,
            kw.get("by", 7),
            kw.get("at", "now()"),
            kw.get("hash", "b" * 64),
            kw.get("ack_sha", SHA),
            None,
        ),
    )
    return int(cur.fetchone()[0])


def test_columns_and_builtin_seed(db: PgTestDatabase) -> None:
    conn = _connect(db)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='marketplace_sources' ORDER BY column_name"
        )
        assert [r[0] for r in cur.fetchall()] == EXPECTED_COLUMNS
        cur.execute(
            "SELECT count(*), min(build_mode), min(status), bool_and(git_url IS NULL), "
            "bool_and(public_id IS NOT NULL) FROM marketplace_sources WHERE is_builtin"
        )
        assert cur.fetchone() == (1, 3, 2, True, True)
        for table in ("app_versions", "app_catalog"):
            cur.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name=%s AND column_name='source_id'",
                (table,),
            )
            assert cur.fetchone() == ("YES",)
            cur.execute(
                "SELECT count(*) FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu USING (constraint_name) "
                "WHERE tc.table_name=%s AND tc.constraint_type='FOREIGN KEY' "
                "AND ccu.table_name='marketplace_sources'",
                (table,),
            )
            assert cur.fetchone() == (1,)
    conn.close()


@pytest.mark.parametrize(
    ("label", "build", "expected"),
    [
        (
            "http url",
            lambda c: _prebuilt(c, url="http://github.com/a/b"),
            psycopg2.errors.CheckViolation,
        ),
        (
            "ssh url",
            lambda c: _prebuilt(c, url="ssh://git@github.com/a/b"),
            psycopg2.errors.CheckViolation,
        ),
        (
            "file url",
            lambda c: _prebuilt(c, url="file:///etc"),
            psycopg2.errors.CheckViolation,
        ),
        (
            "userinfo url",
            lambda c: _prebuilt(c, url="https://u:p@github.com/a/b"),
            psycopg2.errors.CheckViolation,
        ),
        (
            "bad sha",
            lambda c: _prebuilt(c, sha="XYZ" + "0" * 37),
            psycopg2.errors.CheckViolation,
        ),
        (
            "uppercase sha",
            lambda c: _prebuilt(c, sha="A" * 40),
            psycopg2.errors.CheckViolation,
        ),
        (
            "prebuilt no key",
            lambda c: _prebuilt(c, fp=None, key=None),
            psycopg2.errors.CheckViolation,
        ),
        (
            "sandbox no ack",
            lambda c: _sandbox(c, by=None),
            psycopg2.errors.CheckViolation,
        ),
        (
            "sandbox no hash",
            lambda c: _sandbox(c, hash=None),
            psycopg2.errors.CheckViolation,
        ),
        ("no ref", lambda c: _prebuilt(c, ref=None), psycopg2.errors.CheckViolation),
    ],
)
def test_check_constraints_reject(
    db: PgTestDatabase, label: str, build, expected
) -> None:  # type: ignore[no-untyped-def]
    conn = _connect(db)
    with pytest.raises(expected), conn.cursor() as cur:
        build(cur)
    conn.close()


def test_valid_prebuilt_and_sandbox_accepted_with_uuid(db: PgTestDatabase) -> None:
    conn = _connect(db)
    with conn.cursor() as cur:
        pid = _prebuilt(
            cur, url=f"https://github.com/acme/{uuid.uuid4().hex[:8]}", sha=SHA
        )
        sid = _sandbox(cur)
        cur.execute(
            "SELECT public_id FROM marketplace_sources WHERE id = ANY(%s)",
            ([pid, sid],),
        )
        ids = [r[0] for r in cur.fetchall()]
    assert len(set(ids)) == 2
    conn.close()


def test_status_and_mode_enum_constraints(db: PgTestDatabase) -> None:
    conn = _connect(db)
    with conn.cursor() as cur:
        pid = _prebuilt(cur, url="https://github.com/acme/enum")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "UPDATE marketplace_sources SET status = 9 WHERE id = %s", (pid,)
            )
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "UPDATE marketplace_sources SET build_mode = 4 WHERE id = %s", (pid,)
            )
        with pytest.raises(
            psycopg2.errors.CheckViolation
        ):  # mode 3 only for the builtin
            cur.execute(
                "UPDATE marketplace_sources SET build_mode = 3 WHERE id = %s", (pid,)
            )
    conn.close()


def test_second_builtin_rejected(db: PgTestDatabase) -> None:
    conn = _connect(db)
    with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO marketplace_sources (name, is_builtin, build_mode) "
            "VALUES ('dup', TRUE, 3)"
        )
    conn.close()


def test_builtin_is_immutable_but_status_writable(db: PgTestDatabase) -> None:
    conn = _connect(db)
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("DELETE FROM marketplace_sources WHERE is_builtin")
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute(
                "UPDATE marketplace_sources SET git_url='https://x.io/a' WHERE is_builtin"
            )
        cur.execute("UPDATE marketplace_sources SET status = 4 WHERE is_builtin")
        cur.execute("UPDATE marketplace_sources SET status = 2 WHERE is_builtin")
    conn.close()


def test_audit_rows_written(db: PgTestDatabase) -> None:
    conn = _connect(db)
    with conn.cursor() as cur:
        pid = _prebuilt(cur, url="https://github.com/acme/audit")
        cur.execute("SELECT public_id FROM marketplace_sources WHERE id=%s", (pid,))
        public_id = cur.fetchone()[0]
        cur.execute("UPDATE marketplace_sources SET status = 2 WHERE id=%s", (pid,))
        cur.execute("DELETE FROM marketplace_sources WHERE id=%s", (pid,))
        cur.execute(
            "SELECT operation, old_status, new_status FROM marketplace_sources_audit_log "
            "WHERE source_public_id=%s ORDER BY id",
            (public_id,),
        )
        assert cur.fetchall() == [
            ("INSERT", None, 1),
            ("UPDATE", 1, 2),
            ("DELETE", 2, None),
        ]
        cur.execute(
            "SELECT count(*) FROM marketplace_sources_audit_log a JOIN marketplace_sources "
            "s ON s.public_id=a.source_public_id WHERE s.is_builtin AND a.operation='INSERT'"
        )
        assert cur.fetchone() == (1,)  # the seed itself is audited
    conn.close()


def test_downgrade_roundtrip_and_existing_rows_untouched() -> None:
    """Down to the explicit predecessor, add a pre-existing row, upgrade: NULL source_id."""
    with migrated_postgres("0067-roundtrip") as database:
        alembic_cli("downgrade", PREVIOUS_REVISION, dsn=database.dsn)
        conn = _connect(database)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('marketplace_sources'), to_regclass('marketplace_sources_audit_log')"
            )
            assert cur.fetchone() == (None, None)
            cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE column_name='source_id' AND table_name IN ('app_versions','app_catalog')"
            )
            assert cur.fetchone() == (0,)
            cur.execute("INSERT INTO app_catalog (app_id) VALUES ('legacy.app')")
        conn.close()
        alembic_cli("upgrade", "head", dsn=database.dsn)
        conn = _connect(database)
        with conn.cursor() as cur:
            cur.execute("SELECT source_id FROM app_catalog WHERE app_id='legacy.app'")
            assert cur.fetchone() == (None,)
            cur.execute("SELECT count(*) FROM marketplace_sources WHERE is_builtin")
            assert cur.fetchone() == (1,)
        conn.close()


def test_trigger_functions_pin_search_path(db: PgTestDatabase) -> None:
    """Both trigger functions pin search_path (hijack/audit-evasion hardening)."""
    conn = _connect(db)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT proname, proconfig FROM pg_proc WHERE proname IN "
            "('fn_marketplace_sources_audit','fn_marketplace_sources_builtin_guard')"
        )
        rows = dict(cur.fetchall())
    conn.close()
    assert set(rows) == {"fn_marketplace_sources_audit", "fn_marketplace_sources_builtin_guard"}
    for cfg in rows.values():
        assert cfg == ["search_path=pg_catalog, pg_temp"]
