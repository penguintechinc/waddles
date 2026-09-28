"""
Per-Service EdDSA Machine JWTs
==============================

Replaces the platform-wide HS256 shared ``SECRET_KEY`` (see ``auth.py``) and
static API keys for service-to-service calls, starting with the new internal
PII endpoints (security.md Service-to-Service Auth). Each service is issued
a short-lived (<=1h) Ed25519-signed JWT scoped to specific ``scope`` values
instead of sharing one signing secret platform-wide.

Bootstrap (no shared secret): a calling service authenticates to the token
endpoint with its Kubernetes projected ServiceAccount token. hub-api
validates that token via the Kubernetes TokenReview API (in-cluster) and
maps the ServiceAccount identity to a fixed, allow-listed service id and
scope set -- it never trusts a client-supplied service id.

Migration path to SPIFFE/SPIRE via Skauswatch (JWT-SVIDs): this module is
built so that swap is a *configuration* change, not a rewrite --

- ``sub`` is already a real SPIFFE ID (``spiffe://penguintech.io/<env>/<svc>``,
  the platform's reserved pattern -- see ``penguintech.md`` SPIFFE Identity),
  and ``aud``/``kid``/JWKS shape match JWT-SVID conventions.
- Verification is decoupled from issuance via the ``TrustBundleSource``
  protocol. Today, ``ServiceJwtIssuer`` is both issuer and trust bundle
  (hub-api mints and verifies against its own JWKS). Retiring hub-api
  issuance later means pointing ``ServiceJwtVerifier`` at a
  ``SkauswatchTrustBundleSource`` (fetching Skauswatch's JWT-SVID bundle
  endpoint) instead -- ``require_service_scope`` and every caller of
  ``ServiceJwtVerifier.verify`` are unchanged.
- Lifetimes stay short (<=1h, default 15m) per JWT-SVID norms, so the
  eventual switch doesn't need a TTL renegotiation.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Protocol, TypeVar

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from flask import current_app, g, jsonify, request

logger = logging.getLogger(__name__)

#: Reserved SPIFFE trust domain (penguintech.md SPIFFE Identity) -- every
#: `sub` this module issues or verifies lives under it, today via hub-api
#: issuance, tomorrow via Skauswatch JWT-SVIDs, without changing the shape.
SPIFFE_TRUST_DOMAIN = "penguintech.io"
_SPIFFE_ID_RE = re.compile(r"^spiffe://penguintech\.io/[a-z0-9-]+/[a-z0-9-]+$")


def spiffe_id(env: str, service: str) -> str:
    """Build the reserved `spiffe://penguintech.io/<env>/<service>` ID."""
    candidate = f"spiffe://{SPIFFE_TRUST_DOMAIN}/{env}/{service}"
    if not _SPIFFE_ID_RE.match(candidate):
        raise ServiceJwtError(f"invalid SPIFFE id components: env={env!r} service={service!r}")
    return candidate

#: Machine JWTs are capped at 1h per security.md JWT Claims ("Machine access
#: tokens are short-lived (1h, same ceiling as JWT Claims above)").
MAX_TOKEN_TTL_SECONDS = 3600
DEFAULT_TOKEN_TTL_SECONDS = 900

#: Allowed clock skew when validating `iat`/`exp` across services.
CLOCK_SKEW_SECONDS = 30

ISSUER = os.getenv("SERVICE_JWT_ISSUER", "hub-api")


class ServiceJwtError(Exception):
    """Base class for every machine-JWT failure raised by this module."""


class UnknownKeyId(ServiceJwtError):
    """Token's `kid` does not match any key in the active JWKS."""


class BootstrapRejected(ServiceJwtError):
    """The presented ServiceAccount token did not map to a known service."""


class InvalidServiceToken(ServiceJwtError):
    """Token failed signature, `iss`, `aud`, `exp`, or `scope` validation."""


class TrustBundleSource(Protocol):
    """Pluggable source of verification public keys, by `kid`.

    Today ``ServiceJwtIssuer`` is the only implementation (hub-api verifies
    against the keys it signs with). Adopting Skauswatch/SPIRE JWT-SVIDs
    later means adding a ``SkauswatchTrustBundleSource`` that fetches and
    caches Skauswatch's bundle endpoint instead -- ``ServiceJwtVerifier``
    and every caller of it (``require_service_scope`` included) are
    unchanged, because they only ever depend on this protocol.
    """

    def get_public_key(self, kid: str) -> Ed25519PublicKey | None:
        """Return the public key for `kid`, or None if unknown/expired."""
        ...


@dataclass(slots=True)
class ServiceJwtVerifier:
    """Verifies machine JWTs against a pluggable `TrustBundleSource`.

    Decoupled from issuance on purpose: swapping hub-api-issued tokens for
    Skauswatch JWT-SVIDs later is "construct this with a different
    `TrustBundleSource`", not a rewrite of every `require_service_scope`
    call site.
    """

    trust_bundle: TrustBundleSource
    audience: str
    trusted_issuers: frozenset[str]

    def verify(self, token: str, *, required_scope: str) -> dict[str, Any]:
        """Validate signature, `iss`, `aud`, `exp` (with clock skew) and `scope`.

        Raises `UnknownKeyId` for an unrecognized `kid` and
        `InvalidServiceToken` for every other validation failure --
        callers should treat both as an authn/authz failure (401/403),
        never leak which check failed to the caller.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise InvalidServiceToken("malformed token header") from exc
        kid = header.get("kid")
        public_key = self.trust_bundle.get_public_key(kid) if kid else None
        if public_key is None:
            raise UnknownKeyId(f"unknown kid {kid!r}")
        try:
            payload = jwt.decode(
                token,
                public_key,
                algorithms=["EdDSA"],
                audience=self.audience,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "scope", "jti"], "verify_iss": False},
            )
        except jwt.InvalidTokenError as exc:
            raise InvalidServiceToken(str(exc)) from exc
        if payload.get("iss") not in self.trusted_issuers:
            raise InvalidServiceToken(f"untrusted issuer {payload.get('iss')!r}")
        if payload.get("scope") != required_scope:
            raise InvalidServiceToken(f"scope {payload.get('scope')!r} != required {required_scope!r}")
        return payload


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    """A single allow-listed caller, e.g. ``svc-process`` or ``svc-action``.

    ``service_id`` is a full SPIFFE ID (``spiffe://penguintech.io/<env>/<svc>``)
    on purpose so a future SPIFFE/Skauswatch bootstrap swaps in without
    changing callers of this class.
    """

    service_id: str
    k8s_namespace: str
    k8s_service_account: str
    allowed_scopes: frozenset[str]

    def matches_service_account(self, namespace: str, service_account: str) -> bool:
        """Return True if a validated TokenReview identity is this service."""
        return self.k8s_namespace == namespace and self.k8s_service_account == service_account


@dataclass(frozen=True, slots=True)
class SigningKey:
    """One Ed25519 keypair in the active JWKS, identified by `kid`.

    Holding both the private key (issuance) and public key (verification)
    lets a single hub-api process both mint and self-verify during
    rotation windows where an old `kid` is still accepted for verification
    but no longer used for issuance.
    """

    kid: str
    private_key: Ed25519PrivateKey | None  # gitleaks:allow -- type annotation, not a secret value
    public_key: Ed25519PublicKey


@dataclass(slots=True)
class ServiceJwtIssuer:
    """Issues and verifies Ed25519-signed per-service machine JWTs.

    ``keys`` holds every key still valid for verification, keyed by `kid`;
    ``active_kid`` selects which key new tokens are signed with, enabling
    zero-downtime rotation (publish the new public key everywhere, flip
    ``active_kid``, then drop the old key once its longest-lived token
    would have expired).
    """

    keys: dict[str, SigningKey]
    active_kid: str
    identities: dict[str, ServiceIdentity] = field(default_factory=dict)
    audience: str = "waddlebot-internal"

    def issue(self, service_id: str, scope: str, *, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS) -> str:
        """Mint a short-lived JWT for `service_id` scoped to `scope`.

        Raises `BootstrapRejected` if `service_id` isn't allow-listed or
        doesn't carry `scope`, and `ServiceJwtError` if `ttl_seconds`
        exceeds the 1h platform ceiling.
        """
        if ttl_seconds > MAX_TOKEN_TTL_SECONDS:
            raise ServiceJwtError(f"ttl_seconds {ttl_seconds} exceeds {MAX_TOKEN_TTL_SECONDS}s ceiling")
        identity = self.identities.get(service_id)
        if identity is None or scope not in identity.allowed_scopes:
            raise BootstrapRejected(f"{service_id!r} is not allow-listed for scope {scope!r}")
        key = self.keys[self.active_kid]
        if key.private_key is None:
            raise ServiceJwtError(f"active key {self.active_kid!r} has no private key loaded")
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "aud": self.audience,
            "sub": service_id,
            "scope": scope,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": str(uuid.uuid4()),
        }
        private_bytes = key.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        # PyJWT's EdDSA signer accepts a cryptography private-key object
        # directly; avoid ever holding the raw bytes longer than needed.
        del private_bytes
        return jwt.encode(payload, key.private_key, algorithm="EdDSA", headers={"kid": key.kid})

    def get_public_key(self, kid: str) -> Ed25519PublicKey | None:
        """Implements `TrustBundleSource` -- hub-api verifies its own JWKS today."""
        key = self.keys.get(kid)
        return key.public_key if key else None

    def as_verifier(self) -> "ServiceJwtVerifier":
        """Wrap this issuer as a `ServiceJwtVerifier` (self-verification today).

        Migration note: retiring hub-api issuance later means constructing
        `ServiceJwtVerifier` with a `SkauswatchTrustBundleSource` instead of
        `self` -- every `require_service_scope` call site is unaffected.
        """
        return ServiceJwtVerifier(trust_bundle=self, audience=self.audience, trusted_issuers=frozenset({ISSUER}))

    def jwks(self) -> dict[str, Any]:
        """Public-key set in JWKS-shaped form for cross-service verifiers."""
        keys = []
        for kid, key in self.keys.items():
            raw = key.public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            keys.append(
                {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "kid": kid,
                    "x": _b64url(raw),
                    "use": "sig",
                    "alg": "EdDSA",
                }
            )
        return {"keys": keys}


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def load_issuer_from_env(identities: list[ServiceIdentity]) -> ServiceJwtIssuer:
    """Build a `ServiceJwtIssuer` from env-sourced key material.

    `SERVICE_JWT_PRIVATE_KEY_<KID>` (PEM, PKCS8) supplies each signable
    key; `SERVICE_JWT_PUBLIC_KEY_<KID>` supplies verify-only keys kept
    around during rotation. Keys come from a k8s Secret projected as env
    vars -- never logged, never written to disk by this module.
    """
    active_kid = os.environ["SERVICE_JWT_ACTIVE_KID"]
    audience = os.getenv("SERVICE_JWT_AUDIENCE", "waddlebot-internal")
    keys: dict[str, SigningKey] = {}
    prefix = "SERVICE_JWT_PRIVATE_KEY_"
    for env_name, pem in os.environ.items():
        if not env_name.startswith(prefix):
            continue
        kid = env_name[len(prefix) :]
        private_key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ServiceJwtError(f"key {kid!r} is not an Ed25519 key")
        keys[kid] = SigningKey(kid=kid, private_key=private_key, public_key=private_key.public_key())
    pub_prefix = "SERVICE_JWT_PUBLIC_KEY_"
    for env_name, pem in os.environ.items():
        if not env_name.startswith(pub_prefix):
            continue
        kid = env_name[len(pub_prefix) :]
        if kid in keys:
            continue  # already have the private half, which carries the public key too
        public_key = serialization.load_pem_public_key(pem.encode())
        if not isinstance(public_key, Ed25519PublicKey):
            raise ServiceJwtError(f"key {kid!r} is not an Ed25519 key")
        keys[kid] = SigningKey(kid=kid, private_key=None, public_key=public_key)
    if active_kid not in keys or keys[active_kid].private_key is None:
        raise ServiceJwtError(f"active kid {active_kid!r} has no private key loaded")
    return ServiceJwtIssuer(
        keys=keys,
        active_kid=active_kid,
        identities={identity.service_id: identity for identity in identities},
        audience=audience,
    )


def verify_service_account_token(*, sa_token: str, audience: str, k8s_api_server: str, ca_cert_path: str) -> tuple[str, str]:
    """Validate a projected ServiceAccount token via the k8s TokenReview API.

    Returns `(namespace, service_account_name)` on success. This is the
    bootstrap credential: a calling pod authenticates with the SA token
    Kubernetes already injects (via a projected volume bound to
    `audience`), so hub-api never distributes a shared secret to mint the
    first machine JWT. Raises `BootstrapRejected` on any failure -- an
    expired, wrong-audience, or unauthenticated SA token, or a TokenReview
    API error, is treated identically as "no token" to the caller.
    """
    try:
        response = requests.post(
            f"{k8s_api_server}/apis/authentication.k8s.io/v1/tokenreviews",
            json={
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": sa_token, "audiences": [audience]},
            },
            headers={"Authorization": f"Bearer {_read_local_sa_token()}"},
            verify=ca_cert_path,
            timeout=5,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise BootstrapRejected("TokenReview request failed") from exc
    status = body.get("status", {})
    if not status.get("authenticated"):
        raise BootstrapRejected("ServiceAccount token not authenticated")
    if audience not in status.get("audiences", []):
        raise BootstrapRejected("ServiceAccount token missing required audience")
    username = status.get("user", {}).get("username", "")
    # `system:serviceaccount:<namespace>:<name>`
    parts = username.split(":")
    if len(parts) != 4 or parts[0] != "system" or parts[1] != "serviceaccount":
        raise BootstrapRejected(f"unexpected TokenReview username {username!r}")
    return parts[2], parts[3]


def _read_local_sa_token() -> str:
    """hub-api's own in-cluster SA token, used to call the TokenReview API."""
    path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    with open(path, encoding="ascii") as fh:
        return fh.read().strip()


F = TypeVar("F", bound=Callable[..., Any])


def require_service_scope(scope: str) -> Callable[[F], F]:
    """Flask route decorator requiring a valid machine JWT with `scope`.

    Reads `Authorization: Bearer <token>`, verifies it against
    `current_app.config["SERVICE_JWT_VERIFIER"]` (a `ServiceJwtVerifier` --
    swappable for a Skauswatch-backed trust bundle without touching this
    decorator), and stores the validated claims on `g.service_claims`.
    Returns 401 for a missing/invalid token and 403 for a valid token
    lacking `scope` -- mirrors `require_scope` in `authz.py` but for
    service-to-service calls (security.md: "middleware checks scopes
    only, never role names").
    """

    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            verifier: ServiceJwtVerifier = current_app.config["SERVICE_JWT_VERIFIER"]
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return jsonify({"error": "missing bearer token"}), 401
            token = auth_header[len("Bearer ") :]
            try:
                claims = verifier.verify(token, required_scope=scope)
            except UnknownKeyId:
                logger.warning("service_jwt.unknown_kid")
                return jsonify({"error": "unauthorized"}), 401
            except InvalidServiceToken as exc:
                logger.warning("service_jwt.invalid", extra={"reason": str(exc)})
                return jsonify({"error": "unauthorized"}), 401
            g.service_claims = claims
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
