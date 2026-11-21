# Credential Manager Module — Overview

**Maintained by**: Penguin Tech Inc
**Source location**: core/credential_manager_module
**Language**: Python 3 (async, Quart web framework)
**REST Port**: 8095 (env var: `MODULE_PORT`, default `8095`)
**Module Name**: `credential_manager`
**Module Version**: `1.0.0`

---

## Purpose

The Credential Manager Module provides centralized, automated lifecycle management of OAuth2 credentials for all platform integrations supported by Waddlebot. Its primary responsibility is to ensure that access tokens used by action modules (Discord, Twitch, Slack, YouTube, Spotify, Kick) remain valid at all times by proactively refreshing them before expiry.

This module does not expose credentials to external consumers directly. Instead, it operates as a background service that:

1. Polls the shared `platform_integrations` database table for tokens approaching expiry.
2. Calls the appropriate platform OAuth2 token endpoint to obtain a fresh token.
3. Writes the new token back to the database.
4. Publishes a Redis pub/sub notification so that dependent modules can reload their in-memory credentials without a restart.

The design ensures that no action module ever has to implement its own refresh logic — the Credential Manager handles the entire token lifecycle on behalf of all platform integrations.

---

## Capabilities

| Capability | Description |
|---|---|
| Proactive token refresh | Refreshes tokens `TOKEN_REFRESH_BUFFER` seconds before expiry (default: 300 s) |
| Background polling | Runs a continuous async poll loop, checking every `POLL_INTERVAL` seconds (default: 60 s) |
| Retry with exponential backoff | Up to `MAX_REFRESH_RETRIES` attempts with `RETRY_BACKOFF_BASE`-second exponential delay |
| Redis pub/sub notifications | Publishes `credentials:<platform>:<type>[:<community_id>]:refreshed` on successful refresh |
| Credential status API | Exposes per-platform token expiry statistics via REST API |
| Force-refresh endpoint | Supports immediate manual trigger of a full refresh cycle |
| Health check | Standard `/health` endpoint reports running state, last cycle time, and counters |
| Multi-platform support | Twitch, Discord, Slack, YouTube/Google, Spotify, Kick |
| Credential listener | Background thread subscribes to Redis events to invalidate in-memory credential state |

---

## Supported Platforms

| Platform | OAuth2 Grant Types | Token Endpoint |
|---|---|---|
| Twitch | `client_credentials`, `authorization_code` | `https://id.twitch.tv/oauth2/token` |
| Discord | `authorization_code` | `https://discord.com/api/v10/oauth2/token` |
| Slack | Token rotation | `https://slack.com/api/oauth.v2.access` |
| YouTube / Google | `authorization_code` | `https://oauth2.googleapis.com/token` |
| Spotify | `authorization_code` | `https://accounts.spotify.com/api/token` |
| Kick | `authorization_code` | `https://id.kick.com/oauth/token` |

---

## Documentation Index

| Document | Contents |
|---|---|
| [OVERVIEW.md](OVERVIEW.md) | This file — purpose, capabilities, quick reference |
| [USAGE.md](USAGE.md) | Getting started, Docker, health check, credential status, force refresh |
| [API.md](API.md) | All REST endpoints with schemas, auth requirements, error codes |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Component design, refresh flow, Redis pub/sub, database schema |
| [CONFIGURATION.md](CONFIGURATION.md) | All environment variables, defaults, security hardening, example `.env` |
| [TESTING.md](TESTING.md) | Test fixtures, mocking, running the test suite |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Decryption failures, missing credentials, rotation errors, debug steps |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Version history |

---

## Quick Reference

### Start the module (Docker)

```bash
docker run --rm \
  -e DATABASE_URL="postgresql://mod_credential_manager:<password>@db:5432/waddlebot" \
  -e REDIS_URL="redis://redis:6379/0" \
  -e PLATFORM_ENCRYPTION_KEY="<base64-encoded-32-byte-key>" \
  -p 8095:8095 \
  waddlebot/credential-manager:latest
```

### Health check

```bash
curl http://localhost:8095/health
```

### Check credential status

```bash
curl http://localhost:8095/api/v1/credentials/status
```

### Force immediate refresh

```bash
curl -X POST http://localhost:8095/api/v1/credentials/refresh-now
```

### Key environment variables

| Variable | Purpose | Default |
|---|---|---|
| `MODULE_PORT` | HTTP listen port | `8095` |
| `DATABASE_URL` | PostgreSQL connection string | _(required)_ |
| `REDIS_URL` | Redis connection string | _(required)_ |
| `PLATFORM_ENCRYPTION_KEY` | Encryption key for at-rest secrets | _(recommended)_ |
| `TOKEN_REFRESH_BUFFER` | Seconds before expiry to trigger refresh | `300` |
| `POLL_INTERVAL` | Seconds between poll cycles | `60` |
| `MAX_REFRESH_RETRIES` | Retry attempts per token | `3` |
| `RETRY_BACKOFF_BASE` | Base seconds for exponential backoff | `5` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |

---

## Architecture Summary

```
┌─────────────────────────────────────────────────────────┐
│              Credential Manager Module                   │
│                                                         │
│  Quart (async HTTP)                                     │
│  ├── /health                (GET)                       │
│  ├── /api/v1/credentials/status   (GET)                 │
│  └── /api/v1/credentials/refresh-now  (POST)            │
│                                                         │
│  RefreshService (background asyncio task)               │
│  ├── Poll loop (every POLL_INTERVAL seconds)            │
│  ├── Query platform_integrations WHERE expires_at < now │
│  ├── Call OAuth handler per platform                    │
│  ├── Update database with new tokens                    │
│  └── Publish Redis pub/sub event                        │
│                                                         │
│  OAuthHandlers (per-platform)                           │
│  ├── TwitchOAuthHandler                                 │
│  ├── DiscordOAuthHandler                                │
│  ├── SlackOAuthHandler                                  │
│  ├── YouTubeOAuthHandler                                │
│  ├── SpotifyOAuthHandler                                │
│  └── KickOAuthHandler                                   │
└──────────────────┬──────────────────────────────────────┘
                   │
        ┌──────────┴───────────┐
        │                      │
  PostgreSQL               Redis
  platform_integrations    credentials:<platform>:
  (tokens stored here)     <type>:<community_id>:refreshed
```

---

## Company

**Penguin Tech Inc** — https://www.penguintech.io
Support: support@penguintech.io
