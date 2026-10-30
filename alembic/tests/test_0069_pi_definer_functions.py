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
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import (
    DOCKER_AVAILABLE,
    REPO_ROOT,
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
URL_CANARY = "https://hooks.example.com/cb?token=CANARY-URL-TOKEN-xyz"
OWNER = "waddles_pi_definer"
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
    """Fresh-replayed (empty DB -> head) container; two tenants/communities + canary rows."""
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
            ids = []
            for slug, cname in (("pi", "pi-c"), ("pi-b", "pi-other")):
                cur.execute(
                    "INSERT INTO tenants (slug, display_name) VALUES (%s, %s) RETURNING id",
                    (slug, slug),
                )
                tenant = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
                    (cname, tenant),
                )
                ids.append((tenant, cur.fetchone()[0]))
            cur.execute(
                "INSERT INTO platform_integrations (platform, integration_type, access_token, "
                "refresh_token, client_id, client_secret, config_data) VALUES "
                "('twitch','bot',%s,%s,'bot-client',%s,%s)",
                (
                    BOT_CANARY,
                    BOT_CANARY,
                    BOT_CANARY,
                    json.dumps(
                        {"note": "bot", "auth_code": CANARY_CFG, "url": URL_CANARY}
                    ),
                ),
            )
            cur.execute(
                "INSERT INTO platform_integrations (platform, integration_type, community_id, "
                "access_token, refresh_token, client_id, client_secret, config_data, scopes, "
                "is_encrypted, seal_version) VALUES ('youtube','community_oauth',%s,%s,%s,"
                "'cid',%s,%s,%s,TRUE,1)",
                (
                    ids[0][1],
                    CANARY_ACCESS,
                    CANARY_REFRESH,
                    CANARY_SECRET,
                    json.dumps(
                        {
                            "account_label": "Chan",
                            # secrets under ARBITRARY keys / inside a value must never escape
                            "auth_code": CANARY_CFG,
                            "cookie": CANARY_CFG,
                            "session_key": CANARY_CFG,
                            "pem": CANARY_CFG,
                            "callback": URL_CANARY,
                            "nested": {"client_secret": CANARY_CFG, "ok": 1},
                            "list": [{"refresh_token": CANARY_CFG, "keep": 2}],
                        }
                    ),
                    ["a", "b"],
                ),
            )
            # non-string account_label (an object smuggling a secret) -> allowlist drops it
            cur.execute(
                "INSERT INTO platform_integrations (platform, integration_type, community_id, "
                "access_token, config_data) VALUES ('slack','community_oauth',%s,'x',%s)",
                (ids[0][1], json.dumps({"account_label": {"token": CANARY_CFG}})),
            )
        db_ids[db.dbname] = ids
        yield db


db_ids: dict[str, list[tuple[int, int]]] = {}


def _world(db: PgTestDatabase) -> tuple[int, int, int, int]:
    """`(tenant_a, community_a, tenant_b, community_b)`."""
    (ta, ca), (tb, cb) = db_ids[db.dbname]
    return ta, ca, tb, cb


def _community(db: PgTestDatabase) -> int:
    return _world(db)[1]


def _audit_rows(db: PgTestDatabase, since_id: int) -> list[tuple[Any, ...]]:
    with _cur(db) as cur:
        cur.execute(
            "SELECT action, success, detail, db_user, platform, community_id "
            "FROM credential_access_log WHERE id > %s ORDER BY id",
            (since_id,),
        )
        return list(cur.fetchall())


def _audit_mark(db: PgTestDatabase) -> int:
    with _cur(db) as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM credential_access_log")
        return int(cur.fetchone()[0])


class TestStatic:
    def test_revision_chain(self) -> None:
        assert M.revision == "0069_pi_definer_functions"
        assert len(M.revision) <= 32
        assert M.down_revision == "0068_app_catalog_kind"

    def test_catalog_lists_exactly_the_callable_signatures(self) -> None:
        listed = set(next(r for r in CATALOG.roles if r.name == HUB).functions)
        assert len(M.CALLABLE_SIGNATURES) == 6
        assert set(M.CALLABLE_SIGNATURES) <= listed  # by exact SIGNATURE, not bare name
        pi_entries = {f for f in listed if f.startswith("pi_")}
        assert pi_entries == set(M.CALLABLE_SIGNATURES)
        assert all("(" in f for f in pi_entries)
        assert not {s.split("(")[0] for s in M.HELPER_SIGNATURES} & {
            f.split("(")[0] for f in listed
        }

    def test_every_definer_body_pins_search_path_and_no_dynamic_sql(self) -> None:
        sql = M._GROUP_A_SQL + M._GROUP_B_SQL
        assert sql.count("SECURITY DEFINER") == 6
        assert sql.count("SET search_path = pg_catalog, pg_temp") == 6
        assert "EXECUTE format" not in sql and "EXECUTE '" not in sql
        assert "'bot'" not in M._GROUP_B_SQL.replace(
            "-- ", ""
        )  # lane is community_oauth only
        body = M._GROUP_B_SQL.split("pi_community_secret_get")[1].split(
            "pi_community_upsert"
        )[0]
        returns = body.split("RETURNS TABLE")[1].split("LANGUAGE")[0]
        for secret_col in ("client_secret", "client_id"):
            assert secret_col not in returns

    def test_no_caller_supplied_actor_parameter(self) -> None:
        sql = M._GROUP_B_SQL
        assert "p_actor" not in sql and "created_by_user_id" not in sql
        assert "updated_by_user_id" not in sql

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

    def test_config_is_an_allowlist_not_a_denylist(self) -> None:
        sql = M._HELPERS_SQL
        assert "pi_public_config" in sql and "account_label" in sql
        assert (
            "!~*" not in sql and "jsonb_object_agg" not in sql
        )  # no key-name denylist


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
        # nor may it become the definer owner
        cur.execute(
            "SELECT count(*) FROM pg_auth_members am JOIN pg_roles g ON g.oid = am.roleid "
            "JOIN pg_roles m ON m.oid = am.member WHERE g.rolname = %s AND m.rolname = %s",
            (OWNER, HUB),
        )
        assert cur.fetchone()[0] == 0
    with _cur(pg_db, HUB) as cur, pytest.raises(psycopg2.Error):
        cur.execute(f"SET ROLE {OWNER}")


@requires_docker
def test_definer_owner_is_a_dedicated_non_superuser_bypassrls_role(
    pg_db: PgTestDatabase,
) -> None:
    with _cur(pg_db) as cur:
        cur.execute(
            "SELECT rolsuper, rolbypassrls, rolcanlogin, rolcreaterole, rolcreatedb, "
            "rolreplication FROM pg_roles WHERE rolname = %s",
            (OWNER,),
        )
        assert cur.fetchone() == (False, True, False, False, False, False)
        cur.execute(
            "SELECT p.proname, r.rolname, r.rolsuper FROM pg_proc p JOIN pg_roles r "
            "ON r.oid = p.proowner WHERE p.proname LIKE 'pi\\_%' "
            "AND p.pronamespace = 'public'::regnamespace"
        )
        rows = cur.fetchall()
        assert len(rows) == len(M.CALLABLE_SIGNATURES) + len(M.HELPER_SIGNATURES)
        assert {(r[1], r[2]) for r in rows} == {(OWNER, False)}
        # least privilege: no DELETE, no other tables
        for table, privs in (
            ("platform_integrations", {"SELECT", "INSERT", "UPDATE"}),
            ("credential_access_log", {"INSERT"}),
            ("communities", {"SELECT"}),
        ):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (OWNER, f"public.{table}", priv),
                )
                assert cur.fetchone()[0] is (priv in privs), (table, priv)
        cur.execute(
            "SELECT has_table_privilege(%s, 'public.hub_users', 'SELECT')", (OWNER,)
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
        # exact ACL: grantees of every pi_* function are only {owner, (hub-api if callable)}
        cur.execute(
            "SELECT p.oid::regprocedure::text, array_agg(DISTINCT r.rolname) FROM pg_proc p, "
            "LATERAL aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "WHERE p.proname LIKE 'pi\\_%' AND p.pronamespace = 'public'::regnamespace "
            "GROUP BY 1"
        )
        acls = dict(cur.fetchall())
    assert len(acls) == len(M.CALLABLE_SIGNATURES) + len(M.HELPER_SIGNATURES)
    for sig, grantees in acls.items():
        callable_ = any(
            sig.startswith(c.split("(")[0] + "(") for c in M.CALLABLE_SIGNATURES
        )
        assert set(grantees) == ({OWNER, HUB} if callable_ else {OWNER}), (
            sig,
            grantees,
        )
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
def test_metadata_never_returns_secrets_config_is_an_allowlist(
    pg_db: PgTestDatabase,
) -> None:
    """Canaries in every secret column AND under arbitrary config keys / URL values never escape."""
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
    assert len(listed) == 3 and len(status) == 2 and len(bots) == 1
    blob = json.dumps([listed, status, bots], default=str)
    for canary in (
        CANARY_ACCESS,
        CANARY_REFRESH,
        CANARY_SECRET,
        CANARY_CFG,
        BOT_CANARY,
        URL_CANARY,
    ):
        assert canary not in blob, canary
    forbidden = {"access_token", "refresh_token", "client_secret"}
    assert not forbidden & set(listed_cols) and not forbidden & set(status_cols)
    assert {"access_token_set", "refresh_token_set", "client_secret_set"} <= set(
        listed_cols
    )
    by_platform = {
        r[status_cols.index("platform")]: r[status_cols.index("config_data")]
        for r in status
    }
    assert by_platform["youtube"] == {
        "account_label": "Chan"
    }  # ONLY the allowlisted key
    assert by_platform["slack"] == {}  # non-string account_label dropped entirely
    bot_cfg = bots[0][listed_cols.index("config_data")]
    assert bot_cfg == {}  # bot row: `note`, `auth_code`, `url` are not allowlisted
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
    mark = _audit_mark(pg_db)
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
        # refusals are RETURNED (empty), never raised -- so their audit row commits
        for bad in ((0, "youtube"), (community, "nope<script>"), (-1, "twitch")):
            cur.execute("SELECT * FROM pi_community_secret_get(%s, %s)", bad)
            assert cur.fetchall() == []
    rows = _audit_rows(pg_db, mark)
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("community_secret_read", True, "ok"),
        ("community_secret_read", False, "not_found"),
        ("community_secret_read", False, "invalid_args"),
        ("community_secret_read", False, "invalid_args"),
        ("community_secret_read", False, "invalid_args"),
    ]
    assert all(r[3] == HUB for r in rows)
    assert rows[0][4:] == ("youtube", community) and rows[1][4:] == (
        "twitch",
        community,
    )
    assert rows[3][4] is None  # caller-supplied platform text is never logged
    assert CANARY_ACCESS not in json.dumps(rows, default=str)


@requires_docker
def test_foreign_community_is_rejected_in_the_database_and_audited(
    pg_db: PgTestDatabase,
) -> None:
    """Defence in depth: a validated tenant_id rejects another tenant's community IN SQL."""
    ta, ca, tb, cb = _world(pg_db)
    mark = _audit_mark(pg_db)
    with _cur(pg_db, HUB) as cur:
        # community B belongs to tenant B: every Group B call scoped to tenant A is refused
        cur.execute("SELECT * FROM pi_community_secret_get(%s,'youtube',%s)", (cb, ta))
        assert cur.fetchall() == []
        cur.execute(
            "SELECT outcome, id FROM pi_community_upsert(%s,'youtube','x','y',NULL,'{}','l',"
            "1::smallint,%s)",
            (cb, ta),
        )
        assert cur.fetchone() == ("forbidden_tenant", None)
        cur.execute("SELECT pi_community_disconnect(%s,'youtube',%s)", (cb, ta))
        assert cur.fetchone()[0] == "forbidden_tenant"
        cur.execute(
            "SELECT pi_community_set_sealed(%s,'youtube','a','b',NULL,NULL,1::smallint,TRUE,%s)",
            (cb, ta),
        )
        assert cur.fetchone()[0] == "forbidden_tenant"
        cur.execute("SELECT * FROM pi_community_status(%s,NULL,%s)", (cb, ta))
        assert cur.fetchall() == []
        # the right tenant works; NULL (trusted system plane) works
        cur.execute("SELECT * FROM pi_community_secret_get(%s,'youtube',%s)", (ca, ta))
        assert len(cur.fetchall()) == 1
        cur.execute("SELECT * FROM pi_community_status(%s,NULL,%s)", (ca, ta))
        assert len(cur.fetchall()) == 2
        cur.execute("SELECT * FROM pi_community_secret_get(%s,'youtube',NULL)", (ca,))
        assert len(cur.fetchall()) == 1
        # tenant A cannot read A's community by claiming tenant B either
        cur.execute("SELECT * FROM pi_community_secret_get(%s,'youtube',%s)", (ca, tb))
        assert cur.fetchall() == []
    denied = [r for r in _audit_rows(pg_db, mark) if r[2] == "forbidden_tenant"]
    assert [r[0] for r in denied] == [
        "community_secret_read",
        "community_upsert",
        "community_disconnect",
        "community_set_sealed",
        "community_secret_read",
    ]
    assert all(r[1] is False and r[3] == HUB for r in denied)
    with _cur(pg_db) as cur:  # nothing was written to community B
        cur.execute(
            "SELECT count(*) FROM platform_integrations WHERE community_id = %s", (cb,)
        )
        assert cur.fetchone()[0] == 0


@requires_docker
def test_writes_are_scoped_to_community_oauth_cannot_downgrade_and_every_call_is_audited(
    pg_db: PgTestDatabase,
) -> None:
    ta, community, _, _ = _world(pg_db)
    mark = _audit_mark(pg_db)
    expected: list[tuple[str, bool, str]] = []
    with _cur(pg_db, HUB) as cur:

        def upsert(
            access: str, seal: int, label: str = "Slackers", plat: str = "slack"
        ) -> Any:
            cur.execute(
                "SELECT * FROM pi_community_upsert(%s,%s,%s,%s,NULL,%s,%s,%s::smallint,%s)",
                (community, plat, access, "rt-1", ["chat:write"], label, seal, ta),
            )
            return cur.fetchone()

        r = upsert("ct-1", 1)
        assert (
            r[0] == "ok"
            and r[2] == ["chat:write"]
            and r[5] == {"account_label": "Slackers"}
        )
        expected.append(("community_upsert", True, "ok"))
        assert upsert("ct-2", 0)[0] == "seal_downgrade"
        expected.append(("community_upsert", False, "seal_downgrade"))
        assert upsert("  ", 1)[0] == "invalid_args"
        expected.append(("community_upsert", False, "invalid_args"))
        assert upsert("ct-9", 1, plat="evil")[0] == "invalid_args"
        expected.append(("community_upsert", False, "invalid_args"))

        def sealed(expected_access: str, access: str, seal: int) -> str:
            cur.execute(
                "SELECT pi_community_set_sealed(%s,'slack',%s,%s,%s,NULL,%s::smallint,FALSE,%s)",
                (community, expected_access, access, None, seal, ta),
            )
            return str(cur.fetchone()[0])

        assert sealed("STALE", "ct-3", 1) == "stale"
        expected.append(("community_set_sealed", False, "stale"))
        assert sealed("ct-1", "ct-3", 1) == "ok"
        expected.append(("community_set_sealed", True, "ok"))
        assert sealed("ct-3", "ct-4", 0) == "seal_downgrade"
        expected.append(("community_set_sealed", False, "seal_downgrade"))
        assert sealed("ct-3", " ", 1) == "invalid_args"
        expected.append(("community_set_sealed", False, "invalid_args"))
        cur.execute(
            "SELECT access_token, refresh_token FROM pi_community_secret_get(%s,'slack',%s)",
            (community, ta),
        )
        assert cur.fetchone() == ("ct-3", "rt-1")  # NULL refresh leaves the stored one
        expected.append(("community_secret_read", True, "ok"))
        for want in ("ok", "not_found"):
            cur.execute(
                "SELECT pi_community_disconnect(%s,'slack',%s)", (community, ta)
            )
            assert cur.fetchone()[0] == want
        expected += [
            ("community_disconnect", True, "ok"),
            ("community_disconnect", False, "not_found"),
        ]
        cur.execute("SELECT pi_community_disconnect(%s,'nope',%s)", (community, ta))
        assert cur.fetchone()[0] == "invalid_args"
        expected.append(("community_disconnect", False, "invalid_args"))
        cur.execute(
            "SELECT pi_community_set_sealed(%s,'slack','a','b',NULL,NULL,1::smallint,FALSE,%s)",
            (community, ta),
        )
        assert cur.fetchone()[0] == "not_found"
        expected.append(("community_set_sealed", False, "not_found"))
    # EVERY call -- success, not found, stale, invalid, downgrade -- left exactly one audit row
    rows = _audit_rows(pg_db, mark)
    assert [(r[0], r[1], r[2]) for r in rows] == expected
    assert len(rows) == len(expected) == 13
    assert all(r[3] == HUB for r in rows)
    with _cur(pg_db) as cur:
        # bot lane + the seeded youtube row are untouched by every write above
        cur.execute(
            "SELECT access_token FROM platform_integrations WHERE integration_type='bot'"
        )
        assert cur.fetchall() == [(BOT_CANARY,)]
        cur.execute(
            "SELECT access_token, is_active, created_by_user_id, updated_by_user_id "
            "FROM platform_integrations WHERE platform = 'slack' AND access_token IS NULL"
        )
        # no end-user attribution is persisted (the DB cannot verify it)
        assert cur.fetchall() == [(None, False, None, None)]


@requires_docker
def test_applies_to_an_existing_db_downgrade_then_upgrade_and_drops_stray_overloads(
    pg_db: PgTestDatabase,
) -> None:
    """Existing-DB reach + ACL rebuild: re-run re-creates, re-grants by signature, drops strays."""
    env = {PASSWORDS_ENV: json.dumps(pg_db.service_role_passwords)}
    alembic_cli("downgrade", "0068_app_catalog_kind", dsn=pg_db.dsn, env_overrides=env)
    with _cur(pg_db) as cur:
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname LIKE 'pi\\_%'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s", (OWNER,))
        assert cur.fetchone()[0] == 0
        # a stray overload / look-alike granted broadly, left behind by "someone"
        cur.execute(
            "CREATE FUNCTION public.pi_list_metadata(integer) RETURNS int "
            "LANGUAGE sql AS 'SELECT 1'"
        )
        cur.execute(
            "GRANT EXECUTE ON FUNCTION public.pi_list_metadata(integer) TO PUBLIC"
        )
    alembic_cli("upgrade", "head", dsn=pg_db.dsn, env_overrides=env)
    alembic_cli(
        "upgrade", "head", dsn=pg_db.dsn, env_overrides=env
    )  # no-op, still green
    with _cur(pg_db) as cur:
        cur.execute("SELECT to_regprocedure('public.pi_list_metadata(integer)')")
        assert (
            cur.fetchone()[0] is None
        )  # stray overload dropped, cannot inherit anything
    with _cur(pg_db, HUB) as cur:
        cur.execute("SELECT count(*) FROM pi_list_metadata()")
        assert cur.fetchone()[0] >= 2


@requires_docker
def test_reconcile_grants_by_exact_signature_not_bare_name(
    pg_db: PgTestDatabase,
) -> None:
    sig0 = M.CALLABLE_SIGNATURES[0]
    with _cur(pg_db) as cur:
        cur.execute(f"REVOKE EXECUTE ON FUNCTION public.{sig0} FROM {HUB}")
        # a same-named overload created later must NOT inherit hub-api's grant on reconcile
        cur.execute(
            "CREATE FUNCTION public.pi_list_metadata(bigint) RETURNS int "
            "LANGUAGE sql AS 'SELECT 1'"
        )
        cur.execute(
            "REVOKE EXECUTE ON FUNCTION public.pi_list_metadata(bigint) FROM PUBLIC"
        )
    subprocess.run(
        [sys.executable, "scripts/db/service_roles.py", "reconcile", "--strict"],
        cwd=REPO_ROOT,
        env={
            "DATABASE_URL": pg_db.dsn,
            PASSWORDS_ENV: json.dumps(pg_db.service_role_passwords),
            "PATH": "/usr/bin:/bin",
        },
        check=True,
        capture_output=True,
    )
    with _cur(pg_db) as cur:
        cur.execute(
            "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
            (HUB, f"public.{sig0}"),
        )
        assert cur.fetchone()[0] is True  # drifted grant repaired
        cur.execute(
            "SELECT has_function_privilege(%s, 'public.pi_list_metadata(bigint)'::regprocedure, "
            "'EXECUTE')",
            (HUB,),
        )
        assert cur.fetchone()[0] is False  # overload did NOT inherit it
        cur.execute("DROP FUNCTION public.pi_list_metadata(bigint)")


@requires_docker
def test_migration_fails_loud_without_a_superuser_owner() -> None:
    """A non-superuser migration role is refused (cannot create BYPASSRLS / re-own)."""
    with empty_postgres("0069-rls") as db:
        alembic_cli("upgrade", "0068_app_catalog_kind", dsn=db.dsn)
        with _cur(db) as cur:
            cur.execute(
                "CREATE ROLE pi_weak LOGIN PASSWORD 'pw' NOSUPERUSER NOBYPASSRLS"
            )
            cur.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO pi_weak")
            cur.execute("GRANT ALL ON SCHEMA public TO pi_weak")
        weak = f"postgresql://pi_weak:pw@{db.host}:{db.port}/{db.dbname}"
        with pytest.raises(RuntimeError, match="superuser|permission denied"):
            alembic_cli("upgrade", "head", dsn=weak)
