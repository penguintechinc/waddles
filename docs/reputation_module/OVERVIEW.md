# Reputation Module

> FICO-style reputation scoring system with event-driven processing, premium weight customization, and automated policy enforcement.

## Purpose

The Reputation Module maintains a comprehensive reputation scoring system for community members using a FICO-inspired 300-850 scale. It processes user behavior events (messages, subscriptions, raids, etc.) and applies configurable weights to calculate reputation scores and tier assignments (Exceptional, Very Good, Good, Fair, Poor). The module supports both community-scoped and global reputation tracking, enables premium weight customization for communities, and automatically enforces policies like auto-banning based on reputation thresholds. Integration with the Security Core Module enables coordinated moderation actions.

## Key Capabilities

- FICO-style reputation scoring (300-850 scale)
- Community and global reputation tracking
- Event-driven batch processing with customizable weights
- Tier-based reputation classification
- Leaderboard generation and querying
- Premium weight configuration per community
- Automated policy enforcement (auto-ban, at-risk warnings)
- gRPC and REST API interfaces
- History and audit logging

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
| Source | core/reputation_module |
| Language | Python |
| Port | 8021 (REST) |
| gRPC Port | 50021 |
| Maintained by | Penguin Tech Inc |
