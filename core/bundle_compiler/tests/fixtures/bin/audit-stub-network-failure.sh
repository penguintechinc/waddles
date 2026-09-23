#!/usr/bin/env bash
# Simulates a dependency-audit tool (cargo-audit/pip-audit/npm audit)
# failing to fetch its advisory database over the network -- the exact
# real-world CI failure this crate observed with cargo-audit's RustSec
# advisory-db git clone. Produces no parseable stdout and a nonzero exit,
# matching that failure shape, so scan::sast's `dependencies_examined`
# (parsed from the lockfile/requirements file on disk, not from this
# tool's output) can be proven to stay correct regardless.
set -euo pipefail
echo "error: failed to fetch advisory database: could not resolve host" >&2
exit 1
