#!/usr/bin/env bash
# Connector-overlay golden-payload parity gate -- `make test-connector-parity PLATFORM=<p>`.
# Env: PLATFORM (twitch|discord|youtube, required)
#      PARITY_OVERLAY_WASM    built overlay .wasm (unset => fixture HARNESS SELF-CHECK only;
#                             set-but-missing => FAIL, never skip)
#      PARITY_OVERLAY_DIGEST  optional sha256:<hex> pin   PARITY_OVERLAY_APP_ID  PARITY_OVERLAY_WORLD
# Docs: docs/connector-parity-harness.md
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PLATFORM="${PLATFORM:-}"
case "$PLATFORM" in
    twitch|discord|youtube) ;;
    *) echo "[connector-parity] PLATFORM must be twitch|discord|youtube (got '${PLATFORM}')" >&2; exit 2 ;;
esac
command -v cargo >/dev/null 2>&1 || { echo "[connector-parity] cargo not found" >&2; exit 127; }
command -v python3 >/dev/null 2>&1 || { echo "[connector-parity] python3 not found" >&2; exit 127; }

if [ -n "${PARITY_OVERLAY_WASM+x}" ]; then
    if [ ! -f "$PARITY_OVERLAY_WASM" ]; then
        echo "[connector-parity] FAIL: PARITY_OVERLAY_WASM=$PARITY_OVERLAY_WASM does not exist (never a skip)" >&2
        exit 1
    fi
    PARITY_OVERLAY_WASM="$(cd "$(dirname "$PARITY_OVERLAY_WASM")" && pwd)/$(basename "$PARITY_OVERLAY_WASM")"
    export PARITY_OVERLAY_WASM
fi

echo "[connector-parity] === expected/ current with corpus ==="
python3 -I scripts/ci/gen-connector-parity-expected.py --check

echo "[connector-parity] === on-frame parity: $PLATFORM ==="
out="$(mktemp)"
trap 'rm -f "$out"' EXIT
set +e
(cd core/bundle_executor && PARITY_PLATFORM="$PLATFORM" cargo test --locked --test connector_parity overlay_parity -- --ignored --nocapture) 2>&1 | tee "$out"
rc=${PIPESTATUS[0]}
set -e
[ "$rc" -eq 0 ] || { echo "[connector-parity] FAIL: parity run exited $rc" >&2; exit "$rc"; }
grep -Eq '^connector_parity: .* examined=[1-9][0-9]* passed=[1-9][0-9]* failed=0 ' "$out" \
    || { echo "[connector-parity] FAIL: denominator line missing/zero -- nothing examined" >&2; exit 1; }
if [ -z "${PARITY_OVERLAY_WASM+x}" ]; then
    echo "[connector-parity] NOTE: harness self-check only (no PARITY_OVERLAY_WASM) -- NOT overlay parity"
fi
echo "[connector-parity] PASSED"
