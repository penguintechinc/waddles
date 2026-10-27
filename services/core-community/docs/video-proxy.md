# Video Proxy Module

Multi-platform streaming proxy with encoding support for WaddleBot.

## Overview

The video proxy module provides:
- Stream key generation per community
- Multi-destination output (Twitch, Kick, YouTube, Custom RTMP)
- Low-quality preview in admin panel
- Force-cut toggle (admin only)
- Premium gating with feature limits
- Auto-premium for waddlebot.penguintech.io domain
- x265/AV1/x264 encoding support via MarchProxy

## Architecture

```
Video Proxy Module (REST API + gRPC)
├── Stream Configuration Service
├── Destination Management
├── Encoding Service (via MarchProxy)
├── Preview Service
├── Force-Cut Controller
├── License Service Integration
└── Database (PostgreSQL via AsyncDAL)
```

## Features

### Stream Management

- **Per-Community Streams**: Unique stream key per community
- **Multi-Output**: Simultaneous streaming to multiple platforms
- **Stream Status**: Real-time status monitoring
- **Force Cut**: Admin-only stream termination

### Premium Features

**Free Tier:**
- Maximum 3 destinations
- Only 1 destination can use 2K resolution
- AV1 encoding available

**Premium Tier:**
- Unlimited destinations
- All resolutions supported
- Full feature access

**Auto-Premium:**
- `waddlebot.penguintech.io` domain automatically premium

### Encoding

Encoding handled by MarchProxy with support for:
- x264 (baseline compatibility)
- x265 (HEVC)
- AV1 (modern efficiency)

Configurable per destination.

## REST API Endpoints

### Stream Configuration

**GET** `/api/v1/streams/:community_id` - Get stream configuration
- Response: Stream config with key and metadata
- Requires: Community ownership

**POST** `/api/v1/streams` - Create stream configuration
- Request: { community_id, encoder, bitrate, resolution }
- Response: 201 Created
- Requires: Community ownership

**POST** `/api/v1/streams/:community_id/key/regenerate` - Regenerate stream key
- Response: New stream key (secret key)
- Requires: Admin or community owner

### Destinations

**GET** `/api/v1/streams/:community_id/destinations` - List destinations
- Response: Array of destination configs
- Requires: Community access

**POST** `/api/v1/streams/:community_id/destinations` - Add destination
- Request: { platform, channel_id, encoder, bitrate, resolution }
- Response: 201 Created
- Requires: Community owner
- Validation: Premium tier checks

**DELETE** `/api/v1/streams/:community_id/destinations/:id` - Remove destination
- Response: 200 OK
- Requires: Community owner

**POST** `/api/v1/streams/:community_id/destinations/:id/force-cut` - Force stream cut
- Response: 200 OK (cut initiated)
- Requires: Admin only

### Status

**GET** `/api/v1/streams/:community_id/status` - Get stream status
- Response: { is_active, viewers, bitrate, resolution, uptime }
- Requires: Community access

**GET** `/health` - Health check
- Response: 200 OK with service status

## gRPC Interface

Parallel gRPC service for low-latency operations:

```protobuf
service VideoProxyService {
  rpc GetStreamStatus(StreamId) returns (StreamStatus);
  rpc UpdateDestination(DestinationUpdate) returns (UpdateResponse);
  rpc ForceCut(StreamId) returns (Response);
}
```

## Database Tables

### video_stream_configs

```sql
id, community_id (FK), stream_key, stream_secret,
encoder, bitrate, resolution, is_active,
created_at, updated_at
```

### video_stream_destinations

```sql
id, stream_config_id (FK), platform (twitch|kick|youtube|custom),
platform_channel_id, encoder, bitrate, resolution,
created_at, updated_at
```

### video_stream_sessions

```sql
id, stream_config_id (FK),
started_at, ended_at, duration_seconds,
peak_viewers, average_bitrate, resolution
```

### video_feature_usage

```sql
id, community_id (FK), feature_name,
usage_count, last_used_at, month_quota
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| REST_PORT | HTTP REST API port | 8092 |
| GRPC_PORT | gRPC service port | 50065 |
| DB_HOST | PostgreSQL host | localhost |
| DB_PORT | PostgreSQL port | 5432 |
| DB_NAME | Database name | waddlebot |
| DB_USER | Database user | waddlebot |
| DB_PASS | Database password | (required) |
| MARCHPROXY_HOST | MarchProxy RTMP host | marchproxy-rtmp |
| MARCHPROXY_GRPC_PORT | MarchProxy gRPC port | 50050 |
| S3_ENDPOINT_URL | S3-compatible object store endpoint (SeaweedFS) | http://seaweedfs:8333 |
| S3_ACCESS_KEY_ID | S3 access key | (required) |
| S3_SECRET_ACCESS_KEY | S3 secret key | (required) |
| S3_BUCKET_NAME | S3 bucket name | video-proxy |
| S3_REGION | S3 region | us-east-1 |
| S3_FORCE_PATH_STYLE | Force path-style addressing (required by SeaweedFS) | true |
| LICENSE_SERVER_URL | License server URL | https://license.penguintech.io |

### Docker

Build and run the container:

```bash
docker build -t waddlebot/video-proxy .
docker run -p 8092:8092 -p 50065:50065 \
  -e DB_PASS=<password> \
  -e S3_ACCESS_KEY_ID=<key> \
  -e S3_SECRET_ACCESS_KEY=<secret> \
  waddlebot/video-proxy
```

## Premium Gating

### License Validation

Premium features checked via PenguinTech License Server:

```python
try:
    await license_service.validate_stream_creation(
        community_id=community_id,
        destination_count=len(destinations)
    )
except LicenseValidationException as e:
    return error_response(e.message, 402)  # Payment Required
```

### Tier Limits

**Free Tier:**
```python
max_destinations = 3
high_res_destinations = 1  # Only 1 can be 2K+
```

**Premium Tier:**
```python
max_destinations = unlimited
high_res_destinations = unlimited
```

### Domain-Based Auto-Premium

```python
if "waddlebot.penguintech.io" in request_domain:
    tier = "premium"  # Auto-premium for official domain
```

## Integration with MarchProxy

MarchProxy handles RTMP ingest and multi-destination output:

1. Stream comes in: `rtmp://marchproxy/live/{{stream_key}}`
2. MarchProxy encodes and distributes
3. Outputs to configured destinations via gRPC
4. Video proxy monitors via gRPC health checks

### RTMP URL Format

```
rtmp://marchproxy-rtmp:1935/live/{{stream_key}}
```

## Streaming Workflow

1. **Stream Start**
   - Community initiates stream
   - Stream key generated
   - Destinations validated (license check)
   - MarchProxy session created

2. **Multi-Output**
   - RTMP input to MarchProxy
   - Parallel encoding and distribution
   - Real-time monitoring

3. **Admin Controls**
   - Preview available in admin panel
   - Force-cut available for emergency stops
   - Real-time statistics

4. **Stream End**
   - Automatic cleanup
   - Session metrics recorded
   - Archive created if enabled

## Previews

### Low-Quality Admin Preview

```
GET /api/v1/streams/:community_id/preview
→ Low bitrate RTMP stream for admin viewing
```

Used in admin panel for monitoring without full bandwidth.

## Error Handling

### HTTP Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Invalid input |
| 401 | Unauthorized |
| 402 | Payment Required (license) |
| 403 | Forbidden |
| 404 | Not found |
| 429 | Quota exceeded |
| 500 | Server error |

### Error Codes

- `INVALID_PLATFORM` - Unsupported platform
- `INVALID_RESOLUTION` - Resolution not allowed
- `PREMIUM_FEATURE_REQUIRED` - Feature requires premium
- `DESTINATION_LIMIT_EXCEEDED` - Too many destinations
- `STREAM_NOT_FOUND` - Stream doesn't exist
- `MARSHPROXY_ERROR` - MarchProxy communication failed

## Performance

### Bitrate Recommendations

| Resolution | Min | Recommended | Max |
|-----------|-----|-------------|-----|
| 720p | 2500 | 4500 | 8000 |
| 1080p | 3500 | 6000 | 12000 |
| 2K | 4500 | 8000 | 16000 |

### Encoding Quality

Default settings provide balance between quality and bandwidth:
- x264: Baseline compatibility
- x265: ~30% better compression
- AV1: ~50% better compression (modern clients only)

## Monitoring

### Metrics to Track

- Active streams per hour
- Destinations per stream (for upsell)
- Premium tier conversion
- Force-cut frequency
- MarchProxy uptime
- Average bitrate utilization

### Health Checks

```
GET /health
Response: {
  "status": "healthy",
  "database": "connected",
  "marchproxy": "connected",
  "uptime_seconds": 86400
}
```

## Testing

### Manual Testing

```bash
# Health check
curl http://localhost:8092/health

# Create stream
curl -X POST http://localhost:8092/api/v1/streams \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "community_id": 123,
    "encoder": "x264",
    "bitrate": 4500,
    "resolution": "1080p"
  }'

# List destinations
curl http://localhost:8092/api/v1/streams/123/destinations \
  -H "Authorization: Bearer <token>"

# Get status
curl http://localhost:8092/api/v1/streams/123/status \
  -H "Authorization: Bearer <token>"
```

## Security

### Stream Keys

- 32-character random hex strings
- Never logged in plain text
- Can be regenerated at any time
- One per community

### RTMP Authentication

RTMP ingest uses stream key in URL:
```
rtmp://marchproxy/live/{{stream_key}}
```

No additional auth required (stream key is the secret).

### API Authentication

All REST endpoints require JWT or API key:
```
Authorization: Bearer {{jwt_token}}
```

## License Enforcement

Premium features are license-gated:

```python
# Free tier limit
if tier == "free" and len(destinations) > 3:
    return error_response("Upgrade to Premium for more destinations", 402)

# Resolution limit
if tier == "free" and destination.resolution == "2k":
    if high_res_count >= 1:
        return error_response("Free tier limited to 1 high-res destination", 402)
```

## Related Documentation

- **workflow-engine.md** - Workflow automation
- **shared-libs.md** - Shared library components
