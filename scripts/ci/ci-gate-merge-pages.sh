#!/usr/bin/env bash
# Merges the per-page JSON arrays emitted by `gh api --paginate --jq '[...]'`
# into a single JSON array. With --paginate, gh runs the --jq filter once per
# page of results; for a resource with more than 100 items (e.g. check-runs
# on a PR with a large merge-queue fan-out) that prints several arrays back
# to back (`[..][..]`), which is not a single valid JSON document and breaks
# any consumer expecting one (e.g. `jq -n --argjson`). `jq -s` (slurp) reads
# the whole stream as a sequence of top-level values and wraps them in an
# outer array; `add` flattens that array-of-arrays into one merged array.
# `// []` covers the zero-page case where the input stream was empty, so the
# result is always `[]` at minimum, never null.
set -euo pipefail
jq -s 'add // []'
