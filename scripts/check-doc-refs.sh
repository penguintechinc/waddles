#!/usr/bin/env bash
# Documentation reference gate. Extracts every repository path referenced in
# backticks in docs/**/*.md and asserts each one still exists on disk.
#
# Docs go stale because nothing fails when they do. This is the failing thing:
# a `services/foo` in a doc that no longer exists on disk is a dead reference,
# and the count of dead references may not exceed DOC_REF_BUDGET.
#
# The budget is seeded at today's measured debt and ratcheted down, never up —
# existing debt stays visible without blocking work, and any NEW dead reference
# fails immediately.
#
# A run that extracts zero references is itself a FAILURE: a validator pointed
# at a moved directory finds nothing and reports clean. The denominator is part
# of the result, so it is guarded and printed.
#
# Bash 3.2 compatible (general.md) — no associative arrays, no mapfile.
#
# Usage: scripts/check-doc-refs.sh            full gate (make check-docs)
#        scripts/check-doc-refs.sh --count    print only the dead count (lint)
#        DOC_REF_BUDGET=N scripts/check-doc-refs.sh   override the budget

set -euo pipefail
cd "$(dirname "$0")/.."

# Seeded 2026-08-29 by measuring the tree at HEAD. Lower as phases land; never
# raise it.
DOC_REF_BUDGET="${DOC_REF_BUDGET:-21}"

count_only=0
[ "${1:-}" = "--count" ] && count_only=1

# Every repo path referenced in backticks under a top-level dir we own. Trailing
# slashes normalised off; deduplicated. `.worktrees/` holds other branches'
# checkouts and is excluded so this branch is never judged against their docs.
# The regex is a literal grep pattern; single quotes are intentional.
# shellcheck disable=SC2016
refs=$(grep -rhoE '`(action|core|trigger|processing|services|libs|admin|k8s)/[a-zA-Z0-9_/-]+`' \
        docs/ --include='*.md' --exclude-dir='.worktrees' \
        | tr -d '`' | sed 's:/$::' | sort -u)
total=$(printf '%s\n' "$refs" | grep -c . || true)

if [ "$total" -eq 0 ]; then
  echo "FAIL: zero references extracted — validator is broken (moved docs/ or bad regex)" >&2
  exit 1
fi

dead=0
dead_list=""
for r in $refs; do
  if [ ! -e "$r" ]; then
    dead=$((dead + 1))
    dead_list="${dead_list}dead: ${r}"$'\n'
  fi
done

if [ "$count_only" -eq 1 ]; then
  echo "$dead"
  exit 0
fi

[ -n "$dead_list" ] && printf '%s' "$dead_list"
echo "examined=$total dead=$dead allowed=$DOC_REF_BUDGET"
[ "$dead" -le "$DOC_REF_BUDGET" ]
