"""Per-service Postgres LOGIN roles: catalog loader, SQL renderer, idempotent reconcile.

Security findings H-1 (repo-known DB passwords) and H-3 (one shared DB superuser for
every workload). The single place that turns `config/postgres/service-roles.yaml` into
live roles/grants, imported by:

* `alembic/versions/0049_per_service_db_roles.py` -- provisions the roles inside the
  migration chain (and neutralizes the repo-credentialed legacy roles);
* `migrations/run-alembic.sh` -- re-asserts the exact catalog on EVERY db-migrate Job
  run (`python3 scripts/db/service_roles.py reconcile --strict`), which is how password
  rotation, tables added by later migrations, and out-of-band grant drift are repaired.

**Credentials.** Passwords are never in the repo. They arrive as a JSON object
(`WADDLES_DB_SERVICE_ROLE_PASSWORDS`, role -> password) rendered by the Helm chart from
the auto-provisioned / existingSecret mechanism. They reach SQL only through a bound
`set_config` GUC read back inside a `DO $$` block via `format('%L')` -- never
interpolated into SQL text, never logged, never a CLI argument (same technique as
`scripts/db/bundle_reader_role.py`).

**Dev-only convenience.** `WADDLES_DEV_DB_ROLE_PW_SUFFIX` (docker-compose's
`db-migrations` service only) derives `<role><suffix>` passwords when no JSON is
supplied. It is refused outright when `WADDLES_DEPLOYMENT_TIER` is alpha/beta/gamma/
production, so a stray dev variable can never put guessable credentials on a shared DB.

**Fail loud.** Missing/empty/weak/unknown-role passwords raise `ServiceRoleError`; in
`strict` mode a catalog table that does not exist raises `CatalogDriftError` instead of
being silently skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import string
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from sqlalchemy.engine import Connection

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = REPO_ROOT / "config" / "postgres" / "service-roles.yaml"
DEFAULT_MATRIX_PATH = REPO_ROOT / "config" / "postgres" / "rbac-matrix.yaml"

PASSWORDS_ENV = "WADDLES_DB_SERVICE_ROLE_PASSWORDS"
DEV_SUFFIX_ENV = "WADDLES_DEV_DB_ROLE_PW_SUFFIX"
TIER_ENV = "WADDLES_DEPLOYMENT_TIER"
#: Session GUC the dev suffix is staged into so legacy SQL (031) can read it.
DEV_SUFFIX_GUC = "waddles.dev_db_role_pw_suffix"
_PASSWORD_GUC = "waddles.svc_role_pw"  # noqa: S105 -- GUC name, not a credential

#: Tiers whose databases are shared/real: the dev-password suffix is refused here.
SHARED_TIERS = frozenset({"alpha", "beta", "gamma", "production", "prod"})

ALLOWED_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
MIN_PASSWORD_LENGTH = 16
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_URL_SAFE = re.compile(r"^[A-Za-z0-9._~-]+$")
_BANNED_PASSWORD = re.compile(r"(?i)(changeme|change_me|replace_me|example|password|dev_)")

#: Every LOGIN role the repo ever shipped with a repo-known password: the 31
#: scoped-user roles (031_scoped_database_users.sql), the docker-compose-only roles in
#: config/postgres/init.sql, and its dev/kong accounts. Neutralized outside dev mode.
REPO_CREDENTIAL_ROLES = (
    "hub_admin",
    "mod_router",
    "mod_trigger_twitch",
    "mod_trigger_discord",
    "mod_trigger_slack",
    "mod_trigger_youtube",
    "mod_trigger_kick",
    "mod_action_twitch",
    "mod_action_discord",
    "mod_action_slack",
    "mod_action_youtube",
    "mod_action_lambda",
    "mod_action_gcp",
    "mod_interactive_ai",
    "mod_interactive_alias",
    "mod_interactive_shoutout",
    "mod_interactive_inventory",
    "mod_interactive_calendar",
    "mod_interactive_memories",
    "mod_interactive_ytmusic",
    "mod_interactive_spotify",
    "mod_interactive_loyalty",
    "mod_interactive_quote",
    "mod_core_labels",
    "mod_core_browser_source",
    "mod_core_identity",
    "mod_core_ai_researcher",
    "mod_core_workflow",
    "mod_core_community",
    "mod_core_reputation",
    "mod_core_analytics",
    "mod_core_security",
    "mod_core_video_proxy",
    "mod_core_engagement",
    "mod_core_rtc",
    "mod_credential_manager",
    # config/postgres/init.sql (docker-compose initdb only)
    "waddlebot_dev",
    "kong",
    "discord_action",
    "gcp_functions_action",
    "lambda_action",
    "openwhisk_action",
    "slack_action",
    "twitch_action",
    "youtube_action",
    "ai_interaction",
    "alias_interaction",
    "calendar_interaction",
    "inventory_interaction",
    "loyalty_interaction",
    "memories_interaction",
    "quote_interaction",
    "shoutout_interaction",
    "spotify_interaction",
    "youtube_music_interaction",
    "discord_trigger",
    "kick_trigger",
    "slack_trigger",
    "twitch_trigger",
    "youtube_live_trigger",
    "ai_researcher",
    "analytics",
    "browser_source",
    "community",
    "credential_manager",
    "engagement",
    "identity",
    "labels",
    "reputation",
    "security",
    "video_proxy",
    "workflow",
    "clip_interaction",
    "lfg_interaction",
    "server_status_interaction",
)

#: SECURITY DEFINER helpers (034_module_db_accounts.sql) that run caller-supplied
#: `custom_grants` SQL as the function OWNER (the DB admin): whoever may EXECUTE them
#: can escalate to superuser. Revoked from hub_admin outside dev mode.
PRIVILEGED_FUNCTIONS = (
    "provision_module_db_account",
    "deactivate_module_db_account",
    "rotate_module_db_password",
)


#: Invoker-rights TRIGGER functions that read/mint rows in tables unrelated to the table they
#: guard. Left INVOKER, every role that may write the guarded table would also need privileges
#: on those tables -- e.g. `community_members_set_user_uuid` (0045) reads hub_users /
#: hub_user_identities and mints ephemeral_pseudonyms, so the reputation service (a
#: community_members writer) would need PII-table access it has no business holding. They are
#: made SECURITY DEFINER with a pinned search_path instead. Safe because a trigger function
#: cannot be invoked directly ("can only be called as a trigger") and its body is fixed;
#: re-asserted on every reconcile so a later `CREATE OR REPLACE` cannot silently revert it.
DEFINER_TRIGGER_FUNCTIONS = ("community_members_set_user_uuid",)


class ServiceRoleError(RuntimeError):
    """Catalog / credential problem that must stop the migration (never a silent skip)."""


class CatalogDriftError(ServiceRoleError):
    """A table named in the catalog does not exist in the target database (strict mode)."""


@dataclass(slots=True, frozen=True)
class RoleSpec:
    """One catalog entry: a LOGIN role and exactly what it may touch."""

    name: str
    description: str
    workloads: tuple[str, ...]
    member_of: tuple[str, ...]
    public_dml: bool
    exclude_matrix_tables: bool
    deny_tables: frozenset[str]
    tables: Mapping[str, frozenset[str]]
    views: Mapping[str, frozenset[str]]
    allow_tables: frozenset[str]
    functions: tuple[str, ...]
    optional_tables: frozenset[str]
    columns: Mapping[str, Mapping[str, tuple[str, ...]]]

    @property
    def is_legacy(self) -> bool:
        """Legacy-profile roles also lose the `legacy_deny_tables` (credential stores)."""
        return self.public_dml and self.exclude_matrix_tables


@dataclass(slots=True, frozen=True)
class Catalog:
    """The validated service-roles catalog plus the RBAC-matrix table set it excludes."""

    roles: tuple[RoleSpec, ...]
    external_groups: frozenset[str]
    legacy_deny_tables: frozenset[str]
    read_only_tables: frozenset[str]
    matrix_tables: frozenset[str]

    @property
    def names(self) -> tuple[str, ...]:
        """Role names in catalog order."""
        return tuple(r.name for r in self.roles)

    def role(self, name: str) -> RoleSpec:
        """Return the spec for `name` or raise `ServiceRoleError`."""
        for spec in self.roles:
            if spec.name == name:
                return spec
        raise ServiceRoleError(f"unknown service role {name!r}")


@dataclass(slots=True)
class ReconcileReport:
    """What `reconcile` actually did -- counts, never secrets."""

    roles_applied: list[str] = field(default_factory=list)
    skipped_groups: dict[str, list[str]] = field(default_factory=dict)
    missing_tables: dict[str, list[str]] = field(default_factory=dict)
    granted_table_count: dict[str, int] = field(default_factory=dict)


def quote_ident(name: str) -> str:
    """Quote a validated identifier; rejects anything outside `[a-z][a-z0-9_]*`."""
    if not _IDENT.fullmatch(name):
        raise ServiceRoleError(f"unsafe SQL identifier {name!r}")
    return f'"{name}"'


def _as_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in (value or ()))


def load_catalog(
    path: str | Path = DEFAULT_CATALOG_PATH, matrix_path: str | Path = DEFAULT_MATRIX_PATH
) -> Catalog:
    """Parse and validate the catalog; raises `ServiceRoleError` on any malformed entry."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    matrix: dict[str, Any] = yaml.safe_load(Path(matrix_path).read_text(encoding="utf-8")) or {}
    external = frozenset(_as_tuple(raw.get("external_groups")))
    roles_raw: dict[str, Any] = raw.get("roles") or {}
    if not roles_raw:
        raise ServiceRoleError(f"{path}: 'roles' is empty -- refusing to run with zero roles")
    specs: list[RoleSpec] = []
    for name, body in roles_raw.items():
        if not _IDENT.fullmatch(name):
            raise ServiceRoleError(f"{path}: role name {name!r} is not a safe identifier")
        body = body or {}
        tables: dict[str, frozenset[str]] = {}
        for table, privs in (body.get("tables") or {}).items():
            if not _IDENT.fullmatch(table):
                raise ServiceRoleError(f"{name}: unsafe table name {table!r}")
            priv_set = frozenset(str(p).upper() for p in privs)
            if not priv_set or not priv_set <= ALLOWED_PRIVILEGES:
                raise ServiceRoleError(f"{name}.{table}: privileges {sorted(priv_set)} invalid")
            tables[table] = priv_set
        views: dict[str, frozenset[str]] = {}
        for view, privs in (body.get("views") or {}).items():
            if not _IDENT.fullmatch(view):
                raise ServiceRoleError(f"{name}: unsafe view name {view!r}")
            view_privs = frozenset(str(p).upper() for p in privs)
            if view_privs != {"SELECT"}:
                raise ServiceRoleError(
                    f"{name}.{view}: views are SELECT-only, got {sorted(view_privs)}"
                )
            views[view] = view_privs
        columns: dict[str, dict[str, tuple[str, ...]]] = {}
        for table, by_priv in (body.get("columns") or {}).items():
            columns[table] = {}
            for priv, cols in by_priv.items():
                if priv.upper() not in ALLOWED_PRIVILEGES - {"DELETE"}:
                    raise ServiceRoleError(f"{name}.{table}: column privilege {priv!r} invalid")
                if not all(_IDENT.fullmatch(c) for c in cols):
                    raise ServiceRoleError(f"{name}.{table}: unsafe column in {cols!r}")
                columns[table][priv.upper()] = tuple(cols)
        member_of = _as_tuple(body.get("member_of"))
        for group in member_of:
            if group not in external:
                raise ServiceRoleError(f"{name}: member_of {group!r} not in external_groups")
        allow_tables = frozenset(_as_tuple(body.get("allow_tables")))
        functions = _as_tuple(body.get("functions"))
        if not all(_IDENT.fullmatch(f) for f in functions):
            raise ServiceRoleError(f"{name}: unsafe function name in {functions!r}")
        optional = frozenset(_as_tuple(body.get("optional_tables")))
        if not optional <= set(tables):
            raise ServiceRoleError(f"{name}: optional_tables must be a subset of tables")
        public_dml = bool(body.get("public_dml", False))
        if public_dml and tables:
            raise ServiceRoleError(f"{name}: public_dml roles must not also list explicit tables")
        if set(tables) & set(views):
            raise ServiceRoleError(f"{name}: a relation cannot be both a table and a view entry")
        specs.append(
            RoleSpec(
                name=name,
                description=str(body.get("description", "")),
                workloads=_as_tuple(body.get("workloads")),
                member_of=member_of,
                public_dml=public_dml,
                exclude_matrix_tables=bool(body.get("exclude_matrix_tables", False)),
                deny_tables=frozenset(_as_tuple(body.get("deny_tables"))),
                tables=tables,
                views=views,
                allow_tables=allow_tables,
                functions=functions,
                optional_tables=optional,
                columns=columns,
            )
        )
    if len({s.name for s in specs}) != len(specs):
        raise ServiceRoleError(f"{path}: duplicate role names")
    legacy_deny = frozenset(_as_tuple(raw.get("legacy_deny_tables")))
    for spec in specs:
        if spec.allow_tables and not (spec.is_legacy and spec.allow_tables <= legacy_deny):
            raise ServiceRoleError(
                f"{spec.name}: allow_tables must be a subset of legacy_deny_tables on a legacy role"
            )
    return Catalog(
        roles=tuple(specs),
        external_groups=external,
        legacy_deny_tables=frozenset(_as_tuple(raw.get("legacy_deny_tables"))),
        read_only_tables=frozenset(_as_tuple(raw.get("read_only_tables"))),
        matrix_tables=frozenset(_as_tuple(matrix.get("tables"))),
    )


def generate_passwords(catalog: Catalog) -> dict[str, str]:
    """Random 32-char alphanumeric password per role (tests / CI throwaway DBs only)."""
    alphabet = string.ascii_letters + string.digits
    return {n: "".join(secrets.choice(alphabet) for _ in range(32)) for n in catalog.names}


def dev_suffix_from_env(env: Mapping[str, str] | None = None) -> str:
    """The dev-only password suffix, or '' -- refused on any shared deployment tier."""
    env = os.environ if env is None else env
    suffix = env.get(DEV_SUFFIX_ENV, "")
    tier = env.get(TIER_ENV, "").strip().lower()
    if suffix and tier in SHARED_TIERS:
        raise ServiceRoleError(
            f"{DEV_SUFFIX_ENV} is set while {TIER_ENV}={tier!r}: repo-derived dev passwords "
            "must never reach a shared database. Unset it."
        )
    return suffix


def _validate_password(role: str, password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ServiceRoleError(f"password for {role} shorter than {MIN_PASSWORD_LENGTH} chars")
    if not _URL_SAFE.fullmatch(password):
        raise ServiceRoleError(
            f"password for {role} must be URL-safe ([A-Za-z0-9._~-]) -- "
            "it is embedded in DATABASE_URL"
        )
    if _BANNED_PASSWORD.search(password) or password == role:
        raise ServiceRoleError(f"password for {role} looks like a placeholder/default; refusing")


def resolve_passwords(catalog: Catalog, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Role -> password for EVERY catalog role, or raise.

    Precedence: the JSON env var; in dev mode ONLY, missing roles fall back to
    `<role><suffix>`. Unknown role keys, missing roles (outside dev), and weak values
    are hard errors -- the migration must not half-provision.
    """
    env = os.environ if env is None else env
    suffix = dev_suffix_from_env(env)
    raw = env.get(PASSWORDS_ENV, "").strip()
    supplied: dict[str, str] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServiceRoleError(f"{PASSWORDS_ENV} is not valid JSON ({exc.msg})") from None
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise ServiceRoleError(f"{PASSWORDS_ENV} must be a JSON object of role -> password")
        supplied = parsed
    unknown = sorted(set(supplied) - set(catalog.names))
    if unknown:
        raise ServiceRoleError(f"{PASSWORDS_ENV} names roles not in the catalog: {unknown}")
    result: dict[str, str] = {}
    missing: list[str] = []
    for name in catalog.names:
        if name in supplied:
            _validate_password(name, supplied[name])
            result[name] = supplied[name]
        elif suffix:
            result[name] = f"{name}{suffix}"
        else:
            missing.append(name)
    if missing:
        raise ServiceRoleError(
            f"{PASSWORDS_ENV} is missing a password for {len(missing)} role(s): {missing}. "
            "Refusing to provision service roles without credentials."
        )
    return result


def _privs(privs: frozenset[str]) -> str:
    return ", ".join(sorted(privs))


def _fetch_set(conn: Connection, sql: str) -> set[str]:
    return {str(row[0]) for row in conn.execute(sa.text(sql))}


def _create_or_alter_login(conn: Connection, role: str, password: str) -> None:
    """Create `role` LOGIN (or refresh its password); attributes pinned least-privilege."""
    conn.execute(sa.text("SELECT set_config(:k, :v, false)"), {"k": _PASSWORD_GUC, "v": password})
    attrs = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    conn.execute(
        sa.text(
            f"""
DO $$
DECLARE
    pw text := current_setting('{_PASSWORD_GUC}', true);
BEGIN
    IF pw IS NULL OR pw = '' THEN
        RAISE EXCEPTION 'no password staged for service role {role}';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        EXECUTE format('CREATE ROLE %I {attrs} PASSWORD %L', '{role}', pw);
    ELSE
        EXECUTE format('ALTER ROLE %I WITH {attrs} PASSWORD %L', '{role}', pw);
    END IF;
END $$;
"""  # noqa: S608  # nosec B608 -- {role}/{attrs} are catalog-validated identifiers/literals
        )
    )
    conn.execute(sa.text("SELECT set_config(:k, '', false)"), {"k": _PASSWORD_GUC})


_TABLE_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE")
_SEQUENCE_PRIVS = frozenset({"USAGE", "SELECT"})


def _current_relation_acl(
    conn: Connection, role_names: list[str]
) -> dict[str, dict[tuple[str, str], set[str]]]:
    """role -> {(relkind-class, relname) -> privileges} for DIRECT grants in schema public.

    relkind-class is 'T' (tables/views/matviews/foreign tables, `GRANT ... ON TABLE`) or
    'S' (sequences). One query for every role keeps a steady-state reconcile at zero GRANTs.
    """
    rows = conn.execute(
        sa.text(
            "SELECT pg_get_userbyid(x.grantee), c.relname, c.relkind, x.privilege_type "
            "FROM pg_class c, aclexplode(c.relacl) x "
            "WHERE c.relnamespace = 'public'::regnamespace AND c.relacl IS NOT NULL "
            "AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') "
            "AND pg_get_userbyid(x.grantee) = ANY(:roles)"
        ),
        {"roles": role_names},
    )
    out: dict[str, dict[tuple[str, str], set[str]]] = {r: {} for r in role_names}
    for grantee, relname, relkind, priv in rows:
        kind = "S" if relkind == "S" else "T"
        out[str(grantee)].setdefault((kind, str(relname)), set()).add(str(priv))
    return out


def _current_column_acl(
    conn: Connection, role_names: list[str]
) -> dict[str, dict[tuple[str, str], set[str]]]:
    """role -> {(relname, column) -> privileges} for DIRECT column-level grants in public."""
    rows = conn.execute(
        sa.text(
            "SELECT pg_get_userbyid(x.grantee), c.relname, a.attname, x.privilege_type "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid, aclexplode(a.attacl) x "
            "WHERE c.relnamespace = 'public'::regnamespace AND a.attacl IS NOT NULL "
            "AND NOT a.attisdropped AND pg_get_userbyid(x.grantee) = ANY(:roles)"
        ),
        {"roles": role_names},
    )
    out: dict[str, dict[tuple[str, str], set[str]]] = {r: {} for r in role_names}
    for grantee, relname, attname, priv in rows:
        out[str(grantee)].setdefault((str(relname), str(attname)), set()).add(str(priv))
    return out


def _diff_relation_privileges(
    conn: Connection,
    role: str,
    current: Mapping[tuple[str, str], set[str]],
    desired: Mapping[tuple[str, str], frozenset[str]],
) -> int:
    """Make `role`'s direct relation privileges equal `desired`; returns statements issued.

    Differences are batched by privilege set (`GRANT p ON TABLE t1, t2, ...`), so a first
    provisioning of a public_dml role is a handful of statements and a steady-state
    reconcile (nothing drifted) issues none -- no catalog churn on every deploy.
    """
    quoted = quote_ident(role)
    grants: dict[tuple[str, frozenset[str]], list[str]] = {}
    revokes: dict[tuple[str, frozenset[str]], list[str]] = {}
    for key in set(current) | set(desired):
        have = frozenset(current.get(key, ()))
        want = desired.get(key, frozenset())
        kind, name = key
        if want - have:
            grants.setdefault((kind, frozenset(want - have)), []).append(name)
        if have - want:
            revokes.setdefault((kind, frozenset(have - want)), []).append(name)
    issued = 0
    for action, batches in (("REVOKE", revokes), ("GRANT", grants)):
        for (kind, privs), names in sorted(
            batches.items(), key=lambda kv: (kv[0][0], sorted(kv[0][1]))
        ):
            target = "SEQUENCE" if kind == "S" else "TABLE"
            rels = ", ".join(quote_ident(n) for n in sorted(names))
            direction = "FROM" if action == "REVOKE" else "TO"
            conn.execute(
                sa.text(f"{action} {_privs(privs)} ON {target} {rels} {direction} {quoted}")
            )
            issued += 1
    return issued


def harden_trigger_functions(conn: Connection) -> list[str]:
    """Make `DEFINER_TRIGGER_FUNCTIONS` SECURITY DEFINER with a pinned search_path (idempotent)."""
    hardened: list[str] = []
    for name in DEFINER_TRIGGER_FUNCTIONS:
        rows = conn.execute(
            sa.text(
                "SELECT p.oid::regprocedure::text FROM pg_proc p "
                "WHERE p.proname = :n AND p.pronamespace = 'public'::regnamespace "
                "AND p.prorettype = 'trigger'::regtype"
            ),
            {"n": name},
        ).fetchall()
        for (signature,) in rows:
            conn.execute(
                sa.text(
                    f"ALTER FUNCTION {signature} SECURITY DEFINER "
                    "SET search_path = pg_catalog, public"
                )
            )
            hardened.append(str(signature))
    return hardened


def reconcile(
    conn: Connection,
    catalog: Catalog,
    passwords: Mapping[str, str],
    *,
    strict: bool = False,
) -> ReconcileReport:
    """Idempotently make the database match the catalog EXACTLY for every service role.

    Runs inside the caller's transaction (alembic's, or `engine.begin()`). Each role's
    direct relation, sequence and column privileges are diffed against the catalog-derived
    desired state and only the difference is applied -- so a removed catalog entry or an
    out-of-band GRANT is repaired (not merely added to), and a no-drift reconcile writes
    nothing to the catalog tables beyond the password refresh.
    """
    report = ReconcileReport()
    harden_trigger_functions(conn)
    tables = _fetch_set(
        conn,
        "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
        "AND relkind IN ('r', 'p')",
    )
    # Views / matviews / foreign tables: `GRANT ... ON ALL TABLES` would also cover these,
    # and several are deliberate privilege boundaries (connector_pii_*, tenant_platform_
    # credentials), so a role gets one only via an explicit `views:` entry or a designed group.
    views = _fetch_set(
        conn,
        "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
        "AND relkind IN ('v', 'm', 'f')",
    )
    column_tables = sorted({t for spec in catalog.roles for t in spec.columns})
    columns_present: set[tuple[str, str]] = {
        (str(t), str(c))
        for t, c in conn.execute(
            sa.text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ANY(:t)"
            ),
            {"t": column_tables},
        )
    }
    roles = _fetch_set(conn, "SELECT rolname FROM pg_roles")
    database = str(conn.execute(sa.text("SELECT current_database()")).scalar())
    memberships = {
        (str(m), str(g))
        for m, g in conn.execute(
            sa.text(
                "SELECT m.rolname, g.rolname FROM pg_auth_members am "
                "JOIN pg_roles m ON m.oid = am.member JOIN pg_roles g ON g.oid = am.roleid"
            )
        )
    }
    sequences_of: dict[str, list[str]] = {}
    for row in conn.execute(
        sa.text(
            "SELECT t.relname, s.relname FROM pg_class s "
            "JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i') "
            "JOIN pg_class t ON t.oid = d.refobjid "
            "WHERE s.relkind = 'S' AND t.relnamespace = 'public'::regnamespace "
            "AND s.relnamespace = 'public'::regnamespace"
        )
    ):
        sequences_of.setdefault(str(row[0]), []).append(str(row[1]))
    all_sequences = _fetch_set(
        conn,
        "SELECT relname FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relkind = 'S'",
    )
    for spec in catalog.roles:
        _create_or_alter_login(conn, spec.name, passwords[spec.name])
    existing_now = _fetch_set(conn, "SELECT rolname FROM pg_roles")
    current_rel = _current_relation_acl(conn, list(catalog.names))
    current_col = _current_column_acl(conn, list(catalog.names))

    for spec in catalog.roles:
        role = quote_ident(spec.name)
        conn.execute(sa.text(f"GRANT CONNECT ON DATABASE {_db_ident(database)} TO {role}"))
        conn.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        conn.execute(sa.text(f"REVOKE CREATE ON SCHEMA public FROM {role}"))

        for group in sorted(catalog.external_groups):
            direct = (spec.name, group) in memberships
            if group in spec.member_of:
                if group in roles or group in existing_now:
                    if not direct:
                        conn.execute(sa.text(f"GRANT {quote_ident(group)} TO {role}"))
                else:
                    report.skipped_groups.setdefault(spec.name, []).append(group)
            elif direct:
                conn.execute(sa.text(f"REVOKE {quote_ident(group)} FROM {role}"))

        desired: dict[tuple[str, str], frozenset[str]] = {}
        denied: set[str] = set()
        if spec.public_dml:
            denied = set(spec.deny_tables)
            if spec.exclude_matrix_tables:
                denied |= catalog.matrix_tables
            if spec.is_legacy:
                denied |= catalog.legacy_deny_tables - spec.allow_tables
            for table in tables - denied:
                privs = frozenset(_TABLE_PRIVS)
                if table in catalog.read_only_tables:
                    privs = frozenset({"SELECT"})
                desired[("T", table)] = privs
            denied_sequences = {s for t in denied for s in sequences_of.get(t, ())}
            for seq in all_sequences - denied_sequences:
                desired[("S", seq)] = _SEQUENCE_PRIVS
        missing: list[str] = []
        for table, privs in sorted(spec.tables.items()):
            if table not in tables:
                if table not in spec.optional_tables:
                    missing.append(table)
                continue
            desired[("T", table)] = privs
            if "INSERT" in privs:
                for seq in sequences_of.get(table, ()):
                    desired[("S", seq)] = _SEQUENCE_PRIVS
        for view, privs in sorted(spec.views.items()):
            if view not in views:
                missing.append(view)
                continue
            desired[("T", view)] = privs
        _diff_relation_privileges(conn, spec.name, current_rel[spec.name], desired)

        for function in spec.functions:
            # Function / role names are catalog-validated identifiers (load_catalog), never
            # user input. Only grants when EXECUTE is missing, so a steady state writes nothing.
            conn.execute(
                sa.text(
                    "DO $$ DECLARE f record; BEGIN "  # noqa: S608  # nosec B608
                    "FOR f IN SELECT p.oid::regprocedure AS sig FROM pg_proc p "
                    f"WHERE p.proname = '{function}' AND p.pronamespace = 'public'::regnamespace "
                    f"AND NOT has_function_privilege('{spec.name}', p.oid, 'EXECUTE') LOOP "
                    f"EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO {role}', f.sig); "
                    "END LOOP; END $$"
                )
            )

        desired_cols: dict[tuple[str, str], set[str]] = {}
        for table, by_priv in sorted(spec.columns.items()):
            if table not in tables:
                missing.append(table)
                continue
            for priv, cols in by_priv.items():
                for col in cols:
                    if (table, col) not in columns_present:
                        missing.append(f"{table}.{col}")
                        continue
                    desired_cols.setdefault((table, col), set()).add(priv)
        have_cols = current_col[spec.name]
        for key in sorted(set(have_cols) | set(desired_cols)):
            have = frozenset(have_cols.get(key, ()))
            want = frozenset(desired_cols.get(key, ()))
            table, col = key
            for priv in sorted(want - have):
                conn.execute(
                    sa.text(
                        f"GRANT {priv} ({quote_ident(col)}) ON TABLE {quote_ident(table)} TO {role}"
                    )
                )
            for priv in sorted(have - want):
                conn.execute(
                    sa.text(
                        f"REVOKE {priv} ({quote_ident(col)}) "
                        f"ON TABLE {quote_ident(table)} FROM {role}"
                    )
                )
        if missing:
            report.missing_tables[spec.name] = sorted(set(missing))
        report.granted_table_count[spec.name] = sum(1 for k in desired if k[0] == "T")
        report.roles_applied.append(spec.name)
    if strict and report.missing_tables:
        raise CatalogDriftError(
            "service-roles catalog names tables that do not exist: "
            + "; ".join(f"{r}: {t}" for r, t in sorted(report.missing_tables.items()))
        )
    return report


def _db_ident(database: str) -> str:
    """Quote a database name (not constrained to the lowercase-identifier regex)."""
    if not database or '"' in database or "\x00" in database:
        raise ServiceRoleError(f"unsafe database name {database!r}")
    return f'"{database}"'


def neutralize_repo_credential_roles(conn: Connection) -> list[str]:
    """Strip LOGIN + password from every role that ever shipped a repo-known password.

    Idempotent. Roles are kept (their GRANTs and RLS policies are still the designed
    group privileges the legacy pods inherit) -- they just can no longer authenticate.
    Also revokes the roles' default privileges and the owner-escalation functions.
    Returns the roles that existed and were neutralized (counts feed the migration log).
    """
    existing = _fetch_set(conn, "SELECT rolname FROM pg_roles")
    neutralized: list[str] = []
    for name in REPO_CREDENTIAL_ROLES:
        if name not in existing:
            continue
        conn.execute(sa.text(f"ALTER ROLE {quote_ident(name)} WITH NOLOGIN PASSWORD NULL"))
        neutralized.append(name)
    if "hub_admin" in existing:
        for name in PRIVILEGED_FUNCTIONS:
            for (signature,) in conn.execute(
                sa.text(
                    "SELECT p.oid::regprocedure::text FROM pg_proc p "
                    "WHERE p.proname = :n AND p.pronamespace = 'public'::regnamespace"
                ),
                {"n": name},
            ).fetchall():
                conn.execute(sa.text(f"REVOKE ALL ON FUNCTION {signature} FROM hub_admin"))
        conn.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM hub_admin"))
    _revoke_default_privileges(conn, set(neutralized))
    return neutralized


_DEFACL_OBJECT = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS"}


def _revoke_default_privileges(conn: Connection, grantees: set[str]) -> None:
    """Drop `ALTER DEFAULT PRIVILEGES` entries that auto-grant new objects to `grantees`."""
    rows = conn.execute(
        sa.text(
            "SELECT d.defaclrole::regrole::text, "
            "COALESCE(d.defaclnamespace::regnamespace::text, ''), d.defaclobjtype, "
            "pg_get_userbyid(a.grantee) "
            "FROM pg_default_acl d, aclexplode(d.defaclacl) a WHERE a.grantee <> 0"
        )
    ).fetchall()
    for owner, schema, objtype, grantee in rows:
        kind = _DEFACL_OBJECT.get(str(objtype))
        if grantee not in grantees or kind is None:
            continue
        scope = f"IN SCHEMA {quote_ident(schema)} " if schema and schema != "-" else ""
        conn.execute(
            sa.text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {_pg_ident(owner)} {scope}"
                f"REVOKE ALL ON {kind} FROM {quote_ident(grantee)}"
            )
        )


def _pg_ident(name: str) -> str:
    """Quote a role name read back from the catalog (already a valid existing identifier)."""
    return '"' + name.replace('"', '""') + '"'


def _engine_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise ServiceRoleError("DATABASE_URL is not set")
    return url.replace("postgresql://", "postgresql+psycopg2://", 1)


def main(argv: list[str] | None = None) -> int:
    """CLI: `reconcile [--strict]`, `validate-catalog`."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("reconcile", help="re-assert the catalog on DATABASE_URL")
    rec.add_argument("--strict", action="store_true", help="fail if a catalog table is missing")
    sub.add_parser("validate-catalog", help="load + validate the catalog, print role count")
    args = parser.parse_args(argv)
    catalog = load_catalog()
    if args.cmd == "validate-catalog":
        print(f"catalog ok: {len(catalog.roles)} service roles")
        return 0
    passwords = resolve_passwords(catalog)
    engine = sa.create_engine(_engine_url())
    with engine.begin() as conn:
        report = reconcile(conn, catalog, passwords, strict=args.strict)
    for role, tables in sorted(report.missing_tables.items()):
        print(f"WARN: {role}: catalog tables absent from database: {tables}", file=sys.stderr)
    print(
        f"Reconciled {len(report.roles_applied)} service roles "
        f"({sum(report.granted_table_count.values())} table grants)."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ServiceRoleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
