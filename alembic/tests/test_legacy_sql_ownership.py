"""Guard: every post-baseline legacy SQL migration is bridged into Alembic (no docker needed).

# regression: config/postgres SQL migrations 097-102 never reached existing databases

`config/postgres/migrations/NNN_*.sql` is replayed only by `0001_baseline` on a FRESH DB;
an existing DB (alpha/beta/prod) never sees a file added after it was baselined. The
bridge is an Alembic revision that re-applies the DDL idempotently and records the file in
`schema_migrations` -- declared by a module-level `LEGACY_VERSIONS` tuple (0014 covers
084-096, 0060 covers 097-102, 0061 covers 103). This test fails when a new numbered SQL file lands with no
such port, so the gap cannot silently reopen. See docs/DATABASE.md "SQL vs Alembic ownership".
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SQL_DIR = _REPO_ROOT / "config" / "postgres" / "migrations"
_VERSIONS_DIR = _REPO_ROOT / "alembic" / "versions"

#: First numbered SQL file that needs an Alembic bridge (084 is 0014's first).
_FIRST_BRIDGED = 84

#: SQL files known to be un-ported, each with the PR that owns the follow-up. An entry
#: must be REMOVED once its port lands (asserted below), so this list can only shrink.
PENDING_PORT: dict[str, str] = {}


def _declared_ported_versions() -> dict[str, str]:
    """Map each `LEGACY_VERSIONS` entry to the Alembic file declaring it (AST, no imports)."""
    ported: dict[str, str] = {}
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if any(isinstance(t, ast.Name) and t.id == "LEGACY_VERSIONS" for t in targets):
                for version in ast.literal_eval(value):
                    ported[version] = path.name
    return ported


def test_every_post_baseline_sql_file_is_ported_or_explicitly_pending() -> None:
    """Denominator = SQL files numbered >= 084; each is in a LEGACY_VERSIONS or PENDING_PORT."""
    sql_files = sorted(
        p.stem for p in _SQL_DIR.glob("*.sql") if int(p.name[:3]) >= _FIRST_BRIDGED
    )
    assert len(sql_files) >= 19, f"expected >=19 SQL files numbered >= 084, saw {len(sql_files)}"
    ported = _declared_ported_versions()
    unbridged = [v for v in sql_files if v not in ported and v not in PENDING_PORT]
    assert unbridged == [], (
        f"legacy SQL file(s) {unbridged} have no Alembic bridge: an existing DB would never "
        "get them. Add a revision that re-applies the DDL idempotently and ledgers the version "
        "in a module-level LEGACY_VERSIONS tuple (see 0060_port_legacy_sql_097_102.py)."
    )


def test_ported_versions_are_real_files_and_pending_list_stays_honest() -> None:
    """A ported version names a SQL file on disk; a pending entry is not already ported."""
    ported = _declared_ported_versions()
    assert len(ported) >= 19
    missing = [v for v in ported if not (_SQL_DIR / f"{v}.sql").is_file()]
    assert missing == [], f"LEGACY_VERSIONS names SQL file(s) that do not exist: {missing}"
    stale = sorted(set(PENDING_PORT) & set(ported))
    assert stale == [], f"already ported -- remove from PENDING_PORT: {stale}"
