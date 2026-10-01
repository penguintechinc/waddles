#!/usr/bin/env bash
# Verifies bundles/Dockerfile.core-bundles's two example-bundle artifacts --
# waddles.core.example.ping (rust-bundle-builder stage) and
# waddles.core.example.pyping (python-bundle-builder stage) -- are both byte-reproducible:
# two independent `--no-cache` builds of the FULL core-bundles image, each from a clean
# docker build cache, must produce the exact same sha256 for ping.wasm AND pyping.wasm.
#
# Why this exists: the core-bundle-seeder Job got a 409 digest_conflict against the alpha DB
# because the Rust build was not reproducible -- three different sha256 digests existed for
# the same nominal waddles.core.example.ping@1.0.0 artifact across a manual onboarding, a live
# pipeline env, and a CI-built image. bundles/Dockerfile.core-bundles pins
# SOURCE_DATE_EPOCH/CARGO_INCREMENTAL/RUSTFLAGS remap-path-prefix/codegen-units/strip in the
# rust-bundle-builder stage, and PYTHONHASHSEED/SOURCE_DATE_EPOCH/TZ/LC_ALL in the
# python-bundle-builder stage (see that file's own comment blocks for what each pin closes).
#
# This script builds the FULL final image (not just one stage) so it also exercises the
# hub-deps-builder stage and the final COPY layout exactly as the seeder image is actually
# shipped -- this is the CI regression gate (.github/workflows/core-bundles-reproducible.yml),
# run on any change under bundles/, wit/, sdk/waddle-sdk-rs/, sdk/waddle-sdk/. A fix nobody
# re-verifies on every change is not actually fixed (critical-rules.md Verification Integrity:
# a check must prove it actually ran).
#
# Bash 3.2 compatible (general.md) -- no associative arrays, no mapfile.
#
# Usage: scripts/verify-core-bundles-reproducible.sh   (make verify-core-bundles-reproducible)

set -euo pipefail
cd "$(dirname "$0")/.."

DOCKERFILE="bundles/Dockerfile.core-bundles"

if ! command -v docker >/dev/null 2>&1; then
  echo "verify-core-bundles-reproducible: FAIL -- docker is required and not on PATH" >&2
  exit 1
fi

if [ ! -f "$DOCKERFILE" ]; then
  echo "verify-core-bundles-reproducible: FAIL -- expected file missing: $DOCKERFILE" >&2
  exit 1
fi

tag1="waddles/core-bundles-repro-check:build1-$$"
tag2="waddles/core-bundles-repro-check:build2-$$"
cleanup() {
  docker rmi "$tag1" "$tag2" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "verify-core-bundles-reproducible: build 1/2 (--no-cache, full image)"
docker build --no-cache -f "$DOCKERFILE" -t "$tag1" . >/dev/null

echo "verify-core-bundles-reproducible: build 2/2 (--no-cache, full image)"
docker build --no-cache -f "$DOCKERFILE" -t "$tag2" . >/dev/null

checks_run=0
failed=0

# Args: <label> <container-relative-path>
check_artifact() {
  label="$1"
  path="$2"
  hash1=$(docker run --rm --entrypoint sha256sum "$tag1" "$path" | awk '{print $1}')
  hash2=$(docker run --rm --entrypoint sha256sum "$tag2" "$path" | awk '{print $1}')
  checks_run=$((checks_run + 1))

  if [ -z "$hash1" ] || [ -z "$hash2" ]; then
    echo "verify-core-bundles-reproducible: FAIL -- could not compute a sha256 for ${label} (${path}) in one or both builds" >&2
    failed=1
    return
  fi

  echo "verify-core-bundles-reproducible: ${label} build1 sha256=${hash1}"
  echo "verify-core-bundles-reproducible: ${label} build2 sha256=${hash2}"

  if [ "$hash1" != "$hash2" ]; then
    echo "verify-core-bundles-reproducible: FAIL -- two independent builds of ${label} (${path}) produced different digests (${hash1} != ${hash2}); the build is not reproducible" >&2
    failed=1
  fi
}

check_artifact "waddles.core.example.ping" "/core-bundles/ping.wasm"
check_artifact "waddles.core.example.pyping" "/core-bundles/pyping.wasm"

if [ "$failed" -ne 0 ]; then
  echo "verify-core-bundles-reproducible: FAIL -- checks_run=${checks_run}" >&2
  exit 1
fi

echo "verify-core-bundles-reproducible: PASS -- two independent --no-cache builds are byte-identical for both artifacts (checks_run=${checks_run})"
exit 0
