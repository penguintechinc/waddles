"""Every-run reconcile of the bundle storage v2 LOGIN roles (alembic 0030).

`waddles_bundle_migrator` (DDL owner of `app_core`/`app_community`; hub-api + the core-bundle
seeder) and `waddles_bundle_runtime` (DML only; svc-process-rust / svc-action-rust).

**Why every run.** `0030_bundle_app_schemas.py` sets a password only the one time it runs; a
database already past 0030 (alpha: role created with NO password because the chart never
supplied one) would otherwise never receive the value the provision-secrets hook later
generates into `waddlebot-bundle-migrator` / `waddlebot-bundle-db`. `migrations/run-alembic.sh`
calls this after `alembic upgrade head`, with the values the db-migrate hook Secret derives from
those same Secrets, so Secret and role always match.

**Idempotent.** A role whose stored SCRAM-SHA-256 verifier already matches the password is left
untouched (no `ALTER ROLE`, so a re-run is a true no-op); a different value (the Secret changed
by design, or the role had none) is applied. A missing role is created LOGIN with no elevated
attribute; least-privilege attributes are re-asserted every run. Schema ownership / grants are
re-asserted only where 0030 already created the schemas.

**Password handling.** Values come from the environment only (never an argv, never logged by
this module). The plaintext NEVER reaches the server: the SCRAM-SHA-256 verifier is computed
client-side (RFC 5802/7677, random 16-byte salt, 4096 iterations -- Postgres' own default) and
only that verifier is sent in `CREATE/ALTER ROLE ... PASSWORD '<verifier>'`, so a server
statement/error log can at most capture a salted verifier. Defence in depth: the transaction
also runs `SET LOCAL log_statement = 'none'` / `log_min_duration_statement = -1` where the
connecting role may set them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection

LOG = logging.getLogger("waddles.bundle_storage_roles")

MIGRATOR_ROLE = "waddles_bundle_migrator"
RUNTIME_ROLE = "waddles_bundle_runtime"
MIGRATOR_PASSWORD_ENV = "BUNDLE_MIGRATOR_ROLE_PASSWORD"  # noqa: S105 -- env var NAME, not a secret
RUNTIME_PASSWORD_ENV = "BUNDLE_RUNTIME_ROLE_PASSWORD"  # noqa: S105 -- env var NAME, not a secret
APP_SCHEMAS = ("app_core", "app_community")
#: Same floor provision-secrets enforces (`min=16`); a shorter value is never a chart value.
MIN_PASSWORD_LEN = 16
#: Never elevated -- re-asserted on every run so out-of-band drift is repaired.
_LEAST_PRIVILEGE = (
    "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT"
)


@dataclass(slots=True, frozen=True)
class RoleResult:
    """Outcome of reconciling one role (never carries the password)."""

    role: str
    created: bool
    password_changed: bool


#: Postgres' default `scram_iterations`.
SCRAM_ITERATIONS = 4096
#: Shape check before a verifier is embedded as a SQL literal (base64 alphabet + separators only,
#: so it can never break out of the quotes).
_VERIFIER_RE = re.compile(
    r"^SCRAM-SHA-256\$[0-9]+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$"
)


def _scram_keys(password: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    """(StoredKey, ServerKey) per RFC 5802/7677 for an ASCII `password` (SASLprep = identity)."""
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored = hashlib.sha256(client_key).digest()
    server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return stored, server


def scram_verifier(password: str, *, salt: bytes | None = None) -> str:
    """Client-side SCRAM-SHA-256 verifier in pg_authid's format -- the only form sent to the server.

    `SCRAM-SHA-256$<iter>:<b64 salt>$<b64 StoredKey>:<b64 ServerKey>`. Raises ValueError for a
    non-ASCII password (SASLprep would be needed; chart values are always ASCII/base64url).
    """
    if not password.isascii():
        raise ValueError("non-ASCII role password is not supported")
    salt = salt if salt is not None else secrets.token_bytes(16)
    stored, server = _scram_keys(password, salt, SCRAM_ITERATIONS)
    b64 = base64.b64encode
    return (
        f"SCRAM-SHA-256${SCRAM_ITERATIONS}:{b64(salt).decode()}"
        f"${b64(stored).decode()}:{b64(server).decode()}"
    )


def _suppress_statement_logging(conn: Connection) -> None:
    """Defence in depth: no statement logging for this transaction where the role may set it.

    Both GUCs are superuser-settable only; a non-superuser connection keeps the server's setting
    (harmless -- only a verifier is ever sent).
    """
    for stmt in (
        "SET LOCAL log_statement = 'none'",
        "SET LOCAL log_min_duration_statement = -1",
    ):
        try:
            with conn.begin_nested():
                conn.execute(sa.text(stmt))
        except sa.exc.DBAPIError:
            LOG.info(
                "bundle_storage_roles: cannot %s (not superuser); continuing", stmt
            )


def scram_matches(verifier: str, password: str) -> bool:
    """True when `verifier` (pg_authid.rolpassword, SCRAM-SHA-256) was derived from `password`.

    RFC 5802/7677: StoredKey = H(HMAC(SaltedPassword, "Client Key")), ServerKey =
    HMAC(SaltedPassword, "Server Key"). SASLprep is the identity for the ASCII passwords the chart
    generates; any non-ASCII value simply compares unequal (-> re-applied, still correct).
    """
    try:
        method, rest = verifier.split("$", 1)
        iter_salt, keys = rest.split("$", 1)
        iterations_s, salt_b64 = iter_salt.split(":", 1)
        stored_b64, server_b64 = keys.split(":", 1)
        if method != "SCRAM-SHA-256" or not password.isascii():
            return False
        stored, server = _scram_keys(
            password, base64.b64decode(salt_b64), int(iterations_s)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(
        stored, base64.b64decode(stored_b64)
    ) and hmac.compare_digest(server, base64.b64decode(server_b64))


def password_matches(verifier: str | None, password: str) -> bool:
    """True when the stored SCRAM-SHA-256 verifier already encodes `password`.

    A legacy md5 (or any other) verifier is deliberately treated as a mismatch: the role is
    re-applied with a SCRAM verifier, which also upgrades it off md5.
    """
    if not verifier or not verifier.startswith("SCRAM-SHA-256$"):
        return False
    return scram_matches(verifier, password)


def _stored_verifier(conn: Connection, role: str) -> tuple[bool, str | None, bool]:
    """(exists, verifier, readable). `pg_authid` is superuser-only; unreadable -> always re-apply."""
    exists = bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
    )
    if not exists:
        return False, None, True
    try:
        with conn.begin_nested():
            verifier = conn.execute(
                sa.text("SELECT rolpassword FROM pg_authid WHERE rolname = :r"),
                {"r": role},
            ).scalar()
    except sa.exc.DBAPIError:
        return True, None, False
    return True, verifier, True


def _validate(env_name: str, password: str) -> None:
    """Fail loud on an unusable value -- never set/leave a weak or empty role password."""
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"{env_name} is shorter than {MIN_PASSWORD_LEN} characters")


def reconcile_role(
    conn: Connection, role: str, password: str, env_name: str
) -> RoleResult:
    """Create `role` if missing, set its password only if it differs, re-assert least privilege."""
    _validate(env_name, password)
    exists, verifier, readable = _stored_verifier(conn, role)
    if exists and readable and password_matches(verifier, password):
        conn.execute(sa.text(f"ALTER ROLE {role} WITH {_LEAST_PRIVILEGE}"))
        return RoleResult(role, created=False, password_changed=False)
    verifier_out = scram_verifier(password)
    if not _VERIFIER_RE.fullmatch(
        verifier_out
    ):  # pragma: no cover -- defensive invariant
        raise ValueError("computed SCRAM verifier has an unexpected shape")
    verb = "ALTER" if exists else "CREATE"
    # Only the salted verifier is ever in the statement text (shape-checked above, so the quoted
    # literal cannot be broken out of); the plaintext never leaves this process.
    # exec_driver_sql, not sa.text: the verifier's ':' must never be parsed as a bind parameter.
    conn.exec_driver_sql(
        f"{verb} ROLE {role} WITH {_LEAST_PRIVILEGE} PASSWORD '{verifier_out}'"
    )
    return RoleResult(role, created=not exists, password_changed=True)


def _reassert_schema_grants(conn: Connection, *, migrator: bool, runtime: bool) -> None:
    """Re-assert 0030's ownership/grants where its schemas exist (repairs a recreated role)."""
    for schema in APP_SCHEMAS:
        if (
            conn.execute(sa.text("SELECT to_regnamespace(:s)"), {"s": schema}).scalar()
            is None
        ):
            continue
        if migrator:
            owner = conn.execute(
                sa.text(
                    "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = :s"
                ),
                {"s": schema},
            ).scalar()
            if owner != MIGRATOR_ROLE:
                conn.execute(sa.text(f"ALTER SCHEMA {schema} OWNER TO {MIGRATOR_ROLE}"))
        if runtime:
            conn.execute(sa.text(f"GRANT USAGE ON SCHEMA {schema} TO {RUNTIME_ROLE}"))
        if migrator and runtime:
            conn.execute(
                sa.text(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {MIGRATOR_ROLE} IN SCHEMA {schema} "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RUNTIME_ROLE}"
                )
            )
    if runtime:
        conn.execute(
            sa.text(
                f"ALTER ROLE {RUNTIME_ROLE} SET search_path = app_core, app_community, pg_catalog"
            )
        )


def reconcile(conn: Connection, env: Mapping[str, str]) -> list[RoleResult]:
    """Reconcile every role whose password env var is set; an unset one is skipped (feature off)."""
    _suppress_statement_logging(conn)
    results: list[RoleResult] = []
    plan = (
        (MIGRATOR_ROLE, MIGRATOR_PASSWORD_ENV),
        (RUNTIME_ROLE, RUNTIME_PASSWORD_ENV),
    )
    for role, env_name in plan:
        password = env.get(env_name, "")
        if password:
            results.append(reconcile_role(conn, role, password, env_name))
    done = {r.role for r in results}
    _reassert_schema_grants(
        conn, migrator=MIGRATOR_ROLE in done, runtime=RUNTIME_ROLE in done
    )
    return results


def main() -> int:
    """CLI entry (run-alembic.sh): reconcile against DATABASE_URL; prints names/booleans only."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        return 2
    url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            results = reconcile(conn, os.environ)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    if not results:
        print(
            "Bundle storage roles: no BUNDLE_*_PASSWORD supplied -- feature disabled, skipped."
        )
    # Only fixed role names + yes/no flags are printed (never a value derived from a password).
    for r in results:
        name = MIGRATOR_ROLE if r.role == MIGRATOR_ROLE else RUNTIME_ROLE
        created = "yes" if r.created else "no"
        changed = "yes" if r.password_changed else "no"
        print(f"Reconciled {name}: created={created} password_changed={changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
