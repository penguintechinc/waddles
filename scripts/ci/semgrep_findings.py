"""Convert Semgrep JSON to gate findings using the checked-out source.

Semgrep can redact ``extra.lines`` to ``requires login``. Source ranges are
still present; read those full lines locally, preserving the baseline's
existing matched-line fingerprint rather than hashing a server message.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def convert(report: dict, root: Path) -> list[dict]:
    root = root.resolve(strict=True)
    findings = []
    for result in report["results"]:
        relative = Path(result["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid source path: {relative}")
        source = (root / relative).resolve(strict=True)
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError(f"source outside repository or not a file: {relative}")
        lines = source.read_text(encoding="utf-8").splitlines()
        start = result["start"]["line"]
        end = result["end"]["line"]
        if (type(start) is not int or type(end) is not int
                or not 1 <= start <= end <= len(lines)):
            raise ValueError(f"invalid source line range: {relative}:{start}-{end}")
        findings.append({
            "kind": "fallback",
            "rule_id": result["check_id"].rsplit(".", 1)[-1],
            "path": relative.as_posix(),
            "line": start,
            "text": "\n".join(lines[start - 1:end]),
        })
    return findings


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: semgrep_findings.py <semgrep.json> <repository-root>")
    with open(sys.argv[1], encoding="utf-8") as stream:
        report = json.load(stream)
    print(json.dumps(convert(report, Path(sys.argv[2]))))


if __name__ == "__main__":
    main()
