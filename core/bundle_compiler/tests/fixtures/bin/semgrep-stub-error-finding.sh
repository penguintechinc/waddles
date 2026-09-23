#!/usr/bin/env bash
# Variant of semgrep-stub.sh that reports one ERROR-severity finding --
# exercises scan::sast's `sast_finding` block path.
set -euo pipefail

target_dir=""
for arg in "$@"; do
    target_dir="$arg"
done
first_file=$(find "$target_dir" -type f | head -n1)

printf '{"results": [{"extra": {"severity": "ERROR"}, "path": "%s"}], "paths": {"scanned": ["%s"]}}' "$first_file" "$first_file"
