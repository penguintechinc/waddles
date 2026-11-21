# svc-streaming (RTC + Stream-Forward / Transcode) — Design Specification

> **Historical document.** Written against the repository layout at the time; some paths named below (shown as plain text, not links) have since moved, been renamed or been removed. Not maintained as current architecture.

Status: **DRAFT — for review, not implementation.** Extends
`docs/plans/2026-08-26-v3-scbm-apps-design.md` (Modules/Features/Apps, seven→eight
container topology), `docs/plans/2026-08-31-app-bundle-sdk-design.md` (pipeline, isolation
keys, poll/read-replica distribution), `docs/plans/2026-08-31-music-station-design.md`
(the `svc-presentation` render surface it shares with), and the v3 SCCEBM master program
plan (`git show origin/feature/v3-program-plan:docs/plans/2026-08-31-v3-sccembs-program-plan.md`,
§4.3, §5, P5/P6, §10). Cross-refs the metered-token-billing spec being authored in
parallel (`docs/plans/2026-08-31-metered-token-billing-design.md`). Every "today" claim
cites the real file/line it is grounded in; every new field, container, or table is a
**proposal**, called out as such. Open decisions are collected in §8 — none are settled
here.

> **Load-bearing correction to the brief this spec was commissioned against.** The brief
> (and `.PLAN:326-389`) describe the streaming engine as **"Gazer-derived Go"** media code to
> migrate to Rust. Repo reconnaissance found **no Gazer code and no first-party media engine
> at all** — no HLS/RTMP/AV1/ffmpeg/transcode implementation exists anywhere in `core/`,
> `services/`, `action/`, or `trigger/`. What exists is **two thin control planes over
> *external* media servers**: `video_proxy_module` (Python/Quart) fronts an external
> **MarchProxy** RTMP relay that does the real RTMP ingest + x265/AV1/x264 transcode +
> multi-destination fan-out (`video_proxy_module/README.md:3`, not in this repo), and
> `module_rtc` (Go) fronts an external **LiveKit** SFU (`module_rtc/go.mod:8-11`). This moves
> the central design question from "port Go media code to Rust" to **"does svc-streaming
> become a first-party Rust media engine, or a Rust control plane orchestrating external
> MarchProxy/LiveKit?"** — the highest-impact open decision (§8.1), threaded through §1, §6,
> and §7 below.

> **Terminology (mandatory).** This service *forwards* / *stream-forwards* an ingested
> stream to one or more targets. The single-word competitor term for that (a trademark) is
> never used anywhere in this design, its code, its UI, or its docs. Say "forward",
> "stream-forwarding", or "forward to targets".

---

## 1. Overview

`svc-streaming` is the paired **RTC + broadcast media** container — the engine of the
**Streaming** module (the "S" in SCCEBM, `program-plan §1`). It is the **8th** container
(`.PLAN:385-389`, program-plan container table): the legacy `svc-rtc` (interactive
voice/video) is *folded in* rather than kept separate, because both are media-heavy and
share one transcode/media stack.

**Language: Rust.** There is no Gazer-derived media code to migrate (see the correction
above). The one first-party **Go** component is `module_rtc` — `module
github.com/penguintech/waddlebot/module_rtc`, `go 1.24.0` (`core/module_rtc/go.mod:1-3`),
`cmd/server/main.go:19`, a **LiveKit** control plane (`go.mod:8-11`: `livekit/protocol`
v1.6.1 + `server-sdk-go` v1.0.16), HTTP-only (REST 8093, gRPC 50067). High-performance
media/networking is the Rust target per `general.md` Language Selection and the Go phase-out
(`critical-rules.md`); **no new Go**. svc-streaming is authored in Rust (Tokio/Axum control
surface + a media core whose scope depends on §8.1). The two existing control planes are
consolidated into it (§6); the Go `module_rtc` control plane is retired, not kept.

**Not a pipeline stage-runner.** Unlike `svc-ingest/process/action` (which load bundle
scripts, program-plan container table), svc-streaming is a **first-party media service** like
`svc-presentation` — it runs trusted first-party code only, is driven by the hub-api
control plane, and exposes media transport rather than a bundle dispatch loop (§3). This
matches the original reason `svc-rtc` was never in the pipeline: "WebRTC/SFU media; UDP
transport and media scaling share nothing with HTTP request handling"
(`2026-08-26-v3-scbm-apps-design.md:683`).

---

## 2. Capabilities & feature flags

Each capability is its own Feature (own `FeatureContract`, own PostHog flag; flag key must
equal `waddles.{id}`, `feature_contract.py:151-155`), independently module-toggleable. The
Streaming module global toggle (`modules.streaming.enabled`, built pattern
`program-plan §3`) gates the container; per-capability flags gate each capability inside it;
each is additionally tier-gated (two-gate: flag × license tier).

| # | Capability | Flag (`waddles.streaming.*`) | Tier | Grounding / note |
|---|---|---|---|---|
| 1 | **INGEST** — accept a source stream (HLS / RTMP / AV1) | `waddles.streaming.ingest` | Free | intake side of today's `video_proxy_module`; RTMP 1935 / HLS pull |
| 2 | **DISPLAY** — community "live streams" section | `waddles.streaming.display` | Free | rendered by `svc-presentation` (§4, music spec §8) |
| 3 | **FORWARD** — forward to 1..N targets | `waddles.streaming.forward` | Free (base) | = today's `video_proxy.streaming` Free tier — `FREE_LIMITS` 3 dest / 0×2K / 6000 kbps / 7d (`license_service.py:11-16`) |
| 4 | **FORWARD premium limits** | `waddles.streaming.premium_limits` | Professional | = `video_proxy.premium_limits` — `PREMIUM_LIMITS` 10 dest / 5×2K / 15000 kbps / 90d retention (`license_service.py:18-23`) |
| 5 | **RECORD** — segment + persist | `waddles.streaming.record` | Professional | storage/retention cost; object store (MinIO — module_rtc already records to MinIO, `module_rtc/README.md:11`) |
| 6 | **ENCODE/TRANSCODE** (optional) | `waddles.streaming.transcode` | Professional **+ metered tokens** | AV1 = heavy compute ("beefy host", `.PLAN:334`); consumes transcoding tokens (§5) |
| 7 | **RTC** — interactive voice/video | `waddles.streaming.rtc` | Free | the folded-in `svc-rtc`/`module_rtc`; = today's `social.rtc` |

Flags 1–7 are **proposals**; today's catalog (`.PLAN:52-62`) has `social.rtc` (Free),
`video_proxy.streaming` (Free), `video_proxy.premium_limits` (Pro) — §6 covers migrating
those three keys into the `streaming.*` namespace.

---

## 3. Pipeline fit

svc-streaming is a **first-party engine addressed by the control plane**, not a bundle that
targets a pipeline stage. Three integration edges:

```
                 hub-api control plane (Python/Quart)
                 stream / streaming / calls / overlay controllers  (.PLAN:410)
                 - per-community FORWARD targets  - RTC session broker  - live-status store
                        │ writes (primary)          │ reads (read replica, §6.3 bundle spec)
                        ▼                            ▼
   OBS / RTMP ─INGEST─▶ ┌───────────────────────────────────────┐
   WebRTC     ─RTC────▶ │            svc-streaming (Rust)         │
                        │ ingest · record · transcode · forward · SFU │
                        └───────────────┬───────────────┬────────┘
                          FORWARD to     │ DISPLAY state │ RECORD
                          N targets      ▼               ▼
                       (Twitch/YT/…)  svc-presentation   object store (MinIO)
                                       renders live-streams section → OBS browser source
```

- **DISPLAY renders through `svc-presentation`, not svc-streaming.** svc-streaming publishes
  its own live-stream state; `svc-presentation` (the render surface for the `overlay` action
  target and the music station, music spec §8.1-8.2) renders the community "live streams"
  section HTML for browser sources / community webui. svc-streaming never serves overlay
  HTML itself — cross-ref music spec §8, program-plan §4.4.
- **FORWARD targets are per-community control-plane config.** The existing model is already
  there in `video_proxy_module`: `stream_configurations` + `stream_destinations` (`platform`,
  `rtmp_url`, `stream_key`, `resolution`, `bitrate`, `is_enabled`, `force_cut`,
  `services/database.py:56,70`), CRUD in `services/destination_service.py:12,82,110,166`, and
  a gRPC `VideoProxyService` (`proto/video_proxy.proto:5-13`: `AddDestination`,
  `SetForceCut`, `GetStreamStatus`, …). Under svc-streaming these move to hub-api
  (`stream`/`streaming` controllers, `.PLAN:410`), written to the **primary**, read by
  svc-streaming from the **read replica** on the stage-runner routing-read pattern
  (`app-bundle-sdk-design.md §6.3`, `:343-360`). Destination secrets (target stream keys)
  resolve via `penguin-sal` references, never the inline `stream_key` column they use
  today — §8.4.
- **Relation to bundle action targets.** The action `target_type` enum
  (`webhook | rest_api | grpc_api | graphql_api | email | overlay | message_queue`,
  `.PLAN:273-279`) has **no streaming target** — a bundle's action stage cannot emit a
  broadcast today. **Proposal:** svc-streaming stays a control-plane-addressed engine for
  v3; a future `target_type` (e.g. clip/segment push into svc-streaming) is deferred (§8).
  `overlay` (→ svc-presentation) remains the only media-adjacent action target.

**API surface** follows the standard bundle-oriented path
(`/api/v2/{module}/{surface}/{app_bundle}/{target}`, `.PLAN:391-399`) for the control-plane
management API (`module=streaming`), with an OpenAPI 3.x spec (`backend.md` OpenAPI). Media
transport (RTMP/WebRTC/HLS) is **not** REST and is exposed on dedicated media ports (§7).

---

## 4. Live-streams aggregation (DISPLAY)

The community "live streams" section is an **aggregation** of two sources
(`.PLAN:337-343`, program-plan §4.4):

| Source | Where it comes from | Live-status mechanism |
|---|---|---|
| (a) The community's **own** svc-streaming streams | svc-streaming ingest/forward state | direct — svc-streaming knows its own live sessions |
| (b) **Connected external platform channels** currently LIVE | existing Python receivers already detect live-state (§4.1) | per-platform, largely **already built** |

### 4.1 Live-status detection — **the detection already exists; the new work is aggregation, not detection**

Both platform receivers already learn live-state today — this changes the recommendation
from "how do we detect" to "surface what's already detected":

| Platform | Receiver (Python/Quart) | Live-state already implemented today | Connected-channel model |
|---|---|---|---|
| **Twitch** | trigger/receiver/twitch_module | **PUSH, done** — `eventsub_handler.py:215-236` already emits `stream_online` (with `stream_type`/`started_at`) and `stream_offline` from `stream.online`/`stream.offline` EventSub events; HMAC-verified (`:90`) | `community_servers` table → community map, Redis-cached (`channel_manager.py:41-63`) |
| **YouTube Live** | trigger/receiver/youtube_live_module | **PUSH + POLL, done** — WebSub/PubSubHubbub callback detects live streams (`webhook_handler.py:5-6,32`); `youtube_client.get_live_broadcasts()` searches `eventType=live` (`youtube_client.py:141-178`) as the poll path | runtime registration `POST /api/v1/channels/register` (`app.py:107-152`) — no persistent table |

**Recommendation:** do **not** build new detection. Add a thin **live-status projection** in
the hub-api control plane that these receivers write to (they already have the events —
Twitch `stream_online`/`stream_offline`, YouTube WebSub/poll): a per-connected-channel
`live | offline` record on the **primary**. `svc-presentation` reads that projection (read
replica) and renders it alongside (a). The projection layer is source-agnostic — push
(Twitch) and push+poll (YouTube) both just write the same record; a future platform plugs in
by writing the same shape. **Open (§8.6):** where the projection lives (extend each receiver
to write it directly vs a hub-api control-plane job that subscribes to their emitted events),
and normalizing YouTube's runtime-registration model onto the same per-community
connected-channel store Twitch uses (`community_servers`).

**Embed vs link (proposal):** embed the community's **own** svc-streaming output
(first-party player in the presentation surface); for **connected external** channels,
render the platform's official embed where its ToS permits (Twitch/YouTube both provide
iframe embeds) and fall back to a **link** otherwise. Confirm per-platform at implementation
(§8).

---

## 5. Transcoding-token billing (metered consumable)

ENCODE/TRANSCODE (capability #6) is monetized as a **metered consumable** — the third
billing axis beyond per-node and per-seat (`.PLAN:352-356`, program-plan §5). This design
**consumes** that mechanism; the mechanism itself is specified in
`docs/plans/2026-08-31-metered-token-billing-design.md` (parallel). svc-streaming's
responsibilities against it:

- **Meter emission.** Each transcode/encode job reports units consumed (proposal: a token
  cost function of output profile × duration × codec — AV1 costs more than H.264, reflecting
  its "beefy host" compute, `.PLAN:334`) to the ledger in the **marketplace module inside
  hub-api** (`.PLAN:358-364`) + license-server metering.
- **Pricing.** Global admin sets per-token prices; community buys a balance; usage
  decrements — all in hub-api/marketplace + license-server, **not** in svc-streaming.
  svc-streaming only emits usage and checks entitlement before *starting* a job.
- **Enforcement posture (recommendation, confirm in token-billing spec §8).** Mirror the
  seat/node asymmetry (`critical-rules.md` Licensing Model): **never kill an in-flight
  transcode / live forward** when a balance runs out mid-stream (that's a customer-visible
  outage, like suspending an enrolled node) — let it finish and true-up. **Block starting a
  new transcode job** when the balance is exhausted, with an upgrade/refill path (a
  deliberate action, safe to block, like seat creation). FORWARD/INGEST/RTC without
  transcode consume **no** tokens.

---

## 6. video_proxy_module & svc-rtc reconciliation — recommendation: **ABSORB both**

**Decision (locked upstream, `.PLAN:385-389`, program-plan container table: "absorbs svc-rtc
+ video_proxy"): svc-streaming supersedes and absorbs both control planes — it does not sit
beside them.** But note *what* is being absorbed: **two control planes, not two media
engines** (see the correction at the top). One consolidated Rust control plane; the media
engine itself is §8.1.

| Legacy component | Language today | What it actually is | Absorbed as |
|---|---|---|---|
| core/module_rtc (+ `svc-rtc.yaml`) | **Go 1.24** (`go.mod:1-3`); `cmd/server/main.go:19`; **LiveKit** control plane (`go.mod:8-11`), HTTP-only, LiveKit SFU is external; rooms/join-JWT/mute/kick/raise-hand (`internal/services/room_service.go:53-178`, `call_features.go:32-106`); optional MinIO recording | RTC capability (#7) — control plane **rewritten in Rust**; LiveKit stays external (or §8.1) |
| core/video_proxy_module | **Python/Quart** (`app.py:68`, hypercorn); control plane over external **MarchProxy** RTMP relay (`README.md:3,27`; `MARCHPROXY_GRPC_HOST/PORT`, `config.py:39-40`) — MarchProxy does the real RTMP ingest + x265/AV1/x264 transcode + fan-out | INGEST/FORWARD/RECORD/TRANSCODE control (#1,3,4,5,6) — control plane **rewritten in Rust**; MarchProxy stays external (or §8.1) |

**There is nothing to "port" for the actual media path** — no first-party ingest/transcode/
SFU code exists to translate Go→Rust. The Go→Rust work is confined to the two **control
planes** (destination CRUD, limits, gRPC, session state). Whether svc-streaming *also* grows
a first-party Rust media core (replacing external MarchProxy/LiveKit) is the separate, larger
§8.1 decision.

**Catalog key migration** (three keys → `streaming.*` namespace; add deprecation aliases,
don't hard-break `.PLAN:52-62` consumers):

| Today | Becomes | Tier |
|---|---|---|
| `social.rtc` | `streaming.rtc` | Free |
| `video_proxy.streaming` | `streaming.ingest` + `streaming.forward` (split intake vs forward) | Free |
| `video_proxy.premium_limits` | `streaming.premium_limits` | Professional |

`+` new: `streaming.display`, `streaming.record`, `streaming.transcode`. This also moves
the module gating: `svc-rtc.yaml` is gated today on `modules.social.enabled`
(`svc-rtc.yaml:5-12`) because "Only Social owns module_rtc"; under SCCEBM the container is
gated on **`modules.streaming.enabled`** instead.

**Migration note (staged — §8.1/§8.2 timing):**
1. Stand up Rust svc-streaming as the **consolidated control plane** for INGEST + FORWARD +
   RECORD first — reimplement `video_proxy_module`'s destination CRUD / limits / gRPC in Rust
   (Python → Rust), still orchestrating external MarchProxy. Lower risk, no realtime/SFU, no
   media-code rewrite.
2. Fold the **RTC** control plane in — reimplement `module_rtc`'s LiveKit control (room/join-
   JWT/moderation) in Rust; retire the Go `module_rtc` and the `svc-rtc.yaml` skeleton chart.
   LiveKit stays external unless §8.1 chooses a first-party SFU.
3. Wire **TRANSCODE** (AV1) + token metering (§5), gated on the beefy-host node pool (§7).
   Whether transcode runs in-process (Rust/ffmpeg, §8.1 "build") or stays delegated to
   MarchProxy (§8.1 "external") is decided here.

Until step 1 lands, `svc-rtc.yaml` remains a skeleton placeholder pinned to
`pipeline.goRuntimeImage` (a `debian:bookworm-slim` digest, `svc-rtc.yaml:39-43`,
`values.yaml:384`) — it is replaced, not extended.

---

## 7. Container & deployment

**New `svc-streaming` container, Rust**, replacing the `svc-rtc.yaml` skeleton chart. Rust
multi-stage build (`rust:1.97-slim-bookworm` builder → `debian:bookworm-slim` runtime,
`devops-containers.md`), SHA256-pinned; `building-rust-services` skill for the template.

**Resource sizing — media-heavy.** High CPU (transcode) + high memory; distinct from every
HTTP pod. AV1 transcode is the heaviest path (`.PLAN:334`):

- **Beefy-host / node-pool proposal:** schedule transcode-capable replicas onto a dedicated
  node pool (GPU or high-core), via `nodeSelector` + toleration; keep INGEST/FORWARD/RTC
  replicas on general nodes. Hardware-accel (VAAPI/NVENC/AV1) needs a device plugin +
  device mount — **flag (§8)**.
- Resource tiers per env in `alpha.yml`/`beta.yml`/`gamma.yml`/`production.yml`
  (`devops-kubernetes.md`); `k8s-manifest-builder` authors them.

**Ports / networking.** The media-transport rows below apply **only if §8.1 chooses a
first-party media engine**. If media stays external (MarchProxy/LiveKit), svc-streaming
exposes only its control REST/gRPC and needs **egress** to those servers (MarchProxy gRPC
`50050`, `config.py:39-40`; LiveKit host) — the RTMP/WebRTC media ports live on the external
servers, and §7.1 becomes their problem, not this pod's.

| Protocol | Port | Transport | Note |
|---|---|---|---|
| Health / metrics / control REST | e.g. 8080 (today: module_rtc REST 8093, video_proxy 8092; `svcRtc` chart 8206, `values.yaml:462-472`) | TCP/HTTP | liveness+readiness `/health` (mirrors `svc-rtc.yaml:58-73`) |
| Control gRPC | e.g. 50051 (today: module_rtc 50067, video_proxy 50065) | TCP/HTTP2 | `backend.md`: service-to-service is gRPC |
| RTMP ingest *(build only)* | 1935 | TCP | >1024, no privileged bind |
| HLS pull/serve *(build only)* | HTTP | TCP | via ingress |
| WebRTC / SFU media (RTC) *(build only)* | ephemeral range | **UDP** | the hard part — §7.1 |

**7.1 UDP media exposure vs cluster policy (open, §8) — only bites if §8.1 = build.** WebRTC/SFU needs a UDP media port
range reachable from clients. Beta/prod security baseline **forbids `NodePort`/`hostPort`/
`hostNetwork`/`externalIPs`** (`security.md` K8s Network Security) — so UDP media must go
through a **Gateway/LB with UDP support**, scoped to only the media ports
(`security.md`: external access scoped to the pod's serving ports). `svc-rtc.yaml:9-11`
already flags that UDP transport is **not wired** and that any `NET_ADMIN`/`hostNetwork`
requirement needs **explicit user approval (ROOT EXCEPTION)** — that flag carries forward.

**Rootless / securityContext.** Process rootless (`runAsNonRoot: true`, drop ALL caps,
seccomp `RuntimeDefault`) is the default and target (`critical-rules.md` Rootless
Containers). **Two capabilities may pressure it, each a documented `# ROOT EXCEPTION
(approved)` only if the user confirms (§8):**
- **hostNetwork / NET_ADMIN** if SFU media performance genuinely requires it (default: do
  **not** — use a UDP Gateway first).
- **XDP/AF_XDP kernel-bypass** (the Rust high-perf media path, `building-rust-services`
  skill) needs `NET_ADMIN`/`NET_RAW` — same-tag runtime-detected fallback so the same image
  runs unprivileged; **not** assumed here.

**Baseline (mandatory, `devops-kubernetes.md`):** CiliumNetworkPolicy default-deny + explicit
allow (external allowlist scoped to media ports only), Tetragon `TracingPolicy` allowlisting
the Rust binary + `ffmpeg`/transcode helper, Pod Security Admission `restricted`. Per-service
DB account (read-only on the read replica for routing reads, §3; `backend.md` Database Tier).

---

## 8. Open decisions (for the user — not decided here)

| # | Decision | Why it's open |
|---|---|---|
| **8.1** | **Build vs external media engine (highest-impact).** Does svc-streaming embed a **first-party Rust media engine** (RTMP/HLS ingest, AV1 transcode, WebRTC SFU) — replacing external MarchProxy + LiveKit — or stay a **Rust control plane** orchestrating those external servers? | No first-party media code exists today (correction, top of doc); both existing services are control planes. "Build" is large greenfield Rust + the AV1/UDP/rootless costs of §7; "external" keeps a third-party dependency (MarchProxy is not in-repo, LiveKit is external) and its supply-chain/licensing posture. Everything else (§7 ports, §8.3, §8.7) hangs off this |
| 8.2 | **Rust migration timing** — staged (control plane: forward → RTC → transcode, §6) vs big-bang | Reimplementing the LiveKit control plane in Rust is the higher-risk step; staging de-risks but runs two services briefly |
| 8.3 | **AV1 compute / hardware** — GPU node pool + device plugin vs CPU-only; VAAPI/NVENC/AV1 device mount (§7); only relevant if 8.1 = build in-process | Determines the cost model behind the transcoding-token price and whether "beefy host" (`.PLAN:334`) is a real hardware requirement or MarchProxy's problem |
| 8.4 | **Forward-target secret storage** — per-community target stream keys move off the inline `stream_destinations.stream_key` column (`database.py:70`) to `penguin-sal` refs; rotation via `credential_manager_module` (`oauth_handlers.py`, `refresh_service.py`) (§3) | Today the target stream key is a plaintext column; `security.md` forbids inline secrets — must be redesigned |
| 8.5 | **Recording storage** — reuse the MinIO both services already touch (`module_rtc/README.md:11`, `video_proxy` `MINIO_ENDPOINT` `config.py:60`) vs other object store; retention (7d Free / 90d Pro, `license_service.py:16,22`); at-rest encryption | `security.md` requires at-rest encryption on any store holding this; retention ties to tier |
| 8.6 | **Live-status projection location** — extend each receiver to write the `live/offline` record directly vs a hub-api job subscribing to their already-emitted events (§4.1); normalize YouTube's runtime-registration onto Twitch's `community_servers` model; embed vs link per platform | Detection already exists (Twitch `eventsub_handler.py:215-236`, YouTube `webhook_handler.py`/`youtube_client.py`); only the projection home + channel-model unification are unsettled |
| 8.7 | **UDP media exposure & rootless** (§7.1) — UDP Gateway/LB vs `hostNetwork`/`NET_ADMIN` ROOT EXCEPTION vs XDP/AF_XDP; **only bites if 8.1 = build** | Beta/prod forbid NodePort/hostPort (`security.md`); a first-party SFU's media UDP must reach clients within that constraint. If 8.1 = external, LiveKit owns this |
| 8.8 | **Absorption sequencing & catalog aliases** — how long `social.rtc` / `video_proxy.*` keys stay aliased before removal (§6) | Consumers of the old keys (`.PLAN:52-62` catalog, PostHog flags, entitlement) must migrate first |
| 8.9 | **Ingest protocols at launch** — RTMP + HLS; is AV1/WebRTC-**ingest** in v3 scope or forward-only? | Affects whether ingest and RTC share one WebRTC stack on day one |
| 8.10 | **Metered-token enforcement posture** — block-new vs allow-overage (§5 recommendation) | Deferred to `2026-08-31-metered-token-billing-design.md`; svc-streaming only emits usage + checks entitlement |
| 8.11 | **Future streaming action `target_type`** (§3) — add a bundle-addressable streaming target later, or keep svc-streaming control-plane-only | The action enum has no streaming target today (`.PLAN:273-279`); adding one is net-new bundle-SDK work |

---

## 9. Gap list (what's new)

| Today | Becomes |
|---|---|
| **No first-party media engine** — external MarchProxy (RTMP/AV1 transcode) + external LiveKit (SFU); no HLS/RTMP/AV1/ffmpeg code in-repo | Consolidated Rust control plane; first-party media engine is the §8.1 fork (build vs keep external) |
| core/module_rtc — Go 1.24 **LiveKit control plane** (`go.mod:8-11`, `main.go:19`), HTTP-only, skeleton chart | RTC control in Rust `svc-streaming` (§6); Go service retired |
| core/video_proxy_module — Python/Quart **control plane over external MarchProxy** (`README.md:3`); limits in `license_service.py:11-23` | INGEST/FORWARD/RECORD/TRANSCODE control in Rust `svc-streaming` (§6) |
| `k8s/helm/waddlebot/templates/svc-rtc.yaml` — skeleton, `goRuntimeImage`, `modules.social.enabled`, no UDP (`svc-rtc.yaml:5-49`) | `svc-streaming` chart — Rust image, `modules.streaming.enabled`; media ports/UDP only if §8.1 = build (§7) |
| Catalog keys `social.rtc`, `video_proxy.streaming`, `video_proxy.premium_limits` (`.PLAN:52-62`) | `streaming.rtc/ingest/forward/premium_limits` + new `display/record/transcode` (§2, §6) |
| Twitch `stream.online/offline` **already emitted** (`eventsub_handler.py:215-236`) but nothing consumes it for a live-streams UI | consumed by the live-status projection → `svc-presentation` aggregation (§4) |
| YouTube live detection **already exists** (WebSub `webhook_handler.py`; `get_live_broadcasts()` `youtube_client.py:141-178`) but unsurfaced | consumed by the same projection; channel model unified onto `community_servers` (§4.1, §8.6) |
| No community "live streams" aggregation section anywhere | `svc-presentation`-rendered aggregation of own streams + connected-channel live-status (§4) |
| No transcoding-token metering | per-job usage emission to hub-api marketplace + license-server ledger (§5) |
| Forward target stream keys stored inline (`stream_destinations.stream_key`, `database.py:70`) | `penguin-sal` secret refs + rotation (§8.4) |
| No streaming module gating (`svc-rtc` rides `modules.social.enabled`) | `modules.streaming.enabled` — Streaming is its own SCCEBM module (§6) |
