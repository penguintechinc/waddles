"""Stream identity-field DEK broker behind ``KeyService.GetStreamDek`` (#442).

svc-ingest encrypts stream identity fields under a per-tenant DEK and svc-process decrypts
them; neither holds a KEK. This module decides whether a verified caller may have a tenant's
stream DEK and, if so, returns it sealed to the caller's per-request ephemeral X25519 key.

Key custody (no new primitives -- reuses :mod:`services.envelope`):

```
tenant stream DEK (keystore.tenant_encryption_keys, purpose='ingest-stream', per version)
  --wrapped by--> platform KEK (TENANT_KEK_HEX) | tenant's BYOK KMS key (Enterprise)
handed-out key = HKDF(DEK, tenant + lineage 'ingest-stream')     (never the raw DEK)
sealed_dek     = enc(32) || AES-256-GCM(k, n, key, aad=context)  (k||n = HKDF(ECDH, info=context))
context        = "<service>|<tenant_id>|<purpose>|<dek_version>"
```

Authorization (all fail closed, every outcome audited to ``keystore.stream_dek_issuances``):
the ``key:stream-dek:read`` scope is enforced by ``AuthInterceptor``; here the caller's SPIFFE
id must name ``svc-ingest`` or ``svc-process``; ``tenant_id`` must be a numeric tenant id
(never a slug) of an existing tenant; a tenant-bound token may only ask for its own tenant
(the operator-plane ``system`` tenant may name any). An issuance is only returned after its
audit row commits. No key material, sealed bytes or caller public key is ever logged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask_core.service_jwt import SYSTEM_TENANT
from opentelemetry import metrics, trace
from penguin_dal import AsyncDB

from services.bundle_install_dal import raw_sql_rows, raw_sql_write
from services.envelope.errors import (
    EnvelopeInputError,
    TenantKeyNotFoundError,
    TenantKeyUnavailableError,
)
from services.envelope.repository import PURPOSE_INGEST_STREAM
from services.envelope.service import TenantEnvelopeService

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("hub_api.stream_dek_broker")
_meter = metrics.get_meter("hub_api.stream_dek_broker")
_issuances = _meter.create_counter(
    name="waddles_stream_dek_requests",
    description="GetStreamDek outcomes by caller service and outcome (no tenant label).",
)
_latency = _meter.create_histogram(
    name="waddles_stream_dek_duration_ms",
    unit="ms",
    description="GetStreamDek broker latency by outcome.",
)

#: Scope `AuthInterceptor` requires for GetStreamDek (servicers.REQUIRED_SCOPES).
STREAM_DEK_SCOPE: Final = "key:stream-dek:read"
#: The only key lineages a caller may request (DB CHECK on `purpose` agrees).
ALLOWED_PURPOSES: Final = frozenset({PURPOSE_INGEST_STREAM})
#: Service names (last SPIFFE path segment) allowed to hold stream DEKs. ``svc-gateway`` is the
#: v3.0 rename of ``svc-ingest`` (same role, same tenant semantics, same SPIFFE_ENV pin); both are
#: accepted during the rename so the caller's sealed ctx always matches its own SVID segment.
ALLOWED_SERVICES: Final = frozenset({"svc-ingest", "svc-gateway", "svc-process"})
#: Default cache TTL for the unsealed DEK on the caller (matches svc-ingest's 10 min ceiling).
DEFAULT_TTL_SECONDS: Final = 600
_TTL_BOUNDS: Final = (60, 3600)
_TTL_ENV: Final = "STREAM_DEK_TTL_SECONDS"

DEK_LENGTH: Final = 32
X25519_KEY_LENGTH: Final = 32
NONCE_LENGTH: Final = 12

_SPIFFE_RE: Final = re.compile(r"^spiffe://penguintech\.io/([a-z0-9-]{1,32})/([a-z0-9-]{1,63})$")
#: This deployment's SPIFFE env segment (``spiffe://penguintech.io/<env>/<service>``). Unset
#: or malformed -> every caller is denied (fail closed); another env's SVID never matches.
_SPIFFE_ENV_VAR: Final = "SPIFFE_ENV"
_SPIFFE_ENV_RE: Final = re.compile(r"^[a-z0-9-]{1,32}$")
#: Domain-separation salt for hashing rejected subs into the audit table (never stored raw).
_AUDIT_SUB_SALT: Final = b"waddles.stream-dek.rejected-sub.v1|"
_AUDIT_SUB_PREFIX: Final = "rejected:"
_AUDIT_SUB_HEX: Final = 32
_TENANT_ID_RE: Final = re.compile(r"^[1-9][0-9]{0,9}$")
_MAX_TENANT_ID: Final = 2_147_483_647  # tenants.id is INTEGER

#: `keystore.stream_dek_issuances.outcome` int-enum codes (documented in migration 0101).
OUTCOME_ISSUED: Final = 1
OUTCOME_DENIED_SERVICE: Final = 2
OUTCOME_DENIED_TENANT: Final = 3
OUTCOME_INVALID: Final = 4
OUTCOME_NOT_FOUND: Final = 5
OUTCOME_UNAVAILABLE: Final = 6

_OUTCOME_LABEL: Final[Mapping[int, str]] = {
    OUTCOME_ISSUED: "issued",
    OUTCOME_DENIED_SERVICE: "denied_service",
    OUTCOME_DENIED_TENANT: "denied_tenant",
    OUTCOME_INVALID: "invalid",
    OUTCOME_NOT_FOUND: "not_found",
    OUTCOME_UNAVAILABLE: "unavailable",
}

_SQL_TENANT_EXISTS = "SELECT id FROM tenants WHERE id = :t"
_SQL_TENANT_BY_REF = (
    "SELECT id FROM tenants WHERE id::text = :r OR slug = :r ORDER BY (id::text = :r) DESC LIMIT 1"
)
_SQL_AUDIT = """
INSERT INTO keystore.stream_dek_issuances
    (tenant_id, purpose, dek_version, caller_service, outcome)
VALUES (:tenant_id, :purpose, :dek_version, :caller_service, :outcome)
RETURNING id
"""


class StreamDekError(Exception):
    """Base for broker refusals; `outcome` is the audited int code, message is PII/key-free."""

    outcome: int = OUTCOME_UNAVAILABLE


class StreamDekInvalidError(StreamDekError):
    """Malformed request (bad tenant id, purpose, version or public key)."""

    outcome = OUTCOME_INVALID


class StreamDekDeniedError(StreamDekError):
    """Caller identity or tenant binding does not permit this DEK."""

    outcome = OUTCOME_DENIED_TENANT


class StreamDekServiceDeniedError(StreamDekDeniedError):
    """Caller's SPIFFE identity is not an allowed stream-DEK holder."""

    outcome = OUTCOME_DENIED_SERVICE


class StreamDekNotFoundError(StreamDekError):
    """Requested DEK version does not exist (or was destroyed)."""

    outcome = OUTCOME_NOT_FOUND


class StreamDekUnavailableError(StreamDekError):
    """Key store / KEK / audit store unusable right now -- fail closed."""

    outcome = OUTCOME_UNAVAILABLE


@dataclass(slots=True, frozen=True)
class StreamDekRequest:
    """Validated-at-the-boundary inputs: request fields plus the verified token claims."""

    tenant_id: str
    purpose: str
    version: int
    caller_public_key: bytes
    claim_tenant: str
    caller_sub: str


@dataclass(slots=True, frozen=True)
class SealedStreamDek:
    """The response: sealed DEK (enc||ciphertext), its version, and the caller cache TTL."""

    sealed_dek: bytes
    version: int
    ttl_seconds: int


def ttl_seconds_from_env(environ: Mapping[str, str] | None = None) -> int:
    """Return the configured caller cache TTL, bounded; out-of-range config fails loudly."""
    raw = (environ if environ is not None else os.environ).get(_TTL_ENV)
    if raw is None or raw == "":
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{_TTL_ENV} must be an integer") from exc
    low, high = _TTL_BOUNDS
    if not low <= value <= high:
        raise ValueError(f"{_TTL_ENV} must be within [{low}, {high}]")
    return value


def deployment_spiffe_env(environ: Mapping[str, str] | None = None) -> str | None:
    """Return this deployment's SPIFFE env segment from ``SPIFFE_ENV``, or None if unset/bad."""
    raw = (environ if environ is not None else os.environ).get(_SPIFFE_ENV_VAR, "").strip()
    return raw if _SPIFFE_ENV_RE.fullmatch(raw) else None


def service_name(sub: str, environ: Mapping[str, str] | None = None) -> str:
    """Return the service name of an allowed caller in THIS deployment's env, else deny.

    The env segment is pinned to ``SPIFFE_ENV`` so an SVID minted for another env in the same
    trust domain is refused; with ``SPIFFE_ENV`` unset every caller is refused (fail closed).
    """
    env = deployment_spiffe_env(environ)
    if env is None:
        logger.error("stream_dek.spiffe_env_unset", extra={"var": _SPIFFE_ENV_VAR})
        raise StreamDekServiceDeniedError("caller is not permitted stream DEKs")
    match = _SPIFFE_RE.fullmatch(sub or "")
    if match is None or match.group(1) != env or match.group(2) not in ALLOWED_SERVICES:
        raise StreamDekServiceDeniedError("caller is not permitted stream DEKs")
    return match.group(2)


def audit_caller_ref(sub: str) -> str:
    """Salted, truncated SHA-256 of a rejected caller sub for the audit row (never the raw sub)."""
    if not sub:
        return ""
    digest = hashlib.sha256(_AUDIT_SUB_SALT + sub.encode("utf-8", "replace")).hexdigest()
    return _AUDIT_SUB_PREFIX + digest[:_AUDIT_SUB_HEX]


def seal_context(service: str, tenant_id: int, purpose: str, version: int) -> bytes:
    """The HKDF info + AEAD AAD binding a sealed DEK to its holder, tenant, purpose, version."""
    return f"{service}|{tenant_id}|{purpose}|{version}".encode("ascii")


def seal_dek(dek: bytes, recipient_public_key: bytes, context: bytes) -> bytes:
    """Seal `dek` to `recipient_public_key`: ``enc(32) || AES-256-GCM ciphertext+tag``.

    Fresh server X25519 keypair per call; ``HKDF-SHA256(salt=None, ikm=ECDH, info=context)``
    yields 32-byte key || 12-byte nonce; ``context`` is also the AAD. Byte-compatible with
    svc-ingest/svc-process ``hpke_seal_open`` (enc = first 32 bytes, ciphertext = the rest).
    """
    if len(dek) != DEK_LENGTH:
        raise StreamDekUnavailableError("data key has an unexpected length")
    if len(recipient_public_key) != X25519_KEY_LENGTH:
        raise StreamDekInvalidError("caller_ephemeral_public_key must be 32 bytes")
    try:
        peer = X25519PublicKey.from_public_bytes(recipient_public_key)
        server = X25519PrivateKey.generate()
        shared = server.exchange(peer)  # raises on an all-zero (low-order) result
    except ValueError as exc:
        raise StreamDekInvalidError("caller_ephemeral_public_key is not usable") from exc
    okm = HKDF(
        algorithm=hashes.SHA256(), length=DEK_LENGTH + NONCE_LENGTH, salt=None, info=context
    ).derive(shared)
    key, nonce = okm[:DEK_LENGTH], okm[DEK_LENGTH:]
    ciphertext = AESGCM(key).encrypt(nonce, dek, context)
    enc = server.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return enc + ciphertext


def _parse_tenant_id(raw: str) -> int:
    """Numeric tenant id only (never a slug), positive, within INTEGER range."""
    if not _TENANT_ID_RE.fullmatch(raw or "") or int(raw) > _MAX_TENANT_ID:
        raise StreamDekInvalidError("tenant_id must be a numeric tenant id")
    return int(raw)


class StreamDekBroker:
    """Authorize, fetch, seal and audit one stream DEK issuance."""

    def __init__(
        self,
        dal: AsyncDB,
        keys: TenantEnvelopeService,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Bind the hub-api DAL (tenants + audit table) and the ingest-stream key service."""
        self._dal = dal
        self._keys = keys
        self._ttl = ttl_seconds

    async def _authorize_tenant(self, claim_tenant: str, tenant_id: int) -> None:
        """Requested tenant must exist and match the token's tenant (or token is `system`)."""
        claim = claim_tenant.strip()
        if not claim:
            raise StreamDekDeniedError("tenant access denied")
        if claim == SYSTEM_TENANT:
            rows = await raw_sql_rows(self._dal, _SQL_TENANT_EXISTS, {"t": tenant_id})
            if not rows:
                raise StreamDekDeniedError("tenant access denied")
            return
        rows = await raw_sql_rows(self._dal, _SQL_TENANT_BY_REF, {"r": claim})
        if not rows or int(rows[0]["id"]) != tenant_id:
            # Same constant message for unknown and foreign tenants: no existence oracle.
            raise StreamDekDeniedError("tenant access denied")

    async def _audit(
        self, *, tenant_id: int | None, purpose: str, version: int | None, caller: str, outcome: int
    ) -> None:
        """Append one issuance/denial row; raises on failure (callers decide fail-closed)."""
        await raw_sql_write(
            self._dal,
            _SQL_AUDIT,
            {
                "tenant_id": tenant_id,
                "purpose": purpose if purpose in ALLOWED_PURPOSES else None,
                "dek_version": version,
                "caller_service": caller[:64] if caller else None,
                "outcome": outcome,
            },
        )

    async def issue(self, request: StreamDekRequest) -> SealedStreamDek:
        """Return the sealed DEK or raise a :class:`StreamDekError` subclass (always audited)."""
        started = time.monotonic()
        caller = ""
        tenant: int | None = None
        version: int | None = None
        outcome = OUTCOME_UNAVAILABLE
        audit_attempted = False
        with _tracer.start_as_current_span("stream_dek.issue") as span:
            try:
                caller = service_name(request.caller_sub)
                span.set_attribute("stream_dek.caller", caller)
                tenant_id = _parse_tenant_id(request.tenant_id)
                tenant = tenant_id
                if request.purpose not in ALLOWED_PURPOSES:
                    raise StreamDekInvalidError("purpose is not a stream key lineage")
                if len(request.caller_public_key) != X25519_KEY_LENGTH:
                    raise StreamDekInvalidError("caller_ephemeral_public_key must be 32 bytes")
                await self._authorize_tenant(request.claim_tenant, tenant_id)
                requested = request.version if request.version > 0 else None
                try:
                    dek_version, key = await self._keys.data_key(tenant_id, requested)
                except TenantKeyNotFoundError as exc:
                    raise StreamDekNotFoundError("stream DEK version not found") from exc
                except (TenantKeyUnavailableError, EnvelopeInputError) as exc:
                    raise StreamDekUnavailableError("stream DEK unavailable") from exc
                version = dek_version
                context = seal_context(caller, tenant_id, request.purpose, dek_version)
                sealed = seal_dek(key, request.caller_public_key, context)
                del key
                outcome = OUTCOME_ISSUED
                audit_attempted = True
                try:
                    await self._audit(
                        tenant_id=tenant_id,
                        purpose=request.purpose,
                        version=dek_version,
                        caller=caller,
                        outcome=outcome,
                    )
                except Exception as exc:
                    # No audit row, no key: an unaudited issuance is never returned.
                    logger.error(
                        "stream_dek.audit_failed",
                        extra={"caller": caller, "error": type(exc).__name__},
                        exc_info=True,
                    )
                    outcome = OUTCOME_UNAVAILABLE
                    raise StreamDekUnavailableError("stream DEK audit unavailable") from exc
                logger.info(
                    "stream_dek.issued",
                    extra={"caller": caller, "tenant_id": tenant_id, "dek_version": dek_version},
                )
                return SealedStreamDek(
                    sealed_dek=sealed, version=dek_version, ttl_seconds=self._ttl
                )
            except StreamDekError as exc:
                outcome = exc.outcome
                if not audit_attempted:  # the store just failed once; don't hammer it again
                    audited = caller or audit_caller_ref(request.caller_sub)
                    await self._audit_denial(tenant, request.purpose, version, audited, outcome)
                logger.warning(
                    "stream_dek.refused",
                    extra={
                        "caller": caller or "unknown",
                        "tenant_id": tenant,
                        "outcome": _OUTCOME_LABEL[outcome],
                    },
                )
                raise
            except Exception as exc:
                # Unexpected store/KMS failure: fail closed, logged with type + traceback.
                outcome = OUTCOME_UNAVAILABLE
                logger.error(
                    "stream_dek.failed",
                    extra={"caller": caller or "unknown", "error": type(exc).__name__},
                    exc_info=True,
                )
                if not audit_attempted:
                    audited = caller or audit_caller_ref(request.caller_sub)
                    await self._audit_denial(tenant, request.purpose, version, audited, outcome)
                raise StreamDekUnavailableError("stream DEK unavailable") from exc
            finally:
                label = _OUTCOME_LABEL.get(outcome, "unavailable")
                span.set_attribute("stream_dek.outcome", label)
                _issuances.add(1, {"caller": caller or "unknown", "outcome": label})
                _latency.record((time.monotonic() - started) * 1000.0, {"outcome": label})

    async def _audit_denial(
        self, tenant: int | None, purpose: str, version: int | None, caller: str, outcome: int
    ) -> None:
        """Best-effort denial audit: a failed write is logged loudly; the request stays denied."""
        try:
            await self._audit(
                tenant_id=tenant, purpose=purpose, version=version, caller=caller, outcome=outcome
            )
        except Exception as exc:
            logger.error(
                "stream_dek.denial_audit_failed",
                extra={"outcome": _OUTCOME_LABEL[outcome], "error": type(exc).__name__},
                exc_info=True,
            )


def broker_from_config(config: Mapping[str, Any]) -> StreamDekBroker | None:
    """Build the broker from the Quart app config, or ``None`` when its stores are absent."""
    runtime = config.get("envelope_runtime")
    dal = config.get("install_dal")
    if runtime is None or dal is None:
        return None
    return StreamDekBroker(dal, runtime.stream_service, ttl_seconds=ttl_seconds_from_env())
