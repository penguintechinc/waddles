# Translate Interaction Module — Configuration

All configuration is via environment variables. Defaults are shown.

## Core

| Variable | Default | Description |
|----------|---------|-------------|
| `MODULE_PORT` | `8033` | REST server port |
| `GRPC_PORT` | `50033` | gRPC server port |
| `HOST` | `0.0.0.0` | Bind address |
| `GRPC_MAX_WORKERS` | `10` | gRPC thread pool size |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `LOG_DIR` | `/var/log/waddlebotlog` | Log file directory |

## Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `MODULE_SECRET_KEY` | *(must be set)* | JWT signing key — must match router's `SECRET_KEY` |
| `JWT_ALGORITHM` | `HS256` | JWT algorithm |
| `JWT_EXPIRATION_SECONDS` | `3600` | Token TTL |

> **Security:** Set `MODULE_SECRET_KEY` to a 64-character random string. Never commit the real value.

## Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://waddlebot:password@localhost:5432/waddlebot` | PostgreSQL connection URL |

## Redis Cache

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | *(empty)* | Redis connection URL (e.g. `redis://infra-redis:6379/0`) |
| `REDIS_HOST` | `localhost` | Fallback if REDIS_URL not set |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis database index |

## WaddleAI (local AI fallback)

| Variable | Default | Description |
|----------|---------|-------------|
| `WADDLEAI_BASE_URL` | `http://ollama:11434` | Ollama base URL |
| `WADDLEAI_MODEL` | `gemma4:e2b` | Model for translation fallback |
| `WADDLEAI_TEMPERATURE` | `0.1` | Generation temperature |
| `WADDLEAI_MAX_TOKENS` | `500` | Max output tokens |
| `WADDLEAI_TIMEOUT` | `30` | Request timeout (seconds) |

## Emote Providers

| Variable | Default | Description |
|----------|---------|-------------|
| `BTTV_API_URL` | `https://api.betterttv.net/3` | BTTV API base URL |
| `FFZ_API_URL` | `https://api.frankerfacez.com/v1` | FrankerFaceZ API base URL |
| `SEVENTV_API_URL` | `https://7tv.io/v3` | 7TV API base URL |
| `TWITCH_CLIENT_ID` | *(empty)* | Twitch app client ID (for native emotes) |
| `TWITCH_CLIENT_SECRET` | *(empty)* | Twitch app client secret |
| `DISCORD_BOT_TOKEN` | *(empty)* | Discord bot token (for guild emotes) |
| `EMOTE_CACHE_TTL_GLOBAL` | `2592000` (30d) | Global emote cache TTL (seconds) |
| `EMOTE_CACHE_TTL_CHANNEL` | `86400` (1d) | Channel emote cache TTL (seconds) |

## AI Token Decision

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_DECISION_MAX_CALLS_PER_MESSAGE` | `3` | Max WaddleAI calls per message for token classification |
| `AI_DECISION_TIMEOUT` | `2` | Timeout for AI token decision calls (seconds) |
