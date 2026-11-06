#!/usr/bin/env bash
# Runs core/bundle_executor/tests/flag_on_command_e2e.rs -- the real-path
# "flag ON -> command replies, flag OFF -> no reply" regression test that
# would have caught the entire 2026-10-04 alpha command-bundle saga (stubbed
# flags capability, missing WIT %flags import, stale wasm, env-baseline
# wiring), all in one shot.
#
# Requires `docker` (to freshly compile bundles/python/eightball to wasm via
# the exact componentize-py invocation bundles/Dockerfile.core-bundles's
# python-bundles-builder stage uses) and a Rust 1.97.x toolchain. Gated
# behind the `e2e-main-tier` cargo feature: runs in CI only for PRs into main /
# push to main (.github/workflows/rust-bundle-executor-e2e.yml), not in the
# release-PR coverage job. This script is the local/manual entry point.
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
cargo test --locked --features e2e-main-tier --test flag_on_command_e2e -- --nocapture
