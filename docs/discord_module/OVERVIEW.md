# Discord Module Overview

## Purpose

The Discord module is a message receiver that bridges Discord servers to WaddleBot's central router. It implements a py-cord bot that handles slash commands, prefix commands, button/select interactions, and modals, normalizing all interactions into WaddleBot's standard event format.

## Quick Facts

- **Language**: Python 3.12
- **Framework**: Quart (async HTTP) + py-cord (Discord bot library)
- **Port**: 8003
- **Container**: trigger/receiver/discord_module/Dockerfile
- **Environment Variables**: See [CONFIGURATION.md](CONFIGURATION.md)

## Architecture at a Glance

```
Discord Servers
       |
       | (WebSocket events)
       v
  py-cord Bot
       |
       +---> Event normalization
       |
       v
  DiscordBotService (manages bot, registers commands)
       |
       +---> InteractionHandler (builds UI elements)
       |
       v
  WaddleBot Router API
       |
       v
   Core Services
```

## Key Components

### DiscordBotService
Manages the py-cord bot instance, handles all Discord events (message creation, interactions, guild joins), registers slash command groups dynamically, and forwards normalized events to the WaddleBot router.

### InteractionHandler
Builds Discord UI elements (modals, buttons, select menus, embeds) from router responses. Manages interaction context and response rendering.

### Event Normalization
All Discord interactions are normalized to a standard WaddleBot format:
- `entity_id`: `guild:channel` format
- `message_type`: `slashCommand`, `chatMessage`, or `interaction`
- `platform`: `"discord"`

## Slash Command Groups

The module registers 20+ slash command groups with the Discord API:

- `/waddlebot` - Core WaddleBot commands
- `/form` - Form submission and management
- `/poll` - Create and manage polls
- `/ticket` - Support ticket system
- `/balance` - Check user balances
- `/give` - Transfer balance/currency
- `/slots` - Slot machine game
- `/duel` - 1v1 duel commands
- `/giveaway` - Giveaway management
- `/quote` - Quote system
- `/bookmark` - Bookmark messages
- `/remind` - Reminders
- `/lfg` - Looking for group
- `/event` - Event management
- `/rsvp` - Event RSVP
- `/so` - Shoutout command
- `/translate` - Message translation
- `/clip` - Clip management
- `/alias` - Custom command aliases
- `/ask` - Question asking
- `/rep` - Reputation system
- `/label` - Label management
- `/top` - Rankings/leaderboards
- `/context` - Admin context switching
- `/link` - Admin server linking
- `/feedback` - User feedback

## Features

- **Slash Command Autocomplete**: Dynamic suggestions for command parameters
- **Rich Interactions**: Buttons, select menus, and modal forms
- **Message Splitting**: Automatic handling of Discord's 2000-character limit
- **Credential Management**: Database-backed credentials with Redis caching
- **Admin Commands**: `/context` and `/link` with admin-only restrictions
- **Event Logging**: Comprehensive logging of all interactions
- **Health Checks**: `/health` and `/metrics` endpoints for monitoring

## Integration Points

### Router API (`ROUTER_API_URL`)
All user interactions are forwarded to the router's event processing endpoint. The router returns structured responses (text, embeds, buttons, modals) that are rendered back to Discord.

### Core API (`CORE_API_URL`)
Used for credential lookup, user validation, and feature checks.

### Database (`DATABASE_URL`)
Stores guild configurations, user credentials, and interaction metadata.

### Redis (`REDIS_URL`)
Caches credential lookups and manages distributed interaction state.

## Event Flow Example

1. User types `/balance` in a Discord server
2. py-cord detects slash command and calls handler
3. DiscordBotService normalizes to WaddleBot event format
4. Event sent to ROUTER_API_URL with `entity_id=guild:channel`
5. Router processes and returns balance data
6. InteractionHandler renders response as Discord embed
7. Embed posted back to Discord channel

## Ports and Endpoints

- **HTTP Server**: Port 8003
  - `GET /api/v1/status` - Service status
  - `GET /api/v1/bot/guilds` - List connected guilds
  - `GET /health` - Health check
  - `GET /metrics` - Prometheus metrics

## Dependencies

Core dependencies (see `requirements.txt`):
- `quart` - Async HTTP framework
- `hypercorn` - ASGI server
- `py-cord>=2.4.1` - Discord bot library
- `httpx` - Async HTTP client
- `redis` - Distributed caching
- `pydal` - Database abstraction
- `python-dotenv` - Environment configuration
- `flask_core` - WaddleBot core utilities
- `platform_receiver` - Platform normalization

## Logging

All operations are logged to stdout/stderr. Log level controlled by `LOG_LEVEL` environment variable (default: `INFO`).

```
[2026-02-24 10:15:30] INFO: Discord bot connected as @WaddleBot
[2026-02-24 10:15:35] INFO: Guild 123456789 joined
[2026-02-24 10:15:40] DEBUG: Slash command /balance from user 987654321
```

## Next Steps

- **First Time Setup**: See [CONFIGURATION.md](CONFIGURATION.md) for environment variables and bot setup
- **Local Development**: See [TESTING.md](TESTING.md) for development environment
- **API Details**: See [API.md](API.md) for endpoint documentation
- **Architecture Details**: See [ARCHITECTURE.md](ARCHITECTURE.md) for internal design
