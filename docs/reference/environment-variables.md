# Waddles Environment Variables Reference

This document provides a comprehensive reference for environment variables used across Waddles's legacy per-module services (still running today alongside the v3.0.x 8-container pipeline — see [`ARCHITECTURE.md`](../ARCHITECTURE.md#build-status-caveats)). Each module's configuration is organized by category with descriptions and default values where applicable. For the v3 control-plane API (hub-api), see [`hub_api/README.md`](../../hub_api/README.md).

## Table of Contents

- [svc-ingest (Rust data plane)](#svc-ingest-rust-data-plane)
- [Twitch Module](#twitch-module)
- [Discord Module](#discord-module)
- [Slack Module](#slack-module)
- [AI Interaction Module](#ai-interaction-module)
- [Router Module](#router-module)
- [Hub Module](#hub-module)
- [Labels Core Module](#labels-core-module)
- [Inventory Interaction Module](#inventory-interaction-module)
- [Calendar Interaction Module](#calendar-interaction-module)
- [Memories Interaction Module](#memories-interaction-module)
- [YouTube Music Interaction Module](#youtube-music-interaction-module)
- [Spotify Interaction Module](#spotify-interaction-module)
- [Browser Source Core Module](#browser-source-core-module)
- [Identity Core Module](#identity-core-module)
- [Common Environment Variables](#common-environment-variables)

---

## svc-ingest (Rust data plane)

`core/svc_ingest` — the outbound drain is the **only** platform egress, so it alone holds the platform bot token and detokenizes `{user:<token>}` placeholders immediately before each send. svc-action holds no platform token.

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | — (required for Discord) | Discord bot token for the gateway receiver and outbound REST sends. Injected from the chart Secret (`secretKeyRef`), never a CLI arg or values literal. |
| `PII_DETOKENIZATION_ENABLED` | unset (= enabled) | Plain env off-switch for egress detokenization, independent of the `waddles.core.disable-pii-detokenization` PostHog kill-switch. `false` sends `{user:<token>}` placeholders unchanged (dev/air-gapped escape hatch; never in a production tenant). The chart renders `false` only when `pipeline.piiTokenization.enabled=false`. Detokenization reads display names directly from Postgres via `DATABASE_URL` (the pod's own read-only `waddles_svc_ingest` role, rendered by `waddlebot.dbEnv`); no hub-api call. |

---

## svc-process / svc-action (live bundle config, issue #1306)

`core/svc_process` and `core/svc_action` read per-community bundle config (`get-context().config_json`) directly from Postgres through `core/hub_client/src/bundle_config_db.rs` -- hub-api (the control-plane writer) is not on the hot path and the stages keep serving config while it is down. Details: `docs/BUNDLE_COMMUNITY_CONFIG_LIVE.md`.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_REPLICA_URL` | unset | DSN of the **read-only replica**; preferred. Same column-scoped per-service role as the primary (`waddles_svc_process` / `waddles_svc_action`, alembic `0106_bundle_config_db_reads`). The chart renders it only when `infrastructure.postgresql.readReplicas.host` is set (password via `$(DB_PASSWORD)` expansion, never a literal). |
| `DATABASE_URL` | rendered by `waddlebot.dbEnv` | Primary DSN, used **only** when `DATABASE_REPLICA_URL` is unset -- a WARN is logged ("reading bundle config from the PRIMARY database"). With neither set the DB path is unavailable (ERROR). |
| `waddles.disable-db-bundle-config-reads` (PostHog flag, not an env var) | OFF | Opt-out kill switch: unseen/OFF/license-server-unreachable = direct database reads; ON = legacy hub-api gRPC path (`HUB_API_GRPC_ENDPOINT`). Never env/CLI-bypassable. |

---

## Twitch Module

Configuration for the Twitch EventSub webhooks, OAuth, and API integration collector.

```bash
# Twitch API
TWITCH_APP_ID=your_app_id
TWITCH_APP_SECRET=your_app_secret
TWITCH_WEBHOOK_SECRET=webhook_secret
TWITCH_WEBHOOK_CALLBACK_URL=https://domain.com/twitch/webhook
TWITCH_REDIRECT_URI=https://domain.com/twitch/auth/callback

# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Core API
CORE_API_URL=http://core-api:8001
CONTEXT_API_URL=http://core-api:8001/api/context
REPUTATION_API_URL=http://core-api:8001/api/reputation
GATEWAY_ACTIVATE_URL=http://core-api:8001/api/gateway/activate

# Coordination System
MAX_CLAIMS=5
HEARTBEAT_INTERVAL=300
CONTAINER_ID=twitch_container_1

# Module Info
MODULE_NAME=twitch
MODULE_VERSION=1.0.0
```

---

## Discord Module

Configuration for the Discord py-cord bot integration with events and slash commands.

```bash
# Discord Bot
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_APPLICATION_ID=your_app_id
DISCORD_PUBLIC_KEY=your_public_key
DISCORD_COMMAND_PREFIX=!

# Core API
CORE_API_URL=http://core-api:8001
CONTEXT_API_URL=http://core-api:8001/api/context
REPUTATION_API_URL=http://core-api:8001/api/reputation
GATEWAY_ACTIVATE_URL=http://core-api:8001/api/gateway/activate

# Coordination System
MAX_CLAIMS=5
HEARTBEAT_INTERVAL=300
CONTAINER_ID=discord_container_1

# Module Info
MODULE_NAME=discord
MODULE_VERSION=1.0.0
```

---

## Slack Module

Configuration for the Slack SDK bot integration with events and slash commands.

```bash
# Slack App
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
SLACK_CLIENT_ID=your_client_id
SLACK_CLIENT_SECRET=your_client_secret
SLACK_SIGNING_SECRET=your_signing_secret
SLACK_OAUTH_REDIRECT_URI=https://domain.com/slack/oauth/callback
SLACK_SOCKET_MODE=false

# Core API
CORE_API_URL=http://core-api:8001
CONTEXT_API_URL=http://core-api:8001/api/context
REPUTATION_API_URL=http://core-api:8001/api/reputation
GATEWAY_ACTIVATE_URL=http://core-api:8001/api/gateway/activate

# Coordination System
MAX_CLAIMS=5
HEARTBEAT_INTERVAL=300
CONTAINER_ID=slack_container_1

# Module Info
MODULE_NAME=slack
MODULE_VERSION=1.0.0
```

---

## AI Interaction Module

Configuration for multi-provider AI chat with support for Ollama, OpenAI, and MCP providers.

```bash
# AI Provider Configuration
AI_PROVIDER=ollama  # 'ollama', 'openai', or 'mcp'
AI_HOST=http://ollama:11434
AI_PORT=11434
AI_API_KEY=your_api_key

# Model Configuration
AI_MODEL=llama3.2
AI_TEMPERATURE=0.7
AI_MAX_TOKENS=500

# OpenAI Specific Configuration
OPENAI_API_KEY=sk-your-openai-key
OPENAI_MODEL=gpt-3.5-turbo
OPENAI_BASE_URL=https://api.openai.com/v1

# MCP Configuration
MCP_SERVER_URL=http://mcp-server:8080
MCP_TIMEOUT=30

# System Behavior
SYSTEM_PROMPT="You are a helpful chatbot assistant. Provide friendly, concise, and helpful responses to users in chat."
QUESTION_TRIGGERS=?
RESPONSE_PREFIX="🤖 "
RESPOND_TO_EVENTS=true
EVENT_RESPONSE_TYPES=subscription,follow,donation

# Performance Settings
MAX_CONCURRENT_REQUESTS=10
REQUEST_TIMEOUT=30
ENABLE_CHAT_CONTEXT=true
CONTEXT_HISTORY_LIMIT=5

# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Core API Integration
CORE_API_URL=http://router:8000
ROUTER_API_URL=http://router:8000/router

# Module Info
MODULE_NAME=ai_interaction
MODULE_VERSION=1.0.0
```

---

## Router Module

Configuration for the high-performance command router with multi-threading and caching.

```bash
# Database (Primary + Read Replica)
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot
READ_REPLICA_URL=postgresql://user:pass@read-host:5432/waddlebot

# Redis (Session Management)
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=your_redis_password
REDIS_DB=0
SESSION_TTL=3600
SESSION_PREFIX=waddlebot:session:

# Performance Settings
ROUTER_MAX_WORKERS=20
ROUTER_MAX_CONCURRENT=100
ROUTER_REQUEST_TIMEOUT=30
ROUTER_DEFAULT_RATE_LIMIT=60

# Caching
ROUTER_COMMAND_CACHE_TTL=300
ROUTER_ENTITY_CACHE_TTL=600

# AWS Lambda
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
LAMBDA_FUNCTION_PREFIX=waddlebot-

# OpenWhisk
OPENWHISK_API_HOST=openwhisk.example.com
OPENWHISK_AUTH_KEY=your_auth_key
OPENWHISK_NAMESPACE=waddlebot

# Module Info
MODULE_NAME=router
MODULE_VERSION=1.0.0
```

---

## Hub Module

Configuration for the community management portal with authentication and direct routing.

```bash
# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Hub Configuration
HUB_URL=http://localhost:8000
APP_NAME=Waddles Community Hub

# Email Configuration (Flask Mailer)
SMTP_HOST=smtp.company.com
SMTP_PORT=587
SMTP_USERNAME=hub@company.com
SMTP_PASSWORD=smtp_password
SMTP_TLS=true
FROM_EMAIL=noreply@waddlebot.com

# Browser Source Integration
BROWSER_SOURCE_BASE_URL=http://browser-source-core:8027

# Service URLs for Routing
ROUTER_SERVICE_URL=http://router-service:8000
AI_SERVICE_URL=http://ai-interaction:8005
IDENTITY_SERVICE_URL=http://identity-core:8050
TWITCH_SERVICE_URL=http://twitch-collector:8002
DISCORD_SERVICE_URL=http://discord-collector:8003
SLACK_SERVICE_URL=http://slack-collector:8004
YOUTUBE_SERVICE_URL=http://youtube-music:8025
SPOTIFY_SERVICE_URL=http://spotify-interaction:8026
BROWSER_SOURCE_URL=http://browser-source:8027
REPUTATION_SERVICE_URL=http://reputation:8028
COMMUNITY_SERVICE_URL=http://community:8029

# Rate Limiting
RATE_LIMIT_ENABLED=true
RATE_LIMIT_STORAGE_URL=redis://redis:6379/0

# Module Info
MODULE_NAME=hub_module
MODULE_VERSION=1.0.0
```

---

## Labels Core Module

Configuration for the high-performance label management system with Redis caching.

```bash
# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Redis Configuration
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=your_redis_password

# Core API Integration
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/router

# Performance Settings
MAX_WORKERS=20
CACHE_TTL=300
BULK_OPERATION_SIZE=1000
REQUEST_TIMEOUT=30

# Module Info
MODULE_NAME=labels_core
MODULE_VERSION=1.0.0
```

---

## Inventory Interaction Module

Configuration for the multi-threaded inventory management system.

```bash
# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Core API Integration
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/router

# Performance Settings
MAX_WORKERS=20
MAX_LABELS_PER_ITEM=5
CACHE_TTL=300
REQUEST_TIMEOUT=30

# AAA Logging Configuration
LOG_LEVEL=INFO
LOG_DIR=/var/log/waddlebotlog
ENABLE_SYSLOG=false
SYSLOG_HOST=localhost
SYSLOG_PORT=514
SYSLOG_FACILITY=LOCAL0

# Module Info
MODULE_NAME=inventory_interaction_module
MODULE_VERSION=1.0.0
MODULE_PORT=8024
```

---

## Calendar Interaction Module

Configuration for the event management system with approval workflows.

```bash
# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Core API Integration
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/router

# Labels Core Integration (for event-autoapprove label)
LABELS_API_URL=http://labels-core-service:8025

# Module Info
MODULE_NAME=calendar_interaction_module
MODULE_VERSION=1.0.0
MODULE_PORT=8030
```

---

## Memories Interaction Module

Configuration for the community memory management system.

```bash
# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Core API Integration
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/router

# Labels Core Integration (for memories label permission)
LABELS_API_URL=http://labels-core-service:8025

# Module Info
MODULE_NAME=memories_interaction_module
MODULE_VERSION=1.0.0
MODULE_PORT=8031
```

---

## YouTube Music Interaction Module

Configuration for YouTube Music integration with browser source output.

```bash
# YouTube API Configuration
YOUTUBE_API_KEY=your_youtube_api_key_here
YOUTUBE_API_VERSION=v3
YOUTUBE_MUSIC_CATEGORY_ID=10
YOUTUBE_REGION_CODE=US

# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Core API Integration
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/router

# Browser Source Integration
BROWSER_SOURCE_API_URL=http://browser-source:8027/browser/source

# Performance Settings
MAX_SEARCH_RESULTS=10
CACHE_TTL=300
REQUEST_TIMEOUT=30
MAX_QUEUE_SIZE=50

# Feature Flags
ENABLE_PLAYLISTS=true
ENABLE_QUEUE=true
ENABLE_HISTORY=true
ENABLE_AUTOPLAY=true

# AAA Logging Configuration
LOG_LEVEL=INFO
LOG_DIR=/var/log/waddlebotlog
ENABLE_SYSLOG=false

# Module Info
MODULE_NAME=youtube_music_interaction
MODULE_VERSION=1.0.0
MODULE_PORT=8025
```

---

## Spotify Interaction Module

Configuration for Spotify integration with OAuth and playback control.

```bash
# Spotify API Configuration
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
SPOTIFY_REDIRECT_URI=http://localhost:8026/spotify/auth/callback
SPOTIFY_SCOPES=user-read-playback-state user-modify-playback-state user-read-currently-playing streaming

# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Core API Integration
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/router

# Browser Source Integration
BROWSER_SOURCE_API_URL=http://browser-source:8027/browser/source

# Performance Settings
MAX_SEARCH_RESULTS=10
CACHE_TTL=300
REQUEST_TIMEOUT=30
TOKEN_REFRESH_BUFFER=300

# Feature Flags
ENABLE_PLAYLISTS=true
ENABLE_QUEUE=true
ENABLE_HISTORY=true
ENABLE_DEVICE_CONTROL=true

# Media Display Settings
MEDIA_DISPLAY_DURATION=30
SHOW_ALBUM_ART=true
SHOW_PROGRESS_BAR=true

# AAA Logging Configuration
LOG_LEVEL=INFO
LOG_DIR=/var/log/waddlebotlog
ENABLE_SYSLOG=false

# Module Info
MODULE_NAME=spotify_interaction
MODULE_VERSION=1.0.0
MODULE_PORT=8026
```

---

## Browser Source Core Module

Configuration for the multi-threaded browser source management system.

```bash
# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Core API Integration
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/router

# WebSocket Configuration
WEBSOCKET_HOST=0.0.0.0
WEBSOCKET_PORT=8028
MAX_CONNECTIONS=1000

# Performance Settings
MAX_WORKERS=50
QUEUE_PROCESSING_INTERVAL=1
CLEANUP_INTERVAL=300
TICKER_QUEUE_SIZE=100

# Browser Source Settings
BASE_URL=http://localhost:8027
TOKEN_LENGTH=32
ACCESS_LOG_RETENTION_DAYS=30

# Display Settings
DEFAULT_TICKER_DURATION=10
DEFAULT_MEDIA_DURATION=30
MAX_TICKER_LENGTH=200

# AAA Logging Configuration
LOG_LEVEL=INFO
LOG_DIR=/var/log/waddlebotlog
ENABLE_SYSLOG=false

# Module Info
MODULE_NAME=browser_source_core
MODULE_VERSION=1.0.0
MODULE_PORT=8027
```

---

## Identity Core Module

Configuration for cross-platform identity linking and verification with Flask-Security-Too.

```bash
# Module Configuration
MODULE_NAME=identity_core_module
MODULE_VERSION=1.0.0
MODULE_PORT=8050

# Database
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Redis Configuration
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=your_redis_password

# Session and Security
SECRET_KEY=waddlebot_identity_secret_key_change_me_in_production
SESSION_TTL=3600

# API Keys
VALID_API_KEYS=system_key1,system_key2
MAX_API_KEYS_PER_USER=5
API_KEY_DEFAULT_EXPIRY_DAYS=365

# Core API Integration
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/router

# Platform APIs (for whisper/DM functionality)
TWITCH_API_URL=http://twitch-collector:8002
DISCORD_API_URL=http://discord-collector:8003
SLACK_API_URL=http://slack-collector:8004

# Verification Settings
VERIFICATION_CODE_LENGTH=6
VERIFICATION_TIMEOUT_MINUTES=10
RESEND_COOLDOWN_SECONDS=60
MAX_VERIFICATION_ATTEMPTS=5

# Email Configuration (SMTP)
SMTP_HOST=smtp.company.com
SMTP_PORT=587
SMTP_USERNAME=identity@company.com
SMTP_PASSWORD=smtp_password
SMTP_TLS=true
FROM_EMAIL=noreply@waddlebot.com

# Performance Settings
MAX_WORKERS=20
CACHE_TTL=300
REQUEST_TIMEOUT=30
BULK_OPERATION_SIZE=100

# Rate Limiting
RATE_LIMIT_REQUESTS=60
RATE_LIMIT_WINDOW=60

# Logging Configuration
LOG_LEVEL=INFO
LOG_DIR=/var/log/waddlebotlog
ENABLE_SYSLOG=false
SYSLOG_HOST=localhost
SYSLOG_PORT=514
SYSLOG_FACILITY=LOCAL0

# Feature Flags
ENABLE_EMAIL_VERIFICATION=false
ENABLE_TWO_FACTOR=false
ENABLE_OAUTH_PROVIDERS=false
```

---

## Common Environment Variables

These environment variables are shared across multiple modules and should be configured consistently.

### Database Configuration

```bash
# PostgreSQL database connection
DATABASE_URL=postgresql://user:pass@host:5432/waddlebot

# Read replica for high-performance modules (Router)
READ_REPLICA_URL=postgresql://user:pass@read-host:5432/waddlebot
```

### Redis Configuration

```bash
# Redis connection settings
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=your_redis_password

# Session management (Router)
SESSION_TTL=3600
SESSION_PREFIX=waddlebot:session:
```

### Core API Integration

```bash
# Core API and Router endpoints
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/router

# Legacy core API endpoints (collectors)
CONTEXT_API_URL=http://core-api:8001/api/context
REPUTATION_API_URL=http://core-api:8001/api/reputation
GATEWAY_ACTIVATE_URL=http://core-api:8001/api/gateway/activate
```

### Logging Configuration (AAA)

```bash
# Comprehensive logging settings
LOG_LEVEL=INFO                    # DEBUG, INFO, WARNING, ERROR
LOG_DIR=/var/log/waddlebotlog    # Log directory path
ENABLE_SYSLOG=false              # Enable syslog output
SYSLOG_HOST=localhost            # Syslog server host
SYSLOG_PORT=514                  # Syslog server port
SYSLOG_FACILITY=LOCAL0           # Syslog facility
```

### Performance Settings

```bash
# Worker thread configuration
MAX_WORKERS=20                   # ThreadPoolExecutor worker count
MAX_CONCURRENT_REQUESTS=10       # Concurrent request limit
REQUEST_TIMEOUT=30               # Request timeout in seconds
CACHE_TTL=300                    # Cache time-to-live in seconds
BULK_OPERATION_SIZE=1000         # Bulk operation batch size
```

### Module Identification

```bash
# Module metadata (required for all modules)
MODULE_NAME=module_name          # Unique module identifier
MODULE_VERSION=1.0.0            # Semantic version
MODULE_PORT=8000                # Service port number
```

### Coordination System (Collectors)

```bash
# Horizontal scaling configuration
MAX_CLAIMS=5                     # Maximum entities per container
HEARTBEAT_INTERVAL=300           # Heartbeat frequency in seconds
CONTAINER_ID=container_1         # Unique container identifier
```

### Email Configuration

```bash
# SMTP settings for email delivery
SMTP_HOST=smtp.company.com
SMTP_PORT=587
SMTP_USERNAME=service@company.com
SMTP_PASSWORD=smtp_password
SMTP_TLS=true
FROM_EMAIL=noreply@waddlebot.com
```

### Security Configuration

```bash
# Security and authentication
SECRET_KEY=change_me_in_production           # Flask secret key
VALID_API_KEYS=key1,key2                    # System API keys
API_KEY_DEFAULT_EXPIRY_DAYS=365             # API key expiration
MAX_API_KEYS_PER_USER=5                     # Per-user key limit
```

---

## Configuration Best Practices

1. **Never commit secrets**: Use environment variables, not hardcoded values
2. **Use Docker secrets**: Mount secrets as files in production environments
3. **Validate on startup**: Check required environment variables at module initialization
4. **Provide defaults**: Use sensible defaults for optional configuration
5. **Document dependencies**: Clearly indicate which variables are required vs optional
6. **Use type validation**: Validate variable types (int, bool, URL, etc.) on load
7. **Secure sensitive data**: Encrypt DATABASE_URL, API keys, and passwords in production
8. **Environment separation**: Use different values for dev, staging, and production
9. **Configuration testing**: Test module startup with missing/invalid environment variables
10. **Version compatibility**: Document which module versions require which variables

---

## Related Documentation

- [Architecture Overview](../ARCHITECTURE.md)
- [Deployment Guide](../getting-started/deployment-guide.md)
- [Security Policy](../SECURITY.md)
