# Observability-as-code targets. Run from the repo root: make -C k8s/observability <target>
.PHONY: validate dashboards render-rules check-metrics

# Full gate: jq + drift + PromQL syntax + real-metric check + promtool check/test.
validate:
	bash scripts/validate.sh

# Regenerate dashboards/*.json from scripts/obs_tool.py.
dashboards:
	python3 -I scripts/obs_tool.py dashboards

# Regenerate manifests/prometheusrule.yaml from alerts/waddles-slo.rules.yml.
render-rules:
	python3 -I scripts/obs_tool.py prometheusrule

# Fail on any dashboard/alert metric the code does not emit.
check-metrics:
	python3 -I scripts/obs_tool.py check-metrics
