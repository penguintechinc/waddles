# Waddles System Architecture

**Architecture generation:** v3.0.x (alpha, `release/v3.0.X`)

This is the reference architecture doc. For a quick orientation, see the [README](../README.md#architecture-8-containers).
For the App Bundle authoring spec, see [`docs/plans/2026-08-31-app-bundle-sdk-design.md`](plans/2026-08-31-app-bundle-sdk-design.md).

## Table of Contents

- [The 8 containers](#the-8-containers)
- [Pipeline flow](#pipeline-flow)
- [App Bundle model](#app-bundle-model)
- [Distribution & control plane](#distribution--control-plane)
- [Module ownership (SCCEBM)](#module-ownership-sccebm)
- [Feature flags & licensing](#feature-flags--licensing)
- [Build status caveats](#build-status-caveats)
- [Technology stack](#technology-stack)

---

## The 8 containers

`svc-*` = pipeline/service containers; `hub-*` = hub. The marketplace is a **module inside
hub-api**, not its own container.

| Container | Responsibility | Language | HTTP port | gRPC port |
|---|---|---|---|---|
| `svc-ingest` | Platform receivers + inbound webhooks; loads each activated bundle's `ingest` component | RustLang | 8200 | — |
| `svc-process` | Event bus, command routing, workflow orchestration; bundles' `process` component | RustLang | 8201 | — |
| `svc-action` | Outbound actions/interactions/3rd-party calls; bundles' `action` component + target adapters | RustLang | 8202 | — |
| `svc-core` | Identity, security, credentials, entitlement — synchronous gRPC, every stage depends on it | RustLang | 8203 | 50203 |
| `hub-api` | Admin, tenancy, marketplace, billing, AI routing, MCP — control plane | Python/Quart | 8204 | 50204 |
| `hub-webui` | SPA assets, static-serve + `/api` proxy for the ReactJS webui | ExpressScript + ReactJS | 8205 | — |
| `svc-presentation` | Core overlays (`full_screen`/`media`/`crawler`/`ticker`/`alert_box`/`chat`/`goals`) + Music Station + bundles' `presentation` component; every push is detokenized + HTML-escaped before it is fanned out to the browser page ([guide](guides/overlay-browser-source.md)) | RustLang | 8207 | — |
| `svc-streaming` | RTC + HLS/RTMP/AV1 record/forward/transcode control plane | RustLang | 8208 | 50208 |

`svc-core` is synchronous gRPC, not a pipeline stage — every other container calls it directly for
auth/entitlement checks rather than going through a Valkey queue, since those calls block the
caller and can't tolerate queue latency.

A ninth container, **`svc-rtc`** (Go, WebRTC/SFU via LiveKit, port 8206), still runs standalone.
It is the design's absorption target into `svc-streaming` (RTC capability) but that merge has not
happened — `svc-rtc` and `svc-streaming` are two containers today, gated independently in Helm.

## Pipeline flow

```
 inbound                                                              outbound
   │                                                                     ▲
   ▼        ┌───────────────┐    ┌───────────────┐    ┌───────────────┐ │
 svc-ingest │ Valkey stream │ svc-process │ Valkey │  svc-action  │─────┘
   │  ────▶ └───────────────┘  ────▶       ────▶   └──────┬────────┘
   │                                                       │ action target_type: overlay
   │                                                ┌──────▼─────────┐
   │                                                │ svc-presentation │
   │                                                └──────────────────┘
   │        ┌───────────────┐
   └───────▶│ svc-streaming │ (independent path — RTC/broadcast, not ingest→process→action)
            └───────────────┘
```

Each stage publishes onto the **next** stage's own Valkey stream — `process` never calls `action`
directly, and `action` never blocks `process`. A slow or backed-up `action` handler only fills its
own queue, spilling to a DLQ on overflow; `ingest` and `process` keep running.

**Isolation keys** — every stream, config, and state key is scoped per `(tenant, community,
app_id, stage)`:

```
waddles:t:{tenant}:c:{community}:app:{app_id}:{ingest|process|action}
waddles:t:{tenant}:c:{community}:app:{app_id}:cfg
waddles:t:{tenant}:c:{community}:app:{app_id}:state
```

Tenant-wide activations (no specific community) use a literal `c:_tenant` segment so every key
stays uniformly parseable. Consumer groups: `{app_id}:{stage}-group`.

### Typed stage contract

Every message crossing a stage boundary is a frozen `@dataclass(slots=True, frozen=True)` from
`flask_core.stream_pipeline` (re-exported from `flask_core`), never a raw dict — round-tripped
through Valkey as JSON (`json.dumps(envelope.to_dict())` on LPUSH, `StageEnvelope.from_dict(
json.loads(raw))` on RPOP):

- **`PlatformEvent`** — `{platform, event_type, actor, payload, occurred_at}`. What an `ingest`
  bundle's `normalize()` returns.
- **`StageEnvelope`** — `{tenant, community, app_id, stage, event, ts}`. The actual queue message.
  The carried event lives under **`event`**, deliberately not `payload` — a bundle always reaches
  its data at `envelope.event.payload[...]`, never `envelope.payload[...]`, so a
  payload-under-payload double-nesting bug is structurally impossible.

Bundle entrypoint contracts per stage:

| Stage | Signature |
|---|---|
| `ingest` | `normalize(raw: dict) -> PlatformEvent` |
| `process` | `transform(event: PlatformEvent) -> PlatformEvent` |
| `action` | `send(envelope: StageEnvelope, config, *, http_client) -> TransportResult` |

A malformed or legacy-shaped message (missing `event`, wrong field types) raises `EnvelopeError`
on read — refused, never silently coerced.

## App Bundle model

```
Core → Module → Feature → App (Bundle)
```

An **App Bundle is the App** — not a grouping of Apps. It's `bundle.yaml` (the manifest) plus a
`{config, spec, script}` directory per pipeline stage it implements:

```
giveaway-classic/
├── bundle.yaml
├── ingest/{handler.py, config.yaml, spec.yaml}
├── process/{handler.py, config.yaml, spec.yaml}
├── action/{handler.py, config.yaml, spec.yaml}
└── presentation/                 # optional 4th component — no script, no queue hop
    ├── overlay.html
    ├── overlay.js
    └── overlay.css
```

A bundle may implement 1–4 components, omitting any it doesn't touch. `presentation` is not a
pipeline stage: no `entrypoint`, no DLQ, no idempotency contract — it's a client-side HTML/JS
overlay `svc-presentation` serves per-community at a `browser_source_path`, optionally reading a
stream directly from its own JS for live data.

### Execution models

| | Native | Third-party |
|---|---|---|
| Runs | In-process inside the stage-runner, loaded via `entrypoint` | Always a network hop — never in-process |
| Stage scope | May span all 3 script stages | Exactly one stage per manifest block |
| Transport | Direct function call | `webhook_push` (HMAC-SHA256 signed) or `rest_pull` (bearer/HMAC) |
| Failure mode | Exception → DLQ | Timeout (default 5000ms) or non-2xx → DLQ |

Going forward, all App Bundles execute within a sandboxed **gVisor + WASM runtime environment**, ensuring strong multi-tenant isolation, memory safety, and controlled syscall capability gating across native and third-party bundles.

### Standardized action targets

Every `action`-stage bundle declares one `target_type`; the platform provides one adapter per type,
all implemented in `svc-action`:

| `target_type` | Adapter behavior |
|---|---|
| `webhook` | SSRF-guarded POST, HMAC-SHA256 signed |
| `rest_api` | SSRF-guarded, configurable HTTP method |
| `grpc_api` | Internal gRPC call |
| `graphql_api` | GraphQL mutation/query |
| `email` | Real SMTP via `aiosmtplib` |
| `overlay` | SSRF-guarded push to `svc-presentation`, sub-surfaces `full_screen` / `media` / `crawler` |
| `message_queue` | Valkey `PUBLISH` |

Retries with exponential backoff on 5xx/network failures; **never** retries 401/403/other 4xx.
Every dispatch is audit-logged (`action_dispatch_log`).

### 3-tier lifecycle

Strict subset invariant: `activated ⊆ available ⊆ installed`.

| Tier | Scope | Table | Meaning |
|---|---|---|---|
| **Installed** | Global/platform | `app_catalog` | Which bundles exist on the platform at all |
| **Available** | Tenant | `app_tenant_availability` | Which installed bundles a tenant may activate |
| **Activated** | Community — a **set** | `app_activations` | Which available bundles a community has turned on |

### Coexistence, not single-winner

`resolve_apps(feature, tenant, community)` returns **every** enabled, activated bundle for a
Feature — union, not narrowest-scope-wins. A community can run 4 giveaway bundles side by side;
each gets its own isolated stream/config/state under the key scheme above. Conflicts are explicit:
`incompatible_with` (symmetric — either side declaring it is enough) blocks co-activation at the
`app_activations` write. The community must deactivate one bundle before the other can be turned
on — mutual exclusion is a manual choice, never auto-resolved.

## Distribution & control plane

Three planes, each hitting a different DB tier for a different reason:

| Plane | What | Who | DB tier |
|---|---|---|---|
| **Control (write)** | Install/uninstall/update a bundle | hub-api — sole writer to `app_catalog` | Primary |
| **Code distribution (pull)** | `GET /api/v1/apps/installed?stage=X`, `GET /api/v1/apps/{app_id}/artifact` | Each stage-runner **polls** on an interval, fetches only changed `artifact_hash`es | hub-api API, not DB-direct |
| **Routing (hot path)** | Which activated bundles fan out for `(tenant, community, feature)` on every event | Stage-runners read directly | **Read replica** — never hub-api, never primary |

Polling (not push) means a restarting or newly-scaled-out runner self-heals to the current
installed set on its own next poll — hub-api never needs to know a given replica exists. Routing
reads are cached with a short TTL, invalidated on the stage-runner's own poll cycle.

## Module ownership (SCCEBM)

7 product modules (SCCEBM + Streaming), each a Helm-toggleable grouping of default App Bundles
(`modules.<name>.enabled`), plus Core (mandatory, no toggle):

| Module | Owns |
|---|---|
| **Socials** | External social-media posts/responses/analytics (LinkedIn, X, Facebook, Instagram, TikTok) |
| **Customers** | CRM — accounts, contacts, opportunities, cases |
| **Community** | Forum, chat, virtual stages, polls, announcements, raffles |
| **Event** | Calendar, ticketing |
| **Marketing** | Campaigns, shortlinking, emailing |
| **Bot** | Platform receivers, outbound actions, interaction commands |
| **Streaming** | `svc-streaming` + hub-api's stream/calls/overlay/Music Station control plane |

**Core** (always deployed, no module toggle, but its Features still tier-gate): identity, security,
credentials, tenancy (the **community entity** — teams/OUs — lives here, not in the Community
module), marketplace, token/billing, analytics, event bus.

`KNOWN_MODULES` in `libs/flask_core/flask_core/app_manifest.py` carries the canonical SCCEBM set
plus two transitional aliases (`social`, `customer`, singular) kept because pre-migration Feature
contracts still register under those names — a follow-up rename, not yet done.

## Feature flags & licensing

```
MODULE global toggle (Helm: modules.<name>.enabled)
        │
PER-CAPABILITY PostHog flag  waddles.{module}.{feature}   (new flags default OFF)
        │
TIER gate (license entitlement: free / professional / enterprise)
```

A Feature is available iff **both** gates pass. Unreachable license/flag server degrades to the
last-known cached value — never fails open on a flag it has never seen. Entitlement resolution
order: community → tenant (narrowest first).

## Build status caveats

Read this before assuming a container is production-ready:

- **App code is real** for `svc-ingest`, `svc-process`, `svc-action`, `svc-presentation`,
  `svc-streaming`, `hub-api`, and `hub-webui` — each has a working implementation, tests, and
  its own `Dockerfile`.
- **Helm chart wiring lags the code**: every pipeline container's Deployment template still pins
  `image:` to a shared placeholder base digest (`pipeline.pythonBaseImage` /
  `pipeline.nodeBaseImage` in `values.yaml`) — no CI build publishes a per-container image yet.
- **`svc-core` is the exception** — no consolidated container exists. Identity, security, and
  credential logic still run as three separate module services (`core/identity_core_module`,
  `core/security_core_module`, `core/credential_manager_module`); `svc-core.yaml` is a Helm
  skeleton with no backing app code.
- **The 8-container Helm templates coexist with ~31 legacy per-module Deployments** carried over
  from v2.2.x (router, per-platform collectors, action-platforms, etc.) — nothing has been deleted.
  The legacy set still serves most live functionality during cutover. Full 31→8 mapping:
  [`k8s/helm/waddlebot/PIPELINE_MAPPING.md`](../k8s/helm/waddlebot/PIPELINE_MAPPING.md).
- **`svc-streaming` fronts external engines**, it does not implement its own media transport: an
  internal RTMP/AV1 transcode proxy and LiveKit (SFU) sit behind it today. A native Rust data
  plane is a documented future direction (`docs/plans/2026-08-31-svc-streaming-design.md`).
- **Music Station**: the presentation layer (player, overlay, live SSE push to OBS) and the
  YouTube/Spotify provider resolvers (normalized `Track` model, `hub_api/services/music_providers/`)
  are real. Per-source `ingest` bundles, a shared `process`-stage queue manager, and SoundCloud
  support are not yet built — the presentation layer reads an existing queue key format directly.

## Technology stack

**Services (`svc-*`):** RustLang
**Control Plane (`hub-api`):** Python 3.13, Quart (async)
**Web UI (`hub-webui`):** ExpressScript + ReactJS (React 18), Vite, TailwindCSS v4
**Infrastructure:** Docker, Kubernetes (Helm v3), GitHub Actions
**Data & Caching:** PostgreSQL, Valkey, SeaweedFS (S3), Qdrant (vectors)

## See also

- [`docs/plans/2026-08-31-app-bundle-sdk-design.md`](plans/2026-08-31-app-bundle-sdk-design.md) — full bundle authoring spec
- [`docs/plans/2026-08-31-v3-sccebm-program-plan.md`](plans/2026-08-31-v3-sccebm-program-plan.md) — program roadmap and phase history
- [`docs/architecture/core-boundary.md`](architecture/core-boundary.md) — per-file module-ownership evidence
- [`k8s/helm/waddlebot/PIPELINE_MAPPING.md`](../k8s/helm/waddlebot/PIPELINE_MAPPING.md) — legacy container → pipeline container mapping
