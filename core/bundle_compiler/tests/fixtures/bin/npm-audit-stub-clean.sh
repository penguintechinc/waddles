#!/usr/bin/env bash
# Stand-in for `npm audit --json` reporting zero high/critical advisories
# -- used by the "examines a real lockfile" happy-path test so it never
# depends on a live network call to the npm registry (CI runs with no
# network access; a real `npm audit` invocation there fails exactly like
# `audit-stub-network-failure.sh` simulates, which now correctly blocks
# the build per the fail-closed fix -- so this test must not exercise the
# real tool at all). Mirrors real npm's `auditReportVersion: 2` shape
# exactly, same as `npm-audit-stub-vulnerable.sh`.
set -euo pipefail
printf '{"metadata": {"vulnerabilities": {"high": 0, "critical": 0}, "dependencies": {"total": 1}}}'
