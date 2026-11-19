# waddlebot-spire

SPIFFE/SPIRE workload identity subchart for waddlebot (v3 plan Task 0.3c).
Single trust domain: `penguintech.io`.

## Subchart value paths

This is a **Helm subchart** of `k8s/helm/waddlebot`. The parent's
`dependencies:` entry (`Chart.yaml`) declares it with `condition:
spire.enabled`, and the parent's `spire:` block in `values.yaml` (and any
per-env override file) is passed to this subchart as its own top-level
`.Values`. All templates in this subchart therefore address values WITHOUT
a `spire.` prefix (e.g. `.Values.trustDomain`, not `.Values.spire.trustDomain`).

Enable per environment (`values-alpha.yaml`, `values-beta.yaml`,
`values-local.yaml`):

```yaml
spire:
  enabled: true
  environment: "alpha"   # or "beta" — SPIFFE ID env segment
```

Build/refresh the parent's subchart lock before templating or installing:

```bash
helm dependency build k8s/helm/waddlebot
```

## Topology: child, upstream disabled by default

`topology.role: child` with `topology.upstreamRoot.enabled: false` is the
default — the server runs self-signed. The full nested-CA wiring
(`UpstreamAuthority "spire"` block in `server-configmap.yaml`, the
downstream-agent sidecar + its ConfigMap/volumes in `server-deployment.yaml`,
`downstream-agent-configmap.yaml`) is present but gated off, keyed on
`and (eq .Values.topology.role "child") .Values.topology.upstreamRoot.enabled`.

**Two modes, one flip**: once an org-wide penguintech.io root exists and a
join token has been minted for this cluster, cutting over to nested mode is
a values change only —

```yaml
spire:
  topology:
    upstreamRoot:
      enabled: true
      serverAddress: "<root-host>"
      serverPort: 8081
      downstreamSpiffeID: "spiffe://penguintech.io/spire/server/waddlebot-<env>"
```

— never a template rewrite. `role: root`/`standalone` are **not** wired in
this subchart (no org-wide root exists yet for waddlebot to nest under); if
that changes, port the root-side wiring from the reference chart
(`skauswatch/k8s/helm/spire`) at that time.

## SVID lifetimes

`svidTtl.x509` / `svidTtl.jwt` default `5m`; `server.caTtl` defaults `168h`
(7d). `ca_ttl` must stay `>= 48h` (2x the 24h admin-adjustable SVID TTL
ceiling used elsewhere in the SPIRE ecosystem) — do not lower it without
also reviewing that ceiling.

## Seven registration entries (auto-enroll Job)

The post-install/post-upgrade `auto-enroll` Job registers exactly the seven
waddlebot v3 services, each as `spiffe://penguintech.io/{environment}/{name}`:

| name | namespace | serviceAccount (placeholder) |
|---|---|---|
| svc-ingest | waddlebot | svc-ingest |
| svc-process | waddlebot | svc-process |
| svc-action | waddlebot | svc-action |
| svc-core | waddlebot | svc-core |
| hub-api | waddlebot | hub-api |
| hub-webui | waddlebot | hub-webui |

**Caveat — serviceAccount is a placeholder.** These seven containers do not
exist yet (the seven-container collapse is a separate, later v3 task). The
workload attestor selector the Job registers is `k8s:sa:<serviceAccount>`,
which MUST equal the real rendered K8s ServiceAccount name once those
service charts/templates land — verify with `helm template <svc>
k8s/helm/<svc>` the same way the reference `skauswatch-spire` chart's
`autoEnroll.services` entries were verified there. A wrong value here
silently breaks workload attestation: the SPIRE entry exists, but no real
pod's ServiceAccount ever matches the selector, so the workload never gets
an SVID.

## Workload API socket

Delivered via `hostPath` at `agent.socketDir` (`/run/spire/sockets`), mounted
read-write into the spire-agent DaemonSet and (by convention, for other
charts to adopt) read-only into service pods at the same path, exposing
`agent.socketPath` (`/run/spire/sockets/agent.sock`).

## ROOT EXCEPTION (approved): spire-agent

The spire-agent DaemonSet runs `runAsUser: 0` — it must chown/create the
hostPath Workload API socket directory shared with every workload pod on the
node, a standard documented SPIRE agent requirement. Not compatible with Pod
Security Admission `restricted` (needs `baseline` or looser for this
component only). No added Linux capabilities, not privileged. See
`agent.podSecurityContext` in `values.yaml` and `agent-daemonset.yaml`.

## Dropped vs. reference chart

Adapted from `skauswatch/k8s/helm/spire` (a standalone chart). The following
reference features are intentionally **not** carried over — not needed for
waddlebot: cross-trust-domain federation, the OIDC discovery provider, the
SPIFFE CSI driver, and the skauswatch/PenguinCloud-specific `suiteServices`
list.
