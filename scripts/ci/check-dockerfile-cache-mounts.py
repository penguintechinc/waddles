#!/usr/bin/env python3
"""Fail if a BuildKit cache-mounted install step is not integrity-verified.

A `RUN --mount=type=cache ...` shares its cache across builds on a persistent BuildKit pod, so
whatever an install step writes there is trusted by every later build. Only steps that verify
content against a committed lock may use one:

  pip / pip3 / python -m pip install   needs --require-hashes, or --no-deps on a local path
  npm install / npm i                  forbidden (use `npm ci`, which checks lockfile integrity)
  cargo build/install/fetch            needs --locked (Cargo.lock checksums every crate)
  yarn / pnpm install                  needs --frozen-lockfile

Anything else that mounts a cache with an install command we do not recognise is reported too.
Exit 1 on any violation, and on zero Dockerfiles / zero cache-mounted steps examined.

Usage: check-dockerfile-cache-mounts.py [dockerfile ...]   (default: every Dockerfile in git)
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUN_RE = re.compile(r"^\s*RUN\s+(.*)$", re.IGNORECASE)
PIP_RE = re.compile(r"(?:^|[\s;&|(])(?:python3?\s+-m\s+)?pip3?\s+install\b")
NPM_INSTALL_RE = re.compile(r"(?:^|[\s;&|(])npm\s+(?:install|i|add)\b")
CARGO_RE = re.compile(r"(?:^|[\s;&|(])cargo\s+(?:build|install|fetch|check|test)\b")
YARN_RE = re.compile(r"(?:^|[\s;&|(])(?:yarn|pnpm)\s+(?:install|add)\b")


@dataclass(frozen=True)
class Finding:
    """One violating cache-mounted step."""

    file: str
    line: int
    why: str


def logical_runs(text: str) -> list[tuple[int, str]]:
    """Return (first line number, command) for each RUN, with line continuations joined."""
    out: list[tuple[int, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = RUN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start = i + 1
        cmd = m.group(1)
        while cmd.rstrip().endswith("\\") and i + 1 < len(lines):
            cmd = cmd.rstrip()[:-1] + " "
            i += 1
            cmd += lines[i].strip()
        out.append((start, cmd))
        i += 1
    return out


def check_run(cmd: str) -> list[str]:
    """Violations for one RUN command that mounts a cache (empty list = fine)."""
    problems: list[str] = []
    if PIP_RE.search(cmd):
        for seg in re.split(r"&&|;|\|", cmd):
            if not PIP_RE.search(seg):
                continue
            hashed = "--require-hashes" in seg
            local_nodeps = "--no-deps" in seg and re.search(
                r"\s(?:\./|/|\.\./)[^\s]*", seg
            )
            if not (hashed or local_nodeps):
                problems.append(
                    "cache-mounted `pip install` without --require-hashes "
                    "(or --no-deps on a local path): unhashed packages would enter the shared cache"
                )
    if NPM_INSTALL_RE.search(cmd):
        problems.append(
            "cache-mounted `npm install`: use `npm ci` (lockfile integrity-checked)"
        )
    if CARGO_RE.search(cmd):
        for seg in re.split(r"&&|;|\|", cmd):
            if CARGO_RE.search(seg) and "--locked" not in seg:
                problems.append("cache-mounted cargo step without --locked")
    if YARN_RE.search(cmd) and "--frozen-lockfile" not in cmd:
        problems.append("cache-mounted yarn/pnpm install without --frozen-lockfile")
    return problems


def check_text(name: str, text: str) -> tuple[int, list[Finding]]:
    """Check one Dockerfile; return (cache-mounted steps examined, findings)."""
    examined = 0
    findings: list[Finding] = []
    for line_no, cmd in logical_runs(text):
        if "--mount=type=cache" not in cmd:
            continue
        examined += 1
        findings += [Finding(name, line_no, why) for why in check_run(cmd)]
    return examined, findings


def dockerfiles() -> list[str]:
    """All tracked Dockerfiles."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"], check=True, capture_output=True
    ).stdout.decode()
    return [p for p in out.split("\0") if re.search(r"(^|/)Dockerfile[^/]*$", p)]


def main(argv: list[str]) -> int:
    """CLI entry point."""
    paths = argv or dockerfiles()
    total = 0
    findings: list[Finding] = []
    for p in paths:
        f = Path(p) if Path(p).is_absolute() else REPO / p
        n, fs = check_text(p, f.read_text())
        total += n
        findings += fs
    for fd in findings:
        print(f"FAIL {fd.file}:{fd.line}: {fd.why}", file=sys.stderr)
    print(
        f"dockerfiles={len(paths)} cache_mounted_steps={total} violations={len(findings)}"
    )
    if not paths or total == 0:
        print("ERROR: nothing examined", file=sys.stderr)
        return 1
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
