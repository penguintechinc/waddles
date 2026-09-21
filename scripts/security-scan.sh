#!/usr/bin/env bash
# Security gate. Replaces a `make test-security` in which every scanner was
# wrapped in `|| true` and several had stderr sent to /dev/null, so a scanner
# that crashed looked exactly like one that found nothing.
#
# Usage: scripts/security-scan.sh
#        ALLOW_MISSING_TOOLS=1 scripts/security-scan.sh   (local)

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/checks.sh
. scripts/lib/checks.sh

echo "=== Security Scans ==="

py_files=$(discover -name "*.py")
py_count=$(count_lines "$py_files")
# pip-audit must run on the same Python major the images use. Lockfiles are
# resolved per version, so auditing a 3.13 lockfile on 3.12 reports phantom
# missing dependencies for markers like `python_version < "3.13"` — 15 of 29
# lockfiles "failed" that way before this was pinned. uv gives a 3.13
# interpreter without requiring one on PATH.
PY_AUDIT_313=0
if command -v uv >/dev/null 2>&1; then
  PY_AUDIT_313=1
fi

run_bandit() {
  # HIGH severity + HIGH confidence only. The MEDIUM backlog is real but
  # pre-existing; gating on it now would mean the gate is switched off again
  # within a week. Ratchet down deliberately, never up.
  # `./.venv,./venv` only match the repo-root venvs -- each service carries its
  # own (core/svc_*/.venv, hub_api/.venv), and `.claude/worktrees/` holds agent
  # worktrees, i.e. other branches' checkouts. Left unexcluded, all 384 HIGH/HIGH
  # findings came from vendored site-packages and other branches; zero were
  # first-party. Same omission as CHECK_PRUNE_ARGS in scripts/lib/checks.sh.
  bandit -r . -lll -iii --quiet \
    --exclude ./node_modules,./.worktrees,./.claude,./tests,./.venv,./venv,./mobile,./.git,*/.venv/*,*/site-packages/*
}
run_check "bandit" bandit "$py_count" run_bandit

# Only hash-pinned lockfiles gate. Unhashed requirement files carry known
# pre-existing CVEs and are reported by the CI job, not by this local gate.
# `&& echo` would leave the loop's status non-zero when the final file has no
# hashes, and `set -e` would abort the whole suite on a legitimately empty
# result. Discovery finding nothing is not a failure; a gate finding nothing is.
locks=$(discover -name "requirements.txt" | while read -r f; do
  if grep -q -- "--hash=sha256" "$f" 2>/dev/null; then echo "$f"; fi
done)
lock_count=$(count_lines "$locks")
count_vulnerable_locks() {
  local bad=0
  for f in $locks; do
    # PYSEC-2022-252: deep-translator PyPI account takeover, no fixed version
    # published, so every release since 1.8.5 is flagged in perpetuity.
    if [ "$PY_AUDIT_313" = "1" ]; then
      uv run --quiet --python 3.13 --with pip-audit pip-audit -r "$f" \
        --no-deps --progress-spinner off --ignore-vuln PYSEC-2022-252 \
        >/dev/null 2>&1 || { echo "  vulnerable: $f" >&2; bad=$((bad + 1)); }
    else
      pip-audit -r "$f" --no-deps --progress-spinner off \
        --ignore-vuln PYSEC-2022-252 >/dev/null 2>&1 \
        || { echo "  vulnerable: $f" >&2; bad=$((bad + 1)); }
    fi
  done
  echo "$bad"
}
if [ "$PY_AUDIT_313" = "1" ]; then
  run_counted_check "pip-audit" uv "$lock_count" count_vulnerable_locks
else
  run_counted_check "pip-audit" pip-audit "$lock_count" count_vulnerable_locks
fi

pkg_files=$(discover -name "package-lock.json")
pkg_count=$(count_lines "$pkg_files")
repo_root=$(pwd)
count_vulnerable_npm() {
  local bad=0 dir
  for f in $pkg_files; do
    dir=$(dirname "$f")
    ( cd "$dir" && NPM_AUDIT_ALLOW="${NPM_AUDIT_ALLOW:-}" \
        node "$repo_root/.github/scripts/npm-audit.mjs" >/dev/null 2>&1 ) \
      || { echo "  vulnerable: $dir" >&2; bad=$((bad + 1)); }
  done
  echo "$bad"
}
run_counted_check "npm-audit" node "$pkg_count" count_vulnerable_npm

go_mods=$(discover -name "go.mod")
go_count=$(count_lines "$go_mods")
# core/module_rtc is the only Go left (822 lines) and is slated for a Rust
# rewrite at P3. These checks skip themselves once the last go.mod is gone.
count_gosec() {
  local total=0 dir n
  for mod in $go_mods; do
    dir=$(dirname "$mod")
    n=$( ( cd "$dir" && gosec -quiet ./... 2>&1 ) | grep -cE "^\\[/" || true )
    total=$((total + n))
  done
  echo "$total"
}
run_counted_check "gosec" gosec "$go_count" count_gosec

count_govulncheck() {
  local total=0 dir n
  for mod in $go_mods; do
    dir=$(dirname "$mod")
    n=$( ( cd "$dir" && govulncheck ./... 2>&1 ) | grep -cE "^Vulnerability #" || true )
    total=$((total + n))
  done
  echo "$total"
}
run_counted_check "govulncheck" govulncheck "$go_count" count_govulncheck

# Secrets: scan tracked files only. `--no-git --source .` walks the working
# tree including .worktrees/, which reports other branches' findings here.
tracked=$(git ls-files | wc -l | tr -d ' ')
count_gitleaks() {
  gitleaks detect --source . --redact --no-banner --report-format json \
    --report-path /dev/stdout 2>/dev/null \
    | grep -c '"RuleID"' || true
}
run_counted_check "gitleaks" gitleaks "$tracked" count_gitleaks

finish_checks "security"
