#!/usr/bin/env bash
# Generic pod-phase check for the dal2-alpha deploy smoke step (step 2).
#
# Every pod in the namespace must be Running or Succeeded, EXCEPT the AOT-precompile batch
# pods, which are excluded here and judged only by the explicit precompile gate (step 2b,
# scripts/ci/smoke/precompile-batch-gate.py). A precompile compile batch is legitimately
# Pending (init containers) for minutes and the executor JIT-falls-back meanwhile, so it
# must never fail the deploy.
#
# Precompile pods are excluded by NAME, not by the waddlebot.io/workload label. A label
# selector (`waddlebot.io/workload!=executor-precompile`) is WRONG for two reasons: k8s
# equality `!=` also selects pods that LACK the key, so a CronJob batch pod created before
# that label was added to the chart (or any future label rename) slips the exclusion and
# fails the deploy (alpha deploy 43894038667: waddlebot-executor-precompile-29914020-cnpqp,
# a pre-#1391 run, counted as bad). The pod NAME is stable: every precompile pod -- CronJob
# batch (<release>-executor-precompile-<runid>-*) and per-release enqueue
# (<release>-executor-precompile-enqueue-r<rev>-*) -- contains "executor-precompile".
#
# Prints "pods examined=N bad=M" (zero examined is a FAILURE, not a pass). Exits non-zero on
# any bad pod or an empty denominator.
#
# Usage: pod-phase-check.sh <namespace>
set -euo pipefail

NS="${1:?usage: pod-phase-check.sh <namespace>}"

PODS=0
BAD=0
while read -r name phase; do
  [ -n "${name}" ] || continue
  case "${name}" in
    *executor-precompile*) continue ;;  # judged only by the precompile gate (step 2b)
  esac
  PODS=$((PODS + 1))
  case "${phase}" in
    Running | Succeeded) ;;
    *)
      echo "NOT READY: ${name} phase=${phase}" >&2
      BAD=$((BAD + 1))
      ;;
  esac
done < <(kubectl -n "${NS}" get pods --no-headers \
  -o custom-columns=NAME:.metadata.name,PHASE:.status.phase)

echo "pods examined=${PODS} bad=${BAD}"
[ "${PODS}" -gt 0 ] || { echo "::error::zero pods examined in ${NS}" >&2; exit 1; }
[ "${BAD}" -eq 0 ] || exit 1
