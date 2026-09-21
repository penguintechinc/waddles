#!/usr/bin/env bash
# Lint gate. Replaces a `make lint` in which every tool was wrapped in
# `|| true`, so the target exited 0 no matter what any linter reported.
#
# Findings are counted against `.checks-baseline` rather than against zero:
# switching these on revealed ~8,700 pre-existing findings, and a gate that
# blocks every commit on day one gets switched back off by day two. New
# findings fail; the baselines only move down.
#
# Usage: scripts/lint.sh                        (make lint)
#        ALLOW_MISSING_TOOLS=1 scripts/lint.sh  (local, not all tools installed)

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/checks.sh
. scripts/lib/checks.sh

echo "=== Linting ==="

py_files=$(discover -name "*.py")
py_count=$(count_lines "$py_files")
count_flake8() {
  flake8 . --max-line-length=120 \
    --exclude=.git,__pycache__,venv,.venv,node_modules,.worktrees,.claude/worktrees,vendor \
    2>/dev/null | grep -c . || true
}
run_counted_check "flake8" flake8 "$py_count" count_flake8

sh_files=$(discover -name "*.sh")
sh_count=$(count_lines "$sh_files")
count_shellcheck() {
  # shellcheck disable=SC2086
  shellcheck -f gcc $sh_files 2>/dev/null | grep -c . || true
}
run_counted_check "shellcheck" shellcheck "$sh_count" count_shellcheck

docker_files=$(discover -name "Dockerfile*")
docker_count=$(count_lines "$docker_files")
count_hadolint() {
  # shellcheck disable=SC2086
  hadolint -f tty $docker_files 2>/dev/null | grep -c . || true
}
run_counted_check "hadolint" hadolint "$docker_count" count_hadolint

# Go is being phased out (critical-rules.md). This skips cleanly once the last
# go.mod is gone, rather than needing the check deleted.
go_mods=$(discover -name "go.mod")
go_count=$(count_lines "$go_mods")
count_golangci() {
  local total=0 dir n
  for mod in $go_mods; do
    dir=$(dirname "$mod")
    n=$( ( cd "$dir" && golangci-lint run 2>/dev/null ) | grep -c . || true )
    total=$((total + n))
  done
  echo "$total"
}
run_counted_check "golangci-lint" golangci-lint "$go_count" count_golangci

# Workflow security. HIGH only — the medium backlog is large and mostly
# advisory, and gating on it now would bury the findings that matter.
wf_count=$(count_lines "$(discover -path "./.github/workflows/*" -name "*.yml")")
count_zizmor() {
  zizmor .github/workflows/ --min-severity high 2>/dev/null \
    | grep -cE "^(error|warning)\[" || true
}
run_counted_check "zizmor" zizmor "$wf_count" count_zizmor

# Documentation reference gate. Counts repo paths referenced in docs/**/*.md
# that no longer exist on disk, ratcheted against the baseline. The script owns
# the zero-references guard; here a broken run is forced over budget so lint
# fails rather than silently passing.
doc_md_files=$(discover -path "./docs/*" -name "*.md")
doc_md_count=$(count_lines "$doc_md_files")
count_doc_refs() {
  scripts/check-doc-refs.sh --count 2>/dev/null || echo 999999
}
run_counted_check "doc-refs" bash "$doc_md_count" count_doc_refs

finish_checks "lint"
