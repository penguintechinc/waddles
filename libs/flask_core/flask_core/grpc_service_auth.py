"""Shared gRPC server interceptor for EdDSA machine-JWT service-to-service auth.

Replaces the per-module copy-pasted ``grpc_auth_interceptor.py`` files that
verified an HS256 JWT against the fleet-wide ``MODULE_SECRET_KEY``. A module
cutover is: build a verify-only verifier (`service_jwt.load_verifier_from_env`),
register this interceptor with one module-specific scope and a caller allow-list.
Every failure denies; there is no HS256/shared-secret branch.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import grpc

from .service_jwt import (
    InvalidServiceToken,
    ServiceJwtError,
    ServiceJwtVerifier,
    UnknownKeyId,
)

logger = logging.getLogger(__name__)


class ServiceJwtAuthInterceptor(grpc.aio.ServerInterceptor):
    """Verifies the EdDSA bearer JWT, scope and caller identity before any handler runs.

    ``required_scope`` should be unique per module (e.g. ``action:discord:invoke``) so a
    token minted for one module is useless at another (cross-module replay). ``allowed_callers``
    is the set of permitted ``sub`` SPIFFE IDs; empty is rejected at construction (fail closed).
    """

    def __init__(
        self,
        verifier: ServiceJwtVerifier,
        *,
        required_scope: str,
        allowed_callers: frozenset[str],
    ) -> None:
        """Bind the verifier, the module's scope and its caller allow-list."""
        if not required_scope:
            raise ServiceJwtError("required_scope must be non-empty")
        if not allowed_callers:
            raise ServiceJwtError("allowed_callers must be non-empty (an empty allow-list denies all)")
        self._verifier = verifier
        self._scope = required_scope
        self._allowed = allowed_callers

    def _authenticate(self, metadata: dict[str, str | bytes]) -> tuple[grpc.StatusCode, str] | None:
        """Return a (status, message) denial, or None when the call is authorized."""
        raw = metadata.get("authorization", "")
        header = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if not header.startswith("Bearer ") or not header[7:].strip():
            return grpc.StatusCode.UNAUTHENTICATED, "missing bearer token"
        try:
            claims = self._verifier.verify(header[7:].strip(), required_scope=self._scope)
        except (UnknownKeyId, InvalidServiceToken):
            # Never reveal which check failed (security.md Service-to-Service Auth).
            return grpc.StatusCode.UNAUTHENTICATED, "unauthorized"
        if claims.get("sub") not in self._allowed:
            logger.warning("service_jwt.caller_not_allowed")
            return grpc.StatusCode.PERMISSION_DENIED, "caller not authorized"
        return None

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        """Authenticate the call, short-circuiting with an abort handler on denial."""
        denial = self._authenticate(dict(handler_call_details.invocation_metadata or ()))
        if denial is None:
            return await continuation(handler_call_details)
        code, message = denial

        async def _reject(request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
            await context.abort(code, message)

        return grpc.unary_unary_rpc_method_handler(_reject)
