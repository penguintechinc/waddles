# Slack Module Configuration

## Environment Variables

All configuration is managed via environment variables following 12-factor app principles.

### Core Module Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULE_PORT` | No | `8004` | HTTP server listening port |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `SECRET_KEY` | Yes | None | Secret key for CSRF tokens and security (min 32 chars) |
| `ENVIRONMENT` | No | `development` | Environment: development, staging, production |

### Database Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | None | Database connection string |

**Database URL Formats:**
```
# PostgreSQL (recommended for production)
postgresql://user:password@localhost:5432/waddlebot

# MySQL
mysql://user:password@localhost:3306/waddlebot

# SQLite (development only)
sqlite:///./waddlebot.db
```

**PostgreSQL Connection Parameters:**
```
# Full URL with options
postgresql://user:password@localhost:5432/waddlebot?
  sslmode=require&
  application_name=slack_module&
  connect_timeout=5&
  statement_timeout=30000
```

### API Service URLs

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CORE_API_URL` | Yes | None | Core API service base URL (for user lookups) |
| `ROUTER_API_URL` | Yes | None | Router API service base URL (for command execution) |

**Examples:**
```bash
# Local development
CORE_API_URL=http://localhost:5000
ROUTER_API_URL=http://localhost:5001

# Production
CORE_API_URL=https://api.example.com
ROUTER_API_URL=https://router.example.com

# With path prefixes
ROUTER_API_URL=https://api.example.com/router/v1
```

### Slack Authentication

| Variable | Required (HTTP) | Required (Socket) | Description |
|----------|---|---|-------------|
| `SLACK_BOT_TOKEN` | Yes | Yes | Slack Bot User OAuth token (xoxb-...) |
| `SLACK_SIGNING_SECRET` | Yes | No | Signing secret for webhook validation |
| `SLACK_APP_TOKEN` | No | Yes | Slack App-Level token for Socket Mode (xapp-...) |
| `USE_SOCKET_MODE` | No | No | Enable Socket Mode instead of HTTP webhooks |

**Slack Token Locations:**
- **Bot Token**: Slack App → OAuth & Permissions → Bot User OAuth Token
- **Signing Secret**: Slack App → Basic Information → Signing Secret
- **App Token**: Slack App → Socket Mode → Generate Token (requires Socket Mode enabled)

**Token Validation:**
```bash
# Bot token format
echo $SLACK_BOT_TOKEN | grep -E '^xoxb-' && echo "Valid format" || echo "Invalid"

# Signing secret format (32 chars, hex)
echo -n $SLACK_SIGNING_SECRET | wc -c  # Should be 32

# App token format
echo $SLACK_APP_TOKEN | grep -E '^xapp-' && echo "Valid format" || echo "Invalid"
```

### Optional Services

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REDIS_URL` | No | None | Redis connection for credential caching |
| `REDIS_CACHE_TTL` | No | `300` | Token cache lifetime in seconds |
| `METRICS_ENABLED` | No | `false` | Enable Prometheus metrics endpoint |

**Redis URL Format:**
```
# Standard
redis://localhost:6379/0

# With authentication
redis://:password@localhost:6379/0

# TLS connection
rediss://localhost:6379/0

# Cluster
redis://host1:6379,host2:6379,host3:6379
```

---

## Configuration Files

### .env File

Place in trigger/receiver/slack_module directory:

```bash
# .env
MODULE_PORT=8004
LOG_LEVEL=INFO
ENVIRONMENT=development
SECRET_KEY=your-256-bit-secret-key-minimum-32-chars

# Database
DATABASE_URL=postgresql://waddlebot:password@localhost:5432/waddlebot

# APIs
CORE_API_URL=http://localhost:5000
ROUTER_API_URL=http://localhost:5001

# Slack
SLACK_BOT_TOKEN=xoxb-YOUR-TOKEN
SLACK_SIGNING_SECRET=YOUR-SECRET
USE_SOCKET_MODE=false

# Optional
REDIS_URL=redis://localhost:6379/0
METRICS_ENABLED=true
```

### Docker Environment

For containerized deployments, pass via docker-compose:

```yaml
# docker-compose.yml
services:
  slack-module:
    image: waddlebot/slack-module:latest
    environment:
      MODULE_PORT: 8004
      LOG_LEVEL: INFO
      DATABASE_URL: postgresql://waddlebot:${DB_PASSWORD}@postgres:5432/waddlebot
      CORE_API_URL: http://hub-api:5000
      ROUTER_API_URL: http://router:5001
      SLACK_BOT_TOKEN: ${SLACK_BOT_TOKEN}
      SLACK_SIGNING_SECRET: ${SLACK_SIGNING_SECRET}
    depends_on:
      - postgres
      - redis
    ports:
      - "8004:8004"
```

### Kubernetes ConfigMap & Secrets

```yaml
# configmap.yml
apiVersion: v1
kind: ConfigMap
metadata:
  name: slack-module-config
data:
  MODULE_PORT: "8004"
  LOG_LEVEL: "INFO"
  CORE_API_URL: "http://hub-api:5000"
  ROUTER_API_URL: "http://router:5001"
---
# secret.yml
apiVersion: v1
kind: Secret
metadata:
  name: slack-module-secrets
type: Opaque
stringData:
  DATABASE_URL: postgresql://user:pass@postgres:5432/waddlebot
  SLACK_BOT_TOKEN: xoxb-...
  SLACK_SIGNING_SECRET: ...
  SECRET_KEY: ...
```

---

## Slack App Configuration

### Required Scopes

Bot token must have these OAuth scopes:

```
commands                    # Receive slash commands
chat:write                  # Post messages
chat:write.public           # Post in public channels
chat:write.customize        # Customize message posting
chat:write.customize.bot    # Bot message customization
users:read                  # Read user information
users:read.email            # Read user email
channels:read               # Read channel information
groups:read                 # Read private channel info
im:read                     # Read DM information
reactions:read              # Read message reactions
files:read                  # Read file information
```

**To update scopes:**
1. Go to Slack App → OAuth & Permissions
2. Add required scopes to "Bot Token Scopes"
3. Reinstall app in workspace
4. Copy new token to `SLACK_BOT_TOKEN`

### Event Subscriptions

**For HTTP Mode:**

Configure in Slack App → Event Subscriptions:

1. Enable Event Subscriptions: ON
2. Request URL: `https://{your-domain}/slack/events`
3. Verify URL (Slack will test it)
4. Subscribe to bot events:
   - `message.channels`
   - `message.groups`
   - `message.im`
   - `message.mpim`
   - `app_mention`
5. Subscribe to workspace events:
   - None required (slash commands handled separately)

**For Socket Mode:**

1. Enable Socket Mode: ON
2. No Request URL needed
3. Event subscriptions still required (same list)

### Slash Commands

Create slash commands in Slack App → Slash Commands:

**For HTTP Mode:**

| Command | Request URL | Short Description |
|---------|-------------|-------------------|
| `/waddlebot` | `https://{domain}/slack/commands` | WaddleBot main command |
| `/form` | `https://{domain}/slack/commands` | Manage forms |
| `/poll` | `https://{domain}/slack/commands` | Create polls |
| `/ticket` | `https://{domain}/slack/commands` | Create tickets |
| (... all 24 commands) | Same URL | ... |

**For Socket Mode:**

- Request URL: Not used
- Slash commands still created in app config
- Module receives via WebSocket

### Interactivity & Shortcuts

Configure in Slack App → Interactivity & Shortcuts:

**For HTTP Mode:**

- Interactivity: ON
- Request URL: `https://{domain}/slack/actions`
- Select Menus: Enable
- Shortcut Request URL: `https://{domain}/slack/shortcuts`

**For Socket Mode:**

- Interactivity: ON
- Request URL: Not applicable (leave blank)
- Module handles via WebSocket

### Permissions

Ensure app has these permissions:

```
✓ Receive messages and events
✓ Install app to workspace
✓ Post messages on behalf of users
✓ Read workspace profile information
✓ View and modify channel topics
```

---

## Advanced Configuration

### Rate Limiting

Configure request rate limits per team:

```python
# src/config.py
RATE_LIMITS = {
    'slash_commands': {
        'requests': 300,
        'period': 60,  # seconds
    },
    'events': {
        'requests': 300,
        'period': 60,
    },
    'actions': {
        'requests': 300,
        'period': 60,
    },
}
```

### Async Processing

For commands exceeding 3-second response deadline:

```python
# src/config.py
ASYNC_COMMANDS = {
    'form': {'timeout': 5, 'use_response_url': True},
    'ticket': {'timeout': 10, 'use_response_url': True},
    'poll': {'timeout': 5, 'use_response_url': True},
}
```

### Logging Configuration

```python
# src/config.py
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'detailed': {
            'format': '[%(asctime)s] %(levelname)s - %(name)s: %(message)s'
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'detailed',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': 'logs/slack-module.log',
            'maxBytes': 10485760,  # 10MB
            'backupCount': 10,
            'formatter': 'detailed',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'INFO',
    },
}
```

### Credential Management

Credentials are stored encrypted in database:

```python
# src/config.py
ENCRYPTION = {
    'algorithm': 'AES-256-GCM',
    'key_derivation': 'PBKDF2',
    'iterations': 100000,
}
```

Credentials are refreshed via Redis cache:

```python
# Cache policy
CREDENTIAL_CACHE = {
    'enabled': True,
    'ttl': 300,  # 5 minutes
    'backend': 'redis',  # or 'memory' for single instance
}
```

### Performance Tuning

For high-throughput environments:

```python
# src/config.py
PERFORMANCE = {
    'worker_threads': 4,
    'max_queue_size': 1000,
    'batch_size': 50,  # Process messages in batches
    'enable_compression': True,
}
```

---

## Multi-Workspace Setup

To support multiple Slack workspaces with single module instance:

1. **Database**: Store one row per workspace in `slack_workspaces` table
   ```sql
   CREATE TABLE slack_workspaces (
       id SERIAL PRIMARY KEY,
       team_id VARCHAR(20) UNIQUE,
       team_name VARCHAR(255),
       bot_token VARCHAR(255) ENCRYPTED,
       signing_secret VARCHAR(255) ENCRYPTED,
       created_at TIMESTAMP DEFAULT NOW()
   );
   ```

2. **Routing**: Module determines which workspace from `team_id` in request
   ```python
   workspace = await db.slack_workspaces.find(team_id=request.team_id)
   bot_token = workspace.decrypt(workspace.bot_token)
   ```

3. **Configuration**: Single module serves all workspaces
   - All credentials stored in database
   - No need to restart for adding new workspace
   - Automatic credential refresh per workspace

---

## Configuration Validation

Validate configuration on startup:

```bash
# Test configuration
python -m src.utils.validate_config

# Expected output:
# ✓ MODULE_PORT configured
# ✓ SECRET_KEY valid (min 32 chars)
# ✓ DATABASE_URL valid and connectable
# ✓ CORE_API_URL reachable
# ✓ ROUTER_API_URL reachable
# ✓ SLACK_BOT_TOKEN valid format
# ✓ SLACK_SIGNING_SECRET valid format
# ✓ All validations passed
```

---

## Security Best Practices

1. **Never commit .env**: Add to `.gitignore`
   ```
   .env
   .env.local
   .env.*.local
   *.key
   ```

2. **Rotate tokens regularly**: Every 90 days
   ```bash
   # Update bot token
   export SLACK_BOT_TOKEN=xoxb-new-token
   # Verify health
   curl http://localhost:8004/health
   ```

3. **Use strong SECRET_KEY**: Generate with:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

4. **Enable TLS for REDIS**: In production
   ```
   REDIS_URL=rediss://password@redis.example.com:6380
   ```

5. **Database encryption**: Use encrypted connection
   ```
   DATABASE_URL=postgresql://user:pass@db.example.com/waddlebot?sslmode=require
   ```

6. **Restrict module access**: Only expose via internal network or VPN in production
