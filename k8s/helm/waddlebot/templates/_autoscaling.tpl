{{/*
Autoscaling / PodDisruptionBudget registry + helpers.

`waddlebot.scaleTargets` is the ONE list of Deployments that may be autoscaled or covered by
a PodDisruptionBudget. Singletons are deliberately absent and must stay absent:
  - svc-streaming    -- stateful RTMP/SRT ingest + local stream dir, Recreate singleton
  - svc-presentation -- overlay fan-out is in-process; >1 replica splits frames
  - postgres/redis/qdrant/ollama/seaweedfs -- single-writer stateful stores
  - collectors/svc-rtc -- per-platform connection owners
`hpa: false` entries get a PDB when multi-replica but never an HPA: svc-ingest-rust holds
single-connection leases (Discord gateway / Twitch IRC) so extra replicas add nothing;
egress-proxy is fixed-size by design (sized for the egress allowlist, not load).
svc-bundles-executor is NOT in this registry: it is a StatefulSet (stable pod names =
executor_id for slot sharding) with its own per-pool PDB + KEDA/HPA-external autoscaling in
templates/svc-bundles-executor-autoscaling.yaml -- CPU/memory HPA was blind for it (no
metrics API) and the wrong signal anyway (queue wait / pool exhaustion are the real ones).
*/}}

{{/*
waddlebot.executorPools -- JSON list of executor pools (one StatefulSet each). Empty
`bundleExecutor.pools` => ONE pool "default" serving every stage, named exactly
`<fullname>-svc-bundles-executor` (so executor_id = `<fullname>-svc-bundles-executor-<ordinal>`).
A pool entry: {name, stages: [stage targets], replicas?, resources?, memoryBudgetMb?,
autoscaling?}. Non-default pools are suffixed `-<name>`. Each stage must belong to exactly one
pool (sharding computes owners over the executors CONNECTED to a stage, so a stage split
across pools would mix two membership sets).
*/}}
{{- define "waddlebot.executorPools" -}}
{{- $be := .Values.pipeline.rustDataPlane.bundleExecutor -}}
{{- $all := $be.stages | default list -}}
{{- if not $all -}}
{{- fail "pipeline.rustDataPlane.bundleExecutor.stages must list at least one stage ({target, port}) for svc-bundles-executor to dial" -}}
{{- end -}}
{{- $base := dict "replicas" (int $be.replicas) "resources" $be.resources "memoryBudgetMb" (int $be.memoryBudgetMb) "autoscaling" ($be.autoscaling | default dict) -}}
{{- $out := list -}}
{{- if not $be.pools -}}
{{- $out = append $out (merge (dict "name" "default" "suffix" "" "stages" $all) (deepCopy $base)) -}}
{{- else -}}
{{- $seen := dict -}}
{{- range $p := $be.pools -}}
{{- if not (regexMatch "^[a-z0-9]([a-z0-9-]{0,14}[a-z0-9])?$" ($p.name | default "")) -}}
{{- fail (printf "bundleExecutor.pools[].name %q must be a DNS label of <=16 chars" ($p.name | default "")) -}}
{{- end -}}
{{- $st := list -}}
{{- range $t := $p.stages -}}
{{- $found := false -}}
{{- range $s := $all -}}{{- if eq $s.target $t -}}{{- $st = append $st $s -}}{{- $found = true -}}{{- end -}}{{- end -}}
{{- if not $found -}}{{- fail (printf "bundleExecutor.pools[%s].stages: %q is not in bundleExecutor.stages" $p.name $t) -}}{{- end -}}
{{- if hasKey $seen $t -}}{{- fail (printf "bundleExecutor.pools: stage %q is assigned to more than one pool" $t) -}}{{- end -}}
{{- $_ := set $seen $t true -}}
{{- end -}}
{{- if not $st -}}{{- fail (printf "bundleExecutor.pools[%s].stages must list at least one stage" $p.name) -}}{{- end -}}
{{- $suffix := ternary "" (printf "-%s" $p.name) (eq $p.name "default") -}}
{{- $pool := dict "name" $p.name "suffix" $suffix "stages" $st "replicas" (int ($p.replicas | default $base.replicas)) "resources" ($p.resources | default $base.resources) "memoryBudgetMb" (int ($p.memoryBudgetMb | default $base.memoryBudgetMb)) "autoscaling" (merge (deepCopy ($p.autoscaling | default dict)) (deepCopy $base.autoscaling)) -}}
{{- $out = append $out $pool -}}
{{- end -}}
{{- range $s := $all -}}
{{- if not (hasKey $seen $s.target) -}}{{- fail (printf "bundleExecutor.pools: stage %q is not served by any pool" $s.target) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{/*
waddlebot.memToMi -- integer MiB for a K8s memory quantity with a Gi/Mi suffix (the only
two the executor sizing accepts; anything else fails the render loudly).
*/}}
{{- define "waddlebot.memToMi" -}}
{{- $q := toString . -}}
{{- if hasSuffix "Gi" $q -}}{{- mul (int (trimSuffix "Gi" $q)) 1024 -}}
{{- else if hasSuffix "Mi" $q -}}{{- int (trimSuffix "Mi" $q) -}}
{{- else -}}{{- fail (printf "svc-bundles-executor memory limit %q must use a Gi or Mi suffix" $q) -}}
{{- end -}}
{{- end -}}

{{/*
waddlebot.rustOtelEnv -- OTLP exporter env for a Rust data-plane container. Endpoint comes
from values only, never hardcoded: `otel.exporter.endpoint` wins; else, when the in-chart
collector is enabled, its in-namespace Service; else NO endpoint is rendered and the process
runs with telemetry export off (a dead/absent exporter never breaks the app).
Call with (dict "root" $ "service" "svc-bundles-executor").
*/}}
{{- define "waddlebot.otlpEndpoint" -}}
{{- $o := .Values.otel -}}
{{- if ($o.exporter).endpoint -}}{{- $o.exporter.endpoint -}}
{{- else if ($o.collector).enabled -}}{{- printf "http://%s-otel-collector:%v" (include "waddlebot.fullname" .) .Values.egressPolicy.inCluster.otelGrpcPort -}}
{{- end -}}
{{- end -}}

{{- define "waddlebot.rustOtelEnv" -}}
{{- $root := .root -}}
{{- $ex := $root.Values.otel.exporter | default dict -}}
- name: OTEL_SERVICE_NAME
  value: {{ .service | quote }}
- name: K8S_POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: OTEL_RESOURCE_ATTRIBUTES
  value: "deployment.environment={{ $root.Values.global.deploymentTier | default "alpha" }},k8s.pod.name=$(K8S_POD_NAME)"
{{- $ep := include "waddlebot.otlpEndpoint" $root }}
{{- if $ep }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ $ep | quote }}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: {{ $ex.protocol | default "grpc" | quote }}
- name: OTEL_METRIC_EXPORT_INTERVAL
  value: {{ $ex.metricExportIntervalMs | default 15000 | quote }}
{{- end }}
{{- end -}}
{{- define "waddlebot.scaleTargets" -}}
{{- $v := .Values -}}
{{- $rdp := $v.pipeline.rustDataPlane -}}
{{- $targets := list
  (dict "key" "hubApi" "name" "hub-api-v3" "component" "hub-api" "on" (not (not $v.pipeline.hubApi.enabled)) "replicas" (int $v.pipeline.hubApi.replicas) "hpa" true)
  (dict "key" "hubWebui" "name" "hub-webui-v3" "component" "hub-webui-v3" "on" (not (not $v.pipeline.hubWebui.enabled)) "replicas" (int $v.pipeline.hubWebui.replicas) "hpa" true)
  (dict "key" "svcCore" "name" "svc-core" "component" "svc-core" "on" (not (not $v.pipeline.svcCore.enabled)) "replicas" (int $v.pipeline.svcCore.replicas) "hpa" true)
  (dict "key" "svcProcessRust" "name" "svc-process-rust" "component" "svc-process-rust" "on" (not (not (and $rdp.enabled $rdp.svcProcess.enabled))) "replicas" (int $rdp.svcProcess.replicas) "hpa" true)
  (dict "key" "svcActionRust" "name" "svc-action-rust" "component" "svc-action-rust" "on" (not (not (and $rdp.enabled $rdp.svcAction.enabled))) "replicas" (int $rdp.svcAction.replicas) "hpa" true)
  (dict "key" "svcIngestRust" "name" "svc-ingest-rust" "component" "svc-ingest-rust" "on" (not (not (and $rdp.enabled $rdp.svcIngest.enabled))) "replicas" (int $rdp.svcIngest.replicas) "hpa" false)
  (dict "key" "egressProxy" "name" "egress-proxy" "component" "egress-proxy" "on" (not (not $v.egressProxy.enabled)) "replicas" (int $v.egressProxy.replicas) "hpa" false)
-}}
{{- $targets | toJson -}}
{{- end -}}

{{/*
waddlebot.hpaCfg -- effective HPA settings (JSON) for one service key: per-service entry in
`autoscaling.services.<key>` over `autoscaling.defaults`. `enabled` is resolved explicitly
(not via merge) because mergo treats an explicit `false` as unset and would override it.
Call with (dict "root" $ "key" "hubApi").
*/}}
{{- define "waddlebot.hpaCfg" -}}
{{- $defaults := .root.Values.autoscaling.defaults -}}
{{- $svc := (index (.root.Values.autoscaling.services | default dict) .key) | default dict -}}
{{- $cfg := merge (deepCopy $svc) (deepCopy $defaults) -}}
{{- $_ := set $cfg "enabled" (ternary $svc.enabled $defaults.enabled (hasKey $svc "enabled")) -}}
{{- $cfg | toJson -}}
{{- end -}}

{{/*
waddlebot.hpaManaged -- renders "true" when an HPA owns this Deployment's replica count, so
the Deployment must omit `spec.replicas` (otherwise every helm upgrade resets the HPA's
scale). Empty otherwise. Call with (dict "root" $ "key" "hubApi").
*/}}
{{- define "waddlebot.hpaManaged" -}}
{{- $cfg := include "waddlebot.hpaCfg" . | fromJson -}}
{{- if $cfg.enabled -}}true{{- end -}}
{{- end -}}

{{/*
waddlebot.pdbCfg -- effective PodDisruptionBudget settings (JSON) for one service key:
per-service `podDisruptionBudget.services.<key>` over the global `podDisruptionBudget.*`.
Call with (dict "root" $ "key" "hubApi").
*/}}
{{- define "waddlebot.pdbCfg" -}}
{{- $g := .root.Values.podDisruptionBudget -}}
{{- $svc := (index ($g.services | default dict) .key) | default dict -}}
{{- $cfg := dict "enabled" (ternary $svc.enabled $g.enabled (hasKey $svc "enabled")) "minAvailable" (default $g.minAvailable $svc.minAvailable) "maxUnavailable" (default $g.maxUnavailable $svc.maxUnavailable) -}}
{{- $cfg | toJson -}}
{{- end -}}
