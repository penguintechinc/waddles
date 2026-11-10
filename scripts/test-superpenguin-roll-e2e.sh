#!/usr/bin/env bash
# Real-path gate for the C# superpenguin-roll bundle: builds the component from its pinned Dockerfile, then loads it
# in the REAL bundle-executor (wasmtime + production linker) and drives process-stage.transform through
# core/bundle_executor/tests/csharp_superpenguin_roll_e2e.rs. Fails (never skips) if either step fails.
#
# Usage: scripts/test-superpenguin-roll-e2e.sh   (make test-superpenguin-roll-e2e)
set -euo pipefail
cd "$(dirname "$0")/.."

out_dir="${SUPERPENGUIN_ROLL_OUT:-/tmp/superpenguin-roll-out}"
bash scripts/verify-superpenguin-roll-fixture.sh "$out_dir"

wasm="${out_dir}/superpenguin_roll.wasm"
[ -s "$wasm" ] || { echo "test-superpenguin-roll-e2e: FAIL -- ${wasm} missing or empty" >&2; exit 1; }

output=$(cd core/bundle_executor && SUPERPENGUIN_ROLL_WASM="$wasm" cargo test --locked \
  --test csharp_superpenguin_roll_e2e -- --ignored --nocapture 2>&1) || { echo "$output" >&2; echo "test-superpenguin-roll-e2e: FAIL" >&2; exit 1; }
echo "$output" | tail -15
# Zero tests run is a failure, not a pass (critical-rules.md Verification Integrity).
echo "$output" | grep -Eq 'test result: ok\. [1-9][0-9]* passed' || { echo "test-superpenguin-roll-e2e: FAIL -- no test passed" >&2; exit 1; }
echo "test-superpenguin-roll-e2e: PASS"
