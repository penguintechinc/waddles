#!/usr/bin/env bash
# Generic pod-phase check for the dal2-alpha deploy smoke step (step 2).
#
# Long-running workloads (Deployment/StatefulSet/DaemonSet pods) must be Running/Succeeded.
# Job/CronJob-owned pods are NOT judged by their instantaneous phase -- a role-sync /
# event-sync CronJob fires every minute and its pod is legitimately Pending/Init for a few
# seconds, so a single snapshot failed the deploy at random. Instead, a Job created at/after
# this release's deploy time whose status is Failed trips the gate; a merely Pending/active
# Job pod does not, and an ancient pre-release Failed run never does. AOT-precompile pods are
# excluded here and judged only by the explicit precompile gate (step 2b). Classification
# logic + tests: scripts/ci/smoke/pod-phase-classify.py.
#
# Prints "pods examined=N bad=M" (N = long-running pods; zero is a FAILURE). Exits non-zero
# on any bad long-running pod or any Failed this-release Job.
#
# Usage: pod-phase-check.sh <namespace> [release]   (release defaults to $RELEASE/waddlebot)
set -euo pipefail

NS="${1:?usage: pod-phase-check.sh <namespace> [release]}"
RELEASE="${2:-${RELEASE:-waddlebot}}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# Release boundary: the current Helm revision's deploy time. Jobs created at/after it are
# "this release"; older Failed CronJob runs must not trip this deploy's smoke.
SINCE="$(helm status "${RELEASE}" -n "${NS}" -o json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["last_deployed"])')"
[ -n "${SINCE}" ] || { echo "::error::could not determine deploy time for release ${RELEASE}" >&2; exit 1; }

kubectl -n "${NS}" get pods -o json > "${WORK}/pods.json"
kubectl -n "${NS}" get jobs -o json > "${WORK}/jobs.json"

python3 "${HERE}/pod-phase-classify.py" "${WORK}/pods.json" "${WORK}/jobs.json" "${SINCE}"
