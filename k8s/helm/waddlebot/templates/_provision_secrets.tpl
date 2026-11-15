{{- /*
Spec-building helpers for templates/provision-secrets.yaml (the runtime create-if-absent hook).
`acc` is a dict {lines: list, names: list, inputs: dict, skipped: list} mutated through `set`;
every value that came from .Values travels via `inputs` (a hook-only Secret, never the spec
ConfigMap), the spec carries structure + non-secret config only. Spec grammar and policy:
files/provision-secrets.sh.
*/ -}}

{{- /* Open a Secret block. args: acc, name. */ -}}
{{- define "waddlebot.prov.secret" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "secret|%s|" .name)) -}}
{{- $_ := set .acc "names" (append .acc.names .name) -}}
{{- end -}}

{{- /* An externally owned Secret (existingSecret / externalSecret: true): logged, never touched. args: acc, name, reason. */ -}}
{{- define "waddlebot.prov.skip" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "secret|%s|skip=%s" .name .reason)) -}}
{{- $_ := set .acc "skipped" (append .acc.skipped .name) -}}
{{- end -}}

{{- /* Guard: spec fields are pipe-separated single lines. */ -}}
{{- define "waddlebot.prov.assertLine" -}}
{{- if or (contains "|" .v) (contains "\n" .v) (contains "\r" .v) -}}
{{- fail (printf "provision-secrets: value for %s must not contain '|' or newlines" .what) -}}
{{- end -}}
{{- end -}}

{{- /*
Generated-if-unset key. args: acc, secret, key, alg (b64url:32|alnum:N|hex:32|ring:<kid>),
opts (';'-separated, see script), explicit ("" or "REPLACE_ME*" = unset; a real value is handed to the hook through
the inputs Secret and wins over generation, but never over an existing real value).
*/ -}}
{{- define "waddlebot.prov.gen" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "key|%s|gen|%s|%s" .key .alg (.opts | default ""))) -}}
{{- $explicit := (.explicit | default "") | toString -}}
{{- if and (ne $explicit "") (not (hasPrefix "REPLACE_ME" $explicit)) -}}
{{- $_ := set .acc.inputs (printf "%s__%s" .secret .key) $explicit -}}
{{- end -}}
{{- end -}}

{{- /* Derived key (recomputed every run). args: acc, key, template, opts. Non-secret text + %%markers%% only. */ -}}
{{- define "waddlebot.prov.derived" -}}
{{- include "waddlebot.prov.assertLine" (dict "what" .key "v" .template) -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "key|%s|derived|%s|%s" .key .template (.opts | default ""))) -}}
{{- end -}}

{{- /* Owner/values pass-through that may be sensitive (kept in inputs). args: acc, secret, key, value. */ -}}
{{- define "waddlebot.prov.static" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "key|%s|static|" .key)) -}}
{{- $v := (.value | default "") | toString -}}
{{- if ne $v "" -}}
{{- $_ := set .acc.inputs (printf "%s__%s" .secret .key) $v -}}
{{- end -}}
{{- end -}}

{{- define "waddlebot.prov.blank" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "key|%s|blank|" .key)) -}}
{{- end -}}

{{- define "waddlebot.prov.marker" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "marker|%s|root-hmac-v1" .key)) -}}
{{- end -}}

{{- define "waddlebot.prov.tlsMeta" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "tls_meta|%s|%s" (.identity | default "") (.sansSha256 | default ""))) -}}
{{- end -}}

{{- define "waddlebot.prov.tlsCa" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "tls_ca|%s|%s|%s|%s" .caTag .key .cn (printf "%v%s" (.days | default 365) (ternary ",anytier" "" (.anyTier | default false))))) -}}
{{- $explicit := (.explicit | default "") | toString -}}
{{- if ne $explicit "" -}}
{{- $_ := set .acc.inputs (printf "%s__%s" .secret .key) $explicit -}}
{{- end -}}
{{- end -}}

{{- define "waddlebot.prov.tlsLeaf" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "tls_leaf|%s|%s|%s|%s|%s|%s|%s" .caTag .certKey .keyKey .cn (.dnsCsv | default "") (.uriCsv | default "") (printf "%v%s" (.days | default 365) (ternary ",anytier" "" (.anyTier | default false))))) -}}
{{- $ec := (.explicitCert | default "") | toString -}}
{{- $ek := (.explicitKey | default "") | toString -}}
{{- if and (ne $ec "") (ne $ek "") -}}
{{- $_ := set .acc.inputs (printf "%s__%s" .secret .certKey) $ec -}}
{{- $_ := set .acc.inputs (printf "%s__%s" .secret .keyKey) $ek -}}
{{- end -}}
{{- end -}}

{{- define "waddlebot.prov.tlsAlias" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "tls_alias|%s|%s" .key .targetKey)) -}}
{{- end -}}

{{- define "waddlebot.prov.tlsConcat" -}}
{{- $_ := set .acc "lines" (append .acc.lines (printf "tls_concat|%s|%s" .key .refs)) -}}
{{- end -}}
