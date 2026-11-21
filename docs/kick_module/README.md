# Kick Module Documentation

This directory contains comprehensive documentation for the Kick streaming platform integration module (trigger/receiver/kick_module_flask). The Kick Module provides webhook event reception and real-time chat integration via Pusher WebSocket.

## Quick Navigation

| Document | Purpose | Audience |
|----------|---------|----------|
| **[OVERVIEW.md](OVERVIEW.md)** | System overview, architecture, and capabilities | Everyone (start here) |
| **[API.md](API.md)** | HTTP endpoints, event payloads, and error handling | Developers, Integrators |
| **[CONFIGURATION.md](CONFIGURATION.md)** | Environment variables and deployment setup | DevOps, Operators |
| **[USAGE.md](USAGE.md)** | Local development, deployment, and testing | Developers |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | Detailed system design, data flows, and components | Architects, Senior Devs |
| **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** | Common issues and debugging guide | Support, Operators |
| **[RELEASE_NOTES.md](RELEASE_NOTES.md)** | Version history, upgrade path, roadmap | Project Managers, Release Team |
| **[TESTING.md](TESTING.md)** | Test strategy, test cases, and CI/CD | QA, Developers |

## Getting Started

### 1. Understand the System (5 min)

Read [OVERVIEW.md](OVERVIEW.md) to understand:
- What the Kick Module does
- Key capabilities (webhooks + WebSocket)
- How it integrates with WaddleBot
- Supported event types

### 2. Set Up Locally (15 min)

Follow [USAGE.md](USAGE.md) Quick Start section:
```bash
cd trigger/receiver/kick_module_flask
pip install -r requirements.txt
# Create .env file with required variables
python -m quart run --port 8007
```

### 3. Configure for Your Environment (10 min)

Review [CONFIGURATION.md](CONFIGURATION.md):
- Database connection setup
- Kick webhook secret configuration
- Pusher credentials
- Router API integration

### 4. Deploy (20 min)

Choose your deployment method from [USAGE.md](USAGE.md):
- Docker: `docker build -t kick-module .`
- Kubernetes: See YAML examples in CONFIGURATION.md
- Docker Compose: See local development example

## Key Concepts

### Dual-Mode Event Handling

The module operates in two modes simultaneously:

1. **Webhook Mode** (HTTP)
   - Kick platform sends signed HTTP POST events
   - HMAC-SHA256 verification
   - Supports: chat, subscription, raid, stream events, moderation

2. **WebSocket Mode** (Pusher)
   - Real-time chat via Pusher chatrooms
   - Automatic reconnection with backoff
   - Lower latency for live chat

### Event Normalization

Both webhook and WebSocket events are converted to a standard WaddleBot event format and forwarded to the Router API for processing.

```
Kick Webhook → Normalize → Router API → Command Processors
              ↓
Pusher Chat ──┘
```

### Architecture Layers

```
HTTP/WebSocket Input Layer     (Quart routes, Pusher client)
    ↓
Validation & Security Layer    (HMAC verification, input validation)
    ↓
Event Processing Layer         (Parsing, enrichment, normalization)
    ↓
Integration Layer              (Router API, Database, Cache)
    ↓
Monitoring & Observability     (Metrics, logging, health checks)
```

## Common Tasks

### Enable Webhook Events

1. Log into Kick creator dashboard
2. Go to Integrations → Webhooks
3. Add endpoint: `https://your-domain.com/webhook/kick`
4. Set secret to match `KICK_WEBHOOK_SECRET` env var
5. Enable event types in dashboard

### Monitor Module Health

```bash
# Check if module is running
curl http://localhost:8007/health

# Get detailed status
curl http://localhost:8007/api/v1/status | jq .

# View metrics
curl http://localhost:8007/metrics | grep kick_
```

### Debug Event Processing

```bash
# Enable debug logging
LOG_LEVEL=DEBUG docker run ... kick-module

# Filter for specific event types
docker logs kick-module | grep -i "chat_message\|subscription\|raid"

# Monitor router forwarding
docker logs kick-module | grep "forwarding_event\|router"
```

### Test Webhook Signature

```bash
# Generate correct signature
PAYLOAD='{"event":"test"}'
SECRET="your-webhook-secret"
SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" -hex | cut -d' ' -f2)

# Send test webhook
curl -X POST http://localhost:8007/webhook/kick \
  -H "X-Signature: sha256=$SIG" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD"
```

## Troubleshooting Quick Links

| Problem | See Section |
|---------|-------------|
| "Signature mismatch" errors | [TROUBLESHOOTING.md](TROUBLESHOOTING.md#webhook-signature-verification-failures) |
| WebSocket connection failing | [TROUBLESHOOTING.md](TROUBLESHOOTING.md#websocket-connection-issues) |
| Router API not receiving events | [TROUBLESHOOTING.md](TROUBLESHOOTING.md#router-api-integration-failures) |
| Database connection errors | [TROUBLESHOOTING.md](TROUBLESHOOTING.md#database-connection-errors) |
| Module won't start | [TROUBLESHOOTING.md](TROUBLESHOOTING.md#module-wont-start) |
| High latency or slow processing | [TROUBLESHOOTING.md](TROUBLESHOOTING.md#high-latency-or-slow-event-processing) |
| Memory leaks | [TROUBLESHOOTING.md](TROUBLESHOOTING.md#memory-leaks-or-growing-memory-usage) |

## API Quick Reference

### Webhook Endpoint

```
POST /webhook/kick
Headers: X-Signature: sha256=<hmac>
Body: JSON event from Kick platform
Response: 202 Accepted
```

### Health Endpoints

```
GET /health                 → {status: healthy|unhealthy}
GET /api/v1/status         → {module, status, components, stats}
GET /metrics               → Prometheus metrics
```

## Environment Variables Quick Reference

```bash
# Core
MODULE_PORT=8007
SECRET_KEY=<min 32 chars>
LOG_LEVEL=INFO

# Database
DATABASE_URL=postgresql://user:pass@host/db
DB_POOL_SIZE=10

# Integration
CORE_API_URL=http://core-api:8000
ROUTER_API_URL=http://router-api:8001

# Kick Platform
KICK_WEBHOOK_SECRET=<min 32 chars>
KICK_PUSHER_KEY=eb1d5f283081a78b932c
KICK_PUSHER_CLUSTER=us2

# Cache
REDIS_URL=redis://host:6379/0
```

See [CONFIGURATION.md](CONFIGURATION.md) for all variables and examples.

## Supported Event Types

| Kick Event | Normalized Type | Example Use |
|-----------|------------------|-------------|
| ChatMessage | `chat` | User message detection |
| Subscription | `subscription` | Subscriber alerts |
| GiftedSubscription | `gift_subscription` | Gift alerts |
| ChannelFollow | `follow` | New follower tracking |
| StreamStart | `stream_start` | Stream initialization |
| StreamEnd | `stream_end` | Stream cleanup |
| Raid | `raid` | Raid notifications |
| Ban/Timeout | `moderation` | Moderation logging |

## Performance Characteristics

- **Throughput**: 500+ webhooks/sec (with batching)
- **Latency**: &lt;500ms p99 (webhook → Router)
- **Memory**: ~100 MB base + ~2 MB per WebSocket connection
- **CPU**: &lt;20% on 4-core for 100 msg/sec

See [ARCHITECTURE.md](ARCHITECTURE.md#performance-characteristics) for detailed benchmarks.

## Development Workflow

1. **Make changes** in trigger/receiver/kick_module_flask
2. **Run tests**: `pytest tests/ -v`
3. **Check linting**: `flake8 src/`
4. **Build Docker image**: `docker build -f trigger/receiver/kick_module_flask/Dockerfile -t kick-module:test .`
5. **Test locally**: `docker-compose up trigger-kick` (repo's `docker-compose.yml` — there is no separate test compose file)
6. **Submit PR** with test results

See [TESTING.md](TESTING.md) for full testing guide.

## Monitoring & Alerts

Recommended Prometheus alerts:

```yaml
- alert: KickWebhookErrors
  expr: rate(kick_webhook_rejected_total[5m]) > 0.1
  for: 5m

- alert: KickRouterLatency
  expr: histogram_quantile(0.99, kick_router_api_latency_ms) > 1000
  for: 10m

- alert: KickWebSocketDown
  expr: kick_websocket_connections_active == 0
  for: 5m

- alert: KickUnprocessedEvents
  expr: kick_webhook_received_total - kick_event_processed_total > 100
  for: 5m
```

## Deployment Checklist

Before deploying to production:

- [ ] Review [CONFIGURATION.md](CONFIGURATION.md) for all required variables
- [ ] Set strong `SECRET_KEY` and `KICK_WEBHOOK_SECRET` (min 32 chars)
- [ ] Configure `ROUTER_API_URL` endpoint
- [ ] Set up PostgreSQL database with migrations
- [ ] Configure Kick webhook in dashboard with correct secret
- [ ] Test webhook signature verification
- [ ] Verify Router API connectivity
- [ ] Run full test suite: `pytest tests/ -v`
- [ ] Run smoke tests: See [TESTING.md](TESTING.md#smoke-test-checklist)
- [ ] Set up monitoring and alerts
- [ ] Configure log aggregation
- [ ] Document deployment configuration

## Getting Help

- **Documentation**: Start with [OVERVIEW.md](OVERVIEW.md)
- **API Reference**: See [API.md](API.md) for endpoints
- **Troubleshooting**: See [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- **Testing**: See [TESTING.md](TESTING.md) for test examples
- **Configuration**: See [CONFIGURATION.md](CONFIGURATION.md) for all options
- **Support Email**: support@penguintech.io
- **Status Page**: https://status.penguintech.io

## Module Information

- **Language**: Python 3.12
- **Framework**: Quart (async)
- **Port**: 8007 (configurable)
- **Path**: trigger/receiver/kick_module_flask
- **Version**: v1.0.0
- **License**: Limited AGPL-3.0 (see LICENSE.md in repo root)
- **Maintainer**: Penguin Tech Inc

## Related Documentation

- WaddleBot Main Documentation: `/home/penguin/code/waddlebot/docs/`
- Router Module: `/home/penguin/code/waddlebot/docs/router_module/` (if exists)
- Core API: `/home/penguin/code/waddlebot/docs/core_api/` (if exists)
- Hub Module: `/home/penguin/code/waddlebot/docs/hub_module/` (if exists)
- Project Standards: `/home/penguin/code/waddlebot/docs/APP_STANDARDS.md`

---

Last updated: 2026-02-24
Documentation version: 1.0.0
