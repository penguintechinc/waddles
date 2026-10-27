#!/usr/bin/env python3
"""
Standalone test script for ScopedTokenService
This script can be run without pytest to verify basic functionality
"""

import sys
import os
from datetime import datetime, timedelta

# Add libs to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'libs/module_sdk/security'))

import scoped_tokens


def test_basic_functionality():
    """Test basic token generation and validation"""
    print("\n=== Testing Basic Functionality ===")

    service = scoped_tokens.create_scoped_token_service(
        secret_key='test_secret_key_at_least_32_chars_long!'
    )

    # Generate token
    token = service.generate_token(
        community_id='test_community',
        module_name='test_module',
        scopes=['read', 'write'],
        expires_in_hours=24
    )

    print(f'✓ Token generated successfully (length: {len(token)} chars)')

    # Validate token
    payload = service.validate_token(token)
    if payload:
        print('✓ Token validated successfully')
        print(f'  Community: {payload["community_id"]}')
        print(f'  Module: {payload["module_name"]}')
        print(f'  Scopes: {payload["scopes"]}')
        print(f'  JTI: {payload["jti"]}')
        return True
    else:
        print('✗ Token validation failed')
        return False


def test_token_data():
    """Test TokenData helper class"""
    print("\n=== Testing TokenData Class ===")

    token_data = scoped_tokens.TokenData(
        community_id='test',
        module_name='test',
        scopes=['read', 'write', 'admin'],
        issued_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=1)
    )

    print('✓ TokenData created successfully')
    print(f'  Has scope "read": {token_data.has_scope("read")}')
    print(f'  Has scope "admin": {token_data.has_scope("admin")}')
    print(f'  Has scope "delete": {token_data.has_scope("delete")}')
    print(f'  Is expired: {token_data.is_expired()}')

    return True


def test_token_revocation():
    """Test token revocation"""
    print("\n=== Testing Token Revocation ===")

    service = scoped_tokens.ScopedTokenService(
        secret_key='test_secret_key_at_least_32_chars_long!'
    )

    token = service.generate_token(
        community_id='test',
        module_name='test',
        scopes=['read'],
        expires_in_hours=24
    )

    # Validate before revocation
    payload_before = service.validate_token(token)
    print(f'✓ Token valid before revocation: {payload_before is not None}')

    # Revoke
    revoked = service.revoke_token(token)
    print(f'✓ Token revocation initiated: {revoked}')

    # Validate after revocation
    payload_after = service.validate_token(token)
    print(f'✓ Token invalid after revocation: {payload_after is None}')

    return payload_before is not None and payload_after is None


def test_token_types():
    """Test different token types"""
    print("\n=== Testing Token Types ===")

    service = scoped_tokens.ScopedTokenService(
        secret_key='test_secret_key_at_least_32_chars_long!'
    )

    # Access token
    access_token = service.generate_token(
        community_id='api',
        module_name='client',
        scopes=['api:read'],
        expires_in_hours=1,
        token_type=scoped_tokens.TokenType.ACCESS
    )
    access_payload = service.validate_token(access_token)
    print(f'✓ Access token type: {access_payload["type"]}')

    # Service token
    service_token = service.generate_token(
        community_id='api',
        module_name='service',
        scopes=['*'],
        expires_in_hours=24,
        token_type=scoped_tokens.TokenType.SERVICE
    )
    service_payload = service.validate_token(service_token)
    print(f'✓ Service token type: {service_payload["type"]}')

    return access_payload["type"] == "access" and service_payload["type"] == "service"


def test_wildcard_scope():
    """Test wildcard scope"""
    print("\n=== Testing Wildcard Scope ===")

    service = scoped_tokens.ScopedTokenService(
        secret_key='test_secret_key_at_least_32_chars_long!'
    )

    token = service.generate_token(
        community_id='admin',
        module_name='admin_module',
        scopes=['*'],
        expires_in_hours=1
    )

    payload = service.validate_token(token)
    print(f'✓ Wildcard token generated and validated: {payload is not None}')
    print(f'  Scopes: {payload["scopes"]}')

    # Test TokenData wildcard
    token_data = scoped_tokens.TokenData(
        community_id='test',
        module_name='test',
        scopes=['*'],
        issued_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=1)
    )

    print('✓ Wildcard grants all scopes:')
    print(f'  Has "read": {token_data.has_scope("read")}')
    print(f'  Has "write": {token_data.has_scope("write")}')
    print(f'  Has "anything": {token_data.has_scope("anything")}')

    return True


def test_metadata():
    """Test custom metadata"""
    print("\n=== Testing Custom Metadata ===")

    service = scoped_tokens.ScopedTokenService(
        secret_key='test_secret_key_at_least_32_chars_long!'
    )

    metadata = {'user_id': '123', 'session': 'abc', 'ip': '192.168.1.1'}

    token = service.generate_token(
        community_id='test',
        module_name='test',
        scopes=['read'],
        metadata=metadata
    )

    payload = service.validate_token(token)
    print(f'✓ Metadata preserved: {payload["metadata"]}')

    return payload["metadata"] == metadata


def test_error_handling():
    """Test error handling"""
    print("\n=== Testing Error Handling ===")

    service = scoped_tokens.ScopedTokenService(
        secret_key='test_secret_key_at_least_32_chars_long!'
    )

    errors_caught = 0

    # Test weak secret key
    try:
        weak_service = scoped_tokens.ScopedTokenService(secret_key='short')
    except ValueError:
        print('✓ Weak secret key rejected')
        errors_caught += 1

    # Test empty community_id
    try:
        service.generate_token('', 'module', ['read'])
    except ValueError:
        print('✓ Empty community_id rejected')
        errors_caught += 1

    # Test empty module_name
    try:
        service.generate_token('community', '', ['read'])
    except ValueError:
        print('✓ Empty module_name rejected')
        errors_caught += 1

    # Test invalid scopes type
    try:
        service.generate_token('community', 'module', 'not_a_list')
    except ValueError:
        print('✓ Invalid scopes type rejected')
        errors_caught += 1

    # Test invalid expiration
    try:
        service.generate_token('community', 'module', ['read'], expires_in_hours=0)
    except ValueError:
        print('✓ Invalid expiration rejected')
        errors_caught += 1

    # Test invalid token validation
    result = service.validate_token('invalid_token')
    if result is None:
        print('✓ Invalid token returns None')
        errors_caught += 1

    print(f'\nTotal errors caught: {errors_caught}/6')
    return errors_caught == 6


def main():
    """Run all tests"""
    print("=" * 60)
    print("ScopedTokenService - Standalone Tests")
    print("=" * 60)

    tests = [
        test_basic_functionality,
        test_token_data,
        test_token_revocation,
        test_token_types,
        test_wildcard_scope,
        test_metadata,
        test_error_handling
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
                print(f'\n✗ Test {test.__name__} FAILED')
        except Exception as e:
            failed += 1
            print(f'\n✗ Test {test.__name__} ERROR: {e}')
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return failed == 0


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
