# Unified Music Module

> Multi-provider music orchestration system unifying Spotify, YouTube, and SoundCloud with queue-based playback and radio streaming capabilities.

## Purpose

The Unified Music Module provides a single, cohesive music experience across multiple streaming platforms and delivery channels. It abstracts provider differences (Spotify, YouTube, SoundCloud) behind a unified API, manages community-isolated playback queues with Redis persistence, supports both on-demand queue playback and automated radio streaming modes, and integrates with browser overlays for live visualization. The module enables flexible mode switching between queue and radio modes, handles provider authentication and token management, and gracefully degrades when providers are unavailable.

## Key Capabilities

- Multi-provider support (Spotify, YouTube, SoundCloud)
- Provider-agnostic unified API
- Community-isolated queue management with Redis persistence
- On-demand queue playback with user request tracking
- Automated radio streaming mode
- Mode controller for seamless switching
- Browser overlay integration for real-time visualization
- Asynchronous architecture for high performance
- Graceful provider failover and degradation

## Documentation Index

| Document | Description |
|---|---|
| [USAGE.md](USAGE.md) | Getting started, running locally, common workflows |
| [API.md](API.md) | Endpoints, request/response formats, error codes |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Internal design, data flows, component breakdown |
| [CONFIGURATION.md](CONFIGURATION.md) | Environment variables, setup, feature flags |
| [TESTING.md](TESTING.md) | Test strategy, mock data, how to run tests |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common issues, debug steps, FAQ |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Version history, migrations |

## Quick Reference

| Item | Value |
|---|---|
| Source | core/unified_music_module |
| Language | Python |
| Port | 8051 |
| Maintained by | Penguin Tech Inc |
