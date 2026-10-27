#!/usr/bin/env bash
# Pre-commit wrapper for scripts/ci/check-no-stubs.sh -- WARN-ONLY, never
# blocks a local commit.
#
# User decision (devops.md Branch Backups): the no-stubs gate enforces ONLY
# at the release/main merge boundary (see .github/workflows/pr-validation.yml
# no-stubs-gate job, FATAL to ci-gate) -- NOT on every local commit to a
# short-lived feature/fix/chore branch. A dev pushing WIP with a fresh TODO
# must never be blocked from backing up their work (devops.md: "an unpushed
# branch is unsaved work"). This script always exits 0; it only prints a
# heads-up so the TODO/silent-fallback doesn't come as a surprise when the
# PR actually targets release/*.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

"${REPO_ROOT}/scripts/ci/check-no-stubs.sh"
status=$?

if [ "${status}" -ne 0 ]; then
    echo "" >&2
    echo "WARNING (non-blocking): check-no-stubs found new stub marker(s) and/or silent-fallback pattern(s)." >&2
    echo "  This is NOT enforced on feature/fix/chore/docs branches -- commit proceeds." >&2
    echo "  It WILL be enforced (FATAL) once this change targets release/* or main -- see .ci/stub-allowlist.yml" >&2
    echo "  to deliberately defer a finding (requires a tracking issue), or fix it before opening that PR." >&2
    echo "" >&2
fi

exit 0
