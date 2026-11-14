"""Tests for 0109_platform_vault: the platform secret store schema and its grant model.

Two layers, each stating its denominator:

* static (no database, run anywhere) -- revision metadata and a single head, the catalog denies the
  vault relations to every legacy role, the migration's declared grants carry no DELETE/TRUNCATE,
  the SQL it emits revokes from PUBLIC and its view exposes no secret column;
* real Postgres (`requires_docker`, skipped without docker) -- on a TRUE fresh `alembic` replay: the
  full grant matrix over every role in the cluster, the data-plane role cannot read `sealed_value`
  (with a control proving the same statement works for hub-api), legacy roles cannot even see the
  schema, DELETE/TRUNCATE are refused for every role (owner included), state/rollback guards,
  the CHECK constraints, the view contents, the refused downgrade, and `DROP ROLE` after downgrade
  (a leftover grant makes it fail -- a prior bug -- and the same drop FAILS while grants remain).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import (
    DOCKER_AVAILABLE,
    REPO_ROOT,
    PgTestDatabase,
    _free_port,
    _wait_ready,
    alembic_cli,
    load_service_roles_module,
    migrated_postgres,
)

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION = (
    Path(__file__).resolve().parent.parent / "versions" / "0109_platform_vault.py"
)
_PRIOR = "0107_bundle_config_db_reads"
_KEK = "0123456789abcdef"
_TABLE_PRIVS = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
_TABLES = ("secrets", "secret_events", "root_marker")


def _load() -> ModuleType:
    """Import the migration module by path."""
    spec = importlib.util.spec_from_file_location("migration_0109", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(fn_name: str) -> list[str]:
    """Every SQL string the migration's `upgrade`/`downgrade` passes to `op.execute`."""
    seen: list[str] = []
    with patch("alembic.op.execute", side_effect=seen.append):
        getattr(_load(), fn_name)()
    return seen


# ---- static -----------------------------------------------------------------------------


def test_metadata_and_single_head() -> None:
    """Revision id <= 32 chars, chained off 0107, and the graph has exactly one head."""
    m = _load()
    assert (m.revision, m.down_revision) == ("0109_platform_vault", _PRIOR)
    assert len(m.revision) <= 32
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == [m.revision], f"alembic heads: {heads}"


def test_catalog_denies_the_vault_to_every_legacy_role_and_names_no_vault_grant() -> (
    None
):
    """Defense in depth: legacy_deny_tables lists the relations; no catalog role grants them."""
    catalog = load_service_roles_module().load_catalog()
    schema = _load().SCHEMA
    for table in _TABLES:
        assert f"{schema}.{table}" in catalog.legacy_deny_tables
    examined = 0
    for spec in catalog.roles:
        examined += 1
        names = {
            *spec.tables,
            *spec.views,
            *spec.columns,
            *spec.deny_tables,
            *spec.allow_tables,
        }
        assert not {n for n in names if n.startswith(f"{schema}.")}, spec.name
        assert not spec.allow_tables & {f"{schema}.{t}" for t in _TABLES}, spec.name
    assert examined >= 30, "denominator: every catalog role"
    by_name = {r.name: r for r in catalog.roles}
    m = _load()
    assert m.HUB_API_ROLE in by_name
    for role in m.PUBLIC_KEY_READERS:
        assert role in by_name, f"{role} is not a catalog role"
        assert not by_name[role].public_dml, (
            f"{role} is public_dml; it must stay a strict role"
        )


def test_declared_grants_have_no_delete_truncate_or_all() -> None:
    """hub-api never holds DELETE/TRUNCATE; events are INSERT/SELECT only (append-only)."""
    grants = _load().HUB_API_GRANTS
    assert set(grants) == set(_TABLES)
    for table, privs in grants.items():
        assert not {"DELETE", "TRUNCATE", "ALL"} & set(privs), table
    assert grants["secret_events"] == ("SELECT", "INSERT")


def test_emitted_sql_shape() -> None:
    """The statements revoke from PUBLIC, never GRANT ALL / DELETE / TRUNCATE, and the view is a
    security-barrier projection of public columns only."""
    up = _statements("upgrade")
    assert len(up) >= 30, "denominator: upgrade statements examined"
    text = "\n".join(up)
    assert "REVOKE ALL ON SCHEMA platform_vault FROM PUBLIC" in text
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA platform_vault FROM PUBLIC" in text
    grants = [s for s in up if "GRANT " in s and "REVOKE" not in s]
    assert grants, "no GRANT statements found"
    for stmt in grants:
        upper = stmt.upper()
        assert "GRANT ALL" not in upper
        assert "DELETE" not in upper and "TRUNCATE" not in upper
    view_sql = next(s for s in up if "CREATE VIEW" in s)
    assert "security_barrier = true" in view_sql
    assert "security_invoker" not in text
    for secret_column in ("sealed_value", "fingerprint", "kek_id", "actor_uuid"):
        assert secret_column not in view_sql.split("FROM")[0], secret_column
    assert "state IN (2, 3)" in view_sql
    for trigger in (
        "secrets_no_delete",
        "secrets_no_truncate",
        "secret_events_append_only",
    ):
        assert trigger in text
    assert "root_marker_no_delete" in text and "secrets_guard" in text


def test_downgrade_refuses_to_drop_a_populated_store_unless_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the env confirmation the first downgrade statement is the refusal guard."""
    monkeypatch.delenv("WADDLES_ALLOW_PLATFORM_VAULT_DROP", raising=False)
    guarded = _statements("downgrade")
    monkeypatch.setenv("WADDLES_ALLOW_PLATFORM_VAULT_DROP", "1")
    confirmed = _statements("downgrade")
    assert "refusing to drop" in guarded[0]
    assert len(guarded) == 3 and len(confirmed) == 2
    assert all("refusing to drop" not in s for s in confirmed)
    assert guarded[-1] == "DROP SCHEMA IF EXISTS platform_vault CASCADE"


# ---- real Postgres ------------------------------------------------------------------------


def _connect(db: PgTestDatabase, user: str, password: str) -> Any:
    """Autocommit connection (one denied statement never poisons the next)."""
    conn = psycopg2.connect(
        host=db.host,
        port=db.port,
        user=user,
        password=password,
        dbname=db.dbname,
        connect_timeout=10,
    )
    conn.autocommit = True
    return conn


def _owner(db: PgTestDatabase) -> Any:
    """Owner (DDL) connection, autocommit."""
    return _connect(db, db.user, db.password)


def _as(db: PgTestDatabase, role: str) -> Any:
    """Connection authenticated AS the service role."""
    return _connect(db, role, db.service_role_passwords[role])


def _envelope(
    kek: str = _KEK, version: int = 1, tail: int = 17
) -> psycopg2.extensions.Binary:
    """A structurally valid vault envelope (the database only checks shape, never decrypts)."""
    return psycopg2.Binary(
        b"WPS" + bytes([version]) + bytes.fromhex(kek) + b"\x00" * 12 + b"\xaa" * tail
    )


_INSERT = (
    "INSERT INTO platform_vault.secrets (secret_type, scope_id, version, state, algorithm, "
    "sealed_value, kek_id, seal_version, kid, public_material, fingerprint, reason, "
    "activated_at, retired_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, 1, "
    "CASE WHEN %s IN (2, 3, 4) THEN now() END, CASE WHEN %s = 4 THEN now() END)"
)


def _insert_secret(
    cur: Any,
    *,
    secret_type: int,
    version: int,
    state: int = 2,
    scope_id: int | None = None,
    algorithm: int = 3,
    sealed: Any = "default",
    kek: str = _KEK,
    kid: str | None = None,
    public: bytes | None = None,
) -> None:
    """Insert one `secrets` row (symmetric by default; pass kid+public for algorithm 1/2)."""
    value = None if state == 5 else (_envelope(kek) if sealed == "default" else sealed)
    cur.execute(
        _INSERT,
        (
            secret_type,
            scope_id,
            version,
            state,
            algorithm,
            value,
            kek,
            kid,
            None if public is None else psycopg2.Binary(public),
            psycopg2.Binary(b"\x01" * 32),
            state,
            state,
        ),
    )


def _insert_key(
    cur: Any, *, secret_type: int, version: int, state: int, kid: str
) -> None:
    """Insert an ES256-shaped row with public material."""
    _insert_secret(
        cur,
        secret_type=secret_type,
        version=version,
        state=state,
        algorithm=2,
        kid=kid,
        public=b"\x04" * 64,
    )


def _chain_head(cur: Any) -> bytes | None:
    """The `chain_hash` of the newest event (the one nothing extends), or None on an empty log."""
    cur.execute(
        "SELECT chain_hash FROM platform_vault.secret_events e WHERE NOT EXISTS "
        "(SELECT 1 FROM platform_vault.secret_events n WHERE n.prev_chain_hash = e.chain_hash)"
    )
    row = cur.fetchone()
    return None if row is None else bytes(row[0])


def _append_event(cur: Any, *, secret_type: int) -> bytes:
    """Append one event extending the current chain head (the log has a single genesis)."""
    head = _chain_head(cur)
    new_hash = os.urandom(32)
    cur.execute(
        "INSERT INTO platform_vault.secret_events (secret_type, event_type, outcome, "
        "prev_chain_hash, chain_hash) VALUES (%s, 1, 1, %s, %s)",
        (
            secret_type,
            None if head is None else psycopg2.Binary(head),
            psycopg2.Binary(new_hash),
        ),
    )
    return new_hash


@pytest.fixture(scope="module")
def real_chain() -> Iterator[PgTestDatabase]:
    """Fresh full-chain Postgres at head (incl. 0109)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0109-platform-vault") as db:
        yield db


def _expected(role: str) -> dict[str, set[str]]:
    """Expected privilege set per relation for `role` (everyone else holds nothing)."""
    m = _load()
    expected: dict[str, set[str]] = {t: set() for t in (*_TABLES, m.VIEW)}
    if role == m.HUB_API_ROLE:
        for table, privs in m.HUB_API_GRANTS.items():
            expected[table] = set(privs)
    if role in m.PUBLIC_KEY_READERS:
        expected[m.VIEW] = {"SELECT"}
    return expected


@requires_docker
def test_grant_matrix_over_every_role(real_chain: PgTestDatabase) -> None:
    """EVERY non-superuser role in the cluster holds exactly the documented privileges."""
    m = _load()
    catalog_roles = set(load_service_roles_module().load_catalog().names)
    owner = _owner(real_chain)
    with owner.cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE NOT rolsuper AND rolname !~ '^pg_' "
            "AND rolname <> current_user ORDER BY 1"
        )
        roles = [r[0] for r in cur.fetchall()]
        assert catalog_roles <= set(roles), "denominator: every catalog role exists"
        assert len(roles) >= 30
        for role in roles:
            expected = _expected(role)
            for relation, want in expected.items():
                have = set()
                for priv in _TABLE_PRIVS:
                    cur.execute(
                        "SELECT has_table_privilege(%s, %s, %s)",
                        (role, f"{m.SCHEMA}.{relation}", priv),
                    )
                    if cur.fetchone()[0]:
                        have.add(priv)
                assert have == want, (
                    f"{role} on {relation}: {sorted(have)} != {sorted(want)}"
                )
            cur.execute(
                "SELECT has_column_privilege(%s, 'platform_vault.secrets', 'sealed_value', "
                "'SELECT')",
                (role,),
            )
            assert bool(cur.fetchone()[0]) == (role == m.HUB_API_ROLE), role
            cur.execute(
                "SELECT has_schema_privilege(%s, %s, 'CREATE')", (role, m.SCHEMA)
            )
            assert not cur.fetchone()[0], f"{role} can CREATE in the vault schema"
            cur.execute(
                "SELECT has_schema_privilege(%s, %s, 'USAGE')", (role, m.SCHEMA)
            )
            assert bool(cur.fetchone()[0]) == (
                role == m.HUB_API_ROLE or role in m.PUBLIC_KEY_READERS
            ), role
    owner.close()


@requires_docker
def test_data_plane_cannot_read_sealed_value_but_hub_api_can(
    real_chain: PgTestDatabase,
) -> None:
    """A data-plane role is denied `sealed_value` and the base table; the SAME statement works as
    hub-api (negative control), and the data-plane role can read the view."""
    owner = _owner(real_chain)
    with owner.cursor() as cur:
        _insert_key(cur, secret_type=901, version=1, state=2, kid="dp-test-1")
    owner.close()
    statement = (
        "SELECT sealed_value FROM platform_vault.secrets WHERE secret_type = 901"
    )
    hub = _as(real_chain, "waddles_hub_api")
    with hub.cursor() as cur:
        cur.execute(statement)
        assert len(cur.fetchall()) == 1, "control: hub-api reads the same row"
    hub.close()
    for role in _load().PUBLIC_KEY_READERS:
        conn = _as(real_chain, role)
        for denied in (
            statement,
            "SELECT * FROM platform_vault.secrets",
            "SELECT fingerprint FROM platform_vault.secrets",
            "SELECT * FROM platform_vault.secret_events",
            "SELECT * FROM platform_vault.root_marker",
        ):
            with (
                conn.cursor() as cur,
                pytest.raises(psycopg2.errors.InsufficientPrivilege),
            ):
                cur.execute(denied)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kid, state FROM platform_vault.public_keys WHERE secret_type = 901"
            )
            assert cur.fetchall() == [("dp-test-1", 2)], role
        conn.close()


@requires_docker
def test_legacy_role_cannot_touch_the_vault(real_chain: PgTestDatabase) -> None:
    """A public_dml legacy role fails on schema USAGE for read, delete and truncate alike."""
    conn = _as(real_chain, "waddles_legacy_discord_collector")
    for stmt in (
        "SELECT * FROM platform_vault.secrets",
        "SELECT * FROM platform_vault.public_keys",
        "DELETE FROM platform_vault.secrets",
        "TRUNCATE platform_vault.secrets",
        "UPDATE platform_vault.root_marker SET reinit_count = reinit_count + 1",
    ):
        with conn.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(stmt)
    conn.close()


@requires_docker
def test_hub_api_privilege_boundary(real_chain: PgTestDatabase) -> None:
    """hub-api can insert/update secrets and append events, never delete, truncate or edit events."""
    hub = _as(real_chain, "waddles_hub_api")
    with hub.cursor() as cur:
        _insert_secret(cur, secret_type=902, version=1, state=1)
        cur.execute(
            "UPDATE platform_vault.secrets SET external_sync_state = 2 WHERE secret_type = 902"
        )
        _append_event(cur, secret_type=902)
    for stmt in (
        "DELETE FROM platform_vault.secrets",
        "TRUNCATE platform_vault.secrets",
        "UPDATE platform_vault.secret_events SET outcome = 2",
        "DELETE FROM platform_vault.secret_events",
        "TRUNCATE platform_vault.secret_events",
        "DELETE FROM platform_vault.root_marker",
    ):
        with hub.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(stmt)
    hub.close()


@requires_docker
def test_triggers_refuse_delete_truncate_even_for_the_owner(
    real_chain: PgTestDatabase,
) -> None:
    """The owner (who holds every privilege) is refused by the guards: wipes are impossible."""
    owner = _owner(real_chain)
    with owner.cursor() as cur:
        _insert_secret(cur, secret_type=903, version=1)
        _append_event(cur, secret_type=903)
        cur.execute(
            "INSERT INTO platform_vault.root_marker (kek_id, check_mac) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (_KEK, psycopg2.Binary(b"\x02" * 32)),
        )
    refused = (
        "DELETE FROM platform_vault.secrets WHERE secret_type = 903",
        "TRUNCATE platform_vault.secrets CASCADE",
        "UPDATE platform_vault.secret_events SET outcome = 2",
        "DELETE FROM platform_vault.secret_events",
        "TRUNCATE platform_vault.secret_events",
        "DELETE FROM platform_vault.root_marker",
        "TRUNCATE platform_vault.root_marker",
    )
    for stmt in refused:
        with (
            owner.cursor() as cur,
            pytest.raises(psycopg2.errors.IntegrityConstraintViolation),
        ):
            cur.execute(stmt)
    with owner.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM platform_vault.secrets WHERE secret_type = 903"
        )
        assert cur.fetchone()[0] == 1, "the row survived every refused wipe"
    owner.close()


@requires_docker
def test_state_only_moves_forward_and_rollback_is_refused(
    real_chain: PgTestDatabase,
) -> None:
    """Backward transitions, reviving a destroyed row, activating an older version and editing
    identity columns are all refused; the legal forward path works."""
    owner = _owner(real_chain)
    refused = psycopg2.errors.IntegrityConstraintViolation
    with owner.cursor() as cur:
        _insert_secret(cur, secret_type=904, version=1, state=2, scope_id=7)
        _insert_secret(cur, secret_type=904, version=2, state=1, scope_id=7)
        # legal: v1 active -> verify-only, then v2 pending -> active (older never revived below)
        cur.execute(
            "UPDATE platform_vault.secrets SET state = 3, rotated_at = now() "
            "WHERE secret_type = 904 AND version = 1"
        )
        cur.execute(
            "UPDATE platform_vault.secrets SET state = 2, activated_at = now() "
            "WHERE secret_type = 904 AND version = 2"
        )
    for stmt in (
        # backward: verify-only -> active, active -> pending
        "UPDATE platform_vault.secrets SET state = 2 WHERE secret_type = 904 AND version = 1",
        "UPDATE platform_vault.secrets SET state = 1 WHERE secret_type = 904 AND version = 2",
        # identity / public columns are immutable
        "UPDATE platform_vault.secrets SET version = 9 WHERE secret_type = 904 AND version = 2",
        "UPDATE platform_vault.secrets SET secret_type = 905 WHERE secret_type = 904",
        "UPDATE platform_vault.secrets SET scope_id = 8 WHERE secret_type = 904",
        # the keyed fingerprint may change only together with a re-wrap (a kek_id change)
        (
            "UPDATE platform_vault.secrets SET fingerprint = decode(repeat('02', 32), 'hex') "
            "WHERE secret_type = 904 AND version = 2"
        ),
        (
            "UPDATE platform_vault.secrets SET activated_at = NULL "
            "WHERE secret_type = 904 AND version = 2"
        ),
    ):
        with owner.cursor() as cur, pytest.raises(refused):
            cur.execute(stmt)
    with owner.cursor() as cur:
        # inserting an ACTIVE version lower than an activated one is a rollback
        _insert_secret(cur, secret_type=904, version=3, state=2, scope_id=70)
        with pytest.raises(refused):
            _insert_secret(cur, secret_type=904, version=2, state=2, scope_id=70)
    owner.close()


@requires_docker
def test_rollback_to_an_older_version_and_destroyed_immutability(
    real_chain: PgTestDatabase,
) -> None:
    """A pending OLDER version cannot be activated once a newer one was; destroyed is terminal
    and crypto-shredded (sealed_value NULL, enforced both ways)."""
    owner = _owner(real_chain)
    refused = psycopg2.errors.IntegrityConstraintViolation
    with owner.cursor() as cur:
        _insert_secret(cur, secret_type=906, version=1, state=1)
        _insert_secret(cur, secret_type=906, version=2, state=2)
        with pytest.raises(refused):
            cur.execute(
                "UPDATE platform_vault.secrets SET state = 2, activated_at = now() "
                "WHERE secret_type = 906 AND version = 1"
            )
        # destroy v1: sealed_value must be NULLed in the same statement (CHECK), then immutable
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "UPDATE platform_vault.secrets SET state = 5 WHERE secret_type = 906 AND version = 1"
            )
        cur.execute(
            "UPDATE platform_vault.secrets SET state = 5, sealed_value = NULL "
            "WHERE secret_type = 906 AND version = 1"
        )
        with pytest.raises(refused):
            cur.execute(
                "UPDATE platform_vault.secrets SET external_sync_state = 1 "
                "WHERE secret_type = 906 AND version = 1"
            )
    owner.close()


@requires_docker
def test_check_and_unique_constraints(real_chain: PgTestDatabase) -> None:
    """Envelope prefix/version/kek-id, state range, key shape, one-active and kid uniqueness
    (NULL scope included) are all enforced by the database."""
    owner = _owner(real_chain)
    bad_envelopes = (
        psycopg2.Binary(
            b"XPS\x01" + bytes.fromhex(_KEK) + b"\x00" * 12 + b"\xaa" * 17
        ),  # prefix
        psycopg2.Binary(
            b"WPS\x02" + bytes.fromhex(_KEK) + b"\x00" * 12 + b"\xaa" * 17
        ),  # version
        _envelope(kek="fedcba9876543210"),  # header kek id != the stored kek_id column
        psycopg2.Binary(
            b"WPS\x01" + bytes.fromhex(_KEK) + b"\x00" * 12 + b"\xaa" * 16
        ),  # short
        psycopg2.Binary(b"plaintext-secret-stored-by-mistake-0123456789"),
    )
    with owner.cursor() as cur:
        for i, sealed in enumerate(bad_envelopes):
            with pytest.raises(psycopg2.errors.CheckViolation):
                _insert_secret(
                    cur, secret_type=907, version=100 + i, state=1, sealed=sealed
                )
        with pytest.raises(psycopg2.errors.CheckViolation):  # state out of range
            cur.execute(
                _INSERT,
                (
                    907,
                    None,
                    200,
                    6,
                    3,
                    _envelope(),
                    _KEK,
                    None,
                    None,
                    psycopg2.Binary(b"\x01" * 32),
                    6,
                    6,
                ),
            )
        with pytest.raises(
            psycopg2.errors.CheckViolation
        ):  # asymmetric needs kid + public key
            _insert_secret(cur, secret_type=907, version=201, state=1, algorithm=2)
        with pytest.raises(
            psycopg2.errors.CheckViolation
        ):  # symmetric must not carry a key
            _insert_secret(
                cur,
                secret_type=907,
                version=202,
                state=1,
                kid="oops",
                public=b"\x04" * 64,
            )
        # one ACTIVE per (type, scope) -- NULL scope collides with NULL scope
        _insert_secret(cur, secret_type=908, version=1, state=2, scope_id=None)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert_secret(cur, secret_type=908, version=2, state=2, scope_id=None)
        with pytest.raises(
            psycopg2.errors.UniqueViolation
        ):  # same (type, NULL scope, version)
            _insert_secret(cur, secret_type=908, version=1, state=1, scope_id=None)
        _insert_secret(
            cur, secret_type=908, version=1, state=2, scope_id=0
        )  # scope 0 != NULL
        # kid unique within (type, scope)
        _insert_key(cur, secret_type=909, version=1, state=1, kid="same-kid")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert_key(cur, secret_type=909, version=2, state=1, kid="same-kid")
        with pytest.raises(psycopg2.errors.CheckViolation):  # kid charset
            _insert_key(cur, secret_type=909, version=3, state=1, kid="bad kid!")
    owner.close()


@requires_docker
def test_state_timestamps_check(real_chain: PgTestDatabase) -> None:
    """Active/verify-only/retired rows must carry activated_at (retired also retired_at)."""
    owner = _owner(real_chain)
    with owner.cursor() as cur:
        _insert_secret(cur, secret_type=910, version=1, state=1)
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "UPDATE platform_vault.secrets SET state = 2 "
                "WHERE secret_type = 910 AND version = 1"
            )
    owner.close()


@requires_docker
def test_public_keys_view_exposes_only_active_and_verify_only_public_columns(
    real_chain: PgTestDatabase,
) -> None:
    """Pending/retired/destroyed and symmetric secrets are invisible; no secret column exists."""
    owner = _owner(real_chain)
    with owner.cursor() as cur:
        _insert_key(cur, secret_type=911, version=1, state=2, kid="view-active")
        _insert_key(cur, secret_type=911, version=2, state=1, kid="view-pending")
        _insert_secret(
            cur, secret_type=912, version=1, state=2
        )  # symmetric: never in the view
        cur.execute(
            "SELECT kid, state FROM platform_vault.public_keys WHERE secret_type = 911"
        )
        assert cur.fetchall() == [("view-active", 2)]
        cur.execute(
            "SELECT count(*) FROM platform_vault.public_keys WHERE secret_type = 912"
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'platform_vault' AND table_name = 'public_keys' ORDER BY 1"
        )
        columns = [r[0] for r in cur.fetchall()]
        assert columns == sorted(
            [
                "secret_type",
                "scope_id",
                "version",
                "kid",
                "state",
                "public_material",
                "activated_at",
                "rotated_at",
            ]
        )
        cur.execute(
            "SELECT reloptions FROM pg_class WHERE oid = 'platform_vault.public_keys'::regclass"
        )
        assert "security_barrier=true" in (cur.fetchone()[0] or [])
    owner.close()


@requires_docker
def test_event_chain_is_linear(real_chain: PgTestDatabase) -> None:
    """A second genesis is refused and two events can never extend the same predecessor."""
    owner = _owner(real_chain)
    insert = (
        "INSERT INTO platform_vault.secret_events (secret_type, event_type, outcome, "
        "prev_chain_hash, chain_hash) VALUES (913, 1, 1, %s, %s)"
    )
    with owner.cursor() as cur:
        _append_event(
            cur, secret_type=913
        )  # genesis if the log was empty, else extends head
        head = _chain_head(cur)
        assert head is not None
        with pytest.raises(psycopg2.errors.UniqueViolation):  # a second genesis
            cur.execute(insert, (None, psycopg2.Binary(os.urandom(32))))
        _append_event(cur, secret_type=913)
        with pytest.raises(
            psycopg2.errors.UniqueViolation
        ):  # a fork off an already-extended head
            cur.execute(
                insert, (psycopg2.Binary(head), psycopg2.Binary(os.urandom(32)))
            )
    owner.close()


@requires_docker
def test_downgrade_is_refused_while_populated_then_round_trips(
    real_chain: PgTestDatabase,
) -> None:
    """A populated store cannot be dropped silently; with explicit confirmation downgrade then
    upgrade round-trips and leaves no vault ACL entry behind for any role."""
    owner = _owner(real_chain)
    with owner.cursor() as cur:
        _insert_secret(cur, secret_type=914, version=1)
        cur.execute("SELECT count(*) FROM platform_vault.secrets")
        assert cur.fetchone()[0] > 0
    with pytest.raises(RuntimeError, match="refusing to drop platform_vault"):
        alembic_cli("downgrade", _PRIOR, dsn=real_chain.dsn)
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM platform_vault.secrets")
        assert cur.fetchone()[0] > 0, "the refused downgrade changed nothing"
    alembic_cli(
        "downgrade",
        _PRIOR,
        dsn=real_chain.dsn,
        env_overrides={"WADDLES_ALLOW_PLATFORM_VAULT_DROP": "1"},
    )
    with owner.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname = 'platform_vault'"
        )
        assert cur.fetchone()[0] == 0
    alembic_cli("upgrade", "head", dsn=real_chain.dsn)
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM platform_vault.secrets")
        assert cur.fetchone()[0] == 0, "a fresh upgrade starts empty"
    owner.close()
    hub = _as(real_chain, "waddles_hub_api")
    with hub.cursor() as cur:
        cur.execute("SELECT count(*) FROM platform_vault.secrets")
        assert cur.fetchone()[0] == 0
    hub.close()


_MINI_SQL = "\n".join(
    f"CREATE ROLE {role} LOGIN PASSWORD 'testpass123';"
    for role in ("waddles_hub_api", *_load().PUBLIC_KEY_READERS)
)


@pytest.fixture
def mini_db() -> Iterator[PgTestDatabase]:
    """Bare Postgres 17 with just the roles (fsync off: fast on a loaded disk)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    container = f"waddles-migtest-0109-mini-{_free_port()}"
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
            f"127.0.0.1:{port}:5432",
            "postgres:17-bookworm",
            "-c",
            "fsync=off",
            "-c",
            "full_page_writes=off",
        ],
        capture_output=True,
        check=True,
    )
    try:
        _wait_ready(container, db.user, db.dbname)
        subprocess.run(
            ["docker", "exec", "-i", container, "psql", "-U", db.user, "-d", db.dbname],
            input=_MINI_SQL,
            text=True,
            capture_output=True,
            check=True,
        )
        yield db
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=False
        )


def _run(db: PgTestDatabase, fn_name: str) -> None:
    """Run the migration's upgrade/downgrade with op.execute bound to a live cursor."""
    conn = _owner(db)
    with conn.cursor() as cur, patch("alembic.op.execute", side_effect=cur.execute):
        getattr(_load(), fn_name)()
    conn.close()


def _acl_entries(owner: Any, role: str) -> int:
    """ACL entries naming `role` on the vault schema or any relation in it (blocks DROP ROLE)."""
    with owner.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM pg_namespace WHERE nspname = 'platform_vault' "
            "AND nspacl::text LIKE %s) + (SELECT count(*) FROM pg_class c WHERE c.relnamespace "
            "= to_regnamespace('platform_vault') AND c.relacl::text LIKE %s)",
            (f"%{role}=%", f"%{role}=%"),
        )
        return int(cur.fetchone()[0])


@requires_docker
def test_drop_role_succeeds_after_downgrade_and_fails_without_it(
    mini_db: PgTestDatabase,
) -> None:
    """DROP ROLE works once the migration is downgraded -- and FAILS while grants remain."""
    m = _load()
    roles = (m.HUB_API_ROLE, *m.PUBLIC_KEY_READERS)
    _run(mini_db, "upgrade")
    owner = _owner(mini_db)
    for role in roles:
        assert _acl_entries(owner, role) > 0
        with (
            owner.cursor() as cur,
            pytest.raises(psycopg2.errors.DependentObjectsStillExist),
        ):
            cur.execute(f"DROP ROLE {role}")  # control: leftover grants block the drop
    with patch.dict("os.environ", {"WADDLES_ALLOW_PLATFORM_VAULT_DROP": "1"}):
        _run(mini_db, "downgrade")
        _run(mini_db, "downgrade")  # idempotent
    for role in roles:
        assert _acl_entries(owner, role) == 0
        with owner.cursor() as cur:
            cur.execute(f"DROP ROLE {role}")
    with owner.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (list(roles),)
        )
        assert cur.fetchone()[0] == 0
    owner.close()
