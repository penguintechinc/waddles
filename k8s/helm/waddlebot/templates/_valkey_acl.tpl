{{- /*
Per-service Valkey ACL users (infrastructure.valkeyAcl). Shared by valkey-durable.yaml,
valkey-kv.yaml and provision-secrets.yaml so the server ACL, the server's password env and the
provisioned per-service Secrets can never drift apart. Least-privilege rules are enforced here
at render time AND on the rendered manifest by scripts/ci/check-valkey-acl-least-privilege.py.
*/ -}}

{{- /* Secret holding one service's Valkey passwords. arg: dict root, name. */ -}}
{{- define "waddlebot.valkeyAcl.userSecretName" -}}
{{- printf "%s-valkey-user-%s" (include "waddlebot.fullname" .root) .name -}}
{{- end -}}

{{- /* Server-side env var carrying one user's password. arg: user name. */ -}}
{{- define "waddlebot.valkeyAcl.pwEnv" -}}
{{- printf "VALKEY_ACL_PW_%s" (. | upper | replace "-" "_") -}}
{{- end -}}

{{- /* Secret key for an instance ("durable"|"kv"). */ -}}
{{- define "waddlebot.valkeyAcl.pwKey" -}}
{{- if eq . "durable" }}VALKEY_DURABLE_PASSWORD{{ else }}VALKEY_KV_PASSWORD{{ end -}}
{{- end -}}

{{- /*
Validate every configured user (fail loud, never render a wide grant). arg: root.
Returns nothing; `fail`s on the first violation.
*/ -}}
{{- define "waddlebot.valkeyAcl.validate" -}}
{{- $acl := .Values.infrastructure.valkeyAcl -}}
{{- $reserved := list "default" "admin" "probe" -}}
{{- $badCmd := list "+@all" "allcommands" "+@dangerous" "+@admin" "+flushall" "+flushdb" "+keys" "+acl" "+config" "+debug" "+shutdown" "+replicaof" "+slaveof" "+module" "+migrate" "+swapdb" "allchannels" -}}
{{- range $name, $u := $acl.users -}}
{{- if not (regexMatch "^[a-z][a-z0-9-]{1,30}$" $name) -}}
{{- fail (printf "infrastructure.valkeyAcl.users: invalid user name %q (want ^[a-z][a-z0-9-]{1,30}$)" $name) -}}
{{- end -}}
{{- if has $name $reserved -}}
{{- fail (printf "infrastructure.valkeyAcl.users: %q is reserved (default/admin/probe are chart-owned)" $name) -}}
{{- end -}}
{{- range $inst := list "durable" "kv" -}}
{{- $c := index $u $inst -}}
{{- if $c -}}
{{- if not $c.keys -}}
{{- fail (printf "infrastructure.valkeyAcl.users.%s.%s.keys is empty -- omit the instance instead" $name $inst) -}}
{{- end -}}
{{- range $k := $c.keys -}}
{{- if not (regexMatch "^(~|%R~|%W~|%RW~)[A-Za-z0-9_.:-]" $k) -}}
{{- fail (printf "infrastructure.valkeyAcl.users.%s.%s.keys: %q must be ~/%%R~/%%W~ + a literal prefix (no ~*, allkeys or leading wildcard)" $name $inst $k) -}}
{{- end -}}
{{- if regexMatch "['\"\\\\\\s]" $k -}}
{{- fail (printf "infrastructure.valkeyAcl.users.%s.%s.keys: %q contains quotes/whitespace" $name $inst $k) -}}
{{- end -}}
{{- end -}}
{{- $cmds := splitList " " ($c.commands | default "-@all") -}}
{{- if ne (first $cmds) "-@all" -}}
{{- fail (printf "infrastructure.valkeyAcl.users.%s.%s.commands must start with -@all" $name $inst) -}}
{{- end -}}
{{- range $t := $cmds -}}
{{- if or (has (lower $t) $badCmd) (hasPrefix "&" $t) (hasPrefix "~" $t) (hasPrefix "%" $t) (hasPrefix ">" $t) (hasPrefix "#" $t) (eq (lower $t) "nopass") -}}
{{- fail (printf "infrastructure.valkeyAcl.users.%s.%s.commands: %q is not allowed for a service user" $name $inst $t) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- /* Sorted names of the users that have `inst` configured. arg: dict root, inst. Output: space-joined. */ -}}
{{- define "waddlebot.valkeyAcl.usersFor" -}}
{{- $out := list -}}
{{- range $name, $u := .root.Values.infrastructure.valkeyAcl.users -}}
{{- if index $u $.inst -}}{{- $out = append $out $name -}}{{- end -}}
{{- end -}}
{{- $out | sortAlpha | join " " -}}
{{- end -}}

{{- /* Server container env: one secretKeyRef per user of `inst`. arg: dict root, inst. */ -}}
{{- define "waddlebot.valkeyAcl.serverEnv" -}}
{{- $root := .root -}}{{- $inst := .inst -}}
{{- range $name := splitList " " (include "waddlebot.valkeyAcl.usersFor" .) -}}
{{- if $name }}
- name: {{ include "waddlebot.valkeyAcl.pwEnv" $name }}
  valueFrom:
    secretKeyRef:
      name: {{ include "waddlebot.valkeyAcl.userSecretName" (dict "root" $root "name" $name) }}
      key: {{ include "waddlebot.valkeyAcl.pwKey" $inst }}
{{- end -}}
{{- end -}}
{{- end -}}

{{- /*
Shell lines appending one `--user` rule per service user of `inst` to "$@" (the server's argv).
The `default` user (legacy or reset) is rendered by the instance template itself. Only SHA-256
hashes reach the ACL rules; an empty password fails the pod loudly. arg: dict root, inst.
*/ -}}
{{- define "waddlebot.valkeyAcl.serverArgs" -}}
{{- $acl := .root.Values.infrastructure.valkeyAcl -}}
{{- $inst := .inst -}}
{{- $base := ternary $acl.durableBaseCommands $acl.kvBaseCommands (eq $inst "durable") -}}
{{- range $name := splitList " " (include "waddlebot.valkeyAcl.usersFor" .) -}}
{{- if $name -}}
{{- $c := index (index $acl.users $name) $inst -}}
{{- $env := include "waddlebot.valkeyAcl.pwEnv" $name -}}
{{- $keys := list -}}
{{- range $k := $c.keys -}}{{- $keys = append $keys (printf "'%s'" $k) -}}{{- end -}}
{{- $cmds := list -}}
{{- range $t := splitList " " ($c.commands | default $base) -}}{{- if $t -}}{{- $cmds = append $cmds (printf "'%s'" $t) -}}{{- end -}}{{- end }}
[ -n "${{ $env }}" ] || { echo "FATAL: {{ $env }} is empty -- per-service Valkey password missing (provision-secrets hook)" >&2; exit 1; }
H="$(printf %s "${{ $env }}" | sha256sum | cut -d' ' -f1)"
set -- "$@" --user {{ $name }} reset on "#$H" resetchannels {{ join " " $keys }} {{ join " " $cmds }}
{{- end -}}
{{- end }}
{{- end -}}
