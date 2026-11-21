# Browser Source Core Module

> A dual-protocol microservice providing browser source management for OBS with real-time caption streaming via WebSockets and secure overlay token validation via REST and gRPC.

## Purpose

The Browser Source Core Module bridges Waddles and OBS, enabling streamers to display live captions, alerts, and overlays directly in their broadcast. It provides secure token-based overlay access where each community has a unique overlay key, preventing unauthorized access to sensitive content while allowing seamless integration with OBS.

The module operates on two channels: (1) REST API for overlay token management and browser source setup, and (2) WebSocket connections for real-time caption distribution. When the Caption Service sends captions, they're instantly broadcast to all WebSocket clients connected to a community, allowing caption overlays to update in near real-time. The module also provides gRPC endpoints for programmatic overlay management by admin tools.

This module is essential for professional streaming workflows—streamers depend on it to maintain a polished, captioned broadcast without manual intervention.

## Key Capabilities

- **Browser Source Integration**: Unified HTML overlay combining captions, alerts, and widgets for OBS display
- **Real-Time Caption Streaming**: WebSocket-based caption distribution with sub-second latency to all connected clients
- **Token-Based Overlay Access**: Secure, time-limited overlay keys that can be rotated without affecting active sessions
- **Grace Period Management**: Validates both current and next overlay keys to enable seamless token rotation
- **Access Logging**: Records all overlay access attempts for security auditing
- **REST and gRPC APIs**: Multiple protocol support for integration flexibility
- **Recent Caption History**: New WebSocket clients receive recent caption history on connection
- **Automatic Cleanup**: Disconnects stale clients and cleans up unused resources

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
| Source | core/browser_source_core_module |
| Language | Python 3.13 |
| Framework | Quart (async Flask) |
| REST Port | 8027 |
| gRPC Port | 50050 |
| Maintained by | Penguin Tech Inc |
