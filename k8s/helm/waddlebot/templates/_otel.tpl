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
Optional keys:
  "extraResourceAttributes" -- dict merged into OTEL_RESOURCE_ATTRIBUTES (wins over global.otel.resourceAttributes,
                              never duplicates a key). Executor pools pass executor.pool / executor.language_class /
                              service.instance.id here; service.name stays ONE per component (never per pool).
svc-presentation's pre-existing per-service override only:
  "endpoint" / "protocol"  -- a per-service endpoint that wins over global.otel.endpoint
  "legacy" true            -- when global.otel is DISABLED keep emitting the workload's historical
                              OTEL_SERVICE_NAME + OTEL_RESOURCE_ATTRIBUTES (+ its own endpoint, if set)
*/}}
{{- define "waddlebot.otel.env" -}}
{{- $root := .root -}}
{{- $o := ($root.Values.global).otel | default dict -}}
{{- $svc := required "waddlebot.otel.env: service name is required" .service -}}
{{- if and $o.enabled (include "waddlebot.otel.emits" (dict "root" $root "service" $svc)) -}}
{{- $ep := .endpoint | default $o.endpoint -}}
{{- if not $ep -}}
{{- fail "global.otel.enabled=true requires global.otel.endpoint (OTLP collector URL, e.g. http://signoz-otel-collector.signoz.svc.cluster.local:4317)" -}}
{{- end -}}
{{- $proto := $o.protocol | default "grpc" -}}
{{- if .endpoint -}}{{- $proto = .protocol | default "grpc" -}}{{- end -}}
{{- $ex := $o.exporters | default dict -}}
{{- /* Merge order (later wins, so no duplicate keys): deployment.environment default, global.otel.resourceAttributes,
       caller extraResourceAttributes. Render: deployment.environment + service.namespace first, the rest sorted,
       k8s.pod.name last. */ -}}
{{- $merged := dict "deployment.environment" ($o.environment | default $root.Values.global.deploymentTier | default "alpha") -}}
{{- range $k, $v := ($o.resourceAttributes | default dict) -}}{{- $_ := set $merged $k (toString $v) -}}{{- end -}}
{{- range $k, $v := (.extraResourceAttributes | default dict) -}}{{- $_ := set $merged $k (toString $v) -}}{{- end -}}
{{- if not (hasKey $merged "k8s.pod.name") -}}{{- $_ := set $merged "k8s.pod.name" "$(K8S_POD_NAME)" -}}{{- end -}}
{{- $attrs := list -}}
{{- $first := list "deployment.environment" "service.namespace" -}}
{{- $last := list "k8s.pod.name" -}}
{{- range $k, $v := $merged -}}
{{- if or (contains "," (toString $v)) (contains "," $k) (contains "=" $k) -}}
{{- fail (printf "OTEL resource attribute %q: commas/'=' are not allowed in OTEL_RESOURCE_ATTRIBUTES keys/values" $k) -}}
{{- end -}}
{{- end -}}
{{- range $k := $first -}}{{- if hasKey $merged $k -}}{{- $attrs = append $attrs (printf "%s=%s" $k (index $merged $k)) -}}{{- end -}}{{- end -}}
{{- range $k := (keys $merged | sortAlpha) -}}
{{- if not (or (has $k $first) (has $k $last)) -}}{{- $attrs = append $attrs (printf "%s=%s" $k (index $merged $k)) -}}{{- end -}}
{{- end -}}
{{- range $k := $last -}}{{- if hasKey $merged $k -}}{{- $attrs = append $attrs (printf "%s=%s" $k (index $merged $k)) -}}{{- end -}}{{- end -}}
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
{{- $hs := $o.headersSecretRef | default dict }}
{{- if $hs.name }}
{{- /* Receiver auth (bearer token / API-key header). Value only ever comes from the referenced Secret, never from
       values; optional so a missing Secret can never block a pod from starting (a dead exporter must not break the app). */}}
- name: OTEL_EXPORTER_OTLP_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ $hs.name | quote }}
      key: {{ $hs.key | default "headers" | quote }}
      optional: true
{{- end }}
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
omit it for a policy that already selects only emitters (dataplane-egress-deny). The egress-proxy and
executor CNPs pass their component so the per-emitter flags below apply.

It is included INSIDE every egress policy that already default-denies a workload (workloadPolicies,
dataplane-egress-deny, egress-proxy, executor) rather than rendered as a standalone CNP: a standalone
CNP that selects a pod with no other egress policy would itself flip that pod to default-deny egress
(alpha/local run workloadEgress.enabled=false). Cilium unions allows, so this only ever adds.
*/}}
{{- define "waddlebot.otel.egressRule" -}}
{{- $root := .root -}}
{{- $o := ($root.Values.global).otel | default dict -}}
{{- $eg := $o.egress | default dict -}}
{{- if and $o.enabled $eg.enabled (or (not .component) (has .component (include "waddlebot.otel.emitters" $root | fromJsonArray))) (or (not .component) (include "waddlebot.otel.emits" (dict "root" $root "service" .component))) -}}
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

{{/*
Per-emitter opt-in/out, beyond global.otel.enabled. Call with (dict "root" $ "service" "<component>");
returns "true" or empty.
  egress-proxy          OPT-IN  (global.otel.egress.egressProxy, default false). Its CNP is the SSRF backstop for the
                                bundle-directed dialer ("world" minus every cluster-internal range); a collector allow
                                there would let an SSRF reach the collector and inject telemetry. Its stdout logs still
                                reach SigNoz via the signoz-agents infra collection; its traces/metrics stay off.
  svc-bundles-executor  opt-out (global.otel.egress.bundleExecutor, default true). The pod is the trusted HOST process;
                                guest WASM components have no sockets (all I/O is host capabilities), so the exporter
                                lives in trusted host code and the rule is scoped to the collector endpoints only.
Both the OTEL env and the egress rule follow the flag, so a pod never exports toward a destination it cannot reach.
*/}}
{{- define "waddlebot.otel.emits" -}}
{{- $o := (.root.Values.global).otel | default dict -}}
{{- $eg := $o.egress | default dict -}}
{{- if $o.enabled -}}
{{- if eq .service "egress-proxy" -}}{{- if $eg.egressProxy -}}true{{- end -}}
{{- else if eq .service "svc-bundles-executor" -}}{{- if ne (toString (index $eg "bundleExecutor")) "false" -}}true{{- end -}}
{{- else -}}true{{- end -}}
{{- end -}}
{{- end -}}
