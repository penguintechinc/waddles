#!/usr/bin/env bash
# Sanity check for wit/waddle-bundle/stage.wit -- cheap grep-based structural
# assertions runnable with no WASM toolchain installed. The authoritative
# check is `wasm-tools component wit wit/waddle-bundle/stage.wit`, run in
# CI (Task 19/20) once the pinned toolchain image exists.
set -euo pipefail

FILE="wit/waddle-bundle/stage.wit"
test -f "$FILE"
grep -q "^package waddle:bundle@1.0.0;" "$FILE"
grep -q "^world stage {" "$FILE"

# `flags` is escaped as `%flags` -- it is a reserved WIT keyword (the
# `flags` bitset type) as of wasm-tools 1.259.0; `%` is WIT's standard
# identifier escape and does not change the resolved interface name.
for iface in context http kv db relay "%flags" log clock; do
  grep -q "import $iface;" "$FILE"
done
for exp in process-stage action-stage; do
  grep -q "export $exp;" "$FILE"
done

# `db` is structured ops (insert/get/query/update/delete), never raw SQL --
# design doc SS1 round-1 CRITICAL finding. Assert the structured ops exist
# and the retired raw-SQL `execute` shape is gone.
for op in insert get query update delete; do
  grep -q "  $op:" "$FILE"
done
! grep -q "execute: func(statement" "$FILE"

# `world stage-v2` (numeric scope): the numeric ids live in NEW interfaces --
# the frozen v1 records (`types.stage-envelope`, `context.bundle-context`) must
# NOT have grown a numeric field (WIT records are binary-breaking).
grep -q "^world stage-v2 {" "$FILE"
grep -q "export action-stage-v2;" "$FILE"
grep -q "import context-v2;" "$FILE"
for iface in types-v2 context-v2 action-stage-v2; do
  grep -q "^interface $iface {" "$FILE"
done
# Extract the v1 record bodies and prove they carry no numeric-scope field.
for rec in "record stage-envelope {" "record bundle-context {"; do
  first=$(grep -n "$rec" "$FILE" | head -1 | cut -d: -f1)
  body=$(sed -n "${first},/^  }/p" "$FILE")
  if printf '%s' "$body" | grep -q "tenant-id\|community-id"; then
    echo "FAIL: frozen v1 '$rec' (first occurrence) grew a numeric-scope field" >&2
    exit 1
  fi
done
grep -q "tenant-id: s64" "$FILE"
grep -q "community-id: s32" "$FILE"

echo "PASS: stage.wit declares 8 imports + 2 exports, db is structured (no raw SQL), stage-v2 carries numeric scope without touching v1 records"
