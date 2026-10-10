# AI Interaction Module - Configuration Guide

## Overview

The AI Interaction Module supports two AI provider backends with comprehensive configuration options for optimal performance and customization.

---

## Provider Selection

### AI_PROVIDER

**Type:** String
**Default:** `waddleai`
**Options:** `ollama`, `waddleai`

Determines which AI backend to use:
- **ollama**: Direct connection to Ollama server
- **waddleai**: Centralized proxy with intelligent routing to multiple providers

```env
AI_PROVIDER=waddleai
```

---

## Ollama Direct Connection Configuration

Used when `AI_PROVIDER=ollama`

### OLLAMA_HOST
**Type:** String
**Default:** `localhost`

Hostname or IP address of the Ollama server.

```env
OLLAMA_HOST=192.168.1.100
```

### OLLAMA_PORT
**Type:** String
**Default:** `11434`

Port number for Ollama server.

```env
OLLAMA_PORT=11434
```

### OLLAMA_USE_TLS
**Type:** Boolean
**Default:** `false`

Enable TLS/SSL encryption for Ollama connection.

```env
OLLAMA_USE_TLS=true
```

### OLLAMA_MODEL
**Type:** String
**Default:** `llama3.2`

Ollama model identifier.

**Available Models:**
- llama3.2
- llama3.1
- mistral
- codellama
- phi3

```env
OLLAMA_MODEL=llama3.2
```

### OLLAMA_TEMPERATURE
**Type:** Float
**Default:** `0.7`
**Range:** 0.0-2.0

Controls randomness in responses. Lower = more deterministic.

```env
OLLAMA_TEMPERATURE=0.8
```

### OLLAMA_MAX_TOKENS
**Type:** Integer
**Default:** `500`
**Range:** 1-10000

Maximum tokens in generated responses.

```env
OLLAMA_MAX_TOKENS=300
```

### OLLAMA_TIMEOUT
**Type:** Integer
**Default:** `30`
**Unit:** Seconds

Request timeout for Ollama API calls.

```env
OLLAMA_TIMEOUT=45
```

### OLLAMA_DISABLE_THINKING
**Type:** Boolean
**Default:** `true`

Send `think: false` so reasoning models (e.g. gemma4) answer directly instead of spending the whole
`OLLAMA_MAX_TOKENS` budget on hidden reasoning and returning an empty reply. This module only ever asks
for plain-text chat replies (no JSON / structured-output params are sent), so it works with text-only
models. A blank reply is logged with its `done_reason` before the canned fallback is used.

```env
OLLAMA_DISABLE_THINKING=true
```

See `docs/testing/ollama-realpath.md` for the env-gated real-Ollama test suite.

### OLLAMA_CERT_PATH
**Type:** String
**Default:** Empty

Path to TLS certificate file for SSL verification.

```env
OLLAMA_CERT_PATH=/path/to/cert.pem
```

### OLLAMA_VERIFY_SSL
**Type:** Boolean
**Default:** `true`

Verify SSL certificates when using TLS.

```env
OLLAMA_VERIFY_SSL=true
```

---

## WaddleAI Proxy Configuration

Used when `AI_PROVIDER=waddleai`

### WADDLEAI_BASE_URL
**Type:** String
**Default:** `http://waddleai-proxy:8000`

Base URL of the WaddleAI proxy service.

```env
WADDLEAI_BASE_URL=https://waddleai.example.com
```

### WADDLEAI_API_KEY
**Type:** String
**Default:** Empty
**Format:** `wa-xxxxx`

WaddleAI API key for authentication. Must start with `wa-`.

```env
WADDLEAI_API_KEY=wa-abc123def456
```

### WADDLEAI_MODEL
**Type:** String
**Default:** `auto`

Model selection for WaddleAI routing.

**Options:**
- `auto`: Intelligent routing based on request
- `gpt-4`: Force GPT-4
- `gpt-3.5-turbo`: Force GPT-3.5
- `claude-3-sonnet`: Force Claude 3 Sonnet
- `claude-3-haiku`: Force Claude 3 Haiku
- `llama3.2`: Route to Llama

```env
WADDLEAI_MODEL=auto
```

### WADDLEAI_TEMPERATURE
**Type:** Float
**Default:** `0.7`
**Range:** 0.0-2.0

Temperature setting for WaddleAI requests.

```env
WADDLEAI_TEMPERATURE=0.75
```

### WADDLEAI_MAX_TOKENS
**Type:** Integer
**Default:** `500`
**Range:** 1-10000

Maximum tokens for WaddleAI responses.

```env
WADDLEAI_MAX_TOKENS=400
```

### WADDLEAI_TIMEOUT
**Type:** Integer
**Default:** `30`
**Unit:** Seconds

Request timeout for WaddleAI API calls.

```env
WADDLEAI_TIMEOUT=60
```

### WADDLEAI_PREFERRED_MODEL
**Type:** String
**Default:** Empty

Force a specific provider through WaddleAI.

```env
WADDLEAI_PREFERRED_MODEL=claude-3-sonnet
```

---

## Shared AI Configuration

### AI_MODEL
**Type:** String
**Default:** Provider-dependent

Override model selection regardless of provider.

```env
AI_MODEL=gpt-4
```

### AI_TEMPERATURE
**Type:** Float
**Default:** `0.7`
**Range:** 0.0-2.0

Global temperature setting.

```env
AI_TEMPERATURE=0.7
```

### AI_MAX_TOKENS
**Type:** Integer
**Default:** `500`

Global max tokens setting.

```env
AI_MAX_TOKENS=500
```

### SYSTEM_PROMPT
**Type:** String
**Default:** See below

Custom system prompt for AI personality.

**Default:**
```
You are a helpful chatbot assistant for a streaming community.
Provide friendly, concise, and helpful responses.
Keep responses under 200 characters.
```

```env
SYSTEM_PROMPT="You are Waddles, a friendly penguin assistant..."
```

---

## Interaction Configuration

### QUESTION_TRIGGERS
**Type:** Comma-separated list
**Default:** `?`

Symbols/words that trigger AI responses.

```env
QUESTION_TRIGGERS=?,!waddle,@bot
```

### RESPONSE_PREFIX
**Type:** String
**Default:** `🤖 `

Prefix added to all AI responses.

```env
RESPONSE_PREFIX="[AI] "
```

### RESPOND_TO_EVENTS
**Type:** Boolean
**Default:** `true`

Enable AI responses to platform events.

```env
RESPOND_TO_EVENTS=true
```

### EVENT_RESPONSE_TYPES
**Type:** Comma-separated list
**Default:** `subscription,follow,donation,cheer,raid,boost`

Event types that trigger AI responses.

```env
EVENT_RESPONSE_TYPES=subscription,follow,raid
```

---

## Context Configuration

### ENABLE_CHAT_CONTEXT
**Type:** Boolean
**Default:** `true`

Include previous messages for context-aware responses.

```env
ENABLE_CHAT_CONTEXT=true
```

### CONTEXT_HISTORY_LIMIT
**Type:** Integer
**Default:** `5`

Number of previous messages to include in context.

```env
CONTEXT_HISTORY_LIMIT=10
```

---

## Performance Configuration

### MAX_CONCURRENT_REQUESTS
**Type:** Integer
**Default:** `10`

Maximum concurrent AI requests.

```env
MAX_CONCURRENT_REQUESTS=20
```

### REQUEST_TIMEOUT
**Type:** Integer
**Default:** `30`
**Unit:** Seconds

Global request timeout.

```env
REQUEST_TIMEOUT=45
```

---

## Module Configuration

### MODULE_NAME
**Type:** String
**Default:** `ai_interaction_module`

Module identifier.

```env
MODULE_NAME=ai_interaction_module
```

### MODULE_VERSION
**Type:** String
**Default:** `2.0.0`

Module version.

```env
MODULE_VERSION=2.0.0
```

### MODULE_PORT
**Type:** Integer
**Default:** `8005`

HTTP server port.

```env
MODULE_PORT=8005
```

---

## Database Configuration

### DATABASE_URL
**Type:** String
**Default:** `postgresql://waddlebot:password@localhost:5432/waddlebot`

PostgreSQL connection string.

```env
DATABASE_URL=postgresql://user:pass@db-server:5432/waddlebot
```

---

## Service URLs

### CORE_API_URL
**Type:** String
**Default:** `http://router-service:8000`

Core router service URL.

```env
CORE_API_URL=http://router:8000
```

### ROUTER_API_URL
**Type:** String
**Default:** `http://router-service:8000/api/v1/router`

Router API endpoint for submitting responses.

```env
ROUTER_API_URL=http://router:8000/api/v1/router
```

---

## Logging Configuration

### LOG_LEVEL
**Type:** String
**Default:** `INFO`
**Options:** DEBUG, INFO, WARNING, ERROR, CRITICAL

Logging verbosity level.

```env
LOG_LEVEL=DEBUG
```

### LOG_DIR
**Type:** String
**Default:** `/var/log/waddlebotlog`

Directory for log files.

```env
LOG_DIR=/var/log/ai_module
```

### ENABLE_SYSLOG
**Type:** Boolean
**Default:** `false`

Enable syslog output.

```env
ENABLE_SYSLOG=true
```

---

## Security Configuration

### SECRET_KEY
**Type:** String
**Default:** `change-me-in-production`

Secret key for cryptographic operations.

```env
SECRET_KEY=your-secret-key-here
```

### VALID_API_KEYS
**Type:** Comma-separated list
**Default:** Empty

Allowed API keys for authentication.

```env
VALID_API_KEYS=key1,key2,key3
```

---

## Example Configurations

### Production with WaddleAI

```env
# Provider
AI_PROVIDER=waddleai
WADDLEAI_BASE_URL=https://waddleai.production.com
WADDLEAI_API_KEY=wa-prod-key-here
WADDLEAI_MODEL=auto
WADDLEAI_TEMPERATURE=0.7
WADDLEAI_MAX_TOKENS=500

# Module
MODULE_PORT=8005
LOG_LEVEL=INFO

# Security
SECRET_KEY=production-secret-key
VALID_API_KEYS=prod-key-1,prod-key-2

# Performance
MAX_CONCURRENT_REQUESTS=20
REQUEST_TIMEOUT=45
```

### Development with Ollama

```env
# Provider
AI_PROVIDER=ollama
OLLAMA_HOST=localhost
OLLAMA_PORT=11434
OLLAMA_MODEL=llama3.2
OLLAMA_TEMPERATURE=0.8
OLLAMA_MAX_TOKENS=300

# Module
MODULE_PORT=8005
LOG_LEVEL=DEBUG

# Development
ENABLE_CHAT_CONTEXT=true
CONTEXT_HISTORY_LIMIT=10
```

---

## Configuration Validation

The module validates configuration on startup. Common validation errors:

### Invalid Provider
```
Configuration errors: Invalid AI_PROVIDER: invalid. Must be 'ollama' or 'waddleai'
```

### Missing WaddleAI API Key
```
Configuration errors: WADDLEAI_API_KEY is required when AI_PROVIDER='waddleai'
```

### Invalid Temperature
```
Configuration errors: AI_TEMPERATURE must be between 0 and 2
```

---

## Best Practices

1. **Use WaddleAI in Production**: Better reliability and model flexibility
2. **Set Appropriate Timeouts**: Match your use case (streaming vs batch)
3. **Monitor Concurrent Requests**: Adjust based on load patterns
4. **Secure API Keys**: Never commit to version control
5. **Enable Context for Better Responses**: But limit history to avoid token overflow
6. **Test Configuration**: Use `/api/v1/ai/test` endpoint before deployment
7. **Set Response Prefix**: Help users identify AI responses
8. **Configure Event Responses**: Only enable events you want AI to respond to
