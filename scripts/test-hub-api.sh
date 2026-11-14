#!/usr/bin/env bash
# Runs the hub_api pytest suite in a dedicated, hash-pinned venv.
#
# Exists because every agent/dev was rebuilding this environment by hand:
# builds (or refreshes, when any requirements file changes) a Python 3.13 venv
# with hub_api's dev deps (incl. penguin-dal) + libs/flask_core, regenerates
# the internal gRPC stubs only if one is missing, then runs pytest from
# hub_api/ with PYTHONPATH=grpc_internal/pb:../alembic/tests.
#
# Usage: scripts/test-hub-api.sh [pytest args...]
#   make test-hub-api PYTEST_ARGS="tests/test_config.py -x"
# Env:   HUB_API_VENV (default <repo>/.venv-hub-api), PYTHON (default python3.13)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="${HUB_API_VENV:-$REPO_ROOT/.venv-hub-api}"
PYTHON="${PYTHON:-python3.13}"

command -v "$PYTHON" >/dev/null 2>&1 || { echo "test-hub-api: $PYTHON not found (hub_api needs 3.13)" >&2; exit 1; }

REQ_FILES=(
  "$REPO_ROOT/hub_api/requirements-dev.txt"
  "$REPO_ROOT/libs/flask_core/requirements.txt"
  "$REPO_ROOT/scripts/requirements-protoc.txt"
)
for f in "${REQ_FILES[@]}"; do
  [ -f "$f" ] || { echo "test-hub-api: missing $f" >&2; exit 1; }
done

# Stamp = hash of every requirements file + flask_core's packaging metadata;
# a mismatch forces a full reinstall so the venv never silently drifts.
STAMP_WANT="$(cat "${REQ_FILES[@]}" "$REPO_ROOT/libs/flask_core/setup.py" | sha256sum | cut -d' ' -f1)"
STAMP_FILE="$VENV/.hub-api-stamp"
if [ ! -x "$VENV/bin/python" ] || [ "$(cat "$STAMP_FILE" 2>/dev/null || true)" != "$STAMP_WANT" ]; then
  echo "test-hub-api: building venv at $VENV"
  rm -rf "$VENV"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
  for f in "${REQ_FILES[@]}"; do
    "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --require-hashes -r "$f"
  done
  # flask_core's deps are already hash-locked above; install the package itself only.
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --no-deps -e "$REPO_ROOT/libs/flask_core"
  echo "$STAMP_WANT" > "$STAMP_FILE"
fi

# Regenerate stubs only when a .proto has no checked-in *_pb2.py counterpart.
PB_DIR="$REPO_ROOT/hub_api/grpc_internal/pb/waddles/hub/internal/v1"
missing=0
for proto in "$REPO_ROOT"/proto/waddles/hub/internal/v1/*.proto; do
  [ -f "$PB_DIR/$(basename "$proto" .proto)_pb2.py" ] || missing=1
done
if [ "$missing" -eq 1 ]; then
  echo "test-hub-api: generating gRPC stubs"
  PATH="$VENV/bin:$PATH" bash "$SCRIPT_DIR/compile_protos_v1.sh"
fi

cd "$REPO_ROOT/hub_api"
export PYTHONPATH="grpc_internal/pb:../alembic/tests${PYTHONPATH:+:$PYTHONPATH}"
exec "$VENV/bin/python" -m pytest "$@"
