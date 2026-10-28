#!/usr/bin/env bash
# Live e2e gate: Rust `connection_resolver` <-> REAL hub-api blueprints <-> REAL Postgres 17.
#
# 1. starts hub_api/tests/live/live_hub.py (Postgres container migrated to Alembic head, hub-api
#    routes, identities rendered from the real Helm chart) and waits for its manifest;
# 2. runs core/connection_resolver/tests/e2e_live_hub.rs (`#[ignore]`d tests) against it;
# 3. FAILS unless (a) every ignored test ran and passed (zero run = fail) and (b) the resolver
#    emitted >=1 tracing log record, >=1 span and >=1 metric data point (testing.md telemetry gate).
# Counts are always printed. bash 3.2 compatible. Needs docker, helm, cargo, and a python with the
# hub-api requirements (HUB_PYTHON, default python3).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HUB_PYTHON="${HUB_PYTHON:-python3}"
# Own target dir: the shared CARGO_TARGET_DIR is contended (serialises on the cargo build lock).
export CARGO_TARGET_DIR="${CONNECTION_RESOLVER_LIVE_TARGET:-${TMPDIR:-/tmp}/connection-resolver-live-target}"

for tool in docker helm cargo "$HUB_PYTHON"; do
  command -v "$tool" >/dev/null 2>&1 || { echo "FAIL: required tool '$tool' not on PATH" >&2; exit 3; }
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/connection-resolver-live.XXXXXX")"
MANIFEST="$WORK/manifest.json"
HUB_LOG="$WORK/hub.log"
TEST_LOG="$WORK/test.log"
HUB_PID=""

cleanup() {
  if [ -n "$HUB_PID" ] && kill -0 "$HUB_PID" 2>/dev/null; then
    kill -TERM "$HUB_PID" 2>/dev/null || true
    wait "$HUB_PID" 2>/dev/null || true   # teardown only; the gates below already decided pass/fail
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cd "$ROOT/hub_api"
PYTHONPATH="$ROOT/libs/flask_core:$ROOT/hub_api" "$HUB_PYTHON" -m tests.live.live_hub "$MANIFEST" >"$HUB_LOG" 2>&1 &
HUB_PID=$!

waited=0
while [ ! -s "$MANIFEST" ]; do
  if ! kill -0 "$HUB_PID" 2>/dev/null; then
    echo "FAIL: live hub-api exited before becoming ready" >&2
    tail -40 "$HUB_LOG" >&2
    exit 1
  fi
  if [ "$waited" -ge 240 ]; then
    echo "FAIL: live hub-api not ready after ${waited}s" >&2
    tail -40 "$HUB_LOG" >&2
    exit 1
  fi
  sleep 2
  waited=$((waited + 2))
done
echo "live hub-api ready after ~${waited}s"

cd "$ROOT/core/connection_resolver"
expected="$(cargo test --locked --test e2e_live_hub -- --ignored --list 2>/dev/null | grep -c ': test$' || true)"
if [ "$expected" -lt 1 ]; then
  echo "FAIL: no #[ignore]d live tests found (expected >= 1)" >&2
  exit 1
fi

set +e
LIVE_HUB_MANIFEST="$MANIFEST" cargo test --locked --test e2e_live_hub -- \
  --ignored --test-threads=1 --nocapture 2>&1 | tee "$TEST_LOG"
test_status=${PIPESTATUS[0]}
set -e
if [ "$test_status" -ne 0 ]; then
  echo "FAIL: live tests failed (exit $test_status)" >&2
  exit "$test_status"
fi

passed="$(sed -n 's/^test result: ok\. \([0-9][0-9]*\) passed.*/\1/p' "$TEST_LOG" | tail -1)"
passed="${passed:-0}"
logs=0; spans=0; metrics=0
while read -r l s m; do
  logs=$((logs + l)); spans=$((spans + s)); metrics=$((metrics + m))
done < <(sed -n 's/^.*LIVE_TELEMETRY test=[a-z]* logs=\([0-9]*\) spans=\([0-9]*\) metrics=\([0-9]*\).*$/\1 \2 \3/p' "$TEST_LOG")

echo "LIVE SUMMARY: tests_passed=$passed tests_expected=$expected log_records=$logs spans=$spans metric_points=$metrics"
[ "$passed" -ge 1 ] || { echo "FAIL: zero live tests ran" >&2; exit 1; }
[ "$passed" -eq "$expected" ] || { echo "FAIL: passed ($passed) != expected ($expected)" >&2; exit 1; }
[ "$logs" -ge 1 ] || { echo "FAIL: zero log records emitted by the resolver" >&2; exit 1; }
[ "$spans" -ge 1 ] || { echo "FAIL: zero spans emitted by the resolver" >&2; exit 1; }
[ "$metrics" -ge 1 ] || { echo "FAIL: zero metric data points emitted by the resolver" >&2; exit 1; }
echo "PASS: connection_resolver live e2e"
