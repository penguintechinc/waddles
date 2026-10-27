{{/*
R5 cluster-security-baseline helpers (security.md Kubernetes Network Security; devops-kubernetes.md
Cluster Security Baseline). One include per workload template renders, next to that workload:

  1. a CiliumNetworkPolicy (never a plain NetworkPolicy) that
       - ingress: admits traffic ONLY on the workload's declared containerPort(s) — from the same
         namespace, plus the ingress controller / Gateway for `networkPolicy.externalIngressComponents`
       - egress: default-deny + DNS + same namespace (destination ingress CNPs scope the ports) + the
         outbound profile the workload needs; RFC1918/CGNAT/link-local/loopback/cluster CIDRs are
         never reachable via the "world" rules (169.254.169.254 included)
  2. a Tetragon TracingPolicyNamespaced exec allowlist (ENFORCED, SIGKILL) so a shell/curl/nc/wget
     dropped by a compromised dependency can never run inside the pod.

Because the policy is rendered inside the workload's own template it exists exactly when the
workload does, and the port list is the same expression the Deployment's containerPort uses.

Call with:
  (dict "root" $ "component" "svc-foo" "ports" (list 8080 "9000/UDP" "40000-40100/UDP")
        "runtime" "python|node|rust|go"
        "binaries" (list "/app/svc-foo")      # optional, this workload's own binaries
        "shell" true                          # optional, legacy `sh -c` CMD only
        "egress" "internal|https|world|managed"  # optional, default internal
        "kubeApiServer" true                  # optional, workload calls the kube-apiserver
        "excludePorts" (list 50204)           # optional, ports left to a dedicated CNP
        "selector" (dict ...)                 # optional, pod matchLabels (default instance+component)
        "name" "x"                            # optional, policy-name suffix (default component)
        "tetragon" false                      # optional, skip the exec allowlist (third-party images)
        "cnp" false)                          # optional, skip the CNP (workload has its own dedicated one)
*/}}

{{/* CNP toPorts entries from "N", "N/PROTO" or "LO-HI/PROTO" specs. */}}
{{- define "waddlebot.cnpPortList" -}}
{{- range . }}
{{- $spec := toString . }}
{{- $proto := "TCP" }}
{{- $nums := $spec }}
{{- if contains "/" $spec }}
{{- $parts := splitList "/" $spec }}
{{- $nums = index $parts 0 }}
{{- $proto = upper (index $parts 1) }}
{{- end }}
{{- if contains "-" $nums }}
{{- $lohi := splitList "-" $nums }}
- port: {{ index $lohi 0 | quote }}
  endPort: {{ index $lohi 1 | int }}
  protocol: {{ $proto }}
{{- else }}
- port: {{ $nums | quote }}
  protocol: {{ $proto }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
"world" CIDR sets (IPv4 + IPv6) with every private/link-local/loopback/cluster range carved out.
Cilium requires each `except` to be a subnet of its `cidr`, so v4 and v6 carve-outs are split into
separate entries (mixing `::1/128` under `0.0.0.0/0` is invalid). Call with the root context.
*/}}
{{- define "waddlebot.worldCidrSet" -}}
{{- $ep := .Values.egressPolicy -}}
{{- include "waddlebot.splitCidrSet" (concat $ep.denyCidrs $ep.privateCidrs (list $ep.clusterCidrs.podCidr $ep.clusterCidrs.serviceCidr $ep.clusterCidrs.nodeCidr)) -}}
{{- end -}}

{{/* Same, for an explicit carve-out list (used by the egress proxy, which may reach private ranges). */}}
{{- define "waddlebot.splitCidrSet" -}}
{{- $except := . | uniq -}}
{{- $v4 := list -}}
{{- $v6 := list -}}
{{- range $except -}}
{{- if contains ":" . -}}{{- $v6 = append $v6 . -}}{{- else -}}{{- $v4 = append $v4 . -}}{{- end -}}
{{- end -}}
- cidr: "0.0.0.0/0"
  except:
    {{- range $v4 }}
    - {{ . | quote }}
    {{- end }}
- cidr: "::/0"
  except:
    {{- range $v6 }}
    - {{ . | quote }}
    {{- end }}
{{- end -}}

{{- define "waddlebot.workloadPolicies" -}}
{{- $root := .root -}}
{{- $c := .component -}}
{{- $pname := .name | default $c -}}
{{- $sel := .selector | default (dict "app.kubernetes.io/instance" $root.Release.Name "app.kubernetes.io/component" $c) -}}
{{- $tg := true -}}
{{- if hasKey . "tetragon" -}}{{- $tg = .tetragon -}}{{- end -}}
{{- $cnp := true -}}
{{- if hasKey . "cnp" -}}{{- $cnp = .cnp -}}{{- end -}}
{{- $ns := $root.Values.namespace -}}
{{- $np := $root.Values.networkPolicy -}}
{{- $ep := $root.Values.egressPolicy -}}
{{- $profile := .egress | default "internal" -}}
{{- if hasKey $np.egressProfileOverrides $c -}}{{- $profile = index $np.egressProfileOverrides $c -}}{{- end -}}
{{- if has $c $ep.dataPlaneComponents -}}{{- $profile = "managed" -}}{{- end -}}
{{- if not (has $profile (list "internal" "https" "world" "managed")) -}}
{{- fail (printf "workload %q: egress profile must be internal|https|world|managed, got %q" $c $profile) -}}
{{- end -}}
{{- $ingressOn := $np.enabled -}}
{{- $egressOn := and $ep.enabled $ep.workloadEgress.enabled (ne $profile "managed") -}}
{{- $exclude := list -}}
{{- range (.excludePorts | default list) -}}{{- $exclude = append $exclude (toString .) -}}{{- end -}}
{{- $ports := list -}}
{{- range .ports -}}
{{- if not (has (toString .) $exclude) -}}{{- $ports = append $ports . -}}{{- end -}}
{{- end -}}
{{- if and $cnp (or $ingressOn $egressOn) }}
---
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: {{ include "waddlebot.fullname" $root }}-{{ $pname }}-policy
  namespace: {{ $ns }}
  labels:
    {{- include "waddlebot.labels" $root | nindent 4 }}
    app.kubernetes.io/component: {{ $c }}
spec:
  endpointSelector:
    matchLabels:
      {{- toYaml $sel | nindent 6 }}
  {{- if $ingressOn }}
  ingress:
    {{- if $ports }}
    # Same-namespace callers, declared serving port(s) only.
    - fromEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: {{ $ns }}
      toPorts:
        - ports:
            {{- include "waddlebot.cnpPortList" $ports | trim | nindent 12 }}
    {{- end }}
    {{- if and $ports (has $c $np.externalIngressComponents) }}
    # Ingress controller / Gateway API, declared serving port(s) only.
    - fromEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: {{ $np.ingressNamespace }}
        {{- if $root.Values.httproute.enabled }}
        - matchLabels:
            io.kubernetes.pod.namespace: {{ $root.Values.httproute.gatewayNamespace }}
        {{- end }}
      toPorts:
        - ports:
            {{- include "waddlebot.cnpPortList" $ports | trim | nindent 12 }}
    {{- if $root.Values.httproute.enabled }}
    - fromEntities:
        - ingress
      toPorts:
        - ports:
            {{- include "waddlebot.cnpPortList" $ports | trim | nindent 12 }}
    {{- end }}
    {{- end }}
    {{- if not $ports }}
    # Only ports served by a dedicated CNP (see excludePorts): deny everything here.
    - {}
    {{- end }}
  {{- end }}
  {{- if $egressOn }}
  egress:
    # DNS.
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: kube-system
            k8s-app: kube-dns
      toPorts:
        - ports:
            - port: {{ $ep.inCluster.dnsPort | quote }}
              protocol: UDP
            - port: {{ $ep.inCluster.dnsPort | quote }}
              protocol: TCP
          {{- if and (eq $profile "https") $ep.httpsFqdns }}
          rules:
            dns:
              - matchPattern: "*"
          {{- end }}
    # Own namespace (DB / cache / hub-api / peers) — the destination's ingress CNP scopes the port.
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: {{ $ns }}
    {{- if .kubeApiServer }}
    - toEntities:
        - kube-apiserver
    {{- end }}
    {{- if and (eq $profile "https") $ep.httpsFqdns }}
    # Known external APIs over TLS, by FQDN (egressPolicy.httpsFqdns) — nothing else.
    - toFQDNs:
        {{- toYaml $ep.httpsFqdns | nindent 8 }}
      toPorts:
        - ports:
            - port: "443"
              protocol: TCP
    {{- else if eq $profile "https" }}
    # External APIs over TLS only (set egressPolicy.httpsFqdns to pin FQDNs) — never
    # private/link-local/metadata/cluster ranges.
    - toCIDRSet:
        {{- include "waddlebot.worldCidrSet" $root | nindent 8 }}
      toPorts:
        - ports:
            - port: "443"
              protocol: TCP
    {{- else if eq $profile "world" }}
    # Documented exception: arbitrary outbound media/relay ports — still never private ranges.
    - toCIDRSet:
        {{- include "waddlebot.worldCidrSet" $root | nindent 8 }}
    {{- end }}
  {{- end }}
{{- end }}
{{- if and $root.Values.tetragon.enabled $tg }}
{{- $rt := .runtime | default "" -}}
{{- if not (hasKey $root.Values.tetragon.runtimes $rt) -}}
{{- fail (printf "workload %q: runtime must be one of %v, got %q" $c (keys $root.Values.tetragon.runtimes | sortAlpha) $rt) -}}
{{- end -}}
{{- $allowed := concat (index $root.Values.tetragon.runtimes $rt) (.binaries | default list) $root.Values.tetragon.commonBinaries -}}
{{- if .shell -}}{{- $allowed = concat $allowed $root.Values.tetragon.shellBinaries -}}{{- end -}}
{{- $allowed = $allowed | uniq }}
{{- /* runc exec re-exec (values.yaml tetragon.runcInit): `/proc/self/fd/<N>` execs are exempt from the
       allowlist selector below but SIGKILLed by a second selector unless the caller is runc. */ -}}
{{- $runcInit := $root.Values.tetragon.runcInit | default dict -}}
{{- $runcCallers := $runcInit.callerBinaries | default list -}}
{{- if and $runcInit.enabled (not $runcCallers) -}}
{{- fail (printf "workload %q: tetragon.runcInit.enabled requires a non-empty tetragon.runcInit.callerBinaries (refusing to allow /proc/self/fd/ execs unscoped)" $c) -}}
{{- end }}
---
apiVersion: cilium.io/v1alpha1
kind: TracingPolicyNamespaced
metadata:
  name: {{ include "waddlebot.fullname" $root }}-{{ $c }}-exec-allowlist
  namespace: {{ $ns }}
  labels:
    {{- include "waddlebot.labels" $root | nindent 4 }}
    app.kubernetes.io/component: {{ $c }}
spec:
  podSelector:
    matchLabels:
      {{- toYaml $sel | nindent 6 }}
  kprobes:
    - call: "sys_execve"
      syscall: true
      args:
        - index: 0
          type: "string"
      selectors:
        - matchArgs:
            - index: 0
              operator: "NotEqual"
              values:
                {{- range $allowed }}
                - {{ . | quote }}
                {{- end }}
            {{- if $runcInit.enabled }}
            # `/proc/self/fd/<N>` is judged by the next selector (runc-caller-scoped), not here.
            - index: 0
              operator: "NotPrefix"
              values:
                - "/proc/self/fd/"
            {{- end }}
          matchActions:
            - action: {{ $root.Values.tetragon.action }}
        {{- if $runcInit.enabled }}
        # runc exec re-exec guard: kill any `/proc/self/fd/<N>` exec whose caller is NOT runc
        # (blocks fileless memfd exec by app code; runc's own `runc init` is the only exempt caller).
        - matchBinaries:
            - operator: "NotIn"
              values:
                {{- range $runcCallers }}
                - {{ . | quote }}
                {{- end }}
          matchArgs:
            - index: 0
              operator: "Prefix"
              values:
                - "/proc/self/fd/"
          matchActions:
            - action: {{ $root.Values.tetragon.action }}
        {{- end }}
{{- end }}
{{- end -}}
