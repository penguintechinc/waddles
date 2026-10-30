"""Real-Postgres tests for 0069_pi_definer_functions (gh-969).

# regression: gh-969 hub-api 500s on platform_integrations (42501) -- the role is DENIED the
# table by design (H-3); it reaches it ONLY through the narrow SECURITY DEFINER functions.

A TRUE fresh `alembic upgrade head` replay of a Postgres 17 container, with the real per-service
roles. Proven: the table stays denied to `waddles_hub_api` (incl. writes); only that role can
EXECUTE the functions; the metadata functions can never return a secret (seeded canary strings,
including inside `config_data`); the secret lane returns ciphertext for `community_oauth` rows
only, never bot/user_oauth, and audits every read; writes can't touch bot rows or downgrade the
seal; the revision applies on an existing DB (downgrade -> upgrade) and heads == 1. Every
assertion states its denominator so a vacuous pass is a failure.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import (
    DOCKER_AVAILABLE,
    PgTestDatabase,
    alembic_cli,
    empty_postgres,
    load_service_roles_module,
    throwaway_service_role_passwords,
)

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION = (
    Path(__file__).resolve().parent.parent / "versions" / "0069_pi_definer_functions.py"
)
_spec = importlib.util.spec_from_file_location("migration_0069", _MIGRATION)
assert _spec is not None and _spec.loader is not None
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

roles_mod = load_service_roles_module()
CATALOG = roles_mod.load_catalog()
PASSWORDS_ENV = roles_mod.PASSWORDS_ENV
HUB = "waddles_hub_api"
CANARY_ACCESS = "CANARY-ACCESS-TOKEN-xyz"
CANARY_REFRESH = "CANARY-REFRESH-TOKEN-xyz"
CANARY_SECRET = "CANARY-CLIENT-SECRET-xyz"
CANARY_CFG = "CANARY-CONFIG-TOKEN-xyz"
BOT_CANARY = "CANARY-BOT-TOKEN-xyz"


def _connect(db: PgTestDatabase, user: str, password: str) -> Any:
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
def _cur(db: PgTestDatabase, role: str | None = None) -> Iterator[Any]:
    conn = (
        _connect(db, role, db.service_role_passwords[role])
        if role
        else _connect(db, db.user, db.password)
    )
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """Fresh-replayed (empty DB -> head) container; seeded with one community + canary rows."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    passwords = throwaway_service_role_passwords()
    with empty_postgres("0069-pi") as db:
        alembic_cli(
            "upgrade",
            "head",
            dsn=db.dsn,
            env_overrides={PASSWORDS_ENV: json.dumps(passwords)},
        )
        db = dataclasses.replace(db, service_role_passwords=passwords)
        with _cur(db) as cur:
            cur.execute(
                "INSERT INTO tenants (slug, display_name) VALUES ('pi','PI') RETURNING id"
            )
            tenant = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO communities (name, tenant_id) VALUES ('pi-c', %s) RETURNING id",
                (tenant,),
            )
            community = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO platform_integrations (platform, integration_type, access_token, "
                "refresh_token, client_id, client_secret, config_data) VALUES "
                "('twitch','bot',%s,%s,'bot-client',%s,%s)",
                (BOT_CANARY, BOT_CANARY, BOT_CANARY, json.dumps({"note": "bot"})),
            )
            cur.execute(
                "INSERT INTO platform_integrations (platform, integration_type, community_id, "
                "access_token, refresh_token, client_id, client_secret, config_data, scopes, "
                "is_encrypted, seal_version) VALUES ('youtube','community_oauth',%s,%s,%s,"
                "'cid',%s,%s,%s,TRUE,1)",
                (
                    community,
                    CANARY_ACCESS,
                    CANARY_REFRESH,
                    CANARY_SECRET,
                    json.dumps(
                        {
                            "account_label": "Chan",
                            "access_token": CANARY_CFG,
                            "nested": {"client_secret": CANARY_CFG, "ok": 1},
                            "list": [{"refresh_token": CANARY_CFG, "keep": 2}],
                        }
                    ),
                    ["a", "b"],
                ),
            )
        yield db


def _community(db: PgTestDatabase) -> int:
    with _cur(db) as cur:
        cur.execute("SELECT id FROM communities WHERE name = 'pi-c'")
        return int(cur.fetchone()[0])


class TestStatic:
    def test_revision_chain(self) -> None:
        assert M.revision == "0069_pi_definer_functions"
        assert len(M.revision) <= 32
        assert M.down_revision == "0068_app_catalog_kind"

    def test_catalog_lists_every_callable_function(self) -> None:
        listed = set(next(r for r in CATALOG.roles if r.name == HUB).functions)
        callable_names = {sig.split("(")[0] for sig in M.CALLABLE_SIGNATURES}
        assert len(callable_names) == 6
        assert callable_names <= listed
        assert not {sig.split("(")[0] for sig in M.HELPER_SIGNATURES} & listed

    def test_every_definer_body_pins_search_path_and_no_dynamic_sql(self) -> None:
        sql = M._GROUP_A_SQL + M._GROUP_B_SQL
        assert sql.count("SECURITY DEFINER") == 6
        assert sql.count("SET search_path = pg_catalog, pg_temp") == 6
        assert "EXECUTE" not in sql.replace("REVOKE", "").upper().replace(
            "EXECUTE ON", ""
        )
        assert "'bot'" not in M._GROUP_B_SQL.replace(
            "-- ", ""
        )  # lane is community_oauth only
        for secret_col in ("client_secret", "client_id"):
            # secret_get must not return them
            body = M._GROUP_B_SQL.split("pi_community_secret_get")[1].split(
                "pi_community_upsert"
            )[0]
            returns = body.split("RETURNS TABLE")[1].split("LANGUAGE")[0]
            assert secret_col not in returns

    def test_metadata_return_types_contain_no_secret_column(self) -> None:
        for fn in ("pi_list_metadata", "pi_community_status"):
            body = M._GROUP_A_SQL.split(f"public.{fn}(")[1].split("LANGUAGE")[0]
            returns = body.split("RETURNS TABLE")[1]
            for forbidden in (
                "access_token text",
                "refresh_token text",
                "client_secret text",
            ):
                assert forbidden not in returns, (fn, forbidden)


@requires_docker
def test_alembic_has_a_single_head(pg_db: PgTestDatabase) -> None:
    out = alembic_cli("heads", dsn=pg_db.dsn).stdout
    heads = [ln for ln in out.splitlines() if "(head)" in ln]
    assert len(heads) == 1 and heads[0].startswith("0069_pi_definer_functions"), out


@requires_docker
def test_hub_api_is_still_denied_the_table_directly(pg_db: PgTestDatabase) -> None:
    """The H-3 boundary: no direct privilege of any kind (the must-be-denied test stays true)."""
    examined = 0
    with _cur(pg_db, HUB) as cur:
        for stmt in (
            "SELECT 1 FROM platform_integrations",
            "SELECT access_token FROM platform_integrations",
            "INSERT INTO platform_integrations (platform, integration_type) VALUES ('x','bot')",
            "UPDATE platform_integrations SET is_active = FALSE",
            "DELETE FROM platform_integrations",
            "TRUNCATE platform_integrations",
        ):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(stmt)
            examined += 1
    assert examined == 6
    with _cur(pg_db) as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE table_name = 'platform_integrations' AND grantee = %s",
            (HUB,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT has_table_privilege(%s, 'platform_integrations', 'SELECT')", (HUB,)
        )
        assert cur.fetchone()[0] is False


@requires_docker
def test_only_hub_api_can_execute_the_callable_functions(pg_db: PgTestDatabase) -> None:
    examined = 0
    with _cur(pg_db) as cur:
        for sig in M.CALLABLE_SIGNATURES:
            for role in (*CATALOG.names, "public"):
                if role == "public":
                    cur.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_proc p, aclexplode(coalesce(p.proacl, "
                        "acldefault('f', p.proowner))) a WHERE p.oid = %s::regprocedure "
                        "AND a.grantee = 0 AND a.privilege_type = 'EXECUTE')",
                        (f"public.{sig}",),
                    )
                    assert cur.fetchone()[0] is False, f"PUBLIC can EXECUTE {sig}"
                else:
                    cur.execute(
                        "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
                        (role, f"public.{sig}"),
                    )
                    assert cur.fetchone()[0] is (role == HUB), (role, sig)
                examined += 1
        for sig in M.HELPER_SIGNATURES:
            for role in CATALOG.names:
                cur.execute(
                    "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
                    (role, f"public.{sig}"),
                )
                assert cur.fetchone()[0] is False, (role, sig)
                examined += 1
    assert examined == (len(M.CALLABLE_SIGNATURES) + len(M.HELPER_SIGNATURES)) * len(
        CATALOG.names
    ) + len(M.CALLABLE_SIGNATURES)
    with (
        _cur(pg_db, "waddles_svc_action") as cur,
        pytest.raises(psycopg2.errors.InsufficientPrivilege),
    ):
        cur.execute("SELECT * FROM pi_list_metadata()")


@requires_docker
def test_definer_functions_are_pinned(pg_db: PgTestDatabase) -> None:
    with _cur(pg_db) as cur:
        cur.execute(
            "SELECT proname, prosecdef, proconfig FROM pg_proc WHERE proname LIKE 'pi\\_%' "
            "AND pronamespace = 'public'::regnamespace"
        )
        rows = cur.fetchall()
    assert len(rows) == len(M.CALLABLE_SIGNATURES) + len(M.HELPER_SIGNATURES)
    for name, secdef, config in rows:
        assert config == ["search_path=pg_catalog, pg_temp"], name
        assert secdef is (name not in {s.split("(")[0] for s in M.HELPER_SIGNATURES}), (
            name
        )


@requires_docker
def test_metadata_functions_never_return_secrets(pg_db: PgTestDatabase) -> None:
    """NEW secret-leak test: canary strings in every secret column + config_data never escape."""
    community = _community(pg_db)
    with _cur(pg_db, HUB) as cur:
        cur.execute("SELECT * FROM pi_list_metadata()")
        listed = cur.fetchall()
        listed_cols = [d.name for d in cur.description]
        cur.execute("SELECT * FROM pi_community_status(%s)", (community,))
        status = cur.fetchall()
        status_cols = [d.name for d in cur.description]
        cur.execute("SELECT * FROM pi_list_metadata('bot', 'twitch')")
        bots = cur.fetchall()
    assert len(listed) == 2 and len(status) == 1 and len(bots) == 1
    blob = json.dumps([listed, status, bots], default=str)
    for canary in (
        CANARY_ACCESS,
        CANARY_REFRESH,
        CANARY_SECRET,
        CANARY_CFG,
        BOT_CANARY,
    ):
        assert canary not in blob, canary
    forbidden = {"access_token", "refresh_token", "client_secret"}
    assert not forbidden & set(listed_cols) and not forbidden & set(status_cols)
    assert {"access_token_set", "refresh_token_set", "client_secret_set"} <= set(
        listed_cols
    )
    cfg = status[0][status_cols.index("config_data")]
    assert cfg == {"account_label": "Chan", "nested": {"ok": 1}, "list": [{"keep": 2}]}
    meta = dict(
        zip(listed_cols, next(r for r in listed if r[1] == "twitch"), strict=True)
    )
    assert meta["access_token_set"] is True and meta["client_secret_set"] is True
    with (
        _cur(pg_db, HUB) as cur,
        pytest.raises(psycopg2.Error, match="unsupported integration_type"),
    ):
        cur.execute("SELECT * FROM pi_list_metadata('nope')")


@requires_docker
def test_secret_get_is_one_row_community_oauth_only_and_audited(
    pg_db: PgTestDatabase,
) -> None:
    community = _community(pg_db)
    with _cur(pg_db) as cur:
        cur.execute(
            "SELECT count(*) FROM credential_access_log WHERE action = 'community_secret_read'"
        )
        before = cur.fetchone()[0]
    with _cur(pg_db, HUB) as cur:
        cur.execute(
            "SELECT * FROM pi_community_secret_get(%s, 'youtube')", (community,)
        )
        row = cur.fetchone()
        cols = [d.name for d in cur.description]
        assert cols == [
            "id",
            "access_token",
            "refresh_token",
            "expires_at",
            "scopes",
            "is_encrypted",
            "seal_version",
        ]  # no client_id / client_secret (gh-969 least privilege)
        assert row[1] == CANARY_ACCESS and row[2] == CANARY_REFRESH
        # the bot row for the same platform family is NOT reachable through this lane
        cur.execute("SELECT * FROM pi_community_secret_get(%s, 'twitch')", (community,))
        assert cur.fetchall() == []
        for bad in ((0, "youtube"), (community, "nope"), (-1, "twitch")):
            with pytest.raises(psycopg2.Error, match="pi:"):
                cur.execute("SELECT * FROM pi_community_secret_get(%s, %s)", bad)
    with _cur(pg_db) as cur:
        cur.execute(
            "SELECT db_user, platform, community_id, success, detail FROM credential_access_log "
            "WHERE action = 'community_secret_read' ORDER BY id"
        )
        rows = cur.fetchall()
    assert len(rows) - before == 2
    assert rows[-2] == (HUB, "youtube", community, True, None)
    assert rows[-1] == (HUB, "twitch", community, False, None)
    assert CANARY_ACCESS not in json.dumps(rows, default=str)


@requires_docker
def test_writes_are_scoped_to_community_oauth_and_cannot_downgrade_seal(
    pg_db: PgTestDatabase,
) -> None:
    community = _community(pg_db)
    with _cur(pg_db, HUB) as cur:
        cur.execute(
            "SELECT * FROM pi_community_upsert(%s,'slack','ct-1','rt-1',NULL,%s,'Slackers',1::smallint,NULL)",
            (community, ["chat:write"]),
        )
        assert cur.fetchone()[1] == ["chat:write"]
        with pytest.raises(psycopg2.Error, match="seal downgrade refused"):
            cur.execute(
                "SELECT * FROM pi_community_upsert(%s,'slack','ct-2','rt-2',NULL,'{}','x',0::smallint,NULL)",
                (community,),
            )
        with pytest.raises(psycopg2.Error, match="access token is required"):
            cur.execute(
                "SELECT * FROM pi_community_upsert(%s,'slack','  ',NULL,NULL,'{}','x',1::smallint,NULL)",
                (community,),
            )
        # compare-and-set: stale expected ciphertext -> false, nothing written
        cur.execute(
            "SELECT pi_community_set_sealed(%s,'slack','STALE','ct-3','rt-3',NULL,1::smallint,TRUE)",
            (community,),
        )
        assert cur.fetchone()[0] is False
        cur.execute(
            "SELECT pi_community_set_sealed(%s,'slack','ct-1','ct-3',NULL,NULL,1::smallint,FALSE)",
            (community,),
        )
        assert cur.fetchone()[0] is True
        cur.execute(
            "SELECT access_token, refresh_token FROM pi_community_secret_get(%s,'slack')",
            (community,),
        )
        assert cur.fetchone() == ("ct-3", "rt-1")  # NULL refresh leaves the stored one
        with pytest.raises(psycopg2.Error, match="seal downgrade refused"):
            cur.execute(
                "SELECT pi_community_set_sealed(%s,'slack','ct-3','ct-4',NULL,NULL,0::smallint,TRUE)",
                (community,),
            )
        cur.execute("SELECT pi_community_disconnect(%s,'slack',NULL)", (community,))
        assert cur.fetchone()[0] is True
        cur.execute("SELECT pi_community_disconnect(%s,'slack',NULL)", (community,))
        assert cur.fetchone()[0] is False
        cur.execute("SELECT * FROM pi_community_secret_get(%s,'slack')", (community,))
        assert cur.fetchall() == []
    with _cur(pg_db) as cur:
        # bot lane + the seeded youtube row are untouched by every write above
        cur.execute(
            "SELECT access_token FROM platform_integrations WHERE integration_type = 'bot'"
        )
        assert cur.fetchall() == [(BOT_CANARY,)]
        cur.execute(
            "SELECT access_token, is_active FROM platform_integrations WHERE platform = 'slack'"
        )
        assert cur.fetchall() == [(None, False)]
        cur.execute(
            "SELECT count(*) FROM credential_access_log WHERE action LIKE 'community\\_%%' "
            "AND db_user = %s AND platform = 'slack'",
            (HUB,),
        )
        assert cur.fetchone()[0] == 5  # upsert, set_sealed, disconnect, 2x secret_get


@requires_docker
def test_applies_to_an_existing_db_downgrade_then_upgrade(
    pg_db: PgTestDatabase,
) -> None:
    """Existing-DB reach: from 0068 the revision re-creates + re-grants everything, idempotently."""
    env = {PASSWORDS_ENV: json.dumps(pg_db.service_role_passwords)}
    alembic_cli("downgrade", "0068_app_catalog_kind", dsn=pg_db.dsn, env_overrides=env)
    with _cur(pg_db) as cur:
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname LIKE 'pi\\_%'")
        assert cur.fetchone()[0] == 0
    alembic_cli("upgrade", "head", dsn=pg_db.dsn, env_overrides=env)
    alembic_cli(
        "upgrade", "head", dsn=pg_db.dsn, env_overrides=env
    )  # no-op, still green
    with _cur(pg_db, HUB) as cur:
        cur.execute("SELECT count(*) FROM pi_list_metadata()")
        assert cur.fetchone()[0] >= 2
    # reconcile --strict (every migrate Job) re-grants a drifted EXECUTE
    with _cur(pg_db) as cur:
        cur.execute(
            f"REVOKE EXECUTE ON FUNCTION public.{M.CALLABLE_SIGNATURES[0]} FROM {HUB}"
        )
    import subprocess
    import sys

    from pg_docker import REPO_ROOT

    subprocess.run(
        [sys.executable, "scripts/db/service_roles.py", "reconcile", "--strict"],
        cwd=REPO_ROOT,
        env={
            "DATABASE_URL": pg_db.dsn,
            PASSWORDS_ENV: env[PASSWORDS_ENV],
            "PATH": "/usr/bin:/bin",
        },
        check=True,
        capture_output=True,
    )
    with _cur(pg_db, HUB) as cur:
        cur.execute("SELECT count(*) FROM pi_list_metadata()")
        assert cur.fetchone()[0] >= 2


@requires_docker
def test_migration_fails_loud_when_owner_would_be_subject_to_force_rls() -> None:
    """The owner-bypass assertion: a non-bypass, non-owner migration role must be refused."""
    with empty_postgres("0069-rls") as db:
        alembic_cli("upgrade", "0068_app_catalog_kind", dsn=db.dsn)
        with _cur(db) as cur:
            cur.execute(
                "CREATE ROLE pi_weak LOGIN PASSWORD 'pw' NOSUPERUSER NOBYPASSRLS"
            )
            cur.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO pi_weak")
            cur.execute("GRANT ALL ON SCHEMA public TO pi_weak")
        weak = f"postgresql://pi_weak:pw@{db.host}:{db.port}/{db.dbname}"
        with pytest.raises(RuntimeError, match="would be subject to RLS"):
            alembic_cli("upgrade", "head", dsn=weak)
