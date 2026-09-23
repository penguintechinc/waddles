#!/usr/bin/env bash
# Variant of semgrep-stub.sh that reports valid JSON but an empty
# `paths.scanned` list -- exercises scan::sast's semgrep-side
# `scan_empty_denominator` path (distinct from the file-count check at
# the top of `run_source_scans_with_config`, which this bypasses by
# design: the target directory is non-empty, semgrep itself is what
# claims to have examined nothing).
set -euo pipefail
printf '{"results": [], "paths": {"scanned": []}}'
