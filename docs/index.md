# Waddles Documentation

Waddles is a multi-platform community and bot platform for Twitch, Discord, Slack, YouTube, Kick,
Teams, Mattermost, and Google Chat, built around an **App Bundle marketplace** on a fixed
ingest → process → action → presentation pipeline.

## What is Waddles?

The platform ships as **8 containers**: `svc-ingest`, `svc-process`, `svc-action`, `svc-core`,
`svc-presentation`, `svc-streaming`, `hub-api`, and `hub-webui`. Behavior — commands, integrations,
overlays — ships as **App Bundles**: versioned packages of per-stage scripts that a tenant installs,
makes available, and a community activates. First-party functionality is built as bundles too;
there is no separate "built-in" code path.

Product capability is organized into 7 modules, **SCCEBM** (+ Streaming): Socials, Customer, Community, Event, Bot, Marketing — each independently toggleable and tier-gated. See
[Architecture](ARCHITECTURE.md) for the full container/module breakdown.

## Key Features

### Fixed pipeline, App Bundle extensibility
- 8-container ingest → process → action → presentation pipeline, each stage independently scalable
- App Bundles installed/available/activated through a 3-tier lifecycle (`installed ⊆ available ⊆ activated`, narrowing global → tenant → community)
- Multiple bundles can run side by side for the same Feature — no single-winner override

### Multi-Platform Support
- **Twitch** — EventSub webhooks, IRC chat, OAuth
- **Discord** — bot events, slash commands
- **Slack** — Events API, slash commands
- **YouTube Live** — live chat, SuperChat
- **Kick** — webhook integration
- **Microsoft Teams**, **Mattermost**, **Google Chat** — webhook/Events API integrations

### Hero features
- **App Bundle marketplace** — vendor submissions, review, install lifecycle, discount codes
- **Music Station** — per-community YouTube + Spotify queue with a live OBS overlay player
- **Presentation & overlays** — `full_screen` / `media` / `crawler` surfaces for OBS browser sources
- **Streaming proxy control plane** — record / forward / transcode / RTC
- **Premium AI routing** — free-local → premium-metered-local → BYOK
- **Metered token billing** — one ledger for streaming-transcode and premium-AI tokens

## Getting Started

```bash
git clone https://github.com/penguintechinc/waddlebot.git
cd waddlebot
helm install waddlebot ./k8s/helm/waddlebot -n waddlebot --create-namespace \
  -f k8s/helm/waddlebot/values-alpha.yaml
```

Full deployment and first-run walkthrough: [Quick Start](QUICKSTART.md).

## Architecture Overview

```
 inbound                                                              outbound
   │                                                                     ▲
   ▼        ┌───────────────┐    ┌───────────────┐    ┌───────────────┐ │
 svc-ingest │ Valkey stream │ svc-process │ Valkey │  svc-action  │─────┘
   │  ────▶ └───────────────┘  ────▶       ────▶   └──────┬────────┘
   │                                                       │ overlay target
   │                                                ┌──────▼─────────┐
   │                                                │ svc-presentation │ overlays + Music Station
   │                                                └──────────────────┘
   │        ┌───────────────┐
   └───────▶│ svc-streaming │ RTC + HLS/RTMP/AV1 record/forward/transcode
            └───────────────┘

  svc-core   identity · security · credentials · entitlement (RustLang, gRPC, every stage depends on it)
  hub-api    admin + tenancy + marketplace + billing + gRPC/REST/MCP (Python/Quart control plane)
  hub-webui  ExpressScript + ReactJS (static-serve/proxy)
```

Messages crossing a stage boundary are typed `flask_core.stream_pipeline` dataclasses
(`PlatformEvent`, `StageEnvelope`), not raw dicts.

Full detail, per-container table, typed stage contract, and current build status:
[Architecture](ARCHITECTURE.md).

## Core Components

| Component | Description | Technology |
|-----------|-------------|------------|
| `svc-core` | Identity, security, credentials, entitlement | RustLang, gRPC |
| `svc-ingest`, `svc-process`, `svc-action` | Event pipeline receivers, routing, and outbound actions | RustLang |
| `svc-presentation` | Overlays + Music Station for OBS browser sources | RustLang |
| `svc-streaming` | RTC + broadcast media control plane | RustLang |
| `hub-api` | Admin, tenancy, marketplace, billing, AI routing, MCP | Python/Quart |
| `hub-webui` | Community/admin web portal static-serve & proxy | ExpressScript + ReactJS |

## Why Waddles?

**For community managers**
- Unified management across 8 chat/streaming platforms
- Web portal for community administration, workflows, and moderation
- App Bundle marketplace for optional capability, without redeploying the platform

**For developers**
- Author an App Bundle against a documented per-stage contract instead of a monolith
- RustLang across `svc-*` containers; Python/Quart for `hub-api`; ExpressScript + ReactJS for `hub-webui`
- OpenAPI-generated REST, gRPC, and MCP surfaces on `hub-api`

**For streamers**
- OBS browser-source overlays and a live Music Station player
- Loyalty, minigames, giveaways, and shoutouts as default App Bundles

## Quick Links

- **[Quick Start](QUICKSTART.md)** — step-by-step deployment
- **[Architecture](ARCHITECTURE.md)** — 8-container pipeline, App Bundle model, build status
- **[App Bundle SDK](plans/2026-08-31-app-bundle-sdk-design.md)** — bundle authoring spec
- **[Enterprise SSO](SSO.md)** — SAML 2.0 / OIDC (Enterprise) and Google (Professional) sign-in: deployment, setup, security model
- **[JWT Verification](JWT_VERIFICATION.md)** — verifier hardening (one alg per verifier, enforced `iss`/`aud`/claims, `kid`), rejection reasons, per-algorithm metric
- **[Contributing](CONTRIBUTING.md)** — how to contribute

## Community & Support

- **GitHub**: [penguintechinc/waddlebot](https://github.com/penguintechinc/waddlebot)
- **Issues**: report bugs and request features on GitHub
- **Company**: [www.penguintech.io](https://www.penguintech.io)
