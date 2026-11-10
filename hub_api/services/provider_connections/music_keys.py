"""Per-community music API credentials: SoundCloud app credentials and a YouTube Data API key.

Both are *static application credentials*, not user-consented OAuth installs, so neither adapter
makes an outbound call at write time (the same stance as the Wix adapter: nothing is probed, nothing
is echoed). The secret is sealed at rest like every connection credential, is write-only through the
admin API, and reaches the data plane only through the resolver (``connections:resolve``) as the
resolve ``accessToken`` with the non-secret identifiers in ``metadata``.

* **SoundCloud** (``soundcloud``): the community's own SoundCloud app. ``access_token`` is the app
  *client secret*; the non-secret ``client_id`` is metadata (it becomes the resource id). The host
  side exchanges the pair for a short-lived token via the client-credentials grant
  (https://developers.soundcloud.com/docs/api/guide#authentication) -- hub-api never stores a minted
  token for it.
* **YouTube Data API key** (``youtube_api``): a Google API key for public-data reads (search,
  video lookups) where no channel OAuth is needed. The per-community OAuth install lives in
  :mod:`~services.provider_connections.youtube`; this is the keyed, read-only complement.
  ``key_label`` is an admin-chosen non-secret name (it becomes the resource id, so a tenant can
  hold several keys).

Spotify's app-level client credentials are NOT per community: they come from the platform
environment/secret store (``services.oauth_providers``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from services.provider_connections.adapter import (
    INSTALL_KIND_CREDENTIALS,
    Account,
    AdapterSpec,
    BindingKey,
    CredentialField,
    Credentials,
    MetadataField,
    RefreshedTokens,
    ResolvedResource,
)
from services.provider_connections.errors import InvalidConnectionInput, RefreshNotConfigured

SOUNDCLOUD_PROVIDER_ID: Final = "soundcloud"
SOUNDCLOUD_RESOURCE_TYPE: Final = "soundcloud_app"
YOUTUBE_API_PROVIDER_ID: Final = "youtube_api"
YOUTUBE_API_RESOURCE_TYPE: Final = "youtube_api_key"
CAPABILITY_MUSIC_SEARCH: Final = "music_search"

#: SoundCloud client ids / secrets are opaque alphanumerics (32 chars today); bounded superset.
SOUNDCLOUD_CLIENT_ID_PATTERN: Final = r"[A-Za-z0-9_-]{16,64}"
_SOUNDCLOUD_SECRET_RE: Final = re.compile(r"[A-Za-z0-9_-]{16,128}")
#: Google API keys are ``AIza`` + 35 URL-safe characters.
_GOOGLE_API_KEY_RE: Final = re.compile(r"AIza[0-9A-Za-z_-]{35}")
KEY_LABEL_PATTERN: Final = r"[A-Za-z0-9_.-]{1,64}"

SOUNDCLOUD_SPEC: Final = AdapterSpec(
    provider=SOUNDCLOUD_PROVIDER_ID,
    display_name="SoundCloud",
    platform=SOUNDCLOUD_PROVIDER_ID,
    resource_type=SOUNDCLOUD_RESOURCE_TYPE,
    install_kind=INSTALL_KIND_CREDENTIALS,
    capabilities=frozenset({CAPABILITY_MUSIC_SEARCH}),
    required_scopes=(),
    credential_fields=(
        CredentialField(
            name="access_token",
            required=True,
            description="SoundCloud app client secret (stored encrypted, never shown again)",
        ),
    ),
    metadata_fields=(
        MetadataField(
            name="client_id",
            required=True,
            description="SoundCloud app client id (public identifier of the app)",
            pattern=SOUNDCLOUD_CLIENT_ID_PATTERN,
            max_length=64,
        ),
    ),
    public_metadata_keys=frozenset({"client_id", "scopes", "capabilities"}),
    runtime_metadata_keys=frozenset({"client_id", "scopes", "capabilities"}),
    refreshable=False,
    binding_metadata=(BindingKey(wire="client_id", stored="client_id", required=True),),
)

YOUTUBE_API_SPEC: Final = AdapterSpec(
    provider=YOUTUBE_API_PROVIDER_ID,
    display_name="YouTube Data API key",
    platform=YOUTUBE_API_PROVIDER_ID,
    resource_type=YOUTUBE_API_RESOURCE_TYPE,
    install_kind=INSTALL_KIND_CREDENTIALS,
    capabilities=frozenset({CAPABILITY_MUSIC_SEARCH}),
    required_scopes=(),
    credential_fields=(
        CredentialField(
            name="access_token",
            required=True,
            description="Google API key with the YouTube Data API enabled (stored encrypted)",
        ),
    ),
    metadata_fields=(
        MetadataField(
            name="key_label",
            required=True,
            description="Admin-chosen name for this key (not secret)",
            pattern=KEY_LABEL_PATTERN,
            max_length=64,
        ),
    ),
    public_metadata_keys=frozenset({"key_label", "scopes", "capabilities"}),
    runtime_metadata_keys=frozenset({"key_label", "scopes", "capabilities"}),
    refreshable=False,
    binding_metadata=(BindingKey(wire="key_label", stored="key_label", required=True),),
)


class _StaticKeyAdapter:
    """Shared behaviour of the two keyed adapters: structural validation, no outbound call."""

    spec: AdapterSpec
    _secret_re: re.Pattern[str]
    _kind_label: str
    _resource_field: str

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """Refuse a secret that is not the provider's documented shape (value never echoed)."""
        if not self._secret_re.fullmatch(credentials.access_token):
            raise InvalidConnectionInput(f"access_token is not a valid {self._kind_label}")
        return Credentials(access_token=credentials.access_token)

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """A static key carries no OAuth scopes: it grants exactly the spec's capabilities."""
        return self.spec.capabilities

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """Structural only (already enforced by ``normalize_credentials``); nothing is probed."""
        return Account(
            account_id=f"{self.spec.provider}-key",
            account_label=self.spec.display_name,
            app_id="",
            scopes=(),
            expires_at=None,
        )

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """The named identifier (client id / key label) is the resource; nothing to look up."""
        value = metadata[self._resource_field]
        facts: dict[str, Any] = {self._resource_field: value}
        return ResolvedResource(resource_id=value, facts=facts)

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """A static key has no refresh flow."""
        raise RefreshNotConfigured(f"{self.spec.display_name} credentials are not refreshable")


class SoundcloudAdapter(_StaticKeyAdapter):
    """SoundCloud app credentials (client secret + public client id)."""

    spec: AdapterSpec = SOUNDCLOUD_SPEC
    _secret_re = _SOUNDCLOUD_SECRET_RE
    _kind_label = "SoundCloud client secret"
    _resource_field = "client_id"


class YoutubeApiKeyAdapter(_StaticKeyAdapter):
    """A Google API key for YouTube Data API public reads."""

    spec: AdapterSpec = YOUTUBE_API_SPEC
    _secret_re = _GOOGLE_API_KEY_RE
    _kind_label = "Google API key"
    _resource_field = "key_label"
