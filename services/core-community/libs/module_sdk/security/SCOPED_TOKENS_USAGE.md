# ScopedTokenService Usage Guide

## Overview

The `ScopedTokenService` provides OAuth-like permission management for WaddleBot modules. It enables secure, scope-based access control with JWT tokens.

## Features

- **JWT-based tokens** with cryptographic signing
- **Scope-based permissions** for fine-grained access control
- **Token expiration** with configurable lifetime
- **Token revocation** for security incidents
- **Community-module isolation** for multi-tenancy
- **Secure token storage** via SHA-256 hashing
- **Comprehensive validation** with detailed error handling

## Installation

The service is part of the module SDK. No additional dependencies are required beyond PyJWT (already included in WaddleBot).

```python
from libs.module_sdk.security import ScopedTokenService, create_scoped_token_service
```

## Quick Start

### Basic Usage

```python
from libs.module_sdk.security import create_scoped_token_service

# Create service instance
service = create_scoped_token_service(
    secret_key="your-secret-key-at-least-32-chars-long"
)

# Generate a token
token = service.generate_token(
    community_id="community_123",
    module_name="music_module",
    scopes=["read", "write"],
    expires_in_hours=24
)

# Validate token
token_data = service.validate_token(token)
if token_data:
    print(f"Valid token for {token_data['module_name']}")
    print(f"Scopes: {token_data['scopes']}")
else:
    print("Invalid or expired token")
```

### With Database Integration

```python
from libs.flask_core.flask_core.database import init_database
from libs.module_sdk.security import ScopedTokenService

# Initialize database
dal = init_database("postgresql://user:pass@localhost/waddlebot")

# Create service with database support
service = ScopedTokenService(
    secret_key="your-secret-key-at-least-32-chars-long",
    dal=dal
)

# Grant scopes (async version)
await service.grant_scope_async(
    community_id="community_123",
    module_name="music_module",
    scope="playlist:manage",
    granted_by_user_id="user_456"
)

# Get granted scopes
scopes = await service.get_granted_scopes_async(
    community_id="community_123",
    module_name="music_module"
)
print(f"Granted scopes: {scopes}")

# Revoke a scope
await service.revoke_scope_async(
    community_id="community_123",
    module_name="music_module",
    scope="playlist:manage"
)
```

## API Reference

### ScopedTokenService

#### Constructor

```python
ScopedTokenService(secret_key: str, algorithm: str = 'HS256', dal = None)
```

**Parameters:**
- `secret_key` (str): Secret key for JWT signing (minimum 32 characters)
- `algorithm` (str): JWT algorithm (default: 'HS256')
- `dal` (optional): AsyncDAL database instance for persistence

**Raises:**
- `ValueError`: If secret_key is too weak (< 32 characters)

#### generate_token()

```python
generate_token(
    community_id: str,
    module_name: str,
    scopes: List[str],
    expires_in_hours: int = 24,
    token_type: TokenType = TokenType.ACCESS,
    metadata: Optional[Dict[str, Any]] = None
) -> str
```

Generates a new scoped JWT token.

**Parameters:**
- `community_id`: Community identifier
- `module_name`: Module name
- `scopes`: List of permission scopes
- `expires_in_hours`: Token expiration (1-8760 hours, default: 24)
- `token_type`: Token type (ACCESS, REFRESH, SERVICE)
- `metadata`: Optional additional data

**Returns:** JWT token string

**Raises:**
- `ValueError`: If inputs are invalid

**Example:**
```python
token = service.generate_token(
    community_id="stream_community",
    module_name="chat_bot",
    scopes=["messages:read", "messages:write", "users:manage"],
    expires_in_hours=48,
    metadata={"bot_version": "2.0"}
)
```

#### validate_token()

```python
validate_token(token: str) -> Optional[Dict[str, Any]]
```

Validates and decodes a token.

**Parameters:**
- `token`: JWT token string to validate

**Returns:** Token payload dict if valid, None if invalid

**Token Payload:**
```python
{
    'community_id': 'community_123',
    'module_name': 'music_module',
    'scopes': ['read', 'write'],
    'iat': 1701234567,  # Issued at timestamp
    'exp': 1701320967,  # Expiration timestamp
    'type': 'access',
    'jti': 'unique_token_id',
    'metadata': {}
}
```

**Example:**
```python
payload = service.validate_token(token)
if payload:
    if 'write' in payload['scopes']:
        # Allow write operations
        pass
else:
    # Token invalid, expired, or revoked
    raise Unauthorized("Invalid token")
```

#### revoke_token()

```python
revoke_token(token: str) -> bool
```

Revokes a token to prevent further use.

**Parameters:**
- `token`: JWT token string to revoke

**Returns:** True if revoked successfully, False otherwise

**Example:**
```python
# Security incident - revoke all tokens
if security_incident:
    service.revoke_token(user_token)
```

#### grant_scope() / grant_scope_async()

```python
grant_scope(
    community_id: str,
    module_name: str,
    scope: str,
    granted_by_user_id: str
) -> bool
```

Grants a scope to a module in a community.

**Requires:** Database (dal) configuration

**Parameters:**
- `community_id`: Community identifier
- `module_name`: Module name
- `scope`: Scope to grant
- `granted_by_user_id`: User ID granting the scope (for audit)

**Returns:** True if granted successfully

**Example:**
```python
# Sync version
service.grant_scope(
    community_id="community_123",
    module_name="analytics_module",
    scope="analytics:read",
    granted_by_user_id="admin_user_789"
)

# Async version
await service.grant_scope_async(
    community_id="community_123",
    module_name="analytics_module",
    scope="analytics:write",
    granted_by_user_id="admin_user_789"
)
```

#### revoke_scope() / revoke_scope_async()

```python
revoke_scope(
    community_id: str,
    module_name: str,
    scope: str
) -> bool
```

Revokes a scope from a module.

**Requires:** Database (dal) configuration

**Example:**
```python
await service.revoke_scope_async(
    community_id="community_123",
    module_name="analytics_module",
    scope="analytics:write"
)
```

#### get_granted_scopes() / get_granted_scopes_async()

```python
get_granted_scopes(
    community_id: str,
    module_name: str
) -> List[str]
```

Retrieves all granted scopes for a module.

**Requires:** Database (dal) configuration

**Returns:** List of scope strings

**Example:**
```python
scopes = await service.get_granted_scopes_async(
    community_id="community_123",
    module_name="analytics_module"
)
# ['analytics:read', 'analytics:write', 'analytics:export']
```

## Scope Naming Conventions

Scopes should follow these patterns:

### Format
- `resource:action` - Standard pattern (e.g., `users:read`, `messages:write`)
- `namespace.resource:action` - Namespaced (e.g., `api.v1.users:read`)
- `resource-name` - Simple resource (e.g., `admin`, `moderator`)
- `*` - Wildcard (all permissions)

### Valid Characters
- Letters: `a-z`, `A-Z`
- Numbers: `0-9`
- Special: `:`, `.`, `-`, `_`

### Examples

```python
# Read/Write/Delete operations
scopes = ["messages:read", "messages:write", "messages:delete"]

# Hierarchical scopes
scopes = ["api.v1:read", "api.v2:read", "api.v2:write"]

# Admin scopes
scopes = ["admin", "moderator", "users:manage"]

# Wildcard (all permissions)
scopes = ["*"]
```

## Token Types

```python
from libs.module_sdk.security import TokenType

# Access token (default) - for API access
token = service.generate_token(..., token_type=TokenType.ACCESS)

# Service token - for service-to-service communication
token = service.generate_token(..., token_type=TokenType.SERVICE)

# Refresh token - for obtaining new access tokens
token = service.generate_token(..., token_type=TokenType.REFRESH)
```

## Security Best Practices

### 1. Secret Key Management

```python
import os

# DO: Load from environment
secret_key = os.environ.get('TOKEN_SECRET_KEY')
if not secret_key:
    raise ValueError("TOKEN_SECRET_KEY not configured")

service = ScopedTokenService(secret_key=secret_key)

# DON'T: Hardcode secrets
service = ScopedTokenService(secret_key="hardcoded_secret")  # BAD!
```

### 2. Token Expiration

```python
# Short-lived access tokens
access_token = service.generate_token(
    ...,
    expires_in_hours=1,  # 1 hour
    token_type=TokenType.ACCESS
)

# Longer-lived refresh tokens
refresh_token = service.generate_token(
    ...,
    expires_in_hours=168,  # 1 week
    token_type=TokenType.REFRESH
)
```

### 3. Principle of Least Privilege

```python
# DON'T: Grant all scopes
scopes = ["*"]  # BAD - too permissive

# DO: Grant only necessary scopes
scopes = ["messages:read"]  # GOOD - minimal required
```

### 4. Token Validation

```python
def protected_endpoint(token: str):
    # Always validate tokens
    payload = service.validate_token(token)
    if not payload:
        raise Unauthorized("Invalid token")

    # Check required scopes
    if 'admin' not in payload['scopes']:
        raise Forbidden("Admin scope required")

    # Proceed with operation
    return perform_admin_action()
```

### 5. Token Revocation

```python
# Revoke on logout
def logout(token: str):
    service.revoke_token(token)

# Revoke on password change
def change_password(user_id: str, new_password: str):
    # Revoke all user tokens
    user_tokens = get_user_tokens(user_id)
    for token in user_tokens:
        service.revoke_token(token)
```

## Database Schema

For database-backed scope management, create this table:

```sql
CREATE TABLE module_scopes (
    id SERIAL PRIMARY KEY,
    community_id VARCHAR(255) NOT NULL,
    module_name VARCHAR(255) NOT NULL,
    scope VARCHAR(100) NOT NULL,
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    granted_by_user_id VARCHAR(255),
    UNIQUE(community_id, module_name, scope)
);

CREATE INDEX idx_module_scopes_lookup
ON module_scopes(community_id, module_name);
```

## Error Handling

```python
from libs.module_sdk.security import TokenValidationError, ScopeError

try:
    token = service.generate_token(
        community_id="",  # Invalid
        module_name="test",
        scopes=["read"]
    )
except ValueError as e:
    print(f"Invalid input: {e}")

# Validate and handle errors
payload = service.validate_token(token)
if payload is None:
    # Token invalid, expired, or revoked
    # Check logs for specific reason
    pass
```

## Testing

Run the test suite:

```bash
# Install pytest if not already installed
pip install pytest

# Run tests
python -m pytest libs/module_sdk/security/test_scoped_tokens.py -v
```

## Advanced Usage

### Custom Token Metadata

```python
token = service.generate_token(
    community_id="community_123",
    module_name="music_module",
    scopes=["read"],
    metadata={
        "user_id": "user_456",
        "session_id": "sess_789",
        "ip_address": "192.168.1.1",
        "user_agent": "WaddleBot/2.0"
    }
)

# Access metadata
payload = service.validate_token(token)
user_id = payload['metadata']['user_id']
```

### Token Refresh Flow

```python
def refresh_access_token(refresh_token: str) -> str:
    # Validate refresh token
    payload = service.validate_token(refresh_token)
    if not payload or payload['type'] != 'refresh':
        raise Unauthorized("Invalid refresh token")

    # Generate new access token
    access_token = service.generate_token(
        community_id=payload['community_id'],
        module_name=payload['module_name'],
        scopes=payload['scopes'],
        expires_in_hours=1,
        token_type=TokenType.ACCESS
    )

    return access_token
```

### Scope-Based Middleware

```python
from functools import wraps

def require_scopes(*required_scopes):
    """Decorator to enforce scope requirements"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Extract token from request
            token = request.headers.get('Authorization', '').replace('Bearer ', '')

            # Validate token
            payload = service.validate_token(token)
            if not payload:
                raise Unauthorized("Invalid token")

            # Check scopes
            token_scopes = set(payload['scopes'])
            if '*' not in token_scopes:
                if not any(scope in token_scopes for scope in required_scopes):
                    raise Forbidden(f"Required scopes: {required_scopes}")

            # Attach payload to request
            request.token_payload = payload

            return func(*args, **kwargs)
        return wrapper
    return decorator

# Usage
@require_scopes('messages:write', 'messages:delete')
def delete_message(message_id: str):
    # Only accessible with write or delete scope
    pass
```

## Troubleshooting

### Token Always Returns None

**Cause:** Token expired or invalid secret key

**Solution:**
```python
# Check expiration
import jwt
payload = jwt.decode(token, options={"verify_signature": False})
print(f"Expires: {payload['exp']}")

# Verify secret key matches
print(f"Secret: {service.secret_key}")
```

### Scope Validation Fails

**Cause:** Invalid scope format

**Solution:**
```python
# Valid scope formats
valid = ["read", "write", "users:manage", "api.v1:read"]

# Invalid formats
invalid = ["read write", "admin@system", ""]
```

## Support

For issues or questions:
- Check logs: `logging.getLogger('libs.module_sdk.security.scoped_tokens')`
- Review test suite: `test_scoped_tokens.py`
- WaddleBot documentation: See main project README
