#!/usr/bin/env bash
# Runs core/bundle_executor/tests/flag_on_command_e2e.rs -- the real-path
# "flag ON -> command replies, flag OFF -> no reply" regression test that
# would have caught the entire 2026-10-04 alpha command-bundle saga (stubbed
# flags capability, missing WIT %flags import, stale wasm, env-baseline
# wiring), all in one shot.
#
# Requires `docker` (to freshly compile bundles/python/eightball to wasm via
# the exact componentize-py invocation bundles/Dockerfile.core-bundles's
# python-bundles-builder stage uses) and a Rust 1.97.x toolchain. Already
# runs as part of this crate's own `cargo test` gate
# (.github/workflows/rust-bundle-executor.yml -> rust-crate-ci.yml, FATAL on
# every push/PR touching core/bundle_executor/**) -- this script is a
# standalone, directly-invocable entry point for local/manual runs.
#
# Usage: scripts/test-bundle-flag-on-command-e2e.sh   (make test-bundle-flag-on-command-e2e)

set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "test-bundle-flag-on-command-e2e: FAIL -- docker is required (builds the real eightball.wasm fixture fresh) and is not on PATH" >&2
  exit 1
fi

cd core/bundle_executor
echo "test-bundle-flag-on-command-e2e: running flag_on_command_e2e (real wasm + real flags host capability + Docker-ENV baseline)"
cargo test --locked --test flag_on_command_e2e -- --nocapture
