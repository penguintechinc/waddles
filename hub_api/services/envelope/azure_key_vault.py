"""Azure Key Vault adapter -- customer-managed KEK on Azure (Key Vault or Managed HSM).

**Trust model (multi-tenant Entra app).** Waddles owns one multi-tenant Entra
application. The customer registers that application in *their* directory
(admin consent), creates an RSA key in *their* vault, and assigns the
application's service principal the ``Key Vault Crypto User`` role on that one
key/vault. Waddles then requests a client-credentials token from the
*customer's* directory (``principal`` = the customer's directory id), scoped
to Key Vault, and calls ``wrapkey`` / ``unwrapkey``. The application secret
is a platform secret (``ENVELOPE_AZURE_CLIENT_SECRET``, from an existing
Kubernetes Secret); no customer credential is stored, and tokens live in
memory only.

**Confused-deputy guard (proof of control).** ``verify`` requires the key to
carry the tag ``waddles-external-id=<ExternalId>`` -- the server-generated
token shown to the customer when they configure the key -- so tenant M cannot
point their config at tenant V's vault (V consented for V's own tenant).

**Context binding.** Key Vault's RSA wrap has no associated-data input, so
the wrap context is bound *inside* the payload: ``wrapkey`` seals
``DEK || SHA-256(context)`` and ``unwrap`` verifies that digest in constant
time before releasing the DEK. A wrapped DEK copied to another tenant's row
therefore fails to unwrap, exactly as with AWS/GCP's native context.

**Key versions.** ``unwrapkey`` must name the key *version* that wrapped the
blob, and the customer will rotate keys, so the wrapped DEK is a small
self-describing JSON document ``{"v":1,"kv":<version>,"ct":<ciphertext>}``.
``kek_ref`` stays the versionless key URL.

**SSRF posture.** The tenant-supplied key URL must match a strict
``https://<vault>.vault.azure.net/keys/<name>`` (or ``managedhsm``) pattern;
the host is never anything else, redirects are off, and the version parsed
from a response is validated as 32 hex chars before it can reach a URL.

**Failure mapping** -- see :func:`map_vault_error` / :func:`map_token_error`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from services.envelope._http import SharedHttp, TokenCache, error_code, observed_call
from services.envelope.crypto import (
    CONTEXT_PURPOSE,
    CONTEXT_TENANT_ID,
    KEY_LENGTH,
    PURPOSE_VERIFY,
    context_aad,
)
from services.envelope.errors import (
    KmsAccessDeniedError,
    KmsConfigError,
    KmsCredentialsError,
    KmsError,
    KmsRejectedError,
    KmsUnavailableError,
)
from services.envelope.kms_adapter import (
    PROVIDER_AZURE,
    AdapterFactory,
    CanonicalConfig,
    KmsAdapter,
)
from services.envelope.models import KEK_KIND_CUSTOMER, KmsKeyInfo, TenantKmsConfig

logger = logging.getLogger(__name__)

PROVIDER_ID = PROVIDER_AZURE

#: Tag the customer sets on their key to prove control (see module docstring).
PROOF_TAG = "waddles-external-id"

DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
API_VERSION = "7.4"
_ALG = "RSA-OAEP-256"
_MIN_RSA_BITS = 2048

_KEY_URL = re.compile(
    r"^https://(?P<vault>[a-z0-9][a-z0-9-]{1,22}[a-z0-9])\.(?P<kind>vault|managedhsm)\.azure\.net"
    r"/keys/(?P<name>[A-Za-z0-9-]{1,127})$"
)
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VERSION = re.compile(r"^[0-9a-f]{32}$")
_AUDIENCE = {"vault": "https://vault.azure.net", "managedhsm": "https://managedhsm.azure.net"}

#: AAD error codes meaning "the customer's directory does not trust / has not
#: consented to the Waddles application" -- a customer-side revocation signal.
_TENANT_DENIED_CODES = frozenset({700016, 65001, 7000112, 90002, 500011, 700082})
#: AAD error codes meaning the *platform's* application credential is bad.
_PLATFORM_CRED_CODES = frozenset({7000215, 7000222, 7000218, 70002, 7000216})


def validate_azure_config(
    key_ref: str, region: str | None, principal: str | None
) -> CanonicalConfig:
    """Validate a tenant-supplied Azure config; `principal` is the customer's directory id.

    Raises:
        KmsConfigError: key URL is not a plain ``https://<vault>.vault.azure.net/keys/<name>``
            (a version suffix is rejected), `principal` is not a directory GUID, or
            `region` was supplied (unused on Azure).
    """
    if region:
        raise KmsConfigError("region is not used for azure_key_vault; leave it empty")
    candidate = key_ref or ""
    if "://" in candidate:
        scheme, _, rest = candidate.partition("://")
        host, slash, path = rest.partition("/")
        candidate = f"{scheme}://{host.lower()}{slash}{path}"
    match = _KEY_URL.match(candidate)
    if match is None:
        raise KmsConfigError(
            "key_ref must be a Key Vault key URL "
            "(https://<vault>.vault.azure.net/keys/<name>); key versions are not accepted"
        )
    directory = (principal or "").strip().lower()
    if not _GUID.match(directory):
        raise KmsConfigError("principal must be the customer's Entra directory (tenant) id, a GUID")
    return CanonicalConfig(key_ref=candidate, region=None, principal=directory)


@dataclass(slots=True, frozen=True)
class AzurePlatformSettings:
    """Platform-side Azure wiring: the multi-tenant application identity and the login authority.

    `vault_url_rewrite` exists for tests only (it maps a canonical vault URL to
    a local mock); it has no environment variable on purpose, so production
    traffic can never be redirected away from ``*.azure.net``.
    """

    client_id: str
    client_secret: str = field(repr=False)
    authority: str = DEFAULT_AUTHORITY
    vault_url_rewrite: Callable[[str], str] | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AzurePlatformSettings:
        """Read ``ENVELOPE_AZURE_*``; missing/invalid values raise ``KmsConfigError`` at startup."""
        env = os.environ if environ is None else environ
        client_id = env.get("ENVELOPE_AZURE_CLIENT_ID", "").strip()
        secret = env.get("ENVELOPE_AZURE_CLIENT_SECRET", "").strip()
        if not client_id or not secret:
            raise KmsConfigError(
                "azure_key_vault is enabled but ENVELOPE_AZURE_CLIENT_ID / "
                "ENVELOPE_AZURE_CLIENT_SECRET are not set"
            )
        authority = env.get("ENVELOPE_AZURE_AUTHORITY", DEFAULT_AUTHORITY).rstrip("/")
        if not authority.startswith("https://"):
            raise KmsConfigError("ENVELOPE_AZURE_AUTHORITY must be an https:// URL")
        return cls(client_id=client_id, client_secret=secret, authority=authority)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(value: Any) -> bytes:
    text = str(value)
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise KmsRejectedError("Key Vault returned malformed base64") from exc


def map_vault_error(response: httpx.Response) -> KmsError:
    """Classify a non-2xx Key Vault response into the provider-neutral KMS error types."""
    status = response.status_code
    code = error_code(response, "error", "code") or str(status)
    if status in (403, 404):
        return KmsAccessDeniedError(f"Key Vault denied the request ({code})", code=code)
    if status == 429 or status >= 500:
        return KmsUnavailableError(f"Key Vault transient error ({code})", code=code)
    return KmsRejectedError(f"Key Vault rejected the request ({code})", code=code)


def map_token_error(response: httpx.Response) -> KmsError:
    """Classify a failed Entra token request (customer-side vs platform-side vs transient)."""
    status = response.status_code
    if status == 429 or status >= 500:
        return KmsUnavailableError("Entra token endpoint unavailable", code=str(status))
    try:
        body = response.json()
        codes = {int(c) for c in body.get("error_codes", []) if isinstance(c, int)}
    except (ValueError, AttributeError, TypeError):
        logger.debug("envelope.azure.token_error_body_unparsed", extra={"status": status})
        codes = set()
    label = error_code(response, "error") or str(status)
    if codes & _PLATFORM_CRED_CODES:
        return KmsCredentialsError("Entra rejected the platform application credential", code=label)
    if codes & _TENANT_DENIED_CODES:
        return KmsAccessDeniedError("customer directory does not trust the application", code=label)
    return KmsRejectedError("Entra rejected the token request", code=label)


class AzureRuntime:
    """Shared HTTP client + per-directory token caches for every Azure adapter in the process."""

    def __init__(self, settings: AzurePlatformSettings, *, http: SharedHttp | None = None) -> None:
        """Bind to the platform settings; `http` is injectable for tests."""
        self.settings = settings
        self.http = http or SharedHttp()
        self._tokens: dict[tuple[str, str], TokenCache] = {}

    def tokens(self, directory: str, kind: str) -> TokenCache:
        """Return the token cache for one (customer directory, vault kind) pair."""
        key = (directory, kind)
        cache = self._tokens.get(key)
        if cache is None:
            cache = TokenCache(lambda: self._fetch_token(directory, kind))
            self._tokens[key] = cache
        return cache

    def vault_url(self, canonical: str) -> str:
        """Apply the (test-only) URL rewrite to an outbound vault URL."""
        rewrite = self.settings.vault_url_rewrite
        return rewrite(canonical) if rewrite is not None else canonical

    async def _fetch_token(self, directory: str, kind: str) -> tuple[str, float]:
        """Client-credentials token for the customer's directory, scoped to Key Vault."""
        response = await self.http.client().post(
            f"{self.settings.authority}/{directory}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "scope": f"{_AUDIENCE[kind]}/.default",
            },
        )
        if response.status_code != 200:
            raise map_token_error(response)
        try:
            body = response.json()
            return str(body["access_token"]), float(body.get("expires_in", 300))
        except (ValueError, KeyError, TypeError) as exc:
            raise KmsRejectedError("Entra token response was malformed") from exc


@dataclass(slots=True, frozen=True)
class AzureKeySpec:
    """Everything needed to reach one customer key."""

    tenant_id: int
    key_url: str
    kind: str
    directory: str
    external_id: str


def _context_digest(context: Mapping[str, str]) -> bytes:
    return hashlib.sha256(context_aad(context)).digest()


def _encode_blob(version: str, ciphertext: bytes) -> bytes:
    return json.dumps({"v": 1, "kv": version, "ct": _b64url(ciphertext)}, sort_keys=True).encode()


def _decode_blob(blob: bytes) -> tuple[str, bytes]:
    try:
        doc = json.loads(blob)
        version, ciphertext = str(doc["kv"]), _unb64url(doc["ct"])
        valid = doc.get("v") == 1 and _VERSION.match(version) is not None and bool(ciphertext)
    except (ValueError, KeyError, TypeError) as exc:
        raise KmsRejectedError("wrapped DEK is not an Azure Key Vault wrap") from exc
    if not valid:
        raise KmsRejectedError("wrapped DEK is not an Azure Key Vault wrap")
    return version, ciphertext


class AzureKeyVaultAdapter:
    """:class:`~services.envelope.kms_adapter.KmsAdapter` over one customer Key Vault key."""

    def __init__(
        self, spec: AzureKeySpec, runtime: AzureRuntime, *, timeout_s: float = 10.0
    ) -> None:
        """Bind to one customer key; `runtime` carries the shared client and token caches."""
        self._spec = spec
        self._runtime = runtime
        self._timeout_s = timeout_s

    @property
    def kek_kind(self) -> str:
        """Always ``customer_kms``."""
        return KEK_KIND_CUSTOMER

    @property
    def key_ref(self) -> str:
        """The customer's versionless key URL."""
        return self._spec.key_url

    async def _send(self, method: str, suffix: str, body: dict[str, str] | None) -> dict[str, Any]:
        """One authenticated call; refreshes the token once on a 401, maps errors, parses JSON."""
        url = self._runtime.vault_url(f"{self._spec.key_url}{suffix}")
        tokens = self._runtime.tokens(self._spec.directory, self._spec.kind)
        for attempt in (1, 2):
            token = await tokens.get()
            response = await self._runtime.http.client().request(
                method,
                url,
                params={"api-version": API_VERSION},
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 401:
                tokens.invalidate()
                if attempt == 1:
                    continue
                raise KmsCredentialsError(
                    "Key Vault rejected the platform access token", code="Unauthorized"
                )
            if response.status_code >= 300:
                raise map_vault_error(response)
            try:
                parsed = response.json()
            except ValueError as exc:
                raise KmsRejectedError("Key Vault returned a non-JSON body") from exc
            if not isinstance(parsed, dict):
                raise KmsRejectedError("Key Vault returned an unexpected body")
            return parsed
        raise KmsUnavailableError("Key Vault call could not be completed")  # pragma: no cover

    async def _call(
        self, name: str, method: str, suffix: str, body: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return await observed_call(
            PROVIDER_ID, name, self._timeout_s, lambda: self._send(method, suffix, body)
        )

    def _kid_version(self, kid: Any) -> str:
        """Extract the key version from a ``kid`` URL, requiring it to be OUR key."""
        prefix = f"{self._spec.key_url}/"
        text = str(kid)
        if not text.lower().startswith(prefix.lower()):
            raise KmsRejectedError("Key Vault answered for an unexpected key")
        version = text[len(prefix) :]
        if not _VERSION.match(version):
            raise KmsRejectedError("Key Vault returned an unexpected key version")
        return version

    async def wrap(self, plaintext_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """``wrapkey`` the DEK (plus a context digest) under the customer key."""
        payload = plaintext_dek + _context_digest(context)
        result = await self._call(
            "wrap", "POST", "/wrapkey", {"alg": _ALG, "value": _b64url(payload)}
        )
        version = self._kid_version(result.get("kid", ""))
        ciphertext = _unb64url(result.get("value", ""))
        if not ciphertext:
            raise KmsRejectedError("Key Vault returned an empty wrap")
        return _encode_blob(version, ciphertext)

    async def unwrap(self, wrapped_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """``unwrapkey`` with the recorded key version, then verify the bound context digest."""
        version, ciphertext = _decode_blob(wrapped_dek)
        result = await self._call(
            "unwrap",
            "POST",
            f"/{version}/unwrapkey",
            {"alg": _ALG, "value": _b64url(ciphertext)},
        )
        payload = _unb64url(result.get("value", ""))
        if len(payload) != KEY_LENGTH + 32:
            raise KmsRejectedError("Key Vault returned an unexpected payload")
        dek, digest = payload[:KEY_LENGTH], payload[KEY_LENGTH:]
        if not hmac.compare_digest(digest, _context_digest(context)):
            raise KmsRejectedError("wrapped DEK was sealed for a different context")
        return dek

    async def verify(self) -> KmsKeyInfo:
        """Preflight: key shape + proof-of-control tag, then a wrap/unwrap probe.

        Raises:
            KmsConfigError: not an enabled RSA (>= 2048-bit) key permitting
                wrapKey/unwrapKey, or the ``waddles-external-id`` tag is missing/wrong.
            KmsAccessDeniedError / KmsUnavailableError / KmsRejectedError:
                a Key Vault call failed (see module docstring).
        """
        metadata = await self._call("describe", "GET", "")
        key = metadata.get("key") or {}
        attributes = metadata.get("attributes") or {}
        self._kid_version(key.get("kid", ""))
        if not str(key.get("kty", "")).upper().startswith("RSA"):
            raise KmsConfigError("Key Vault key must be an RSA key")
        ops = {str(op) for op in key.get("key_ops", [])}
        if not {"wrapKey", "unwrapKey"} <= ops:
            raise KmsConfigError("Key Vault key must permit wrapKey and unwrapKey")
        if len(_unb64url(key.get("n", ""))) * 8 < _MIN_RSA_BITS:
            raise KmsConfigError("Key Vault RSA key must be at least 2048 bits")
        if attributes.get("enabled") is not True:
            raise KmsConfigError("Key Vault key is disabled; it must be enabled")
        expires = attributes.get("exp")
        if isinstance(expires, int) and expires <= time.time():
            raise KmsConfigError("Key Vault key has expired")
        tags = metadata.get("tags") or {}
        proof = tags.get(PROOF_TAG) if isinstance(tags, dict) else None
        if not isinstance(proof, str) or not hmac.compare_digest(
            proof.encode(), self._spec.external_id.encode()
        ):
            raise KmsConfigError(
                f"tag {PROOF_TAG} on the key must equal the ExternalId shown in Waddles"
            )
        probe = os.urandom(KEY_LENGTH)
        context = {CONTEXT_PURPOSE: PURPOSE_VERIFY, CONTEXT_TENANT_ID: str(self._spec.tenant_id)}
        round_trip = await self.unwrap(await self.wrap(probe, context=context), context=context)
        if not hmac.compare_digest(round_trip, probe):
            raise KmsRejectedError("KMS wrap/unwrap probe did not round-trip")
        return KmsKeyInfo(provider=PROVIDER_ID, key_ref=self._spec.key_url, key_state="Enabled")


def azure_adapter_factory(runtime: AzureRuntime, *, timeout_s: float = 10.0) -> AdapterFactory:
    """Build the registry factory turning a `TenantKmsConfig` into an adapter.

    `key_ref` overrides the config's key so rows still wrapped under a
    previous key stay readable until re-wrapped.
    """

    def factory(config: TenantKmsConfig, key_ref: str | None) -> KmsAdapter:
        canonical = validate_azure_config(key_ref or config.key_ref, None, config.principal)
        match = _KEY_URL.match(canonical.key_ref)
        if match is None:  # pragma: no cover - validate_azure_config already guarantees this
            raise KmsConfigError("invalid Key Vault key URL")
        spec = AzureKeySpec(
            tenant_id=config.tenant_id,
            key_url=canonical.key_ref,
            kind=match["kind"],
            directory=canonical.principal or "",
            external_id=config.external_id,
        )
        return AzureKeyVaultAdapter(spec, runtime, timeout_s=timeout_s)

    return factory
