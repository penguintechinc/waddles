#!/usr/bin/env python3
"""Structural guard: every bundle app_id must be ``<github-org>.<repo>.<bundle-name>``.

Parses app_id-bearing fields only (never free text, never PostHog flag keys such as
``waddles.community.polls`` or tier_catalog license keys):

* SQL / alembic ``INSERT INTO app_catalog|app_tenant_availability`` statements (dotted string literals),
* alembic ``APP_ID = "..."`` and ``*_APP_IDS = (...)`` seed tuples,
* ``bundles/core-bundles.yaml`` and bundle manifests (``app_id:``),
* Rust ``const *_APP_ID: &str = "..."``,
* ``tool.call:<app_id>:<tool>`` grants in manifests / catalogs / seeds,
* the ``feature`` column of seeded ``INSERT INTO app_catalog`` rows: descriptive-only since PR #1481, so a
  seed must use the new-shape namespace ``<org>.<repo>`` (the manifest default) -- never an old
  ``waddles.<category>.<name>`` id (hard cutover).

Each id must match ``^(penguintechinc\\.waddles|psychoboy\\.penguintwitchbot)\\.[a-z0-9-]+$`` and its
name must contain no ``default``/``example``/``demo`` segment. Zero ids examined is a FAILURE.

Second pass (``sweep``): ANY tracked text file -- test fixtures included -- may not mention an
old-style id (``waddles.<ns>.<name>``, regex in ``OLD_ID_RE``). The structural parsers above only
read app_id-bearing fields in specific file kinds, which is how old ids in ``alembic/tests`` fixtures
slipped through. Excluded: this guard's own pattern, ``scripts/rename/`` (the old->new mapping),
and the platform PostHog flag keys ``waddles.core.rust-data-plane`` / ``waddles.core.disable-*``.
Pre-existing debt lives in ``scripts/ci/old_id_baseline.json`` (per-file match counts, many are
flag keys / proto packages / hostnames); a file may never exceed its baseline count and a file with
no baseline entry has a budget of zero.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ID_RE = re.compile(
    r"^(penguintechinc\.waddles|psychoboy\.penguintwitchbot)\.[a-z0-9-]+$"
)
FEATURE_RE = re.compile(r"^(penguintechinc\.waddles|psychoboy\.penguintwitchbot)$")
BANNED_SEGMENTS = {"default", "example", "demo"}
DOTTED_LITERAL = re.compile(r"""['"]([a-z0-9_]+(?:\.[a-z0-9_-]+)+)['"]""")
INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+(?:app_catalog|app_tenant_availability)\s*\(([^)]*)\)\s*(VALUES|SELECT)\b",
    re.IGNORECASE,
)
SQL_LITERAL = re.compile(r"^'([^']*)'$")
ALEMBIC_APP_ID = re.compile(r"""^\s*APP_ID\s*=\s*["']([^"']+)["']""", re.MULTILINE)
ALEMBIC_SEEDS = re.compile(r"^\s*[A-Z_]*APP_IDS\s*=\s*[(\[](.*?)[)\]]\s*$", re.MULTILINE | re.DOTALL)
YAML_APP_ID = re.compile(r"""^\s*(?:-\s*)?app_id:\s*["']?([^\s"'#]+)""", re.MULTILINE)
RUST_CONST = re.compile(
    r"""const\s+[A-Z0-9_]*APP_ID\s*:\s*&(?:'static\s+)?str\s*=\s*"([^"{}]*\.[^"{}]*)"\s*;"""
)
TOOL_CALL = re.compile(r"""tool\.call:([a-z0-9][a-z0-9._-]*):""")

# Built by concatenation so this file never matches its own pattern.
OLD_ID_RE = re.compile(
    r"(?<![a-z0-9.-])" + "waddles" + r"\.(?:core|integrations|social|[a-z]+)\.[a-z][a-z0-9_.-]*"
)
FLAG_KEY_RE = re.compile(
    r"^" + "waddles" + r"\.core\.(?:rust-data-plane$|disable-)"
)
SWEEP_SKIP_PREFIXES = (
    "scripts/ci/check_app_ids.py",
    "scripts/ci/tests/test_check_app_ids.py",
    "scripts/ci/old_id_baseline.json",
    "scripts/rename/",
)
BASELINE_REL = "scripts/ci/old_id_baseline.json"

SOURCES = (
    "sql_insert",
    "alembic_app_id",
    "catalog_yaml",
    "manifest",
    "rust_const",
    "tool_call_grant",
    "seed_feature",
)


def _files(root: Path) -> list[Path]:
    """List tracked files (git ls-files), falling back to a directory walk outside a repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True
        ).stdout
        return [root / p for p in out.decode().split("\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _split_top_level(s: str) -> list[str]:
    """Split a SQL value list on top-level commas, honouring quotes and nested parentheses."""
    parts, depth, cur, quote = [], 0, [], ""
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return parts


def _first_tuple(text: str, start: int) -> tuple[str, int]:
    """Return the body of the parenthesised tuple (or SELECT list) starting at ``start`` and its offset."""
    i = (
        text.find("(", start)
        if text[start:].lstrip().upper().startswith("(")
        or text[start : start + 1] == "("
        else -1
    )
    if i == -1:
        end = min(
            [
                j
                for j in (
                    text.find(";", start),
                    text.find('"""', start),
                    text.find("FROM", start),
                )
                if j != -1
            ]
            or [len(text)]
        )
        return text[start:end], start
    depth, quote = 0, ""
    for j in range(i, len(text)):
        ch = text[j]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j], i + 1
    return text[i + 1 :], i + 1


def parse_file(path: Path, rel: str, text: str) -> list[tuple[str, int, str, str]]:
    """Return (source, line, id, relpath) for every app_id-bearing field in one file."""
    found: list[tuple[str, int, str, str]] = []
    is_sql = rel.endswith(".sql") or (
        rel.startswith("alembic/versions/") and rel.endswith(".py")
    )
    if is_sql:
        for m in INSERT_RE.finditer(text):
            cols = [c.strip().lower() for c in m.group(1).split(",")]
            if "app_id" not in cols:
                continue
            body, off = _first_tuple(text, m.end())
            vals = _split_top_level(body)
            idx = cols.index("app_id")
            if idx < len(vals):
                lit = SQL_LITERAL.match(vals[idx])
                if (
                    lit and "{" not in lit.group(1)
                ):  # placeholders / bound params are covered by the alembic APP_ID parsers
                    found.append(("sql_insert", _line(text, off), lit.group(1), rel))
        for m in INSERT_RE.finditer(text):
            cols = [c.strip().lower() for c in m.group(1).split(",")]
            if "feature" not in cols or m.group(2).upper() != "VALUES":
                continue
            body, off = _first_tuple(text, m.end())
            vals = _split_top_level(body)
            idx = cols.index("feature")
            if idx < len(vals):
                lit = SQL_LITERAL.match(vals[idx])
                if lit and "{" not in lit.group(1):
                    found.append(("seed_feature", _line(text, off), lit.group(1), rel))
    if rel.startswith("alembic/versions/") and rel.endswith(".py"):
        for m in ALEMBIC_APP_ID.finditer(text):
            found.append(("alembic_app_id", _line(text, m.start(1)), m.group(1), rel))
        for m in ALEMBIC_SEEDS.finditer(text):
            for lit in DOTTED_LITERAL.finditer(m.group(1)):
                found.append(
                    (
                        "alembic_app_id",
                        _line(text, m.start(1) + lit.start()),
                        lit.group(1),
                        rel,
                    )
                )
    if rel == "bundles/core-bundles.yaml":
        for m in YAML_APP_ID.finditer(text):
            found.append(("catalog_yaml", _line(text, m.start(1)), m.group(1), rel))
    elif rel.startswith("bundles/") and path.name in (
        "bundle.yaml",
        "hub-manifest.yaml",
    ):
        for m in YAML_APP_ID.finditer(text):
            found.append(("manifest", _line(text, m.start(1)), m.group(1), rel))
    if rel.endswith(".rs"):
        for m in RUST_CONST.finditer(text):
            found.append(("rust_const", _line(text, m.start(1)), m.group(1), rel))
    if (rel.startswith("bundles/") and rel.endswith(".yaml")) or is_sql:
        for m in TOOL_CALL.finditer(text):
            found.append(("tool_call_grant", _line(text, m.start(1)), m.group(1), rel))
    return found


def validate_feature(feature: str) -> str | None:
    """Return a reason when a seeded ``feature`` value is not the new-shape ``<org>.<repo>`` namespace."""
    if not FEATURE_RE.match(feature):
        return "feature must be the <org>.<repo> namespace (old waddles.<category>.<name> ids are banned)"
    return None


def validate(app_id: str) -> str | None:
    """Return a human-readable reason when ``app_id`` is not a legal new-shape id, else None."""
    if not ID_RE.match(app_id):
        return "does not match <org>.<repo>.<name> with an allowed org.repo prefix"
    banned = BANNED_SEGMENTS & set(app_id.rsplit(".", 1)[1].split("-"))
    if banned:
        return f"name contains banned segment {sorted(banned)}"
    return None


def scan(root: Path) -> tuple[list[tuple[str, int, str, str]], dict[str, int]]:
    """Scan ``root``; return (violations, per-source denominator)."""
    denom = {s: 0 for s in SOURCES}
    bad: list[tuple[str, int, str, str]] = []
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(
            ("scripts/ci/check_app_ids.py", "scripts/ci/tests/test_check_app_ids.py")
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for src, line, app_id, rp in parse_file(path, rel, text):
            denom[src] += 1
            reason = validate_feature(app_id) if src == "seed_feature" else validate(app_id)
            if reason:
                bad.append((rp, line, app_id, f"{src}: {reason}"))
    return bad, denom


def sweep(root: Path) -> dict[str, list[tuple[int, str]]]:
    """Return {relpath: [(line, token), ...]} for every old-style id mention in tracked text files."""
    hits: dict[str, list[tuple[int, str]]] = {}
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(SWEEP_SKIP_PREFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in OLD_ID_RE.finditer(text):
            tok = m.group(0).rstrip(".")
            if FLAG_KEY_RE.match(tok):
                continue
            hits.setdefault(rel, []).append((_line(text, m.start()), tok))
    return hits


def load_baseline(root: Path) -> dict[str, int]:
    """Read the per-file pre-existing match budget (empty when absent)."""
    f = root / BASELINE_REL
    return json.loads(f.read_text()) if f.is_file() else {}


def sweep_violations(
    hits: dict[str, list[tuple[int, str]]], baseline: dict[str, int]
) -> list[tuple[str, int, str]]:
    """Hits beyond each file's baseline budget (budget 0 for unlisted files)."""
    out: list[tuple[str, int, str]] = []
    for rel, items in hits.items():
        budget = baseline.get(rel, 0)
        if len(items) > budget:
            out.extend((rel, ln, tok) for ln, tok in items[budget:])
    return out


def main(argv: list[str]) -> int:
    """CLI entry: exit 1 on any violation or a zero denominator in any source family."""
    write_baseline = "--write-baseline" in argv
    argv = [a for a in argv if a != "--write-baseline"]
    root = (
        Path(argv[1]).resolve()
        if len(argv) > 1
        else Path(__file__).resolve().parents[2]
    )
    if write_baseline:
        hits = sweep(root)
        (root / BASELINE_REL).write_text(
            json.dumps({k: len(v) for k, v in sorted(hits.items())}, indent=1) + "\n"
        )
        print(f"baseline written: files={len(hits)} matches={sum(map(len, hits.values()))}")
        return 0
    bad, denom = scan(root)
    total = sum(denom.values())
    print(
        f"check_app_ids: examined={total} "
        + " ".join(f"{k}={v}" for k, v in denom.items())
    )
    for rp, line, app_id, why in bad:
        print(f"  VIOLATION {rp}:{line}: {app_id!r} ({why})")
    hits = sweep(root)
    swept = len(_files(root))
    over = sweep_violations(hits, load_baseline(root))
    print(
        f"check_app_ids: sweep files_examined={swept} files_with_old_ids={len(hits)} "
        f"matches={sum(map(len, hits.values()))} over_baseline={len(over)}"
    )
    for rp, line, tok in over:
        print(f"  OLD-ID {rp}:{line}: {tok!r} (old-style app_id; use penguintechinc.waddles.<name>)")
    if swept == 0:
        print("check_app_ids: FAIL sweep examined zero files")
        return 1
    if over:
        print(f"check_app_ids: FAIL {len(over)} old-id mention(s) beyond baseline")
        return 1
    empty = [k for k, v in denom.items() if v == 0]
    if empty:
        print(f"check_app_ids: FAIL zero ids examined for source(s): {empty}")
        return 1
    if bad:
        print(f"check_app_ids: FAIL {len(bad)} violation(s)")
        return 1
    print("check_app_ids: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
