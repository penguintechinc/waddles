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

echo "PASS: stage.wit declares 8 imports + 2 exports"
