#!/usr/bin/env bash
# No-stubs / no-silent-fallbacks CI gate (bash 3.2 compatible).
#
# Incident: a `flags capability not wired -- TODO(M4+)` stub returned a
# silent default and shipped labeled "done" -- it took a multi-hour alpha
# outage to notice the capability was never actually wired. This gate
# enforces the repo's existing (previously-ignored) no-stubs rule in two
# layers against NON-TEST shipped source:
#
#   1. Stub markers (grep): TODO/FIXME/XXX/HACK, unimplemented!()/todo!(),
#      panic!("...not impl..."), NotImplementedError, "not wired"/
#      "not implemented" string literals -- checked against
#      .ci/stub-allowlist.yml (exact path + pattern + REQUIRED tracking
#      issue; see that file's header).
#   2. Silent fallbacks (semgrep, scripts/ci/semgrep-silent-fallback.yml):
#      an error/except branch that swallows the error and returns a
#      default/None with no log call -- checked against
#      .ci/silent-fallback-baseline.json (frozen pre-existing-debt
#      snapshot, gh-605; NEW findings outside it fail the gate).
#
# Scope: Rust core/, Python hub_api/+sdk/+bundles/, TS
# admin/hub_module/frontend/src -- excludes tests/test files (critical-
# rules.md Verification Integrity: a 0-files-scanned run is a FAIL, not a
# skip -- see the files_scanned check below).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

ALLOWLIST="${REPO_ROOT}/.ci/stub-allowlist.yml"
BASELINE="${REPO_ROOT}/.ci/silent-fallback-baseline.json"
SEMGREP_CONFIG="${REPO_ROOT}/scripts/ci/semgrep-silent-fallback.yml"
FILTER_SCRIPT="${REPO_ROOT}/scripts/ci/check_no_stubs_filter.py"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Directories/extensions in scope -- "lang:root:ext1,ext2" triples.
SCAN_SPECS=(
    "rust:core:rs"
    "python:hub_api:py"
    "python:sdk:py"
    "python:bundles:py"
    "ts:admin/hub_module/frontend/src:ts,tsx,js,jsx"
)

# Exclusion shape shared by both the stub-marker scan and the semgrep run --
# generated/test code, never shipped-source-authored-by-us.
EXCLUDE_REGEX='(^|/)(tests?|__tests__|node_modules|target|dist|build)(/|$)|_test\.(rs|py)$|^test_.*\.py$|\.test\.(ts|tsx|js|jsx)$|\.spec\.(ts|tsx|js|jsx)$|_pb2(_grpc)?\.py$'

STUB_PATTERN='TODO|FIXME|XXX|HACK|unimplemented!\(\)|todo!\(\)|NotImplementedError|not wired|not implemented|panic!\([^)]*not impl[^)]*\)'

if [ ! -f "${ALLOWLIST}" ]; then
    echo "check-no-stubs: FAIL -- missing ${ALLOWLIST}" >&2
    exit 1
fi
if [ ! -f "${SEMGREP_CONFIG}" ]; then
    echo "check-no-stubs: FAIL -- missing ${SEMGREP_CONFIG}" >&2
    exit 1
fi
if [ ! -f "${BASELINE}" ]; then
    echo "check-no-stubs: FAIL -- missing ${BASELINE}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Phase 1: file discovery (shared denominator proof)
# ---------------------------------------------------------------------------
files_scanned=0
scan_dirs=()
file_list="$(mktemp)"
findings_json=""
semgrep_json=""
combined_json=""
trap 'rm -f "${file_list}" "${findings_json}" "${semgrep_json}" "${combined_json}"' EXIT

for spec in "${SCAN_SPECS[@]}"; do
    root="$(echo "${spec}" | cut -d: -f2)"
    exts="$(echo "${spec}" | cut -d: -f3)"
    [ -d "${root}" ] || continue
    scan_dirs+=("${root}")
    old_ifs="${IFS}"
    IFS=','
    for ext in ${exts}; do
        IFS="${old_ifs}"
        while IFS= read -r -d '' f; do
            rel="${f#./}"
            if echo "${rel}" | grep -qE "${EXCLUDE_REGEX}"; then
                continue
            fi
            echo "${rel}" >> "${file_list}"
            files_scanned=$((files_scanned + 1))
        done < <(find "${root}" -type f -name "*.${ext}" -print0)
    done
    IFS="${old_ifs}"
done

if [ "${files_scanned}" -eq 0 ]; then
    echo "check-no-stubs: FAIL -- zero files scanned (scan roots moved/missing?). Roots checked: ${SCAN_SPECS[*]}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Phase 2: stub-marker grep -> JSON findings
# ---------------------------------------------------------------------------
findings_json="$(mktemp)"
echo "[" > "${findings_json}"
first=1
while IFS= read -r rel; do
    [ -z "${rel}" ] && continue
    while IFS=: read -r lineno text; do
        [ -z "${lineno}" ] && continue
        if [ "${first}" -eq 0 ]; then printf ',\n' >> "${findings_json}"; fi
        first=0
        esc_path=$("${PYTHON_BIN}" -c 'import json,sys; print(json.dumps(sys.argv[1]))' "${rel}")
        esc_text=$("${PYTHON_BIN}" -c 'import json,sys; print(json.dumps(sys.argv[1]))' "${text}")
        printf '{"kind":"stub","path":%s,"line":%s,"text":%s}' "${esc_path}" "${lineno}" "${esc_text}" >> "${findings_json}"
    done < <(grep -nE "${STUB_PATTERN}" "${rel}" 2>/dev/null || true)
done < "${file_list}"
echo "]" >> "${findings_json}"

# ---------------------------------------------------------------------------
# Phase 3: semgrep silent-fallback scan -> JSON findings
# ---------------------------------------------------------------------------
semgrep_raw="$(mktemp)"
if ! command -v semgrep >/dev/null 2>&1; then
    echo "check-no-stubs: FAIL -- semgrep not found on PATH (required for the silent-fallback layer)" >&2
    exit 1
fi
# semgrep exits 1 when it has findings (expected -- that's the whole point
# of this scan) and 2+ on a real scan failure (bad rule, crash, etc.) --
# only the latter is fatal here. `set +e`/`set -e` bracket this one command
# deliberately so the exit code can be branched on, never masked with
# `|| true` (critical-rules.md Verification Integrity).
set +e
semgrep --config "${SEMGREP_CONFIG}" --json --metrics=off -q "${scan_dirs[@]}" > "${semgrep_raw}"
semgrep_status=$?
set -e
if [ "${semgrep_status}" -gt 1 ]; then
    echo "check-no-stubs: FAIL -- semgrep exited ${semgrep_status} (scan error, not just findings)" >&2
    cat "${semgrep_raw}" >&2
    exit 1
fi

semgrep_json="$(mktemp)"
"${PYTHON_BIN}" - "${semgrep_raw}" > "${semgrep_json}" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
out = []
for r in d["results"]:
    out.append({
        "kind": "fallback",
        "rule_id": r["check_id"].rsplit(".", 1)[-1],
        "path": r["path"],
        "line": r["start"]["line"],
        "text": r.get("extra", {}).get("lines", ""),
    })
print(json.dumps(out))
PYEOF
rm -f "${semgrep_raw}"

# ---------------------------------------------------------------------------
# Phase 4: combine + filter against allowlist/baseline
# ---------------------------------------------------------------------------
combined_json="$(mktemp)"
"${PYTHON_BIN}" -c '
import json, sys
a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
print(json.dumps(a + b))
' "${findings_json}" "${semgrep_json}" > "${combined_json}"

echo "check-no-stubs: files_scanned=${files_scanned}"

set +e
"${PYTHON_BIN}" "${FILTER_SCRIPT}" "${ALLOWLIST}" "${BASELINE}" < "${combined_json}"
status=$?
set -e

if [ "${status}" -ne 0 ]; then
    echo "check-no-stubs: FAIL" >&2
    exit 1
fi

echo "check-no-stubs: PASS"
exit 0
