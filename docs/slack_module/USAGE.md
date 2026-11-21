# Slack Module Usage Guide

## Running the Module

### Prerequisites

- Python 3.12+
- PostgreSQL, MySQL, or SQLite database
- Redis (recommended for production)
- Slack workspace with bot app created
- Valid bot token and signing secret from Slack app configuration

### Installation

```bash
cd trigger/receiver/slack_module
pip install -r requirements.txt
```

**Key Dependencies:**
```
quart>=0.19.0
hypercorn>=0.14.0
slack-bolt>=1.18.0
slack-sdk>=3.21.0
aiohttp>=3.8.0
httpx>=0.24.0
pydal>=20230721.1
python-dotenv>=1.0.0
```

### Environment Setup

Create `.env` file in trigger/receiver/slack_module:

```bash
# Server Configuration
MODULE_PORT=8004
LOG_LEVEL=INFO

# Database
DATABASE_URL=postgresql://user:password@localhost:5432/waddlebot
# or: mysql://user:password@localhost:3306/waddlebot
# or: sqlite:///./waddlebot.db

# API Services
CORE_API_URL=http://localhost:5000
ROUTER_API_URL=http://localhost:5001

# Slack Configuration
SLACK_BOT_TOKEN=xoxb-YOUR-TOKEN-HERE
SLACK_SIGNING_SECRET=YOUR-SIGNING-SECRET
SLACK_APP_TOKEN=xapp-YOUR-TOKEN-HERE  # Only for Socket Mode

# Optional
USE_SOCKET_MODE=false
SECRET_KEY=your-secret-key-for-csrf
REDIS_URL=redis://localhost:6379/0
```

### Starting the Module

#### HTTP Mode (Production)

```bash
# Using hypercorn directly
hypercorn src/index.py:app --bind 0.0.0.0:8004 --workers 4

# Or using make command
make run-slack-module

# Or using docker-compose
docker-compose -f docker-compose.yml up slack-module
```

**Expected output:**
```
[2024-02-24 10:30:45] INFO Starting Hypercorn
[2024-02-24 10:30:45] INFO Slack Bot Service initialized
[2024-02-24 10:30:45] INFO Socket Mode enabled: False
[2024-02-24 10:30:45] INFO Listening on 0.0.0.0:8004
```

#### Socket Mode (Development)

```bash
export USE_SOCKET_MODE=true
hypercorn src/index.py:app --bind 0.0.0.0:8004
```

**Expected output:**
```
[2024-02-24 10:30:45] INFO Starting Hypercorn
[2024-02-24 10:30:45] INFO Slack Bot Service initialized
[2024-02-24 10:30:45] INFO Socket Mode enabled: True
[2024-02-24 10:30:45] INFO Connecting to Slack via Socket Mode...
[2024-02-24 10:30:46] INFO Socket Mode connected successfully
```

### Health Check

```bash
curl http://localhost:8004/health
```

Response:
```json
{
  "status": "healthy",
  "version": "v1.0.0",
  "uptime_seconds": 120,
  "database": "connected",
  "redis": "connected"
}
```

---

## Common Operations

### Testing Slash Commands Locally

**Using ngrok (HTTP Mode):**

```bash
# Terminal 1: Start ngrok tunnel
ngrok http 8004

# Terminal 2: Set environment variable with ngrok URL
export SLACK_WEBHOOK_URL=https://xxxx.ngrok.io

# Terminal 3: Start module
make run-slack-module
```

Then in Slack app settings, set Request URL to:
```
https://xxxx.ngrok.io/slack/events
https://xxxx.ngrok.io/slack/commands
https://xxxx.ngrok.io/slack/actions
https://xxxx.ngrok.io/slack/shortcuts
```

**Using Socket Mode (No ngrok needed):**

```bash
export USE_SOCKET_MODE=true
export SLACK_APP_TOKEN=xapp-...
make run-slack-module
```

### Checking Module Logs

```bash
# If running with docker-compose
docker-compose logs -f trigger-slack

# If running directly
tail -f logs/slack-module.log

# Filter for errors
docker-compose logs trigger-slack | grep ERROR
```

### Debugging Commands

Enable debug logging:

```bash
export LOG_LEVEL=DEBUG
make run-slack-module
```

**Debug output example:**
```
[2024-02-24 10:30:45] DEBUG Received slash command: /waddlebot
[2024-02-24 10:30:45] DEBUG Command payload: {'user_id': 'U123', 'team_id': 'T456', 'text': 'balance'}
[2024-02-24 10:30:45] DEBUG Validating admin status for user U123
[2024-02-24 10:30:45] DEBUG Forwarding to router: POST /execute-command
[2024-02-24 10:30:46] DEBUG Router response: {'status': 'success', 'blocks': [...]}
[2024-02-24 10:30:46] DEBUG Posting response to Slack channel C789
```

### Viewing Active Credentials

```bash
# Connect to database
psql waddlebot

# Query stored Slack credentials
SELECT id, team_id, bot_name, created_at FROM slack_workspaces;

# Check token refresh history
SELECT id, workspace_id, refreshed_at FROM token_refreshes ORDER BY refreshed_at DESC LIMIT 10;
```

### Manually Refreshing Workspace Tokens

```python
# In Python shell connected to the database
from services.slack_bolt_service import SlackBoltService

service = SlackBoltService()
success = service.refresh_workspace_token(team_id="T123456")
print(f"Token refresh: {'Success' if success else 'Failed'}")
```

### Verifying Modal Submissions

Test form submissions with proper validation:

```bash
# 1. Trigger modal open command
/waddlebot form new

# 2. Fill form in Slack
# User fills out fields in modal

# 3. Submit - should see validation errors if required fields empty
# Or success response if all valid

# 4. Check logs for submission details
docker-compose logs trigger-slack | grep "view_submission"
```

---

## Operational Procedures

### Daily Health Checks

```bash
#!/bin/bash
# health-check.sh

echo "Checking module health..."
curl -s http://localhost:8004/health | jq .

echo "Checking database connection..."
psql $DATABASE_URL -c "SELECT 1;" > /dev/null && echo "Database: OK" || echo "Database: FAIL"

echo "Checking redis connection..."
redis-cli -u $REDIS_URL ping

echo "Checking recent command executions..."
curl -s http://localhost:8004/metrics | grep slack_commands_total
```

### Monitoring Command Execution

```bash
# Watch real-time commands
watch -n 5 'curl -s http://localhost:8004/metrics | grep slack_'

# Count commands by type
curl -s http://localhost:8004/metrics | grep slack_commands_total | awk '{print $2}'
```

### Database Maintenance

```bash
# Backup workspace tokens before changes
pg_dump -t slack_workspaces $DATABASE_URL > backup_workspaces.sql

# Remove old token refresh history (> 30 days)
DELETE FROM token_refreshes WHERE refreshed_at < NOW() - INTERVAL '30 days';

# Analyze table for query optimization
ANALYZE slack_workspaces;
```

### Log Rotation

Configure logrotate for production:

```
/var/log/waddlebot/slack-module.log {
    daily
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 waddlebot waddlebot
    sharedscripts
    postrotate
        systemctl reload slack-module >/dev/null 2>&1 || true
    endscript
}
```

---

## Troubleshooting

### Module Won't Start

**Error: "SLACK_BOT_TOKEN not configured"**
```bash
# Verify token is set
echo $SLACK_BOT_TOKEN

# If empty, set it
export SLACK_BOT_TOKEN=xoxb-...
```

**Error: "Database connection failed"**
```bash
# Test database connection
psql $DATABASE_URL -c "SELECT 1;"

# Check DATABASE_URL format
echo $DATABASE_URL
```

### Commands Not Triggering

**Symptom: Slash command shows "loading" but never responds**

```bash
# Check if module is receiving the request
docker-compose logs trigger-slack | grep -i "slash command"

# Test router connectivity
curl -X POST http://localhost:5001/execute-command \
  -H "Content-Type: application/json" \
  -d '{"command": "test"}'

# Check router logs
docker-compose logs core-router
```

### Ephemeral Responses Not Appearing

**Symptom: Response sent but user doesn't see it**

```bash
# Verify ephemeral flag in response
docker-compose logs trigger-slack | grep -i ephemeral

# Check if using correct response_url
docker-compose logs trigger-slack | grep -i response_url

# Test ephemeral response format
curl -X POST $RESPONSE_URL \
  -H "Content-Type: application/json" \
  -d '{"response_type": "ephemeral", "text": "test"}'
```

### Modal Validation Not Working

**Symptom: Invalid inputs accepted in modal**

```bash
# Check validation logic in logs
docker-compose logs trigger-slack | grep -i validation

# Verify modal schema definition
grep -r "callback_id" src/services/ | head -5

# Test validation endpoint directly
curl -X POST http://localhost:8004/slack/actions \
  -H "Content-Type: application/json" \
  -d '{"type": "view_submission", "view": {...}}'
```

### Socket Mode Not Connecting

**Symptom: "Socket Mode enabled: True" but no "connected" message**

```bash
# Check app token is valid
echo $SLACK_APP_TOKEN | head -c 10  # Should start with xapp-

# Enable debug logging
export LOG_LEVEL=DEBUG

# Check for connection errors
docker-compose logs trigger-slack | grep -i "socket\|websocket"

# Test Slack API directly
curl -X POST https://slack.com/api/apps.connections.open \
  -H "Authorization: Bearer $SLACK_APP_TOKEN"
```

---

## Performance Optimization

### Connection Pooling

For HTTP mode with many concurrent users:

```python
# In src/config.py
HYPERCORN_CONFIG = {
    'bind': '0.0.0.0:8004',
    'workers': 4,  # Increase based on CPU cores
    'keep_alive': 5,
    'timeout_keep_alive': 5,
}

# For database pooling
DATABASE_CONFIG = {
    'max_connections': 20,
    'min_connections': 5,
}
```

### Caching Strategy

Enable Redis caching for frequent lookups:

```bash
export REDIS_URL=redis://localhost:6379/0
export REDIS_CACHE_TTL=300  # 5 minutes
```

### Async Command Processing

For slow commands, use async responses:

```python
# In router handler, if command takes >1 second:
if processing_time > 1:
    return {
        "response_type": "in_channel",
        "text": "Processing... (async response coming)",
        "response_url": context.response_url
    }
    # Then send actual response via response_url after 5+ seconds
```

---

## Deployment Checklist

- [ ] Environment variables configured
- [ ] Database migrations applied
- [ ] Slack app tokens verified and stored securely
- [ ] Request URLs set in Slack app configuration
- [ ] Health check endpoint responding
- [ ] Logs configured and rotating
- [ ] Database backups scheduled
- [ ] Rate limiting configured
- [ ] Monitoring/alerting in place
- [ ] Incident response plan documented
