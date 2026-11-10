"""`ServiceAuthService`: the machine-JWT bootstrap over gRPC.

Replaces the REST `POST /internal/service-token` / `GET /internal/service-jwks.json` routes
(gRPC for all intra-cluster calls). Both RPCs run *before* a caller holds a machine JWT, so
`AuthInterceptor` lets them through (`BOOTSTRAP_METHODS`) and this servicer authenticates
`MintServiceToken` itself, with the credential the REST route used: the caller's projected,
audience-bound Kubernetes ServiceAccount token (`authorization: Bearer <sa-token>` metadata,
validated via TokenReview) mapped onto an allow-listed `ServiceIdentity`. The channel is TLS
(server.py) and, when `GRPC_TLS_CLIENT_CA_PATH` is set, mTLS as well. `GetServiceJwks` serves
public keys only and needs no credential.

Every rejection is a fixed, PII-free status string; the SA token and the minted JWT are never
logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

import grpc
from flask_core.service_jwt import (
    BOUND_TOKEN_TTL_SECONDS,
    COMMUNITY_BOUND_SCOPES,
    DEFAULT_TOKEN_TTL_SECONDS,
    SCOPE_OVERLAY_PUSH,
    BootstrapRejected,
    ServiceJwtIssuer,
    overlay_push_scope,
    verify_service_account_token,
)
from waddles.hub.internal.v1 import service_auth_pb2, service_auth_pb2_grpc

logger = logging.getLogger(__name__)

_MINT_METHOD = "/waddles.hub.internal.v1.ServiceAuthService/MintServiceToken"
_JWKS_METHOD = "/waddles.hub.internal.v1.ServiceAuthService/GetServiceJwks"

#: RPCs reachable without a machine JWT (they are what mints / verifies one).
BOOTSTRAP_METHODS: frozenset[str] = frozenset({_MINT_METHOD, _JWKS_METHOD})

#: Audience the bootstrap ServiceAccount token projection must carry
#: (Helm: `serviceJwt.bootstrapAudience` on each caller's pod spec).
BOOTSTRAP_AUDIENCE = os.getenv("SERVICE_JWT_BOOTSTRAP_AUDIENCE", "hub-api")
K8S_API_SERVER = os.getenv("KUBERNETES_SERVICE_HOST_URL", "https://kubernetes.default.svc")
K8S_CA_CERT_PATH = os.getenv(
    "KUBERNETES_CA_CERT_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
)

_SUPPORTED_API_VERSIONS = frozenset({"", "v1"})


class _BindingError(ValueError):
    """The request's community/connection binding fields are invalid for its scope."""


def binding_claims(request: service_auth_pb2.MintServiceTokenRequest) -> dict[str, int] | None:
    """Translate the request's binding fields into issuer ``extra_claims``.

    ``connections:resolve`` / ``:resolve:static`` / ``presentation.overlay:push`` REQUIRE
    ``community_id`` (non-negative; ``connection_id`` optional, never for push) -- those tokens
    are always community-bound. Every other scope (``connections:list`` included) must not carry
    either field. Raises ``_BindingError`` on any violation.
    """
    has_community = request.HasField("community_id")
    has_connection = request.HasField("connection_id")
    if request.scope not in COMMUNITY_BOUND_SCOPES:
        if has_community or has_connection:
            raise _BindingError("binding fields not accepted for this scope")
        return None
    if not has_community:
        raise _BindingError("community_id is required")
    if request.community_id < 0 or (has_connection and request.connection_id < 0):
        raise _BindingError("binding ids must be non-negative")
    claims = {"community": request.community_id}
    if has_connection:
        claims["conn"] = request.connection_id
    return claims


class ServiceAuthServicer(service_auth_pb2_grpc.ServiceAuthServiceServicer):  # type: ignore[misc]
    """Adapter for `ServiceAuthService` over the single process-wide `ServiceJwtIssuer`."""

    def __init__(
        self,
        issuer: ServiceJwtIssuer,
        *,
        audit_logger: Any = None,
        token_reviewer: Callable[..., tuple[str, str]] | None = None,
    ) -> None:
        """Bind the issuer, the AAA audit logger and (tests only) a TokenReview stand-in."""
        self._issuer = issuer
        self._audit_logger = audit_logger
        self._token_reviewer = token_reviewer

    async def _reject(
        self, context: grpc.aio.ServicerContext[Any, Any], event: str, **extra: Any
    ) -> None:
        """Log a fixed event and abort UNAUTHENTICATED without saying which check failed."""
        logger.warning(event, extra=extra or None)
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")

    async def MintServiceToken(  # noqa: N802 - gRPC generated method name
        self,
        request: service_auth_pb2.MintServiceTokenRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> service_auth_pb2.MintServiceTokenResponse:
        """Exchange the caller's projected SA token for a (possibly community-bound) JWT."""
        if request.api_version not in _SUPPORTED_API_VERSIONS:
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED, f"api_version {request.api_version} not supported"
            )
        raw = dict(context.invocation_metadata() or ()).get("authorization", "")
        auth_header = raw.decode() if isinstance(raw, bytes) else raw
        if not auth_header.startswith("Bearer ") or not auth_header[len("Bearer ") :]:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing bearer token")
        sa_token = auth_header[len("Bearer ") :]
        scope = request.scope
        if not scope:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "scope is required")
        try:
            extra_claims = binding_claims(request)
        except _BindingError:
            # Fixed event only: the rejected fields are caller-controlled, never logged.
            logger.warning("service_jwt.invalid_binding_request")
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid request")

        reviewer = self._token_reviewer or verify_service_account_token
        try:
            # TokenReview is a blocking `requests` call -- keep it off the event loop.
            namespace, service_account = await asyncio.to_thread(
                reviewer,
                sa_token=sa_token,
                audience=BOOTSTRAP_AUDIENCE,
                k8s_api_server=K8S_API_SERVER,
                ca_cert_path=K8S_CA_CERT_PATH,
            )
        except BootstrapRejected:
            await self._reject(context, "service_jwt.bootstrap_rejected")

        matched = next(
            (
                identity
                for identity in self._issuer.identities.values()
                if identity.matches_service_account(namespace, service_account)
            ),
            None,
        )
        if matched is None:
            await self._reject(
                context,
                "service_jwt.no_matching_identity",
                namespace=namespace,
                service_account=service_account,
            )
            raise AssertionError("unreachable")

        # An overlay PUSH request names the BASE scope; the token carries the per-community scope
        # svc-presentation's verifier requires (`overlay_auth::push_scope`). Never a connection id.
        minted_scope = scope
        if scope == SCOPE_OVERLAY_PUSH:
            if extra_claims is None or "conn" in extra_claims:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid request")
            minted_scope = overlay_push_scope(extra_claims["community"])

        try:
            token, claims = self._issuer.issue_with_claims(
                matched.service_id,
                minted_scope,
                ttl_seconds=BOUND_TOKEN_TTL_SECONDS if extra_claims else DEFAULT_TOKEN_TTL_SECONDS,
                extra_claims=extra_claims,
            )
        except BootstrapRejected:
            await self._reject(context, "service_jwt.issue_rejected")
            raise AssertionError("unreachable") from None

        # security.md audit logging: who, what scope, which token by `jti`, when it expires --
        # never the token itself. Claims come from what the issuer just signed.
        if self._audit_logger is not None:
            self._audit_logger.audit(
                action="service_jwt.issue",
                user=matched.service_id,
                community="internal",
                result="SUCCESS",
                scope=scope,
                tenant=claims.get("tenant"),
                jti=claims.get("jti"),
                exp=claims.get("exp"),
                community_id=claims.get("community"),
                connection_id=claims.get("conn"),
            )
        return service_auth_pb2.MintServiceTokenResponse(
            token=token, expires_in=claims["exp"] - claims["iat"]
        )

    async def GetServiceJwks(  # noqa: N802 - gRPC generated method name
        self,
        request: service_auth_pb2.GetServiceJwksRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> service_auth_pb2.GetServiceJwksResponse:
        """Public-key set for verifiers -- no credential required (public keys only)."""
        if request.api_version not in _SUPPORTED_API_VERSIONS:
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED, f"api_version {request.api_version} not supported"
            )
        keys = [
            service_auth_pb2.Jwk(
                kid=k["kid"],
                kty=k["kty"],
                crv=k["crv"],
                alg=k["alg"],
                use=k.get("use", "sig"),
                x=k["x"],
                y=k.get("y", ""),
            )
            for k in self._issuer.jwks()["keys"]
        ]
        return service_auth_pb2.GetServiceJwksResponse(keys=keys)
