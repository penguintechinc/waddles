# Video Proxy Module — Configuration Guide

Complete reference for all environment variables, required settings, optional configurations, and example .env files.

---

## Table of Contents

1. [Overview](#overview)
2. [Database Configuration](#database-configuration)
3. [HTTP Server Configuration](#http-server-configuration)
4. [gRPC Configuration](#grpc-configuration)
5. [Upstream Services](#upstream-services)
6. [Authentication & Security](#authentication--security)
7. [S3-Compatible Object Storage Configuration](#s3-compatible-object-storage-configuration)
8. [License & Feature Gating](#license--feature-gating)
9. [Logging](#logging)
10. [Connection Pools & Timeouts](#connection-pools--timeouts)
11. [Redis Configuration](#redis-configuration)
12. [Example .env Files](#example-env-files)
13. [Configuration Validation](#configuration-validation)

---

## Overview

Configuration is managed via:
1. **Environment Variables**: Highest priority
2. **Defaults in `config.py`**: Used if env vars not set
3. **.env File**: Loaded at startup (optional, for local dev)

**Precedence**:
```
Command-line override → Environment Variable → .env File → config.py Default
```

**Note**: For production, set environment variables directly in container/pod; do not commit .env files.

---

## Database Configuration

### DATABASE_URL

**Type**: String (PostgreSQL connection string)
**Required**: Yes
**Default**: `postgresql://waddlebot:password@localhost:5432/waddlebot`
**Alternatives**: PostgreSQL, SQLite, MySQL, MariaDB

**Format**:
```
postgresql://[user]:[password]@[host]:[port]/[database]
```

**Examples**:
```bash
# PostgreSQL (standard)
DATABASE_URL=postgresql://waddlebot:secure_pass@db.example.com:5432/waddlebot

# PostgreSQL (local development)
DATABASE_URL=postgresql://postgres:password@localhost:5432/waddlebot_dev

# SQLite (single-file, dev only)
DATABASE_URL=sqlite://./videos.db

# MySQL
DATABASE_URL=mysql://waddlebot:password@localhost:3306/waddlebot

# MariaDB Galera (multi-node)
DATABASE_URL=mysql://waddlebot:password@galera-node1,galera-node2/waddlebot
```

**Notes**:
- Connection string is validated at startup
- SQLite not recommended for production (no concurrency)
- PostgreSQL recommended (ACID compliance, JSON support)

---

### DB_POOL_SIZE

**Type**: Integer
**Required**: No
**Default**: 10
**Range**: 1-50

Number of persistent database connections to maintain in the pool.

```bash
# Development (low concurrency)
DB_POOL_SIZE=5

# Production (high concurrency)
DB_POOL_SIZE=20
```

---

### DB_POOL_RECYCLE

**Type**: Integer (seconds)
**Required**: No
**Default**: 3600
**Range**: 60-86400

Recycle database connections after this many seconds to prevent stale connections.

```bash
# Hourly recycle
DB_POOL_RECYCLE=3600

# 30-minute recycle (for more aggressive cleanup)
DB_POOL_RECYCLE=1800
```

---

## HTTP Server Configuration

### MODULE_HOST

**Type**: String (IP address or hostname)
**Required**: No
**Default**: `0.0.0.0` (all interfaces)

Interface to bind REST API to.

```bash
# All interfaces (Kubernetes, Docker)
MODULE_HOST=0.0.0.0

# Localhost only (local dev)
MODULE_HOST=127.0.0.1

# Specific interface
MODULE_HOST=192.168.1.100
```

---

### MODULE_PORT

**Type**: Integer (port number)
**Required**: No
**Default**: 8092
**Range**: 1-65535

REST API HTTP listen port.

```bash
# Standard port
MODULE_PORT=8092

# Non-privileged port
MODULE_PORT=9000

# Kubernetes service port
MODULE_PORT=8080
```

---

### MODULE_VERSION

**Type**: String (semver)
**Required**: No
**Default**: `1.0.0`

Application version string. Appears in `/health` endpoint response.

```bash
# Semantic versioning
MODULE_VERSION=1.2.0

# Pre-release
MODULE_VERSION=1.2.0-beta.1

# With build metadata
MODULE_VERSION=1.2.0+build.123
```

---

## gRPC Configuration

### GRPC_HOST

**Type**: String (IP address or hostname)
**Required**: No
**Default**: `0.0.0.0` (all interfaces)

Interface to bind gRPC service to.

```bash
# All interfaces
GRPC_HOST=0.0.0.0

# Localhost only
GRPC_HOST=127.0.0.1
```

---

### GRPC_PORT

**Type**: Integer (port number)
**Required**: No
**Default**: 50065
**Range**: 1-65535

gRPC service listen port. (See [GRPC_PORT_VISUAL_REFERENCE.txt](../GRPC_PORT_VISUAL_REFERENCE.txt))

```bash
# Standard allocation
GRPC_PORT=50065

# Custom port
GRPC_PORT=9001
```

---

### GRPC_TIMEOUT

**Type**: Integer (seconds)
**Required**: No
**Default**: 30
**Range**: 1-300

gRPC request timeout.

```bash
# Standard timeout
GRPC_TIMEOUT=30

# Extended timeout for large uploads
GRPC_TIMEOUT=60

# Short timeout for fast operations
GRPC_TIMEOUT=5
```

---

## Upstream Services

### MARCHPROXY_GRPC_HOST

**Type**: String (hostname or IP)
**Required**: No
**Default**: `localhost`

Hostname of MarchProxy gRPC service (upstream RTMP handler).

```bash
# Local Kubernetes service
MARCHPROXY_GRPC_HOST=marchproxy-service

# Docker container
MARCHPROXY_GRPC_HOST=marchproxy

# External service
MARCHPROXY_GRPC_HOST=marchproxy.example.com
```

---

### MARCHPROXY_GRPC_PORT

**Type**: Integer
**Required**: No
**Default**: 50050

Port of MarchProxy gRPC service.

```bash
MARCHPROXY_GRPC_PORT=50050
```

---

## Authentication & Security

### JWT_SECRET_KEY

**Type**: String (cryptographic key)
**Required**: Yes
**Default**: `jwt-secret-change-in-production` (DEV ONLY)

Secret key for JWT signing and verification. **Must be changed in production.**

**Generation**:
```bash
# Python
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# OpenSSL
openssl rand -base64 32
```

**Examples**:
```bash
# 32-byte random key
JWT_SECRET_KEY=<32-byte-random-string-from-openssl-rand>

# Base64 encoded
JWT_SECRET_KEY=<base64-encoded-32-byte-random-string>

# In .env file (recommended)
JWT_SECRET_KEY=$(openssl rand -base64 32)
```

**Security Best Practices**:
- Minimum 32 characters (256 bits)
- Use cryptographically random source
- Rotate quarterly
- Store in secrets manager (Kubernetes secrets, AWS Secrets Manager)
- Never commit to version control

---

### MODULE_SECRET_KEY

**Type**: String (cryptographic key)
**Required**: No
**Default**: `change-me-in-production`

General-purpose secret key for module. (Currently unused, reserved for future features)

```bash
MODULE_SECRET_KEY=your-secure-secret-key-here
```

---

## S3-Compatible Object Storage Configuration

### S3_ENDPOINT_URL

**Type**: String (hostname:port)
**Required**: No
**Default**: `http://localhost:8333`

S3-compatible (SeaweedFS) object storage endpoint.

```bash
# Local development
S3_ENDPOINT_URL=localhost:8333

# Docker Compose
S3_ENDPOINT_URL=infra-seaweedfs:8333

# Kubernetes service
S3_ENDPOINT_URL=infra-seaweedfs:8333

# AWS S3
S3_ENDPOINT_URL=s3.amazonaws.com

# DigitalOcean Spaces
S3_ENDPOINT_URL=nyc3.digitaloceanspaces.com
```

---

### S3_ACCESS_KEY_ID

**Type**: String (access key ID)
**Required**: No
**Default**: `waddlebot`

S3 access key.

```bash
S3_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
```

---

### S3_SECRET_ACCESS_KEY

**Type**: String (secret access key)
**Required**: No
**Default**: `waddlebot`

S3 secret access key. **Should be kept secure.**

```bash
S3_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

---

### S3_BUCKET_NAME

**Type**: String (bucket name)
**Required**: No
**Default**: `video-proxy`

S3 bucket for video thumbnails and metadata.

```bash
# Standard bucket
S3_BUCKET_NAME=video-proxy

# Community-specific bucket
S3_BUCKET_NAME=waddlebot-videos-prod

# Environment-specific
S3_BUCKET_NAME=waddlebot-videos-staging
```

---

### S3_FORCE_PATH_STYLE

**Type**: Boolean (`true` or `false`)
**Required**: No
**Default**: `true`

Force path-style addressing (required by SeaweedFS).

```bash
# Local development (no SSL)
S3_FORCE_PATH_STYLE=true

# Production (use SSL)
S3_FORCE_PATH_STYLE=true
```

---

## License & Feature Gating

### LICENSE_SERVER_URL

**Type**: String (HTTPS URL)
**Required**: No
**Default**: `https://license.penguintech.io`

URL of Penguin Tech License Server for feature validation.

```bash
LICENSE_SERVER_URL=https://license.penguintech.io

# Staging license server
LICENSE_SERVER_URL=https://license-staging.penguintech.io

# Custom license server
LICENSE_SERVER_URL=https://licensing.example.com
```

---

### LICENSE_KEY

**Type**: String (license key)
**Required**: No (unless RELEASE_MODE=true)
**Default**: None

License key for premium features. Required in production (RELEASE_MODE=true).

```bash
# Development (no license needed)
LICENSE_KEY=

# Production (license required)
LICENSE_KEY=LIC-2026-0001-XXXXX-XXXXX
```

**License Format**:
```
LIC-[year]-[sequence]-[customer_id]-[verification]
```

---

### RELEASE_MODE

**Type**: Boolean (`true` or `false`)
**Required**: No
**Default**: `false`

Enable release mode (production). Requires valid LICENSE_KEY.

```bash
# Development (features available, no license check)
RELEASE_MODE=false

# Production (license enforced)
RELEASE_MODE=true
```

**Effect When true**:
- LICENSE_KEY validation required at startup
- Feature limits enforced from license
- Premium features require valid license
- Fails fast if license invalid/expired

---

### FREE_MAX_DESTINATIONS

**Type**: Integer
**Required**: No
**Default**: 3
**Range**: 1-10

Maximum number of destinations for free-tier streams.

```bash
# Conservative (2 destinations)
FREE_MAX_DESTINATIONS=2

# Standard (3 destinations)
FREE_MAX_DESTINATIONS=3

# Generous (5 destinations)
FREE_MAX_DESTINATIONS=5
```

---

### FREE_MAX_2K_DESTINATIONS

**Type**: Integer
**Required**: No
**Default**: 1
**Range**: 0-5

Maximum number of 2K+ resolution destinations for free tier.

```bash
# Strict (0 high-res destinations)
FREE_MAX_2K_DESTINATIONS=0

# Standard (1 high-res destination)
FREE_MAX_2K_DESTINATIONS=1

# Generous (2 high-res destinations)
FREE_MAX_2K_DESTINATIONS=2
```

---

## Logging

### LOG_LEVEL

**Type**: String (Python logging level)
**Required**: No
**Default**: `INFO`
**Options**: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`

Logging verbosity level.

```bash
# Development (verbose)
LOG_LEVEL=DEBUG

# Standard production
LOG_LEVEL=INFO

# Minimal logging (high-volume scenarios)
LOG_LEVEL=WARNING

# Only errors
LOG_LEVEL=ERROR
```

---

### LOG_FORMAT

**Type**: String
**Required**: No
**Default**: `text`
**Options**: `text`, `json`

Log output format.

```bash
# Human-readable text
LOG_FORMAT=text

# Structured JSON (for log aggregation)
LOG_FORMAT=json
```

---

## Connection Pools & Timeouts

### HTTP_TIMEOUT

**Type**: Integer (seconds)
**Required**: No
**Default**: 30
**Range**: 1-300

HTTP request timeout.

```bash
# Standard
HTTP_TIMEOUT=30

# Extended (for slow clients)
HTTP_TIMEOUT=60

# Short (for fast operations)
HTTP_TIMEOUT=5
```

---

## Redis Configuration

### REDIS_HOST

**Type**: String (hostname)
**Required**: No
**Default**: `localhost`

Redis server hostname.

```bash
# Local development
REDIS_HOST=localhost

# Kubernetes service
REDIS_HOST=redis-service

# External Redis
REDIS_HOST=redis.example.com
```

---

### REDIS_PORT

**Type**: Integer
**Required**: No
**Default**: 6379

Redis server port.

```bash
REDIS_PORT=6379
```

---

### REDIS_PASSWORD

**Type**: String (optional password)
**Required**: No
**Default**: None

Redis password (if authentication enabled).

```bash
# No password
REDIS_PASSWORD=

# With password
REDIS_PASSWORD=your-redis-password
```

---

### REDIS_DB

**Type**: Integer (0-15)
**Required**: No
**Default**: 0

Redis database index.

```bash
# Default database
REDIS_DB=0

# Secondary database
REDIS_DB=1
```

---

### REDIS_URL

**Type**: String (connection URL)
**Required**: No
**Default**: None

Full Redis connection URL (overrides REDIS_HOST/PORT/PASSWORD).

```bash
# Format: redis://[:password@]host:port/db
REDIS_URL=redis://localhost:6379/0

# With password
REDIS_URL=redis://:password@redis.example.com:6379/1
```

---

## Example .env Files

### Development (Local)

**File**: `.env.development`

```bash
# Database (local PostgreSQL)
DATABASE_URL=postgresql://waddlebot:password@localhost:5432/waddlebot_dev
DB_POOL_SIZE=5
DB_POOL_RECYCLE=3600

# HTTP Server
MODULE_HOST=0.0.0.0
MODULE_PORT=8092
MODULE_VERSION=1.0.0-dev

# gRPC
GRPC_HOST=0.0.0.0
GRPC_PORT=50065

# Upstream Services
MARCHPROXY_GRPC_HOST=localhost
MARCHPROXY_GRPC_PORT=50050

# JWT
JWT_SECRET_KEY=dev-secret-key-change-me

# S3-Compatible Object Storage (SeaweedFS)
S3_ENDPOINT_URL=localhost:8333
S3_ACCESS_KEY_ID=waddlebot
S3_SECRET_ACCESS_KEY=waddlebot
S3_BUCKET_NAME=video-proxy-dev
S3_FORCE_PATH_STYLE=true

# License (development, no license required)
RELEASE_MODE=false
LICENSE_KEY=

# Features
FREE_MAX_DESTINATIONS=3
FREE_MAX_2K_DESTINATIONS=1

# Logging
LOG_LEVEL=DEBUG
LOG_FORMAT=text

# Timeouts
GRPC_TIMEOUT=30
HTTP_TIMEOUT=30

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
```

---

### Staging (Docker Compose)

**File**: `.env.staging`

```bash
# Database (PostgreSQL in Docker)
DATABASE_URL=postgresql://waddlebot:secure_password@postgres:5432/waddlebot
DB_POOL_SIZE=10
DB_POOL_RECYCLE=3600

# HTTP Server
MODULE_HOST=0.0.0.0
MODULE_PORT=8092
MODULE_VERSION=1.1.0

# gRPC
GRPC_HOST=0.0.0.0
GRPC_PORT=50065

# Upstream Services
MARCHPROXY_GRPC_HOST=marchproxy
MARCHPROXY_GRPC_PORT=50050

# JWT (from secrets manager)
JWT_SECRET_KEY=<generated-secure-key-from-secrets>

# S3-Compatible Object Storage (SeaweedFS, Docker service)
S3_ENDPOINT_URL=infra-seaweedfs:8333
S3_ACCESS_KEY_ID=<from-secrets>
S3_SECRET_ACCESS_KEY=<from-secrets>
S3_BUCKET_NAME=video-proxy-staging
S3_FORCE_PATH_STYLE=true

# License (staging, no license enforced)
RELEASE_MODE=false
LICENSE_KEY=

# Features
FREE_MAX_DESTINATIONS=3
FREE_MAX_2K_DESTINATIONS=1

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=json

# Timeouts
GRPC_TIMEOUT=30
HTTP_TIMEOUT=30

# Redis (Docker service)
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0
```

---

### Production (Kubernetes)

**Note**: Do NOT use .env files in production. Use Kubernetes secrets.

**File**: `k8s/video-proxy-configmap.yaml`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: video-proxy-config
data:
  MODULE_HOST: "0.0.0.0"
  MODULE_PORT: "8092"
  MODULE_VERSION: "1.1.0"
  GRPC_HOST: "0.0.0.0"
  GRPC_PORT: "50065"
  MARCHPROXY_GRPC_HOST: "marchproxy-service"
  MARCHPROXY_GRPC_PORT: "50050"
  RELEASE_MODE: "true"
  FREE_MAX_DESTINATIONS: "3"
  FREE_MAX_2K_DESTINATIONS: "1"
  LOG_LEVEL: "INFO"
  LOG_FORMAT: "json"
  GRPC_TIMEOUT: "30"
  HTTP_TIMEOUT: "30"
  S3_BUCKET_NAME: "video-proxy-prod"
  S3_FORCE_PATH_STYLE: "true"
```

**File**: `k8s/video-proxy-secrets.yaml`

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: video-proxy-secrets
type: Opaque
stringData:
  DATABASE_URL: postgresql://waddlebot:$(DB_PASSWORD)@postgres-ha:5432/waddlebot
  JWT_SECRET_KEY: $(JWT_SECRET_FROM_VAULT)
  S3_ACCESS_KEY_ID: $(S3_ACCESS_KEY_FROM_VAULT)
  S3_SECRET_ACCESS_KEY: $(S3_SECRET_KEY_FROM_VAULT)
  LICENSE_KEY: $(LICENSE_KEY_FROM_VAULT)
```

---

## Configuration Validation

### Startup Validation

Configuration is validated at application startup (in `config.validate()`):

```python
def validate(self) -> None:
    # Port validation
    if not (1 <= self.MODULE_PORT <= 65535):
        raise ValueError(f'Invalid MODULE_PORT: {self.MODULE_PORT}')

    # Database URL validation
    if not self.DATABASE_URL:
        raise ValueError('DATABASE_URL must be set')

    # JWT validation
    if not self.JWT_SECRET_KEY:
        raise ValueError('JWT_SECRET_KEY must be set')

    # License validation
    if self.RELEASE_MODE and not self.LICENSE_KEY:
        raise ValueError('LICENSE_KEY required in RELEASE_MODE')
```

### Testing Configuration

```bash
# Test configuration without starting app
python3 -c "from config import Config; c = Config(); c.validate(); print('Config OK')"

# Check specific values
python3 -c "from config import Config; print(f'Database: {Config().DATABASE_URL}')"
```

---

**Last Updated**: 2026-02-16
**Repository**: github.com/penguintechinc/waddlebot
