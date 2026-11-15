#!/usr/bin/env bash
# CI gate (Secrets T10): the shared HS256 JWT secret must never reappear.
#
# Platform JWTs are ES256 with keys sealed in platform_vault (docs/PLATFORM_VAULT.md). This gate
# fails when a tracked file names a shared JWT secret (`JWT_SECRET`, `JWT_SECRET_KEY`,
# `JWT_HMAC_SECRET`) or an HMAC JWT algorithm (`HS256`/`HS384`/`HS512`) outside the allowlist
# `scripts/ci/jwt-hs256-allowlist.txt`. Allowlisted paths are tests that assert HS* is REJECTED
# (plus the documented exceptions in that file). `docs/` and CHANGELOG.md are history, not code,
# and are not scanned.
#
# Verification integrity: the gate reports how many files it examined and how many allowlisted
# files actually still matched; zero files examined, or an allowlist entry that matches nothing
# (a stale exemption), is a FAILURE, never a pass.
#
# Usage: scripts/ci/check-no-shared-jwt-secret.sh [repo-root]
set -euo pipefail

ROOT="${1:-$(git rev-parse --show-toplevel)}"
cd "$ROOT"
ALLOWLIST="scripts/ci/jwt-hs256-allowlist.txt"
PATTERN='(^|[^A-Za-z0-9_])JWT_(HMAC_)?SECRET(_KEY)?([^A-Za-z0-9_]|$)|(^|[^A-Za-z0-9])HS(256|384|512)([^A-Za-z0-9]|$)'

if [ ! -f "$ALLOWLIST" ]; then
  echo "FAIL: allowlist $ALLOWLIST is missing" >&2
  exit 2
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp" "$tmp.files" "$tmp.allow" "$tmp.hits"' EXIT

EXCLUDES=(':!docs' ':!CHANGELOG.md' ':!scripts/ci/check-no-shared-jwt-secret.sh' ':!scripts/ci/jwt-hs256-allowlist.txt' ':!.github/workflows/no-shared-jwt-secret.yml')

# Denominator: tracked files in scope (history docs and this gate's own files excluded).
examined="$(git ls-files -- . "${EXCLUDES[@]}" | wc -l | tr -d ' ')"
if [ "$examined" -eq 0 ]; then
  echo "FAIL: zero files examined (wrong root?)" >&2
  exit 2
fi

# Allowlist: `path|reason`, comments with '#'. Paths are exact files or directory prefixes (end /).
# grep -v exits 1 on an empty list, which is a legitimate (strictest) allowlist.
{ grep -v -E '^[[:space:]]*(#|$)' "$ALLOWLIST" || [ $? -eq 1 ]; } | cut -d'|' -f1 > "$tmp.allow"

# All matching lines: file:line:text (binary files skipped). git grep: 0 = matches, 1 = none.
set +e
git grep -I -n -E "$PATTERN" -- . "${EXCLUDES[@]}" > "$tmp.hits"
rc=$?
set -e
if [ "$rc" -gt 1 ]; then
  echo "FAIL: git grep errored (rc=$rc)" >&2
  exit 2
fi

violations=0
allowed_files_hit=""
while IFS= read -r line; do
  file="${line%%:*}"
  ok=0
  while IFS= read -r allow; do
    [ -z "$allow" ] && continue
    case "$allow" in
      */) case "$file" in "$allow"*) ok=1 ;; esac ;;
      *) [ "$file" = "$allow" ] && ok=1 ;;
    esac
    if [ "$ok" -eq 1 ]; then
      allowed_files_hit="$allowed_files_hit
$allow"
      break
    fi
  done < "$tmp.allow"
  if [ "$ok" -eq 0 ]; then
    echo "VIOLATION: $line" >&2
    violations=$((violations + 1))
  fi
done < "$tmp.hits"

# A stale allowlist entry (matches nothing any more) hides a future regression: fail on it.
stale=0
while IFS= read -r allow; do
  [ -z "$allow" ] && continue
  if ! printf '%s\n' "$allowed_files_hit" | grep -qxF "$allow"; then
    echo "STALE ALLOWLIST ENTRY (no match; remove it): $allow" >&2
    stale=$((stale + 1))
  fi
done < "$tmp.allow"

allowed_distinct="$(printf '%s\n' "$allowed_files_hit" | grep -v '^$' | sort -u | wc -l | tr -d ' ')"
echo "no-shared-jwt-secret: examined=$examined files, violations=$violations, stale_allowlist=$stale, allowlist_entries_matching=$allowed_distinct"
if [ "$violations" -ne 0 ] || [ "$stale" -ne 0 ]; then
  exit 1
fi
