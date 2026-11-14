{{/*
OpenTelemetry (logs + metrics + traces) wiring -- ONE shared helper pair, driven by `global.otel`.

  waddlebot.otel.env         -- OTEL_* container env for one workload (single include line per template)
  waddlebot.otel.egressRule  -- the CiliumNetworkPolicy egress rule to the collector (one list item)

Rules (critical-rules.md Observability):
  - The destination comes ONLY from the standard OTEL_* env vars, rendered from `global.otel` values;
    nothing is hardcoded in a template or an image.
  - Disabled (`global.otel.enabled: false`, the base default) renders NO OTEL_* env and NO egress rule.
  - Intra-cluster = OTLP gRPC (:4317). A dead collector must never break a service: the endpoint is
    best-effort and OTEL_EXPORTER_OTLP_TIMEOUT caps how long one export may block.
  - No secret material: the SigNoz OTLP receiver is unauthenticated and reachable from in-cluster
    only. If an ingestion key is ever required, wire it from an existingSecret -- never a value here.

Order matters inside the env list: K8S_POD_NAME is defined BEFORE OTEL_RESOURCE_ATTRIBUTES
because Kubernetes only expands `$(VAR)` references to variables defined earlier in the list.
*/}}

{{/* "true" when global.otel.enabled, else empty. Call with the root context. */}}
{{- define "waddlebot.otel.enabled" -}}
{{- $o := (.Values.global).otel | default dict -}}
{{- if $o.enabled -}}true{{- end -}}
{{- end -}}

{{/*
OTEL_* env for ONE container. Call with
  (dict "root" $ "service" "hub-api")
Optional keys (svc-presentation's pre-existing per-service override only):
  "endpoint" / "protocol"  -- a per-service endpoint that wins over global.otel.endpoint
  "legacy" true            -- when global.otel is DISABLED keep emitting the workload's historical
                              OTEL_SERVICE_NAME + OTEL_RESOURCE_ATTRIBUTES (+ its own endpoint, if set)
*/}}
{{- define "waddlebot.otel.env" -}}
{{- $root := .root -}}
{{- $o := ($root.Values.global).otel | default dict -}}
{{- $svc := required "waddlebot.otel.env: service name is required" .service -}}
{{- if $o.enabled -}}
{{- $ep := .endpoint | default $o.endpoint -}}
{{- if not $ep -}}
{{- fail "global.otel.enabled=true requires global.otel.endpoint (OTLP collector URL, e.g. http://signoz-otel-collector.signoz.svc.cluster.local:4317)" -}}
{{- end -}}
{{- $proto := $o.protocol | default "grpc" -}}
{{- if .endpoint -}}{{- $proto = .protocol | default "grpc" -}}{{- end -}}
{{- $ex := $o.exporters | default dict -}}
{{- $attrs := list -}}
{{- $ra := $o.resourceAttributes | default dict -}}
{{- if not (hasKey $ra "deployment.environment") -}}
{{- $attrs = append $attrs (printf "deployment.environment=%s" ($o.environment | default $root.Values.global.deploymentTier | default "alpha")) -}}
{{- end -}}
{{- range $k, $v := $ra -}}
{{- if or (contains "," (toString $v)) (contains "," $k) -}}
{{- fail (printf "global.otel.resourceAttributes[%s]: commas are not allowed in OTEL_RESOURCE_ATTRIBUTES keys/values" $k) -}}
{{- end -}}
{{- $attrs = append $attrs (printf "%s=%s" $k (toString $v)) -}}
{{- end -}}
{{- $attrs = append $attrs "k8s.pod.name=$(K8S_POD_NAME)" -}}
{{- $ratio := $o.samplerRatio | default 1.0 -}}
{{- if or (lt (float64 $ratio) 0.0) (gt (float64 $ratio) 1.0) -}}
{{- fail (printf "global.otel.samplerRatio must be within 0..1, got %v" $ratio) -}}
{{- end }}
- name: K8S_POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: OTEL_SERVICE_NAME
  value: {{ $svc | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ join "," $attrs | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ $ep | quote }}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: {{ $proto | quote }}
- name: OTEL_EXPORTER_OTLP_TIMEOUT
  value: {{ $o.exportTimeoutMs | default 5000 | quote }}
- name: OTEL_TRACES_EXPORTER
  value: {{ $ex.traces | default "otlp" | quote }}
- name: OTEL_METRICS_EXPORTER
  value: {{ $ex.metrics | default "otlp" | quote }}
- name: OTEL_LOGS_EXPORTER
  value: {{ $ex.logs | default "otlp" | quote }}
- name: OTEL_TRACES_SAMPLER
  value: {{ $o.sampler | default "parentbased_traceidratio" | quote }}
- name: OTEL_TRACES_SAMPLER_ARG
  value: {{ $ratio | toString | quote }}
- name: OTEL_METRIC_EXPORT_INTERVAL
  value: {{ $o.metricExportIntervalMs | default 15000 | quote }}
{{- else if .legacy }}
- name: OTEL_SERVICE_NAME
  value: {{ $svc | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "deployment.environment=%s" ($root.Values.global.deploymentTier | default "alpha") | quote }}
{{- if .endpoint }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .endpoint | quote }}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: {{ .protocol | default "grpc" | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
CiliumNetworkPolicy egress rule (ONE list item, unindented) allowing a workload to reach the
OTLP collector in another namespace by namespace + pod labels, on the collector ports only.
Emits nothing unless global.otel.enabled AND global.otel.egress.enabled. Call with
  (dict "root" $ "component" "<component>")   -- "component" optional: when set, only the workloads
listed in waddlebot.otel.emitters get the rule (databases/caches/webui never reach the collector);
omit it for a policy that already selects only emitters (dataplane-egress-deny, egress-proxy, executor).

It is included INSIDE every egress policy that already default-denies a workload (workloadPolicies,
dataplane-egress-deny, egress-proxy, executor) rather than rendered as a standalone CNP: a standalone
CNP that selects a pod with no other egress policy would itself flip that pod to default-deny egress
(alpha/local run workloadEgress.enabled=false). Cilium unions allows, so this only ever adds.
*/}}
{{- define "waddlebot.otel.egressRule" -}}
{{- $root := .root -}}
{{- $o := ($root.Values.global).otel | default dict -}}
{{- $eg := $o.egress | default dict -}}
{{- if and $o.enabled $eg.enabled (or (not .component) (has .component (include "waddlebot.otel.emitters" $root | fromJsonArray))) -}}
{{- $col := $o.collector | default dict -}}
- toEndpoints:
    - matchLabels:
        io.kubernetes.pod.namespace: {{ $col.namespace | default "signoz" | quote }}
        {{- range $k, $v := ($col.podLabels | default dict) }}
        {{ $k }}: {{ $v | quote }}
        {{- end }}
  toPorts:
    - ports:
        {{- range ($col.ports | default (list 4317)) }}
        - port: {{ . | quote }}
          protocol: TCP
        {{- end }}
{{- end -}}
{{- end -}}

{{/* JSON list of the component labels that carry OTEL_* env (waddlebot.otel.env call sites). */}}
{{- define "waddlebot.otel.emitters" -}}
{{- list "hub-api" "svc-ingest-rust" "svc-process-rust" "svc-action-rust" "svc-bundles-executor" "svc-presentation" "svc-streaming" "egress-proxy" | toJson -}}
{{- end -}}
