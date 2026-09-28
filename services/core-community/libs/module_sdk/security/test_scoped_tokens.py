"""
Tests for ScopedTokenService

Comprehensive test suite for the scoped token management service.
Run with: python -m pytest test_scoped_tokens.py -v
"""

import pytest
import time
from datetime import datetime, timedelta
from .scoped_tokens import (
    ScopedTokenService,
    TokenData,
    TokenType,
    TokenValidationError,
    ScopeError,
    create_scoped_token_service
)


class TestScopedTokenService:
    """Test suite for ScopedTokenService"""

    @pytest.fixture
    def service(self):
        """Create a token service instance for testing"""
        return ScopedTokenService(secret_key="test_secret_key_minimum_32_chars_long!")

    @pytest.fixture
    def test_token_data(self):
        """Sample token data for testing"""
        return {
            'community_id': 'test_community_123',
            'module_name': 'music_module',
            'scopes': ['read', 'write'],
            'expires_in_hours': 24
        }

    def test_service_initialization(self):
        """Test service initialization with valid secret key"""
        service = ScopedTokenService(secret_key="a" * 32)
        assert service.secret_key == "a" * 32
        assert service.algorithm == 'HS256'
        assert service.dal is None

    def test_service_initialization_weak_key(self):
        """Test that weak secret keys are rejected"""
        with pytest.raises(ValueError, match="at least 32 characters"):
            ScopedTokenService(secret_key="short")

    def test_generate_token_success(self, service, test_token_data):
        """Test successful token generation"""
        token = service.generate_token(**test_token_data)

        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 0

    def test_generate_token_invalid_community_id(self, service):
        """Test token generation with invalid community_id"""
        with pytest.raises(ValueError, match="community_id"):
            service.generate_token(
                community_id="",
                module_name="test_module",
                scopes=['read']
            )

    def test_generate_token_invalid_module_name(self, service):
        """Test token generation with invalid module_name"""
        with pytest.raises(ValueError, match="module_name"):
            service.generate_token(
                community_id="community123",
                module_name="",
                scopes=['read']
            )

    def test_generate_token_invalid_scopes(self, service):
        """Test token generation with invalid scopes"""
        with pytest.raises(ValueError, match="scopes"):
            service.generate_token(
                community_id="community123",
                module_name="test_module",
                scopes="not_a_list"  # Should be a list
            )

    def test_generate_token_invalid_expiration(self, service):
        """Test token generation with invalid expiration"""
        with pytest.raises(ValueError, match="expires_in_hours"):
            service.generate_token(
                community_id="community123",
                module_name="test_module",
                scopes=['read'],
                expires_in_hours=0  # Invalid
            )

        with pytest.raises(ValueError, match="expires_in_hours"):
            service.generate_token(
                community_id="community123",
                module_name="test_module",
                scopes=['read'],
                expires_in_hours=9000  # Too long
            )

    def test_validate_token_success(self, service, test_token_data):
        """Test successful token validation"""
        token = service.generate_token(**test_token_data)
        payload = service.validate_token(token)

        assert payload is not None
        assert payload['community_id'] == test_token_data['community_id']
        assert payload['module_name'] == test_token_data['module_name']
        assert payload['scopes'] == test_token_data['scopes']
        assert 'jti' in payload
        assert 'iat' in payload
        assert 'exp' in payload

    def test_validate_token_invalid_format(self, service):
        """Test validation with invalid token format"""
        assert service.validate_token("invalid_token") is None
        assert service.validate_token("") is None
        assert service.validate_token(None) is None

    def test_validate_token_wrong_secret(self, test_token_data):
        """Test validation with wrong secret key"""
        service1 = ScopedTokenService(secret_key="a" * 32)
        service2 = ScopedTokenService(secret_key="b" * 32)

        token = service1.generate_token(**test_token_data)
        payload = service2.validate_token(token)

        assert payload is None

    def test_validate_token_expired(self, service):
        """Test validation of expired token"""
        # Generate token that expires in 1 second
        token = service.generate_token(
            community_id="test_community",
            module_name="test_module",
            scopes=['read'],
            expires_in_hours=1/3600  # 1 second
        )

        # Wait for token to expire
        time.sleep(2)

        payload = service.validate_token(token)
        assert payload is None

    def test_revoke_token_success(self, service, test_token_data):
        """Test successful token revocation"""
        token = service.generate_token(**test_token_data)

        # Verify token is valid before revocation
        assert service.validate_token(token) is not None

        # Revoke token
        assert service.revoke_token(token) is True

        # Verify token is invalid after revocation
        assert service.validate_token(token) is None

    def test_revoke_token_invalid(self, service):
        """Test revoking invalid token"""
        assert service.revoke_token("invalid_token") is False
        assert service.revoke_token("") is False
        assert service.revoke_token(None) is False

    def test_token_data_class(self):
        """Test TokenData dataclass"""
        now = datetime.utcnow()
        expires = now + timedelta(hours=24)

        token_data = TokenData(
            community_id="test_community",
            module_name="test_module",
            scopes=['read', 'write'],
            issued_at=now,
            expires_at=expires
        )

        assert token_data.community_id == "test_community"
        assert token_data.module_name == "test_module"
        assert token_data.scopes == ['read', 'write']
        assert not token_data.is_expired()
        assert token_data.has_scope('read')
        assert token_data.has_scope('write')
        assert not token_data.has_scope('admin')

    def test_token_data_wildcard_scope(self):
        """Test wildcard scope in TokenData"""
        now = datetime.utcnow()
        expires = now + timedelta(hours=24)

        token_data = TokenData(
            community_id="test_community",
            module_name="test_module",
            scopes=['*'],
            issued_at=now,
            expires_at=expires
        )

        assert token_data.has_scope('read')
        assert token_data.has_scope('write')
        assert token_data.has_scope('anything')

    def test_token_data_to_dict(self):
        """Test TokenData serialization"""
        now = datetime.utcnow()
        expires = now + timedelta(hours=24)

        token_data = TokenData(
            community_id="test_community",
            module_name="test_module",
            scopes=['read'],
            issued_at=now,
            expires_at=expires,
            jti="test_jti"
        )

        data_dict = token_data.to_dict()

        assert data_dict['community_id'] == "test_community"
        assert data_dict['module_name'] == "test_module"
        assert data_dict['scopes'] == ['read']
        assert data_dict['jti'] == "test_jti"
        assert 'iat' in data_dict
        assert 'exp' in data_dict

    def test_token_data_from_dict(self):
        """Test TokenData deserialization"""
        now = datetime.utcnow()
        expires = now + timedelta(hours=24)

        data_dict = {
            'community_id': 'test_community',
            'module_name': 'test_module',
            'scopes': ['read', 'write'],
            'iat': int(now.timestamp()),
            'exp': int(expires.timestamp()),
            'type': 'access',
            'jti': 'test_jti',
            'metadata': {'key': 'value'}
        }

        token_data = TokenData.from_dict(data_dict)

        assert token_data.community_id == 'test_community'
        assert token_data.module_name == 'test_module'
        assert token_data.scopes == ['read', 'write']
        assert token_data.jti == 'test_jti'
        assert token_data.metadata == {'key': 'value'}

    def test_token_types(self, service):
        """Test different token types"""
        # Access token
        access_token = service.generate_token(
            community_id="test_community",
            module_name="test_module",
            scopes=['read'],
            token_type=TokenType.ACCESS
        )
        access_payload = service.validate_token(access_token)
        assert access_payload['type'] == 'access'

        # Service token
        service_token = service.generate_token(
            community_id="test_community",
            module_name="test_module",
            scopes=['read'],
            token_type=TokenType.SERVICE
        )
        service_payload = service.validate_token(service_token)
        assert service_payload['type'] == 'service'

    def test_token_metadata(self, service):
        """Test token with custom metadata"""
        metadata = {'custom_field': 'custom_value', 'user_id': '12345'}

        token = service.generate_token(
            community_id="test_community",
            module_name="test_module",
            scopes=['read'],
            metadata=metadata
        )

        payload = service.validate_token(token)
        assert payload['metadata'] == metadata

    def test_scope_validation(self, service):
        """Test scope string validation"""
        # Valid scopes
        assert service._is_valid_scope('read')
        assert service._is_valid_scope('write')
        assert service._is_valid_scope('admin')
        assert service._is_valid_scope('users:manage')
        assert service._is_valid_scope('api.v1.read')
        assert service._is_valid_scope('resource-name')
        assert service._is_valid_scope('*')

        # Invalid scopes
        assert not service._is_valid_scope('')
        assert not service._is_valid_scope('x' * 101)  # Too long
        assert not service._is_valid_scope('invalid scope')  # Space
        assert not service._is_valid_scope('invalid@scope')  # Special char

    def test_string_sanitization(self, service):
        """Test input string sanitization"""
        # Valid inputs
        assert service._sanitize_string('  test  ') == 'test'
        assert service._sanitize_string('valid_string') == 'valid_string'

        # Invalid inputs
        with pytest.raises(ValueError, match="cannot be empty"):
            service._sanitize_string('')

        with pytest.raises(ValueError, match="cannot be empty"):
            service._sanitize_string('   ')

        with pytest.raises(ValueError, match="null bytes"):
            service._sanitize_string('test\x00value')

        with pytest.raises(ValueError, match="maximum length"):
            service._sanitize_string('x' * 300)

    def test_factory_function(self):
        """Test factory function for service creation"""
        # With custom secret
        service = create_scoped_token_service(secret_key="a" * 32)
        assert service.secret_key == "a" * 32

        # Without secret (generates random)
        service2 = create_scoped_token_service()
        assert service2.secret_key is not None
        assert len(service2.secret_key) >= 32

    def test_get_granted_scopes_no_dal(self, service):
        """Test get_granted_scopes without DAL configured"""
        scopes = service.get_granted_scopes("community123", "module_name")
        assert scopes == []

    def test_grant_scope_no_dal(self, service):
        """Test grant_scope without DAL configured"""
        result = service.grant_scope(
            "community123",
            "module_name",
            "read",
            "user123"
        )
        assert result is True  # Should succeed without DAL (placeholder)

    def test_revoke_scope_no_dal(self, service):
        """Test revoke_scope without DAL configured"""
        result = service.revoke_scope("community123", "module_name", "read")
        assert result is True  # Should succeed without DAL (placeholder)

    def test_multiple_scopes(self, service):
        """Test token with multiple scopes"""
        scopes = ['read', 'write', 'delete', 'admin']

        token = service.generate_token(
            community_id="test_community",
            module_name="test_module",
            scopes=scopes
        )

        payload = service.validate_token(token)
        assert payload['scopes'] == scopes

    def test_jti_uniqueness(self, service):
        """Test that JTI is unique for each token"""
        token1 = service.generate_token(
            community_id="test_community",
            module_name="test_module",
            scopes=['read']
        )

        token2 = service.generate_token(
            community_id="test_community",
            module_name="test_module",
            scopes=['read']
        )

        payload1 = service.validate_token(token1)
        payload2 = service.validate_token(token2)

        assert payload1['jti'] != payload2['jti']

    def test_token_independence(self, service):
        """Test that revoking one token doesn't affect others"""
        token1 = service.generate_token(
            community_id="test_community",
            module_name="test_module",
            scopes=['read']
        )

        token2 = service.generate_token(
            community_id="test_community",
            module_name="test_module",
            scopes=['read']
        )

        # Revoke token1
        service.revoke_token(token1)

        # token1 should be invalid, token2 should still be valid
        assert service.validate_token(token1) is None
        assert service.validate_token(token2) is not None


class TestTokenDataHelpers:
    """Test TokenData helper methods"""

    def test_is_expired_false(self):
        """Test is_expired returns False for valid token"""
        token_data = TokenData(
            community_id="test",
            module_name="test",
            scopes=['read'],
            issued_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=1)
        )
        assert not token_data.is_expired()

    def test_is_expired_true(self):
        """Test is_expired returns True for expired token"""
        token_data = TokenData(
            community_id="test",
            module_name="test",
            scopes=['read'],
            issued_at=datetime.utcnow() - timedelta(hours=2),
            expires_at=datetime.utcnow() - timedelta(hours=1)
        )
        assert token_data.is_expired()

    def test_has_scope_true(self):
        """Test has_scope returns True for present scope"""
        token_data = TokenData(
            community_id="test",
            module_name="test",
            scopes=['read', 'write'],
            issued_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=1)
        )
        assert token_data.has_scope('read')
        assert token_data.has_scope('write')

    def test_has_scope_false(self):
        """Test has_scope returns False for absent scope"""
        token_data = TokenData(
            community_id="test",
            module_name="test",
            scopes=['read'],
            issued_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=1)
        )
        assert not token_data.has_scope('write')
        assert not token_data.has_scope('admin')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
