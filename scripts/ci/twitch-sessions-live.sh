#!/usr/bin/env bash
# Live e2e gate: svc-ingest per-community Twitch sessions <-> REAL hub-api <-> REAL Postgres 17
# <-> REAL Valkey <-> a real-socket IRC test server.
#
# 1. starts a Valkey container (VALKEY_URL unless the caller already provides one) and
#    hub_api/tests/live/live_hub.py (Postgres migrated to Alembic head, hub-api routes, chart-rendered
#    machine-JWT identities, real TwitchAdapter over an in-process fake Twitch) and waits for its manifest;
# 2. runs core/svc_ingest/tests/twitch_sessions_live_hub.rs (`#[ignore]`d tests) against it;
# 3. FAILS unless (a) every ignored test ran and passed (zero run = fail) and (b) the run emitted
#    >=1 log record, >=1 metric data point, >=1 histogram observation and exported
#    svc_ingest_twitch_sessions_live (testing.md telemetry gate).
# Counts are always printed. bash 3.2 compatible. Needs docker, helm, cargo and a python with the
# hub-api requirements (HUB_PYTHON, default python3).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HUB_PYTHON="${HUB_PYTHON:-python3}"
# Own target dir: the shared CARGO_TARGET_DIR is contended (serialises on the cargo build lock).
export CARGO_TARGET_DIR="${TWITCH_LIVE_TARGET:-${TMPDIR:-/tmp}/twitch-sessions-live-target}"

for tool in docker helm cargo "$HUB_PYTHON"; do
  command -v "$tool" >/dev/null 2>&1 || { echo "FAIL: required tool '$tool' not on PATH" >&2; exit 3; }
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/twitch-sessions-live.XXXXXX")"
MANIFEST="$WORK/manifest.json"
HUB_LOG="$WORK/hub.log"
TEST_LOG="$WORK/test.log"
HUB_PID=""
VALKEY_NAME=""

cleanup() {
  if [ -n "$HUB_PID" ] && kill -0 "$HUB_PID" 2>/dev/null; then
    kill -TERM "$HUB_PID" 2>/dev/null || true
    wait "$HUB_PID" 2>/dev/null || true   # teardown only; the gates below already decided pass/fail
  fi
  if [ -n "$VALKEY_NAME" ]; then
    docker rm -f "$VALKEY_NAME" >/dev/null 2>&1 || true   # teardown only
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

if [ -z "${VALKEY_URL:-}" ]; then
  VALKEY_NAME="twitch-live-valkey-$$"
  VALKEY_IMAGE="valkey/valkey:8-bookworm@sha256:fea8b3e67b15729d4bb70589eb03367bab9ad1ee89c876f54327fc7c6e618571"
  docker run -d --rm --name "$VALKEY_NAME" -p 127.0.0.1::6379 "$VALKEY_IMAGE" >/dev/null
  vport="$(docker port "$VALKEY_NAME" 6379/tcp | head -1 | sed 's/.*://')"
  [ -n "$vport" ] || { echo "FAIL: could not resolve valkey port" >&2; exit 1; }
  waited=0
  until docker exec "$VALKEY_NAME" valkey-cli ping 2>/dev/null | grep -q PONG; do
    [ "$waited" -lt 30 ] || { echo "FAIL: valkey not ready after ${waited}s" >&2; exit 1; }
    sleep 1; waited=$((waited + 1))
  done
  export VALKEY_URL="redis://127.0.0.1:${vport}"
fi
echo "valkey ready"

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

cd "$ROOT/core/svc_ingest"
expected="$(cargo test --locked --test twitch_sessions_live_hub -- --ignored --list 2>/dev/null | grep -c ': test$' || true)"
if [ "$expected" -lt 1 ]; then
  echo "FAIL: no #[ignore]d live tests found (expected >= 1)" >&2
  exit 1
fi

set +e
LIVE_HUB_MANIFEST="$MANIFEST" cargo test --locked --test twitch_sessions_live_hub -- \
  --ignored --test-threads=1 --nocapture 2>&1 | tee "$TEST_LOG"
test_status=${PIPESTATUS[0]}
set -e
if [ "$test_status" -ne 0 ]; then
  echo "FAIL: live tests failed (exit $test_status)" >&2
  exit "$test_status"
fi

passed="$(sed -n 's/^test result: ok\. \([0-9][0-9]*\) passed.*/\1/p' "$TEST_LOG" | tail -1)"
passed="${passed:-0}"
logs=0; metrics=0; hists=0; live=0; examined=0
while read -r l m h s; do
  logs=$((logs + l)); metrics=$((metrics + m)); hists=$((hists + h)); live=$((live + s))
done < <(sed -n 's/^.*LIVE_TELEMETRY test=[a-z]* logs=\([0-9]*\) metrics=\([0-9]*\) histograms=\([0-9]*\) sessions_live_series=\([0-9]*\).*$/\1 \2 \3 \4/p' "$TEST_LOG")
while read -r n; do
  examined=$((examined + n))
done < <(sed -n 's/^.*irc_connections_examined=\([0-9]*\).*$/\1/p' "$TEST_LOG")

echo "LIVE SUMMARY: tests_passed=$passed tests_expected=$expected irc_connections_examined=$examined log_records=$logs metric_points=$metrics histograms=$hists sessions_live_series=$live"
[ "$passed" -ge 1 ] || { echo "FAIL: zero live tests ran" >&2; exit 1; }
[ "$passed" -eq "$expected" ] || { echo "FAIL: passed ($passed) != expected ($expected)" >&2; exit 1; }
[ "$examined" -ge 2 ] || { echo "FAIL: IRC connections examined ($examined) < 2" >&2; exit 1; }
[ "$logs" -ge 1 ] || { echo "FAIL: zero log records emitted" >&2; exit 1; }
[ "$metrics" -ge 1 ] || { echo "FAIL: zero metric data points emitted" >&2; exit 1; }
[ "$hists" -ge 1 ] || { echo "FAIL: zero histogram observations emitted" >&2; exit 1; }
[ "$live" -ge 1 ] || { echo "FAIL: svc_ingest_twitch_sessions_live never exported" >&2; exit 1; }
echo "PASS: svc-ingest twitch sessions live e2e"
