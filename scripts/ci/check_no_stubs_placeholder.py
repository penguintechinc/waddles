"""Webui placeholder-import / placeholder-text finder for check-no-stubs.sh.

Any webui page that imports `NotAvailableYet` or carries the literal
"Missing server endpoints" is a stub: a screen whose hub-api endpoints do
not exist. Import statements can span lines, so this is a multi-line regex
pass rather than a line grep (the JSX `<NotAvailableYet` use stays in the
shell grep layer).

Usage: check_no_stubs_placeholder.py <file>...   (paths relative to repo root)
Prints a JSON array of {"kind": "stub", "path", "line", "text"} findings.
"""
from __future__ import annotations

import json
import re
import sys

# `import { A, NotAvailableYet } from '...'` (single or multi-line, incl.
# `import type`), a default import named NotAvailableYet, or any import
# whose module specifier is the NotAvailableYet component file.
IMPORT_RE = re.compile(
    r"\bimport\s+(?:type\s+)?"
    r"(?:\{[^}]*\bNotAvailableYet\b[^}]*\}"
    r"|NotAvailableYet\b[^;]*?\bfrom\b"
    r"|[^;]*?\bfrom\s*['\"][^'\"]*/NotAvailableYet(?:\.[jt]sx?)?['\"])",
    re.S,
)
TOKEN_RE = re.compile(r"\bNotAvailableYet\b")
LITERAL_RE = re.compile(r"Missing server endpoints")


def line_of(src: str, offset: int) -> int:
    """Returns the 1-based line number of a character offset in src."""
    return src.count("\n", 0, offset) + 1


def scan(path: str) -> list[dict]:
    """Returns placeholder-import and placeholder-text findings for one file."""
    with open(path, encoding="utf-8", errors="replace") as f:
        src = f.read()
    lines = src.splitlines()
    hits: dict[int, str] = {}
    for m in IMPORT_RE.finditer(src):
        tok = TOKEN_RE.search(src, m.start(), m.end())
        ln = line_of(src, tok.start() if tok else m.start())
        hits[ln] = "placeholder-import: " + lines[ln - 1].strip()
    for m in LITERAL_RE.finditer(src):
        ln = line_of(src, m.start())
        hits.setdefault(ln, "placeholder-text: " + lines[ln - 1].strip())
    return [
        {"kind": "stub", "path": path, "line": ln, "text": text}
        for ln, text in sorted(hits.items())
    ]


def main() -> int:
    """Scans every path given on argv and prints the combined findings."""
    findings: list[dict] = []
    for path in sys.argv[1:]:
        findings.extend(scan(path))
    print(json.dumps(findings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
