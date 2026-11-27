#!/usr/bin/env bash
# Gate: every Rust bundle / core crate / SDK Cargo.lock agrees with its Cargo.toml
# package versions (incl. local path deps), so `cargo ... --locked` never refuses to
# build at deploy time. Logic + counts in check-bundle-lockfiles.py; zero examined fails.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${here}/check-bundle-lockfiles.py" --repo-root "${1:-${here}/../..}"
