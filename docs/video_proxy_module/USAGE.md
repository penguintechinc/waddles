# Video Proxy Module — Usage Guide

This guide covers getting the video_proxy_module up and running locally, deploying via Docker, and common streaming workflows.

---

## Table of Contents

1. [Local Development Setup](#local-development-setup)
2. [Docker Deployment](#docker-deployment)
3. [Health Checks](#health-checks)
4. [Proxying a Stream](#proxying-a-stream)
5. [Common Workflows](#common-workflows)
6. [Troubleshooting Quick Links](#troubleshooting-quick-links)

---

## Local Development Setup

### Prerequisites

- Python 3.13+
- PostgreSQL 14+ (or SQLite for development)
- pip (Python package manager)
- Git

### Step 1: Clone and Navigate

```bash
git clone https://github.com/penguintechinc/waddlebot.git
cd waddlebot/core/video_proxy_module
```

### Step 2: Create Virtual Environment

```bash
python3.13 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

Dependencies include:
- `quart==0.20.0` — Async Python web framework
- `pydal==20241111.1` — Database abstraction layer
- `pyjwt==2.8.0` — JWT authentication
- `grpcio==1.63.0` — gRPC support
- `boto3` — Object storage client (S3-compatible: SeaweedFS)
- `psycopg2-binary==2.9.9` — PostgreSQL adapter

### Step 4: Configure Environment

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env` with your database credentials:

```ini
# Database Configuration
DATABASE_URL=postgresql://waddlebot:password@localhost:5432/waddlebot
DB_POOL_SIZE=10
DB_POOL_RECYCLE=3600

# HTTP Server Configuration
MODULE_HOST=0.0.0.0
MODULE_PORT=8092
MODULE_VERSION=1.0.0

# gRPC Configuration
GRPC_HOST=0.0.0.0
GRPC_PORT=50065

# MarchProxy gRPC Configuration (upstream RTMP handler)
MARCHPROXY_GRPC_HOST=localhost
MARCHPROXY_GRPC_PORT=50050

# JWT Configuration
JWT_SECRET_KEY=your-secret-key-change-in-production

# S3-Compatible Object Storage Configuration
S3_ENDPOINT_URL=localhost:8333
S3_ACCESS_KEY_ID=waddlebot
S3_SECRET_ACCESS_KEY=waddlebot

# Feature Limits
FREE_MAX_DESTINATIONS=3
FREE_MAX_2K_DESTINATIONS=1

# Logging
LOG_LEVEL=DEBUG
LOG_FORMAT=text
```

### Step 5: Initialize Database

```bash
python3 -c "from app import db, init_database; init_database()"
```

### Step 6: Run the Application

```bash
# Development mode (with debug logging)
python3 app.py

# OR production mode with Hypercorn
hypercorn app:app --bind 0.0.0.0:8092 --workers 4
```

The module starts on:
- REST API: `http://localhost:8092`
- gRPC: `localhost:50065`

---

## Docker Deployment

### Building the Docker Image

```bash
cd /path/to/video_proxy_module
docker build -t waddlebot/video-proxy:latest .
```

### Running with Docker Compose

Add to your `docker-compose.yml`:

```yaml
services:
  video-proxy:
    image: waddlebot/video-proxy:latest
    ports:
      - "8092:8092"    # REST API
      - "50065:50065"  # gRPC
    environment:
      DATABASE_URL: postgresql://waddlebot:password@postgres:5432/waddlebot
      GRPC_PORT: 50065
      MODULE_PORT: 8092
      JWT_SECRET_KEY: your-secret-key
      S3_ENDPOINT_URL: infra-seaweedfs:8333
      S3_ACCESS_KEY_ID: waddlebot
      S3_SECRET_ACCESS_KEY: waddlebot
      LOG_LEVEL: INFO
    depends_on:
      - postgres
      - seaweedfs
    networks:
      - waddlebot-network
```

### Running Standalone Container

```bash
docker run -d \
  --name video-proxy \
  -p 8092:8092 \
  -p 50065:50065 \
  -e DATABASE_URL=postgresql://waddlebot:password@postgres:5432/waddlebot \
  -e JWT_SECRET_KEY=your-secret-key \
  -e S3_ENDPOINT_URL=infra-seaweedfs:8333 \
  waddlebot/video-proxy:latest
```

### Viewing Logs

```bash
# Docker Compose
docker-compose logs -f core-video-proxy

# Standalone Container
docker logs -f video-proxy
```

---

## Health Checks

### REST API Health Check

```bash
curl http://localhost:8092/health
```

**Expected Response** (200 OK):
```json
{
  "status": "healthy",
  "module": "video_proxy_module",
  "version": "1.0.0",
  "timestamp": "2026-02-16T10:30:45.123456",
  "database": "connected"
}
```

### Database Connectivity Test

```bash
python3 -c "
from app import db
try:
    db.executesql('SELECT 1')
    print('Database connected successfully')
except Exception as e:
    print(f'Database error: {e}')
"
```

### gRPC Connectivity Test

```bash
grpcurl -plaintext localhost:50065 list
```

Expected output lists available gRPC services.

---

## Proxying a Stream

### 1. Create a Stream Configuration

First, obtain a JWT token:

```bash
python3 -c "
from datetime import datetime, timedelta
import jwt
import os

secret = os.getenv('JWT_SECRET_KEY', 'jwt-secret-change-in-production')
payload = {
    'sub': 'admin',
    'exp': datetime.utcnow() + timedelta(hours=1),
    'iat': datetime.utcnow()
}
token = jwt.encode(payload, secret, algorithm='HS256')
print(token)
"
```

Create a stream configuration:

```bash
TOKEN="your-jwt-token-from-above"
curl -X POST http://localhost:8092/api/v1/stream/config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "community_id": "community-123"
  }'
```

**Response** (201 Created):
```json
{
  "success": true,
  "config": {
    "id": 1,
    "community_id": "community-123",
    "stream_key": "secure-random-key-here",
    "ingest_url": "rtmp://localhost:8092/live/secure-random-key-here",
    "is_active": true,
    "created_at": "2026-02-16T10:30:45.123456",
    "updated_at": "2026-02-16T10:30:45.123456"
  }
}
```

### 2. Add Streaming Destinations

Add a Twitch destination:

```bash
TOKEN="your-jwt-token"
CONFIG_ID=1

curl -X POST http://localhost:8092/api/v1/stream/destinations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "config_id": '$CONFIG_ID',
    "platform": "twitch",
    "rtmp_url": "rtmp://live.twitch.tv/app",
    "stream_key": "twitch-stream-key",
    "max_resolution": "1080p"
  }'
```

### 3. Configure OBS Encoder

In OBS Settings > Stream:
- **Service**: Custom RTMP
- **Server**: `rtmp://localhost:8092/live`
- **Stream Key**: The one from your config response

Start streaming. The module proxies to all configured destinations.

### 4. Monitor Stream Status

```bash
TOKEN="your-jwt-token"
CONFIG_ID=1

curl http://localhost:8092/api/v1/stream/status/$CONFIG_ID \
  -H "Authorization: Bearer $TOKEN"
```

**Response**:
```json
{
  "success": true,
  "status": {
    "config_id": 1,
    "is_streaming": true,
    "viewer_count": 452,
    "bitrate_kbps": 5000,
    "start_time": "2026-02-16T10:35:00.000000",
    "last_update": "2026-02-16T10:35:45.123456"
  }
}
```

---

## Common Workflows

### Workflow 1: Set Up a Community Stream

**Goal**: Enable community #42 to stream to Twitch and YouTube

**Steps**:

```bash
# 1. Get JWT token
TOKEN=$(python3 -c "import jwt; from datetime import datetime, timedelta; print(jwt.encode({'sub': 'admin', 'exp': datetime.utcnow() + timedelta(hours=1)}, 'jwt-secret-change-in-production', algorithm='HS256'))")

# 2. Create stream config
CONFIG=$(curl -s -X POST http://localhost:8092/api/v1/stream/config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"community_id": "community-42"}')

CONFIG_ID=$(echo $CONFIG | jq -r '.config.id')
echo "Stream Config ID: $CONFIG_ID"

# 3. Add Twitch destination
curl -X POST http://localhost:8092/api/v1/stream/destinations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "config_id": '$CONFIG_ID',
    "platform": "twitch",
    "rtmp_url": "rtmp://live.twitch.tv/app",
    "stream_key": "twitch-key-here",
    "max_resolution": "1080p"
  }'

# 4. Add YouTube destination
curl -X POST http://localhost:8092/api/v1/stream/destinations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "config_id": '$CONFIG_ID',
    "platform": "youtube",
    "rtmp_url": "rtmp://a.rtmp.youtube.com/live2",
    "stream_key": "youtube-key-here",
    "max_resolution": "1080p"
  }'

echo "Community 42 streaming to Twitch and YouTube"
```

### Workflow 2: Emergency Stop (Force Cut)

**Goal**: Immediately disconnect a destination mid-stream

```bash
DESTINATION_ID=3
TOKEN="your-jwt-token"

curl -X PUT http://localhost:8092/api/v1/stream/destinations/$DESTINATION_ID/force-cut \
  -H "Authorization: Bearer $TOKEN"
```

Response toggles `force_cut` flag. Proxy immediately stops sending to that destination.

### Workflow 3: Regenerate Stream Key

**Goal**: Security refresh for community #99

```bash
TOKEN="your-jwt-token"
COMMUNITY_ID="community-99"

curl -X POST http://localhost:8092/api/v1/stream/key/regenerate/$COMMUNITY_ID \
  -H "Authorization: Bearer $TOKEN"
```

Old RTMP ingest URL becomes invalid. Encoder must update.

### Workflow 4: View All Destinations

**Goal**: List all output targets for a stream

```bash
CONFIG_ID=1
TOKEN="your-jwt-token"

curl http://localhost:8092/api/v1/stream/destinations/$CONFIG_ID \
  -H "Authorization: Bearer $TOKEN"
```

### Workflow 5: Remove Problematic Destination

**Goal**: Permanently delete a destination

```bash
DEST_ID=2
TOKEN="your-jwt-token"

curl -X DELETE http://localhost:8092/api/v1/stream/destinations/$DEST_ID \
  -H "Authorization: Bearer $TOKEN"
```

---

## Troubleshooting Quick Links

| Issue | Doc |
|-------|-----|
| "Connection refused on port 8092" | [TROUBLESHOOTING.md - Connection Issues](./TROUBLESHOOTING.md#connection-refused) |
| "Database connection failed" | [TROUBLESHOOTING.md - Database Issues](./TROUBLESHOOTING.md#database-connection-failed) |
| "Stream timeout / drops" | [TROUBLESHOOTING.md - Stream Timeout](./TROUBLESHOOTING.md#stream-timeout) |
| "JWT authentication error" | [TROUBLESHOOTING.md - JWT Issues](./TROUBLESHOOTING.md#jwt-invalid-token) |
| "Destination not receiving stream" | [TROUBLESHOOTING.md - Upstream Unavailable](./TROUBLESHOOTING.md#upstream-unavailable) |
| Port already in use | [TROUBLESHOOTING.md - Port Conflicts](./TROUBLESHOOTING.md#port-already-in-use) |

---

**Last Updated**: 2026-02-16
**Repository**: github.com/penguintechinc/waddlebot
