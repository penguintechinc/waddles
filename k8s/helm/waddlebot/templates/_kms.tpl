{{/*
Enterprise external KMS / BYOK helpers (values subtree: `kms`; manifests: templates/kms.yaml).

External KMS is the Enterprise-tier upsell ON TOP OF the platform-managed at-rest
baseline (security.md Encryption: Storage) -- never a substitute. Everything here is
default OFF and renders NOTHING while `kms.enabled=false`, so the baseline keeps
working with zero configuration. Provider credentials are only ever referenced from
pre-created Secrets (`existingSecret` pattern); no credential value lives in values.

Two independent features share the master switch:
  kms.tenantKeys    hub-api: per-tenant customer-managed keys (AWS KMS / Google Cloud
                    KMS / Azure Key Vault) wrapping each tenant's data-encryption keys
                    (hub_api/services/envelope/). Licence gating happens at RUNTIME per
                    tenant (`compliance.external_kms`, Enterprise) -- Helm cannot ask the
                    licence server, so this chart only wires the providers.
  kms.objectStorage SeaweedFS: SSE-KMS under the operator's key (aws / gcp providers --
                    SeaweedFS 4.x has no native Azure provider). `mode` is
                    off | kms | drain; `drain` is the ungated exit ramp.

The shared files (hub-api.yaml, infrastructure/seaweedfs.yaml) carry one-line `include`
hooks into the helpers below; all KMS logic lives in this file.
*/}}

{{/* "true" iff per-tenant BYOK is requested AND the master switch is on (empty string otherwise). */}}
{{- define "waddlebot.kms.tenantKeysEnabled" -}}
{{- if and .Values.kms.enabled .Values.kms.tenantKeys.enabled -}}true{{- end -}}
{{- end -}}

{{/* "true" iff SeaweedFS must load a KMS provider (mode kms OR drain). */}}
{{- define "waddlebot.kms.objectStorageProvider" -}}
{{- if and .Values.kms.enabled (ne (.Values.kms.objectStorage.mode | default "off") "off") -}}true{{- end -}}
{{- end -}}

{{/* "true" iff the bucket default / new writes must use the KMS key (mode kms only). */}}
{{- define "waddlebot.kms.objectStorageActive" -}}
{{- if and .Values.kms.enabled (eq (.Values.kms.objectStorage.mode | default "off") "kms") -}}true{{- end -}}
{{- end -}}

{{/*
Fail-closed validation. Included from templates/kms.yaml, which renders on every
`helm template`/`install`, so a misconfiguration can never be silently ignored.
*/}}
{{- define "waddlebot.kms.validate" -}}
{{- $k := .Values.kms -}}
{{- $os := $k.objectStorage -}}
{{- $mode := $os.mode | default "off" -}}
{{- if not (has $mode (list "off" "kms" "drain")) -}}
{{- fail (printf "kms.objectStorage.mode=%q is invalid -- use off | kms | drain" $mode) -}}
{{- end -}}
{{- if and (not $k.enabled) (or $k.tenantKeys.enabled (ne $mode "off")) -}}
{{- fail "kms.tenantKeys.enabled / kms.objectStorage.mode request external KMS but kms.enabled=false (master switch) -- refusing to silently ignore a requested KMS feature. Set kms.enabled=true, or turn the sub-feature off." -}}
{{- end -}}
{{- if $k.enabled -}}
{{- if and (not $k.tenantKeys.enabled) (eq $mode "off") -}}
{{- fail "kms.enabled=true but neither kms.tenantKeys.enabled nor kms.objectStorage.mode is set -- nothing to configure. Enable one, or set kms.enabled=false." -}}
{{- end -}}
{{- if $k.tenantKeys.enabled -}}
{{- $providers := $k.tenantKeys.providers | default list -}}
{{- if not $providers -}}
{{- fail "kms.tenantKeys.enabled=true requires kms.tenantKeys.providers (one or more of: aws_kms, gcp_kms, azure_key_vault)" -}}
{{- end -}}
{{- range $providers -}}
{{- if not (has . (list "aws_kms" "gcp_kms" "azure_key_vault")) -}}
{{- fail (printf "kms.tenantKeys.providers contains %q -- valid: aws_kms, gcp_kms, azure_key_vault" .) -}}
{{- end -}}
{{- end -}}
{{- if and (has "azure_key_vault" $providers) (not $k.tenantKeys.azure.credentialsSecret) -}}
{{- fail "kms.tenantKeys.providers includes azure_key_vault but kms.tenantKeys.azure.credentialsSecret is empty -- pre-create a Secret with keys ENVELOPE_AZURE_CLIENT_ID and ENVELOPE_AZURE_CLIENT_SECRET and reference it (existingSecret; no credential values in values.yaml)" -}}
{{- end -}}
{{- range $name, $principal := dict "aws.platformPrincipal" $k.tenantKeys.aws.platformPrincipal "gcp.platformPrincipal" $k.tenantKeys.gcp.platformPrincipal -}}
{{- if and $principal (not (regexMatch "^[A-Za-z0-9:/_.+=,@-]{1,512}$" $principal)) -}}
{{- fail (printf "kms.tenantKeys.%s must be an ARN / service-account email with no spaces (got %q)" $name $principal) -}}
{{- end -}}
{{- end -}}
{{- range $name, $url := dict "aws.kmsEndpointUrl" $k.tenantKeys.aws.kmsEndpointUrl "aws.stsEndpointUrl" $k.tenantKeys.aws.stsEndpointUrl "gcp.kmsEndpoint" $k.tenantKeys.gcp.kmsEndpoint "azure.authority" $k.tenantKeys.azure.authority -}}
{{- if and $url (not (hasPrefix "https://" $url)) -}}
{{- fail (printf "kms.tenantKeys.%s must be an https:// URL (got %q)" $name $url) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if ne $mode "off" -}}
{{- if not .Values.infrastructure.seaweedfs.enabled -}}
{{- fail "kms.objectStorage.mode requires infrastructure.seaweedfs.enabled=true" -}}
{{- end -}}
{{- if not .Values.infrastructure.seaweedfs.encryption.enabled -}}
{{- fail "kms.objectStorage is ON TOP of the platform baseline: infrastructure.seaweedfs.encryption.enabled must stay true (external KMS is never a substitute for at-rest encryption)" -}}
{{- end -}}
{{- if eq $os.provider "azure_key_vault" -}}
{{- fail "kms.objectStorage.provider=azure_key_vault is not supported: SeaweedFS's S3 gateway ships aws and gcp KMS providers only. Use aws_kms or gcp_kms for object storage (azure_key_vault remains available for kms.tenantKeys)." -}}
{{- end -}}
{{- if not (has $os.provider (list "aws_kms" "gcp_kms")) -}}
{{- fail (printf "kms.objectStorage.provider=%q is invalid -- use aws_kms or gcp_kms" $os.provider) -}}
{{- end -}}
{{- if not (regexMatch "^[A-Za-z0-9:/_.+=,@-]{1,512}$" ($os.keyId | default "")) -}}
{{- fail "kms.objectStorage.keyId is required (AWS key ARN/alias, or GCP CryptoKey resource name) and may only contain [A-Za-z0-9:/_.+=,@-]" -}}
{{- end -}}
{{- if eq $os.provider "aws_kms" -}}
{{- if not $os.aws.region -}}
{{- fail "kms.objectStorage.aws.region is required" -}}
{{- end -}}
{{- if not $os.aws.credentialsSecret.name -}}
{{- fail "kms.objectStorage.aws.credentialsSecret.name is required (existingSecret holding the AWS access key id / secret access key for the SeaweedFS KMS client)" -}}
{{- end -}}
{{- if and $os.aws.endpoint (not (hasPrefix "https://" $os.aws.endpoint)) -}}
{{- fail "kms.objectStorage.aws.endpoint must be an https:// URL" -}}
{{- end -}}
{{- end -}}
{{- if eq $os.provider "gcp_kms" -}}
{{- if not $os.gcp.projectId -}}
{{- fail "kms.objectStorage.gcp.projectId is required (SeaweedFS's Google Cloud KMS provider needs project_id)" -}}
{{- end -}}
{{- if not $os.gcp.credentialsSecret.name -}}
{{- fail "kms.objectStorage.gcp.credentialsSecret.name is required (existingSecret holding a service-account key JSON)" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
hub-api `envFrom` entries (included from templates/hub-api.yaml). Renders nothing while
the master switch or tenantKeys is off. Secret key names ARE the environment variable
names (same convention as the platform-credentials Secret):
  tenant KEK (baseline)   TENANT_KEK_HEX                         (autoProvisionedKeys.tenantKek)
  AWS (non-IRSA only)     AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  GCP (omit = Workload Identity / metadata server)   ENVELOPE_GCP_CREDENTIALS_JSON
  Azure                   ENVELOPE_AZURE_CLIENT_ID, ENVELOPE_AZURE_CLIENT_SECRET
The credential Secrets are NOT optional once referenced: a missing Secret fails the pod
loudly at start instead of booting a provider that can never authenticate.
*/}}
{{- define "waddlebot.kms.hubApiEnvFrom" -}}
{{- if or (include "waddlebot.kms.tenantKeysEnabled" .) (include "waddlebot.kms.objectStorageActive" .) }}
- configMapRef:
    name: {{ include "waddlebot.fullname" . }}-kms
{{- end }}
{{- if include "waddlebot.kms.tenantKeysEnabled" . }}
{{- $t := .Values.kms.tenantKeys }}
- secretRef:
    name: {{ .Values.autoProvisionedKeys.tenantKek.secretName }}
{{- with $t.aws.credentialsSecret }}
- secretRef:
    name: {{ . }}
{{- end }}
{{- with $t.gcp.credentialsSecret }}
- secretRef:
    name: {{ . }}
{{- end }}
{{- with $t.azure.credentialsSecret }}
- secretRef:
    name: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{/* SeaweedFS container `env` entries: the KMS client's credentials, from the existingSecret only. */}}
{{- define "waddlebot.kms.seaweedfsEnv" -}}
{{- if and (include "waddlebot.kms.objectStorageProvider" .) (eq .Values.kms.objectStorage.provider "aws_kms") }}
{{- $c := .Values.kms.objectStorage.aws.credentialsSecret }}
- name: KMS_AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ $c.name }}
      key: {{ $c.accessKeyIdKey | default "AWS_ACCESS_KEY_ID" }}
- name: KMS_AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $c.name }}
      key: {{ $c.secretAccessKeyKey | default "AWS_SECRET_ACCESS_KEY" }}
{{- end }}
{{- end -}}

{{/* SeaweedFS pod `volumes` (GCP service-account key file). */}}
{{- define "waddlebot.kms.seaweedfsVolumes" -}}
{{- if and (include "waddlebot.kms.objectStorageProvider" .) (eq .Values.kms.objectStorage.provider "gcp_kms") }}
- name: seaweedfs-kms-gcp
  secret:
    secretName: {{ .Values.kms.objectStorage.gcp.credentialsSecret.name }}
    defaultMode: 0400
    items:
    - key: {{ .Values.kms.objectStorage.gcp.credentialsSecret.key | default "credentials.json" }}
      path: credentials.json
{{- end }}
{{- end -}}

{{/* SeaweedFS container `volumeMounts` (GCP service-account key file, read-only). */}}
{{- define "waddlebot.kms.seaweedfsVolumeMounts" -}}
{{- if and (include "waddlebot.kms.objectStorageProvider" .) (eq .Values.kms.objectStorage.provider "gcp_kms") }}
- name: seaweedfs-kms-gcp
  mountPath: /etc/seaweedfs-kms
  readOnly: true
{{- end }}
{{- end -}}

{{/*
The `kms` section appended to SeaweedFS's S3 config JSON (`-s3.config`). Rendered inside the
shell heredoc in seaweedfs.yaml, so `${KMS_AWS_*}` expand from the env vars above at container
start -- credentials never appear in the manifest. Provider fields are FLAT under the provider
object (verified against chrislusf/seaweedfs 4.48: a nested "config" object is ignored).
Starts with a comma so it can follow the closing `]` of "identities".
*/}}
{{- define "waddlebot.kms.seaweedfsConfigBlock" -}}
{{- if include "waddlebot.kms.objectStorageProvider" . }}
{{- $os := .Values.kms.objectStorage }}
,
"kms": {
  "default_provider": "customer-managed",
  "providers": {
    "customer-managed": {
{{- if eq $os.provider "aws_kms" }}
      "type": "aws",
      "region": {{ $os.aws.region | quote }},
{{- if $os.aws.endpoint }}
      "endpoint": {{ $os.aws.endpoint | quote }},
{{- end }}
      "access_key": "${KMS_AWS_ACCESS_KEY_ID}",
      "secret_key": "${KMS_AWS_SECRET_ACCESS_KEY}"
{{- else }}
      "type": "gcp",
      "project_id": {{ $os.gcp.projectId | quote }},
      "credentials_file": "/etc/seaweedfs-kms/credentials.json"
{{- end }}
    }
  }
}
{{- end }}
{{- end -}}

{{/*
`put-bucket-encryption` configuration JSON for the bucket-init hook. Baseline (SSE-S3 / AES256)
unless kms.objectStorage.mode=kms, in which case the bucket default becomes aws:kms under the
operator's key -- Rust writers (svc-presentation, svc-streaming, bundle seeder) set no per-object
SSE header and rely on this default. `drain` deliberately returns to AES256 while the provider
stays loaded so objects already written under KMS remain readable.
*/}}
{{- define "waddlebot.kms.bucketEncryptionConfig" -}}
{{- if include "waddlebot.kms.objectStorageActive" . -}}
{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":{{ .Values.kms.objectStorage.keyId | quote }}}}]}
{{- else -}}
{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}
{{- end -}}
{{- end -}}
