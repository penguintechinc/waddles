#!/bin/bash
# Regenerates Python stubs for proto/waddles/hub/internal/v1/*.proto.
#
# Rust does NOT use this script -- core/hub_client/build.rs runs
# tonic-build directly against the same .proto files at `cargo build`
# time, so the two language toolchains never drift out of sync with each
# other, only (independently) with the checked-in .proto source of
# truth. buf is used for `buf lint`/`buf breaking` only (see
# proto/buf.yaml) -- generation stays on grpc_tools.protoc, matching the
# existing pattern in core/identity_core_module/compile_protos.sh, since
# wiring a buf remote/local grpc_python plugin adds a second toolchain
# for zero benefit over the one already used elsewhere in this repo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROTO_DIR="$REPO_ROOT/proto"
OUT_DIR="$REPO_ROOT/hub_api/grpc_internal/pb"

command -v protoc >/dev/null 2>&1 || { echo "protoc not found" >&2; exit 1; }
python3 -c "import grpc_tools" 2>/dev/null || pip install --require-hashes -r /dev/null 2>/dev/null || true

buf lint --path "$PROTO_DIR" 2>/dev/null || (cd "$PROTO_DIR" && buf lint)

mkdir -p "$OUT_DIR"
python3 -m grpc_tools.protoc \
  -I "$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  --pyi_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$PROTO_DIR/waddles/hub/internal/v1/identity.proto" \
  "$PROTO_DIR/waddles/hub/internal/v1/key.proto"

# Ensure every generated package directory is importable.
find "$OUT_DIR/waddles" -type d -exec sh -c 'touch "$1/__init__.py"' _ {} \;

echo "Generated stubs under $OUT_DIR"
