"""Real-Postgres tests for 0055_per_service_db_roles (security findings H-1 and H-3).

# regression: H-1 repo-known DB passwords (*_dev_changeme) live on a shared database
# regression: H-3 every workload connected as the shared database superuser

Everything here runs against a TRUE fresh `alembic upgrade head` replay of a real
Postgres 17 container (all ~285 legacy + Alembic tables, the designed RLS policies, the
SECURITY DEFINER provisioning functions) -- grants and row-level security cannot be
verified against mocked SQL text. Each assertion states its denominator so a vacuous
pass (zero roles / zero tables examined) is a failure, not a green.

What is proven:

* every `config/postgres/service-roles.yaml` role is a LOGIN, non-superuser, non-bypass-RLS
  role that authenticates with its OWN password (and no other role's);
* each role's EFFECTIVE table privileges equal exactly its catalog entry plus what its
  designed group roles grant -- the whole `has_table_privilege` matrix, both directions;
* a role CANNOT read/write another service's tables, create objects, create roles, read
  `pg_authid`, or run `COPY ... PROGRAM`;
* no repo-known credential (the old `<role>_dev_changeme` values) authenticates, every
  such role has LOGIN and its password stripped, and `hub_admin` can no longer execute the
  SECURITY DEFINER escalation functions;
* credential rows in `platform_integrations` reach a legacy pod only through its designed
  `mod_*` RLS membership;
* reconcile is idempotent, rotates passwords, and fails loud on catalog drift / missing
  passwords; the dev-only opt-in works locally and is refused on a shared tier;
* downgrade -> upgrade round-trips.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
import sqlalchemy as sa
from pg_docker import (
    DOCKER_AVAILABLE,
    REPO_ROOT,
    PgTestDatabase,
    alembic_cli,
    bootstrap_minimal_schema,
    empty_postgres,
    load_service_roles_module,
    throwaway_service_role_passwords,
)

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

roles_mod = load_service_roles_module()
CATALOG = roles_mod.load_catalog()
PASSWORDS_ENV = roles_mod.PASSWORDS_ENV
PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE")
PRIOR_REVISION = "0054_tenant_external_kms"


def _connect(db: PgTestDatabase, user: str, password: str) -> Any:
    """Autocommit connection (so one denied statement never poisons the next)."""
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


@contextlib.contextmanager
def _session(db: PgTestDatabase, user: str, password: str) -> Iterator[Any]:
    """`psycopg2`'s own `with conn` opens a transaction even in autocommit; close explicitly."""
    conn = _connect(db, user, password)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _admin_cursor(db: PgTestDatabase) -> Iterator[Any]:
    """Cursor on an autocommit DB-owner connection (setup / ground-truth queries only)."""
    conn = _connect(db, db.user, db.password)
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def _as_role(db: PgTestDatabase, role: str) -> Any:
    return _session(db, role, db.service_role_passwords[role])


def _denied(conn: Any, sql: str) -> bool:
    """True iff `sql` fails with InsufficientPrivilege (any other outcome is a test failure)."""
    with conn.cursor() as cur:
        try:
            cur.execute(sql)
        except psycopg2.errors.InsufficientPrivilege:
            return True
    return False


def _public_tables(cur: Any) -> list[str]:
    """Base tables only (relkind r/p) -- views are privilege boundaries handled separately."""
    cur.execute(
        "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
        "AND relkind IN ('r', 'p') ORDER BY 1"
    )
    return [r[0] for r in cur.fetchall()]


def _public_views(cur: Any) -> list[str]:
    cur.execute(
        "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
        "AND relkind IN ('v', 'm', 'f') ORDER BY 1"
    )
    return [r[0] for r in cur.fetchall()]


def _effective(cur: Any, role: str) -> set[tuple[str, str]]:
    cur.execute(
        "SELECT c.relname, p.priv FROM pg_class c "
        "CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) AS p(priv) "
        "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND c.relnamespace = 'public'::regnamespace "
        "AND has_table_privilege(%s, c.oid, p.priv)",
        (role,),
    )
    return {(r[0], r[1]) for r in cur.fetchall()}


def _expected_direct(spec: Any, tables: list[str], views: list[str]) -> set[tuple[str, str]]:
    """Independent re-derivation of a role's direct grants from the catalog (not reconcile())."""
    present = set(tables)
    direct: set[tuple[str, str]] = set()
    if spec.public_dml:
        denied = set(spec.deny_tables)
        if spec.exclude_matrix_tables:
            denied |= CATALOG.matrix_tables
        if spec.is_legacy:
            denied |= CATALOG.legacy_deny_tables - spec.allow_tables
        for table in present - denied:
            for priv in PRIVS:
                if table in CATALOG.read_only_tables and priv != "SELECT":
                    continue
                direct.add((table, priv))
    for table, privs in spec.tables.items():
        if table in present:
            direct |= {(table, p) for p in privs}
    for view, privs in spec.views.items():
        if view in views:
            direct |= {(view, p) for p in privs}
    return direct


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One fresh-replayed Postgres 17 container with a known per-role password set."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    passwords = throwaway_service_role_passwords()
    with empty_postgres("0055-roles") as db:
        alembic_cli(
            "upgrade",
            "head",
            dsn=db.dsn,
            env_overrides={PASSWORDS_ENV: json.dumps(passwords)},
        )
        yield dataclasses.replace(db, service_role_passwords=passwords)


@requires_docker
def test_catalog_has_expected_denominator() -> None:
    """Guard against a vacuous pass: the catalog must name a real, large role set."""
    assert len(CATALOG.roles) >= 30
    strict = [r for r in CATALOG.roles if not r.public_dml]
    assert len(strict) >= 7


@requires_docker
def test_every_service_role_is_a_least_privilege_login(pg_db: PgTestDatabase) -> None:
    with _admin_cursor(pg_db) as cur:
        cur.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s)",
            (list(CATALOG.names),),
        )
        rows = {r[0]: r[1:] for r in cur.fetchall()}
    assert set(rows) == set(CATALOG.names), "roles missing from the database"
    for name, (login, sup, createdb, createrole, repl, bypass) in rows.items():
        assert login, name
        assert not (sup or createdb or createrole or repl or bypass), name


@requires_docker
def test_each_role_authenticates_with_its_own_distinct_password(pg_db: PgTestDatabase) -> None:
    assert len(set(pg_db.service_role_passwords.values())) == len(CATALOG.names)
    for name in CATALOG.names:
        with _as_role(pg_db, name) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
            )
            user, is_super = cur.fetchone()
            assert user == name and is_super is False
    # a role's password does NOT open another role
    with pytest.raises(psycopg2.OperationalError):
        _connect(pg_db, "waddles_svc_action", pg_db.service_role_passwords["waddles_hub_api"])


@requires_docker
@pytest.mark.parametrize("role", [r.name for r in CATALOG.roles])
def test_effective_privileges_equal_catalog_plus_designed_groups(
    pg_db: PgTestDatabase, role: str
) -> None:
    """The full has_table_privilege matrix, both directions, for every catalog role."""
    spec = CATALOG.role(role)
    with _admin_cursor(pg_db) as cur:
        tables = _public_tables(cur)
        views = _public_views(cur)
        assert len(tables) > 200, "fresh replay should expose the full schema"
        assert len(views) >= 6, "views are privilege boundaries and must be in the denominator"
        cur.execute("SELECT rolname FROM pg_roles")
        existing = {r[0] for r in cur.fetchall()}
        expected = _expected_direct(spec, tables, views)
        for group in spec.member_of:
            if group in existing:
                expected |= _effective(cur, group)
        actual = _effective(cur, role)
    assert actual == expected, (
        f"{role}: unexpected={sorted(actual - expected)[:10]} missing={sorted(expected - actual)[:10]}"
    )
    if spec.tables:
        assert actual, f"{role} lists explicit tables but holds no table privilege at all"
    if not spec.public_dml and not spec.tables and not spec.member_of:
        assert not actual, f"{role} is a zero-table role but holds {sorted(actual)[:5]}"


@requires_docker
def test_a_role_cannot_reach_another_services_tables(pg_db: PgTestDatabase) -> None:
    """Explicit cross-service denials, executed as the role -- not just ACL introspection."""
    cases = [
        # (role, sql that MUST be denied)
        ("waddles_svc_action", "SELECT 1 FROM streaming_configs"),
        ("waddles_svc_action", "SELECT 1 FROM overlay_images"),
        ("waddles_svc_action", "SELECT 1 FROM hub_users"),
        ("waddles_svc_action", "UPDATE action_dispatch_log SET detail = 'x'"),
        ("waddles_svc_action", "DELETE FROM action_dispatch_log"),
        ("waddles_svc_streaming", "SELECT 1 FROM action_dispatch_log"),
        ("waddles_svc_streaming", "SELECT 1 FROM caption_events"),
        ("waddles_svc_streaming", "SELECT 1 FROM hub_users"),
        ("waddles_svc_presentation", "SELECT 1 FROM streaming_targets"),
        ("waddles_svc_presentation", "SELECT 1 FROM action_dispatch_log"),
        ("waddles_svc_presentation", "UPDATE overlay_view_credentials SET id = id"),
        ("waddles_svc_process", "SELECT 1 FROM communities"),
        ("waddles_svc_process", "SELECT 1 FROM action_dispatch_log"),
        ("waddles_svc_ingest", "SELECT 1 FROM tenants"),
        ("waddles_svc_core", "SELECT 1 FROM tenants"),
        ("waddles_reputation", "SELECT 1 FROM action_dispatch_log"),
        ("waddles_reputation", "SELECT 1 FROM streaming_configs"),
        ("waddles_reputation", "SELECT password_hash FROM hub_users"),
        ("waddles_hub_api", "SELECT 1 FROM platform_integrations"),
        ("waddles_hub_api", "UPDATE alembic_version SET version_num = 'x'"),
        ("waddles_legacy_core_data", "SELECT 1 FROM app_versions"),
        ("waddles_legacy_core_data", "SELECT 1 FROM tenant_platform_credentials"),
        ("waddles_legacy_core_data", "SELECT 1 FROM connector_pii_identities"),
        ("waddles_hub_api", "SELECT 1 FROM connector_pii_members"),
        ("waddles_legacy_core_data", "SELECT 1 FROM platform_integrations"),
        ("waddles_legacy_core_data", "SELECT password_hash FROM hub_users"),
        ("waddles_legacy_router", "SELECT 1 FROM ephemeral_pseudonyms"),
        ("waddles_legacy_interactive_social", "SELECT 1 FROM hub_sessions"),
        ("waddles_legacy_hub", "SELECT 1 FROM ephemeral_pseudonyms"),
        ("waddles_hub_api", "SELECT provision_module_db_account('x','custom','custom','y')"),
        ("waddles_legacy_router", "SELECT resolve_identity_uuid(1,'discord','x',NULL,NULL)"),
        ("waddles_legacy_router", "INSERT INTO app_versions DEFAULT VALUES"),
    ]
    examined = 0
    for role, sql in cases:
        with _as_role(pg_db, role) as conn:
            assert _denied(conn, sql), f"{role} was NOT denied: {sql}"
        examined += 1
    assert examined == len(cases) >= 20
    # ... and each strict role CAN do its own job
    with _as_role(pg_db, "waddles_svc_action") as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO action_dispatch_log (tenant_id, app_id, target_type, status, attempt, detail)"
            " VALUES (1, 'a', 't', 'ok', 1, 'd') RETURNING id"
        )
        assert cur.fetchone()[0] >= 1
    with _as_role(pg_db, "waddles_svc_streaming") as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM streaming_configs")
        cur.execute("SELECT count(*) FROM tenants")


@requires_docker
@pytest.mark.parametrize(
    "role", ["waddles_hub_api", "waddles_svc_action", "waddles_legacy_router", "waddles_reputation"]
)
def test_service_roles_have_no_owner_powers(pg_db: PgTestDatabase, role: str) -> None:
    statements = [
        "CREATE TABLE service_role_probe (i int)",
        "CREATE SCHEMA service_role_probe",
        "CREATE ROLE service_role_probe LOGIN",
        "ALTER ROLE waddlebot NOSUPERUSER",
        f"ALTER ROLE {role} SUPERUSER",
        "SELECT rolpassword FROM pg_authid",
        "COPY (SELECT 1) TO PROGRAM 'true'",
        "DROP TABLE communities",
        "TRUNCATE tenants",
        "CREATE EXTENSION hstore",
    ]
    with _as_role(pg_db, role) as conn:
        for sql in statements:
            assert _denied(conn, sql), f"{role} was NOT denied: {sql}"


@requires_docker
def test_repo_known_credentials_are_dead(pg_db: PgTestDatabase) -> None:
    """H-1: no role that ever shipped a repo password can log in, with ANY known password."""
    with _admin_cursor(pg_db) as cur:
        cur.execute(
            "SELECT r.rolname, r.rolcanlogin, a.rolpassword IS NULL FROM pg_roles r "
            "JOIN pg_authid a ON a.oid = r.oid WHERE r.rolname = ANY(%s)",
            (list(roles_mod.REPO_CREDENTIAL_ROLES),),
        )
        rows = cur.fetchall()
    assert len(rows) >= 35, f"expected the 031 role set to exist on a fresh replay, saw {len(rows)}"
    for name, can_login, no_password in rows:
        assert not can_login, f"{name} can still log in"
        assert no_password, f"{name} still has a stored password"
    guesses = ["{r}_dev_changeme", "mod_{r}_dev_changeme", "dev123", "changeme", "{r}"]
    for name, *_ in rows:
        for guess in guesses:
            with pytest.raises(psycopg2.OperationalError):
                _connect(pg_db, name, guess.format(r=name))


@requires_docker
def test_hub_admin_escalation_chain_is_closed(pg_db: PgTestDatabase) -> None:
    """The pre-fix chain: repo password -> hub_admin -> SECURITY DEFINER custom_grants -> superuser."""
    with _admin_cursor(pg_db) as cur:
        cur.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.proname = ANY(%s) "
            "AND p.pronamespace = 'public'::regnamespace",
            (list(roles_mod.PRIVILEGED_FUNCTIONS),),
        )
        signatures = [r[0] for r in cur.fetchall()]
        assert len(signatures) == len(roles_mod.PRIVILEGED_FUNCTIONS)
        for signature in signatures:
            for role in ["hub_admin", "waddles_hub_api", *CATALOG.names]:
                cur.execute(
                    "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
                    (role, signature),
                )
                assert cur.fetchone()[0] is False, f"{role} may execute {signature}"
        # defaults that auto-granted every future table to hub_admin are gone too
        cur.execute(
            "SELECT count(*) FROM pg_default_acl d, aclexplode(d.defaclacl) a "
            "WHERE pg_get_userbyid(a.grantee) = 'hub_admin'"
        )
        assert cur.fetchone()[0] == 0


def _seed_world(cur: Any) -> dict[str, int]:
    """Owner-seeded tenant / community / hub user / streaming config the role paths act on."""
    cur.execute(
        "INSERT INTO tenants (slug, display_name) VALUES ('fn-test', 'Functional Test') RETURNING id"
    )
    tenant = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO communities (name, tenant_id) VALUES ('fn-community', %s) RETURNING id",
        (tenant,),
    )
    community = cur.fetchone()[0]
    cur.execute("INSERT INTO hub_users (username) VALUES ('fn-user') RETURNING id")
    user = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO streaming_configs (community_id, source_url) VALUES (%s, 'rtmp://x/y') RETURNING id",
        (community,),
    )
    return {"tenant": tenant, "community": community, "user": user, "config": cur.fetchone()[0]}


@requires_docker
def test_strict_roles_can_perform_their_real_write_paths(pg_db: PgTestDatabase) -> None:
    """Privilege-matrix equality cannot see invoker-rights trigger side effects: run the paths.

    # regression: reputation INSERT INTO community_members fires 0045's
    # community_members_set_user_uuid, which reads hub_users / hub_user_identities and mints
    # ephemeral_pseudonyms as the INVOKER -- the reputation role has none of that, so it must run
    # as SECURITY DEFINER (reconcile hardens it) or every first-seen member insert fails.
    """
    with _admin_cursor(pg_db) as cur:
        world = _seed_world(cur)
    community, tenant, user, config = (
        world["community"],
        world["tenant"],
        world["user"],
        world["config"],
    )

    with _as_role(pg_db, "waddles_reputation") as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO community_members (community_id, user_id, platform, platform_user_id, "
            "reputation, role) VALUES (%s, NULL, 'discord', 'fn-plat-1', 600, 'member') RETURNING user_uuid",
            (community,),
        )
        assert cur.fetchone()[0] is not None, "trigger must mint a pseudonym uuid"
        cur.execute(
            "UPDATE community_members SET reputation = 650 WHERE community_id = %s", (community,)
        )
        cur.execute(
            "INSERT INTO reputation_events (community_id, platform, platform_user_id, event_type, "
            "score_before, score_after) VALUES (%s, 'discord', 'fn-plat-1', 'msg', 600, 650)",
            (community,),
        )
        cur.execute(
            "INSERT INTO reputation_tenant (tenant_id, hub_user_id) VALUES (%s, %s)", (tenant, user)
        )
        cur.execute(
            "SELECT hu.username, hu.avatar_url FROM reputation_tenant rt "
            "JOIN hub_users hu ON hu.id = rt.hub_user_id WHERE rt.tenant_id = %s",
            (tenant,),
        )
        assert cur.fetchone()[0] == "fn-user"
        # ... and the trigger fix did NOT widen its reach into the identity tables
        for sql in (
            "SELECT 1 FROM hub_user_identities",
            "SELECT 1 FROM ephemeral_pseudonyms",
            "SELECT uuid FROM hub_users",
        ):
            assert _denied(conn, sql), sql

    with _as_role(pg_db, "waddles_svc_streaming") as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tenants")
        cur.execute("SELECT count(*) FROM communities")
        cur.execute("SELECT count(*) FROM community_servers")
        cur.execute(
            "INSERT INTO streaming_targets (config_id, platform, forward_url) VALUES (%s, 'twitch', 'u') "
            "RETURNING id",
            (config,),
        )
        target = cur.fetchone()[0]
        cur.execute("UPDATE streaming_targets SET enabled = FALSE WHERE id = %s", (target,))
        cur.execute("UPDATE streaming_configs SET enabled = TRUE WHERE id = %s", (config,))
        cur.execute("DELETE FROM streaming_targets WHERE id = %s", (target,))

    with _as_role(pg_db, "waddles_svc_presentation") as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO caption_events (community_id, platform, original_message) "
            "VALUES (%s, 'twitch', 'hi') RETURNING id",
            (community,),
        )
        event = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM caption_events WHERE community_id = %s", (community,))
        cur.execute("DELETE FROM caption_events WHERE id = %s", (event,))
        cur.execute(
            "INSERT INTO overlay_images (community_id, asset_id, object_key, content_type, "
            "size_bytes, sha256) VALUES (%s, gen_random_uuid(), 'k', 'image/png', 1, 'ab') RETURNING id",
            (community,),
        )
        cur.execute("SELECT count(*) FROM overlay_images")
        cur.execute("SELECT count(*) FROM overlay_view_credentials")
        cur.execute("SELECT count(*) FROM overlay_surfaces")
        cur.execute("SELECT count(*) FROM presentation_config")

    with _as_role(pg_db, "waddles_hub_api") as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO community_members (community_id, platform, platform_user_id, role) "
            "VALUES (%s, 'discord', 'fn-plat-hub', 'member') RETURNING user_uuid",
            (community,),
        )
        assert cur.fetchone()[0] is not None
        cur.execute("UPDATE communities SET name = 'fn-community-2' WHERE id = %s", (community,))
        cur.execute("SELECT count(*) FROM marketplace_catalog")
        cur.execute("SELECT count(*) FROM tenant_platform_credentials")
        cur.execute("SELECT version_num FROM alembic_version")
        assert cur.fetchone()[0]

    with _as_role(pg_db, "waddles_hub_api") as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT resolve_identity_uuid(%s, 'discord', 'fn-plat-ident', NULL, NULL)", (tenant,)
        )
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT count(*) FROM ephemeral_pseudonyms")

    with _as_role(pg_db, "waddles_legacy_hub") as conn, conn.cursor() as cur:
        for table in ("hub_users", "hub_sessions", "hub_admins", "hub_user_identities"):
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 -- fixed names
    with _as_role(pg_db, "waddles_legacy_core_identity") as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM hub_users")  # designed mod_core_identity grant

    with _as_role(pg_db, "waddles_legacy_core_community") as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO community_members (community_id, platform, platform_user_id, role) "
            "VALUES (%s, 'discord', 'fn-plat-legacy', 'member') RETURNING user_uuid",
            (community,),
        )
        assert cur.fetchone()[0] is not None


@requires_docker
def test_identity_trigger_function_is_hardened_and_not_directly_callable(
    pg_db: PgTestDatabase,
) -> None:
    with _admin_cursor(pg_db) as cur:
        cur.execute(
            "SELECT prosecdef, proconfig FROM pg_proc WHERE proname = 'community_members_set_user_uuid'"
        )
        secdef, config = cur.fetchone()
    assert secdef is True and any("search_path=pg_catalog" in c for c in config)
    with _as_role(pg_db, "waddles_legacy_router") as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.Error, match="trigger"):
            cur.execute("SELECT community_members_set_user_uuid()")


@requires_docker
def test_identity_functions_are_executable_by_hub_api_only(pg_db: PgTestDatabase) -> None:
    """hub-api calls these directly; EXECUTE is revoked from PUBLIC, so exactly one role has it."""
    names = ["resolve_identity_uuid", "erase_ephemeral_pseudonym_handles"]
    with _admin_cursor(pg_db) as cur:
        cur.execute(
            "SELECT p.proname, p.oid::regprocedure::text FROM pg_proc p "
            "WHERE p.proname = ANY(%s) AND p.pronamespace = 'public'::regnamespace",
            (names,),
        )
        found = cur.fetchall()
        assert {name for name, _ in found} == set(names)
        for _, signature in found:
            for role in CATALOG.names:
                cur.execute(
                    "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
                    (role, signature),
                )
                assert cur.fetchone()[0] is (role == "waddles_hub_api"), (role, signature)


@requires_docker
def test_credential_rows_flow_only_through_designed_membership(pg_db: PgTestDatabase) -> None:
    """platform_integrations is FORCE-RLS: a legacy pod sees only its designed platforms' rows."""
    with _admin_cursor(pg_db) as cur:
        cur.execute("DELETE FROM platform_integrations WHERE integration_type = 'bot'")
        cur.execute(
            "INSERT INTO platform_integrations (platform, integration_type, access_token) VALUES "
            "('twitch','bot','t'), ('discord','bot','d'), ('spotify','bot','s')"
        )

    def visible(role: str) -> set[str] | None:
        with _as_role(pg_db, role) as conn, conn.cursor() as cur:
            try:
                cur.execute("SELECT platform FROM platform_integrations")
            except psycopg2.errors.InsufficientPrivilege:
                return None
            return {r[0] for r in cur.fetchall()}

    assert visible("waddles_legacy_action_platforms") == {"twitch", "discord"}
    assert visible("waddles_legacy_interactive_media") == {"spotify"}
    assert visible("waddles_legacy_router") == {"twitch", "discord", "spotify"}
    assert visible("waddles_legacy_hub") == {"twitch", "discord", "spotify"}
    for role in (
        "waddles_hub_api",
        "waddles_legacy_core_data",
        "waddles_svc_streaming",
        "waddles_svc_action",
    ):
        assert visible(role) is None, f"{role} must not reach platform_integrations at all"


@requires_docker
def test_reconcile_rotates_passwords_and_is_idempotent(pg_db: PgTestDatabase) -> None:
    rotated = throwaway_service_role_passwords()
    old = dict(pg_db.service_role_passwords)
    env = {
        "DATABASE_URL": pg_db.dsn,
        PASSWORDS_ENV: json.dumps(rotated),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    with _admin_cursor(pg_db) as cur:
        before = {r: _effective(cur, r) for r in CATALOG.names}
    for _ in range(2):  # twice: idempotent
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "db" / "service_roles.py"),
                "reconcile",
                "--strict",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert f"Reconciled {len(CATALOG.names)} service roles" in result.stdout
    for secret in rotated.values():
        assert secret not in result.stdout + result.stderr, "reconcile leaked a password"
    with pytest.raises(psycopg2.OperationalError):
        _connect(pg_db, "waddles_svc_action", old["waddles_svc_action"])
    with _session(pg_db, "waddles_svc_action", rotated["waddles_svc_action"]) as conn:
        assert conn is not None
    with _admin_cursor(pg_db) as cur:
        assert {r: _effective(cur, r) for r in CATALOG.names} == before
    # restore the module-wide credentials for later tests
    env[PASSWORDS_ENV] = json.dumps(old)
    subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "db" / "service_roles.py"),
            "reconcile",
            "--strict",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


@requires_docker
def test_reconcile_repairs_out_of_band_grant_drift(pg_db: PgTestDatabase) -> None:
    with _admin_cursor(pg_db) as cur:
        cur.execute("GRANT SELECT ON hub_users TO waddles_svc_streaming")
        cur.execute("GRANT ALL ON streaming_configs TO waddles_svc_process")
        assert ("hub_users", "SELECT") in _effective(cur, "waddles_svc_streaming")
    engine = sa.create_engine(
        f"postgresql+psycopg2://{pg_db.user}:{pg_db.password}@{pg_db.host}:{pg_db.port}/{pg_db.dbname}"
    )
    with engine.begin() as conn:
        roles_mod.reconcile(conn, CATALOG, pg_db.service_role_passwords, strict=True)
    with _admin_cursor(pg_db) as cur:
        assert ("hub_users", "SELECT") not in _effective(cur, "waddles_svc_streaming")
        assert not _effective(cur, "waddles_svc_process")


@requires_docker
def test_strict_reconcile_fails_loud_on_catalog_drift(
    pg_db: PgTestDatabase, tmp_path: Path
) -> None:
    drift = tmp_path / "service-roles.yaml"
    drift.write_text(
        (REPO_ROOT / "config" / "postgres" / "service-roles.yaml").read_text(encoding="utf-8")
        + "\n  waddles_drift_probe:\n    description: probe\n    tables:\n      no_such_table_xyz: [SELECT]\n",
        encoding="utf-8",
    )
    catalog = roles_mod.load_catalog(drift)
    passwords = {**pg_db.service_role_passwords, "waddles_drift_probe": secrets.token_hex(16)}
    engine = sa.create_engine(
        f"postgresql+psycopg2://{pg_db.user}:{pg_db.password}@{pg_db.host}:{pg_db.port}/{pg_db.dbname}"
    )
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            with pytest.raises(roles_mod.CatalogDriftError, match="no_such_table_xyz"):
                roles_mod.reconcile(conn, catalog, passwords, strict=True)
        finally:
            tx.rollback()
    with _admin_cursor(pg_db) as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'waddles_drift_probe'")
        assert cur.fetchone() is None, "strict failure must roll the probe role back"


@requires_docker
def test_downgrade_then_upgrade_round_trips(pg_db: PgTestDatabase) -> None:
    env = {PASSWORDS_ENV: json.dumps(pg_db.service_role_passwords)}
    alembic_cli("downgrade", PRIOR_REVISION, dsn=pg_db.dsn, env_overrides=env)
    with _admin_cursor(pg_db) as cur:
        cur.execute("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (list(CATALOG.names),))
        assert cur.fetchone()[0] == 0, "downgrade must drop every service role"
        cur.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'hub_admin'")
        assert cur.fetchone()[0] is False, "downgrade must NOT resurrect repo-known logins"
    alembic_cli("upgrade", "head", dsn=pg_db.dsn, env_overrides=env)
    with _as_role(pg_db, "waddles_hub_api") as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tenants")


@requires_docker
def test_missing_service_role_passwords_fail_the_migration_loudly() -> None:
    """No password JSON and no dev opt-in => 0055 raises; nothing half-provisioned."""
    with empty_postgres("0055-nopw") as db:
        bootstrap_minimal_schema("0055-nopw", db)
        with pytest.raises(RuntimeError, match="missing a password"):
            alembic_cli("upgrade", "head", dsn=db.dsn, env_overrides={PASSWORDS_ENV: ""})
        with _admin_cursor(db) as cur:
            cur.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (list(CATALOG.names),)
            )
            assert cur.fetchone()[0] == 0
        weak = {**throwaway_service_role_passwords(), "waddles_hub_api": "changeme-changeme-12345"}
        with pytest.raises(RuntimeError, match="placeholder"):
            alembic_cli(
                "upgrade", "head", dsn=db.dsn, env_overrides={PASSWORDS_ENV: json.dumps(weak)}
            )


@requires_docker
def test_dev_opt_in_is_local_only() -> None:
    """The docker-compose opt-in keeps dev logins working and is refused on a shared tier."""
    with empty_postgres("0055-dev") as db:
        dev_env = {PASSWORDS_ENV: "", "WADDLES_DEV_DB_ROLE_PW_SUFFIX": "_dev_changeme"}
        for tier in ("alpha", "beta", "gamma", "production"):
            with pytest.raises(RuntimeError, match="must never reach a shared database"):
                alembic_cli(
                    "upgrade",
                    "head",
                    dsn=db.dsn,
                    env_overrides={**dev_env, "WADDLES_DEPLOYMENT_TIER": tier},
                )
        alembic_cli(
            "upgrade",
            "head",
            dsn=db.dsn,
            env_overrides={**dev_env, "WADDLES_DEPLOYMENT_TIER": "dev"},
        )
        with _session(db, "hub_admin", "hub_admin_dev_changeme") as conn:
            assert conn is not None
        with _session(db, "waddles_svc_action", "waddles_svc_action_dev_changeme") as conn:
            assert conn is not None
