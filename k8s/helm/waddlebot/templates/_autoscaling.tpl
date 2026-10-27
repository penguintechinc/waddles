{{/*
Autoscaling / PodDisruptionBudget registry + helpers.

`waddlebot.scaleTargets` is the ONE list of Deployments that may be autoscaled or covered by
a PodDisruptionBudget. Singletons are deliberately absent and must stay absent:
  - svc-streaming    -- stateful RTMP/SRT ingest + local stream dir, Recreate singleton
  - svc-presentation -- overlay fan-out is in-process; >1 replica splits frames
  - postgres/redis/qdrant/ollama/seaweedfs -- single-writer stateful stores
  - collectors/svc-rtc -- per-platform connection owners
`hpa: false` entries get a PDB when multi-replica but never an HPA: svc-ingest* hold
single-connection leases (Discord gateway / Twitch IRC) so extra replicas add nothing;
egress-proxy is fixed-size by design (sized for the egress allowlist, not load).
*/}}
{{- define "waddlebot.scaleTargets" -}}
{{- $v := .Values -}}
{{- $rdp := $v.pipeline.rustDataPlane -}}
{{- $targets := list
  (dict "key" "hubApi" "name" "hub-api-v3" "component" "hub-api" "on" (not (not $v.pipeline.hubApi.enabled)) "replicas" (int $v.pipeline.hubApi.replicas) "hpa" true)
  (dict "key" "hubWebui" "name" "hub-webui-v3" "component" "hub-webui-v3" "on" (not (not $v.pipeline.hubWebui.enabled)) "replicas" (int $v.pipeline.hubWebui.replicas) "hpa" true)
  (dict "key" "svcCore" "name" "svc-core" "component" "svc-core" "on" (not (not $v.pipeline.svcCore.enabled)) "replicas" (int $v.pipeline.svcCore.replicas) "hpa" true)
  (dict "key" "svcProcess" "name" "svc-process" "component" "svc-process" "on" (not (not $v.pipeline.svcProcess.enabled)) "replicas" (int $v.pipeline.svcProcess.replicas) "hpa" true)
  (dict "key" "svcAction" "name" "svc-action" "component" "svc-action" "on" (not (not $v.pipeline.svcAction.enabled)) "replicas" (int $v.pipeline.svcAction.replicas) "hpa" true)
  (dict "key" "svcIngest" "name" "svc-ingest" "component" "svc-ingest" "on" (not (not $v.pipeline.svcIngest.enabled)) "replicas" (int $v.pipeline.svcIngest.replicas) "hpa" false)
  (dict "key" "svcProcessRust" "name" "svc-process-rust" "component" "svc-process-rust" "on" (not (not (and $rdp.enabled $rdp.svcProcess.enabled))) "replicas" (int $rdp.svcProcess.replicas) "hpa" true)
  (dict "key" "svcActionRust" "name" "svc-action-rust" "component" "svc-action-rust" "on" (not (not (and $rdp.enabled $rdp.svcAction.enabled))) "replicas" (int $rdp.svcAction.replicas) "hpa" true)
  (dict "key" "svcIngestRust" "name" "svc-ingest-rust" "component" "svc-ingest-rust" "on" (not (not (and $rdp.enabled $rdp.svcIngest.enabled))) "replicas" (int $rdp.svcIngest.replicas) "hpa" false)
  (dict "key" "bundleExecutor" "name" "bundle-executor" "component" "bundle-executor" "on" (not (not (and $rdp.enabled $rdp.bundleExecutor.enabled))) "replicas" (int $rdp.bundleExecutor.replicas) "hpa" true)
  (dict "key" "bundleExecutorAction" "name" "bundle-executor-action" "component" "bundle-executor-action" "on" (not (not (and $rdp.enabled $rdp.bundleExecutorAction.enabled))) "replicas" (int $rdp.bundleExecutorAction.replicas) "hpa" true)
  (dict "key" "reputation" "name" "reputation" "component" "reputation" "on" (not (not $v.reputation.enabled)) "replicas" (int $v.reputation.replicas) "hpa" true)
  (dict "key" "egressProxy" "name" "egress-proxy" "component" "egress-proxy" "on" (not (not $v.egressProxy.enabled)) "replicas" (int $v.egressProxy.replicas) "hpa" false)
  (dict "key" "router" "name" "router" "component" "router" "on" (not (not $v.modules.router.enabled)) "replicas" (int $v.modules.router.replicas) "hpa" true)
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
