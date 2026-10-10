"""Google Cloud KMS adapter -- customer-managed KEK on GCP.

**Trust model.** The customer owns a symmetric ``ENCRYPT_DECRYPT`` CryptoKey
and grants Waddles' *platform* service account
``roles/cloudkms.cryptoKeyEncrypterDecrypter`` on that one key (the principal
to grant is published in the deployment docs / ``GET .../kms``). No customer
credential is ever stored: Waddles authenticates with its *own* identity --
either a service-account key supplied to the pod from an existing Secret
(``ENVELOPE_GCP_CREDENTIALS_JSON``) or, on GKE/GCE, the workload's metadata-
server identity -- and the access token lives in memory only.

**Confused-deputy guard (proof of control).** GCP has no ExternalId, so the
customer proves they control the key instead: ``verify`` requires the key to
carry the label ``waddles-external-id=<ExternalId>`` -- the server-generated
token shown to the customer when they configure the key. Tenant M cannot
point their config at tenant V's key (even if V granted Waddles access for
V's own tenant) because M cannot set a label on V's key. The label is checked
on configure/activate, not on every unwrap: revocation is expressed by the
customer through IAM / disabling the key, which is what unwrap detects.

**The KEK never leaves KMS.** Only ``encrypt``/``decrypt`` of the 32-byte DEK
are performed, with ``additionalAuthenticatedData`` = the canonical wrap
context (tenant id + purpose), so a wrapped DEK copied to another tenant's
row fails to decrypt. Only the versionless CryptoKey name is accepted: Cloud
KMS selects the right key *version* on decrypt, so the customer can rotate
the key freely without Waddles tracking versions.

**Failure mapping** -- see :func:`map_http_error`. 403/404 and a 400
``FAILED_PRECONDITION`` (disabled / destroyed version) are a revocation
signal (:class:`KmsAccessDeniedError`); 429/5xx/timeouts are transient
(:class:`KmsUnavailableError`); our *own* credential failing is
:class:`KmsCredentialsError` (an outage on our side, never a customer
revocation). Only the short error status is kept, never the provider message.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

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
from services.envelope.kms_adapter import PROVIDER_GCP, AdapterFactory, CanonicalConfig, KmsAdapter
from services.envelope.models import KEK_KIND_CUSTOMER, KmsKeyInfo, TenantKmsConfig

PROVIDER_ID = PROVIDER_GCP

#: Label the customer sets on their key to prove control (see module docstring).
PROOF_LABEL = "waddles-external-id"

DEFAULT_API_BASE = "https://cloudkms.googleapis.com/v1"
DEFAULT_METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
_SCOPE = "https://www.googleapis.com/auth/cloudkms"
_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"

_KEY_NAME = re.compile(
    r"^projects/(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])"
    r"/locations/(?P<location>[a-z0-9-]{1,63})"
    r"/keyRings/(?P<ring>[A-Za-z0-9_-]{1,63})"
    r"/cryptoKeys/(?P<key>[A-Za-z0-9_-]{1,63})$"
)

#: A 400 with this status means the key (version) is disabled/destroyed.
_PRECONDITION = "FAILED_PRECONDITION"


def validate_gcp_config(key_ref: str, region: str | None, principal: str | None) -> CanonicalConfig:
    """Validate a tenant-supplied GCP config; ``region`` is derived from the key's location.

    Raises:
        KmsConfigError: not a CryptoKey resource name (a ``/cryptoKeyVersions/..``
            suffix is rejected), a `principal` was supplied (unused on GCP), or
            `region` disagrees with the key's location.
    """
    match = _KEY_NAME.match(key_ref or "")
    if match is None:
        raise KmsConfigError(
            "key_ref must be a CryptoKey resource name "
            "(projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>); "
            "key versions are not accepted"
        )
    if principal:
        raise KmsConfigError("principal is not used for gcp_kms; leave it empty")
    if region and region != match["location"]:
        raise KmsConfigError("region must match the location in key_ref")
    return CanonicalConfig(key_ref=key_ref, region=match["location"], principal=None)


@dataclass(slots=True, frozen=True)
class ServiceAccountKey:
    """The parts of a Google service-account key file the JWT-bearer flow needs."""

    client_email: str
    private_key: str = field(repr=False)
    private_key_id: str
    token_uri: str

    @classmethod
    def parse(cls, raw: str) -> ServiceAccountKey:
        """Parse and validate a service-account key JSON document.

        Raises:
            KmsConfigError: not JSON, not a ``service_account`` key, or a
                required field is missing -- raised at startup, not first use.
        """
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise KmsConfigError("ENVELOPE_GCP_CREDENTIALS_JSON is not valid JSON") from exc
        if not isinstance(data, dict) or data.get("type") != "service_account":
            raise KmsConfigError("ENVELOPE_GCP_CREDENTIALS_JSON must be a service_account key")
        values = {k: data.get(k) for k in ("client_email", "private_key", "token_uri")}
        if not all(isinstance(v, str) and v for v in values.values()):
            raise KmsConfigError(
                "ENVELOPE_GCP_CREDENTIALS_JSON is missing client_email/private_key/token_uri"
            )
        return cls(
            client_email=str(values["client_email"]),
            private_key=str(values["private_key"]),
            private_key_id=str(data.get("private_key_id") or ""),
            token_uri=str(values["token_uri"]),
        )


@dataclass(slots=True, frozen=True)
class GcpPlatformSettings:
    """Platform-side GCP wiring: which identity Waddles uses, and where Cloud KMS lives.

    Operator settings only -- never derived from tenant input. With no
    `service_account` the metadata-server identity (GKE Workload Identity /
    GCE) is used.
    """

    service_account: ServiceAccountKey | None = None
    api_base: str = DEFAULT_API_BASE
    metadata_url: str = DEFAULT_METADATA_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> GcpPlatformSettings:
        """Read ``ENVELOPE_GCP_*``; an invalid credentials document raises ``KmsConfigError``."""
        env = os.environ if environ is None else environ
        raw = env.get("ENVELOPE_GCP_CREDENTIALS_JSON", "").strip()
        api_base = env.get("ENVELOPE_GCP_KMS_ENDPOINT", DEFAULT_API_BASE).rstrip("/")
        if not api_base.startswith("https://"):
            raise KmsConfigError("ENVELOPE_GCP_KMS_ENDPOINT must be an https:// URL")
        return cls(
            service_account=ServiceAccountKey.parse(raw) if raw else None,
            api_base=api_base,
        )


class GcpRuntime:
    """Shared HTTP client + token cache for every GCP adapter in the process."""

    def __init__(
        self,
        settings: GcpPlatformSettings,
        *,
        http: SharedHttp | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind to the platform settings; `http`/`clock` are injectable for tests."""
        self.settings = settings
        self.http = http or SharedHttp()
        self._clock = clock
        self.tokens = TokenCache(self._fetch_token)

    async def _fetch_token(self) -> tuple[str, float]:
        """Obtain a Cloud KMS access token for Waddles' own identity."""
        client = self.http.client()
        account = self.settings.service_account
        try:
            if account is not None:
                now = int(self._clock())
                assertion = jwt.encode(
                    {
                        "iss": account.client_email,
                        "scope": _SCOPE,
                        "aud": account.token_uri,
                        "iat": now,
                        "exp": now + 3600,
                    },
                    account.private_key,
                    algorithm="RS256",
                    headers={"kid": account.private_key_id} if account.private_key_id else None,
                )
                response = await client.post(
                    account.token_uri, data={"grant_type": _JWT_BEARER, "assertion": assertion}
                )
            else:
                response = await client.get(
                    self.settings.metadata_url,
                    params={"scopes": _SCOPE},
                    headers={"Metadata-Flavor": "Google"},
                )
        except (jwt.PyJWTError, ValueError) as exc:
            raise KmsCredentialsError(
                "GCP service-account key could not sign a token request",
                code=type(exc).__name__,
            ) from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise KmsUnavailableError(
                "GCP token endpoint unavailable", code=str(response.status_code)
            )
        if response.status_code != 200:
            raise KmsCredentialsError(
                "GCP rejected the platform credentials",
                code=error_code(response, "error") or str(response.status_code),
            )
        try:
            body = response.json()
            return str(body["access_token"]), float(body.get("expires_in", 300))
        except (ValueError, KeyError, TypeError) as exc:
            raise KmsCredentialsError("GCP token response was malformed") from exc


def map_http_error(response: httpx.Response) -> KmsError:
    """Classify a non-2xx Cloud KMS response into the provider-neutral KMS error types."""
    status = response.status_code
    code = error_code(response, "error", "status") or str(status)
    if status in (403, 404) or (status == 400 and code == _PRECONDITION):
        return KmsAccessDeniedError(f"Cloud KMS denied the request ({code})", code=code)
    if status == 429 or status >= 500:
        return KmsUnavailableError(f"Cloud KMS transient error ({code})", code=code)
    return KmsRejectedError(f"Cloud KMS rejected the request ({code})", code=code)


@dataclass(slots=True, frozen=True)
class GcpKeySpec:
    """Everything needed to reach one customer key: tenant, CryptoKey name, ExternalId proof."""

    tenant_id: int
    key_name: str
    external_id: str


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: Any) -> bytes:
    try:
        return base64.b64decode(str(value), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KmsRejectedError("Cloud KMS returned malformed base64") from exc


class GcpKmsAdapter:
    """:class:`~services.envelope.kms_adapter.KmsAdapter` over one customer Cloud KMS key."""

    def __init__(self, spec: GcpKeySpec, runtime: GcpRuntime, *, timeout_s: float = 10.0) -> None:
        """Bind to one customer key; `runtime` carries the shared client and token cache."""
        self._spec = spec
        self._runtime = runtime
        self._timeout_s = timeout_s

    @property
    def kek_kind(self) -> str:
        """Always ``customer_kms``."""
        return KEK_KIND_CUSTOMER

    @property
    def key_ref(self) -> str:
        """The customer's CryptoKey resource name."""
        return self._spec.key_name

    async def _send(self, method: str, suffix: str, body: dict[str, str] | None) -> dict[str, Any]:
        """One authenticated call; refreshes the token once on a 401, maps errors, parses JSON."""
        url = f"{self._runtime.settings.api_base}/{self._spec.key_name}{suffix}"
        for attempt in (1, 2):
            token = await self._runtime.tokens.get()
            response = await self._runtime.http.client().request(
                method, url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            if response.status_code == 401:
                self._runtime.tokens.invalidate()
                if attempt == 1:
                    continue
                raise KmsCredentialsError(
                    "Cloud KMS rejected the platform access token", code="UNAUTHENTICATED"
                )
            if response.status_code >= 300:
                raise map_http_error(response)
            try:
                parsed = response.json()
            except ValueError as exc:
                raise KmsRejectedError("Cloud KMS returned a non-JSON body") from exc
            if not isinstance(parsed, dict):
                raise KmsRejectedError("Cloud KMS returned an unexpected body")
            return parsed
        raise KmsUnavailableError("Cloud KMS call could not be completed")  # pragma: no cover

    async def _call(
        self, name: str, method: str, suffix: str, body: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return await observed_call(
            PROVIDER_ID, name, self._timeout_s, lambda: self._send(method, suffix, body)
        )

    async def wrap(self, plaintext_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """``encrypt`` the DEK under the customer key, binding `context` as AAD."""
        result = await self._call(
            "wrap",
            "POST",
            ":encrypt",
            {
                "plaintext": _b64(plaintext_dek),
                "additionalAuthenticatedData": _b64(context_aad(context)),
            },
        )
        used = str(result.get("name", ""))
        ciphertext = _unb64(result.get("ciphertext", ""))
        if not used.startswith(f"{self._spec.key_name}/cryptoKeyVersions/") or not ciphertext:
            raise KmsRejectedError("Cloud KMS encrypted under an unexpected key")
        return ciphertext

    async def unwrap(self, wrapped_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """``decrypt`` a wrapped DEK, requiring the same AAD context used to wrap it."""
        result = await self._call(
            "unwrap",
            "POST",
            ":decrypt",
            {
                "ciphertext": _b64(wrapped_dek),
                "additionalAuthenticatedData": _b64(context_aad(context)),
            },
        )
        plaintext = _unb64(result.get("plaintext", ""))
        if len(plaintext) != KEY_LENGTH:
            raise KmsRejectedError("Cloud KMS returned an unexpected payload")
        return plaintext

    async def verify(self) -> KmsKeyInfo:
        """Preflight: key shape + proof-of-control label, then a wrap/unwrap probe.

        Raises:
            KmsConfigError: not an enabled symmetric ENCRYPT_DECRYPT key, or the
                ``waddles-external-id`` label is missing/wrong.
            KmsAccessDeniedError / KmsUnavailableError / KmsRejectedError:
                a Cloud KMS call failed (see module docstring).
        """
        metadata = await self._call("describe", "GET", "")
        if metadata.get("name") != self._spec.key_name:
            raise KmsConfigError("CryptoKey name does not match the key it resolves to")
        if metadata.get("purpose") != "ENCRYPT_DECRYPT":
            raise KmsConfigError("CryptoKey purpose must be ENCRYPT_DECRYPT (symmetric)")
        primary = metadata.get("primary") or {}
        if primary.get("algorithm") != "GOOGLE_SYMMETRIC_ENCRYPTION":
            raise KmsConfigError("CryptoKey must use GOOGLE_SYMMETRIC_ENCRYPTION")
        state = str(primary.get("state", "Unknown"))
        if state != "ENABLED":
            raise KmsConfigError(f"CryptoKey primary version state is {state}; it must be ENABLED")
        labels = metadata.get("labels") or {}
        proof = labels.get(PROOF_LABEL) if isinstance(labels, dict) else None
        if not isinstance(proof, str) or not hmac.compare_digest(
            proof.encode(), self._spec.external_id.encode()
        ):
            raise KmsConfigError(
                f"label {PROOF_LABEL} on the key must equal the ExternalId shown in Waddles"
            )
        probe = os.urandom(KEY_LENGTH)
        context = {CONTEXT_PURPOSE: PURPOSE_VERIFY, CONTEXT_TENANT_ID: str(self._spec.tenant_id)}
        round_trip = await self.unwrap(await self.wrap(probe, context=context), context=context)
        if not hmac.compare_digest(round_trip, probe):
            raise KmsRejectedError("KMS wrap/unwrap probe did not round-trip")
        return KmsKeyInfo(provider=PROVIDER_ID, key_ref=self._spec.key_name, key_state=state)


def gcp_adapter_factory(runtime: GcpRuntime, *, timeout_s: float = 10.0) -> AdapterFactory:
    """Build the registry factory turning a `TenantKmsConfig` into a :class:`GcpKmsAdapter`.

    `key_ref` overrides the config's key so rows still wrapped under a
    previous key stay readable until re-wrapped.
    """

    def factory(config: TenantKmsConfig, key_ref: str | None) -> KmsAdapter:
        canonical = validate_gcp_config(key_ref or config.key_ref, None, None)
        spec = GcpKeySpec(
            tenant_id=config.tenant_id,
            key_name=canonical.key_ref,
            external_id=config.external_id,
        )
        return GcpKmsAdapter(spec, runtime, timeout_s=timeout_s)

    return factory
