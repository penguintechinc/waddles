# Waddles Security Documentation

## Security Ethos

Waddles is built on a foundation of security-first principles, recognizing that as a critical infrastructure component for community management and chat automation, security is not an optional feature but a core requirement. Our security ethos emphasizes:

- **Defense in Depth**: Multiple layers of security controls across authentication, authorization, network, and application layers
- **Zero Trust Architecture**: Every request is authenticated and authorized, regardless of origin
- **Fail Secure**: Systems fail closed (deny access) rather than open when security controls fail
- **Transparency**: Open security policies, responsible disclosure, and clear communication
- **Continuous Improvement**: Regular security reviews, automated scanning, and proactive threat hunting
- **Compliance Ready**: Standards-based approach aligned with OWASP, NIST, and industry best practices

### Threat Model Overview

Waddles's threat model addresses risks across multiple attack vectors:

**1. External Threats**
- **Unauthorized API Access**: Mitigated through API key authentication, JWT tokens, and rate limiting
- **DDoS Attacks**: Protected via per-service rate limiting and Kubernetes Ingress-level controls
- **SQL Injection**: Prevented through parameterized queries and ORM usage
- **XSS/CSRF**: Frontend sanitization, CSP headers, and CSRF tokens
- **Webhook Spoofing**: HMAC-SHA256 signature verification for all webhooks

**2. Internal Threats**
- **Privilege Escalation**: RBAC with least-privilege enforcement
- **Data Exfiltration**: Community isolation, audit logging, and access controls
- **Malicious Modules**: Sandboxed execution, permission scoping, and code review
- **Credential Compromise**: Automatic rotation, secure storage, and multi-factor authentication

**3. Supply Chain Threats**
- **Compromised Dependencies**: Automated scanning with Dependabot, pip-audit, and Safety
- **Malicious Container Images**: Image signing, scanning with Trivy, and trusted base images
- **Third-Party API Abuse**: Webhook signature verification, OAuth token validation, and rate limiting

---

## Authentication

Waddles implements multiple authentication mechanisms optimized for different use cases.

### 1. API Key Authentication

**Purpose**: Service-to-service authentication and programmatic access

**Implementation**:
```python
from flask_core.auth import create_api_key, verify_api_key_async

# Generate new API key
api_key = create_api_key(prefix="wa", length=64)
# Returns: wa-<64-character-hex>

# Store hashed version in database
hashed = hash_api_key(api_key)
await dal.api_keys.insert(
    user_id=user.id,
    key_hash=hashed,
    name="Production API Key",
    permissions=["action:execute", "trigger:webhook"],
    expires_at=datetime.utcnow() + timedelta(days=365)
)
```

**Usage in Requests**:
```http
POST /api/v1/action/execute
X-API-Key: wa-abc123def456...
Content-Type: application/json

{
  "action_id": "send_message",
  "params": {...}
}
```

**Security Features**:
- SHA-256 hashing for storage (never store plaintext)
- Constant-time comparison to prevent timing attacks
- Automatic expiration and rotation
- Per-key permission scoping
- Last-used tracking for audit purposes

**API Key Roles**:
- `trigger`: Can receive webhooks and events
- `action`: Can execute actions
- `core`: Can access core services
- `admin`: Full administrative access
- `user`: Limited user-level access

### 2. JWT Token Authentication

**Purpose**: User session management and browser-based authentication

**Token Structure** (header carries `alg: HS256` and `kid`; every claim in the first row is
mandatory and a token missing any of them is rejected):
```json
{
  "sub": "12345",
  "iss": "waddlebot",
  "aud": "waddlebot-services",
  "iat": 1702310400,
  "exp": 1702396800,
  "scope": "community:read tenant:read",
  "tenant": "global",
  "teams": [],
  "username": "streamername",
  "email": "user@example.com",
  "roles": ["viewer"],
  "type": "access"
}
```

**Token Generation**:
```python
from flask_core.auth import create_jwt_token

token = create_jwt_token(
    user_id="user_12345",
    username="streamername",
    email="user@example.com",
    roles=["community_owner", "moderator"],
    secret_key=os.getenv('JWT_SECRET'),
    tenant='global',            # mandatory -- there is no untenanted token
    scope='community:read',
    expiration_hours=24
)
```

**Token Verification**:
```python
from flask_core.auth import verify_jwt_token

payload = verify_jwt_token(token, secret_key=os.getenv('JWT_SECRET'))
if payload:
    user_id = payload['sub']
    roles = payload['roles']
else:
    # Token invalid or expired
    return error_response(401, 'invalid_token')
```

**Security Features**:
- HMAC-SHA256 signing algorithm, pinned: one algorithm per verifier, `alg: none` and every other
  algorithm rejected, `jku`/`jwk`/`x5u`/`x5c`/`crit` headers rejected (RFC 8725) -- see
  [JWT Verification](JWT_VERIFICATION.md)
- `iss`, `aud`, `sub`, `iat`, `exp`, `scope` and `tenant` enforced on every verification; no
  default-tenant fallback
- Short expiration windows (1-24 hours)
- Refresh token rotation with grace periods
- Automatic revocation on logout
- Role claims for authorization decisions

**Token Lifecycle**:
1. **Login**: User authenticates → JWT issued with 24h expiration
2. **Refresh**: Before expiration → New token issued, old token grace period (7 days)
3. **Logout**: Token added to blacklist (Redis TTL = remaining token lifetime)
4. **Rotation**: JWT signing keys rotated annually with 7-day grace period

### 3. OAuth 2.0 Flows

**Supported Providers**: Twitch, Discord, Slack, YouTube

**Authorization Code Flow** (Primary):
```
1. User → Authorization Request → Provider
2. Provider → Authorization Grant → Callback URL
3. App → Exchange Code for Token → Provider
4. Provider → Access Token + Refresh Token → App
5. App → Create Session + JWT → User
```

**Implementation Example** (Twitch):
```python
from authlib.integrations.flask_client import OAuth

oauth = OAuth(app)

oauth.register(
    name='twitch',
    client_id=os.getenv('TWITCH_CLIENT_ID'),
    client_secret=os.getenv('TWITCH_CLIENT_SECRET'),
    authorize_url='https://id.twitch.tv/oauth2/authorize',
    access_token_url='https://id.twitch.tv/oauth2/token',
    userinfo_endpoint='https://api.twitch.tv/helix/users',
    client_kwargs={'scope': 'user:read:email'}
)

@app.route('/auth/twitch/login')
async def twitch_login():
    redirect_uri = url_for('twitch_callback', _external=True)
    return await oauth.twitch.authorize_redirect(redirect_uri)

@app.route('/auth/twitch/callback')
async def twitch_callback():
    token = await oauth.twitch.authorize_access_token()
    userinfo = await oauth.twitch.userinfo(token=token)

    # Create or update user
    user = await dal.auth_user.insert_or_update(
        email=userinfo['email'],
        username=userinfo['login'],
        primary_platform='twitch'
    )

    # Issue JWT for session
    jwt_token = create_jwt_token(
        user_id=user.id,
        username=user.username,
        email=user.email,
        roles=await get_user_roles(user.id)
    )

    return {'token': jwt_token}
```

**OAuth Token Storage**:
- Access tokens: Encrypted in Redis (1-hour TTL)
- Refresh tokens: Encrypted in PostgreSQL with expiration tracking
- Automatic refresh before expiration
- Revocation on user logout or security events

---

## Authorization (RBAC)

Waddles implements Role-Based Access Control (RBAC) for fine-grained permission management.

### Role Hierarchy

```
admin (*)
  └─ community_owner (community:*, module:install, module:configure)
      └─ moderator (community:moderate, user:manage)
          └─ user (profile:view, profile:edit)
```

**Default Roles**:

| Role | Permissions | Description |
|------|-------------|-------------|
| `admin` | `*` | Full system access (superuser) |
| `community_owner` | `community:*`, `module:install`, `module:configure` | Owns and manages communities |
| `moderator` | `community:moderate`, `user:manage` | Community moderation |
| `user` | `profile:view`, `profile:edit` | Standard user access |

### Permission Format

Permissions use hierarchical colon-separated format:

```
resource:action
```

**Examples**:
- `community:*` - All community operations
- `community:moderate` - Moderate community content
- `module:install` - Install modules
- `workflow:execute` - Execute workflows
- `*` - All permissions (admin only)

### Role Assignment

```python
# Assign role to user
await dal.auth_user_roles.insert(
    user_id=user.id,
    role_id=role.id,
    assigned_by=admin_user.id,
    assigned_at=datetime.utcnow()
)

# Get user roles
user_roles = await dal.select_async(
    (dal.auth_user_roles.user_id == user.id),
    join=[dal.auth_role.on(dal.auth_role.id == dal.auth_user_roles.role_id)]
)
```

### Permission Checking

```python
def has_permission(user_roles, required_permission):
    """Check if user has required permission"""
    for role in user_roles:
        permissions = role.permissions or []

        # Admin wildcard
        if '*' in permissions:
            return True

        # Exact match
        if required_permission in permissions:
            return True

        # Wildcard match (e.g., community:* matches community:moderate)
        resource, action = required_permission.split(':')
        if f"{resource}:*" in permissions:
            return True

    return False
```

### Community Isolation

Every request must include community context to enforce multi-tenancy:

```python
@app.route('/api/v1/users', methods=['GET'])
@require_api_key(role='admin')
async def list_users():
    community_id = request.headers.get('X-Community-ID')
    if not community_id:
        return error_response(400, 'missing_community_id')

    # All queries filtered by community
    users = await dal.select_async(
        dal.auth_user.community_id == community_id
    )

    return {'users': [u.as_dict() for u in users]}
```

---

## Webhook Signature Verification

All incoming webhooks MUST be verified using HMAC-SHA256 signatures.

### Implementation

```python
import hmac
import hashlib
from functools import wraps

def verify_webhook_signature(payload_bytes, signature, secret):
    """
    Verify HMAC-SHA256 webhook signature.

    Args:
        payload_bytes: Raw request body (bytes)
        signature: Provided signature from header
        secret: Webhook secret for HMAC

    Returns:
        True if signature valid, False otherwise
    """
    expected = hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    # Use constant-time comparison
    return hmac.compare_digest(signature, expected)

def require_webhook_signature(secret_key_name):
    """Decorator to require valid webhook signature"""
    def decorator(f):
        @wraps(f)
        async def wrapper(*args, **kwargs):
            # Get signature from header
            signature = request.headers.get('X-Webhook-Signature')
            if not signature:
                logger.warning("Webhook signature missing")
                return error_response(401, 'missing_signature')

            # Get raw payload
            payload = await request.get_data()

            # Get secret from config
            secret = os.getenv(secret_key_name)
            if not secret:
                logger.error(f"Webhook secret not configured: {secret_key_name}")
                return error_response(500, 'configuration_error')

            # Verify signature
            if not verify_webhook_signature(payload, signature, secret):
                logger.warning("Invalid webhook signature")
                return error_response(401, 'invalid_signature')

            return await f(*args, **kwargs)
        return wrapper
    return decorator
```

### Platform-Specific Verification

**Twitch EventSub**:
```python
@app.route('/webhook/twitch', methods=['POST'])
async def twitch_webhook():
    # Twitch signature format: sha256=<hex>
    signature = request.headers.get('Twitch-Eventsub-Message-Signature', '')
    message_id = request.headers.get('Twitch-Eventsub-Message-Id')
    timestamp = request.headers.get('Twitch-Eventsub-Message-Timestamp')

    # Build HMAC message
    payload = await request.get_data()
    message = message_id.encode() + timestamp.encode() + payload

    # Verify
    expected = 'sha256=' + hmac.new(
        TWITCH_SECRET.encode(),
        message,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return error_response(401, 'invalid_signature')

    return await process_twitch_event(payload)
```

**Discord Interactions**:
```python
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

@app.route('/webhook/discord', methods=['POST'])
async def discord_webhook():
    signature = request.headers.get('X-Signature-Ed25519')
    timestamp = request.headers.get('X-Signature-Timestamp')
    payload = await request.get_data()

    verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))

    try:
        verify_key.verify(
            timestamp.encode() + payload,
            bytes.fromhex(signature)
        )
    except BadSignatureError:
        return error_response(401, 'invalid_signature')

    return await process_discord_interaction(payload)
```

---

## Rate Limiting

Waddles uses distributed rate limiting with Redis for accurate enforcement across all instances.

### Sliding Window Algorithm

```python
from flask_core.rate_limiter import RateLimiter

# Initialize rate limiter
rate_limiter = RateLimiter(
    redis_url=os.getenv('REDIS_URL'),
    namespace='waddlebot'
)
await rate_limiter.connect()

# Check rate limit
allowed = await rate_limiter.check_rate_limit(
    identifier=f"user:{user_id}",
    limit=100,          # 100 requests
    window=60           # per 60 seconds
)

if not allowed:
    return error_response(429, 'rate_limit_exceeded')
```

### Rate Limit Policies

**User-Level Limits**:
```python
# Standard users: 100 requests/minute
await rate_limiter.check_rate_limit(f"user:{user_id}", limit=100, window=60)

# Premium users: 500 requests/minute
await rate_limiter.check_rate_limit(f"user:{user_id}", limit=500, window=60)

# Admin users: 1000 requests/minute
await rate_limiter.check_rate_limit(f"user:{user_id}", limit=1000, window=60)
```

**Command-Level Limits**:
```python
# Per-command rate limiting
command_key = f"command:{user_id}:{command_name}"
await rate_limiter.check_rate_limit(command_key, limit=10, window=60)
```

**IP-Level Limits** (Anti-DDoS):
```python
# Global IP rate limit
ip_address = request.remote_addr
await rate_limiter.check_rate_limit(f"ip:{ip_address}", limit=1000, window=60)
```

### Decorator Usage

```python
@app.route('/api/v1/ai/chat', methods=['POST'])
@rate_limiter.limit(
    identifier_func=lambda: f"user:{request.user_id}",
    limit=10,
    window=60,
    on_limit_exceeded=lambda: error_response(429, 'rate_limit_exceeded')
)
async def ai_chat():
    return await process_ai_chat()
```

### Rate Limit Headers

All responses include rate limit information:

```http
HTTP/1.1 200 OK
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 73
X-RateLimit-Reset: 1702310460
```

---

## Security Best Practices

### 1. Container Security

**Run as Non-Root User**:
```dockerfile
FROM python:3.13-slim

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "main.py"]
```

**Read-Only Root Filesystem**:
```yaml
securityContext:
  readOnlyRootFilesystem: true
  runAsNonRoot: true
  runAsUser: 1000
  capabilities:
    drop:
    - ALL
```

**Regular Image Scanning**:
```bash
# Scan with Trivy
trivy image ghcr.io/penguintechinc/waddlebot/router:latest

# No latest tags - use specific versions
FROM python:3.13.1-slim  # Good
FROM python:latest       # Bad
```

### 2. Database Security

**Connection Encryption**:
```python
db_config = {
    'host': 'postgres.cluster.local',
    'port': 5432,
    'database': 'waddlebot',
    'user': 'waddlebot_app',
    'password': os.getenv('DB_PASSWORD'),
    'sslmode': 'require',           # Require TLS
    'sslrootcert': '/certs/ca.crt'  # Verify server cert
}
```

**Least Privilege Database Roles**:
```sql
-- Application role (read/write on app tables only)
CREATE ROLE waddlebot_app;
GRANT CONNECT ON DATABASE waddlebot TO waddlebot_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO waddlebot_app;
REVOKE CREATE ON SCHEMA public FROM waddlebot_app;

-- Read-only role (analytics)
CREATE ROLE waddlebot_readonly;
GRANT CONNECT ON DATABASE waddlebot TO waddlebot_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO waddlebot_readonly;
```

**Encryption at Rest**:
```bash
# PostgreSQL with pgcrypto
CREATE EXTENSION IF NOT EXISTS pgcrypto;

# Encrypt sensitive columns
UPDATE auth_user SET
    api_secret = pgp_sym_encrypt(api_secret, :'encryption_key');
```

### 3. Secrets Management

**Kubernetes Secrets**:
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: waddlebot-secrets
type: Opaque
data:
  db-password: <base64-encoded>
  jwt-secret: <base64-encoded>
  twitch-secret: <base64-encoded>
```

**Never Commit Secrets**:
```bash
# Use .gitignore
echo ".env" >> .gitignore
echo "*.secret" >> .gitignore
echo "credentials.json" >> .gitignore

# Use git-secrets to prevent accidental commits
git secrets --scan
```

**Rotation Schedule**:
- Database passwords: Every 90 days
- API keys (third-party): Every 180 days
- JWT signing keys: Every 365 days
- OAuth tokens: Auto-refresh (1 hour TTL)

### 4. Input Validation

**Validate All Inputs**:
```python
from flask_core.validation import validate_input, ValidationError

schema = {
    'user_id': {
        'type': 'string',
        'required': True,
        'pattern': r'^[a-z]+:\d+$'  # Format: platform:id
    },
    'command': {
        'type': 'string',
        'required': True,
        'max_length': 255,
        'pattern': r'^[a-zA-Z0-9_-]+$'  # Alphanumeric + underscore/hyphen
    },
    'message': {
        'type': 'string',
        'max_length': 2000,
        'sanitize': True  # Remove HTML/XSS
    }
}

try:
    data = await validate_input(await request.json(), schema)
except ValidationError as e:
    return error_response(400, str(e))
```

**Prevent SQL Injection**:
```python
# GOOD - Parameterized query
users = await dal.select_async(
    dal.auth_user.username == username
)

# BAD - String concatenation (NEVER DO THIS!)
query = f"SELECT * FROM auth_user WHERE username = '{username}'"
```

### 5. Audit Logging

**Log Security Events**:
```python
def log_security_event(event_type, user_id, action, result, **kwargs):
    logger.info(
        f'SECURITY event_type={event_type} user={user_id} action={action} result={result}',
        extra={
            'event_type': event_type,
            'user_id': user_id,
            'action': action,
            'result': result,
            'ip_address': request.remote_addr,
            'user_agent': request.headers.get('User-Agent'),
            'timestamp': datetime.utcnow().isoformat(),
            **kwargs
        }
    )
```

**Events to Log**:
- Authentication attempts (success/failure)
- Authorization denials
- API key usage
- Credential changes
- Administrative actions
- Suspicious activity (repeated failures, unusual patterns)

---

## Vulnerability Management

### Reporting Vulnerabilities

**Email**: security@waddlebot.io (preferred)
**GitHub**: Security Advisory feature for private disclosure

**Include in Report**:
- Detailed description of vulnerability
- Steps to reproduce
- Affected components/versions
- Potential impact assessment
- Your contact information

### Response Timeline

| Severity | CVSS Score | Acknowledgment | Fix Development | Public Disclosure |
|----------|-----------|----------------|-----------------|-------------------|
| Critical | 9.0-10.0 | < 1 hour | < 48 hours | < 7 days |
| High | 7.0-8.9 | < 4 hours | < 5 days | < 30 days |
| Medium | 4.0-6.9 | < 1 day | < 14 days | < 60 days |
| Low | 0.1-3.9 | < 2 days | < 30 days | Coordinated |

### Dependency Scanning

**Automated Tools** (CI/CD Pipeline):
```bash
# Python dependencies
pip-audit --skip-editable
safety check --json
bandit -r . -ll

# Container images
trivy image --severity HIGH,CRITICAL waddlebot/router:latest

# Secrets scanning
git secrets --scan
truffleHog --regex --entropy=True .
```

**Update Schedule**:
- Security patches: Immediate (within severity timeline)
- Minor/patch updates: Monthly (first Monday)
- Major updates: Quarterly (after thorough testing)

---

## Compliance & Standards

Waddles follows industry-standard security frameworks:

- **OWASP Top 10**: Mitigations implemented for all items
- **CWE Coverage**: Focus on CWE-89 (SQL Injection), CWE-79 (XSS), CWE-200 (Information Exposure), CWE-306 (Authentication), CWE-798 (Hardcoded Credentials)
- **NIST Cybersecurity Framework**: Risk assessment and management processes
- **RFC 9110**: security.txt compliance with security contact information

---

## Security Contact

**Primary Contact**: security@waddlebot.io

**Response SLA**:
- Critical vulnerabilities: < 1 hour
- High severity: < 4 hours
- Medium severity: < 1 business day
- General inquiries: < 2 business days

**Mailing List**: Subscribe to security-announce@waddlebot.io for security advisories

---

**Document Version**: 1.0
**Last Updated**: December 2025
**Next Review**: March 2026
**Owner**: Security Team
