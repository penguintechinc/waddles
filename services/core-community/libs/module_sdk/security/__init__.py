"""
Waddles Module SDK Security Module

Provides security utilities for module validation, input sanitization, and token management.

Exports:
    - CommandInput: Pydantic model for validating command inputs
    - InputSanitizer: Utility class for sanitizing various input types
    - ScopedTokenService: Service for managing scoped tokens with OAuth-like permissions
    - TokenData: Token payload data structure
    - TokenType: Token type enumeration
    - TokenValidationError: Exception for token validation failures
    - ScopeError: Exception for scope operation failures
    - create_scoped_token_service: Factory function for token service creation
"""

from .input_sanitizer import CommandInput, InputSanitizer
from .scoped_tokens import (
    ScopedTokenService,
    TokenData,
    TokenType,
    TokenValidationError,
    ScopeError,
    create_scoped_token_service,
)

__all__ = [
    # Input sanitization
    'CommandInput',
    'InputSanitizer',
    # Token management
    'ScopedTokenService',
    'TokenData',
    'TokenType',
    'TokenValidationError',
    'ScopeError',
    'create_scoped_token_service',
]
