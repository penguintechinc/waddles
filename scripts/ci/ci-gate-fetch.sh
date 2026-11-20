#!/usr/bin/env bash
# Fetch a paginated GitHub API list for ci-gate with bounded retry.
# regression: ci-gate failed on a transient HTTP 502 (PR #1469, run 44789521330)
# because the error object was concatenated with the check-run array by jq.
#
# Usage: ci-gate-fetch.sh <api-path> <jq-array-filter>
#
# - Runs `gh api <path> --paginate --jq <filter>` up to CI_GATE_MAX_ATTEMPTS
#   (default 5) times with exponential backoff + jitter.
# - Retries ONLY on 5xx / network errors; 4xx (auth, 404, ...) fails
#   immediately - those are real, not transient.
# - Merges the per-page arrays (ci-gate-merge-pages.sh) and verifies the
#   result is a JSON array. Anything else is a LOUD failure (exit 1); this
#   script never emits partial/error data and never swallows failures, so
#   ci-gate stays fail-closed.
# Env: GH_BIN (default gh), CI_GATE_MAX_ATTEMPTS, CI_GATE_BACKOFF_BASE
#      (seconds, default 2), CI_GATE_BACKOFF_MAX (seconds cap, default 60).
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <api-path> <jq-array-filter>" >&2
  exit 2
fi
api_path="$1"
jq_filter="$2"

gh_bin="${GH_BIN:-gh}"
max_attempts="${CI_GATE_MAX_ATTEMPTS:-5}"
backoff_base="${CI_GATE_BACKOFF_BASE:-2}"
backoff_max="${CI_GATE_BACKOFF_MAX:-60}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
merge_pages="${script_dir}/ci-gate-merge-pages.sh"

err_file="$(mktemp)"
trap 'rm -f "${err_file}"' EXIT

last_reason="unknown"
for attempt in $(seq 1 "${max_attempts}"); do
  rc=0
  out=$("${gh_bin}" api "${api_path}" --paginate --jq "${jq_filter}" 2>"${err_file}") || rc=$?
  if [ "${rc}" -eq 0 ]; then
    if merged=$(printf '%s' "${out}" | "${merge_pages}" 2>>"${err_file}") \
       && jq -e 'type == "array"' <<<"${merged}" >/dev/null 2>&1; then
      printf '%s\n' "${merged}"
      exit 0
    fi
    last_reason="response was not a JSON array"
    retryable=true
  else
    last_reason="$(tr '\n' ' ' <"${err_file}" | cut -c1-300)"
    # Retry only on 5xx / network-level failures.
    if grep -Eqi 'HTTP 5[0-9][0-9]|timeout|timed out|connection (reset|refused)|EOF|temporary failure|no such host|network is unreachable|TLS handshake' "${err_file}"; then
      retryable=true
    else
      retryable=false
    fi
  fi

  if [ "${retryable}" != "true" ]; then
    echo "::error::ci-gate: GitHub API call failed with a non-retryable error for ${api_path}: ${last_reason}" >&2
    exit 1
  fi
  if [ "${attempt}" -lt "${max_attempts}" ]; then
    delay=$(( backoff_base * (1 << (attempt - 1)) ))
    if [ "${delay}" -gt "${backoff_max}" ]; then delay="${backoff_max}"; fi
    # Jitter: add 0..delay/2 seconds (integer math, bash 3.2-safe).
    jitter=$(( RANDOM % (delay / 2 + 1) ))
    echo "ci-gate: transient API failure on ${api_path} (attempt ${attempt}/${max_attempts}): ${last_reason} - retrying in $((delay + jitter))s" >&2
    sleep "$((delay + jitter))"
  fi
done

echo "::error::ci-gate FAIL - GitHub API unavailable after ${max_attempts} attempts for ${api_path}: ${last_reason}" >&2
exit 1
