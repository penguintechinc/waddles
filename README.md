# Waddles

> **One community platform across every channel — powered by sandboxed App Bundles.**
>
> Unify Twitch, Discord, Slack, YouTube, Kick, Microsoft Teams, Mattermost, and Google Chat with extensible moderation, chat automation, live overlays, music queues, streaming, and AI workflows.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](docs/LICENSE.md)
[![Architecture](https://img.shields.io/badge/architecture-v3.0.x%20(alpha)-orange.svg)](docs/ARCHITECTURE.md)
[![Rust Data Plane Spec](https://img.shields.io/badge/spec-Rust%20Data%20Plane%20%2B%20WASM-blueviolet.svg)](docs/superpowers/specs/2026-09-14-rust-data-plane-design.md)
[![Kubernetes](https://img.shields.io/badge/kubernetes-Helm%20v3-blue.svg)](docs/KUBERNETES.md)

---

## Why Waddles?

Modern communities live across chat, voice, and live streams — yet most bots lock you into a single platform or a rigid set of built-in commands.

Waddles separates the **platform** from the **extensions**:

- **Unified Multi-Platform Reach** — Connect Twitch, Discord, Slack, YouTube, Kick, Teams, Mattermost, and Google Chat into one tenant-isolated control plane.
- **SCCEBM Module Architecture** — Built around **S**ocials, **C**ustomer, **C**ommunity, **E**vent, **B**ot, and **M**arketing workflows.
- **App Bundle Marketplace** — Every feature ships as a versioned **App Bundle** (`installed ⊆ available ⊆ activated`). Communities can run multiple bundles side-by-side, upgrade independently, and customize behavior without touching core infrastructure.
- **Zero-Trust Bundle Isolation** — Untrusted or third-party bundles run as credential-less **WASI 0.2 WebAssembly components** inside gVisor (`runsc`) sandboxes, with strict host-enforced capabilities and scopes.

---

## Core Capabilities

- **App Bundle Marketplace & Polyglot SDKs** — Build extensions in **Python, Rust, or JavaScript/TypeScript**, or bring any language that compiles to **WASI 0.2** (C/C++, Go, Zig, Swift, and more).
  - *Learn more:* [App Bundle Authoring Guide](docs/APP_BUNDLE_AUTHORING.md) · [App Bundle SDK Spec](docs/plans/2026-08-31-app-bundle-sdk-design.md)
- **Live Overlays & Music Station** — Per-community music queues (YouTube, Spotify) paired with real-time OBS browser-source overlays (`full_screen`, `media`, `crawler`, `ticker`, `alert_box`, `chat`, `goals`).
  - *Learn more:* [Overlay Browser-Source Guide](docs/guides/overlay-browser-source.md) · [Music Requests Service](docs/music-requests-service.md)
- **Streaming & RTC Control Plane** — Native WebRTC rooms (WHIP/WHEP) and HLS/RTMP/AV1 recording, transcoding, and relay.
  - *Learn more:* [System Architecture](docs/ARCHITECTURE.md) · [RTC Media Exposure](docs/RTC_MEDIA_EXPOSURE.md)
- **Moderation, Reputation & Cross-Platform Sync** — Community-scoped floor bans, native Twitch and Discord unban synchronization, and host-verified OIDC scopes.
  - *Learn more:* [Reputation Service](docs/bundle-reputation-service.md) · [Roles & Scopes](docs/roles-and-scopes.md)
- **AI Routing & Token Billing** — Free local inference, metered models, Bring-Your-Own-Key (BYOK) routing, and a built-in token ledger.
  - *Learn more:* [Token Billing](docs/token-billing-debit.md) · [AI Knowledge Sources](docs/ai-knowledge-sources.md)
- **Enterprise Multi-Tenancy & Compliance** — Hard tenant isolation, SSO (OAuth2, SAML 2.0, OIDC), DSAR inbox workflows, and per-tenant encryption at rest.
  - *Learn more:* [Security Model](docs/SECURITY.md) · [SSO](docs/SSO.md) · [Privacy & Compliance](docs/PRIVACY.md)

---

## Architecture at a Glance

Waddles pairs a high-throughput **Rust data plane** (`svc-*`) over **Valkey Streams** with a **Python/Quart control plane** (`hub-api`), a **React 18 web portal** (`hub-webui`), and **WASI 0.2 App Bundles** isolated in **gVisor**.

For the full pipeline flow diagram, container responsibilities, ports, isolation keys, and typed stage contracts, see **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** and the **[Rust Data Plane Specification](docs/superpowers/specs/2026-09-14-rust-data-plane-design.md)**.

---

## Quick Start

Deploy the alpha stack to a local Kubernetes cluster (MicroK8s or Docker Desktop) with Helm v3:

```bash
git clone https://github.com/penguintechinc/waddles.git
cd waddles

helm install waddlebot ./k8s/helm/waddlebot -n waddlebot --create-namespace \
  -f k8s/helm/waddlebot/values-alpha.yaml
```

See **[docs/QUICKSTART.md](docs/QUICKSTART.md)** for prerequisites, secret provisioning, and first-run setup.

---

## Documentation

| Guide | What's Inside |
|---|---|
| **[Architecture Overview](docs/ARCHITECTURE.md)** | 8-container topology, pipeline flow diagram, ports, and isolation model |
| **[Rust Data Plane & WASM Spec](docs/superpowers/specs/2026-09-14-rust-data-plane-design.md)** | Deep-dive specification for Rust services, WASI 0.2 sandbox, and Valkey Streams |
| **[Quick Start Guide](docs/QUICKSTART.md)** | Step-by-step local deployment and first-run walkthrough |
| **[App Bundle Authoring](docs/APP_BUNDLE_AUTHORING.md)** | End-to-end guide to building, testing, and publishing App Bundles |
| **[App Bundle SDK Spec](docs/plans/2026-08-31-app-bundle-sdk-design.md)** | Stage contracts, WIT interfaces, and manifest schemas |
| **[Kubernetes Deployment](docs/KUBERNETES.md)** | Helm values, overlays, and cluster configuration reference |
| **[Database Architecture](docs/DATABASE.md)** | Schema design, Alembic migrations, and per-service DB accounts |
| **[Security Policy](docs/SECURITY.md)** | Threat model, tenant isolation, and vulnerability reporting |
| **[Contributing](docs/CONTRIBUTING.md)** | Development workflow, testing standards, and PR guidelines |

**Browse all documentation:** [/docs](docs/)

---

## Client Applications

Mobile and desktop client code is maintained in separate repositories (`penguinm` for Android/iOS Flutter apps and `penguind` in `penguin` for the desktop client). Waddles hosts the server-side services and APIs (`hub-api`).

## Acknowledgements

- Command designs inspired by **PenguinTwitchBot by SuperPenguinTV** (GitHub [@Psychoboy](https://github.com/Psychoboy/PenguinTwitchBot)), used with permission. Ideas and design only; no code copied.

## License

**Open Source (AGPL-3.0, with PenguinTech exceptions)** — free for personal, internal, and non-commercial use. A commercial license is required for SaaS hosting, resale, and embedded commercial deployments. SDKs and client libraries are licensed under MIT. See [docs/LICENSE.md](docs/LICENSE.md) and [docs/LICENSING.md](docs/LICENSING.md).

---

**Made with care by [Penguin Tech Inc](https://www.penguintech.io)**
