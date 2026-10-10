"""Real-Postgres tests for 0059_suppression_records (ban-evasion suppression list).

Proves the migration lands on a real Postgres 17 (`pg_docker.migrated_postgres`, which replays
0020 through head), round-trips upgrade -> downgrade -> upgrade, and enforces the properties the
suppression design relies on: a CLOSED restriction vocabulary (no free text / offence detail), a
well-formed keyed code, one row per (tenant, platform, key version, code), per-tenant scoping
(the same code in two tenants is two rows; deleting a tenant deletes its list), and hub-api-only
table privileges (no other service can even read the codes).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

HMAC = "ab" * 32


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _tenant(cur: psycopg2.extensions.cursor) -> int:
    cur.execute("INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (f"t-{uuid.uuid4().hex[:8]}",))
    return int(cur.fetchone()[0])


def _insert(
    cur: psycopg2.extensions.cursor,
    tenant_id: int,
    *,
    platform: str = "discord",
    key_ver: str = "0123456789abcdef",
    hmac: str = HMAC,
    reason: str = "ban",
    expires: str | None = None,
) -> int:
    cur.execute(
        "INSERT INTO suppression_records (tenant_id, platform, key_ver, hmac, reason_enum, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (tenant_id, platform, key_ver, hmac, reason, expires),
    )
    return int(cur.fetchone()[0])


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head, shared by the non-round-trip tests."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0059-suppression-records") as db:
        yield db


@requires_docker
class TestShape:
    def test_columns_are_exactly_the_documented_set(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'suppression_records' ORDER BY column_name"
                )
                columns = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        # No person identifier, no community, no offence text -- the table cannot be joined back
        # to anyone without the master key.
        assert columns == sorted(
            ["id", "tenant_id", "platform", "key_ver", "hmac", "reason_enum", "expires_at", "created_at"]
        )

    def test_defaults_and_nullable_expiry(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                rid = _insert(cur, _tenant(cur))
                cur.execute(
                    "SELECT expires_at, created_at IS NOT NULL FROM suppression_records WHERE id=%s",
                    (rid,),
                )
                assert cur.fetchone() == (None, True)  # NULL = until lifted
        finally:
            conn.close()

    def test_an_expiry_can_be_set(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                rid = _insert(cur, _tenant(cur), expires="2031-01-01T00:00:00Z")
                cur.execute("SELECT expires_at IS NOT NULL FROM suppression_records WHERE id=%s", (rid,))
                assert cur.fetchone() == (True,)
        finally:
            conn.close()

    def test_the_expiry_index_exists(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT indexdef FROM pg_indexes WHERE tablename='suppression_records' "
                    "AND indexname='idx_suppression_records_expiry'"
                )
                [(definition,)] = cur.fetchall()
        finally:
            conn.close()
        assert "expires_at" in definition and "WHERE" in definition  # partial: only dated rows


@requires_docker
class TestClosedVocabulary:
    @pytest.mark.parametrize("reason", ["ban", "auto_ban"])
    def test_the_two_restriction_types_are_accepted(self, pg_db: PgTestDatabase, reason: str) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                _insert(cur, _tenant(cur), reason=reason)
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "reason",
        ["", "BAN", "timeout", "harassment", "spam: posted slurs in #general", "ban ", "other"],
    )
    def test_free_text_and_unknown_types_are_rejected(
        self, pg_db: PgTestDatabase, reason: str
    ) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tid = _tenant(cur)
            # Short unknown values trip the CHECK; text longer than the 16-char column cannot even
            # be stored. Either way free text never lands in the table.
            with (
                pytest.raises(
                    (psycopg2.errors.CheckViolation, psycopg2.errors.StringDataRightTruncation)
                ),
                conn.cursor() as cur,
            ):
                _insert(cur, tid, reason=reason)
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "code",
        ["", "ab" * 31, "ab" * 33, "AB" * 32, "zz" * 32, "12345678901234567", "ab" * 31 + "a!"],
    )
    def test_a_malformed_code_is_rejected(self, pg_db: PgTestDatabase, code: str) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tid = _tenant(cur)
            with (
                pytest.raises(
                    (psycopg2.errors.CheckViolation, psycopg2.errors.StringDataRightTruncation)
                ),
                conn.cursor() as cur,
            ):
                _insert(cur, tid, hmac=code)
        finally:
            conn.close()


@requires_docker
class TestPerTenantScoping:
    def test_one_row_per_tenant_platform_key_version_and_code(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tid = _tenant(cur)
                _insert(cur, tid)
            with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
                _insert(cur, tid)  # duplicate identity -> upsert in the app, never a second row
        finally:
            conn.close()

    def test_the_same_code_in_another_tenant_platform_or_key_version_is_a_different_row(
        self, pg_db: PgTestDatabase
    ) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                a, b = _tenant(cur), _tenant(cur)
                _insert(cur, a)
                _insert(cur, b)
                _insert(cur, a, platform="twitch")
                _insert(cur, a, key_ver="second-key-version")
                cur.execute(
                    "SELECT count(*) FROM suppression_records WHERE tenant_id IN (%s, %s)", (a, b)
                )
                assert cur.fetchone()[0] == 4
        finally:
            conn.close()

    def test_deleting_a_tenant_deletes_its_suppression_list(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                keep, drop = _tenant(cur), _tenant(cur)
                _insert(cur, keep)
                _insert(cur, drop)
                cur.execute("DELETE FROM tenants WHERE id=%s", (drop,))
                cur.execute(
                    "SELECT tenant_id FROM suppression_records WHERE tenant_id IN (%s, %s)",
                    (keep, drop),
                )
                assert [r[0] for r in cur.fetchall()] == [keep]
        finally:
            conn.close()

    def test_a_row_needs_an_existing_tenant(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with pytest.raises(psycopg2.errors.ForeignKeyViolation), conn.cursor() as cur:
                _insert(cur, 999999)
        finally:
            conn.close()


@requires_docker
class TestPrivileges:
    def test_only_hub_api_and_migration_runner_hold_privileges(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT grantee FROM information_schema.role_table_grants "
                    "WHERE table_name = 'suppression_records' AND grantee <> current_user"
                )
                grantees = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
        assert grantees <= {"hub_api", "migration_runner"}, grantees
        assert "hub_api" in grantees
        assert "PUBLIC" not in grantees  # the key never leaves hub-api: no one else can read codes

    def test_hub_api_may_use_the_id_sequence(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT has_sequence_privilege('hub_api', 'suppression_records_id_seq', 'USAGE')"
                )
                assert cur.fetchone() == (True,)
                cur.execute(
                    "SELECT has_table_privilege('svc_ingest', 'suppression_records', 'SELECT'), "
                    "has_table_privilege('svc_process', 'suppression_records', 'SELECT'), "
                    "has_table_privilege('webui', 'suppression_records', 'SELECT')"
                )
                assert cur.fetchone() == (False, False, False)
        finally:
            conn.close()


@requires_docker
class TestRoundTrip:
    def test_downgrade_then_upgrade(self) -> None:
        with migrated_postgres("0059-suppression-roundtrip") as db:
            alembic_cli("downgrade", "0052_bundle_identity_resolve", dsn=db.dsn)
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('suppression_records')")
                    assert cur.fetchone() == (None,), "downgrade left the table behind"
            finally:
                conn.close()
            alembic_cli("upgrade", "head", dsn=db.dsn)
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('suppression_records') IS NOT NULL")
                    assert cur.fetchone() == (True,)
            finally:
                conn.close()
