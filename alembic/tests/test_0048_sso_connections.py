"""Real-Postgres tests for 0048_sso_connections (enterprise SSO connections + identities).

Proves the migration lands on a real Postgres 17 (`pg_docker.migrated_postgres`, which
replays 0020 through head), round-trips upgrade -> downgrade -> upgrade, and enforces the
constraints the SSO service relies on: per-tenant unique display names, protocol CHECK,
`enabled` defaulting FALSE, one identity link per (connection, subject), cascade on
connection delete (so no orphaned identities) and on `hub_users` delete (so an erasure
request leaves no subject behind), and hub-api-only table privileges.
"""

from __future__ import annotations

import json
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


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _tenant(cur: psycopg2.extensions.cursor, slug: str) -> int:
    cur.execute("INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (slug,))
    return int(cur.fetchone()[0])


def _user(cur: psycopg2.extensions.cursor, username: str) -> int:
    # The pg_docker bootstrap `hub_users` is deliberately minimal (id, username, display_name).
    cur.execute("INSERT INTO hub_users (username) VALUES (%s) RETURNING id", (username,))
    return int(cur.fetchone()[0])


def _connection(
    cur: psycopg2.extensions.cursor, tenant_id: int, name: str = "Acme", protocol: str = "oidc"
) -> int:
    cur.execute(
        "INSERT INTO sso_connections (public_id, tenant_id, protocol, display_name, config) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (str(uuid.uuid4()), tenant_id, protocol, name, json.dumps({"issuer": "https://i"})),
    )
    return int(cur.fetchone()[0])


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head, shared by the non-round-trip tests."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0048-sso-connections") as db:
        yield db


@requires_docker
class TestSsoConnections:
    def test_defaults_enabled_false_and_timestamps(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cid = _connection(cur, _tenant(cur, f"t-{uuid.uuid4().hex[:8]}"))
                cur.execute("SELECT enabled, created_at IS NOT NULL, secret_ciphertext FROM sso_connections WHERE id=%s", (cid,))
                assert cur.fetchone() == (False, True, None)
        finally:
            conn.close()

    def test_display_name_is_unique_per_tenant_only(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                a = _tenant(cur, f"a-{uuid.uuid4().hex[:8]}")
                b = _tenant(cur, f"b-{uuid.uuid4().hex[:8]}")
                _connection(cur, a, "Same")
                _connection(cur, b, "Same")  # different tenant: fine
            with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
                _connection(cur, a, "Same")
        finally:
            conn.close()

    @pytest.mark.parametrize("protocol", ["ldap", "", "SAML"])
    def test_protocol_check_constraint(self, pg_db: PgTestDatabase, protocol: str) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tid = _tenant(cur, f"p-{uuid.uuid4().hex[:8]}")
            with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as cur:
                _connection(cur, tid, "bad", protocol)
        finally:
            conn.close()

    @pytest.mark.parametrize("protocol", ["saml", "oidc", "google"])
    def test_valid_protocols(self, pg_db: PgTestDatabase, protocol: str) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                _connection(cur, _tenant(cur, f"v-{uuid.uuid4().hex[:8]}"), protocol, protocol)
        finally:
            conn.close()

    def test_public_id_is_unique(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tid = _tenant(cur, f"u-{uuid.uuid4().hex[:8]}")
                public_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO sso_connections (public_id, tenant_id, protocol, display_name) VALUES (%s,%s,'oidc','x')",
                    (public_id, tid),
                )
            with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sso_connections (public_id, tenant_id, protocol, display_name) VALUES (%s,%s,'oidc','y')",
                    (public_id, tid),
                )
        finally:
            conn.close()

    def test_deleting_a_tenant_cascades_to_its_connections(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tid = _tenant(cur, f"c-{uuid.uuid4().hex[:8]}")
                cid = _connection(cur, tid)
                cur.execute("DELETE FROM tenants WHERE id=%s", (tid,))
                cur.execute("SELECT count(*) FROM sso_connections WHERE id=%s", (cid,))
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()


@requires_docker
class TestSsoIdentities:
    def _setup(self, cur: psycopg2.extensions.cursor) -> tuple[int, int]:
        tid = _tenant(cur, f"i-{uuid.uuid4().hex[:8]}")
        return _connection(cur, tid), _user(cur, f"u-{uuid.uuid4().hex[:8]}")

    def test_one_link_per_connection_and_subject(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cid, uid = self._setup(cur)
                cur.execute("INSERT INTO sso_identities (connection_id, subject, hub_user_id) VALUES (%s,'s1',%s)", (cid, uid))
            with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
                cur.execute("INSERT INTO sso_identities (connection_id, subject, hub_user_id) VALUES (%s,'s1',%s)", (cid, uid))
        finally:
            conn.close()

    def test_same_subject_on_two_connections_is_allowed(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                c1, uid = self._setup(cur)
                c2, _ = self._setup(cur)
                for cid in (c1, c2):
                    cur.execute("INSERT INTO sso_identities (connection_id, subject, hub_user_id) VALUES (%s,'same',%s)", (cid, uid))
        finally:
            conn.close()

    def test_connection_delete_cascades_to_identities(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cid, uid = self._setup(cur)
                cur.execute("INSERT INTO sso_identities (connection_id, subject, hub_user_id) VALUES (%s,'s',%s)", (cid, uid))
                cur.execute("DELETE FROM sso_connections WHERE id=%s", (cid,))
                cur.execute("SELECT count(*) FROM sso_identities WHERE connection_id=%s", (cid,))
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()

    def test_user_erasure_leaves_no_subject_behind(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cid, uid = self._setup(cur)
                cur.execute("INSERT INTO sso_identities (connection_id, subject, hub_user_id) VALUES (%s,'alice@acme.test',%s)", (cid, uid))
                cur.execute("DELETE FROM hub_users WHERE id=%s", (uid,))
                cur.execute("SELECT count(*) FROM sso_identities WHERE subject='alice@acme.test' AND connection_id=%s", (cid,))
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()

    def test_identity_requires_an_existing_user_and_connection(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cid, _ = self._setup(cur)
            with pytest.raises(psycopg2.errors.ForeignKeyViolation), conn.cursor() as cur:
                cur.execute("INSERT INTO sso_identities (connection_id, subject, hub_user_id) VALUES (%s,'s',999999)", (cid,))
            with pytest.raises(psycopg2.errors.ForeignKeyViolation), conn.cursor() as cur:
                cur.execute("INSERT INTO sso_identities (connection_id, subject, hub_user_id) VALUES (999999,'s',1)")
        finally:
            conn.close()


@requires_docker
class TestPrivileges:
    def test_only_hub_api_and_migration_runner_hold_privileges(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, grantee, privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_name IN ('sso_connections','sso_identities') AND grantee <> current_user"
                )
                grants = {(t, g) for t, g, _ in cur.fetchall()}
        finally:
            conn.close()
        grantees = {g for _, g in grants}
        assert grantees <= {"hub_api", "migration_runner"}, grantees
        assert {t for t, _ in grants} == {"sso_connections", "sso_identities"}
        assert "PUBLIC" not in grantees


@requires_docker
class TestRoundTrip:
    def test_downgrade_then_upgrade(self) -> None:
        with migrated_postgres("0048-sso-roundtrip") as db:
            alembic_cli("downgrade", "0047_builtin_handler_paths", dsn=db.dsn)
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('sso_connections'), to_regclass('sso_identities')")
                    assert cur.fetchone() == (None, None), "downgrade left a table behind"
            finally:
                conn.close()
            alembic_cli("upgrade", "head", dsn=db.dsn)
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('sso_connections') IS NOT NULL, to_regclass('sso_identities') IS NOT NULL")
                    assert cur.fetchone() == (True, True)
            finally:
                conn.close()
