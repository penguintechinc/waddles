#!/usr/bin/env bash
# Stand-in for `npm audit --json` reporting one high-severity advisory --
# exercises scan::sast's `dependency_vulnerability` blocking path without
# depending on a real, currently-vulnerable npm package (which would make
# this test flaky as advisories are published/fixed over time). Mirrors
# real npm's `auditReportVersion: 2` shape exactly (see the fix note next
# to scan::sast's `dependencies.total` parsing).
set -euo pipefail
printf '{"metadata": {"vulnerabilities": {"high": 1, "critical": 0}, "dependencies": {"total": 1}}}'
