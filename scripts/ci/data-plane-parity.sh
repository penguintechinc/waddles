#!/usr/bin/env bash
# Data-plane golden-vector parity harness runner (gh #840 PR4) -- `make test-data-plane-parity`.
#
# One shared corpus (tests/data_plane_parity/corpus) is checked four ways; every step
# asserts its own non-zero denominator and the script stops on the first failure:
#   1. Rust oracle    -- core/svc_ingest/tests/parity_golden.rs: real connector frame parsers +
#                        normalize.rs must equal each committed golden.
#   2. Python path    -- tests/data_plane_parity: real receivers + builtin_handlers normalize()
#                        diffed against the SAME goldens (=> Python vs Rust), declared divergences
#                        must match exactly (new OR stale both fail).
#   3. PII gate       -- core/svc_process/tests/parity_pii_tokenize.rs: every corpus event through
#                        the closed-schema pii_tokenize classifier.
#   4. Handler map    -- scripts/ci/check-builtin-handler-inventory.py (+ its planted-violation self-test).
#
# Env: PYTHON_BIN (default python3, must have the svc_ingest unit-test deps --
# scripts/ci/install-unit-test-deps.sh), CARGO_TARGET_DIR (optional, share across worktrees).
# Re-bless goldens after an INTENDED normalize change (review the diff!):
#   PARITY_BLESS=1 cargo test --test parity_golden   (core/svc_ingest, dual-path vectors)
#   PARITY_BLESS=1 python3 -m pytest tests/data_plane_parity   (python-only slots)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
cd "$REPO_ROOT"

if [ "${PARITY_BLESS:-}" = "1" ]; then
    echo "[parity] refusing to run the gate with PARITY_BLESS=1 (bless is a manual, reviewed step)" >&2
    exit 2
fi

command -v cargo >/dev/null 2>&1 || { echo "[parity] cargo not found" >&2; exit 127; }
command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "[parity] $PYTHON_BIN not found" >&2; exit 127; }

step() { echo; echo "[parity] === $* ==="; }

# Captures a step's output so its denominator line can be asserted, while the exit status of the
# real command (not tee/grep) decides pass/fail.
run_and_require() {
    local pattern="$1"; shift
    local out rc
    out="$(mktemp)"
    "$@" 2>&1 | tee "$out"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        rm -f "$out"
        echo "[parity] FAIL: '$*' exited $rc" >&2
        return "$rc"
    fi
    if ! grep -Eq "$pattern" "$out"; then
        rm -f "$out"
        echo "[parity] FAIL: denominator line /$pattern/ missing -- zero examined" >&2
        return 1
    fi
    rm -f "$out"
}

step "1/4 Rust oracle (svc_ingest normalize.rs vs goldens)"
(cd core/svc_ingest && run_and_require '^parity_golden: [1-9][0-9]* dual-path vectors compared' \
    cargo test --locked --test parity_golden -- --nocapture)

step "2/4 Python path vs goldens (real receivers + normalizers)"
run_and_require '^\[parity\] vectors=[1-9][0-9]* dual-path=[1-9][0-9]*' \
    "$PYTHON_BIN" -m pytest tests/data_plane_parity -q -s

step "3/4 PII-tokenize acceptance (svc_process pii_tokenize classifier)"
(cd core/svc_process && run_and_require '^parity_pii_tokenize: [1-9][0-9]* vectors \([1-9][0-9]* accepted' \
    cargo test --locked --test parity_pii_tokenize -- --nocapture)

step "4/4 builtin_handlers -> Rust-path/bundle inventory"
run_and_require '[1-9][0-9]* passed' \
    "$PYTHON_BIN" -m pytest scripts/ci/tests/test_check_builtin_handler_inventory.py -q -p no:cacheprovider
run_and_require '^\[handler-inventory\] handlers=[1-9][0-9]* ' \
    "$PYTHON_BIN" scripts/ci/check-builtin-handler-inventory.py

echo
echo "[parity] ALL GATES PASSED"
