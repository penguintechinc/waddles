# Waddles

> **Multi-platform community & bot platform — App Bundle marketplace on an ingest → process → action → presentation pipeline.**
>
> Twitch, Discord, Slack, YouTube, Kick, Teams, Mattermost, and Google Chat in one platform, extended by an App Bundle marketplace.

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL%203.0-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Architecture](https://img.shields.io/badge/architecture-v3.0.x%20(alpha)-orange.svg)](docs/ARCHITECTURE.md)
[![Rust Data Plane Spec](https://img.shields.io/badge/spec-Rust%20Data%20Plane%20%2B%20WASM-blueviolet.svg)](docs/superpowers/specs/2026-09-14-rust-data-plane-design.md)
[![Kubernetes](https://img.shields.io/badge/kubernetes-Helm%20v3-blue.svg)](https://kubernetes.io/)

---

## What is Waddles?

Waddles is a chat bot and community platform built around the **SCCEBM** module set — **S**ocials, **C**ustomer, **C**ommunity, **E**vent, **B**ot, **M**arketing.

Every capability ships as an **App Bundle**: a versioned package of per-stage components (`process`, `action`, `presentation`) that a tenant installs, makes available, and a community activates. The platform provides the pipeline, identity, entitlement, and marketplace; App Bundles provide the behavior.

## Architecture: 8 Containers & Data Plane

Waddles runs on a fixed 8-container pipeline:

```
 inbound                                                              outbound
   │                                                                     ▲
   ▼        ┌───────────────┐    ┌───────────────┐    ┌───────────────┐ │
 svc-ingest │ Valkey stream │ svc-process │ Valkey │  svc-action  │─────┘
   │  ────▶ └───────────────┘  ────▶       ────▶   └──────┬────────┘
   │                                                       │ overlay target
   │                                                ┌──────▼─────────┐
   │                                                │ svc-presentation │ overlays + Music Station
   │                                                └──────────────────┘ (OBS browser sources)
   │        ┌───────────────┐
   └───────▶│ svc-streaming │ RTC + HLS/RTMP/AV1 record/forward/transcode
            └───────────────┘

  svc-core   identity · security · credentials · entitlement (RustLang, gRPC :50203)
  hub-api    admin + tenancy + marketplace + billing + gRPC/REST/MCP (Python/Quart control plane)
  hub-webui  ExpressScript + ReactJS (static-serve / API proxy)
```

### Core Container Responsibilities

| Container | Responsibility | Stack | Port |
|---|---|---|---|
| `svc-ingest` | Inbound platform receivers (Twitch, Slack, Discord, Kick, YouTube, generic intake) | RustLang | 8200 |
| `svc-process` | Event routing, workflow orchestration & `process`-stage WASM execution | RustLang | 8201 |
| `svc-action` | Outbound actions, target adapters & `action`-stage WASM execution | RustLang | 8202 |
| `svc-core` | Identity, security, credentials & entitlement (gRPC) | RustLang | 8203 / 50203 |
| `svc-presentation` | Core overlays (`full_screen`/`media`/`crawler`) + Music Station | RustLang | 8207 |
| `svc-streaming` | RTC + broadcast media control plane | RustLang | 8208 / 50208 |
| `hub-api` | Control plane: admin, tenancy, marketplace, billing, AI routing & MCP | Python/Quart | 8204 / 50204 |
| `hub-webui` | Admin portal & community web UI | ExpressScript + ReactJS | 8205 |

For in-depth service architectures, transport security, and stream specifications, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and the [`Rust Data Plane Specification`](docs/superpowers/specs/2026-09-14-rust-data-plane-design.md).

## App Bundle Model & WASM Sandbox

- **Logical Hierarchy**: `Core → Module → Feature → App (Bundle)`.
- **Security & Multi-Tenant Isolation**: All App Bundles execute as **WASI 0.2 WebAssembly components** inside a credential-less executor (`bundle-executor`) under a **gVisor `RuntimeClass` (`runsc`)**, providing memory safety and strict capability-scoped isolation.
- **Polyglot Language Portability**: WebAssembly provides full language freedom — **any application or language that compiles to WASI 0.2 can run as an App Bundle**. Tier 1 SDKs support Python, Rust, and JavaScript/TypeScript, while prebuilt WASI 0.2 components compiled from any language (C/C++, Go, Zig, Swift, etc.) are supported out of the box (Tier 2).
- **Lifecycle & Coexistence**: 3-tier lifecycle (`installed ⊆ available ⊆ activated`). Multiple bundles for the same Feature can run side-by-side per community.

For full bundle creation guidelines, see the [`App Bundle SDK Specification`](docs/plans/2026-08-31-app-bundle-sdk-design.md).

## Technology Stack

- **Data Plane (`svc-*`)**: RustLang (Axum, tokio, SeaORM, gRPC, OTel)
- **Control Plane (`hub-api`)**: Python 3.13 / Quart (async)
- **Web UI (`hub-webui`)**: ExpressScript + ReactJS (React 18), Vite, TailwindCSS v4
- **App Bundle Runtime**: Sandboxed WASI 0.2 WASM components in gVisor (`runsc`)
- **Pipeline Spine**: Valkey Streams (TLS + ACL authenticated)
- **Storage & Infrastructure**: PostgreSQL, Valkey, SeaweedFS (S3), Qdrant, Docker, Kubernetes (Helm v3)

## Quick Start

```bash
git clone https://github.com/penguintechinc/waddles.git
cd waddles

# Local / Alpha (MicroK8s or Docker Desktop)
helm install waddlebot ./k8s/helm/waddlebot -n waddlebot --create-namespace \
  -f k8s/helm/waddlebot/values-alpha.yaml
```

See **[docs/QUICKSTART.md](docs/QUICKSTART.md)** for detailed deployment and first-run instructions.

## Hero Features

- **App Bundle Marketplace** — vendor submission, review, and multi-tier lifecycle management.
- **Music Station** — per-community queue mixing YouTube and Spotify with live OBS overlay integration.
- **Presentation & Overlays** — `full_screen`, `media`, and `crawler` OBS browser-source overlay surfaces.
- **Streaming Proxy Control Plane** — RTMP/AV1 record, transcode, relay, and RTC control plane.
- **AI Routing & Token Billing** — free local models, metered local inference, BYOK routing, and token ledger.

## Documentation Map

| Guide | Description |
|---|---|
| **[Architecture Overview](docs/ARCHITECTURE.md)** | Container architecture, pipeline flow, and module ownership |
| **[Rust Data Plane & WASM Spec](docs/superpowers/specs/2026-09-14-rust-data-plane-design.md)** | Technical specification for Rust services, WASM sandbox, and Valkey Streams |
| **[Quick Start Guide](docs/QUICKSTART.md)** | Helm deployment and first-time setup walkthrough |
| **[App Bundle SDK Spec](docs/plans/2026-08-31-app-bundle-sdk-design.md)** | Bundle authoring, stage contracts, and manifest reference |
| **[Kubernetes Deployment](docs/KUBERNETES.md)** | Helm values and Kubernetes configuration reference |
| **[Database Architecture](docs/DATABASE.md)** | Schema, migrations, and database access design |
| **[Contributing](docs/CONTRIBUTING.md)** | Guidelines for developing bundles and contributing code |
| **[Security Policy](docs/SECURITY.md)** | Vulnerability reporting and security model |

**Browse all docs:** [/docs](docs/)

## License

**Open Source (GPL-3.0)** — free for personal, internal, and educational use. Commercial license required for SaaS hosting and embedded commercial deployments. See [LICENSE.md](LICENSE.md).

---

**Made with care by [Penguin Tech Inc](https://www.penguintech.io)**
