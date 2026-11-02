# Credential Manager - API Reference

Complete API specification for the Credential Manager microservice REST endpoints.

## Base URL

```
http://localhost:8095
```

## Endpoints

### 1. Health Check

**Endpoint**: `GET /health`

Returns service health status and metrics.

**Response** (200 OK):
```json
{
  "status": "healthy",
  "module": "credential_manager",
  "version": "1.0.0",
  "running": true,
  "last_cycle": "2025-02-05T10:30:15.123456Z",
  "total_refreshed": 42,
  "total_errors": 2
}
```

**Response** (503 Service Unavailable):
```json
{
  "status": "degraded",
  "module": "credential_manager",
  "version": "1.0.0",
  "running": false,
  "last_cycle": null,
  "total_refreshed": 0,
  "total_errors": 0
}
```

**Fields**:
- `status`: "healthy" or "degraded"
- `module`: Service name (always "credential_manager")
- `version`: Service version
- `running`: Whether refresh loop is active
- `last_cycle`: ISO8601 timestamp of last refresh cycle (null if never run)
- `total_refreshed`: Cumulative tokens refreshed
- `total_errors`: Cumulative refresh errors

**Use Case**: Liveness and readiness probes, monitoring dashboards

---

### 2. Credential Status

**Endpoint**: `GET /api/v1/credentials/status`

Returns breakdown of tracked credentials by platform and integration type.

**Response** (200 OK):
```json
{
  "success": true,
  "stats": [
    {
      "platform": "twitch",
      "integration_type": "bot",
      "total": 10,
      "expiring_soon": 2,
      "expired": 0
    },
    {
      "platform": "discord",
      "integration_type": "bot",
      "total": 5,
      "expiring_soon": 1,
      "expired": 0
    },
    {
      "platform": "slack",
      "integration_type": "user",
      "total": 3,
      "expiring_soon": 0,
      "expired": 0
    }
  ]
}
```

**Response** (503 Service Unavailable):
```json
{
  "error": "Service not initialized"
}
```

**Fields**:
- `success`: Boolean indicating success
- `stats`: Array of credential statistics
  - `platform`: Integration platform (twitch, discord, slack, youtube, spotify, kick)
  - `integration_type`: Type of integration (bot, user, etc.)
  - `total`: Total credentials for this platform/type
  - `expiring_soon`: Count expiring within TOKEN_REFRESH_BUFFER window
  - `expired`: Count already expired

**Notes**:
- "Expiring soon" uses TOKEN_REFRESH_BUFFER (default 5 minutes)
- Only counts active credentials (is_active=true)
- Requires valid database connection

**Use Case**: Monitoring dashboard, admin status page, alerting

---

### 3. Force Refresh

**Endpoint**: `POST /api/v1/credentials/refresh-now`

Triggers immediate token refresh cycle.

**Request**:
```
No request body required
```

**Response** (200 OK):
```json
{
  "success": true,
  "message": "Refreshed 5 credentials"
}
```

**Response** (503 Service Unavailable):
```json
{
  "error": "Service not initialized"
}
```

**Notes**:
- Does not block; returns immediately
- Refresh cycle runs asynchronously
- Call `/api/v1/credentials/status` after a moment to see results
- Useful for manual intervention (e.g., after credential rotation)

**Use Case**: Manual token refresh trigger, admin actions, testing

---

## Error Responses

### 404 Not Found

**Response**:
```json
{
  "error": "Not found"
}
```

**When**: Endpoint doesn't exist

---

### 500 Internal Server Error

**Response**:
```json
{
  "error": "Internal server error"
}
```

**When**: Unexpected error in service

---

## Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Bad request (invalid parameters) |
| 404 | Endpoint not found |
| 500 | Internal server error |
| 503 | Service unavailable/degraded |

---

## Authentication & Authorization

**Current**: No authentication required

**Recommendation**: Implement API key or JWT authentication for production:

```bash
# Example with API key
curl -H "X-API-Key: your-key" http://localhost:8095/api/v1/credentials/status
```

---

## Rate Limiting

**Current**: No rate limiting

**Recommendation**: For production, implement rate limiting:
- 60 requests per minute per client
- 10 refresh-now requests per minute per client

---

## Webhooks & Notifications

### Redis Pub/Sub Events

When a credential is successfully refreshed, an event is published to Redis:

**Channel Format**:
```
credentials:{platform}:{integration_type}:{scope_id}:refreshed
```

**Example Channels**:
```
credentials:twitch:bot:12345:refreshed
credentials:discord:user:67890:refreshed
credentials:slack:bot:54321:refreshed
```

**Message**: ISO8601 timestamp of refresh
```
2025-02-05T10:30:15.123456Z
```

**Subscribe Example** (Node.js):
```javascript
const redis = require('redis');
const client = redis.createClient();

client.subscribe('credentials:twitch:bot:*:refreshed', (message) => {
  console.log('Twitch token refreshed:', message);
});
```

**Subscribe Example** (Python):
```python
import redis
r = redis.Redis()
pubsub = r.pubsub()
pubsub.psubscribe('credentials:twitch:bot:*:refreshed')

for message in pubsub.listen():
    print(f"Token refreshed: {message['data']}")
```

---

## Response Headers

All responses include:

```
Content-Type: application/json
```

---

## Request Headers

No special headers required. Optional:

```
Accept: application/json
User-Agent: your-client/1.0
```

---

## Example Usage

### Check Service Health

```bash
curl http://localhost:8095/health
```

### Get Credential Status

```bash
curl http://localhost:8095/api/v1/credentials/status | jq .stats
```

### Force Refresh

```bash
curl -X POST http://localhost:8095/api/v1/credentials/refresh-now
```

### Monitor in Bash Loop

```bash
while true; do
  echo "=== $(date) ==="
  curl -s http://localhost:8095/health | jq '.status, .total_refreshed, .total_errors'
  sleep 10
done
```

### Monitor with jq

```bash
curl http://localhost:8095/api/v1/credentials/status | \
  jq '.stats | group_by(.platform) | map({platform: .[0].platform, total: map(.total) | add})'
```

---

## Implementation Details

### Response Format

All responses are JSON:
- UTF-8 encoding
- Timestamps in ISO8601 format with timezone
- Null values included (not omitted)

### Concurrency

- All endpoints are safe for concurrent requests
- Service has internal locking for database operations
- Safe for horizontal scaling (stateless)

### Caching

- No caching headers sent
- Results always fresh from database
- Safe for frequent polling

---

## Monitoring & Alerting

### Alert Conditions

```
# Service unhealthy
status != 'healthy'

# Too many errors
total_errors > 100 in last hour

# No refresh in 5 minutes
(now - last_cycle) > 300 seconds

# High failure rate
(total_errors / total_refreshed) > 0.1
```

### Prometheus Metrics (Future)

```
# Metrics endpoint (to be implemented)
GET /metrics

credential_manager_refreshed_total{platform="twitch"} 42
credential_manager_errors_total{platform="discord"} 2
credential_manager_last_cycle_seconds 15
credential_manager_expiring_soon{platform="slack"} 3
```

---

## Versioning

- **API Version**: v1
- **Service Version**: 1.0.0
- **Protocol**: HTTP REST
- **Format**: JSON

Future versions (v2, v3) will maintain backward compatibility with v1.

---

## Test a Platform Bot Credential

**Endpoint**: `POST /api/v1/credentials/<platform>/test` (scope `credentials:admin`)

Validates the active operator BOT token for `platform` against the provider's own API. hub-api's DB
role cannot read operator bot tokens (H-3, gh-969), so its superadmin "test connection" endpoint
delegates here. The token is decrypted in memory for one provider call; the response carries the
verdict only.

| Status | Meaning |
|---|---|
| 200 | `{"success": true, "data": {"platform": "twitch", "valid": true, "error": null}}` |
| 400 | unsupported platform |
| 403 | missing token / scope |
| 404 | no active bot credential for the platform |
| 503 | service not initialized |

---

## Security Considerations

1. **No Credentials Exposed**: API never returns access tokens or secrets
2. **Encrypted Storage**: Tokens encrypted in database
3. **Scoped Database User**: Limited to SELECT/UPDATE on platform_integrations
4. **Internal Only**: Service designed for internal network only
5. **No PII**: No sensitive user information in responses

---

## Related Documentation

- [README](README.md) - Service overview and features
- [DEPLOYMENT](DEPLOYMENT.md) - Deployment and configuration guide
- [Waddlebot API Reference](../../docs/reference/api-reference.md)
- [Database Schema](../../docs/architecture/database-schema.md)
