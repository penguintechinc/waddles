"""Static guard: bootstrap.py must never create or alter schema.

fix/chart-fresh-install-hooks (alpha 2026-10-01) history: this module used to
be a real-Postgres drift check (`xfail(strict=True)`) proving
`flask_core.models.db.metadata.create_all()` did NOT reproduce the full
Alembic migration chain's schema on a fresh database -- it only covers
~14-21 of the 100+ tables the chain creates via raw SQL with no
corresponding model (`commands`/`platform_integrations` among them). That gap
meant a fresh install's hub-api-driven `create_all()`+stamp left those tables
missing, and the very next `helm upgrade` then failed loudly against
`run-alembic.sh`'s own required-table check.

USER DECISION (2026-10-01): the schema always comes from the `db-migrate`
Helm hook Job instead (now post-install too, not just pre-upgrade -- see
k8s/helm/waddlebot/templates/migrations-job.yaml). bootstrap.py no longer has
a create_all()+stamp path at all -- it only observes `alembic_version` and
polls. The drift this file used to document can no longer occur because the
code path that produced it is gone; this file now guards that it STAYS gone,
by statically asserting bootstrap.py's source never calls `create_all` (or
any other schema-mutating Alembic/DDL entry point) again. A regression here
would silently reintroduce the exact bug this PR fixed.
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_PATH = REPO_ROOT / "hub_api" / "bootstrap.py"

#: Call/attribute names that would indicate bootstrap.py is creating or
#: mutating schema again, not merely observing it.
FORBIDDEN_CALL_NAMES = {
    "create_all",  # SQLAlchemy MetaData.create_all() -- the original bug
    "upgrade",  # alembic.command.upgrade() -- would actually run migrations
    "stamp",  # alembic.command.stamp() -- schema bookkeeping mutation
}


def _code_only_source(source: str) -> str:
    """`source` with every STRING and COMMENT token blanked out.

    Lets the belt-and-suspenders textual check below search real code only --
    this module's own docstrings (including bootstrap.py's, which names
    `create_all` while documenting the bug this test guards against) would
    otherwise be indistinguishable from an actual reintroduced call.
    """
    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.STRING, tokenize.COMMENT):
            continue
        out.append(tok.string)
    return " ".join(out)


def _called_names(tree: ast.AST) -> set[str]:
    """Every bare/attribute call name used anywhere in the module (e.g. `foo()` or `x.foo()`)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_bootstrap_source_exists() -> None:
    """Sanity check against the real file -- a scanner pointed at the wrong path reports clean."""
    assert BOOTSTRAP_PATH.is_file(), f"expected {BOOTSTRAP_PATH} to exist"


def test_bootstrap_never_creates_or_alters_schema() -> None:
    """hub_api/bootstrap.py must never call create_all()/alembic upgrade()/stamp().

    Schema creation and migration are the db-migrate Helm hook Job's job
    alone (post-install,pre-upgrade) -- bootstrap.py only reads
    `alembic_version` via plain `SELECT`/`EXISTS` SQL (see
    `_current_db_heads`) and computes the static revision-graph head via
    `alembic.script.ScriptDirectory` (see `_script_heads`), never
    `alembic.command.*`.
    """
    source = BOOTSTRAP_PATH.read_text()
    tree = ast.parse(source, filename=str(BOOTSTRAP_PATH))

    found = _called_names(tree) & FORBIDDEN_CALL_NAMES
    assert not found, (
        f"bootstrap.py calls schema-mutating function(s) {sorted(found)} -- this "
        "reintroduces the fresh-install drift bug this test guards against; schema "
        "changes belong in the db-migrate Helm hook Job only"
    )

    # Belt-and-suspenders textual check (code tokens only -- comments/
    # docstrings excluded, see _code_only_source): catches a mutating call
    # added via a dynamic/aliased import an AST name-walk wouldn't flag
    # (e.g. `getattr(metadata, "create_all")(...)`).
    code_only = _code_only_source(source)
    for forbidden in ("create_all", "alembic.command"):
        assert forbidden not in code_only, (
            f"bootstrap.py code contains {forbidden!r} -- schema changes belong in "
            "the db-migrate Helm hook Job only"
        )


if __name__ == "__main__":
    sys.exit(0)
