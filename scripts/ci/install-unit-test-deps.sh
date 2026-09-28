#!/usr/bin/env bash
# Installs every Python dependency `make test-unit` / tests/k8s/alpha/05-unit-tests.sh
# needs to collect and run the full unit suite (legacy tests/unit +
# identity_core_module, community_module, hub_api, libs/* (flask_core +
# SCCEMBS module libraries), and every core/svc_* stage-runner container).
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
# identity_core_module and community_module both, at minimum -- transitively
# hits `flask_core/__init__.py`'s `from .bundle_runtime import (...)` and
# fails to even collect with `ModuleNotFoundError: No module named
# 'penguin_dal'` unless this is installed first. Pinned to the same
# `penguin-dal==0.4.0` every other consumer in this repo already uses
# (hub_api/requirements.in, core/svc_action/requirements.in,
# core/svc_process/requirements.in) so nothing here introduces a second,
# divergent pin.
echo "[install-unit-test-deps] penguin-dal (libs/flask_core's undeclared runtime import)"
"${PIP[@]}" "penguin-dal==0.4.0"

echo "[install-unit-test-deps] core/identity_core_module + editable libs/flask_core"
"${PIP[@]}" -r core/identity_core_module/requirements.txt -e libs/flask_core

echo "[install-unit-test-deps] core/community_module"
"${PIP[@]}" -r core/community_module/requirements.txt

# libs/waddle_transports is the first libs/* module carrying its own
# runtime dependencies beyond flask_core (websockets/aiosmtplib/httpx[http2]
# for the socket/email/http transports) -- most other libs/* modules (see
# the run_suite loop in tests/k8s/alpha/05-unit-tests.sh) have no
# requirements.txt of their own and are satisfied by flask_core alone. Unlike
# flask_core's combined line above, this requirements.txt IS hash-pinned
# (--generate-hashes) -- pip auto-enables hash-checking the moment any
# requirement in a call carries a hash, and then rejects every other spec
# in that same call lacking one (the unhashed local `-e` path), so the
# hash-pinned install and the editable package install must be two
# separate `pip install` calls, not combined like flask_core's.
echo "[install-unit-test-deps] libs/waddle_transports (hash-pinned deps)"
"${PIP[@]}" --require-hashes -r libs/waddle_transports/requirements.txt
echo "[install-unit-test-deps] editable libs/waddle_transports"
"${PIP[@]}" -e libs/waddle_transports

# libs/moderation_module (content-moderation classifier, see
# docs/plans/2026-09-08-content-moderation-design.md) is consumed directly
# by core/svc_process's services/moderation_gate.py -- same local-package
# shape as libs/waddle_transports above (own hash-pinned requirements.txt,
# installed editable so `import moderation_module` resolves). The Dockerfile
# already installs it this way for the runtime image; this was the missing
# piece for the *test* environment -- without it, core/svc_process's own
# suite fails to collect with ModuleNotFoundError.
echo "[install-unit-test-deps] libs/moderation_module (hash-pinned deps)"
"${PIP[@]}" --require-hashes -r libs/moderation_module/requirements.txt
echo "[install-unit-test-deps] editable libs/moderation_module"
"${PIP[@]}" -e libs/moderation_module

for pkg in hub_api core/svc_action core/svc_ingest core/svc_presentation core/svc_process core/svc_streaming; do
    req="${pkg}/requirements.txt"
    if [ ! -f "$req" ]; then
        echo "[install-unit-test-deps] ERROR: $req not found" >&2
        exit 1
    fi
    echo "[install-unit-test-deps] ${pkg}"
    "${PIP[@]}" --require-hashes -r "$req"
done

echo "[install-unit-test-deps] done"
