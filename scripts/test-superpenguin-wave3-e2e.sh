#!/usr/bin/env bash
# Build the ten wave-3 C# utility ports to real WASM components, then drive each through the production bundle-executor
# (core/bundle_executor/tests/csharp_superpenguin_wave3_e2e.rs). Same offline-docker mechanics as test-superpenguin-ports-e2e.sh.
# An optional first argument is a directory of already-built components (skips the builds).
set -euo pipefail
cd "$(dirname "$0")/.."
repo_root="$(pwd)"
names="lastseen deathcounter dailycounter quotes raidtracker top alias timers weather viewers"
out_dir="${1:-}"
if [ -z "$out_dir" ]; then
  out_dir="$(mktemp -d /tmp/waddles-superpenguin-wave3-e2e-XXXXXX)"
  trap 'rm -rf "$out_dir"' EXIT
  for name in $names; do
    bash scripts/build-superpenguin-bundle.sh "$name" "$out_dir"
  done
fi
cargo_cache="${CARGO_HOME:-${HOME}/.cargo}"
rustup_cache="${RUSTUP_HOME:-${HOME}/.rustup}"
if [ ! -d "$cargo_cache" ] || [ ! -d "$rustup_cache" ]; then
  echo "wave-3 e2e: Rust dependency/toolchain caches are required for the offline Docker test" >&2
  exit 1
fi
log="$(mktemp /tmp/waddles-superpenguin-wave3-log-XXXXXX)"
trap 'rm -f "$log"' EXIT
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$repo_root":/repo -v "$out_dir":/artifact:ro \
  -v "$cargo_cache":/cargo:ro -v "$rustup_cache":/rustup:ro \
  -e CARGO_HOME=/cargo -e RUSTUP_HOME=/rustup -e CARGO_BUILD_JOBS=2 \
  -e CARGO_TARGET_DIR=/repo/core/bundle_executor/target \
  -e SUPERPENGUIN_WAVE3_WASM_DIR=/artifact \
  -e PATH=/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  -w /repo/core/bundle_executor \
  rust:1.97-slim-bookworm@sha256:2775a09d208ff0d7c1f50490c45b62db929e87ba1dcbc3f2132ac71a704bcdd3 \
  cargo test --locked --offline --test csharp_superpenguin_wave3_e2e -- --ignored --nocapture \
  2>&1 | tee "$log"
grep -q 'test result: ok. 1 passed; 0 failed; 0 ignored' "$log"
for name in $names; do
  grep -q "C# wave-3 real-executor matrix: $name PASS" "$log"
done
echo 'wave-3 e2e: PASS (10 real components through the real executor)'
