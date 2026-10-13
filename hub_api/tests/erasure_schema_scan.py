"""Static schema scan: which tables/columns does the migration chain define?

Backs `test_erasure_registry_coverage.py`. The schema is owned by two sources that are replayed
in order on a fresh database -- the legacy `config/postgres/{init.sql,migrations/*.sql}` and the
Alembic revisions (`alembic/versions/*.py`, which also load `scripts/db/*.sql` DDL). This module
reads them as TEXT (no database, no Docker) and reconstructs the final `table -> {columns}` map,
so a CI run with nothing but the checkout can tell that a new user-keyed table appeared.

It is deliberately a small tolerant parser, not a SQL engine: it understands `CREATE TABLE`,
`ALTER TABLE ... ADD/DROP/RENAME COLUMN` and `RENAME TO`, `DROP TABLE` (legacy SQL only), and
Alembic's `op.create_table` / `op.add_column`. It errs toward OVER-reporting (a column that a
later `downgrade()` or a failed statement would have removed is still listed), because a phantom
column only costs the registry one extra classification while a missed one hides a table.
Cross-checked column-for-column against a database replayed from the same files; the only
differences were phantom (superset) columns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Replay order: legacy bootstrap + numbered SQL first (Alembic 0001 replays exactly these), then
#: the DDL snippets Alembic loads from `scripts/db/`, then the Alembic revisions themselves.
_LEGACY_SQL = (REPO_ROOT / "config" / "postgres" / "init.sql",)
_LEGACY_SQL_DIR = REPO_ROOT / "config" / "postgres" / "migrations"
_SCRIPTS_SQL_DIR = REPO_ROOT / "scripts" / "db"
_ALEMBIC_DIR = REPO_ROOT / "alembic" / "versions"

_IDENT = r'"?([A-Za-z_][A-Za-z0-9_]*)"?'
_TABLE_REF = r'(?:public\.|"public"\.)?' + _IDENT

_CREATE_TABLE = re.compile(
    r"CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _TABLE_REF + r"\s*\(", re.I
)
#: An `ALTER TABLE` clause ends at `;`, at the closing of a Python string literal (triple quote, or
#: a `"` followed by `,`/`)`), or at EOF -- never at the next statement, which would leak its
#: `ADD COLUMN`s onto this table.
_ALTER_TABLE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    + _TABLE_REF
    + r"\s+(.*?)"
    + r'(?:;|"""|\'\'\'|"\s*[,)]|\Z)',
    re.I | re.S,
)
_ADD_COLUMN = re.compile(
    r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?" + _IDENT + r"\s+[A-Za-z]", re.I
)
_DROP_COLUMN = re.compile(r"\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?" + _IDENT, re.I)
_RENAME_COLUMN = re.compile(r"\bRENAME\s+COLUMN\s+" + _IDENT + r"\s+TO\s+" + _IDENT, re.I)
_RENAME_TABLE = re.compile(r"RENAME\s+TO\s+" + _IDENT, re.I)
_DROP_TABLE = re.compile(
    r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
    r'((?:public\.)?"?[A-Za-z_]\w*"?(?:\s*,\s*(?:public\.)?"?[A-Za-z_]\w*"?)*)',
    re.I,
)
_CREATE_VIEW = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+" + _TABLE_REF, re.I)

_REFERENCES_HUB_USERS = re.compile(r"REFERENCES\s+(?:public\.)?\"?hub_users\"?\b", re.I)
_FK_COLUMNS = re.compile(r"FOREIGN\s+KEY\s*\(([^)]*)\)", re.I)

_OP_CREATE_TABLE = re.compile(r"op\.create_table\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']")
_OP_ADD_COLUMN = re.compile(
    r"op\.add_column\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
    r"\s*,\s*sa\.Column\(\s*[\"']([A-Za-z_]\w*)[\"']"
)
_SA_COLUMN = re.compile(r"sa\.Column\(\s*[\"']([A-Za-z_]\w*)[\"']")

_NOT_A_COLUMN = frozenset(
    {"if", "constraint", "primary", "unique", "foreign", "check", "exclude", "like"}
)


@dataclass(slots=True)
class SchemaScan:
    """The reconstructed schema: `tables[name] = ordered set of column names`."""

    tables: dict[str, dict[str, None]] = field(default_factory=dict)
    views: set[str] = field(default_factory=set)
    #: `(table, column)` pairs declared `REFERENCES hub_users` (inline or table-level FOREIGN KEY).
    hub_user_fks: set[tuple[str, str]] = field(default_factory=set)
    files_scanned: int = 0

    def columns(self, table: str) -> set[str]:
        """The column names of `table` (empty if unknown)."""
        return set(self.tables.get(table, ()))

    def add(self, table: str, column: str) -> None:
        """Record `column` on `table` (creating the table entry if needed)."""
        self.tables.setdefault(table.lower(), {})[column.lower()] = None


def _strip_sql_comments(text: str) -> str:
    """Drop `-- ...` line comments and `/* ... */` blocks (column lists carry both)."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"--[^\n]*", " ", text)


def _balanced(text: str, open_index: int) -> str:
    """The text inside the parenthesis opened at `open_index` (to EOF if never closed)."""
    depth = 0
    quote: str | None = None
    for i in range(open_index, len(text)):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i]
    return text[open_index + 1 :]


def _split_top_level(body: str) -> list[str]:
    """Split a `CREATE TABLE` body on commas that are not nested in parentheses/quotes."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for ch in body:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            current.append(ch)
    parts.append("".join(current))
    return parts


def _note_hub_user_fk(scan: SchemaScan, table: str, element: str) -> None:
    """Record `(table, column)` if `element` declares an FK to `hub_users` (column/table level)."""
    if not _REFERENCES_HUB_USERS.search(element):
        return
    fk = _FK_COLUMNS.search(element)
    if fk:
        for column in fk.group(1).split(","):
            scan.hub_user_fks.add((table, column.strip().strip('"').lower()))
        return
    column_match = _ADD_COLUMN.search(element)
    name = (
        column_match.group(1)
        if column_match
        else (element.strip().split(None, 1) or [""])[0].strip('"')
    )
    scan.hub_user_fks.add((table, name.lower()))


def _apply_create(scan: SchemaScan, text: str, match: re.Match[str]) -> None:
    """Record every column of one `CREATE TABLE` statement."""
    table = match.group(1).lower()
    body = _balanced(text, match.end() - 1)
    scan.tables.setdefault(table, {})
    for element in _split_top_level(body):
        tokens = element.strip().split(None, 1)
        if not tokens:
            continue
        name = tokens[0].strip('"')
        _note_hub_user_fk(scan, table, element)
        if name.lower() in _NOT_A_COLUMN or not re.fullmatch(r"[A-Za-z_]\w*", name):
            continue
        scan.add(table, name)


def _apply_alter(scan: SchemaScan, match: re.Match[str]) -> None:
    """Apply the ADD/DROP/RENAME clauses of one `ALTER TABLE` statement."""
    table = match.group(1).lower()
    clause = match.group(2)
    renamed = _RENAME_TABLE.match(clause.strip())
    if renamed and table in scan.tables:
        scan.tables[renamed.group(1).lower()] = scan.tables.pop(table)
        return
    for segment in _split_top_level(clause):
        _note_hub_user_fk(scan, table, segment)
    for col in _ADD_COLUMN.finditer(clause):
        if col.group(1).lower() not in _NOT_A_COLUMN:
            scan.add(table, col.group(1))
    for col in _DROP_COLUMN.finditer(clause):
        scan.tables.get(table, {}).pop(col.group(1).lower(), None)
    for ren in _RENAME_COLUMN.finditer(clause):
        cols = scan.tables.get(table)
        if cols is not None:
            cols.pop(ren.group(1).lower(), None)
            scan.add(table, ren.group(2))


def _scan_sql_text(scan: SchemaScan, raw: str, *, honour_drops: bool) -> None:
    """Apply every DDL statement found in `raw` (SQL, or SQL embedded in a Python string)."""
    text = _strip_sql_comments(raw)
    for view in _CREATE_VIEW.finditer(text):
        scan.views.add(view.group(1).lower())
    events: list[tuple[int, str, re.Match[str]]] = []
    events += [(m.start(), "create", m) for m in _CREATE_TABLE.finditer(text)]
    events += [(m.start(), "alter", m) for m in _ALTER_TABLE.finditer(text)]
    if honour_drops:
        events += [(m.start(), "drop", m) for m in _DROP_TABLE.finditer(text)]
    for _pos, kind, match in sorted(events, key=lambda e: e[0]):
        if kind == "create":
            _apply_create(scan, text, match)
        elif kind == "alter":
            _apply_alter(scan, match)
        else:
            for name in re.findall(r'"?([A-Za-z_]\w*)"?', match.group(1)):
                scan.tables.pop(name.lower(), None)


def _scan_alembic_text(scan: SchemaScan, text: str) -> None:
    """Apply `op.create_table` / `op.add_column` calls in an Alembic revision file.

    `op.drop_*` are deliberately NOT replayed: in this repo they only live in `downgrade()` or in
    module-level teardown lists, and treating a rollback as the schema would hide a real column.
    """
    for match in _OP_CREATE_TABLE.finditer(text):
        table = match.group(1).lower()
        body = _balanced(text, text.index("(", match.start()))
        scan.tables.setdefault(table, {})
        for col in _SA_COLUMN.finditer(body):
            scan.add(table, col.group(1))
    for match in _OP_ADD_COLUMN.finditer(text):
        scan.add(match.group(1), match.group(2))


def _without_downgrade(text: str) -> str:
    """Remove each top-level `def downgrade(...)` body -- its statements undo, not define."""
    return re.sub(
        r"^def downgrade\b.*?(?=^(?:def |class |[A-Za-z_]\w* *[=:]|@)|\Z)",
        "",
        text,
        flags=re.S | re.M,
    )


def _inline_name_constants(text: str) -> str:
    """Resolve `{_TABLE}` f-string placeholders from simple `_TABLE = "name"` module constants."""
    constants = dict(
        re.findall(r"^(\w+)\s*(?::[^=\n]+)?=\s*[\"']([A-Za-z_]\w*)[\"']\s*$", text, re.M)
    )
    for name, value in constants.items():
        text = text.replace("{" + name + "}", value)
    return text


def scan_schema() -> SchemaScan:
    """Reconstruct the final schema from the legacy SQL, `scripts/db/*.sql` and Alembic files."""
    scan = SchemaScan()
    sql_files: list[Path] = [p for p in _LEGACY_SQL if p.exists()]
    sql_files += sorted(_LEGACY_SQL_DIR.glob("*.sql"))
    sql_files += sorted(_SCRIPTS_SQL_DIR.glob("*.sql"))
    for path in sql_files:
        _scan_sql_text(scan, path.read_text(encoding="utf-8"), honour_drops=True)
        scan.files_scanned += 1
    for path in sorted(_ALEMBIC_DIR.glob("*.py")):
        text = _inline_name_constants(_without_downgrade(path.read_text(encoding="utf-8")))
        _scan_sql_text(scan, text, honour_drops=False)
        _scan_alembic_text(scan, text)
        scan.files_scanned += 1
    for view in scan.views:
        scan.tables.pop(view, None)
    return scan
