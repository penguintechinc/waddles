#!/usr/bin/env bash
# Stand-in for `semgrep --config <rules> --json --quiet <dir>`, used only
# by this crate's own host-run test suite (tests/scan_test.rs) via
# ScannerConfig::semgrep_bin. The pinned, real semgrep binary runs inside
# the compiler's container image (M2a plan Task 19) and is what actually
# enforces SAST findings in production; this stub only proves
# scan::sast's orchestration -- ordering, the non-zero-denominator gate,
# and JSON parsing -- on a host where the real semgrep install may not be
# usable (e.g. a Python dependency conflict unrelated to this crate).
#
# Always reports zero findings and one "scanned" entry per file under the
# target directory, so the denominator gate is exercised honestly against
# a real, non-zero count -- never a hardcoded fake number.
set -euo pipefail

target_dir=""
for arg in "$@"; do
    target_dir="$arg"
done

first=true
scanned_json="["
while IFS= read -r -d '' f; do
    if [ "$first" = true ]; then
        first=false
    else
        scanned_json+=","
    fi
    scanned_json+="\"${f}\""
done < <(find "$target_dir" -type f -print0)
scanned_json+="]"

printf '{"results": [], "paths": {"scanned": %s}}' "$scanned_json"
