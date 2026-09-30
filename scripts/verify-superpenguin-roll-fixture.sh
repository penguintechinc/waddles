#!/usr/bin/env bash
# Rebuilds bundles/csharp/superpenguin-roll from source (pinned, rootless
# Dockerfile) and reports the resulting component's size and sha256.
# Mirrors scripts/verify-csping-fixture.sh's build/extract mechanics; unlike
# that script, this one does NOT assert byte-identity against a committed
# fixture -- superpenguin_roll.wasm is intentionally NOT committed to
# core/bundle_executor/tests/fixtures/ (see that directory's own note and
# sdk/waddle-sdk-cs/README.md "Building superpenguin-roll" for why: a
# NativeAOT-LLVM component this size is routinely at or above the 5MB
# fixture-size threshold this repo uses for committed wasm blobs). Run this
# to produce the artifact locally, or as the CI build step feeding
# `make verify-bundle-conformance-superpenguin-roll`.
#
# Bash 3.2 compatible (general.md) -- no associative arrays, no mapfile.
#
# Usage: scripts/verify-superpenguin-roll-fixture.sh [output-dir]
#   (make build-superpenguin-roll-bundle)

set -euo pipefail
cd "$(dirname "$0")/.."

DOCKERFILE="bundles/csharp/superpenguin-roll/Dockerfile"
OUT_DIR="${1:-/tmp/superpenguin-roll-out}"

if ! command -v docker >/dev/null 2>&1; then
  echo "verify-superpenguin-roll-fixture: FAIL -- docker is required and not on PATH" >&2
  exit 1
fi

if [ ! -f "$DOCKERFILE" ]; then
  echo "verify-superpenguin-roll-fixture: FAIL -- expected file missing: $DOCKERFILE" >&2
  exit 1
fi

tmp_tag="waddles/bundle-superpenguin-roll-build:$$"
cleanup() { docker rmi "$tmp_tag" >/dev/null 2>&1 || true; }
trap cleanup EXIT

mkdir -p "$OUT_DIR"

echo "verify-superpenguin-roll-fixture: building bundles/csharp/superpenguin-roll from source (downloads/verifies a pinned WASI SDK + NuGet packages, may take 1-2 minutes)"
docker build -f "$DOCKERFILE" -t "$tmp_tag" . >/dev/null

echo "verify-superpenguin-roll-fixture: extracting built superpenguin_roll.wasm"
docker run --rm --user "$(id -u):$(id -g)" -v "${OUT_DIR}:/out" "$tmp_tag" >/dev/null

wasm_path="${OUT_DIR}/superpenguin_roll.wasm"
if [ ! -f "$wasm_path" ]; then
  echo "verify-superpenguin-roll-fixture: FAIL -- build produced no superpenguin_roll.wasm" >&2
  exit 1
fi

size_bytes=$(wc -c < "$wasm_path" | tr -d ' ')
sha256=$(sha256sum "$wasm_path" | awk '{print $1}')

echo "verify-superpenguin-roll-fixture: PASS -- ${wasm_path} (${size_bytes} bytes, sha256:${sha256})"
exit 0
