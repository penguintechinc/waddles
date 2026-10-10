"""Static guards for the per-service DB role catalog and the H-1 repo-credential fix.

# regression: H-1 repo-known DB passwords must never ship in migrations / chart / images
# regression: H-3 catalog <-> Helm drift (a role the chart wires but the DB never gets)

No database or docker needed -- these run anywhere `pytest` + PyYAML do, and every check
states its denominator so scanning the wrong directory fails instead of passing vacuously.
Real-database behaviour (grants, RLS, escalation) lives in
`test_0048_per_service_db_roles.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from pg_docker import REPO_ROOT, load_service_roles_module

roles_mod = load_service_roles_module()
CATALOG = roles_mod.load_catalog()
CHART = REPO_ROOT / "k8s" / "helm" / "waddlebot"
MIGRATION_SQL_DIR = REPO_ROOT / "config" / "postgres" / "migrations"
ALEMBIC_VERSIONS = REPO_ROOT / "alembic" / "versions"

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?",
    re.IGNORECASE,
)
_CREATE_VIEW = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?", re.IGNORECASE
)
_OP_CREATE_TABLE = re.compile(r"create_table\(\s*[\"']([a-z_][a-z0-9_]*)[\"']")


def _created_relations() -> tuple[set[str], set[str]]:
    tables: set[str] = set()
    views: set[str] = set()
    files = list(MIGRATION_SQL_DIR.glob("*.sql")) + list(ALEMBIC_VERSIONS.glob("*.py"))
    assert len(files) > 100, "migration scan found too few files (wrong root?)"
    for path in files:
        text = path.read_text(encoding="utf-8")
        tables |= {m.group(1).lower() for m in _CREATE_TABLE.finditer(text)}
        tables |= {m.group(1).lower() for m in _OP_CREATE_TABLE.finditer(text)}
        views |= {m.group(1).lower() for m in _CREATE_VIEW.finditer(text)}
    return tables, views


def test_catalog_is_wellformed() -> None:
    names = CATALOG.names
    assert len(names) >= 30 and len(set(names)) == len(names)
    for spec in CATALOG.roles:
        assert spec.name.startswith("waddles_") and len(spec.name) <= 63
        assert spec.workloads, f"{spec.name} documents no workload"
        assert spec.description
        assert set(spec.member_of) <= CATALOG.external_groups
        assert (
            spec.public_dml
            or spec.tables
            or spec.member_of
            or spec.name.endswith(("_ingest", "_process", "_core"))
        ), f"{spec.name} grants nothing at all"
    # nobody gets a write privilege on migration bookkeeping
    assert {"alembic_version", "schema_migrations"} <= CATALOG.read_only_tables


def test_catalog_relations_exist_in_the_migration_chain() -> None:
    tables, views = _created_relations()
    assert len(tables) > 150
    examined = 0
    for spec in CATALOG.roles:
        for table in spec.tables:
            examined += 1
            if table not in spec.optional_tables:
                assert table in tables, f"{spec.name}: table {table!r} is created by no migration"
        for view in spec.views:
            examined += 1
            assert view in views, f"{spec.name}: view {view!r} is created by no migration"
        for table in spec.columns:
            assert table in tables, f"{spec.name}: column-grant table {table!r} unknown"
    assert examined >= 20
    for table in CATALOG.legacy_deny_tables:
        assert table in tables, f"legacy_deny_tables names unknown table {table!r}"


def test_chart_values_role_list_equals_catalog() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    chart_roles = values["infrastructure"]["postgresql"]["serviceRoles"]["roles"]
    assert list(chart_roles) == list(CATALOG.names), (
        "values.yaml infrastructure.postgresql.serviceRoles.roles drifted from "
        "config/postgres/service-roles.yaml"
    )


def test_every_documented_workload_has_exactly_one_role() -> None:
    owner: dict[str, str] = {}
    for spec in CATALOG.roles:
        for workload in spec.workloads:
            assert workload not in owner, (
                f"{workload} is claimed by {owner[workload]} and {spec.name}"
            )
            owner[workload] = spec.name
    assert len(owner) >= 35


# ---- password / dev-mode resolution ----------------------------------------------------


def _good() -> dict[str, str]:
    return roles_mod.generate_passwords(CATALOG)


def test_generated_passwords_satisfy_the_policy() -> None:
    passwords = _good()
    assert set(passwords) == set(CATALOG.names)
    assert len(set(passwords.values())) == len(passwords)
    resolved = roles_mod.resolve_passwords(
        CATALOG, {roles_mod.PASSWORDS_ENV: json.dumps(passwords)}
    )
    assert resolved == passwords


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p.pop("waddles_hub_api"), "missing a password"),
        (lambda p: p.update(waddles_hub_api="short"), "shorter"),
        (lambda p: p.update(waddles_hub_api="a" * 15 + "!" * 5), "URL-safe"),
        (lambda p: p.update(waddles_hub_api="x" * 8 + "changeme" + "y" * 8), "placeholder"),
        (lambda p: p.update(waddles_hub_api="REPLACE_ME_" + "z" * 16), "placeholder"),
        (lambda p: p.update(waddles_hub_api="hub_admin_dev_changeme"), "placeholder"),
        (lambda p: p.update(no_such_role="q" * 24), "not in the catalog"),
    ],
)
def test_weak_missing_or_unknown_passwords_fail_loud(mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    passwords = _good()
    mutate(passwords)
    with pytest.raises(roles_mod.ServiceRoleError, match=message):
        roles_mod.resolve_passwords(CATALOG, {roles_mod.PASSWORDS_ENV: json.dumps(passwords)})


def test_empty_or_malformed_env_is_refused_not_defaulted() -> None:
    for env in ({}, {roles_mod.PASSWORDS_ENV: ""}, {roles_mod.PASSWORDS_ENV: "   "}):
        with pytest.raises(roles_mod.ServiceRoleError, match="missing a password"):
            roles_mod.resolve_passwords(CATALOG, env)
    for raw in ("{not json", "[]", '{"waddles_hub_api": 1}'):
        with pytest.raises(roles_mod.ServiceRoleError):
            roles_mod.resolve_passwords(CATALOG, {roles_mod.PASSWORDS_ENV: raw})


@pytest.mark.parametrize(
    "tier", ["alpha", "beta", "gamma", "production", "prod", "BETA", " Production "]
)
def test_dev_suffix_is_refused_on_every_shared_tier(tier: str) -> None:
    env = {roles_mod.DEV_SUFFIX_ENV: "_dev_changeme", roles_mod.TIER_ENV: tier}
    with pytest.raises(roles_mod.ServiceRoleError, match="must never reach a shared database"):
        roles_mod.dev_suffix_from_env(env)
    with pytest.raises(roles_mod.ServiceRoleError):
        roles_mod.resolve_passwords(CATALOG, env)


@pytest.mark.parametrize("tier", ["", "dev", "local"])
def test_dev_suffix_derives_passwords_only_off_shared_tiers(tier: str) -> None:
    env = {roles_mod.DEV_SUFFIX_ENV: "_dev_changeme", roles_mod.TIER_ENV: tier}
    derived = roles_mod.resolve_passwords(CATALOG, env)
    assert derived["waddles_svc_action"] == "waddles_svc_action_dev_changeme"
    assert len(derived) == len(CATALOG.names)
    assert roles_mod.dev_suffix_from_env({}) == ""


@pytest.mark.parametrize("bad", ['a"b', "A", "1abc", "a;drop", "x" * 64, "", "a b"])
def test_quote_ident_rejects_unsafe_names(bad: str) -> None:
    with pytest.raises(roles_mod.ServiceRoleError):
        roles_mod.quote_ident(bad)


# ---- H-1: no repo-known credential ships anywhere but local docker-compose -------------

_REPO_PASSWORD = re.compile(r"[a-z][a-z0-9_]*_dev_changeme|\bdev123\b|kong_db_pass_change_me")


def _scan(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        if path.is_file():
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if _REPO_PASSWORD.search(line):
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    return hits


def test_no_repo_known_db_password_in_migrations_chart_or_images() -> None:
    paths = (
        sorted(MIGRATION_SQL_DIR.glob("*.sql"))
        + sorted(ALEMBIC_VERSIONS.glob("*.py"))
        + sorted((REPO_ROOT / "scripts" / "db").glob("*.py"))
        + sorted((REPO_ROOT / "migrations").glob("*"))
        + [
            p
            for p in CHART.rglob("*")
            if p.is_file()
            and p.suffix in {".yaml", ".yml", ".tpl", ".md", ".txt", ".py"}
            and p.relative_to(CHART).parts[0] not in {"charts", "tests", "__pycache__"}
        ]
        + [
            REPO_ROOT / "alembic" / "env.py",
            REPO_ROOT / "config" / "postgres" / "service-roles.yaml",
        ]
    )
    assert len(paths) > 150, "scan root is wrong -- refusing a vacuous pass"
    hits = _scan(paths)
    assert hits == [], (
        f"repo-known DB password literal shipped outside docker-compose/dev files: {hits}"
    )


def test_init_sql_is_only_mounted_by_docker_compose() -> None:
    consumers: list[str] = []
    candidates = (
        list(CHART.rglob("*"))
        + list((REPO_ROOT / "migrations").glob("*"))
        + list(REPO_ROOT.glob("**/Dockerfile*"))
        + list((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    )
    examined = 0
    for path in candidates:
        rel_parts = path.relative_to(REPO_ROOT).parts
        if not path.is_file() or ".worktrees" in rel_parts or "node_modules" in rel_parts:
            continue
        examined += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        # the chart's own credential-free ConfigMap key is also named init.sql -- only a
        # reference to the repo file (config/postgres/init.sql) is a leak
        if "config/postgres/init.sql" in text and path.name != "service-roles.yaml":
            consumers.append(str(path.relative_to(REPO_ROOT)))
    assert examined > 100
    assert consumers == [], f"config/postgres/init.sql (dev passwords) referenced by: {consumers}"
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./config/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql" in compose
    init = (REPO_ROOT / "config" / "postgres" / "init.sql").read_text(encoding="utf-8")
    assert init.startswith("-- ====") and "LOCAL DEVELOPMENT ONLY" in init.splitlines()[1]


def test_031_scoped_users_migration_carries_no_password_literal() -> None:
    sql = (MIGRATION_SQL_DIR / "031_scoped_database_users.sql").read_text(encoding="utf-8")
    assert sql.count("create_user_if_not_exists('") >= 35, "31 role set must still be created"
    assert "PASSWORD '" not in sql and "changeme" not in sql
    # production path creates NOLOGIN; the only LOGIN branch is gated on the dev GUC
    assert "CREATE ROLE %I NOLOGIN" in sql
    assert "waddles.dev_db_role_pw_suffix" in sql
