# RTC media exposure

How the first-party RTC SFU hosted in the `svc-streaming` pod is reached, per environment, and the
Helm switches that control it. Chart: `k8s/helm/waddlebot` (`templates/svc-streaming.yaml`,
`waddlebot.svcStreaming.rtcMedia` in `_helpers.tpl`).

## What the pod serves

| Port | Proto | Purpose | Who may reach it |
|---|---|---|---|
| 8208 | TCP | HTTP: health, WHIP/WHEP signalling, playback | same namespace + ingress controller / Gateway (generic CNP) |
| 9090 | TCP | metrics | same namespace + ingress (generic CNP, unchanged) |
| 50208 | TCP | `RoomsService` gRPC (mTLS + per-call ES256 service JWT) | **hub-api pods only** (`-svc-streaming-grpc` CNP) |
| `webrtc.udpPort` | UDP | ONE multiplexed WebRTC media port (ICE + SRTP) | `world` (+ `host`/`remote-node` on alpha NodePort) only (`-svc-streaming-media` CNP) |
| `webrtc.iceTcp.port` (opt-in) | TCP | ICE-TCP fallback, one port | same as the UDP media port |
| 1935 / 9000 | TCP / UDP | RTMP / SRT ingest (unchanged) | generic CNP |

No port range, no TURN (v3.3.x, #1318), no `hostPort`, no `hostNetwork`, no `externalIPs`.
`svc-streaming` stays `replicas: 1`, `strategy: Recreate`, with no PDB/HPA: **every svc-streaming
rollout drops all live RTC rooms and streams**, so deploy in a quiet window.

## Per-environment exposure

The media port is published only by the dedicated Service `<release>-svc-streaming-rtc-media`,
never by the RTMP/SRT/HTTP NodePort Service.

| Env | Service type | Published port | CNP entities |
|---|---|---|---|
| local, alpha, dal2alpha | `NodePort` (owner-approved exemption from the no-external-ports rule) | `31400/udp`, nodePort == bound port | `world`, `host`, `remote-node` (dal2alpha; alpha/local have CNPs off) |
| beta, gamma, production | `LoadBalancer`, `externalTrafficPolicy: Local` | `40000/udp` (== bound port) | `world` |
| ci | none (svc-streaming disabled) | - | - |

The chart **fails the render** when: NodePort is used outside `global.deploymentTier` alpha/local; a
published port differs from the bound port; a media port is privileged or collides with another
declared port; the NodePort/LB combination is inconsistent. The SFU advertises the port it binds in
its ICE candidates, so the externally reachable port MUST equal it (this is why alpha binds `31400`
rather than `40000`).

## Owner action: alpha router UDP port-forward

Alpha (dal2alpha and the local lab) sits behind the router. Create one forward:

| Field | Value |
|---|---|
| Protocol | **UDP** |
| External (WAN) port | `31400` |
| Internal host | the IP of any dal2alpha node (the Service uses `externalTrafficPolicy: Cluster`, so any node answers) |
| Internal port | `31400` (same as external, required: the candidate port must equal the reachable port) |

If ICE-TCP is enabled (`pipeline.svcStreaming.webrtc.iceTcp.enabled=true`, set `iceTcp.port` and
`media.service.nodePort.tcp` to the same free NodePort, e.g. `31401`), add the same forward for TCP.
`pipeline.svcStreaming.publicBaseUrl` must resolve to the router's public address, since it is the
ICE host and the base of `whip_url`/`whep_url`. Verify with `kubectl --context dal2alpha -n waddlebot
get svc waddlebot-svc-streaming-rtc-media` (read-only).

## Beta / gamma / production: LoadBalancer note

The chart never uses NodePort/hostPort/externalIPs there. The media Service is a `LoadBalancer`
scoped to the single UDP port (and the ICE-TCP port when enabled) with hooks:
`webrtc.media.service.loadBalancerSourceRanges`, `.annotations`, `.loadBalancerClass`.

- The cluster must provide a **UDP-capable** LB. dal2beta: MetalLB (or equivalent). DigitalOcean
  gamma/production: verify the provider LB supports UDP before relying on it; if it does not, use a
  provider/cluster that does or a Gateway API `UDPRoute` (not templated here), scoped to the same
  single port. Mixed UDP+TCP in one LB Service (ICE-TCP) needs mixed-protocol LB support.
- `loadBalancerSourceRanges` is empty by default: browsers anywhere must reach media. Restrict only
  for a closed audience.
- Set `pipeline.svcStreaming.publicBaseUrl` to the name resolving to the LB address.
- LB provisioning is an owner/infra action; until it exists the Service stays `Pending` (loud, no
  fallback to NodePort).

## Env contract with `core/svc_streaming`

| Env | Set by chart | Notes |
|---|---|---|
| `WEBRTC_UDP_RANGE` | `<udpPort>-<udpPort>` | single mux port expressed as a one-port range (accepted by `config.rs::parse_udp_range`) |
| `WEBRTC_ICE_TCP_PORT` | only when `iceTcp.enabled` | new; read by the mux server (plan T2). Until it lands the pod ignores it |
| `ROOMS_GRPC_PORT`, `ROOMS_GRPC_SERVER_*`, `ROOMS_GRPC_CLIENT_CA_FILE`, `SERVICE_JWT_*`, `ROOMS_GRPC_ALLOWED_SUBJECTS` | unchanged | rendered only when the shared host-api-tls material exists (off in `values-local`) |
| `WEBRTC_ROOM_TOKEN_SECRET` | only when `rooms.roomToken.existingSecret` is set | a Secret reference, never a value. Unset = JoinRoom fails loud |

hub-api (`RoomsClient`): `STREAMING_RTC_GRPC_TARGET`, `_SERVICE_ID` (its SPIFFE id, the minted
ES256 JWT `sub`), `_GRPC_{CA,CLIENT_CERT,CLIENT_KEY}_FILE`, `_GRPC_SERVER_NAME` (a SAN of the shared
host-api-tls cert), `_TIMEOUT_SECONDS`. `STREAMING_RTC_URL` is retired and never rendered.

Seam: the room-token key is moving to ES256 and the `platform_secrets` store will own it; keep the
`rooms.roomToken.existingSecret` indirection and never inline a value.

## Probes (approved exception)

`svc-*` workloads should use native `grpc.health.v1` probes. `svc-streaming` keeps **HTTP**
`startup`/`liveness`/`readiness` on 8208 (`/health`, `/readyz`) because the kubelet's gRPC probe
cannot present the client certificate the 50208 listener requires. Owner-approved; marked
`# EXCEPTION (approved)` in the template. Startup budget is 5s x 60 = 300s for the cold first boot.

## Resources

requests `250m` / `512Mi`, limits `4` CPU / `4Gi` on every environment: the pod idles most of the
time (small guaranteed request keeps the scheduler from reserving transcode capacity) but must burst
for an RTMP transcode or a full room without throttling/OOM. The 512Mi tmpfs HLS scratch counts
against the memory limit.

## Open items

- Shared `waddlebot.otel.env` helper is not on this base (PR #1329 unmerged): no OTel env is added
  here; wire it in once that merges, do not duplicate it.
- No Gateway/HTTPRoute currently routes browsers to svc-streaming 8208 (alpha uses the NodePort,
  beta/prod rely on `publicBaseUrl`): a WHIP/WHEP route is a separate decision.
