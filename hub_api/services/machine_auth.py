"""Machine-identity route guard shared by hub-api's internal (data plane) blueprints.

One implementation of "valid service identity, exact scope, operator-plane ``system`` tenant" so
every internal route family fails closed identically. Two mechanisms, no static shared secret:

* **JWT** -- a short-lived Ed25519 machine JWT (``Authorization: Bearer``). With
  ``bind_community=True`` the token must carry a ``community`` claim (and ``conn`` when it names one
  connection) equal to the route's ``community_id`` / ``connection_id``, so a token minted for
  community A can never act on community B.
* **SVID** -- a verified X.509-SVID peer identity forwarded by the mTLS listener in the header named
  by ``SPIFFE_PEER_ID_HEADER``. Honoured ONLY when ``SPIFFE_PEER_TRUST`` is true (the request really
  arrived on that listener and the edge strips the header from everything else); default OFF means
  the header is ignored outright. The SPIFFE id is mapped through the same allow-listed
  ``ServiceIdentity`` scope + tenant policy the JWT path uses.

The ``system`` tenant is stamped by the issuer / allow-list (never by the caller) and required
because these routes deliberately span tenants. Failure bodies are generic -- never which check.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Collection
from functools import wraps
from typing import Any, Final, LiteralString

from flask_core.service_jwt import (
    SYSTEM_TENANT,
    InvalidServiceToken,
    ServiceJwtIssuer,
    UnknownKeyId,
)
from opentelemetry import metrics
from quart import current_app, g, request

from services.provider_connections.telemetry import log_failure

logger = logging.getLogger(__name__)

#: Header the SVID path reads the caller-asserted community from (checked against the route's).
COMMUNITY_PARAM_HEADER: Final = "X-Connection-Community"

_SPIFFE_ID_RE: Final = re.compile(r"spiffe://penguintech\.io/[a-z0-9-]+/[a-z0-9-]+")
_DIGITS_RE: Final = re.compile(r"[0-9]{1,18}")

_MECH_JWT: Final = "jwt"
_MECH_SVID: Final = "svid"
_OUT_OK: Final = "ok"
_OUT_UNCONFIGURED: Final = "unconfigured"
_OUT_UNAUTHORIZED: Final = "unauthorized"
_OUT_FORBIDDEN: Final = "forbidden"

_meter = metrics.get_meter("waddles.hub_api.machine_auth")
_auth_counter = _meter.create_counter(
    "machine_auth_total",
    description="Machine authentication attempts by mechanism (jwt|svid) and outcome.",
)

_UNAUTHORIZED: Final = {"error": "unauthorized"}
_FORBIDDEN: Final = {"error": "forbidden"}


def _count(mechanism: str, outcome: str) -> None:
    """Bump ``machine_auth_total`` with bounded-enum labels only."""
    _auth_counter.add(1, {"mechanism": mechanism, "outcome": outcome})


def _as_int(value: Any) -> int | None:
    """Return ``value`` as an int only if it already is one (bool refused), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _binding_matches(claim_community: int, claim_conn: int | None, kwargs: dict[str, Any]) -> bool:
    """True iff the route's ``community_id`` (and ``connection_id`` when bound) equal the claims."""
    if _as_int(kwargs.get("community_id")) != claim_community:
        return False
    if claim_conn is None:
        return True
    return _as_int(kwargs.get("connection_id")) == claim_conn


def _svid_peer_id() -> str | None:
    """The forwarded SVID id, or ``None`` unless peer trust is on AND the header is present."""
    if current_app.config.get("SPIFFE_PEER_TRUST") is not True:
        return None
    header = current_app.config.get("SPIFFE_PEER_ID_HEADER") or ""
    if not header:
        return None
    return request.headers.get(header)


def machine_authenticated(
    required_scope: str | Collection[str],
    *,
    unconfigured_event: LiteralString,
    rejected_event: LiteralString,
    wrong_tenant_event: LiteralString,
    bind_community: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Build a decorator requiring a machine identity holding ``required_scope`` from ``system``.

    Fails closed at every step and never says which check failed: no verifier configured -> 503
    (a deployment fault to be seen, never a fallback to a weaker mechanism), missing/invalid/
    wrong-scope credential -> 401, valid credential from a non-``system`` tenant -> 403. With
    ``bind_community`` a missing/garbled community binding -> 401 and a binding that does not equal
    the route's ``community_id`` (or ``connection_id``) -> 403.

    ``required_scope`` may be a collection: a credential holding ANY one of them is accepted, and
    the scopes the caller actually holds are published on ``quart.g.machine_scopes`` so a handler
    can apply a finer, per-resource check (least privilege by connection kind).
    """
    accepted = (required_scope,) if isinstance(required_scope, str) else tuple(required_scope)

    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(handler)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            verifier = current_app.config.get("SERVICE_JWT_VERIFIER")
            if verifier is None:
                logger.error(unconfigured_event)
                _count(_MECH_JWT, _OUT_UNCONFIGURED)
                return {"error": "service authentication is not configured"}, 503
            header = request.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                return await _jwt_path(
                    handler,
                    args,
                    kwargs,
                    verifier,
                    header[len("Bearer ") :],
                    accepted,
                    bind_community,
                    rejected_event,
                    wrong_tenant_event,
                )
            peer = _svid_peer_id()
            if peer is not None:
                return await _svid_path(
                    handler,
                    args,
                    kwargs,
                    peer,
                    accepted,
                    bind_community,
                    unconfigured_event,
                    rejected_event,
                    wrong_tenant_event,
                )
            _count(_MECH_JWT, _OUT_UNAUTHORIZED)
            return _UNAUTHORIZED, 401

        return wrapper

    return decorator


async def _jwt_path(
    handler: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    verifier: Any,
    token: str,
    required_scope: tuple[str, ...],
    bind_community: bool,
    rejected_event: LiteralString,
    wrong_tenant_event: LiteralString,
) -> Any:
    """Verify a bearer machine JWT, enforce tenant + (optionally) community binding, then call."""
    claims: dict[str, Any] | None = None
    failure: Exception | None = None
    for scope in required_scope:
        try:
            claims = verifier.verify(token, required_scope=scope)
            break
        except (UnknownKeyId, InvalidServiceToken) as exc:
            failure = exc
    if claims is None:
        log_failure(
            logger,
            rejected_event,
            failure or InvalidServiceToken("no scope"),
            level=logging.WARNING,
        )
        _count(_MECH_JWT, _OUT_UNAUTHORIZED)
        return _UNAUTHORIZED, 401
    g.machine_scopes = frozenset({str(claims.get("scope"))})
    if claims.get("tenant") != SYSTEM_TENANT:
        logger.warning(wrong_tenant_event)
        _count(_MECH_JWT, _OUT_FORBIDDEN)
        return _FORBIDDEN, 403
    if bind_community:
        claim_community = _as_int(claims.get("community"))
        claim_conn = _as_int(claims.get("conn")) if "conn" in claims else None
        if claim_community is None or ("conn" in claims and claim_conn is None):
            logger.warning(rejected_event)
            _count(_MECH_JWT, _OUT_UNAUTHORIZED)
            return _UNAUTHORIZED, 401
        if not _binding_matches(claim_community, claim_conn, kwargs):
            # Ids only -- never the token. jti/tenant/community of the *presented* token.
            logger.warning(
                wrong_tenant_event,
                extra={
                    "jti": claims.get("jti"),
                    "tenant": claims.get("tenant"),
                    "community": claim_community,
                },
            )
            _count(_MECH_JWT, _OUT_FORBIDDEN)
            return _FORBIDDEN, 403
    _count(_MECH_JWT, _OUT_OK)
    return await handler(*args, **kwargs)


async def _svid_path(
    handler: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    peer: str,
    required_scope: tuple[str, ...],
    bind_community: bool,
    unconfigured_event: LiteralString,
    rejected_event: LiteralString,
    wrong_tenant_event: LiteralString,
) -> Any:
    """Map a trusted peer SPIFFE id through the allow-list; same scope/tenant/binding policy."""
    issuer: ServiceJwtIssuer | None = current_app.config.get("SERVICE_JWT_ISSUER")
    if issuer is None:
        logger.error(unconfigured_event)
        _count(_MECH_SVID, _OUT_UNCONFIGURED)
        return {"error": "service authentication is not configured"}, 503
    identity = issuer.identities.get(peer) if _SPIFFE_ID_RE.fullmatch(peer) else None
    if identity is None or not any(scope in identity.allowed_scopes for scope in required_scope):
        logger.warning(rejected_event)
        _count(_MECH_SVID, _OUT_UNAUTHORIZED)
        return _UNAUTHORIZED, 401
    if identity.tenant != SYSTEM_TENANT:
        logger.warning(wrong_tenant_event)
        _count(_MECH_SVID, _OUT_FORBIDDEN)
        return _FORBIDDEN, 403
    if bind_community:
        raw = request.headers.get(COMMUNITY_PARAM_HEADER, "")
        if not _DIGITS_RE.fullmatch(raw):
            logger.warning(rejected_event)
            _count(_MECH_SVID, _OUT_UNAUTHORIZED)
            return _UNAUTHORIZED, 401
        if _as_int(kwargs.get("community_id")) != int(raw):
            logger.warning(wrong_tenant_event)
            _count(_MECH_SVID, _OUT_FORBIDDEN)
            return _FORBIDDEN, 403
    g.machine_scopes = frozenset(identity.allowed_scopes)
    _count(_MECH_SVID, _OUT_OK)
    return await handler(*args, **kwargs)
