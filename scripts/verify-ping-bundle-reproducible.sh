#!/usr/bin/env bash
# Verifies bundles/rust/ping's WASI 0.2 component build (bundles/Dockerfile.core-bundles's
# `rust-bundle-builder` stage) is byte-reproducible -- two independent `--no-cache` builds,
# each in a fresh container, must produce the exact same sha256 for
# waddle_bundle_ping_rust.wasm.
#
# Why this exists: the core-bundle-seeder Job got a 409 digest_conflict against the alpha DB
# because this build was NOT reproducible -- three different sha256 digests existed for the
# same nominal waddles.core.example.ping@1.0.0 artifact across a manual onboarding, a live
# pipeline env, and the CI-built image. bundles/Dockerfile.core-bundles now pins
# SOURCE_DATE_EPOCH/CARGO_INCREMENTAL/RUSTFLAGS remap-path-prefix/codegen-units/strip (see its
# own comment block) specifically to close that gap; this script is the proof + regression
# gate -- a fix nobody re-verifies on every change is not actually fixed
# (critical-rules.md Verification Integrity: a check must prove it actually ran).
#
# Bash 3.2 compatible (general.md) -- no associative arrays, no mapfile.
#
# Usage: scripts/verify-ping-bundle-reproducible.sh   (make verify-ping-bundle-reproducible)

set -euo pipefail
cd "$(dirname "$0")/.."

DOCKERFILE="bundles/Dockerfile.core-bundles"
TARGET_STAGE="rust-bundle-builder"
WASM_PATH="/repo/bundles/rust/ping/target/wasm32-wasip2/release/waddle_bundle_ping_rust.wasm"

if ! command -v docker >/dev/null 2>&1; then
  echo "verify-ping-bundle-reproducible: FAIL -- docker is required and not on PATH" >&2
  exit 1
fi

if [ ! -f "$DOCKERFILE" ]; then
  echo "verify-ping-bundle-reproducible: FAIL -- expected file missing: $DOCKERFILE" >&2
  exit 1
fi

tag1="waddles/ping-bundle-repro-check:build1-$$"
tag2="waddles/ping-bundle-repro-check:build2-$$"
cleanup() {
  docker rmi "$tag1" "$tag2" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "verify-ping-bundle-reproducible: build 1/2 (--no-cache, target=${TARGET_STAGE})"
docker build --no-cache --target "$TARGET_STAGE" -f "$DOCKERFILE" -t "$tag1" . >/dev/null

echo "verify-ping-bundle-reproducible: build 2/2 (--no-cache, target=${TARGET_STAGE})"
docker build --no-cache --target "$TARGET_STAGE" -f "$DOCKERFILE" -t "$tag2" . >/dev/null

hash1=$(docker run --rm "$tag1" sha256sum "$WASM_PATH" | awk '{print $1}')
hash2=$(docker run --rm "$tag2" sha256sum "$WASM_PATH" | awk '{print $1}')

if [ -z "$hash1" ] || [ -z "$hash2" ]; then
  echo "verify-ping-bundle-reproducible: FAIL -- could not compute a sha256 for $WASM_PATH in one or both builds" >&2
  exit 1
fi

echo "verify-ping-bundle-reproducible: build1 sha256=${hash1}"
echo "verify-ping-bundle-reproducible: build2 sha256=${hash2}"

if [ "$hash1" != "$hash2" ]; then
  echo "verify-ping-bundle-reproducible: FAIL -- two independent builds of waddle_bundle_ping_rust.wasm produced different digests (${hash1} != ${hash2}); the build is not reproducible" >&2
  exit 1
fi

echo "verify-ping-bundle-reproducible: PASS -- two independent --no-cache builds are byte-identical (sha256=${hash1}, checks_run=2)"
exit 0
