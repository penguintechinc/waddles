#!/usr/bin/env python3
"""
ScopedTokenService Example Usage

This script demonstrates basic usage of the ScopedTokenService
for OAuth-like permission management in Waddles modules.

Run: python example_usage.py
"""

import sys
import os
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from libs.module_sdk.security import (
    ScopedTokenService,
    TokenType,
    create_scoped_token_service
)


def example_basic_usage():
    """Example 1: Basic token generation and validation"""
    print("\n" + "=" * 60)
    print("Example 1: Basic Token Usage")
    print("=" * 60)

    # Create service
    service = create_scoped_token_service(
        secret_key="example_secret_key_at_least_32_chars_long_for_security!"
    )

    # Generate token
    token = service.generate_token(
        community_id="stream_community_123",
        module_name="chat_bot",
        scopes=["messages:read", "messages:write"],
        expires_in_hours=24
    )

    print(f"\nGenerated Token: {token[:50]}...")
    print(f"Token Length: {len(token)} characters")

    # Validate token
    payload = service.validate_token(token)
    if payload:
        print("\nToken Validation: SUCCESS")
        print(f"  Community ID: {payload['community_id']}")
        print(f"  Module Name: {payload['module_name']}")
        print(f"  Scopes: {payload['scopes']}")
        print(f"  Token Type: {payload['type']}")
        print(f"  Issued At: {datetime.fromtimestamp(payload['iat'])}")
        print(f"  Expires At: {datetime.fromtimestamp(payload['exp'])}")
        print(f"  JWT ID: {payload['jti']}")
    else:
        print("\nToken Validation: FAILED")


def example_multiple_scopes():
    """Example 2: Token with multiple scopes"""
    print("\n" + "=" * 60)
    print("Example 2: Multiple Scopes")
    print("=" * 60)

    service = create_scoped_token_service(
        secret_key="example_secret_key_at_least_32_chars_long_for_security!"
    )

    # Generate token with multiple scopes
    scopes = [
        "users:read",
        "users:write",
        "messages:read",
        "messages:write",
        "channels:manage"
    ]

    token = service.generate_token(
        community_id="gaming_community",
        module_name="moderation_bot",
        scopes=scopes,
        expires_in_hours=48
    )

    payload = service.validate_token(token)
    if payload:
        print("\nGranted Scopes:")
        for scope in payload['scopes']:
            print(f"  ✓ {scope}")


def example_token_types():
    """Example 3: Different token types"""
    print("\n" + "=" * 60)
    print("Example 3: Token Types")
    print("=" * 60)

    service = create_scoped_token_service(
        secret_key="example_secret_key_at_least_32_chars_long_for_security!"
    )

    # Access token (short-lived)
    access_token = service.generate_token(
        community_id="api_community",
        module_name="api_client",
        scopes=["api:read"],
        expires_in_hours=1,
        token_type=TokenType.ACCESS
    )

    # Service token (longer-lived)
    service_token = service.generate_token(
        community_id="api_community",
        module_name="backend_service",
        scopes=["*"],
        expires_in_hours=720,  # 30 days
        token_type=TokenType.SERVICE
    )

    print("\nAccess Token:")
    access_payload = service.validate_token(access_token)
    print(f"  Type: {access_payload['type']}")
    print(f"  Expires in: 1 hour")

    print("\nService Token:")
    service_payload = service.validate_token(service_token)
    print(f"  Type: {service_payload['type']}")
    print(f"  Expires in: 720 hours (30 days)")


def example_token_metadata():
    """Example 4: Token with custom metadata"""
    print("\n" + "=" * 60)
    print("Example 4: Custom Metadata")
    print("=" * 60)

    service = create_scoped_token_service(
        secret_key="example_secret_key_at_least_32_chars_long_for_security!"
    )

    # Generate token with metadata
    token = service.generate_token(
        community_id="analytics_community",
        module_name="stats_module",
        scopes=["analytics:read"],
        expires_in_hours=24,
        metadata={
            "user_id": "user_12345",
            "session_id": "sess_67890",
            "client_version": "2.0.1",
            "features": ["graphs", "exports"]
        }
    )

    payload = service.validate_token(token)
    print("\nToken Metadata:")
    for key, value in payload['metadata'].items():
        print(f"  {key}: {value}")


def example_token_revocation():
    """Example 5: Token revocation"""
    print("\n" + "=" * 60)
    print("Example 5: Token Revocation")
    print("=" * 60)

    service = create_scoped_token_service(
        secret_key="example_secret_key_at_least_32_chars_long_for_security!"
    )

    # Generate token
    token = service.generate_token(
        community_id="secure_community",
        module_name="secure_module",
        scopes=["admin"],
        expires_in_hours=24
    )

    # Validate before revocation
    print("\nBefore Revocation:")
    payload = service.validate_token(token)
    print(f"  Valid: {payload is not None}")

    # Revoke token
    revoked = service.revoke_token(token)
    print(f"\nRevocation: {'SUCCESS' if revoked else 'FAILED'}")

    # Validate after revocation
    print("\nAfter Revocation:")
    payload = service.validate_token(token)
    print(f"  Valid: {payload is not None}")


def example_scope_validation():
    """Example 6: Scope validation and permissions"""
    print("\n" + "=" * 60)
    print("Example 6: Scope-Based Permissions")
    print("=" * 60)

    service = create_scoped_token_service(
        secret_key="example_secret_key_at_least_32_chars_long_for_security!"
    )

    # Generate tokens with different scopes
    read_token = service.generate_token(
        community_id="docs_community",
        module_name="docs_viewer",
        scopes=["docs:read"]
    )

    write_token = service.generate_token(
        community_id="docs_community",
        module_name="docs_editor",
        scopes=["docs:read", "docs:write"]
    )

    admin_token = service.generate_token(
        community_id="docs_community",
        module_name="docs_admin",
        scopes=["*"]  # Wildcard - all permissions
    )

    # Check permissions
    def can_perform_action(token, required_scope):
        payload = service.validate_token(token)
        if not payload:
            return False
        scopes = payload['scopes']
        return required_scope in scopes or '*' in scopes

    print("\nRead Token Permissions:")
    print(f"  Can read: {can_perform_action(read_token, 'docs:read')}")
    print(f"  Can write: {can_perform_action(read_token, 'docs:write')}")
    print(f"  Can delete: {can_perform_action(read_token, 'docs:delete')}")

    print("\nWrite Token Permissions:")
    print(f"  Can read: {can_perform_action(write_token, 'docs:read')}")
    print(f"  Can write: {can_perform_action(write_token, 'docs:write')}")
    print(f"  Can delete: {can_perform_action(write_token, 'docs:delete')}")

    print("\nAdmin Token Permissions:")
    print(f"  Can read: {can_perform_action(admin_token, 'docs:read')}")
    print(f"  Can write: {can_perform_action(admin_token, 'docs:write')}")
    print(f"  Can delete: {can_perform_action(admin_token, 'docs:delete')}")


def example_error_handling():
    """Example 7: Error handling"""
    print("\n" + "=" * 60)
    print("Example 7: Error Handling")
    print("=" * 60)

    service = create_scoped_token_service(
        secret_key="example_secret_key_at_least_32_chars_long_for_security!"
    )

    print("\nTesting invalid inputs:")

    # Invalid community_id
    try:
        token = service.generate_token(
            community_id="",
            module_name="test",
            scopes=["read"]
        )
    except ValueError as e:
        print(f"  ✓ Empty community_id rejected: {e}")

    # Invalid scopes
    try:
        token = service.generate_token(
            community_id="test",
            module_name="test",
            scopes="not_a_list"
        )
    except ValueError as e:
        print(f"  ✓ Invalid scopes rejected: {e}")

    # Invalid expiration
    try:
        token = service.generate_token(
            community_id="test",
            module_name="test",
            scopes=["read"],
            expires_in_hours=0
        )
    except ValueError as e:
        print(f"  ✓ Invalid expiration rejected: {e}")

    # Invalid token validation
    print("\nTesting invalid token validation:")
    result = service.validate_token("invalid_token_string")
    print(f"  ✓ Invalid token returns None: {result is None}")

    result = service.validate_token("")
    print(f"  ✓ Empty token returns None: {result is None}")

    result = service.validate_token(None)
    print(f"  ✓ Null token returns None: {result is None}")


def main():
    """Run all examples"""
    print("\n" + "=" * 60)
    print("ScopedTokenService - Usage Examples")
    print("=" * 60)

    examples = [
        example_basic_usage,
        example_multiple_scopes,
        example_token_types,
        example_token_metadata,
        example_token_revocation,
        example_scope_validation,
        example_error_handling
    ]

    for example in examples:
        try:
            example()
        except Exception as e:
            print(f"\nError in {example.__name__}: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 60)
    print("All examples completed!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
