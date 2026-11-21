# Router Module

> The central command routing and event processing engine that receives events from all action modules, determines the appropriate interaction module to handle each command, manages rate limiting and caching, and orchestrates responses back to users.

## Purpose

The Router Module is Waddles's brain for command processing and event distribution. It sits at the intersection of all action modules (Discord, Slack, Twitch, YouTube) and interaction modules (Economy, Games, Polls, AI, Calendar), receiving incoming user commands and intelligently routing them to the correct service handler.

The Router manages critical cross-cutting concerns including rate limiting to prevent abuse, response caching to reduce latency, session tracking to maintain user context, and activity logging for analytics. It also handles translation detection and command validation, ensuring only well-formed, legitimate requests reach interaction modules.

The module uses Quart (async Flask) to handle high throughput with minimal latency, leveraging Redis for distributed caching and rate limiting across multiple instances. It integrates with the Hub, Reputation, Workflow, Browser Source, and Identity modules to provide comprehensive command context to downstream handlers.

## Key Capabilities

- **Multi-Channel Command Reception**: Accepts events from Discord, Slack, Twitch, and YouTube action modules via REST and gRPC
- **Intelligent Command Routing**: Maps user commands to appropriate interaction modules using command registry and module capabilities
- **Rate Limiting & Throttling**: Enforces per-user, per-command, and per-module rate limits with configurable thresholds
- **Response Caching**: Caches command responses to reduce latency for frequently-accessed commands and translation results
- **Translation Detection**: Detects non-English commands and routes to translation services for multilingual support
- **Session Management**: Maintains user context across multiple commands within the same session
- **Activity Tracking**: Logs all command activity for analytics, auditing, and debugging
- **Command Validation**: Validates command syntax, parameters, and user permissions before routing
- **Error Handling & Retries**: Gracefully handles module failures and implements exponential backoff for retries

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
| Source | processing/router_module |
| Language | Python 3.13 |
| Framework | Quart (async Flask) |
| Port | 8000 |
| Maintained by | Penguin Tech Inc |
