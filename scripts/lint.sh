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
# Every counter below goes through tool_capture: only the exit codes that mean
# "ran to completion" are accepted, so a scanner that crashes fails the gate
# instead of printing nothing and counting as zero findings.
count_flake8() {
  local out
  # --exit-zero: flake8 exits 1 on findings AND on a crash, which are
  # indistinguishable; with it, any non-zero exit is a genuine crash.
  out=$(tool_capture "0" flake8 . --exit-zero --max-line-length=120 \
    --exclude=.git,__pycache__,venv,.venv,node_modules,.worktrees,.claude/worktrees,vendor) || return 1
  printf '%s\n' "$out" | count_matching '.'
}
run_counted_check "flake8" flake8 "$py_count" count_flake8

sh_files=$(discover -name "*.sh")
sh_count=$(count_lines "$sh_files")
count_shellcheck() {
  local out
  # shellcheck disable=SC2086
  out=$(tool_capture "0 1" shellcheck -f gcc $sh_files) || return 1
  printf '%s\n' "$out" | count_matching '.'
}
run_counted_check "shellcheck" shellcheck "$sh_count" count_shellcheck

docker_files=$(discover -name "Dockerfile*")
docker_count=$(count_lines "$docker_files")
count_hadolint() {
  local out
  # shellcheck disable=SC2086
  out=$(tool_capture "0 1" hadolint -f tty $docker_files) || return 1
  printf '%s\n' "$out" | count_matching '.'
}
run_counted_check "hadolint" hadolint "$docker_count" count_hadolint

# Go is being phased out (critical-rules.md). This skips cleanly once the last
# go.mod is gone, rather than needing the check deleted.
go_mods=$(discover -name "go.mod")
go_count=$(count_lines "$go_mods")
count_golangci() {
  local total=0 dir n out
  for mod in $go_mods; do
    dir=$(dirname "$mod")
    out=$(cd "$dir" && tool_capture "0 1" golangci-lint run) || return 1
    n=$(printf '%s\n' "$out" | count_matching '.')
    total=$((total + n))
  done
  echo "$total"
}
run_counted_check "golangci-lint" golangci-lint "$go_count" count_golangci

# Workflow security. HIGH only — the medium backlog is large and mostly
# advisory, and gating on it now would bury the findings that matter.
wf_count=$(count_lines "$(discover -path "./.github/workflows/*" -name "*.yml")")
count_zizmor() {
  local out
  # zizmor encodes the highest severity found in its exit code (10-14); 0 is
  # clean. Anything else (1 = runtime error, 2 = usage) is a crash.
  out=$(tool_capture "0 10 11 12 13 14" zizmor .github/workflows/ --min-severity high) || return 1
  printf '%s\n' "$out" | count_matching '^(error|warning)\['
}
run_counted_check "zizmor" zizmor "$wf_count" count_zizmor

# Documentation reference gate. Counts repo paths referenced in docs/**/*.md
# that no longer exist on disk, ratcheted against the baseline. The script owns
# the zero-references guard; here a broken run is forced over budget so lint
# fails rather than silently passing.
doc_md_files=$(discover -path "./docs/*" -name "*.md")
doc_md_count=$(count_lines "$doc_md_files")
count_doc_refs() {
  scripts/check-doc-refs.sh --count || echo 999999
}
run_counted_check "doc-refs" bash "$doc_md_count" count_doc_refs

finish_checks "lint"
