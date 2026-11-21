# Shoutout Interaction Module

> Automated and manual shoutout system that generates text and video shoutouts for streamers using Twitch and YouTube platform integrations.

## Purpose

The Shoutout Interaction Module provides a comprehensive platform for generating both text-based and video-based shoutouts for content creators. It integrates with the Twitch Helix API to fetch real-time streamer data (including user information, channel details, and stream status) and combines this with video content from Twitch clips and YouTube videos. The module supports customizable templates for different communities and platforms, enabling streamers to create personalized shoutouts that engage their audiences.

Beyond manual shoutouts triggered by chat commands (!so and !vso), the module includes auto-trigger functionality that can automatically initiate shoutouts based on community-configured events such as first messages, raids, and hosts. This automation is particularly valuable for large streaming communities where manual moderation of every shoutout would be impractical.

The module is built on async Python using Quart framework and implements enterprise-grade reliability features including circuit breaker pattern for API resilience, cross-platform identity resolution for fallback support, and comprehensive permission checking to ensure only authorized users can trigger shoutouts in their communities.

## Key Capabilities

- **Text Shoutouts**: Generate platform-aware shoutout messages with customizable templates (Twitch, Discord, Slack)
- **Video Shoutouts**: Display video clips from Twitch or YouTube with channel information during shoutouts
- **Twitch Helix API Integration**: Real-time user, channel, and stream data fetching with circuit breaker protection
- **Cross-Platform Video Lookup**: Attempt Twitch clips first, fall back to YouTube videos if unavailable
- **Identity Resolution**: Resolve linked accounts across platforms (Twitch, Discord, YouTube) using hub_user_identities
- **Auto-Trigger Support**: Automatically trigger shoutouts on configurable events (first message, raids, hosts)
- **Permission Checking**: Multi-level permission system (admin_only, mod, vip, subscriber, everyone)
- **Cooldown Management**: Prevent shoutout spam with per-community, per-user, and global cooldown configurations
- **History Tracking**: Store all shoutouts for community analytics and moderation
- **Template Customization**: Per-community templates with variable substitution support
- **Role-Based Access Control**: Respect user roles (mod, vip, admin) for permission decisions

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
| Source | action/interactive/shoutout_interaction_module |
| Language | Python 3.13 (Quart async framework) |
| Port | 8011 |
| Version | 2.0.0 |
| Database | PostgreSQL (asyncpg connection pool) |
| API Framework | Quart (async) with flask_core wrapper |
| Maintained by | Penguin Tech Inc |
| Status | Production Ready |

## Module Architecture Overview

The module consists of five core services orchestrated by the main Flask app:

1. **TwitchService** - Fetches user/channel/stream data from Twitch Helix API with circuit breaker protection
2. **ShoutoutService** - Generates text shoutout messages with template substitution
3. **VideoService** - Retrieves video clips from Twitch/YouTube with channel metadata
4. **IdentityService** - Resolves cross-platform linked identities
5. **VideoShoutoutService** - Orchestrates video shoutouts with permission/cooldown checks

All services are async-first, properly handle timeouts and errors, and integrate with the enterprise logging framework (AAA logging for audit trails, action tracking, access control).

## Integration Points

- **Twitch Helix API**: For user, channel, and stream information
- **YouTube Data API**: For video search and metadata
- **Identity Core Module**: Cross-platform identity lookups
- **PostgreSQL Database**: Persistent storage of configurations, history, and credentials
- **Redis** (optional): Credential refresh notifications and real-time updates
- **Router Service**: Core API gateway integration
