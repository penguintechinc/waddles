# Hub Module Configuration

## Overview

The Waddles Hub Module is configured through environment variables, configuration files, and Docker. This document covers all configuration options for both backend and frontend components.

---

## Table of Contents

- [Environment Variables](#environment-variables)
- [Backend Configuration](#backend-configuration)
- [Frontend Configuration](#frontend-configuration)
- [Docker Configuration](#docker-configuration)
- [Database Configuration](#database-configuration)
- [Security Configuration](#security-configuration)
- [Module Integration](#module-integration)
- [OAuth Platform Configuration](#oauth-platform-configuration)
- [Production Deployment](#production-deployment)

---

## Environment Variables

### Backend Environment Variables

Location: `/admin/hub_module/backend/.env`

```bash
# Server Configuration
NODE_ENV=development                    # Environment: development|production
PORT=8060                               # HTTP server port
HOST=0.0.0.0                           # Bind address (0.0.0.0 for all interfaces)

# Database Configuration
DATABASE_URL=postgresql://waddlebot:password@postgres:5432/waddlebot
DATABASE_POOL_SIZE=10                   # Connection pool size

# JWT Authentication
JWT_SECRET=your-jwt-secret-change-in-production
JWT_EXPIRES_IN=3600                     # Token expiration in seconds (1 hour)

# Session Management
SESSION_TTL=3600                        # Session time-to-live in seconds

# Temporary Password System
TEMP_PASSWORD_EXPIRES_HOURS=48          # Temp password expiration (hours)

# OAuth & Identity Integration
IDENTITY_API_URL=http://identity-core:8050
OAUTH_CALLBACK_BASE_URL=http://localhost:8060

# Module Integration URLs
ROUTER_API_URL=http://router:8000
REPUTATION_API_URL=http://reputation:8021
LABELS_API_URL=http://labels-core:8023
INVENTORY_API_URL=http://inventory:8024
MEMORIES_API_URL=http://memories:8031
BROWSER_SOURCE_API_URL=http://browser-source:8027
LOYALTY_API_URL=http://loyalty-interaction:8032

# Custom Domains
BASE_DOMAIN=waddlebot.io

# Rate Limiting
RATE_LIMIT_WINDOW_MS=60000              # Rate limit window (60 seconds)
RATE_LIMIT_MAX_REQUESTS=100             # Max requests per window

# Logging
LOG_LEVEL=info                          # Log level: debug|info|warn|error
LOG_DIR=/var/log/waddlebotlog          # Log directory path

# CORS
CORS_ORIGIN=http://localhost:5173       # Allowed CORS origin for frontend

# Service API Key (for internal service-to-service communication)
SERVICE_API_KEY=dev-service-key-ONLY-FOR-DEVELOPMENT
```

### Frontend Environment Variables

Location: `/admin/hub_module/frontend/.env`

```bash
# API Configuration
VITE_API_URL=                           # API base URL (empty for same origin)
```

---

## Backend Configuration

### Configuration Structure

The backend configuration is located in `/admin/hub_module/backend/src/config/index.js`:

```javascript
export const config = {
  // Server settings
  env: process.env.NODE_ENV || 'development',
  port: parseInt(process.env.PORT || '8060', 10),
  host: process.env.HOST || '0.0.0.0',

  // Database settings
  database: {
    url: process.env.DATABASE_URL || 'postgresql://waddlebot:password@localhost:5432/waddlebot',
    poolSize: parseInt(process.env.DATABASE_POOL_SIZE || '10', 10),
  },

  // JWT settings
  jwt: {
    secret: process.env.JWT_SECRET,
    expiresIn: parseInt(process.env.JWT_EXPIRES_IN || '3600', 10),
  },

  // Session settings
  session: {
    ttl: parseInt(process.env.SESSION_TTL || '3600', 10),
  },

  // OAuth/Identity settings
  identity: {
    apiUrl: process.env.IDENTITY_API_URL || 'http://identity-core:8050',
    callbackBaseUrl: process.env.OAUTH_CALLBACK_BASE_URL || 'http://localhost:8060',
  },

  // Module integration URLs
  modules: {
    router: process.env.ROUTER_API_URL || 'http://router:8000',
    reputation: process.env.REPUTATION_API_URL || 'http://reputation:8021',
    labels: process.env.LABELS_API_URL || 'http://labels-core:8023',
    inventory: process.env.INVENTORY_API_URL || 'http://inventory:8024',
    memories: process.env.MEMORIES_API_URL || 'http://memories:8031',
    browserSource: process.env.BROWSER_SOURCE_API_URL || 'http://browser-source:8027',
    loyalty: process.env.LOYALTY_API_URL || 'http://loyalty-interaction:8032',
  },

  // Custom domains
  baseDomain: process.env.BASE_DOMAIN || 'waddlebot.io',
  blockedSubdomains: [
    'www', 'mail', 'smtp', 'imap', 'pop', 'ftp', 'api', 'admin', 'portal', 'hub',
    'app', 'dashboard', 'status', 'docs', 'help', 'support', 'billing',
    'cdn', 'static', 'assets', 'media', 'img', 'images', 'dev', 'staging',
    'test', 'demo', 'beta', 'auth', 'login', 'oauth', 'sso', 'identity',
  ],

  // Rate limiting
  rateLimit: {
    windowMs: parseInt(process.env.RATE_LIMIT_WINDOW_MS || '60000', 10),
    maxRequests: parseInt(process.env.RATE_LIMIT_MAX_REQUESTS || '100', 10),
  },

  // Service API key
  serviceApiKey: process.env.SERVICE_API_KEY,

  // Logging
  logging: {
    level: process.env.LOG_LEVEL || 'info',
    dir: process.env.LOG_DIR || '/var/log/waddlebotlog',
  },

  // CORS
  cors: {
    origin: process.env.CORS_ORIGIN || 'http://localhost:5173',
  },
};
```

### Production Security Validation

In production mode, the configuration validates critical secrets:

```javascript
if (process.env.NODE_ENV === 'production') {
  if (!process.env.JWT_SECRET ||
      process.env.JWT_SECRET.includes('dev-secret') ||
      process.env.JWT_SECRET.includes('change-in-production')) {
    throw new Error('FATAL: JWT_SECRET must be set to a strong secret in production.');
  }

  if (!process.env.SERVICE_API_KEY ||
      process.env.SERVICE_API_KEY.includes('dev-service') ||
      process.env.SERVICE_API_KEY.includes('change-in-production')) {
    throw new Error('FATAL: SERVICE_API_KEY must be set to a strong key in production.');
  }
}
```

---

## Frontend Configuration

### Vite Configuration

Location: `/admin/hub_module/frontend/vite.config.js`

```javascript
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],

  // Development server
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8060',
        changeOrigin: true,
      },
    },
  },

  // Build configuration
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
```

### Package Configuration

**Backend** (`/admin/hub_module/backend/package.json`):

```json
{
  "name": "waddlebot-hub",
  "version": "1.0.1",
  "type": "module",
  "scripts": {
    "start": "node src/index.js",
    "dev": "node --watch src/index.js",
    "lint": "eslint src/",
    "test": "node --test"
  },
  "engines": {
    "node": ">=20.0.0"
  }
}
```

**Frontend** (`/admin/hub_module/frontend/package.json`):

```json
{
  "name": "waddlebot-hub-frontend",
  "version": "1.0.1",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview",
    "lint": "eslint . --ext js,jsx"
  }
}
```

---

## Docker Configuration

### Dockerfile

Location: `/admin/hub_module/Dockerfile`

**Multi-stage build:**

```dockerfile
# Stage 1: Build frontend
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY admin/hub_module/frontend/package*.json ./
RUN npm install
COPY admin/hub_module/frontend/ ./
RUN npm run build

# Stage 2: Production image
FROM node:20-alpine
WORKDIR /app

# Install dumb-init and build tools for native modules
RUN apk add --no-cache dumb-init python3 make g++

# Copy healthcheck script
COPY scripts/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 755 /usr/local/bin/healthcheck.py

# Create non-root user
RUN addgroup -g 1001 -S nodejs && \
    adduser -S nodejs -u 1001

# Install backend dependencies
COPY admin/hub_module/backend/package*.json ./
RUN npm install --only=production

# Remove build tools
RUN apk del python3 make g++

# Copy backend source
COPY --chown=nodejs:nodejs admin/hub_module/backend/src ./src

# Copy built frontend
COPY --chown=nodejs:nodejs --from=frontend-build /app/frontend/dist ./public

# Create log directory
RUN mkdir -p /var/log/waddlebotlog && chown -R nodejs:nodejs /var/log/waddlebotlog /app

# Environment
ENV NODE_ENV=production
ENV PORT=8060

# Switch to non-root user
USER nodejs

# Expose port
EXPOSE 8060

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 /usr/local/bin/healthcheck.py http://localhost:8060/healthz

# Start with dumb-init
ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "src/index.js"]
```

### Docker Compose Example

```yaml
version: '3.8'

services:
  hub:
    build:
      context: .
      dockerfile: admin/hub_module/Dockerfile
    ports:
      - "8060:8060"
    environment:
      NODE_ENV: production
      DATABASE_URL: postgresql://waddlebot:password@postgres:5432/waddlebot
      JWT_SECRET: ${JWT_SECRET}
      SERVICE_API_KEY: ${SERVICE_API_KEY}
      CORS_ORIGIN: https://hub.example.com
    depends_on:
      - postgres
    restart: unless-stopped
    volumes:
      - hub-logs:/var/log/waddlebotlog

volumes:
  hub-logs:
```

---

## Database Configuration

### Connection Pool

```javascript
// /admin/hub_module/backend/src/config/database.js
import pg from 'pg';
const { Pool } = pg;

const pool = new Pool({
  connectionString: config.database.url,
  max: config.database.poolSize,           // Maximum connections (default: 10)
  idleTimeoutMillis: 30000,                // Close idle clients after 30s
  connectionTimeoutMillis: 5000,           // Connection attempt timeout
  statement_timeout: 30000,                // Query timeout (30s)
});
```

### Database Schema

The hub module creates these tables on first startup:

| Table | Description |
|-------|-------------|
| `hub_admins` | Legacy admin users |
| `hub_sessions` | Active user sessions |
| `hub_temp_passwords` | Temporary password tokens |
| `hub_users` | Unified user accounts |
| `hub_user_identities` | Linked platform identities |
| `communities` | Communities/servers |
| `community_members` | Community membership |
| `hub_chat_messages` | Cross-platform chat history |
| `hub_modules` | Module registry |
| `hub_module_installations` | Installed modules per community |
| `hub_module_reviews` | Module ratings/reviews |
| `announcements` | Community announcements |
| `announcement_broadcasts` | Announcement broadcast status |
| `community_overlay_tokens` | Browser source overlay keys |
| `overlay_access_log` | Overlay access tracking |
| `analytics_bot_scores` | Community bot detection scores |
| `analytics_suspected_bots` | Suspected bot accounts |

### Migrations

Database migrations are located in `/config/postgres/migrations/`:

```sql
-- 007_add_translation_config.sql
ALTER TABLE communities ADD COLUMN IF NOT EXISTS translation_config JSONB DEFAULT '{}';
```

---

## Security Configuration

### Helmet Configuration

The hub module uses Helmet.js for security headers:

```javascript
app.use(helmet({
  contentSecurityPolicy: {
    directives: {
      defaultSrc: ["'self'"],
      scriptSrc: ["'self'", "'unsafe-inline'"],
      styleSrc: ["'self'", "'unsafe-inline'"],
      imgSrc: ["'self'", "data:", "https:"],
      connectSrc: ["'self'", "ws:", "wss:"],
      fontSrc: ["'self'", "data:"],
      objectSrc: ["'none'"],
      mediaSrc: ["'self'"],
      frameSrc: ["'none'"],
    },
  },
  crossOriginEmbedderPolicy: false,      // Allow embedding for OBS
  hsts: {
    maxAge: 31536000,
    includeSubDomains: true,
    preload: true,
  },
}));
```

### CSRF Protection

CSRF tokens are automatically managed:

```javascript
import { setCsrfToken, verifyCsrfToken } from './middleware/csrf.js';

// Set token on all requests
app.use(setCsrfToken);

// Verify on POST/PUT/PATCH/DELETE
app.use(verifyCsrfToken);
```

### Rate Limiting

```javascript
import rateLimit from 'express-rate-limit';

const limiter = rateLimit({
  windowMs: config.rateLimit.windowMs,          // 60 seconds
  max: config.rateLimit.maxRequests,            // 100 requests
  message: {
    success: false,
    error: {
      code: 'RATE_LIMITED',
      message: 'Too many requests'
    }
  },
  standardHeaders: true,
  legacyHeaders: false,
});

app.use('/api/', limiter);
```

### XSS Protection

All user input is sanitized:

```javascript
import { sanitizeBody } from './middleware/validation.js';

app.use(sanitizeBody);  // Sanitizes all string inputs
```

---

## Module Integration

### Service URLs

The hub module integrates with other Waddles services:

| Service | Environment Variable | Default URL |
|---------|---------------------|-------------|
| Router | `ROUTER_API_URL` | `http://router:8000` |
| Reputation | `REPUTATION_API_URL` | `http://reputation:8021` |
| Labels | `LABELS_API_URL` | `http://labels-core:8023` |
| Inventory | `INVENTORY_API_URL` | `http://inventory:8024` |
| Memories | `MEMORIES_API_URL` | `http://memories:8031` |
| Browser Source | `BROWSER_SOURCE_API_URL` | `http://browser-source:8027` |
| Loyalty | `LOYALTY_API_URL` | `http://loyalty-interaction:8032` |
| Identity | `IDENTITY_API_URL` | `http://identity-core:8050` |

### API Gateway Integration

The hub module proxies requests to internal services:

```javascript
// Analytics proxy
router.get('/:communityId/analytics/*', requireCommunityAdmin, async (req, res) => {
  const analyticsPath = req.params[0];
  const response = await httpClient.get(
    `http://analytics-core:8040/api/v1/analytics/${req.params.communityId}/${analyticsPath}`,
    {
      params: req.query,
      headers: {
        'X-API-Key': req.headers['x-api-key'],
        'X-Community-ID': req.params.communityId,
      },
    }
  );
  res.json(response.data);
});
```

---

## OAuth Platform Configuration

### Supported Platforms

| Platform | OAuth Flow | Required Scopes |
|----------|-----------|----------------|
| Discord | OAuth2 | `identify`, `guilds` |
| Twitch | OAuth2 | `user:read:email` |
| YouTube | OAuth2 | `userinfo.email`, `userinfo.profile` |
| KICK | OAuth2 | `user:read` |
| Slack | OAuth2 | `identity.basic` |

### Platform Configuration (SuperAdmin)

Platform OAuth credentials are configured via SuperAdmin panel:

**Endpoint:** `PUT /api/v1/superadmin/platform-config/:platform`

```json
{
  "clientId": "your-client-id",
  "clientSecret": "your-client-secret",
  "redirectUri": "https://hub.example.com/api/v1/auth/oauth/:platform/callback",
  "enabled": true
}
```

### OAuth Flow

```
1. User clicks "Login with Discord"
2. GET /api/v1/auth/oauth/discord → Redirects to Discord
3. User approves on Discord
4. Discord redirects to /api/v1/auth/oauth/discord/callback
5. Hub exchanges code for tokens
6. Hub creates/updates user account
7. Hub issues JWT token
8. Frontend receives token and stores it
```

---

## Production Deployment

### Environment Checklist

Before deploying to production:

- [ ] Set strong `JWT_SECRET` (at least 64 random characters)
- [ ] Set strong `SERVICE_API_KEY` (at least 64 random characters)
- [ ] Configure production database URL
- [ ] Set `NODE_ENV=production`
- [ ] Configure CORS origin for production domain
- [ ] Set up SSL/TLS certificates
- [ ] Configure OAuth redirect URIs for production domain
- [ ] Set up log rotation for `/var/log/waddlebotlog`
- [ ] Configure backup strategy for PostgreSQL
- [ ] Set up monitoring and alerting

### Production Environment Variables

```bash
NODE_ENV=production
PORT=8060
HOST=0.0.0.0

DATABASE_URL=postgresql://waddlebot:STRONG_PASSWORD@postgres:5432/waddlebot
DATABASE_POOL_SIZE=20

JWT_SECRET=GENERATE_64_CHARACTER_RANDOM_STRING_HERE
SERVICE_API_KEY=GENERATE_64_CHARACTER_RANDOM_STRING_HERE

OAUTH_CALLBACK_BASE_URL=https://hub.example.com

CORS_ORIGIN=https://hub.example.com

LOG_LEVEL=info
LOG_DIR=/var/log/waddlebotlog

RATE_LIMIT_WINDOW_MS=60000
RATE_LIMIT_MAX_REQUESTS=100
```

### SSL/TLS Configuration

For production, use a reverse proxy (nginx, Caddy, or Kong) to handle SSL/TLS:

```nginx
# nginx example
server {
    listen 443 ssl http2;
    server_name hub.example.com;

    ssl_certificate /etc/ssl/certs/hub.example.com.crt;
    ssl_certificate_key /etc/ssl/private/hub.example.com.key;

    location / {
        proxy_pass http://localhost:8060;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket support
    location /socket.io/ {
        proxy_pass http://localhost:8060;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### Health Monitoring

**Health Check Endpoint:** `GET /health`

Response:
```json
{
  "module": "hub_module",
  "version": "1.0.0",
  "status": "healthy",
  "timestamp": "2024-03-15T10:00:00Z",
  "database": "connected"
}
```

**Metrics Endpoint:** `GET /metrics`

Response:
```json
{
  "module": "hub_module",
  "version": "1.0.0",
  "timestamp": "2024-03-15T10:00:00Z",
  "database": {
    "pool": {
      "total": 10,
      "idle": 8,
      "waiting": 0
    },
    "health": true
  },
  "uptime": 86400,
  "memory": {
    "rss": 134217728,
    "heapTotal": 67108864,
    "heapUsed": 45088768
  }
}
```

---

## Configuration Best Practices

1. **Never commit secrets** to version control
2. **Use environment variables** for all sensitive configuration
3. **Validate configuration** at startup in production
4. **Use strong secrets** (64+ characters, random)
5. **Enable rate limiting** to prevent abuse
6. **Configure CORS** restrictively for production
7. **Set up log rotation** to prevent disk space issues
8. **Use connection pooling** for database efficiency
9. **Enable health checks** for monitoring
10. **Document all custom configuration** for your team

---

## Troubleshooting Configuration

### Database Connection Errors

```bash
# Check connection string
echo $DATABASE_URL

# Test connection
psql $DATABASE_URL -c "SELECT 1"

# Check pool size
# Increase if seeing "Client has encountered a connection error and is not queryable"
DATABASE_POOL_SIZE=20
```

### JWT Token Errors

```bash
# Ensure JWT_SECRET is set
echo $JWT_SECRET | wc -c  # Should be 64+ characters

# Check token expiration
JWT_EXPIRES_IN=3600  # 1 hour
```

### CORS Errors

```bash
# Ensure frontend URL is allowed
CORS_ORIGIN=https://hub.example.com

# For development with multiple origins
CORS_ORIGIN=http://localhost:5173,http://localhost:3000
```

### Module Integration Errors

```bash
# Check service URLs are reachable
curl http://identity-core:8050/health
curl http://analytics-core:8040/health

# Verify service API key
echo $SERVICE_API_KEY
```
