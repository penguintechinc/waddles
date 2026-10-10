#!/usr/bin/env bash
# Validates the observability bundle end to end; any failing step fails the script.
#
#   1. dashboards: jq structure, unique uids, panels carry queries
#   2. drift: committed dashboards / PrometheusRule == what obs_tool.py generates
#   3. every dashboard query parses (wrapped into a rule file for promtool)
#   4. every metric a dashboard or alert references is emitted by the code (obs_tool.py check-metrics)
#   5. promtool check rules + promtool unit tests (alerts must fire / stay silent as asserted)
#   6. ServiceMonitor manifest shape
#
# promtool: local binary when on PATH, otherwise the pinned prom/prometheus container
# (read-only mount, no network). Counts are printed for every step: zero items examined is a failure.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${here}"

PROMTOOL_IMAGE='prom/prometheus:v3.5.0@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996'
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

python3 -I -c 'import yaml' 2>/dev/null || fail "PyYAML is required (python3 -m pip install pyyaml)"
command -v jq >/dev/null 2>&1 || fail "jq is required"

run_promtool() {
  local dir="$1"
  shift
  if command -v promtool >/dev/null 2>&1; then
    (cd "${dir}" && promtool "$@")
  else
    command -v docker >/dev/null 2>&1 || fail "need promtool on PATH or docker to run ${PROMTOOL_IMAGE}"
    docker run --rm --network none --user "$(id -u):$(id -g)" --entrypoint promtool \
      -v "${dir}:/work:ro" -w /work "${PROMTOOL_IMAGE}" "$@"
  fi
}

echo "== 1. dashboards (jq)"
shopt -s nullglob
files=(dashboards/*.json)
[[ ${#files[@]} -gt 0 ]] || fail "no dashboards found in ${here}/dashboards"
panels_total=0
for f in "${files[@]}"; do
  jq -e '
    (.uid | type == "string" and length > 0)
    and (.title | type == "string" and length > 0)
    and ([.panels[] | select(.type != "row")] | length > 0)
    and ([.panels[] | select(.type != "row") | select((.targets | length) == 0 or any(.targets[]; (.expr | length) == 0))] | length == 0)
    and ([.panels[].id] | length == (unique | length))
    and (any(.templating.list[]; .type == "datasource"))
  ' "${f}" >/dev/null || fail "${f}: structural check failed"
  n="$(jq '[.panels[] | select(.type != "row")] | length' "${f}")"
  echo "   ${f}: ${n} panels"
  panels_total=$((panels_total + n))
done
[[ "${panels_total}" -gt 0 ]] || fail "zero panels examined"
dups="$(jq -r .uid "${files[@]}" | sort | uniq -d)"
[[ -z "${dups}" ]] || fail "duplicate dashboard uid: ${dups}"
echo "   ${#files[@]} dashboards, ${panels_total} panels OK"

echo "== 2. generated files are in sync"
python3 -I scripts/obs_tool.py dashboards --out "${tmp}/dashboards" >/dev/null
diff -r "${tmp}/dashboards" dashboards || fail "dashboards/ drifted - run: make -C k8s/observability dashboards"
python3 -I scripts/obs_tool.py prometheusrule --out "${tmp}/prometheusrule.yaml" >/dev/null
diff "${tmp}/prometheusrule.yaml" manifests/prometheusrule.yaml || fail "manifests/prometheusrule.yaml drifted - run: make -C k8s/observability render-rules"
echo "   OK"

echo "== 3. dashboard PromQL parses (promtool)"
python3 -I scripts/obs_tool.py promql-rules --out "${tmp}/dashboard-queries.rules.yml"
run_promtool "${tmp}" check rules dashboard-queries.rules.yml

echo "== 4. referenced metrics exist in the code"
python3 -I scripts/obs_tool.py check-metrics

echo "== 5. alert rules (promtool check + unit tests)"
run_promtool "${here}/alerts" check rules waddles-slo.rules.yml
run_promtool "${here}/alerts" test rules waddles-slo.rules.test.yml

echo "== 6. ServiceMonitor manifest"
python3 -I - <<'PY'
import sys
import yaml

doc = yaml.safe_load(open("manifests/servicemonitor.yaml"))
ok = (
    doc.get("kind") == "ServiceMonitor"
    and doc.get("apiVersion") == "monitoring.coreos.com/v1"
    and doc["spec"]["endpoints"][0]["port"] == "metrics"
    and doc["spec"]["selector"]["matchExpressions"][0]["values"]
)
if not ok:
    sys.exit("FAIL: manifests/servicemonitor.yaml has an unexpected shape")
print("   OK: %d component selector values" % len(doc["spec"]["selector"]["matchExpressions"][0]["values"]))
PY

echo "validate: ALL CHECKS PASSED"
