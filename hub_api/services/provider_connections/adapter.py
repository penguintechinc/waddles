"""The provider-adapter contract: what a platform must supply to plug into the framework.

A *provider* (Twitch today; Discord, Slack, Kick, YouTube ... next) is described by two things:

* an :class:`AdapterSpec` -- immutable, declarative data (what the provider is, which credential and
  metadata fields it takes, which capabilities it can grant). The connection API derives its
  provider descriptors and all request validation from the spec, so neither the API nor a UI/CLI
  needs any per-provider code.
* a :class:`ProviderAdapter` -- the three provider calls the framework cannot make generically:
  verify a credential, resolve the external resource it is being connected to, and refresh it.

Everything else (encryption at rest, tenant/community isolation, the three-layer storage, the
data-plane resolve flow, telemetry) lives in the framework and is provider-agnostic. This module
must not import anything Waddles-specific beyond the standard library, so the package can be
lifted into ``penguin-aaa`` later without untangling.

Adding a provider is therefore: one adapter module + one :meth:`AdapterRegistry.register` line
(plus a one-line migration extending the ``resource_type`` allow-list when it introduces a new
resource kind). Anything more means the boundary leaked.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

from services.provider_connections.errors import InvalidConnectionInput, UnknownProvider

#: How an admin supplies the credential. ``credentials`` = pasted/CLI-supplied token pair, the only
#: kind implemented by this foundation; ``oauth_connect`` / ``bot_invite_link`` / ``app_manifest``
#: are reserved for the redirect-style installs described in docs/CONNECTION_MODEL.local.md.
INSTALL_KIND_CREDENTIALS: Final = "credentials"

#: Longest access/refresh token accepted. Real provider tokens are tens of bytes (Twitch 30,
#: Discord ~70, Slack ~60); a few KiB covers JWT-shaped tokens and bounds the ciphertext column.
MAX_TOKEN_LENGTH: Final = 4096

_PROVIDER_RE: Final = re.compile(r"^[a-z][a-z0-9_]{1,29}$")
_RESOURCE_TYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_FIELD_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
#: Provider tokens are printable ASCII (base64url, ``.``, ``-``, ``_``); nothing else is a token.
_TOKEN_CHARS_RE: Final = re.compile(r"[\x21-\x7e]+")


@dataclass(slots=True, frozen=True)
class CredentialField:
    """One credential input the provider takes (descriptor only -- values are never described)."""

    name: str
    required: bool
    description: str


@dataclass(slots=True, frozen=True)
class MetadataField:
    """One adapter-validated metadata input (for example the Twitch channel to join)."""

    name: str
    required: bool
    description: str
    pattern: str | None = None
    max_length: int = 128
    lowercase: bool = False
    #: Leading marker stripped before validation (for example the IRC ``#`` of ``#channel``).
    strip_prefix: str = ""


@dataclass(slots=True, frozen=True)
class AdapterSpec:
    """Immutable description of one provider; the single source for descriptors and validation.

    ``public_metadata_keys`` is the allow-list of stored metadata keys an admin response may show;
    ``runtime_metadata_keys`` is the (superset) allow-list handed to the data plane. Anything not
    listed in either is never serialised -- a key added to storage later cannot leak by default.
    """

    provider: str
    display_name: str
    platform: str
    resource_type: str
    install_kind: str
    capabilities: frozenset[str]
    required_scopes: tuple[str, ...]
    credential_fields: tuple[CredentialField, ...]
    metadata_fields: tuple[MetadataField, ...]
    public_metadata_keys: frozenset[str]
    runtime_metadata_keys: frozenset[str]
    refreshable: bool

    def __post_init__(self) -> None:
        """Reject a malformed spec at registration time rather than at first request."""
        if not _PROVIDER_RE.match(self.provider):
            raise ValueError(f"provider id {self.provider!r} must match {_PROVIDER_RE.pattern}")
        if not _RESOURCE_TYPE_RE.match(self.resource_type):
            raise ValueError(f"resource_type {self.resource_type!r} is malformed")
        if not self.platform:
            raise ValueError("platform must be non-empty")
        names = [item.name for item in self.credential_fields]
        names += [item.name for item in self.metadata_fields]
        for name in names:
            if not _FIELD_NAME_RE.match(name):
                raise ValueError(f"field name {name!r} must match {_FIELD_NAME_RE.pattern}")
        for metadata_field in self.metadata_fields:
            if metadata_field.pattern is not None:
                re.compile(metadata_field.pattern)


@dataclass(slots=True, frozen=True)
class Credentials:
    """A token pair as supplied by an admin. Secret fields are excluded from ``repr``."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)


@dataclass(slots=True, frozen=True)
class Account:
    """The identity behind a verified credential, as reported by the provider.

    ``expires_at`` is ``None`` for a token the provider says never expires.
    """

    account_id: str
    account_label: str
    app_id: str
    scopes: tuple[str, ...]
    expires_at: datetime | None


@dataclass(slots=True, frozen=True)
class ResolvedResource:
    """The external resource a connection points at, plus provider-specific facts about it.

    ``resource_id`` becomes ``platform_connections.resource_id`` (stable, provider-issued id);
    ``facts`` is merged into ``resource_metadata`` and must be JSON-safe and secret-free.
    """

    resource_id: str
    facts: Mapping[str, Any]


@dataclass(slots=True, frozen=True)
class RefreshedTokens:
    """Outcome of a successful refresh. ``refresh_token`` is ``None`` when the provider omits it."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: datetime | None
    scopes: tuple[str, ...]


@runtime_checkable
class ProviderAdapter(Protocol):
    """The provider-specific half of the framework. Implementations must never log a token."""

    spec: AdapterSpec

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """Canonicalise pasted credentials (for example strip an ``oauth:`` prefix)."""
        ...

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """Verify the access token with the provider and report whose it is.

        Raises :class:`CredentialsRejected` for a token the provider refuses and
        :class:`ProviderUnavailable` when the provider cannot be reached.
        """
        ...

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """Resolve the external resource named by the validated ``metadata``.

        Raises :class:`ResourceUnresolvable` when the provider has no such resource.
        """
        ...

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """Exchange ``refresh_token`` for a fresh token pair.

        ``facts`` is the connection's stored ``resource_metadata`` (so an adapter can tell which
        OAuth application minted the token). Raises :class:`RefreshRejected` when the provider
        refuses the token, :class:`RefreshNotConfigured` when the platform has no usable OAuth
        application, :class:`ProviderUnavailable` on transient failure.
        """
        ...

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """Return the subset of ``spec.capabilities`` the given ``scopes`` actually grant."""
        ...


def validate_metadata(spec: AdapterSpec, raw: Mapping[str, Any]) -> dict[str, str]:
    """Validate and normalise caller-supplied metadata against ``spec.metadata_fields``.

    Unknown keys, missing required keys, non-string values, over-long values and pattern
    mismatches all raise :class:`InvalidConnectionInput` with a message naming only the *field*,
    never echoing the offending value.
    """
    declared = {item.name: item for item in spec.metadata_fields}
    unknown = sorted(set(raw) - set(declared))
    if unknown:
        raise InvalidConnectionInput(f"unknown metadata field(s): {', '.join(unknown)}")
    cleaned: dict[str, str] = {}
    for name, metadata_field in declared.items():
        value = raw.get(name)
        if value is None:
            if metadata_field.required:
                raise InvalidConnectionInput(f"metadata field {name!r} is required")
            continue
        if not isinstance(value, str):
            raise InvalidConnectionInput(f"metadata field {name!r} must be a string")
        value = value.strip()
        if metadata_field.strip_prefix and value.startswith(metadata_field.strip_prefix):
            value = value[len(metadata_field.strip_prefix) :]
        if metadata_field.lowercase:
            value = value.lower()
        if not value:
            if metadata_field.required:
                raise InvalidConnectionInput(f"metadata field {name!r} must not be empty")
            continue
        if len(value) > metadata_field.max_length:
            raise InvalidConnectionInput(f"metadata field {name!r} is too long")
        if metadata_field.pattern is not None and not re.fullmatch(metadata_field.pattern, value):
            raise InvalidConnectionInput(f"metadata field {name!r} has an invalid format")
        cleaned[name] = value
    return cleaned


def validate_token_shape(label: str, value: str) -> str:
    """Return ``value`` if it is a plausible bearer token; raise otherwise (value never echoed)."""
    if not isinstance(value, str) or not value:
        raise InvalidConnectionInput(f"{label} is required")
    if len(value) > MAX_TOKEN_LENGTH:
        raise InvalidConnectionInput(f"{label} is too long")
    if not _TOKEN_CHARS_RE.fullmatch(value):
        raise InvalidConnectionInput(f"{label} must be printable ASCII without whitespace")
    return value


def validate_credentials_shape(
    spec: AdapterSpec, access_token: str, refresh_token: str | None
) -> Credentials:
    """Shape-check an admin-supplied token pair against the spec before any provider call."""
    access = validate_token_shape("access_token", access_token)
    required = {item.name for item in spec.credential_fields if item.required}
    refresh: str | None = None
    if refresh_token:
        refresh = validate_token_shape("refresh_token", refresh_token)
    elif "refresh_token" in required:
        raise InvalidConnectionInput("refresh_token is required")
    return Credentials(access_token=access, refresh_token=refresh)


class AdapterRegistry:
    """Provider id -> adapter. Built once at startup; lookups of unknown ids fail loudly."""

    def __init__(self, adapters: Iterable[ProviderAdapter] = ()) -> None:
        """Register every adapter in ``adapters``."""
        self._adapters: dict[str, ProviderAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ProviderAdapter) -> None:
        """Add ``adapter``; registering the same provider id twice is a programming error."""
        provider = adapter.spec.provider
        if provider in self._adapters:
            raise ValueError(f"provider {provider!r} is already registered")
        self._adapters[provider] = adapter

    def get(self, provider: str) -> ProviderAdapter:
        """Return the adapter for ``provider`` or raise :class:`UnknownProvider`."""
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise UnknownProvider("unsupported provider")
        return adapter

    def specs(self) -> list[AdapterSpec]:
        """Every registered spec, ordered by provider id (stable for UI/CLI listings)."""
        return [self._adapters[key].spec for key in sorted(self._adapters)]

    def __contains__(self, provider: object) -> bool:
        """Whether ``provider`` has a registered adapter."""
        return provider in self._adapters
