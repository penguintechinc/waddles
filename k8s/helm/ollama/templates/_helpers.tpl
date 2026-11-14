{{/* Resource base name: the release name, suffixed with the chart name unless it already contains it. */}}
{{- define "ollama.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* Selector labels (immutable once installed): chart name + release + component passed in. */}}
{{- define "ollama.selectorLabels" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "ollama.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/part-of: waddleai
{{ include "ollama.selectorLabels" . }}
{{- end -}}

{{- define "ollama.image" -}}
{{- printf "%s:%s@%s" .Values.image.repository .Values.image.tag (required "image.digest is required (pin by digest)" .Values.image.digest) -}}
{{- end -}}

{{/* In-cluster base URL of the server Service. */}}
{{- define "ollama.url" -}}
{{- printf "http://%s.%s.svc:%d" (include "ollama.fullname" .) .Release.Namespace (.Values.service.port | int) -}}
{{- end -}}

{{/* Shared pod security context (rootless). */}}
{{- define "ollama.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .Values.securityContext.runAsUser }}
runAsGroup: {{ .Values.securityContext.runAsGroup }}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "ollama.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* DNS rules limited to cluster-local names (any depth 1..N before .cluster.local). */}}
{{- define "ollama.clusterDnsRules" -}}
{{- range $n := until (int .Values.network.clusterDnsMaxDepth) }}
- matchPattern: {{ printf "%s.cluster.local" (repeat (add $n 1 | int) "*." | trimSuffix ".") | quote }}
{{- end }}
{{- end -}}
