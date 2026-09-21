#!/usr/bin/env bash
# Bundle DAL migration gate (M1.5, docs/superpowers/specs/2026-09-14-rust-data-plane-design.md
# §16 "M1.5 -- Bundle DAL migration", Gate deliverable).
#
# Asserts zero occurrences of the legacy `flask_core.database`/`pydal` surface
# under today's three real App Bundle directories. The spec names
# `bundles/python/`, but that path does not exist until M2/M6's directory
# move -- `core/svc_action/bundles`, `core/svc_ingest/bundles` and
# `core/svc_process/bundles` are the real locations today (verified by
# listing the repo, not assumed).
#
# Unlike scripts/lint.sh's checks, this is not a debt ratchet against
# `.checks-baseline` -- the milestone's Done-when wording is a hard zero, so
# any occurrence fails, every time, until the migration (tracked separately,
# in the parallel svc_action/svc_process branches) actually lands. Expected
# to FAIL today: the bundles still carry docstring/comment references to
# `pydal`'s `Set`/`Field` API describing the legacy AsyncDAL wrapper
# `get_bundle_dal()` returns -- a gate that cannot fail is not a gate
# (critical-rules.md Verification Integrity).
#
# Bash 3.2 compatible (general.md) -- no associative arrays, no mapfile.
#
# Usage: scripts/check-bundle-dal-imports.sh            full gate (make check-bundle-dal)
#        scripts/check-bundle-dal-imports.sh --count     print only the occurrence count

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/checks.sh
. scripts/lib/checks.sh

BUNDLE_DIRS="./core/svc_action/bundles ./core/svc_ingest/bundles ./core/svc_process/bundles"

count_only=0
[ "${1:-}" = "--count" ] && count_only=1

files=""
for dir in $BUNDLE_DIRS; do
  if [ ! -d "$dir" ]; then
    echo "check-bundle-dal-imports: FAIL -- expected bundle directory missing: $dir" >&2
    exit 1
  fi
  files="${files}
$(discover -path "${dir}/*" -name '*.py')"
done
files_scanned=$(count_lines "$files")

if [ "$files_scanned" -eq 0 ]; then
  echo "check-bundle-dal-imports: FAIL -- zero files scanned (bundle dirs moved or emptied?)" >&2
  exit 1
fi

hits=0
hit_files=0
findings=""
while IFS= read -r f; do
  [ -z "$f" ] && continue
  file_hit=0
  while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    hits=$((hits + 1))
    file_hit=1
    findings="${findings}
${f}:${hit}"
  done <<FILE_HITS
$(grep -nE 'flask_core\.database|\bpydal\b' "$f" || true)
FILE_HITS
  [ "$file_hit" -eq 1 ] && hit_files=$((hit_files + 1))
done <<BUNDLE_FILES
$files
BUNDLE_FILES

if [ "$count_only" -eq 1 ]; then
  echo "$hits"
  exit 0
fi

if [ "$hits" -gt 0 ]; then
  printf '%s\n' "$findings" | grep -v '^$'
  echo "check-bundle-dal-imports: FAIL -- files_scanned=$files_scanned legacy_occurrences=$hits in $hit_files file(s)" >&2
  echo "  -> migrate to penguin-dal (see docs/APP_BUNDLE_AUTHORING.md 'Accessing the database')" >&2
  exit 1
fi

echo "check-bundle-dal-imports: PASS -- files_scanned=$files_scanned legacy_occurrences=0"
exit 0
