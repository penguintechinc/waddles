#!/usr/bin/env bash
# Verifies core/bundle_executor/tests/fixtures/csping.wasm (the C#-toolchain
# feasibility spike fixture, spec docs/superpowers/specs/2026-09-14-rust-
# data-plane-design.md S18 R13 / D35) is exactly what its own source
# (bundles/csharp/csping) reproducibly builds -- makes the committed binary
# blob auditable instead of a trust-me artifact (critical-rules.md
# Verification Integrity: a check must prove it actually ran, not just
# report "no findings").
#
# Two checks, both must pass:
#   1. The COMMITTED csping.wasm matches its own recorded
#      csping.wasm.sha256 (catches an edit to the binary without updating
#      the hash file, or vice versa).
#   2. A FRESH build from bundles/csharp/csping (via its pinned,
#      checksum-verified, rootless Dockerfile) produces byte-identical
#      output -- catches an edit to the C# source, csproj, or Dockerfile
#      that silently changed the artifact without anyone re-committing it.
#
# Bash 3.2 compatible (general.md) -- no associative arrays, no mapfile.
#
# Usage: scripts/verify-csping-fixture.sh   (make verify-csping-fixture)

set -euo pipefail
cd "$(dirname "$0")/.."

FIXTURE_DIR="core/bundle_executor/tests/fixtures"
FIXTURE_WASM="${FIXTURE_DIR}/csping.wasm"
FIXTURE_SHA="${FIXTURE_DIR}/csping.wasm.sha256"
DOCKERFILE="bundles/csharp/csping/Dockerfile"

if ! command -v docker >/dev/null 2>&1; then
  echo "verify-csping-fixture: FAIL -- docker is required and not on PATH" >&2
  exit 1
fi

for f in "$FIXTURE_WASM" "$FIXTURE_SHA" "$DOCKERFILE"; do
  if [ ! -f "$f" ]; then
    echo "verify-csping-fixture: FAIL -- expected file missing: $f" >&2
    exit 1
  fi
done

echo "verify-csping-fixture: checking committed csping.wasm against $FIXTURE_SHA"
if ! (cd "$FIXTURE_DIR" && sha256sum -c csping.wasm.sha256); then
  echo "verify-csping-fixture: FAIL -- committed csping.wasm does not match csping.wasm.sha256" >&2
  exit 1
fi

recorded_hash=$(awk '{print $1}' "$FIXTURE_SHA")
if [ -z "$recorded_hash" ]; then
  echo "verify-csping-fixture: FAIL -- could not read a hash out of $FIXTURE_SHA" >&2
  exit 1
fi

tmp_tag="waddles/bundle-csping-verify:$$"
tmp_out=$(mktemp -d)
cleanup() {
  rm -rf "$tmp_out"
  docker rmi "$tmp_tag" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "verify-csping-fixture: rebuilding bundles/csharp/csping from source (this downloads/verifies a pinned WASI SDK + NuGet packages, may take 1-2 minutes)"
docker build -f "$DOCKERFILE" -t "$tmp_tag" . >/dev/null

echo "verify-csping-fixture: extracting freshly-built csping.wasm"
docker run --rm --user "$(id -u):$(id -g)" -v "${tmp_out}:/out" "$tmp_tag" >/dev/null

if [ ! -f "${tmp_out}/csping.wasm" ]; then
  echo "verify-csping-fixture: FAIL -- fresh build produced no csping.wasm" >&2
  exit 1
fi

fresh_hash=$(sha256sum "${tmp_out}/csping.wasm" | awk '{print $1}')

echo "verify-csping-fixture: recorded=${recorded_hash}"
echo "verify-csping-fixture: fresh   =${fresh_hash}"

if [ "$fresh_hash" != "$recorded_hash" ]; then
  echo "verify-csping-fixture: FAIL -- a fresh build from bundles/csharp/csping no longer reproduces the committed csping.wasm (source/csproj/Dockerfile changed without re-committing the fixture + csping.wasm.sha256)" >&2
  exit 1
fi

echo "verify-csping-fixture: PASS -- committed fixture matches its recorded hash AND a fresh rebuild from source (checks_run=2)"
exit 0
