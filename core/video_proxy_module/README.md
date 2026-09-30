# Video Proxy Module

Multi-platform streaming wrapper for MarchProxy with x265/AV1/x264 encoding support.

## Features

- Stream key generation per community
- Multi-destination output (Twitch, Kick, YouTube, Custom RTMP)
- Low-quality preview in admin panel
- Force-cut toggle (admin only)
- Premium gating: Free = 3 destinations (only 1 can be 2K); Premium = unlimited
- Auto-premium for waddlebot.penguintech.io domain

## Configuration

Environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `REST_PORT` | HTTP REST API port | 8092 |
| `GRPC_PORT` | gRPC service port | 50065 |
| `DB_HOST` | PostgreSQL host | localhost |
| `DB_PORT` | PostgreSQL port | 5432 |
| `DB_NAME` | Database name | waddlebot |
| `DB_USER` | Database user | waddlebot |
| `DB_PASS` | Database password | - |
| `MARCHPROXY_HOST` | MarchProxy RTMP host | marchproxy-rtmp |
| `MARCHPROXY_GRPC_PORT` | MarchProxy gRPC port | 50050 |
| `S3_ENDPOINT_URL` | S3-compatible object store endpoint (SeaweedFS) | http://seaweedfs:8333 |
| `S3_ACCESS_KEY_ID` | S3 access key | - |
| `S3_SECRET_ACCESS_KEY` | S3 secret key | - |
| `S3_BUCKET_NAME` | S3 bucket name | video-proxy |
| `S3_REGION` | S3 region | us-east-1 |
| `S3_FORCE_PATH_STYLE` | Force path-style addressing (required by SeaweedFS) | true |
| `LICENSE_SERVER_URL` | License server URL | https://license.penguintech.io |

## API Endpoints

### Stream Configuration

- `GET /api/v1/streams/:community_id` - Get stream configuration
- `POST /api/v1/streams` - Create stream configuration
- `POST /api/v1/streams/:community_id/key/regenerate` - Regenerate stream key

### Destinations

- `GET /api/v1/streams/:community_id/destinations` - List destinations
- `POST /api/v1/streams/:community_id/destinations` - Add destination
- `DELETE /api/v1/streams/:community_id/destinations/:id` - Remove destination
- `POST /api/v1/streams/:community_id/destinations/:id/force-cut` - Toggle force cut

### Status

- `GET /api/v1/streams/:community_id/status` - Get stream status
- `GET /health` - Health check

## Database Tables

- `video_stream_configs` - Stream configurations per community
- `video_stream_destinations` - Multi-platform output targets
- `video_stream_sessions` - Active/historical stream sessions
- `video_feature_usage` - Premium usage tracking

## Docker

```bash
docker build -t waddlebot/video-proxy .
docker run -p 8092:8092 -p 50065:50065 waddlebot/video-proxy
```

## Premium Gating

Free tier limits:
- Maximum 3 destinations
- Only 1 destination can be 2K resolution
- AV1 encoding available

Premium/waddlebot.penguintech.io:
- Unlimited destinations
- All resolutions supported
- Full feature access
