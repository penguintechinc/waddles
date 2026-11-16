#!/usr/bin/env bash
# Pre-push wrapper for scripts/ci/check-bundle-version-bump.py.
#
# The gate compares <base>...HEAD: a core bundle whose build inputs changed
# under an already-published catalog version is a deploy-time 409 digest_conflict
# (see the script's own module docstring). Locally we approximate the eventual
# PR base range. Base selection, in order:
#   1. @{push}        -- the branch's configured push target, when set
#   2. origin/release/v3.0.X -- the standard release base fallback
# A branch with no upstream and no local origin/release/v3.0.X ref must NOT
# error the push (devops.md Branch Backups: backing up WIP is never blocked) --
# we print a notice and exit 0. The real gate runs in pr-validation against the
# actual PR base regardless.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

FALLBACK_BASE="origin/release/v3.0.X"

base=""
if push_ref="$(git rev-parse --abbrev-ref --symbolic-full-name '@{push}' 2>/dev/null)" \
   && [ -n "${push_ref}" ] && git rev-parse --verify --quiet "${push_ref}^{commit}" >/dev/null; then
  base="${push_ref}"
elif git rev-parse --verify --quiet "${FALLBACK_BASE}^{commit}" >/dev/null; then
  base="${FALLBACK_BASE}"
fi

if [ -z "${base}" ]; then
  echo "bundle-version-bump: no @{push} upstream and no ${FALLBACK_BASE} ref -- skipping local check (enforced in pr-validation)."
  exit 0
fi

echo "bundle-version-bump: comparing ${base}...HEAD"
exec python3 scripts/ci/check-bundle-version-bump.py "${base}"
