#!/usr/bin/env bash
# Installs every Python dependency `make test-unit` / tests/k8s/alpha/05-unit-tests.sh
# needs to collect and run the unit suite (hub_api + libs/flask_core +
# libs/waddle_transports).
#
# Each subproject ships its own requirements.txt with its own pins -- most
# are hash-pinned (--require-hashes) and some legitimately disagree on
# transitive pins (e.g. hub_api pins pydal==20260520.0 while svc_action pins
# pydal==20241204.1). pip's --require-hashes rejects mixing hashed and
# unhashed/editable specs in the same invocation, so each requirements.txt
# is installed as its own separate `pip install` call, in order -- a later
# step's pin wins for shared deps, which matches what each subproject's own
# test suite is actually verified against. This is the single source of
# truth for the install sequence; .github/workflows/pr-validation.yml calls
# this same script so CI and local runs never drift.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "Required Python executable not found: $PYTHON_BIN" >&2
    exit 127
}

PIP=("$PYTHON_BIN" -m pip install --disable-pip-version-check)

# penguin-dal is a real runtime import of libs/flask_core/flask_core/
# bundle_runtime.py (`from penguin_dal import Row, Rows`, unconditional --
# not TYPE_CHECKING-guarded) as of the R52 App Bundle DB access work, but
# flask_core's own requirements.in/setup.py never declared it. Any module
# whose `app.py`/`config.py` does `from flask_core import (...)` --
# hub_api included -- transitively
# hits `flask_core/__init__.py`'s `from .bundle_runtime import (...)` and
# fails to even collect with `ModuleNotFoundError: No module named
# 'penguin_dal'` unless this is installed first. Pinned to the same
# `penguin-dal==0.4.0` every other consumer in this repo already uses
# (hub_api/requirements.in) so nothing here introduces a second,
# divergent pin.
echo "[install-unit-test-deps] penguin-dal (libs/flask_core's undeclared runtime import)"
"${PIP[@]}" "penguin-dal==0.4.0"

# pip rejects an editable requirement in any call that enables hash-checking
# ("editable requirement ... cannot be installed when requiring hashes"), so
# the hash-pinned deps and the editable package are separate calls.
# --no-deps on the editable install: every dependency was already installed
# hash-verified from the two requirements.txt files above it, so nothing
# unhashed is resolved from the index.
echo "[install-unit-test-deps] libs/flask_core (hash-pinned deps)"
"${PIP[@]}" --require-hashes -r libs/flask_core/requirements.txt
echo "[install-unit-test-deps] editable libs/flask_core"
"${PIP[@]}" --no-deps -e libs/flask_core

# libs/waddle_transports carries its own hash-pinned runtime deps; pip rejects an editable
# requirement in a hash-checked call, so the deps and the editable package are two calls.
echo "[install-unit-test-deps] libs/waddle_transports (hash-pinned deps)"
"${PIP[@]}" --require-hashes -r libs/waddle_transports/requirements.txt
echo "[install-unit-test-deps] editable libs/waddle_transports"
"${PIP[@]}" -e libs/waddle_transports

req="hub_api/requirements.txt"
if [ ! -f "$req" ]; then
    echo "[install-unit-test-deps] ERROR: $req not found" >&2
    exit 1
fi
echo "[install-unit-test-deps] hub_api"
"${PIP[@]}" --require-hashes -r "$req"

echo "[install-unit-test-deps] done"
