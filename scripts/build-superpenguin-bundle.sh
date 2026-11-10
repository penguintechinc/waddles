#!/usr/bin/env bash
# Builds bundles/csharp/superpenguin-<name> to a real .wasm component through its pinned, rootless Dockerfile (NativeAOT-LLVM +
# sha256-pinned WASI SDK) and reports its size/sha256. Same mechanics as scripts/verify-superpenguin-roll-fixture.sh, parameterized
# over the bundle name (slots | gamble | defuse | ...). The component is intentionally NOT committed (NativeAOT-LLVM output is not
# proven byte-reproducible). Then inspects the component's exports with wasm-tools when it is installed.
#
# Usage: scripts/build-superpenguin-bundle.sh <name> [output-dir]   (make build-superpenguin-<name>-bundle)
set -euo pipefail
cd "$(dirname "$0")/.."

name="${1:?usage: build-superpenguin-bundle.sh <slots|gamble|defuse|...> [output-dir]}"
case "$name" in *[!a-z0-9-]*|"") echo "build-superpenguin-$name: FAIL -- bad bundle name" >&2; exit 1 ;; esac
label="build-superpenguin-${name}"
asm="superpenguin_${name//-/_}"
dockerfile="bundles/csharp/superpenguin-${name}/Dockerfile"
out_dir="${2:-/tmp/superpenguin-${name}-out}"

command -v wasm-tools >/dev/null 2>&1 || { echo "${label}: FAIL -- wasm-tools is required" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "${label}: FAIL -- docker is required and not on PATH" >&2; exit 1; }
[ -f "$dockerfile" ] || { echo "${label}: FAIL -- expected file missing: $dockerfile" >&2; exit 1; }

tmp_tag="waddles/bundle-superpenguin-${name}-build:$$"
trap 'docker rmi "$tmp_tag" >/dev/null 2>&1 || true' EXIT
mkdir -p "$out_dir"

echo "${label}: building ${dockerfile} (pinned WASI SDK + NuGet packages, may take 1-3 minutes)"
docker build -f "$dockerfile" -t "$tmp_tag" . >/dev/null
docker run --rm --user "$(id -u):$(id -g)" -v "${out_dir}:/out" "$tmp_tag" >/dev/null

wasm="${out_dir}/${asm}.wasm"
[ -s "$wasm" ] || { echo "${label}: FAIL -- build produced no ${asm}.wasm" >&2; exit 1; }
echo "${label}: PASS -- ${wasm} ($(wc -c < "$wasm" | tr -d ' ') bytes, sha256:$(sha256sum "$wasm" | awk '{print $1}'))"

wasm-tools validate --features component-model "$wasm"
if command -v wasm-tools >/dev/null 2>&1; then
  exports=$(wasm-tools component wit "$wasm" | grep -E '^\s*export ' || true)
  echo "$exports"
  for e in process-stage action-stage streaming-lifecycle; do
    echo "$exports" | grep -q "$e" || { echo "${label}: FAIL -- component does not export ${e}" >&2; exit 1; }
  done
fi
