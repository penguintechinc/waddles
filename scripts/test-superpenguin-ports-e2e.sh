#!/usr/bin/env bash
# Build every C# port, then exercise the real WASM components in the production executor.
set -euo pipefail
cd "$(dirname "$0")/.."
repo_root="$(pwd)"
out_dir="$(mktemp -d /tmp/waddles-superpenguin-e2e-XXXXXX)"
trap 'rm -rf "$out_dir"' EXIT
cargo_cache="${CARGO_HOME:-${HOME}/.cargo}"
rustup_cache="${RUSTUP_HOME:-${HOME}/.rustup}"
if [ ! -d "$cargo_cache" ] || [ ! -d "$rustup_cache" ]; then
  echo "C# port e2e: Rust dependency/toolchain caches are required for the offline Docker test" >&2
  exit 1
fi
for game in roll slots gamble defuse duel first roulette ffa heist steal; do
  bash scripts/build-superpenguin-bundle.sh "$game" "$out_dir"
done
log="$out_dir/executor.log"
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$repo_root":/repo -v "$out_dir":/artifact:ro \
  -v "$cargo_cache":/cargo:ro -v "$rustup_cache":/rustup:ro \
  -e CARGO_HOME=/cargo -e RUSTUP_HOME=/rustup -e CARGO_BUILD_JOBS=2 \
  -e CARGO_TARGET_DIR=/repo/core/bundle_executor/target \
  -e SUPERPENGUIN_ROLL_WASM=/artifact/superpenguin_roll.wasm \
  -e SUPERPENGUIN_WASM_DIR=/artifact \
  -e PATH=/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  -w /repo/core/bundle_executor \
  rust:1.97-slim-bookworm@sha256:2775a09d208ff0d7c1f50490c45b62db929e87ba1dcbc3f2132ac71a704bcdd3 \
  cargo test --locked --offline --test csharp_superpenguin_roll_e2e -- --ignored --nocapture \
  2>&1 | tee "$log"
grep -q 'test result: ok. 2 passed; 0 failed; 0 ignored' "$log"
for game in slots gamble defuse first roulette duel ffa heist steal; do
  grep -q "C# real-executor matrix: $game PASS" "$log"
done
echo 'C# port e2e: PASS (10 real components, nonzero flag-on/replay coverage)'
