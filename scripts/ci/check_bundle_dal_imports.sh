#!/usr/bin/env bash
# Fails if any App Bundle file imports the legacy flask_core.database/pydal
# surface instead of penguin-dal (D21b). Scans the three real bundle
# directories in today's repo layout -- `bundles/python/` does not exist
# until the M2/M6 directory move.
set -euo pipefail

BUNDLE_DIRS=(
    "core/svc_process/bundles"
    "core/svc_action/bundles"
    "core/svc_ingest/bundles"
)

files_scanned=0
legacy_hits=0
findings=()

for dir in "${BUNDLE_DIRS[@]}"; do
    if [ ! -d "$dir" ]; then
        echo "check_bundle_dal_imports: expected directory missing: $dir" >&2
        exit 1
    fi
    while IFS= read -r -d '' file; do
        files_scanned=$((files_scanned + 1))
        while IFS= read -r hit; do
            [ -z "$hit" ] && continue
            legacy_hits=$((legacy_hits + 1))
            findings+=("$hit")
        done < <(grep -H -nE '(^|[^.[:alnum:]_])(from flask_core\.database import|import flask_core\.database|from pydal import|^import pydal([^.[:alnum:]_]|$))' "$file" || true)
    done < <(find "$dir" -maxdepth 1 -name '*.py' -print0)
done

if [ "$files_scanned" -eq 0 ]; then
    echo "check_bundle_dal_imports: FAIL -- zero files scanned (path moved?)" >&2
    exit 1
fi

echo "check_bundle_dal_imports: files_scanned=$files_scanned legacy_hits=$legacy_hits"

if [ "$legacy_hits" -gt 0 ]; then
    echo "check_bundle_dal_imports: FAIL -- legacy flask_core.database/pydal import(s) found in bundle code:" >&2
    printf '  %s\n' "${findings[@]}" >&2
    echo "  -> use penguin_dal (see docs/APP_BUNDLE_AUTHORING.md 'Accessing the database')." >&2
    exit 1
fi

exit 0
