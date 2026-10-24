"""Error taxonomy for the provider/connection framework.

Every exception carries a fixed, application-authored ``code`` and ``message``. Neither is ever
built from provider response text, a token, or user input, so any of them is safe to log and to
return to an API caller. ``blueprints/v1/provider_connections.py`` is the only place that maps
them onto HTTP statuses; the service layer never imports anything HTTP-shaped, which keeps the
package extractable into a shared library later.
"""

from __future__ import annotations


class ProviderConnectionError(Exception):
    """Base class: ``code`` is a stable machine string, ``message`` is safe to show a caller."""

    code = "connection_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Store the safe ``message`` and (optionally) override the class-level ``code``."""
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class UnknownProvider(ProviderConnectionError):
    """The requested provider has no registered adapter."""

    code = "unknown_provider"


class InvalidConnectionInput(ProviderConnectionError):
    """Request credentials or metadata failed adapter-declared validation."""

    code = "invalid_input"


class CredentialsRejected(ProviderConnectionError):
    """The provider refused the supplied token (expired, revoked, wrong app, malformed)."""

    code = "credentials_rejected"


class InsufficientScopes(ProviderConnectionError):
    """The token is valid but was not granted the scopes this provider's adapter requires."""

    code = "insufficient_scopes"


class ResourceUnresolvable(ProviderConnectionError):
    """The provider has no such resource (for example the Twitch channel does not exist)."""

    code = "resource_not_found"


class ProviderUnavailable(ProviderConnectionError):
    """The provider could not be reached or answered 5xx; nothing was changed. Safe to retry."""

    code = "provider_unavailable"


class RefreshRejected(ProviderConnectionError):
    """The provider refused the refresh token: the credential chain is dead and needs a re-auth."""

    code = "refresh_rejected"


class RefreshNotConfigured(ProviderConnectionError):
    """The platform has no OAuth application configured that could refresh this token."""

    code = "refresh_not_configured"


class ConnectionNotFound(ProviderConnectionError):
    """No such connection in this community (or it belongs to another tenant/community)."""

    code = "not_found"


class ConnectionConflict(ProviderConnectionError):
    """The write would clash with existing state (resource already connected, shared token)."""

    code = "conflict"


class ConnectionNotUsable(ProviderConnectionError):
    """The connection exists but cannot be handed to the data plane right now.

    ``reason`` is a fixed machine code: ``disabled``, ``pending_approval``, ``revoked_grant``,
    ``connection_revoked``, ``connection_expired``, ``refresh_rejected``, ``token_expired``.
    """

    code = "not_usable"

    def __init__(self, message: str, *, reason: str) -> None:
        """Record the fixed ``reason`` code alongside the safe message."""
        super().__init__(message)
        self.reason = reason


class VaultUnavailable(ProviderConnectionError):
    """The at-rest encryption key is missing or malformed; the store refuses to run without it."""

    code = "vault_unavailable"


class VaultCorrupt(ProviderConnectionError):
    """A stored ciphertext failed authentication (tampered row or wrong key)."""

    code = "vault_corrupt"


class SchemaMissing(ProviderConnectionError):
    """The connection tables or migration-0058 columns are not present."""

    code = "schema_missing"


class FeatureNotEnabled(ProviderConnectionError):
    """The tenant's plan / flag does not allow this operation (mapped to HTTP 402 by the API)."""

    code = "feature_not_enabled"
