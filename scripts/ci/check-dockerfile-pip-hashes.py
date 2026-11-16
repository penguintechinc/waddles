#!/usr/bin/env python3
"""Fail if ANY `pip install` in a Dockerfile is not hash-verified.

Standing rule (critical-rules.md Dependency Pinning): every dependency is pinned with
cryptographic hashes in every environment. A bare `pip install <pkg>` or
`pip install /path/to/local/package` (which resolves the package's declared dependencies from
the network, unhashed) breaks that, cache mount or not. Every `pip install` / `python -m pip
install` / `uv pip install` RUN segment must be one of:

  * `--require-hashes` (with a hash-locked `-r` file), or
  * a LOCAL-path install with BOTH `--no-deps` and `--no-build-isolation`, in a build stage that
    already ran a `--require-hashes` install (so the local package's dependencies were
    installed hash-verified and nothing is fetched at build time -- isolation would fetch its
    build backend unhashed).

Stage tracking: `FROM x AS name` starts a stage; `FROM name` inherits an earlier stage's state.
Exit 1 on any violation, and on zero Dockerfiles / zero pip installs examined (a scanner pointed
at the wrong root must not report clean).

Usage: check-dockerfile-pip-hashes.py [dockerfile ...]   (default: every Dockerfile in git)
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FROM_RE = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
RUN_RE = re.compile(r"^\s*RUN\s+(.*)$", re.IGNORECASE)
PIP_RE = re.compile(r"(?:^|[\s;&|(])(?:python3?\s+-m\s+)?pip3?\s+install\b")
SPLIT_RE = re.compile(r"&&|\|\||;|\|")


@dataclass(frozen=True)
class Finding:
    """One violating `pip install` step."""

    file: str
    line: int
    why: str


def logical_runs(text: str) -> list[tuple[int, str]]:
    """Return (line number, command) per FROM/RUN instruction, in file order.

    Continuations are joined and comment lines inside a continued RUN are dropped, as Docker
    does. FROM lines are emitted as ("FROM ...") entries so the caller can track stages.
    """
    out: list[tuple[int, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        if FROM_RE.match(raw):
            out.append((i + 1, raw.strip()))
            i += 1
            continue
        m = RUN_RE.match(raw)
        if not m:
            i += 1
            continue
        start = i + 1
        cmd = m.group(1)
        continued = cmd.rstrip().endswith("\\")
        cmd = cmd.rstrip()[:-1] + " " if continued else cmd
        while continued and i + 1 < len(lines):
            i += 1
            nxt = lines[i].strip()
            if nxt.startswith("#"):
                continue
            continued = nxt.endswith("\\")
            cmd += (nxt[:-1] + " ") if continued else nxt
        out.append((start, cmd))
        i += 1
    return out


def is_local_path(token: str) -> bool:
    """True for a filesystem-path install target (`.`, `./x`, `../x`, `/x`)."""
    return token == "." or token.startswith(("./", "../", "/"))


def install_args(seg: str) -> list[str]:
    """Tokens after `pip install` in one command segment."""
    m = PIP_RE.search(seg)
    assert m is not None
    try:
        toks = shlex.split(seg[m.end() :])
    except ValueError:
        toks = seg[m.end() :].split()
    return toks


def check_segment(seg: str, stage_hashed: bool) -> tuple[list[str], bool]:
    """Return (violations, whether this segment is a --require-hashes install)."""
    toks = install_args(seg)
    if "--require-hashes" in toks:
        return [], True
    # Positional install targets: tokens that are not options or option values of -r/-c/-e/...
    targets: list[str] = []
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        if t in {"-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i",
                 "--index-url", "--extra-index-url", "-f", "--find-links", "-t", "--target",
                 "--prefix", "--root", "--platform", "--python-version", "--only-binary",
                 "--no-binary"}:
            skip = True
            continue
        if t.startswith("-"):
            continue
        targets.append(t)
    local_only = bool(targets) and all(is_local_path(t) for t in targets)
    problems: list[str] = []
    if not local_only:
        problems.append(
            "`pip install` without --require-hashes (unhashed packages resolved from the "
            "network): hash-lock via `uv pip compile --generate-hashes` and install with "
            "`--require-hashes -r`"
        )
    else:
        if "--no-deps" not in toks:
            problems.append(
                "local-path `pip install` without --no-deps: its declared dependencies would be "
                "resolved unhashed -- fold them into the hash-locked requirements first"
            )
        if "--no-build-isolation" not in toks:
            problems.append(
                "local-path `pip install` without --no-build-isolation: build isolation fetches "
                "the build backend unhashed"
            )
        if not stage_hashed:
            problems.append(
                "local-path `pip install --no-deps` with no earlier --require-hashes install in "
                "this build stage: nothing hash-locked supplies its dependencies"
            )
    return problems, False


def check_text(name: str, text: str) -> tuple[int, list[Finding]]:
    """Check one Dockerfile; return (pip install segments examined, findings), stage-aware."""
    examined = 0
    findings: list[Finding] = []
    stage_state: dict[str, bool] = {}
    cur_alias: str | None = None
    hashed = False
    for line_no, cmd in logical_runs(text):
        fm = FROM_RE.match(cmd)
        if fm and not RUN_RE.match(cmd):
            if cur_alias:
                stage_state[cur_alias] = hashed
            base, cur_alias = fm.group(1), fm.group(2)
            hashed = stage_state.get(base, False)
            continue
        for seg in SPLIT_RE.split(cmd):
            if not PIP_RE.search(seg):
                continue
            examined += 1
            problems, is_hashed = check_segment(seg, hashed)
            findings += [Finding(name, line_no, why) for why in problems]
            hashed = hashed or is_hashed
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
    print(f"dockerfiles={len(paths)} pip_installs={total} violations={len(findings)}")
    if not paths or total == 0:
        print("ERROR: nothing examined", file=sys.stderr)
        return 1
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
