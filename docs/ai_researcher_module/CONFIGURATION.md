# AI Researcher Module — Configuration Guide

## Configuration Overview

The AI Researcher Module is configured entirely through environment variables. A `.env` file in the module directory or container environment can provide all settings. Configuration is validated on startup.

## Environment Variables

### Module Information

```bash
MODULE_NAME=ai_researcher_module
MODULE_VERSION=1.0.0
MODULE_PORT=8070
```

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| MODULE_NAME | ai_researcher_module | No | Module identifier |
| MODULE_VERSION | 1.0.0 | No | Version string |
| MODULE_PORT | 8070 | No | REST API port |

### Database Configuration

```bash
DATABASE_URL=postgresql://waddlebot:password@localhost:5432/waddlebot
REDIS_URL=redis://localhost:6379/0
```

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| DATABASE_URL | postgresql://waddlebot:password@localhost:5432/waddlebot | Yes | PostgreSQL connection string |
| REDIS_URL | redis://localhost:6379/0 | Yes | Redis connection string |

**Format:** Standard URI format
- PostgreSQL: `postgresql://user:password@host:port/database`
- Redis: `redis://[:password]@host:port/database`

### Core API Configuration

```bash
CORE_API_URL=http://router-service:8000
ROUTER_API_URL=http://router-service:8000/api/v1/router
HUB_API_URL=http://hub-module:8060
```

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| CORE_API_URL | http://router-service:8000 | No | Core router service URL |
| ROUTER_API_URL | http://router-service:8000/api/v1/router | No | Router API endpoint |
| HUB_API_URL | http://hub-module:8060 | No | Hub module endpoint |

### AI Provider Configuration

```bash
AI_PROVIDER=ollama
```

| Variable | Default | Valid Values | Description |
|----------|---------|--------------|-------------|
| AI_PROVIDER | ollama | ollama, waddleai | AI provider selection |

#### Ollama Configuration (Direct Connection)

Use when `AI_PROVIDER=ollama`:

```bash
OLLAMA_HOST=localhost
OLLAMA_PORT=11434
OLLAMA_MODEL=tinyllama
OLLAMA_TEMPERATURE=0.7
OLLAMA_MAX_TOKENS=2000
OLLAMA_TIMEOUT=60
OLLAMA_USE_TLS=false
OLLAMA_VERIFY_SSL=true
OLLAMA_CERT_PATH=/path/to/cert.pem
OLLAMA_SUPPORTS_JSON=false
OLLAMA_DISABLE_THINKING=true
```

| Variable | Default | Type | Range | Description |
|----------|---------|------|-------|-------------|
| OLLAMA_HOST | localhost | string | - | Ollama server hostname |
| OLLAMA_PORT | 11434 | int | 1-65535 | Ollama server port |
| OLLAMA_MODEL | tinyllama | string | - | Model name |
| OLLAMA_TEMPERATURE | 0.7 | float | 0-2 | Response randomness |
| OLLAMA_MAX_TOKENS | 2000 | int | 1+ | Max response tokens |
| OLLAMA_TIMEOUT | 60 | int | 1+ | Request timeout (seconds) |
| OLLAMA_USE_TLS | false | bool | - | Use TLS for connection |
| OLLAMA_VERIFY_SSL | true | bool | - | Verify SSL certificate |
| OLLAMA_CERT_PATH | empty | string | - | Path to SSL certificate |
| OLLAMA_SUPPORTS_JSON | false | bool | - | Declare that `OLLAMA_MODEL` handles JSON/structured output. `false` = **text-only path**: no `format` is ever sent and summaries use plain labeled-text prompts. Set `true` only for a JSON-capable model (e.g. gemma4:e4b+) |
| OLLAMA_DISABLE_THINKING | true | bool | - | Send `think: false` so reasoning models (e.g. gemma4) answer directly instead of spending the token budget on hidden reasoning and returning an empty reply |

Capability flags describe whatever model `OLLAMA_MODEL` names; there is no built-in model-to-capability
map. Booleans are parsed strictly (`true/false/1/0/yes/no/on/off`) - a typo fails at startup. An empty
completion is a hard error (`EmptyCompletionError`), never a blank success. See
`docs/testing/ollama-realpath.md` for the env-gated real-Ollama test suite.

**Model Recommendations:**
- **Fast:** tinyllama, mistral
- **Balanced:** llama2, neural-chat
- **High Quality:** llama2-13b, openchat

#### WaddleAI Configuration (Cloud Proxy)

Use when `AI_PROVIDER=waddleai`:

```bash
WADDLEAI_BASE_URL=http://waddleai-proxy:8000
WADDLEAI_API_KEY=wa-your-api-key-here
WADDLEAI_MODEL=auto
WADDLEAI_TEMPERATURE=0.7
WADDLEAI_MAX_TOKENS=2000
WADDLEAI_TIMEOUT=60
WADDLEAI_PREFERRED_MODEL=gpt-4
```

| Variable | Default | Type | Description |
|----------|---------|------|-------------|
| WADDLEAI_BASE_URL | http://waddleai-proxy:8000 | string | WaddleAI proxy URL |
| WADDLEAI_API_KEY | empty | string | API key (must start with wa-) |
| WADDLEAI_MODEL | auto | string | Model selection (auto, gpt-4, claude, etc.) |
| WADDLEAI_TEMPERATURE | 0.7 | float | Response randomness (0-2) |
| WADDLEAI_MAX_TOKENS | 2000 | int | Max response tokens |
| WADDLEAI_TIMEOUT | 60 | int | Request timeout (seconds) |
| WADDLEAI_PREFERRED_MODEL | empty | string | Preferred model override |

**API Key Format:**
- Must start with `wa-` (WaddleAI authentication)
- Keep secure — never commit to version control

### Vector Store Configuration (mem0 + Qdrant)

```bash
MEM0_VECTOR_STORE=qdrant
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=ai_researcher_memory
MEM0_EMBEDDER_PROVIDER=ollama
MEM0_EMBEDDER_MODEL=nomic-embed-text
VECTOR_SEARCH_LIMIT=10
VECTOR_SCORE_THRESHOLD=0.7
```

| Variable | Default | Type | Description |
|----------|---------|------|-------------|
| MEM0_VECTOR_STORE | qdrant | string | Vector store type (qdrant only) |
| QDRANT_URL | http://localhost:6333 | string | Qdrant server URL |
| QDRANT_API_KEY | empty | string | Qdrant API key (if required) |
| QDRANT_COLLECTION | ai_researcher_memory | string | Collection name |
| MEM0_EMBEDDER_PROVIDER | ollama | string | Embedding model provider |
| MEM0_EMBEDDER_MODEL | nomic-embed-text | string | Embedding model name |
| VECTOR_SEARCH_LIMIT | 10 | int | Max search results |
| VECTOR_SCORE_THRESHOLD | 0.7 | float | Min similarity score (0-1) |

**Embedding Model Recommendations:**
- **Fast & Good:** nomic-embed-text, all-minilm
- **High Quality:** all-mpnet-base-v2, e5-base

### Rate Limiting Configuration

```bash
RATE_LIMIT_DEFAULT=60
RATE_LIMIT_RESEARCH=30
RATE_LIMIT_MEMORY=100
GLOBAL_RATE_LIMIT_RESEARCH=500
GLOBAL_RATE_LIMIT_MEMORY=1000
```

| Variable | Default | Type | Unit | Description |
|----------|---------|------|------|-------------|
| RATE_LIMIT_DEFAULT | 60 | int | req/min | Default per-user limit |
| RATE_LIMIT_RESEARCH | 30 | int | req/min | Research operations per user |
| RATE_LIMIT_MEMORY | 100 | int | req/min | Memory operations per user |
| GLOBAL_RATE_LIMIT_RESEARCH | 500 | int | req/min | Research global limit |
| GLOBAL_RATE_LIMIT_MEMORY | 1000 | int | req/min | Memory global limit |

**Tuning Guide:**
- **Low traffic:** Use defaults
- **High traffic:** Increase global limits 2-3x
- **Premium communities:** 2-5x higher per-user limits

### Batch Processing Configuration

```bash
CONTEXT_BATCH_SIZE=1000
CONTEXT_BATCH_INTERVAL=60
ENABLE_BATCH_PROCESSING=true
BATCH_WORKER_THREADS=5
```

| Variable | Default | Type | Description |
|----------|---------|------|-------------|
| CONTEXT_BATCH_SIZE | 1000 | int | Messages per batch |
| CONTEXT_BATCH_INTERVAL | 60 | int | Batch interval (seconds) |
| ENABLE_BATCH_PROCESSING | true | bool | Enable batch processing |
| BATCH_WORKER_THREADS | 5 | int | Worker threads for batching |

**Note:** Batch processing automatically:
- Indexes messages in mem0
- Extracts topics and entities
- Updates community context window

### Cache Configuration

```bash
CACHE_TTL_RESEARCH=3600
CACHE_TTL_CONTEXT=600
CACHE_TTL_MEMORY=1800
ENABLE_SEMANTIC_CACHE=true
SEMANTIC_CACHE_THRESHOLD=0.95
```

| Variable | Default | Type | Unit | Description |
|----------|---------|------|------|-------------|
| CACHE_TTL_RESEARCH | 3600 | int | seconds | Research cache expiry |
| CACHE_TTL_CONTEXT | 600 | int | seconds | Context cache expiry |
| CACHE_TTL_MEMORY | 1800 | int | seconds | Memory cache expiry |
| ENABLE_SEMANTIC_CACHE | true | bool | - | Enable similarity matching |
| SEMANTIC_CACHE_THRESHOLD | 0.95 | float | 0-1 | Similarity threshold |

**Cache TTL Tuning:**
- **Fast changing data:** Lower TTL (300-600s)
- **Stable data:** Higher TTL (1800-3600s)

### LLM Concurrency Configuration

```bash
MAX_CONCURRENT_LLM_CALLS=10
LLM_QUEUE_SIZE=100
LLM_REQUEST_TIMEOUT=60
LLM_MAX_RETRIES=3
LLM_RETRY_DELAY=2
```

| Variable | Default | Type | Description |
|----------|---------|------|-------------|
| MAX_CONCURRENT_LLM_CALLS | 10 | int | Concurrent LLM requests |
| LLM_QUEUE_SIZE | 100 | int | Pending request queue size |
| LLM_REQUEST_TIMEOUT | 60 | int | Request timeout (seconds) |
| LLM_MAX_RETRIES | 3 | int | Retry attempts |
| LLM_RETRY_DELAY | 2 | int | Retry backoff (seconds) |

**Tuning for Provider Limits:**
- **Ollama:** Increase MAX_CONCURRENT (local resource available)
- **WaddleAI:** Keep lower (respect API limits)

### Research Configuration

```bash
RESEARCH_SYSTEM_PROMPT=You are an AI research assistant...
RESEARCH_MAX_CONTEXT_MESSAGES=100
RESEARCH_RESPONSE_FORMAT=markdown
```

| Variable | Default | Type | Description |
|----------|---------|------|-------------|
| RESEARCH_SYSTEM_PROMPT | [see default] | string | LLM system prompt |
| RESEARCH_MAX_CONTEXT_MESSAGES | 100 | int | Messages in context window |
| RESEARCH_RESPONSE_FORMAT | markdown | string | Response format |

### Memory Configuration

```bash
MEMORY_RETENTION_DAYS=90
MEMORY_AUTO_PRUNE=true
MEMORY_INDEX_INTERVAL=300
MEMORY_DEDUP_THRESHOLD=0.90
```

| Variable | Default | Type | Description |
|----------|---------|------|-------------|
| MEMORY_RETENTION_DAYS | 90 | int | Keep memories (days) |
| MEMORY_AUTO_PRUNE | true | bool | Auto-delete old memories |
| MEMORY_INDEX_INTERVAL | 300 | int | Re-index interval (seconds) |
| MEMORY_DEDUP_THRESHOLD | 0.90 | float | Dedup similarity (0-1) |

### Logging Configuration

```bash
LOG_LEVEL=INFO
LOG_DIR=/var/log/waddlebotlog
ENABLE_SYSLOG=false
ENABLE_AAA_LOGGING=true
```

| Variable | Default | Type | Valid Values | Description |
|----------|---------|------|--------------|-------------|
| LOG_LEVEL | INFO | string | DEBUG, INFO, WARNING, ERROR | Log level |
| LOG_DIR | /var/log/waddlebotlog | string | - | Log directory |
| ENABLE_SYSLOG | false | bool | - | Send logs to syslog |
| ENABLE_AAA_LOGGING | true | bool | - | Enable audit logging |

### Security Configuration

```bash
SECRET_KEY=change-me-in-production
SERVICE_API_KEY=your-service-api-key
VALID_API_KEYS=key1,key2,key3
```

| Variable | Default | Type | Description |
|----------|---------|------|-------------|
| SECRET_KEY | change-me-in-production | string | JWT secret key |
| SERVICE_API_KEY | empty | string | Service-to-service key |
| VALID_API_KEYS | empty | string | Comma-separated API keys |

**Security Best Practices:**
- **Never commit secrets** to version control
- **Use environment variables** for secrets
- **Rotate keys regularly** in production
- **Use strong keys** (minimum 32 characters)

### Performance Configuration

```bash
THREAD_POOL_WORKERS=20
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=40
REQUEST_TIMEOUT=30
```

| Variable | Default | Type | Description |
|----------|---------|------|-------------|
| THREAD_POOL_WORKERS | 20 | int | Thread pool size |
| DB_POOL_SIZE | 20 | int | Database connection pool |
| DB_MAX_OVERFLOW | 40 | int | Overflow connections |
| REQUEST_TIMEOUT | 30 | int | Request timeout (seconds) |

**Tuning for Load:**
- **Low:** Keep defaults
- **Medium:** 2x defaults
- **High:** 3-4x defaults

## Example .env Files

### Development (Local)

```bash
# Module
MODULE_NAME=ai_researcher_module
MODULE_VERSION=1.0.0
MODULE_PORT=8070

# Database
DATABASE_URL=postgresql://waddlebot:password@localhost:5432/waddlebot
REDIS_URL=redis://localhost:6379/0

# AI Provider
AI_PROVIDER=ollama
OLLAMA_HOST=localhost
OLLAMA_PORT=11434
OLLAMA_MODEL=tinyllama
OLLAMA_TEMPERATURE=0.7
OLLAMA_MAX_TOKENS=2000

# Vector Store
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=ai_researcher_memory

# Rate Limiting
RATE_LIMIT_RESEARCH=30
RATE_LIMIT_MEMORY=100

# Logging
LOG_LEVEL=DEBUG
ENABLE_AAA_LOGGING=true

# Security
SERVICE_API_KEY=dev-service-key
```

### Production (WaddleAI)

```bash
# Module
MODULE_NAME=ai_researcher_module
MODULE_VERSION=1.0.0
MODULE_PORT=8070

# Database (RDS or managed)
DATABASE_URL=postgresql://user:securepass@prod-db.rds.amazonaws.com:5432/waddlebot
REDIS_URL=redis://:password@prod-redis.elasticache.amazonaws.com:6379/0

# AI Provider
AI_PROVIDER=waddleai
WADDLEAI_BASE_URL=http://waddleai-proxy:8000
WADDLEAI_API_KEY=wa-your-production-key
WADDLEAI_PREFERRED_MODEL=gpt-4
WADDLEAI_TEMPERATURE=0.7
WADDLEAI_MAX_TOKENS=2000

# Vector Store
QDRANT_URL=http://qdrant-prod:6333
QDRANT_API_KEY=your-api-key
QDRANT_COLLECTION=ai_researcher_memory

# Rate Limiting
RATE_LIMIT_RESEARCH=50
RATE_LIMIT_MEMORY=200
GLOBAL_RATE_LIMIT_RESEARCH=1000
GLOBAL_RATE_LIMIT_MEMORY=2000

# Performance
MAX_CONCURRENT_LLM_CALLS=20
DB_POOL_SIZE=40
BATCH_WORKER_THREADS=10

# Logging
LOG_LEVEL=INFO
ENABLE_AAA_LOGGING=true
LOG_DIR=/var/log/waddlebotlog

# Security
SECRET_KEY=your-production-secret-key-32-chars-min
SERVICE_API_KEY=your-production-service-key
```

### High Volume

```bash
# Module (same as production)
...

# Rate Limiting - High
RATE_LIMIT_RESEARCH=100
RATE_LIMIT_MEMORY=300
GLOBAL_RATE_LIMIT_RESEARCH=2000
GLOBAL_RATE_LIMIT_MEMORY=5000

# Performance - High
MAX_CONCURRENT_LLM_CALLS=30
LLM_QUEUE_SIZE=200
DB_POOL_SIZE=60
DB_MAX_OVERFLOW=120
BATCH_WORKER_THREADS=20
THREAD_POOL_WORKERS=40

# Caching
CACHE_TTL_RESEARCH=1800
CACHE_TTL_CONTEXT=900
ENABLE_SEMANTIC_CACHE=true
SEMANTIC_CACHE_THRESHOLD=0.85
```

## Configuration Validation

On startup, the module validates configuration:

```bash
# Validate configuration
python -c "from config import Config; Config.validate()"
```

**Validation Checks:**
- AI_PROVIDER in ['ollama', 'waddleai']
- OLLAMA_HOST/PORT set for Ollama provider
- WADDLEAI_BASE_URL/API_KEY set for WaddleAI provider
- Numeric ranges (temperature, thresholds, etc.)
- WADDLEAI_API_KEY starts with 'wa-'
- Batch size and intervals positive
- Concurrency settings positive

**Common Validation Errors:**
```
Configuration errors: Invalid AI_PROVIDER: unknown
Configuration errors: OLLAMA_TEMPERATURE must be between 0 and 2
Configuration errors: WADDLEAI_API_KEY must start with 'wa-'
```

## Feature Flags (via Config)

Enable/disable features by checking Config class:

```python
if Config.BOT_DETECTION_ENABLED:
    # Bot detection is enabled

if Config.FIREHOSE_ENABLED:
    # Message ingestion is enabled

if Config.ENABLE_BATCH_PROCESSING:
    # Batch processing is enabled

if Config.ENABLE_SEMANTIC_CACHE:
    # Semantic caching is enabled
```

## Dynamic Configuration

Per-community configuration is stored in database:

```python
# Get community config
query = "SELECT * FROM ai_researcher_config WHERE community_id = $1"
config = await dal.execute(query, [community_id])

# Override module defaults
firehose_enabled = config['firehose_enabled']  # per-community
bot_detection_enabled = config['bot_detection_enabled']
```

## Configuration Hierarchy

```
Environment Variables (highest priority)
    ↓
.env file (project root)
    ↓
Config class defaults
    ↓
Database per-community settings (lowest priority)
```

Settings are evaluated in order, with higher priority values overriding lower priority.
