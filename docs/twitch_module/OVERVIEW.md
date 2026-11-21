# Twitch Module Overview

## Purpose

The Twitch Module (trigger/receiver/twitch_module) is a real-time message receiver that bridges Twitch streams to WaddleBot. It handles:

- **Dual-ingestion architecture**: TwitchIO IRC bot + EventSub webhooks for 100% message coverage
- **Chat message routing**: Parses !commands, enforces broadcaster-only restrictions, splits long messages (500-char limit)
- **Event ingestion**: Subscriptions, raids, follows, cheers, stream state changes via EventSub
- **Viewer activity tracking**: Polls Twitch Chatters API every 60s, detects join/leave/heartbeat events
- **Leaderboard integration**: Sends viewer presence to Hub API for real-time viewer rankings
- **Dynamic channel management**: Auto-join/leave based on database state, periodic refresh (300s default)
- **Distributed caching**: Channel lists, community mappings, entity lookups with fallback to API

## Architecture

**Three-tier system:**

1. **Message Ingestion**
   - TwitchBotService: IRC chat monitoring + command handling
   - EventSubHandler: Webhook-based event subscriptions (HMAC-SHA256 verified)

2. **Processing & Routing**
   - Message parsing: Extract command, args, sender, chat badges
   - Broadcaster-only enforcement: Reject non-broadcaster !commands
   - Message splitting: Long responses broken into 500-char chunks
   - Router API integration: POST to `/api/v1/messages` in waddlebot-router

3. **Activity Tracking**
   - ViewerTracker: 60s polling of Chatters API
   - Join/leave/heartbeat detection
   - Hub API integration: POST to `/api/v1/leaderboards/viewers/{channel_id}`

## Key Components

| Component | Purpose | Port | Language |
|-----------|---------|------|----------|
| TwitchBotService | IRC bot (TwitchIO) | 8002 | Python 3.12 |
| EventSubHandler | Webhook receiver (Quart) | 8002 | Python 3.12 |
| ChannelManager | Load/refresh channel list | In-memory | Python 3.12 |
| ViewerTracker | Chatters API polling | In-memory | Python 3.12 |
| TwitchCacheManager | Distributed cache layer | Redis/in-memory | Python 3.12 |

## Technology Stack

- **Framework**: Quart (async HTTP, ASGI)
- **Hypercorn**: ASGI server (production)
- **TwitchIO**: IRC bot library (>=2.8.0)
- **httpx**: Async HTTP client
- **PyDAL**: Database abstraction (channel list, configuration)
- **Redis (optional)**: Distributed cache for channels, community mappings

## Deployment

**Container**: Single-container service

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["hypercorn", "main:app", "--bind", "0.0.0.0:8002"]
```

**Environment**:
- **Port**: 8002 (configurable via MODULE_PORT)
- **Health check**: GET /health (must return 200 with `{ "status": "healthy" }`)
- **Metrics**: GET /metrics (Prometheus format)

## Data Flow

```
Twitch Chat
    ↓
TwitchBotService (IRC)
    ↓
Message Parser
    ↓
[Broadcaster check?] → Reject if not broadcaster
    ↓
Split if >500 chars
    ↓
POST to Router API → waddlebot-router
    ↓
[Processing Module] → Command execution, response generation
    ↓
Response routed back to TwitchBotService
    ↓
Send to Twitch Chat

---

Twitch EventSub Events
    ↓
EventSubHandler (Webhook)
    ↓
HMAC-SHA256 verification
    ↓
Deduplication (message ID tracking)
    ↓
Event-specific handler
    ↓
POST to Hub API → leaderboards, notifications
```

## Key Features

1. **Message Splitting**: Responses exceeding 500 characters automatically split into multiple messages with continuation syntax (e.g., "[1/3]", "[2/3]", "[3/3]")

2. **Broadcaster-Only Commands**: Commands prefixed with `!!` restricted to channel broadcaster; attempts by others trigger error response

3. **EventSub Verification**: All webhooks verified via HMAC-SHA256; invalid signatures rejected with 403

4. **Viewer Tracking**: 60s polling interval; detects join (first appearance), leave (absence), heartbeat (continued presence)

5. **Dynamic Channels**: Channels loaded from database every 300s; automatic join/leave via IRC commands

6. **Cache Fallback**: Distributed cache (Redis or in-memory) with automatic fallback to API calls

## Integration Points

- **Router API** (`ROUTER_API_URL`): Message routing, command responses
- **Hub API** (`HUB_API_URL`): Leaderboard updates, viewer presence
- **Twitch API**: Channel info, chatters list (rest API calls)
- **Database** (`DATABASE_URL`): Channel list, community mappings, configuration

## Reliability & Observability

- **Structured logging**: JSON format with correlation IDs
- **Metrics**: Prometheus format at `/metrics` (message count, latency, error rate)
- **Health checks**: Liveness (`/health`), readiness (`/health?type=ready`)
- **Graceful shutdown**: 30s drain on SIGTERM (complete in-flight messages)
- **Error handling**: Retry logic for API calls (3 retries, exponential backoff)

## Documentation Index

- **[ARCHITECTURE.md](ARCHITECTURE.md)** - System design, data flow, service interactions
- **[API.md](API.md)** - Endpoint documentation with curl examples
- **[CONFIGURATION.md](CONFIGURATION.md)** - Environment variables, startup, logging
- **[USAGE.md](USAGE.md)** - Quick start, channel management, command examples
- **[TESTING.md](TESTING.md)** - Unit tests, integration tests, manual testing
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** - Common issues, debugging, log analysis
- **[RELEASE_NOTES.md](RELEASE_NOTES.md)** - Version history, breaking changes

## Quick Start

```bash
# Build Docker image
docker build -t twitch-module:latest .

# Run with environment variables
docker run -p 8002:8002 \
  -e TWITCH_CLIENT_ID=xxx \
  -e TWITCH_CLIENT_SECRET=xxx \
  -e TWITCH_ACCESS_TOKEN=xxx \
  -e EVENTSUB_SECRET=xxx \
  -e DATABASE_URL=postgresql://... \
  -e ROUTER_API_URL=http://router:8001 \
  -e HUB_API_URL=http://hub:8000 \
  twitch-module:latest

# Verify health
curl http://localhost:8002/health
```

## Support & Issues

- **Logs**: `docker logs <container-id>` or check `/var/log/twitch-module.log`
- **Metrics**: `curl http://localhost:8002/metrics` (Prometheus format)
- **Status**: `curl http://localhost:8002/api/v1/status` (detailed service health)
- **Issues**: See [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
