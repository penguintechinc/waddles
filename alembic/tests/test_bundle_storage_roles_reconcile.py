"""`scripts/db/bundle_storage_roles.py` against a REAL Postgres (and the real rendered chart).

# regression: alpha helm upgrade 2026-11-16 -- waddles_bundle_migrator existed with NO password
# (0030 ran without BUNDLE_MIGRATOR_PASSWORD), so the core-bundle seeder failed
# `bundle_data_provisioning_unavailable` for penguintechinc.waddles.music-requests.

`migrated_postgres` migrates WITHOUT BUNDLE_MIGRATOR_PASSWORD / BUNDLE_RUNTIME_PASSWORD -- exactly
alpha's state. Covers: no password -> set; already set -> verifier byte-identical (true no-op);
changed by design -> applied; missing role -> created least-privilege with 0030's grants; weak
value -> refused; the run-alembic.sh CLI never prints a value; and the post-render round trip
(helm template -> real provision-secrets hook -> db-migrate Secret -> reconcile -> the password in
`waddlebot-bundle-migrator` authenticates as the migrator and can run DDL in app_core).

Skipped (never failed) without `docker`; the chart round trip also needs `helm`.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest
import sqlalchemy as sa
import yaml
from pg_docker import DOCKER_AVAILABLE, REPO_ROOT, PgTestDatabase, migrated_postgres

sys.path.insert(0, str(REPO_ROOT / "scripts" / "db"))
import bundle_storage_roles as bsr  # noqa: E402

CHART = REPO_ROOT / "k8s" / "helm" / "waddlebot"
sys.path.insert(0, str(CHART / "tests"))
from provision_harness import with_provisioned  # noqa: E402

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker CLI not available")

MIGRATOR_PW = (
    secrets.token_urlsafe(24)  # throwaway, container destroyed at exit
)
RUNTIME_PW = secrets.token_urlsafe(24)  # throwaway


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    with migrated_postgres("bundle-storage-roles") as database:
        yield database


def _engine(db: PgTestDatabase) -> sa.engine.Engine:
    return sa.create_engine(
        db.dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
    )


def _verifier(db: PgTestDatabase, role: str) -> str | None:
    with psycopg2.connect(db.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,))
        row = cur.fetchone()
    return row[0] if row else None


def _reconcile(db: PgTestDatabase, env: dict[str, str]) -> list[bsr.RoleResult]:
    engine = _engine(db)
    try:
        with engine.begin() as conn:
            return bsr.reconcile(conn, env)
    finally:
        engine.dispose()


def _login(db: PgTestDatabase, role: str, password: str) -> tuple[str, bool]:
    with (
        psycopg2.connect(
            host=db.host,
            port=db.port,
            dbname=db.dbname,
            user=role,
            password=password,
            connect_timeout=10,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"
        )
        user, sup = cur.fetchone()
    return user, sup


def _env() -> dict[str, str]:
    return {
        bsr.MIGRATOR_PASSWORD_ENV: MIGRATOR_PW,
        bsr.RUNTIME_PASSWORD_ENV: RUNTIME_PW,
    }


def test_1_alpha_state_role_without_password_gets_the_secret_value(
    db: PgTestDatabase,
) -> None:
    assert _verifier(db, bsr.MIGRATOR_ROLE) is None, (
        "precondition: 0030 left the role passwordless"
    )
    with pytest.raises(psycopg2.OperationalError):
        _login(db, bsr.MIGRATOR_ROLE, MIGRATOR_PW)
    results = {r.role: r for r in _reconcile(db, _env())}
    assert (
        results[bsr.MIGRATOR_ROLE].password_changed
        and not results[bsr.MIGRATOR_ROLE].created
    )
    assert _login(db, bsr.MIGRATOR_ROLE, MIGRATOR_PW) == (bsr.MIGRATOR_ROLE, False)
    assert _login(db, bsr.RUNTIME_ROLE, RUNTIME_PW) == (bsr.RUNTIME_ROLE, False)
    # migrator can still do its one job: DDL in the schema it owns
    with (
        psycopg2.connect(
            host=db.host,
            port=db.port,
            dbname=db.dbname,
            user=bsr.MIGRATOR_ROLE,
            password=MIGRATOR_PW,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("CREATE TABLE app_core.reconcile_probe (id int)")
        cur.execute("DROP TABLE app_core.reconcile_probe")


def test_2_rerun_with_same_secret_is_a_true_no_op(db: PgTestDatabase) -> None:
    before = (_verifier(db, bsr.MIGRATOR_ROLE), _verifier(db, bsr.RUNTIME_ROLE))
    assert all(before)
    results = _reconcile(db, _env())
    assert [r.password_changed for r in results] == [False, False]
    assert (_verifier(db, bsr.MIGRATOR_ROLE), _verifier(db, bsr.RUNTIME_ROLE)) == before


def test_3_changed_secret_is_applied(db: PgTestDatabase) -> None:
    rotated = secrets.token_urlsafe(24)
    before = _verifier(db, bsr.MIGRATOR_ROLE)
    results = {
        r.role: r
        for r in _reconcile(db, {**_env(), bsr.MIGRATOR_PASSWORD_ENV: rotated})
    }
    assert results[bsr.MIGRATOR_ROLE].password_changed
    assert not results[bsr.RUNTIME_ROLE].password_changed
    assert _verifier(db, bsr.MIGRATOR_ROLE) != before
    assert _login(db, bsr.MIGRATOR_ROLE, rotated)[0] == bsr.MIGRATOR_ROLE
    with pytest.raises(psycopg2.OperationalError):
        _login(db, bsr.MIGRATOR_ROLE, MIGRATOR_PW)
    _reconcile(db, _env())  # restore for later tests


def test_4_missing_role_is_recreated_least_privilege_with_0030_grants(
    db: PgTestDatabase,
) -> None:
    with psycopg2.connect(db.dsn) as conn, conn.cursor() as cur:
        cur.execute(f"DROP OWNED BY {bsr.RUNTIME_ROLE}")
        cur.execute(f"DROP ROLE {bsr.RUNTIME_ROLE}")
    results = {r.role: r for r in _reconcile(db, _env())}
    assert (
        results[bsr.RUNTIME_ROLE].created and results[bsr.RUNTIME_ROLE].password_changed
    )
    with psycopg2.connect(db.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls"
            " FROM pg_roles WHERE rolname = %s",
            (bsr.RUNTIME_ROLE,),
        )
        assert cur.fetchone() == (True, False, False, False, False, False)
        cur.execute(
            "SELECT bool_and(has_schema_privilege(%s, s, 'USAGE')),"
            " bool_or(has_schema_privilege(%s, s, 'CREATE'))"
            " FROM unnest(ARRAY['app_core', 'app_community']) s",
            (bsr.RUNTIME_ROLE, bsr.RUNTIME_ROLE),
        )
        assert cur.fetchone() == (True, False)
    assert _login(db, bsr.RUNTIME_ROLE, RUNTIME_PW)[0] == bsr.RUNTIME_ROLE


@pytest.mark.parametrize("weak", ["short", "x" * 15])
def test_5_weak_value_is_refused_and_nothing_changes(
    db: PgTestDatabase, weak: str
) -> None:
    before = _verifier(db, bsr.MIGRATOR_ROLE)
    with pytest.raises(ValueError, match=bsr.MIGRATOR_PASSWORD_ENV):
        _reconcile(db, {bsr.MIGRATOR_PASSWORD_ENV: weak})
    assert _verifier(db, bsr.MIGRATOR_ROLE) == before


def test_6_cli_never_prints_a_value_and_skips_when_disabled(db: PgTestDatabase) -> None:
    script = str(REPO_ROOT / "scripts" / "db" / "bundle_storage_roles.py")
    base = {"DATABASE_URL": db.dsn, "PATH": "/usr/bin:/bin:/usr/local/bin"}
    run = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [sys.executable, script],
        env={**base, **_env()},
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert (
        "Reconciled waddles_bundle_migrator: created=no password_changed=no"
        in run.stdout
    )
    assert (
        MIGRATOR_PW not in run.stdout + run.stderr
        and RUNTIME_PW not in run.stdout + run.stderr
    )
    off = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [sys.executable, script], env=base, capture_output=True, text=True, check=False
    )
    assert off.returncode == 0 and "skipped" in off.stdout


def _render_dal2alpha() -> list[dict[str, Any]]:
    res = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [
            "helm",
            "template",
            "waddlebot",
            str(CHART),
            "--kube-version",
            "1.30.0",
            "--values",
            str(CHART / "values-dal2alpha.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    return [d for d in yaml.safe_load_all(res.stdout) if d]


@pytest.mark.skipif(not shutil.which("helm"), reason="helm CLI not available")
def test_7_post_render_chart_secret_and_role_match(db: PgTestDatabase) -> None:
    """helm template -> real hook -> db-migrate Secret env -> reconcile -> chart Secret logs in."""
    docs = with_provisioned(_render_dal2alpha())
    secrets = {
        d["metadata"]["name"]: dict(d.get("stringData") or {})
        for d in docs
        if d["kind"] == "Secret" and d.get("x-provisioned")
    }
    hook = secrets["waddlebot-db-migrate-secret"]
    job_env = {
        k: hook[k] for k in (bsr.MIGRATOR_PASSWORD_ENV, bsr.RUNTIME_PASSWORD_ENV)
    }
    results = _reconcile(db, job_env)
    assert {r.role for r in results} == {bsr.MIGRATOR_ROLE, bsr.RUNTIME_ROLE}
    migrator_pw = secrets["waddlebot-bundle-migrator"]["BUNDLE_MIGRATOR_PASSWORD"]
    runtime_pw = secrets["waddlebot-bundle-db"]["BUNDLE_DB_PASSWORD"]
    assert _login(db, bsr.MIGRATOR_ROLE, migrator_pw) == (bsr.MIGRATOR_ROLE, False)
    assert _login(db, bsr.RUNTIME_ROLE, runtime_pw) == (bsr.RUNTIME_ROLE, False)
    # the hub-api / seeder pods read exactly that Secret+key
    for doc in docs:
        if doc["kind"] != "Job" or not doc["metadata"]["name"].endswith(
            "-core-bundle-seeder"
        ):
            continue
        env = {
            e["name"]: e
            for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        ref = env["BUNDLE_MIGRATOR_PASSWORD"]["valueFrom"]["secretKeyRef"]
        assert secrets[ref["name"]][ref["key"]] == migrator_pw
        break
    else:
        pytest.fail("no core-bundle seeder Job rendered")
    # second run of the same hook values: no-op
    assert [r.password_changed for r in _reconcile(db, job_env)] == [False, False]
    # alembic 0030 reads BUNDLE_{MIGRATOR,RUNTIME}_PASSWORD and would bridge the plaintext through a
    # server-side GUC: the db-migrate hook must never hand it those names.
    assert (
        "BUNDLE_MIGRATOR_PASSWORD" not in hook and "BUNDLE_RUNTIME_PASSWORD" not in hook
    )


# Security review (sensitive-data-in-logs): the plaintext never reaches the server.


def test_8_server_never_receives_the_plaintext(db: PgTestDatabase) -> None:
    """Every statement + parameter set sent to Postgres is captured; none contains a password."""
    new_migrator, new_runtime = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    with (
        psycopg2.connect(db.dsn) as conn,
        conn.cursor() as cur,
    ):  # force the CREATE path too
        cur.execute(f"DROP OWNED BY {bsr.RUNTIME_ROLE}")
        cur.execute(f"DROP ROLE {bsr.RUNTIME_ROLE}")
    sent: list[str] = []
    engine = _engine(db)

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, parameters, _context, _many):  # type: ignore[no-untyped-def]
        sent.append(f"{statement}\n{parameters!r}")

    env = {
        bsr.MIGRATOR_PASSWORD_ENV: new_migrator,
        bsr.RUNTIME_PASSWORD_ENV: new_runtime,
    }
    try:
        with engine.begin() as conn:
            results = {r.role: r for r in bsr.reconcile(conn, env)}
    finally:
        engine.dispose()
    assert results[bsr.MIGRATOR_ROLE].password_changed
    assert results[bsr.RUNTIME_ROLE].created
    role_ddl = [s for s in sent if "ROLE " in s and "PASSWORD" in s]
    assert len(role_ddl) == 2, (
        f"expected ALTER + CREATE, examined {len(sent)} statements"
    )
    assert all("'SCRAM-SHA-256$4096:" in s for s in role_ddl)
    assert any("log_statement" in s for s in sent), (
        "defence-in-depth SET LOCAL not issued"
    )
    for s in sent:
        assert new_migrator not in s and new_runtime not in s
    # the server stored exactly the client-computed verifier, and it logs in
    stored = _verifier(db, bsr.MIGRATOR_ROLE)
    assert stored and any(stored in s for s in role_ddl)
    assert _login(db, bsr.MIGRATOR_ROLE, new_migrator)[0] == bsr.MIGRATOR_ROLE
    assert _login(db, bsr.RUNTIME_ROLE, new_runtime)[0] == bsr.RUNTIME_ROLE
    # scram_matches recognises the unchanged password -> the next run is a no-op
    assert bsr.scram_matches(stored, new_migrator) and not bsr.scram_matches(
        stored, MIGRATOR_PW
    )
    assert [r.password_changed for r in _reconcile(db, env)] == [False, False]
    _reconcile(db, _env())  # restore for any later test


def test_9_client_verifier_round_trips_and_is_salted() -> None:
    pw = secrets.token_urlsafe(24)
    a, b = bsr.scram_verifier(pw), bsr.scram_verifier(pw)
    assert a != b, "a fresh random salt per verifier"
    assert bsr.scram_matches(a, pw) and bsr.scram_matches(b, pw)
    assert not bsr.scram_matches(a, pw + "x")
    assert pw not in a
    with pytest.raises(ValueError):
        bsr.scram_verifier("pässwörd-not-ascii-123")
