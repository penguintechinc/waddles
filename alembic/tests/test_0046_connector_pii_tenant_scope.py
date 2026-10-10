"""Real-Postgres regression tests for 0046_connector_pii_tenant_scope.

SECURITY REVIEW (PII / tenant isolation). The one guarantee this migration
exists for -- "`waddles_connector_pii_reader` can never read another tenant's
handle or display name, and a missing tenant fails closed" -- cannot be shown
against mocked SQL text. It needs a live session authenticated as the role,
two tenants' rows in the same tables, and real attempts to read across the
boundary.

**Why a bare container.** Same posture as `test_0044_connector_pii_reader_role`:
hand-bootstrap the minimal pre-0044 state (0043's `hub_users.uuid`, the legacy
identity tables, `communities.tenant_id`), then run 0044's and 0046's real
`upgrade()` by patching `alembic.op.execute` to a live cursor. Full-chain replay
is covered separately by the alembic-chain CI job.
"""

from __future__ import annotations

import importlib.util
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, _free_port, _wait_ready

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_VERSIONS = Path(__file__).resolve().parent.parent / "versions"
_ROLE = "waddles_connector_pii_reader"
_LOGIN = "connector_pii_reader_login"

#: Two tenants, each with one community. Cast:
#: - alice (hub user 1): member of tenant 1 only.
#: - bob   (hub user 2): member of tenant 2 only.
#: - carol (hub user 3): member of BOTH tenants, with a different display name in each.
#: - dave  (hub user 4): linked identity but a member of no community at all.
#: - eve: an UNLINKED platform member of tenant 1 (no hub user).
#: `email`/`password_hash`/`bio` stand in for "every other PII column that must
#: stay unreachable".
_PRE_0044_STATE_SQL = """
CREATE TABLE hub_users (
    id SERIAL PRIMARY KEY,
    uuid UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    email TEXT,
    password_hash TEXT
);
CREATE TABLE hub_user_identities (
    id SERIAL PRIMARY KEY,
    hub_user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    platform_user_id VARCHAR(255) NOT NULL,
    platform_username VARCHAR(255),
    linked_at TIMESTAMP,
    UNIQUE (platform, platform_user_id)
);
CREATE TABLE communities (
    id SERIAL PRIMARY KEY,
    name TEXT,
    tenant_id INTEGER NOT NULL
);
CREATE TABLE community_members (
    id SERIAL PRIMARY KEY,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    user_id VARCHAR(255),
    platform VARCHAR(50),
    platform_user_id VARCHAR(255),
    display_name VARCHAR(255),
    bio TEXT
);
CREATE TABLE app_catalog (
    app_id VARCHAR(255) PRIMARY KEY
);
-- 0030_bundle_app_schemas' real-chain hardening: no implicit PUBLIC access to schema
-- public. Without replicating it, this bare container would mask a role that cannot
-- even resolve a table name (0044 never granted this role USAGE).
REVOKE CREATE, USAGE ON SCHEMA public FROM PUBLIC;

INSERT INTO hub_users (id, email, password_hash) VALUES
    (1, 'alice@example.com', 'x'), (2, 'bob@example.com', 'x'),
    (3, 'carol@example.com', 'x'), (4, 'dave@example.com', 'x');
INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id, platform_username) VALUES
    (1, 'discord', 'd-alice', 'alice#0001'),
    (2, 'discord', 'd-bob', 'bob#0002'),
    (3, 'twitch', 't-carol', 'carol_tv'),
    (4, 'discord', 'd-dave', 'dave#0004');
INSERT INTO communities (id, name, tenant_id) VALUES (10, 'tenant-one-c', 1), (20, 'tenant-two-c', 2);
INSERT INTO community_members (community_id, user_id, platform, platform_user_id, display_name, bio) VALUES
    (10, '1', 'discord', 'd-alice', 'Alice One', 'alice private bio'),
    (20, '2', 'discord', 'd-bob', 'Bob Two', 'bob private bio'),
    (10, '3', 'twitch', 't-carol', 'Carol In One', 'carol bio'),
    (20, '3', 'twitch', 't-carol', 'Carol In Two', 'carol bio'),
    (10, NULL, 'discord', 'd-eve', 'Eve Unlinked', NULL);
"""


def _load(filename: str, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, _VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_0044() -> ModuleType:
    return _load("0044_connector_pii_reader_role.py", "migration_0044_for_0046")


def _migration_0046() -> ModuleType:
    return _load(
        "0046_connector_pii_tenant_scope.py",
        "migration_0046_connector_pii_tenant_scope",
    )


def _run(
    conn: psycopg2.extensions.connection, fn_name: str, module: ModuleType
) -> None:
    """Run `module.<fn_name>()` against `conn` with `op.execute` patched to a live cursor."""
    with conn.cursor() as cur, patch("alembic.op.execute", side_effect=cur.execute):
        getattr(module, fn_name)()


def _set_tenant(cur: psycopg2.extensions.cursor, value: str) -> None:
    """Set the session GUC the way the host does (`waddles.tenant_id`)."""
    cur.execute("SELECT set_config('waddles.tenant_id', %s, false)", (value,))


@pytest.fixture(scope="session")
def pg_db() -> Iterator[PgTestDatabase]:
    """One bare Postgres 17 container, bootstrapped to the pre-0044 state."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    container = "waddles-migtest-0046-pii-scope"
    port = _free_port()
    db = PgTestDatabase(
        host="127.0.0.1",
        port=port,
        user="waddlebot",
        password="testpass123",
        dbname="waddlebot",
    )
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-e",
            f"POSTGRES_USER={db.user}",
            "-e",
            f"POSTGRES_PASSWORD={db.password}",
            "-e",
            f"POSTGRES_DB={db.dbname}",
            "-p",
            f"{port}:5432",
            "postgres:17-bookworm",
        ],
        capture_output=True,
        check=True,
    )
    try:
        _wait_ready(container, db.user, db.dbname)
        subprocess.run(
            ["docker", "exec", "-i", container, "psql", "-U", db.user, "-d", db.dbname],
            input=_PRE_0044_STATE_SQL,
            text=True,
            capture_output=True,
            check=True,
        )
        yield db
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=False
        )


@pytest.fixture(scope="session")
def seeded(pg_db: PgTestDatabase) -> PgTestDatabase:
    """Run 0044 then 0046 for real, and add a login role that is a member of the NOLOGIN reader."""
    conn = psycopg2.connect(pg_db.dsn)
    conn.autocommit = True
    try:
        _run(conn, "upgrade", _migration_0044())
        _run(conn, "upgrade", _migration_0046())
        with conn.cursor() as cur:
            cur.execute(f"CREATE ROLE {_LOGIN} LOGIN PASSWORD 'testpass123'")
            cur.execute(f"GRANT {_ROLE} TO {_LOGIN}")
    finally:
        conn.close()
    return pg_db


@contextmanager
def _reader_session(db: PgTestDatabase) -> Iterator[psycopg2.extensions.cursor]:
    """Open an autocommit session as the login role, `SET ROLE`'d into the reader; always closed."""
    dsn = f"postgresql://{_LOGIN}:testpass123@{db.host}:{db.port}/{db.dbname}"
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(f"SET ROLE {_ROLE}")
        yield cur
    finally:
        conn.close()


@pytest.fixture
def reader_cur(seeded: PgTestDatabase) -> Iterator[psycopg2.extensions.cursor]:
    """A fresh session as the login role, `SET ROLE`'d into the reader, no tenant GUC set yet."""
    with _reader_session(seeded) as cur:
        yield cur


def _handles(cur: psycopg2.extensions.cursor) -> list[str]:
    cur.execute("SELECT handle FROM connector_pii_identities ORDER BY handle")
    return [r[0] for r in cur.fetchall()]


def _display_names(cur: psycopg2.extensions.cursor) -> list[str]:
    cur.execute("SELECT display_name FROM connector_pii_members ORDER BY display_name")
    return [r[0] for r in cur.fetchall()]


class TestMigrationMetadata:
    def test_chains_off_0045_identity_resolution_as_single_head(self) -> None:
        migration = _migration_0046()
        assert migration.revision == "0046_connector_pii_tenant_scope"
        assert migration.down_revision == "0045_identity_resolution"

    def test_revision_id_fits_alembic_version_num_varchar32(self) -> None:
        assert len(_migration_0046().revision) <= 32

    def test_upgrade_never_enables_rls_on_shared_base_tables(self) -> None:
        """RLS on the shared legacy tables would default-deny every other role (see module doc)."""
        with patch("alembic.op.execute") as mock_execute:
            _migration_0046().upgrade()
        sql = "\n".join(str(call.args[0]) for call in mock_execute.call_args_list)
        assert "ROW LEVEL SECURITY" not in sql.upper()


@requires_docker
class TestTenantIsolation:
    def test_tenant_one_sees_only_tenant_one_handles(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        _set_tenant(reader_cur, "1")
        # carol is a member of both tenants, so she is legitimately visible to each;
        # bob (tenant 2 only) and dave (no membership) must not be.
        assert _handles(reader_cur) == ["alice#0001", "carol_tv"]

    def test_tenant_two_sees_only_tenant_two_handles(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        _set_tenant(reader_cur, "2")
        assert _handles(reader_cur) == ["bob#0002", "carol_tv"]

    def test_tenant_a_cannot_read_tenant_b_handle_by_direct_key(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        """A targeted lookup of tenant B's identity from tenant A must return nothing."""
        _set_tenant(reader_cur, "1")
        reader_cur.execute(
            "SELECT handle FROM connector_pii_identities WHERE platform_user_id = 'd-bob'"
        )
        assert reader_cur.fetchall() == []
        reader_cur.execute(
            "SELECT handle FROM connector_pii_identities WHERE handle = 'bob#0002'"
        )
        assert reader_cur.fetchall() == []

    def test_tenant_a_cannot_read_tenant_b_display_name(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        _set_tenant(reader_cur, "1")
        assert _display_names(reader_cur) == [
            "Alice One",
            "Carol In One",
            "Eve Unlinked",
        ]
        reader_cur.execute(
            "SELECT display_name FROM connector_pii_members WHERE display_name = 'Bob Two'"
        )
        assert reader_cur.fetchall() == []

    def test_tenant_b_display_names_are_its_own(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        _set_tenant(reader_cur, "2")
        assert _display_names(reader_cur) == ["Bob Two", "Carol In Two"]

    def test_shared_user_gets_the_tenant_local_display_name(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        for tenant, expected in (("1", "Carol In One"), ("2", "Carol In Two")):
            _set_tenant(reader_cur, tenant)
            reader_cur.execute(
                "SELECT display_name FROM connector_pii_members WHERE platform_user_id = 't-carol'"
            )
            assert reader_cur.fetchall() == [(expected,)]

    def test_hub_user_with_no_membership_is_invisible_to_every_tenant(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        for tenant in ("1", "2", "3"):
            _set_tenant(reader_cur, tenant)
            reader_cur.execute(
                "SELECT handle FROM connector_pii_identities WHERE platform_user_id = 'd-dave'"
            )
            assert reader_cur.fetchall() == []

    def test_unknown_tenant_sees_nothing(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        _set_tenant(reader_cur, "999")
        assert _handles(reader_cur) == []
        assert _display_names(reader_cur) == []

    def test_rows_report_the_session_tenant(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        """`tenant_id` is exposed so a host can assert the reply matches the tenant it asked for."""
        _set_tenant(reader_cur, "2")
        reader_cur.execute("SELECT DISTINCT tenant_id FROM connector_pii_identities")
        assert reader_cur.fetchall() == [(2,)]
        reader_cur.execute("SELECT DISTINCT tenant_id FROM connector_pii_members")
        assert reader_cur.fetchall() == [(2,)]

    def test_tenant_can_be_switched_within_a_session_without_leaking(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        _set_tenant(reader_cur, "1")
        first = _display_names(reader_cur)
        _set_tenant(reader_cur, "2")
        second = _display_names(reader_cur)
        assert not set(first) & {"Bob Two", "Carol In Two"}
        assert not set(second) & {"Alice One", "Carol In One", "Eve Unlinked"}

    def test_join_across_both_views_stays_inside_the_tenant(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        """The uuid -> handle + display_name shape a lookup needs, in one tenant-bounded query."""
        _set_tenant(reader_cur, "1")
        reader_cur.execute(
            "SELECT i.handle, m.display_name FROM connector_pii_identities i "
            "JOIN connector_pii_members m USING (platform, platform_user_id) ORDER BY i.handle"
        )
        assert reader_cur.fetchall() == [
            ("alice#0001", "Alice One"),
            ("carol_tv", "Carol In One"),
        ]


@requires_docker
class TestFailsClosedWithoutTenant:
    def test_unset_tenant_raises_instead_of_returning_rows(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute("SELECT handle FROM connector_pii_identities")
            reader_cur.fetchall()
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute("SELECT display_name FROM connector_pii_members")
            reader_cur.fetchall()

    def test_empty_tenant_raises(self, reader_cur: psycopg2.extensions.cursor) -> None:
        _set_tenant(reader_cur, "")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute("SELECT display_name FROM connector_pii_members")
            reader_cur.fetchall()

    @pytest.mark.parametrize(
        "bad", ["abc", "-1", "1 OR 1=1", "1.5", " 1", "1;", "0x1", "1234567890", "٣"]
    )
    def test_malformed_tenant_raises(
        self, reader_cur: psycopg2.extensions.cursor, bad: str
    ) -> None:
        _set_tenant(reader_cur, bad)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute("SELECT handle FROM connector_pii_identities")
            reader_cur.fetchall()

    def test_failure_message_does_not_echo_the_supplied_value(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        _set_tenant(reader_cur, "not-a-tenant-secret")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc:
            reader_cur.execute("SELECT handle FROM connector_pii_identities")
            reader_cur.fetchall()
        assert "not-a-tenant-secret" not in str(exc.value)


@requires_docker
class TestRawTablesNoLongerReachable:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT platform_username FROM hub_user_identities",
            "SELECT platform_user_id FROM hub_user_identities",
            "SELECT display_name FROM community_members",
            "SELECT platform_user_id FROM community_members",
            "SELECT uuid FROM hub_users",
            "SELECT id FROM hub_users",
            "SELECT email FROM hub_users",
            "SELECT bio FROM community_members",
        ],
    )
    def test_reader_cannot_read_raw_tables(
        self, reader_cur: psycopg2.extensions.cursor, sql: str
    ) -> None:
        _set_tenant(reader_cur, "1")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute(sql)

    def test_reader_cannot_write_through_the_views(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        _set_tenant(reader_cur, "1")
        # Multi-table views are not auto-updatable, so Postgres may refuse before it even
        # reaches the privilege check; either refusal is a denial. The privilege itself is
        # asserted explicitly in `test_reader_privileges_are_select_only_on_the_views`.
        denied = (
            psycopg2.errors.InsufficientPrivilege,
            psycopg2.errors.ObjectNotInPrerequisiteState,
        )
        for sql in (
            "UPDATE connector_pii_members SET display_name = 'x'",
            "DELETE FROM connector_pii_identities",
            "INSERT INTO connector_pii_members (tenant_id) VALUES (1)",
        ):
            with pytest.raises(denied):
                reader_cur.execute(sql)

    def test_reader_privileges_are_select_only_on_the_views(
        self, seeded: PgTestDatabase
    ) -> None:
        conn = psycopg2.connect(seeded.dsn)
        try:
            with conn.cursor() as cur:
                for view in ("connector_pii_identities", "connector_pii_members"):
                    cur.execute(
                        "SELECT has_table_privilege(%s, %s, 'SELECT'), "
                        "has_table_privilege(%s, %s, 'INSERT'), "
                        "has_table_privilege(%s, %s, 'UPDATE'), "
                        "has_table_privilege(%s, %s, 'DELETE')",
                        (_ROLE, view) * 4,
                    )
                    assert cur.fetchone() == (True, False, False, False)
        finally:
            conn.close()

    def test_reader_has_no_access_to_unrelated_tables(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute("SELECT app_id FROM app_catalog")

    def test_reader_has_schema_usage_but_cannot_create_objects(
        self, seeded: PgTestDatabase
    ) -> None:
        conn = psycopg2.connect(seeded.dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT has_schema_privilege(%s, 'public', 'USAGE'), "
                    "has_schema_privilege(%s, 'public', 'CREATE')",
                    (_ROLE, _ROLE),
                )
                assert cur.fetchone() == (True, False)
        finally:
            conn.close()

    def test_base_tables_have_no_row_level_security_enabled(
        self, seeded: PgTestDatabase
    ) -> None:
        conn = psycopg2.connect(seeded.dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN "
                    "('hub_users', 'hub_user_identities', 'community_members') ORDER BY relname"
                )
                assert cur.fetchall() == [
                    ("community_members", False),
                    ("hub_user_identities", False),
                    ("hub_users", False),
                ]
        finally:
            conn.close()

    def test_views_are_security_barriers(self, seeded: PgTestDatabase) -> None:
        conn = psycopg2.connect(seeded.dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT relname, reloptions FROM pg_class WHERE relname IN "
                    "('connector_pii_identities', 'connector_pii_members') ORDER BY relname"
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        assert [r[0] for r in rows] == [
            "connector_pii_identities",
            "connector_pii_members",
        ]
        assert all("security_barrier=true" in (r[1] or []) for r in rows)


@requires_docker
class TestUpgradeDowngrade:
    def test_rerunning_upgrade_is_a_safe_no_op(self, seeded: PgTestDatabase) -> None:
        conn = psycopg2.connect(seeded.dsn)
        conn.autocommit = True
        try:
            _run(conn, "upgrade", _migration_0046())
        finally:
            conn.close()

    def test_downgrade_restores_0044_grants_and_upgrade_rescopes(
        self, seeded: PgTestDatabase
    ) -> None:
        """Round trip: downgrade drops the views and restores 0044's raw grants; upgrade re-narrows."""
        admin = psycopg2.connect(seeded.dsn)
        admin.autocommit = True

        def raw_column_grant() -> bool:
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT has_column_privilege(%s, 'hub_user_identities', "
                    "'platform_username', 'SELECT')",
                    (_ROLE,),
                )
                row = cur.fetchone()
                assert row is not None
                return bool(row[0])

        def view_count() -> int:
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_views WHERE viewname IN "
                    "('connector_pii_identities', 'connector_pii_members')"
                )
                row = cur.fetchone()
                assert row is not None
                return int(row[0])

        try:
            assert not raw_column_grant() and view_count() == 2
            _run(admin, "downgrade", _migration_0046())
            try:
                assert raw_column_grant(), "0044's raw column grant must be restored"
                assert view_count() == 0
            finally:
                _run(admin, "upgrade", _migration_0046())
            assert not raw_column_grant(), "re-upgrade must re-narrow the role"
            assert view_count() == 2
            with _reader_session(seeded) as cur:
                _set_tenant(cur, "1")
                assert _handles(cur) == ["alice#0001", "carol_tv"]
        finally:
            admin.close()
