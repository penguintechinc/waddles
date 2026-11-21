# Security Core Module

> Comprehensive security system providing spam detection, content filtering, warnings, and cross-platform moderation.

## Purpose

The Security Core Module is the central security enforcement point for communities, offering real-time threat detection and response capabilities. It detects and prevents spam using rate-limiting and pattern analysis, filters inappropriate content through customizable word lists and regex patterns, manages user warnings with automatic escalation, and enforces moderation policies across multiple platforms (Discord, Twitch, Slack, YouTube). The module maintains moderation logs for compliance and synchronizes moderation actions with other modules, ensuring consistent security posture across all community platforms.

## Key Capabilities

- Real-time spam detection with Redis-backed rate limiting
- Content filtering with regex patterns and blocked word lists
- Warning system with automatic escalation and timeouts
- Cross-platform moderation synchronization
- Configurable security policies per community
- Moderation action logging and audit trails
- Custom policy engine for complex rules
- Community-scoped security configurations

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
| Source | core/security_core_module |
| Language | Python |
| Port | 8041 |
| Maintained by | Penguin Tech Inc |
