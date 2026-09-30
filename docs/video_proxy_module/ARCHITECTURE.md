# Video Proxy Module — Architecture

Technical architecture, system design, data flows, and component interactions for the video_proxy_module.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Architecture Diagram](#architecture-diagram)
3. [Core Components](#core-components)
4. [Data Model](#data-model)
5. [Stream Routing Pipeline](#stream-routing-pipeline)
6. [Transcoding Pipeline](#transcoding-pipeline)
7. [Upstream Integration](#upstream-integration)
8. [Authentication & Authorization](#authentication--authorization)
9. [Database Schema](#database-schema)

---

## System Overview

The video_proxy_module is a stateless, horizontally-scalable microservice that:

1. **Accepts** video streams from encoders (OBS, Streamlabs, etc.) via RTMP ingest
2. **Routes** each stream to multiple destinations (Twitch, YouTube, Kick, custom RTMP servers)
3. **Transcodes** video/audio on-demand based per-destination quality requirements
4. **Monitors** real-time stream health and connection status
5. **Enforces** feature limits (free tier vs. premium) via license gating

**Design Principles**:
- Stateless: No session affinity required; can scale horizontally
- Asynchronous: Quart-based async/await for concurrency
- Database-driven: All configurations stored in PyDAL-backed database
- gRPC-native: First-class gRPC support alongside REST
- Resilient: Retry logic, timeout handling, graceful degradation

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        VIDEO PROXY MODULE                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────┐         ┌──────────────────────────┐  │
│  │   REST API Layer    │         │   gRPC Service Layer     │  │
│  │  (Quart, port 8092) │         │  (protobuf, port 50065)  │  │
│  └──────────┬──────────┘         └──────────┬───────────────┘  │
│             │                               │                  │
│             └───────────────┬───────────────┘                  │
│                             │                                  │
│                    ┌────────▼─────────┐                        │
│                    │  Request Router   │                        │
│                    │  & Auth Layer     │                        │
│                    │  (JWT Validation) │                        │
│                    └────────┬──────────┘                        │
│                             │                                  │
│      ┌──────────────────────┼──────────────────────┐          │
│      │                      │                      │          │
│      ▼                      ▼                      ▼          │
│  ┌──────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │  Stream  │  │   Destination    │  │  Stream Status   │  │
│  │  Config  │  │    Manager       │  │    Tracker       │  │
│  │ Handler  │  │                  │  │                  │  │
│  └──────────┘  └──────────────────┘  └──────────────────┘  │
│      │                   │                      │            │
│      └───────────────────┼──────────────────────┘            │
│                          │                                   │
│                  ┌───────▼─────────┐                         │
│                  │ PyDAL Database  │                         │
│                  │ Interface       │                         │
│                  └───────┬─────────┘                         │
│                          │                                   │
└──────────────────────────┼───────────────────────────────────┘
                           │
                 ┌─────────▼──────────┐
                 │   PostgreSQL DB    │
                 │                    │
                 │ - stream_configs   │
                 │ - destinations     │
                 │ - stream_status    │
                 └────────────────────┘


┌──────────────────────────────────────────────────────────────────┐
│                  UPSTREAM INTEGRATION LAYER                       │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────────┐  ┌─────────────────────┐  ┌────────────┐ │
│  │   OBS/Encoder    │  │   MarchProxy gRPC   │  │  SeaweedFS     │ │
│  │                  │  │   (50050, RTMPing)  │  │  (storage) │ │
│  │  RTMP Ingest:    │  │                     │  │            │ │
│  │  rtmp://localhost│  │  - Stream proxy     │  │  - Thumbs  │ │
│  │  :8092/live/:key │  │  - Transcode cmds   │  │  - Preview │ │
│  └────────┬─────────┘  └────────┬────────────┘  └────────────┘ │
│           │                     │                               │
│           │  RTMP stream        │  gRPC commands               │
│           └─────────────────────▼                               │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────┐
│              EXTERNAL STREAMING PLATFORMS                         │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Twitch     │  │   YouTube    │  │    Kick      │          │
│  │              │  │              │  │              │          │
│  │ RTMP Endpoint│  │ RTMP Endpoint│  │ RTMP Endpoint│          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## Core Components

### 1. REST API Layer (`app.py`)

**Framework**: Quart (async Python web framework)
**Port**: 8092

**Responsibilities**:
- HTTP request routing and handling
- JWT authentication (Bearer token validation)
- Request/response serialization (JSON)
- Async endpoint implementations

**Key Endpoints**:
- `POST /api/v1/stream/config` — Create stream
- `GET /api/v1/stream/config/{id}` — Retrieve stream config
- `POST /api/v1/stream/destinations` — Add destination
- `GET /health` — Health check

**Code Structure**:
```python
# Authentication decorator
@require_auth
async def protected_endpoint():
    # Only executed if JWT is valid
    pass

# Stream config CRUD
@app.route("/api/v1/stream/config", methods=["POST"])
@require_auth
async def create_stream_config():
    # Generate stream key
    # Insert into PyDAL
    # Return config
```

---

### 2. gRPC Service Layer (`proto/video_proxy.proto`)

**Protocol**: gRPC with Protocol Buffers v3
**Port**: 50065

**Service Definition**:
```protobuf
service VideoProxyService {
  rpc GetStreamConfig(GetStreamConfigRequest) returns (StreamConfig);
  rpc CreateStreamKey(CreateStreamKeyRequest) returns (StreamKey);
  rpc AddDestination(AddDestinationRequest) returns (Destination);
  rpc GetStreamStatus(GetStreamStatusRequest) returns (StreamStatus);
  // ... more methods
}
```

**Advantages Over REST**:
- Binary serialization (smaller payloads)
- Multiplexing (multiple requests per connection)
- Server push (bidirectional streaming)
- 2x-10x faster than JSON REST

---

### 3. Configuration Management (`config.py`)

**Pattern**: Dataclass with environment variable fallbacks

**Key Configurations**:
```python
DATABASE_URL         # PyDAL connection string
MODULE_PORT          # REST API port (8092)
GRPC_PORT           # gRPC service port (50065)
JWT_SECRET_KEY      # JWT signing key
S3_ENDPOINT_URL      # Object storage
LICENSE_SERVER_URL  # Feature licensing
FREE_MAX_DESTINATIONS # Free tier limit (3)
```

**Validation**: All config validated at startup; fails fast if invalid.

---

### 4. Database Layer (PyDAL)

**ORM**: PyDAL (Python Database Abstraction Layer)
**Supported Databases**: PostgreSQL, SQLite, MySQL, MariaDB

**Tables**:
1. `stream_configs` — Per-community stream metadata
2. `stream_destinations` — Output platforms and URLs
3. `stream_status` — Real-time streaming metrics

---

### 5. Authentication Module

**Method**: JWT (JSON Web Token)
**Algorithm**: HS256 (HMAC-SHA256)
**Expiration**: Configurable (default 3600 seconds)

**Flow**:
```
1. Client provides JWT in Authorization: Bearer <token>
2. @require_auth decorator intercepts request
3. Token signature and expiration validated
4. Payload attached to request context
5. Handler executes with auth context
```

**Security**:
- Tokens signed with `JWT_SECRET_KEY`
- Expiration enforced
- Invalid tokens return 401 Unauthorized

---

## Data Model

### StreamConfig Table

```sql
CREATE TABLE stream_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id VARCHAR(255) NOT NULL UNIQUE,
    stream_key VARCHAR(255) NOT NULL UNIQUE,
    ingest_url VARCHAR(512) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Relationships**:
- 1:N with `stream_destinations` (one config, many destinations)
- 1:1 with `stream_status` (one status record per config)

---

### StreamDestination Table

```sql
CREATE TABLE stream_destinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id INTEGER NOT NULL FOREIGN KEY,
    platform VARCHAR(50) NOT NULL,
    rtmp_url VARCHAR(512) NOT NULL,
    stream_key VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    force_cut BOOLEAN DEFAULT FALSE,
    max_resolution VARCHAR(20) DEFAULT '1080p',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Indices**:
- `(config_id)` for efficient destination lookups
- `(platform, is_active)` for filtering

---

### StreamStatus Table

```sql
CREATE TABLE stream_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id INTEGER NOT NULL UNIQUE FOREIGN KEY,
    is_streaming BOOLEAN DEFAULT FALSE,
    viewer_count INTEGER DEFAULT 0,
    bitrate_kbps INTEGER DEFAULT 0,
    start_time TIMESTAMP,
    last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Relationship**: 1:1 with `stream_configs`

---

## Stream Routing Pipeline

```
┌──────────────────────────────────────────┐
│  1. INGEST: Encoder sends RTMP stream    │
│     rtmp://localhost:8092/live/[key]     │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│  2. VALIDATION: Verify stream_key exists │
│     Query: SELECT * FROM stream_configs  │
│     WHERE stream_key = ?                 │
└──────────────┬───────────────────────────┘
               │
        ┌──────┴──────┐
        │             │
    Found          Not Found
        │             │
        ▼             ▼
   Continue      Return 401
        │
        ▼
┌──────────────────────────────────────────┐
│  3. ROUTE: Load all active destinations  │
│     Query: SELECT * FROM destinations    │
│     WHERE config_id = ? AND is_active    │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│  4. FILTER: Remove force_cut destinations│
│     Exclude destinations where           │
│     force_cut = TRUE                     │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│  5. TRANSCODE: For each destination      │
│     - Get max_resolution                 │
│     - Calculate bitrate reduction (if)   │
│     - Apply codec (x264/x265/AV1)        │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│  6. PUSH: Send to destination RTMP URL   │
│     rtmp://platform/[stream_key]         │
│                                          │
│     Platforms:                           │
│     - rtmp://live.twitch.tv/app          │
│     - rtmp://a.rtmp.youtube.com/live2    │
│     - rtmp://ingest.kick.com             │
│     - Custom URLs                        │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│  7. MONITOR: Track success & failures    │
│     - Update stream_status (bitrate,     │
│       viewer count)                      │
│     - Log connection errors              │
│     - Retry on transient failures        │
└──────────────────────────────────────────┘
```

---

## Transcoding Pipeline

**Trigger**: Per-destination max_resolution + platform requirements

**Steps**:

1. **Decode**: Extract video/audio streams from ingest RTMP
2. **Analyze**: Detect input codec, bitrate, resolution
3. **Decide**: Based on `max_resolution`, choose target bitrate
   - 720p: ~2500 kbps
   - 1080p: ~5000 kbps
   - 2K: ~8000 kbps
4. **Encode**: Use appropriate codec
   - x264 (H.264) — Universal compatibility, lower performance
   - x265 (H.265) — Better compression, requires newer player
   - AV1 — Best compression, CPU-intensive
5. **Output**: Stream to destination RTMP URL

**Configuration**:
```python
TRANSCODE_PRESETS = {
    "720p": {"codec": "x264", "bitrate": 2500},
    "1080p": {"codec": "x265", "bitrate": 5000},
    "2K": {"codec": "x265", "bitrate": 8000},
}
```

---

## Upstream Integration

### MarchProxy (gRPC @ 50050)

**Purpose**: High-performance RTMP stream handling

**Integration**:
- Receives RTMP streams from encoders
- Proxies via gRPC to video_proxy_module
- Supports command-and-control for transcoding
- Handles backpressure and buffering

**Communication**:
```protobuf
// video_proxy_module calls MarchProxy gRPC
service MarchProxyService {
  rpc SendTranscodeCommand(TranscodeCommand) returns (TranscodeAck);
  rpc GetStreamMetrics(StreamMetricsRequest) returns (StreamMetrics);
}
```

### SeaweedFS (S3-compatible Object Storage)

**Purpose**: Video preview thumbnails and metadata

**Integration**:
- Store stream preview images every 10 seconds
- Bucket: `video-proxy` (configurable)
- Path: `{community_id}/{stream_key}/preview-{timestamp}.jpg`

**Configuration**:
```python
S3_ENDPOINT_URL = "infra-seaweedfs:8333"
S3_ACCESS_KEY_ID = "waddlebot"
S3_SECRET_ACCESS_KEY = "waddlebot"
S3_BUCKET_NAME = "video-proxy"
```

---

## Authentication & Authorization

### JWT Flow

```
1. Client obtains JWT (from auth service or locally)
   Payload: { "sub": "admin", "exp": 1708103445 }
   Signed with: JWT_SECRET_KEY

2. Client includes in every request:
   Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

3. @require_auth decorator validates:
   - Extract token from header
   - Verify signature
   - Check expiration
   - If invalid: return 401

4. Token payload available in request context:
   request.auth_payload = { "sub": "admin", ... }
```

### Role-Based Access Control (RBAC)

**Future Enhancement**: Add user roles to JWT payload

```python
# Planned implementation
@require_role("admin")
async def toggle_force_cut():
    # Only admins can emergency-stop streams
    pass
```

---

## Database Schema Diagram

```
stream_configs
├── id (PK)
├── community_id (UK) ──┐
├── stream_key (UK)     │
├── ingest_url          │
├── is_active           │
├── created_at          │
└── updated_at          │
                        │
                        1:N
                        │
stream_destinations     │
├── id (PK)             │
├── config_id (FK) ─────┘
├── platform
├── rtmp_url
├── stream_key
├── is_active
├── force_cut
├── max_resolution
├── created_at
└── updated_at

stream_configs
├── id (PK) ──┐
├── ...       │
                │
                1:1
                │
stream_status   │
├── id (PK)     │
├── config_id (FK, UK) ┘
├── is_streaming
├── viewer_count
├── bitrate_kbps
├── start_time
└── last_update
```

---

## Scalability Considerations

### Stateless Design
- No in-memory session state
- Horizontally scalable: add instances behind load balancer
- Database is single source of truth

### Connection Pooling
```python
DB_POOL_SIZE = 10        # 10 connections per instance
DB_POOL_RECYCLE = 3600   # Recycle connections every hour
```

### Load Balancing
```yaml
# Example nginx config
upstream video_proxy {
    server localhost:8092;
    server localhost:8092;  # Second instance on different port
}
```

---

**Last Updated**: 2026-02-16
**Repository**: github.com/penguintechinc/waddlebot
