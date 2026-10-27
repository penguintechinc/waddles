#!/usr/bin/env bash
# Static, no-DB gate against the "diverged alembic heads" failure class hit
# 4x in this repo's history: a stacked-branch rebase or a same-cycle
# parallel migration PR leaves two revisions both claiming no successor,
# which `alembic upgrade head` silently tolerates locally (branches merge
# fine on a dev box with only one branch ever applied) but fails opaquely in
# CI/prod the moment both branches actually need to apply.
#
# Pure revision-graph walk over `alembic/versions/*.py` source text -- no
# `alembic` package import, no DB connection required, so this runs on
# every PR regardless of whether Postgres is available (unlike
# `pr-validation.yml`'s heavier `alembic-migration-chain` job, which is
# real-Postgres and gated to `alembic/**` changes only).
#
# Asserts:
#   1. Exactly one alembic head (no diverged branches).
#   2. Every revision id is <=32 chars (Postgres alembic_version.version_num
#      is VARCHAR(32); a longer id fails silently truncated or rejected at
#      stamp/upgrade time, never at authoring time).
#   3. Every down_revision resolves to a real revision id in the set.
#
# Exit code is the gate: zero revision files examined is a hard failure,
# never a silent pass (critical-rules.md Verification Integrity).
set -euo pipefail

REPO_ROOT="${1:-.}"
VERSIONS_DIR="${REPO_ROOT}/alembic/versions"

if [ ! -d "${VERSIONS_DIR}" ]; then
    echo "::error::alembic versions directory not found at ${VERSIONS_DIR} -- check REPO_ROOT." >&2
    exit 1
fi

python3 - "${VERSIONS_DIR}" <<'PY'
import re
import sys
from pathlib import Path

versions_dir = Path(sys.argv[1])
files = sorted(versions_dir.glob("*.py"))

if not files:
    print(f"::error::0 alembic revision files found under {versions_dir} -- "
          "scanner is pointed at the wrong path.", file=sys.stderr)
    sys.exit(1)

REVISION_RE = re.compile(r'^revision\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
DOWN_SCALAR_RE = re.compile(r'^down_revision\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
DOWN_NONE_RE = re.compile(r'^down_revision\s*=\s*None\b', re.MULTILINE)
DOWN_TUPLE_RE = re.compile(r'^down_revision\s*=\s*\(([^)]*)\)', re.MULTILINE)

revisions: dict[str, list[str]] = {}
errors: list[str] = []

for path in files:
    text = path.read_text()
    rev_match = REVISION_RE.search(text)
    if not rev_match:
        errors.append(f"{path.name}: no `revision = ...` assignment found")
        continue
    rev = rev_match.group(1)

    downs: list[str] = []
    tuple_match = DOWN_TUPLE_RE.search(text)
    if tuple_match:
        downs = [d.strip().strip("'\"") for d in tuple_match.group(1).split(",") if d.strip()]
    elif DOWN_NONE_RE.search(text):
        downs = []
    else:
        scalar_match = DOWN_SCALAR_RE.search(text)
        if scalar_match:
            downs = [scalar_match.group(1)]
        else:
            errors.append(f"{path.name}: no `down_revision = ...` assignment found")
            continue

    if rev in revisions:
        errors.append(f"duplicate revision id '{rev}' in {path.name} and a prior file")
    revisions[rev] = downs

print(f"Examined {len(files)} alembic revision file(s), {len(revisions)} unique revision id(s).")

for rev in revisions:
    if len(rev) > 32:
        errors.append(f"revision id '{rev}' is {len(rev)} chars (>32 -- exceeds Postgres "
                       "alembic_version.version_num column width)")

all_ids = set(revisions)
for rev, downs in revisions.items():
    for down in downs:
        if down not in all_ids:
            errors.append(f"revision '{rev}' has down_revision '{down}' which does not "
                           "resolve to any known revision id")

referenced_as_down: set[str] = set()
for downs in revisions.values():
    referenced_as_down.update(downs)
heads = sorted(set(revisions) - referenced_as_down)

if len(heads) != 1:
    errors.append(f"expected exactly 1 alembic head, found {len(heads)}: {heads}")
else:
    print(f"Single head confirmed: {heads[0]}")

if errors:
    for err in errors:
        print(f"::error::{err}", file=sys.stderr)
    print(f"FAIL -- {len(errors)} issue(s) found.", file=sys.stderr)
    sys.exit(1)

print("PASS -- single head, all revision ids <=32 chars, all down_revisions resolve.")
PY
