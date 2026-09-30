# Video Proxy Module — Overview

**Video Proxy Module** is a multi-platform streaming service that proxies and transcodes live video streams to multiple destinations simultaneously. Built with Python, Quart, and gRPC, it manages stream configurations, handles multi-destination routing, and provides real-time stream status monitoring.

**Primary Purpose**: Act as an intelligent RTMP stream proxy and transcoding gateway, allowing communities to broadcast a single live stream to multiple platforms (Twitch, Kick, YouTube, custom RTMP endpoints) with selective quality control per destination.

**Language**: Python 3.13
**Framework**: Quart (async Python web framework)
**Port (gRPC)**: 50065
**Port (REST API)**: 8092
**Organization**: Penguin Tech Inc
**License**: Limited AGPL-3.0

---

## Core Capabilities

- **Stream Key Management**: Generate and regenerate unique stream keys per community with secure RTMP ingest URLs
- **Multi-Destination Routing**: Route single ingest stream to 3+ destinations (free tier) or unlimited (premium)
- **Quality Control**: Set per-destination maximum resolution (720p, 1080p, 2K, 4K)
- **Force-Cut Toggle**: Admin-only ability to force disconnect specific destinations mid-stream
- **Stream Status Monitoring**: Real-time viewer count, bitrate, and connection duration tracking
- **License Gating**: Free tier (3 destinations, 1 at 2K max) vs. Premium (unlimited)
- **JWT Authentication**: Secure REST API endpoints with JWT bearer tokens
- **PyDAL Database**: Multi-database backend support (PostgreSQL, SQLite, MySQL)

---

## Documentation Index

| Document | Purpose | Audience |
|----------|---------|----------|
| [OVERVIEW.md](./OVERVIEW.md) | Module purpose, capabilities, quick reference | All |
| [USAGE.md](./USAGE.md) | Getting started, Docker setup, common workflows | Developers, DevOps |
| [API.md](./API.md) | REST and gRPC endpoint specifications, schemas | Developers, API integrators |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | System design, data flows, component interactions | Architects, Developers |
| [CONFIGURATION.md](./CONFIGURATION.md) | Environment variables, required/optional settings | DevOps, System admins |
| [TESTING.md](./TESTING.md) | Mock streams, test endpoints, how to run tests | QA, Developers |
| [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) | Common issues, debug steps, solutions | DevOps, Support |
| [RELEASE_NOTES.md](./RELEASE_NOTES.md) | Version history, changelog, deprecations | All |

---

## Quick Reference

### Key Concepts

**Stream Configuration**: A per-community record containing:
- Unique stream key (secure token)
- RTMP ingest URL (where OBS/encoders send video)
- Active status flag

**Destination**: A platform-specific output target:
- Platform name (twitch, kick, youtube, custom)
- RTMP endpoint URL
- Stream key for that platform
- Max resolution setting
- Force-cut toggle

**Stream Status**: Real-time metrics:
- Is streaming (active/inactive)
- Viewer count across all destinations
- Bitrate (kbps)
- Session start time

### Default Ports

| Port | Service | Protocol |
|------|---------|----------|
| 8092 | REST API | HTTP |
| 50065 | gRPC Service | gRPC |

### Environment Fallbacks

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | PostgreSQL localhost | PyDAL connection string |
| `MODULE_PORT` | 8092 | REST API listen port |
| `GRPC_PORT` | 50065 | gRPC service listen port |
| `JWT_SECRET_KEY` | jwt-secret-change-in-production | JWT signing key |
| `FREE_MAX_DESTINATIONS` | 3 | Free tier destination limit |
| `FREE_MAX_2K_DESTINATIONS` | 1 | Free tier 2K destination limit |

### Health Check

```bash
curl http://localhost:8092/health
```

**Response** (200 OK):
```json
{
  "status": "healthy",
  "module": "video_proxy_module",
  "version": "1.0.0",
  "timestamp": "2026-02-16T10:30:45.123456",
  "database": "connected"
}
```

---

## Licensing & Feature Gating

**Free Tier** (RELEASE_MODE=false):
- 3 destinations per stream
- 1 destination maximum at 2K resolution
- Full API access
- AV1 encoding available

**Premium Tier** (RELEASE_MODE=true with valid LICENSE_KEY):
- Unlimited destinations
- All resolutions (4K, 8K support)
- Priority transcoding queue
- Full feature access

**Auto-Premium**: Domains ending in `.penguintech.io` automatically unlock premium features.

---

## Related Services

- **MarchProxy** (gRPC): Upstream RTMP stream handling
- **PostgreSQL/MySQL**: Stream metadata and configuration
- **SeaweedFS**: Video preview/thumbnail storage
- **License Server**: Feature validation and tracking

---

## Getting Started

1. **Read**: [USAGE.md](./USAGE.md) for local setup and Docker deployment
2. **Integrate**: [API.md](./API.md) for endpoint references
3. **Configure**: [CONFIGURATION.md](./CONFIGURATION.md) for environment setup
4. **Deploy**: Follow [ARCHITECTURE.md](./ARCHITECTURE.md) for production patterns
5. **Debug**: [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) for common issues

---

**Last Updated**: 2026-02-16
**Repository**: github.com/penguintechinc/waddlebot
**Support**: support@penguintech.io
