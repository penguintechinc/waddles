{{/*
Resilience helpers: graceful shutdown (terminationGracePeriodSeconds + preStop drain) and
startupProbe, shared by every Deployment so the policy is defined once and tuned via values
(`shutdown.*`, `startupProbe.*`) instead of copy-pasted per template.
*/}}

{{/*
waddlebot.terminationGracePeriod -- pod-level `terminationGracePeriodSeconds`.
Gives the container time to drain in-flight work after the preStop sleep, before SIGKILL.
Call with the root context (`.`/`$`), or with (dict "root" $ "seconds" N) to override the
chart-wide `shutdown.terminationGracePeriodSeconds` for one workload (e.g. svc-streaming).
Fails the render if the grace period cannot cover the preStop sleep, since SIGKILL would
then land before the app ever receives SIGTERM.
*/}}
{{- define "waddlebot.terminationGracePeriod" -}}
{{- $root := . -}}
{{- $seconds := "" -}}
{{- if hasKey . "root" -}}
{{- $root = .root -}}
{{- $seconds = .seconds -}}
{{- end -}}
{{- $grace := int (default $root.Values.shutdown.terminationGracePeriodSeconds $seconds) -}}
{{- $sleep := int $root.Values.shutdown.preStopSleepSeconds -}}
{{- if le $grace $sleep -}}
{{- fail (printf "terminationGracePeriodSeconds (%d) must exceed shutdown.preStopSleepSeconds (%d)" $grace $sleep) -}}
{{- end -}}
terminationGracePeriodSeconds: {{ $grace }}
{{- end -}}

{{/*
waddlebot.preStop -- container-level `lifecycle.preStop` sleep so the pod is removed from
Service endpoints (and in-flight requests drain) before SIGTERM reaches the app. Uses the
native `sleep` lifecycle action on Kubernetes >= 1.30 (no `sleep` binary needed in
distroless/scratch images); falls back to `exec sleep` on older clusters. Renders nothing
when `shutdown.preStopSleepSeconds` is 0. Call with the root context.
*/}}
{{- define "waddlebot.preStop" -}}
{{- $sleep := int .Values.shutdown.preStopSleepSeconds -}}
{{- if gt $sleep 0 -}}
lifecycle:
  preStop:
{{- if semverCompare ">=1.30.0-0" .Capabilities.KubeVersion.Version }}
    sleep:
      seconds: {{ $sleep }}
{{- else }}
    exec:
      command: ["sleep", {{ $sleep | quote }}]
{{- end }}
{{- end -}}
{{- end -}}

{{/*
waddlebot.startupProbe -- slow-start protection: liveness/readiness only begin once this
passes, so a slow cold start (hub-api wait, WASM engine build, cold image) is not killed by
the liveness probe. Budget = failureThreshold x periodSeconds (default 30 x 5s = 150s),
tunable via `startupProbe.*`. Call with (dict "root" $ "path" "/health" "port" "http").
*/}}
{{- define "waddlebot.startupProbe" -}}
startupProbe:
  httpGet:
    path: {{ .path }}
    port: {{ .port }}
  periodSeconds: {{ .root.Values.startupProbe.periodSeconds }}
  timeoutSeconds: 5
  failureThreshold: {{ .root.Values.startupProbe.failureThreshold }}
{{- end -}}

{{/*
waddlebot.presentationReplicaGuard -- svc-presentation's overlay fan-out is IN-PROCESS: each
pod only knows the websocket/SSE clients connected to itself, so with >1 replica a push
frame lands on one pod and clients on the other pods never see it (frames split across
pods). Until the shared fan-out backend (roadmap item) exists, refuse to render more than
one replica rather than silently splitting frames.
*/}}
{{- define "waddlebot.presentationReplicaGuard" -}}
{{- if and (gt (int .Values.pipeline.svcPresentation.replicas) 1) (not .Values.pipeline.svcPresentation.sharedFanout.enabled) -}}
{{- fail "pipeline.svcPresentation.replicas > 1 requires pipeline.svcPresentation.sharedFanout.enabled=true: the overlay fan-out is in-process, so extra replicas split frames across pods. The shared fan-out backend is not built yet -- keep replicas: 1." -}}
{{- end -}}
{{- end -}}
