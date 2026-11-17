#!/usr/bin/env bash
# Proto gate for proto/waddles/**: buf lint, buf breaking against the release branch, and a
# stub-drift check proving the committed hub-api Python stubs match the .proto sources.
#
# Fail-loud by design (critical-rules.md Verification Integrity): zero protos examined, an
# unresolvable breaking-change baseline, or any drift is a hard failure, never a silent pass.
# Counts are always printed.
#
# Env:
#   PROTO_BREAKING_AGAINST  git ref to compare against (default origin/release/v3.0.X); must exist
#                           locally -- CI must `git fetch` it first (no silent skip when absent).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROTO_DIR="$REPO_ROOT/proto"
AGAINST="${PROTO_BREAKING_AGAINST:-origin/release/v3.0.X}"

command -v buf >/dev/null 2>&1 || { echo "check-proto-gate: buf not found" >&2; exit 1; }

n_protos="$(find "$PROTO_DIR/waddles" -name '*.proto' | wc -l | tr -d ' ')"
if [ "$n_protos" -lt 1 ]; then
  echo "check-proto-gate: FAIL - 0 .proto files under $PROTO_DIR/waddles (wrong root?)" >&2
  exit 1
fi
echo "check-proto-gate: examining $n_protos .proto files"

echo "== buf lint"
(cd "$PROTO_DIR" && buf lint)
echo "buf lint: OK ($n_protos files)"

echo "== buf breaking against $AGAINST"
if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "$AGAINST^{commit}" >/dev/null; then
  echo "check-proto-gate: FAIL - baseline ref '$AGAINST' not found; fetch it (git fetch origin release/v3.0.X)" >&2
  exit 1
fi
# A proto package that does not exist in the baseline (a brand-new service) is not a breaking
# change; buf treats missing files in the baseline as additions.
(cd "$PROTO_DIR" && buf breaking --against "$REPO_ROOT/.git#ref=$AGAINST,subdir=proto")
echo "buf breaking: OK (vs $AGAINST)"

echo "== stub drift"
python3 "$REPO_ROOT/scripts/ci/check_proto_stub_drift.py"
