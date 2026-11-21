# Community Module

> Central multi-platform community management system that coordinates community creation, member relationships, and cross-platform integration for all Waddles communities.

## Purpose

The Community Module provides the core infrastructure for managing communities within Waddles. It handles community creation, membership management, multi-platform integration, and community-scoped settings. Each Waddles installation can host multiple communities, each with their own members, roles, configurations, and integration with external platforms like Twitch, YouTube, and Discord.

The module serves as the foundation for multi-tenant community support, enabling users to create isolated community spaces where members can collaborate across multiple streaming and social platforms. It tracks community membership, handles role-based access control per community, and maintains the relationships between communities and their integrated platforms.

The Community Module integrates closely with the Hub Module (for administration) and Identity Core Module (for user identity resolution), while providing data that feeds downstream features like Reputation Module, Security Module, and all feature modules that operate at the community level.

## Key Capabilities

- Create and manage multiple independent communities
- Track community membership with role-based access control
- Integrate communities with external platforms (Twitch, YouTube, Discord, etc.)
- Support community-scoped configuration and settings
- Manage community members and their permissions
- Provide community health and analytics data
- Handle community lifecycle (creation, activation, archival)

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
| Source | core/community_module |
| Language | Python 3.13 (Quart) |
| Port | 8020 (REST API) |
| Database | PostgreSQL (via PyDAL) |
| Maintained by | Penguin Tech Inc |
