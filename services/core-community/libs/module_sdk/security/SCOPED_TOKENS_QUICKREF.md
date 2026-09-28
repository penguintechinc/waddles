# ScopedTokenService Quick Reference

## Import

```python
from libs.module_sdk.security import (
    ScopedTokenService,
    TokenType,
    TokenData,
    create_scoped_token_service
)
```

## Initialize Service

```python
# Factory function (recommended)
service = create_scoped_token_service(
    secret_key="your-32-char-minimum-secret-key"
)

# Direct instantiation
service = ScopedTokenService(
    secret_key="your-32-char-minimum-secret-key",
    algorithm='HS256',  # optional, default HS256
    dal=None  # optional, for database integration
)
```

## Generate Token

```python
token = service.generate_token(
    community_id="community_123",
    module_name="music_module",
    scopes=["read", "write"],
    expires_in_hours=24,  # optional, default 24
    token_type=TokenType.ACCESS,  # optional, default ACCESS
    metadata={"key": "value"}  # optional
)
```

## Validate Token

```python
payload = service.validate_token(token)

if payload:
    # Token is valid
    community_id = payload['community_id']
    module_name = payload['module_name']
    scopes = payload['scopes']
    issued_at = payload['iat']  # Unix timestamp
    expires_at = payload['exp']  # Unix timestamp
    jti = payload['jti']  # JWT ID
    metadata = payload['metadata']
else:
    # Token is invalid, expired, or revoked
    pass
```

## Revoke Token

```python
revoked = service.revoke_token(token)
# Returns True if revoked, False if failed
```

## Scope Management (Async, requires dal)

```python
# Grant scope
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

# Revoke scope
await service.revoke_scope_async(
    community_id="community_123",
    module_name="music_module",
    scope="playlist:manage"
)
```

## Token Types

```python
TokenType.ACCESS   # Short-lived access tokens
TokenType.SERVICE  # Long-lived service tokens
TokenType.REFRESH  # Refresh tokens for obtaining new access tokens
```

## Scope Examples

```python
# Resource:Action format
scopes = ["users:read", "users:write", "users:delete"]

# Namespaced scopes
scopes = ["api.v1:read", "api.v2:write"]

# Simple scopes
scopes = ["admin", "moderator"]

# Wildcard (all permissions)
scopes = ["*"]
```

## TokenData Helper

```python
from datetime import datetime, timedelta

token_data = TokenData(
    community_id="test",
    module_name="test",
    scopes=["read", "write"],
    issued_at=datetime.utcnow(),
    expires_at=datetime.utcnow() + timedelta(hours=1)
)

# Check expiration
if token_data.is_expired():
    print("Token expired")

# Check scope
if token_data.has_scope("write"):
    print("Has write permission")

# Serialize/Deserialize
data_dict = token_data.to_dict()
token_data = TokenData.from_dict(data_dict)
```

## Error Handling

```python
try:
    token = service.generate_token(
        community_id="",  # Will raise ValueError
        module_name="test",
        scopes=["read"]
    )
except ValueError as e:
    print(f"Invalid input: {e}")

# Validation returns None on error
payload = service.validate_token("invalid_token")
if payload is None:
    # Check logs for specific error
    pass
```

## Common Patterns

### Scope-Based Authorization

```python
def require_scope(required_scope):
    payload = service.validate_token(request.headers['Authorization'])
    if not payload:
        raise Unauthorized()

    scopes = payload['scopes']
    if required_scope not in scopes and '*' not in scopes:
        raise Forbidden()
```

### Token Refresh Flow

```python
def refresh_token(refresh_token_str):
    payload = service.validate_token(refresh_token_str)

    if not payload or payload['type'] != 'refresh':
        raise Unauthorized()

    # Generate new access token
    return service.generate_token(
        community_id=payload['community_id'],
        module_name=payload['module_name'],
        scopes=payload['scopes'],
        expires_in_hours=1,
        token_type=TokenType.ACCESS
    )
```

### Security Event - Revoke All Tokens

```python
def on_password_change(user_id):
    # Get all user tokens from database
    user_tokens = get_user_tokens(user_id)

    # Revoke each token
    for token in user_tokens:
        service.revoke_token(token)
```

## Token Payload Structure

```json
{
    "community_id": "community_123",
    "module_name": "music_module",
    "scopes": ["read", "write"],
    "iat": 1701234567,
    "exp": 1701320967,
    "type": "access",
    "jti": "a1b2c3d4...",
    "metadata": {
        "user_id": "user_456",
        "custom_field": "value"
    }
}
```

## Security Best Practices

1. **Secret Key**: Minimum 32 characters, load from environment
2. **Token Expiration**: Short-lived (1-24 hours) for access tokens
3. **Least Privilege**: Grant minimal required scopes
4. **Token Revocation**: Revoke on logout, password change, security events
5. **HTTPS Only**: Always use HTTPS in production
6. **Validation**: Always validate tokens before processing requests
7. **Logging**: Monitor token generation and validation failures

## Database Schema (for scope management)

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

## Links

- Full Documentation: `SCOPED_TOKENS_USAGE.md`
- Tests: `test_scoped_tokens.py`
- Examples: `example_usage.py`
