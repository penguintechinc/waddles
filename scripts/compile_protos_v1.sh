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
# Gencode must not outrun the pinned runtimes (protobuf 6.33.x, grpcio 1.83.x in
# libs/flask_core): a newer grpcio-tools emits stubs that raise VersionError at
# import. Install the hash-locked toolchain: pip install --require-hashes -r scripts/requirements-protoc.txt
PINNED_TOOLS="$(sed -n 's/^grpcio-tools==\([0-9.]*\).*/\1/p' "$SCRIPT_DIR/requirements-protoc.txt")"
ACTUAL_TOOLS="$(python3 -c 'from importlib.metadata import version; print(version("grpcio-tools"))' 2>/dev/null || true)"
[ -n "$PINNED_TOOLS" ] && [ "$ACTUAL_TOOLS" = "$PINNED_TOOLS" ] || {
  echo "grpcio-tools '$ACTUAL_TOOLS' != pinned '$PINNED_TOOLS' (scripts/requirements-protoc.txt)" >&2; exit 1; }

buf lint --path "$PROTO_DIR" 2>/dev/null || (cd "$PROTO_DIR" && buf lint)

mkdir -p "$OUT_DIR"
python3 -m grpc_tools.protoc \
  -I "$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  --pyi_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$PROTO_DIR"/waddles/hub/internal/v1/*.proto

# Ensure every generated package directory is importable.
find "$OUT_DIR/waddles" -type d -exec sh -c 'touch "$1/__init__.py"' _ {} \;

echo "Generated stubs under $OUT_DIR"
