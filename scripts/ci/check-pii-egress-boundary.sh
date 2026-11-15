#!/usr/bin/env bash
# PII boundary gate: only the platform egress (svc-ingest's outbound drain / sender_exec) and
# svc-presentation (overlays) may detokenize. Any other crate referencing the egress
# detokenizer could substitute real names/handles into text that then rides queues, caches or
# logs -- exactly the regression this gate exists to stop (svc-action used to do it).
#
# Usage: check-pii-egress-boundary.sh [repo-root]   (default: the git toplevel)
# Fails (non-zero) on any code reference outside the allowed paths, and on ZERO files examined
# (a scanner pointed at the wrong root must not report clean).
set -euo pipefail

root="${1:-$(git rev-parse --show-toplevel)}"
cd "$root"

# Code references only: crate path use, Cargo dependency, or the two substitution entry points.
pattern='egress_detokenizer::|egress_detokenizer[[:space:]]*=|detokenize_resolving|detokenize_for_egress'

# Where a reference is legitimate (prefix match on the repo-relative path).
allowed=(
  "core/egress_detokenizer/"
  "core/svc_presentation/"
  "core/svc_ingest/src/egress.rs"
  "core/svc_ingest/src/display_names.rs"   # the egress pass's direct-DB resolver
  "core/svc_ingest/src/outbound"
  "core/svc_ingest/src/connector/sender_exec/"
  "core/svc_ingest/src/discord_rest.rs"
  "core/svc_ingest/Cargo.toml"
  "core/svc_ingest/tests/"
)

mapfile -t files < <(find core \( -path '*/target' -o -path '*/node_modules' \) -prune -o \
  \( -name '*.rs' -o -name 'Cargo.toml' \) -type f -print | LC_ALL=C sort)
examined=${#files[@]}
if [ "$examined" -eq 0 ]; then
  echo "FAIL: pii-egress-boundary examined 0 files under $root/core (wrong root?)" >&2
  exit 2
fi

violations=0
for f in "${files[@]}"; do
  ok=0
  for prefix in "${allowed[@]}"; do
    case "$f" in "$prefix"*) ok=1; break ;; esac
  done
  [ "$ok" -eq 1 ] && continue
  # grep exits 1 on no match; that is the normal case, so it is handled explicitly.
  if hits=$(grep -nE "$pattern" "$f"); then
    # A comment may name the crate; only code counts.
    code=$(printf '%s\n' "$hits" | grep -vE '^[0-9]+:[[:space:]]*//' || true)
    if [ -n "$code" ]; then
      printf '%s\n' "$code" | sed "s|^|VIOLATION $f:|" >&2
      violations=$((violations + 1))
    fi
  fi
done

echo "pii-egress-boundary: examined=$examined files, violating_files=$violations"
if [ "$violations" -ne 0 ]; then
  echo "FAIL: only svc-ingest's outbound drain/sender_exec and svc-presentation may detokenize" >&2
  exit 1
fi
