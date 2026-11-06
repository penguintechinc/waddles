#!/usr/bin/env bash
# Builds the YouTube connector component and runs its real-path executor tests
# (core/bundle_executor/tests/youtube_connector.rs) against the compiled wasm.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/bundles/rust/youtube"
cargo test --locked
cargo component build --release --target wasm32-wasip2 --locked

TARGET_DIR="$(cargo metadata --format-version 1 --no-deps | python3 -c 'import json,sys; print(json.load(sys.stdin)["target_directory"])')"
WASM="$(find "$TARGET_DIR" -maxdepth 3 -path "$TARGET_DIR/wasm32-wasip*/release/waddle_bundle_youtube_service.wasm" | sort | head -n1)"
[ -s "$WASM" ] || { echo "FAIL: youtube wasm not built" >&2; exit 1; }

cd "$ROOT/core/bundle_executor"
YOUTUBE_CONNECTOR_WASM="$WASM" cargo test --locked --test youtube_connector -- --ignored --nocapture 2>&1 | tee /dev/stderr | grep -E "test result: ok\. [1-9][0-9]* passed" >/dev/null \
  || { echo "FAIL: zero real-path tests passed" >&2; exit 1; }
echo "OK: youtube connector real-path tests passed"
