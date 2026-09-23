#!/usr/bin/env bash
# Stand-in for `cargo audit --file <lockfile> --json` reporting zero
# advisories -- used by the "examines a real lockfile" happy-path test so
# it never depends on a live network fetch of the RustSec advisory
# database (CI runs with no network access to that database; a real
# `cargo audit` invocation there fails exactly like
# `audit-stub-network-failure.sh` simulates, which now correctly blocks
# the build per the fail-closed fix -- so this test must not exercise the
# real tool at all). Mirrors real cargo-audit's JSON shape exactly (see
# scan::sast's `vulnerabilities.list` parsing).
set -euo pipefail
printf '{"vulnerabilities": {"found": false, "list": []}}'
