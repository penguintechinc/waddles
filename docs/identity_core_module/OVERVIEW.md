# Identity Core Module

> Unified cross-platform identity management service providing OAuth2 integration, user authentication, and identity linking across Twitch, Discord, YouTube, and other platforms with dual REST and gRPC interfaces.

## Purpose

The Identity Core Module is the authentication and identity backbone of Waddles, managing user identity across multiple streaming and social platforms. It provides a unified hub user system that bridges platform-specific identities to a single logical user within Waddles, enabling seamless cross-platform functionality.

The module handles OAuth2 authentication flows for Twitch, Discord, YouTube, and other platforms, manages platform account linking, performs identity lookups and resolution, maintains user profiles, and manages API keys for service-to-service authentication. It exposes both REST API and gRPC interfaces, allowing real-time synchronous gRPC calls for performance-critical identity operations and RESTful access for administrative and integration tasks.

The Identity Core Module is upstream of all user-facing modules, providing identity verification and user resolution for the Community Module, Hub Module, and all feature modules. It maintains the authoritative user database and manages the relationships between platform-specific identities and the unified hub user identity.

## Key Capabilities

- OAuth2 authentication for Twitch, Discord, YouTube, and other platforms
- Cross-platform identity linking and account consolidation
- API key generation and management for service authentication
- Fast identity lookups via gRPC interface
- RESTful identity APIs for admin and integration workflows
- User profile management and history tracking
- Platform-specific credential handling and refresh token management
- Identity resolution from platform-specific IDs to hub users
- Session and token management

## Documentation Index

| Document | Description |
|---|---|
| [USAGE.md](USAGE.md) | Getting started, OAuth flows, identity linking workflows |
| [API.md](API.md) | REST endpoints, gRPC service definitions, request/response |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Dual protocol design, data flows, components |
| [CONFIGURATION.md](CONFIGURATION.md) | OAuth secrets, environment variables, platform setup |
| [TESTING.md](TESTING.md) | Test strategy, mock OAuth flows, integration tests |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common issues, OAuth errors, debug steps |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Version history, API changes, migrations |

## Quick Reference

| Item | Value |
|---|---|
| Source | core/identity_core_module |
| Language | Python 3.13 (Quart + gRPC) |
| REST Port | 8050 |
| gRPC Port | 50030 |
| Database | PostgreSQL |
| Dual Protocol | REST API + gRPC server |
| Maintained by | Penguin Tech Inc |
