#!/usr/bin/env python3
"""Mirror scripts/rename/version_bumps.tsv into every bundle manifest and drop stale ``feature:`` lines.

For each bundle dir holding a bundle.yaml/hub-manifest.yaml whose ``app_id`` is listed in the TSV,
sets ``version:`` to the new version (only when it currently equals the old version or is already
the new one) and bumps pyproject.toml / Cargo.toml / *.csproj ``version`` when it tracked the old
version. Also removes ``feature: waddles.core.*`` lines (the field is optional and defaults to the
org.repo prefix). Idempotent; ``--check`` exits 1 on any drift.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUMPS = {}
with open(ROOT / "scripts/rename/version_bumps.tsv") as fh:
    for r in csv.DictReader(fh, delimiter="\t"):
        BUMPS[r["new_app_id"]] = (r["old_version"], r["new_version"])

APP = re.compile(r"^app_id:\s*[\"']?([^\s\"']+)", re.MULTILINE)
FEATURE = re.compile(r"^feature:\s*[\"']?waddles\.[^\n]*\n", re.MULTILINE)
VER = re.compile(r"^(version:\s*)[\"']?([0-9][^\s\"']*)[\"']?\s*$", re.MULTILINE)
check = "--check" in sys.argv
drift = []


def write(path: Path, new: str, old: str) -> None:
    if new != old:
        drift.append(str(path.relative_to(ROOT)))
        if not check:
            path.write_text(new)


def bump_tracking(path: Path, old_v: str, new_v: str, pat: str) -> None:
    txt = path.read_text()
    new = re.sub(
        pat % re.escape(old_v),
        lambda m: m.group(1) + new_v + m.group(2),
        txt,
        count=1,
        flags=re.MULTILINE,
    )
    write(path, new, txt)


seen = set()
for man in sorted(ROOT.glob("bundles/**/*.yaml")):
    if man.name not in ("bundle.yaml", "hub-manifest.yaml"):
        continue
    txt = man.read_text()
    new = FEATURE.sub("", txt)
    m = APP.search(txt)
    if m and m.group(1) in BUMPS:
        old_v, new_v = BUMPS[m.group(1)]
        seen.add(m.group(1))
        new = VER.sub(
            lambda v: (
                v.group(1) + new_v if v.group(2) in (old_v, new_v) else v.group(0)
            ),
            new,
            count=1,
        )
        d = man.parent
        for pp in (
            d / "pyproject.toml",
            d / "Cargo.toml",
            *d.glob("*.csproj"),
            *d.glob("*/*.csproj"),
        ):
            if pp.is_file():
                if pp.suffix == ".csproj":
                    bump_tracking(pp, old_v, new_v, r"(<Version>)%s(</Version>)")
                else:
                    bump_tracking(pp, old_v, new_v, r'(^version\s*=\s*")%s(")')
    write(man, new, txt)

missing = sorted(set(BUMPS) - seen)
print(
    f"bumps={len(BUMPS)} matched={len(seen)} unmatched={len(missing)} changed={len(drift)}"
)
if missing:
    print("unmatched (catalog-only or no manifest):", ", ".join(missing[:12]))
sys.exit(1 if (check and drift) else 0)
