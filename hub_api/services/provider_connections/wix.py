"""Wix adapter: a community's Wix app PUBLIC key + the one Wix site (app instance) it serves.

Wix delivers app webhooks as RS256 JWTs signed with the app key pair; svc-ingest verifies each
delivery against the public key stored here (``Scheme::WixJwt``) and refuses a token whose signed
``instanceId`` is not this connection's ``instance_id``.

* **Credential** -- the app public key (from the Wix app dashboard, "View ID and Keys"), supplied as
  the base64 body of the key with the ``BEGIN``/``END`` armor and newlines removed (the framework's
  token shape forbids whitespace). The adapter validates it structurally -- an RSA public key of at
  least 2048 bits -- and NEVER makes an outbound call: there is nothing secret to check against Wix.
  It is sealed at rest like every connection credential. A public key does not expire and is not
  refreshable (a rotated key is replaced via the normal credential-replace route, which bumps
  ``secret_version`` and restarts the route).
* **Resource** -- the Wix site, identified by ``metadata.instance_id`` (the app instance id Wix
  puts in every webhook). Non-secret; it becomes the connection's ``resource_id``.

Capability ``webhook_ingest`` is what the data-plane binding list is scoped to.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from typing import Any, Final

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

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
from services.provider_connections.errors import CredentialsRejected, RefreshNotConfigured

PROVIDER_ID: Final = "wix"
RESOURCE_TYPE_SITE: Final = "wix_site"
CAPABILITY_WEBHOOK_INGEST: Final = "webhook_ingest"
#: Wix instance ids are UUIDs; the pattern is a conservative superset (mirrors the Rust profile).
INSTANCE_ID_PATTERN: Final = r"[A-Za-z0-9-]{8,64}"
#: Smallest RSA modulus the host verifier accepts (ring ``RSA_PKCS1_2048_8192``).
MIN_RSA_BITS: Final = 2048
MAX_RSA_BITS: Final = 8192

WIX_SPEC: Final = AdapterSpec(
    provider=PROVIDER_ID,
    display_name="Wix",
    platform="wix",
    resource_type=RESOURCE_TYPE_SITE,
    install_kind=INSTALL_KIND_CREDENTIALS,
    capabilities=frozenset({CAPABILITY_WEBHOOK_INGEST}),
    required_scopes=(),
    credential_fields=(
        CredentialField(
            name="access_token",
            required=True,
            description=(
                "Wix app public key: the base64 body only (no BEGIN/END lines, no newlines)"
            ),
        ),
    ),
    metadata_fields=(
        MetadataField(
            name="instance_id",
            required=True,
            description="Wix app instance (site) id carried in every webhook",
            pattern=INSTANCE_ID_PATTERN,
            max_length=64,
        ),
    ),
    public_metadata_keys=frozenset({"instance_id"}),
    runtime_metadata_keys=frozenset({"instance_id"}),
    refreshable=False,
    binding_metadata=(BindingKey(wire="instance_id", stored="instance_id", required=True),),
)


def _rsa_public_key_der(token: str) -> bytes:
    """Decode ``token`` (base64 SPKI DER) or raise :class:`CredentialsRejected` (never echoed)."""
    try:
        der = base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError):
        raise CredentialsRejected("the Wix public key is not valid base64") from None
    try:
        key = load_der_public_key(der)
    except (ValueError, UnsupportedAlgorithm):
        raise CredentialsRejected("the Wix public key is not a DER public key") from None
    if not isinstance(key, rsa.RSAPublicKey):
        raise CredentialsRejected("the Wix public key must be an RSA key")
    if not MIN_RSA_BITS <= key.key_size <= MAX_RSA_BITS:
        raise CredentialsRejected("the Wix public key must be a 2048-8192 bit RSA key")
    return der


class WixAdapter:
    """Wix implementation of :class:`~services.provider_connections.adapter.ProviderAdapter`."""

    spec: AdapterSpec = WIX_SPEC

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """Keep the key as supplied (the shape check already forbids whitespace)."""
        return Credentials(access_token=credentials.access_token)

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """A public key carries no OAuth scopes: it grants exactly the spec's capabilities."""
        return self.spec.capabilities

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """Structural check only: the token must be an RSA public key (no outbound call)."""
        _rsa_public_key_der(credentials.access_token)
        return Account(
            account_id="wix-app-public-key",
            account_label="Wix app public key",
            app_id="",
            scopes=(),
            expires_at=None,
        )

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """The Wix site is whatever instance id the admin names; nothing to look up remotely."""
        instance_id = metadata["instance_id"]
        facts: dict[str, Any] = {"instance_id": instance_id}
        return ResolvedResource(resource_id=instance_id, facts=facts)

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """A public key has no refresh flow."""
        raise RefreshNotConfigured("Wix connections are not refreshable")
