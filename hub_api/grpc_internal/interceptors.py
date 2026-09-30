"""grpc.aio interceptors for hub-api's internal gRPC server.

Chain order (see `server.py`): `DeadlineInterceptor` -> `AuthInterceptor`
-> `RateLimitInterceptor` -> `OTelInterceptor`. Auth must run before rate
limiting so the limiter can key on the caller's verified `sub` (SPIFFE
ID) rather than an unauthenticated, spoofable value.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, cast

import grpc
from flask_core.service_jwt import (
    InvalidServiceToken,
    ServiceJwtVerifier,
    UnknownKeyId,
)
from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

#: Every RPC's required deadline ceiling (security.md: bounded, predictable
#: per-RPC deadlines; a caller requesting longer is fighting the contract,
#: not exercising a legitimate slow path).
MAX_RPC_DEADLINE_SECONDS = 5.0

#: Populated by `AuthInterceptor` after successful verification; read by
#: `RateLimitInterceptor` further down the same interceptor chain. Both
#: run within the same asyncio task per RPC, so contextvar propagation is
#: safe without additional locking.
service_claims: ContextVar[dict[str, Any] | None] = ContextVar("service_claims", default=None)

_tracer = trace.get_tracer("hub_api.grpc_internal")
_meter = metrics.get_meter("hub_api.grpc_internal")
_latency_histogram = _meter.create_histogram(
    name="grpc_server_request_duration_seconds",
    unit="s",
    description="Internal gRPC request latency, by RPC method and status code.",
)


class DeadlineInterceptor(grpc.aio.ServerInterceptor):
    """Fails closed on any RPC that doesn't carry a bounded client deadline.

    grpc.aio has no server-side way to *set* a client's deadline, only to
    reject calls that omit one -- an unbounded call ties up a server-side
    coroutine indefinitely, which is the actual risk this guards against.
    """

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        """Wraps the next handler in this interceptor's behavior (grpc.aio contract)."""
        handler = await continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler

        inner = handler.unary_unary

        async def wrapped(request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
            remaining = context.time_remaining()
            if remaining is None:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT, "a client deadline is required"
                )
            elif remaining > MAX_RPC_DEADLINE_SECONDS:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"deadline exceeds the {MAX_RPC_DEADLINE_SECONDS}s ceiling",
                )
            return await inner(request, cast(grpc.ServicerContext, context))

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


@dataclass(slots=True)
class AuthInterceptor(grpc.aio.ServerInterceptor):
    """Verifies the `authorization` metadata's EdDSA machine JWT on every RPC.

    `required_scopes` maps a full gRPC method name (e.g.
    ``/waddles.hub.internal.v1.IdentityService/MintEphemeralPseudonyms``) to
    the scope it requires -- an unlisted method fails closed
    (UNAUTHENTICATED), never defaults to "no scope required".
    """

    verifier: ServiceJwtVerifier
    required_scopes: dict[str, str]

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        """Wraps the next handler in this interceptor's behavior (grpc.aio contract)."""
        handler = await continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler

        method = handler_call_details.method
        required_scope = self.required_scopes.get(method)
        inner = handler.unary_unary

        async def wrapped(request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
            if required_scope is None:
                await context.abort(
                    grpc.StatusCode.UNAUTHENTICATED, "no scope registered for this method"
                )
            metadata = dict(context.invocation_metadata() or ())
            raw_auth_header = metadata.get("authorization", "")
            # `authorization` is always a text-valued (non `-bin`) metadata
            # key, but grpc-stubs types every metadata value as `bytes |
            # str` generically -- normalize once here.
            auth_header = (
                raw_auth_header.decode() if isinstance(raw_auth_header, bytes) else raw_auth_header
            )
            if not auth_header.startswith("Bearer "):
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing bearer token")
            token = auth_header[len("Bearer ") :]
            try:
                claims = self.verifier.verify(token, required_scope=required_scope)
            except (UnknownKeyId, InvalidServiceToken):
                # Never distinguish which check failed to the caller
                # (security.md Service-to-Service Auth / JWT Claims).
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
            service_claims.set(claims)
            return await inner(request, cast(grpc.ServicerContext, context))

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


@dataclass(slots=True)
class _Bucket:
    """Token-bucket state for one calling service (`sub` claim)."""

    tokens: float
    last_refill: float


@dataclass(slots=True)
class RateLimitInterceptor(grpc.aio.ServerInterceptor):
    """Per-calling-service token-bucket rate limiting.

    Keyed on the verified `sub` (SPIFFE ID) set by `AuthInterceptor` --
    must run after it in the chain. A caller with no verified claims
    (auth interceptor already aborted) never reaches this code.
    """

    requests_per_second: float = 50.0
    burst: float = 100.0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        """Wraps the next handler in this interceptor's behavior (grpc.aio contract)."""
        handler = await continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler
        inner = handler.unary_unary

        async def wrapped(request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
            claims = service_claims.get()
            key = claims["sub"] if claims else "unknown"
            if not self._take(key):
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "rate limit exceeded")
            return await inner(request, cast(grpc.ServicerContext, context))

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )

    def _take(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self.burst, last_refill=now)
            self._buckets[key] = bucket
        elapsed = now - bucket.last_refill
        bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.requests_per_second)
        bucket.last_refill = now
        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True


class OTelInterceptor(grpc.aio.ServerInterceptor):
    """Emits a trace span plus a latency histogram data point per RPC.

    Together with `flask_core`'s HTTP-side tracing this satisfies
    critical-rules.md Observability (OTel): "Traces: span the real work
    -- inter-service calls" and "Metrics: histograms for load/latency
    first".
    """

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        """Wraps the next handler in this interceptor's behavior (grpc.aio contract)."""
        handler = await continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler
        method = handler_call_details.method
        inner = handler.unary_unary

        async def wrapped(request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
            start = time.monotonic()
            status_code = "OK"
            with _tracer.start_as_current_span(method, kind=SpanKind.SERVER) as span:
                try:
                    response = await inner(request, cast(grpc.ServicerContext, context))
                    return response
                except grpc.aio.AbortError:
                    status_code = context.code().name if context.code() else "UNKNOWN"
                    span.set_status(Status(StatusCode.ERROR, status_code))
                    raise
                except Exception as exc:  # noqa: BLE001 - re-raised after recording span status
                    status_code = "INTERNAL"
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    raise
                finally:
                    _latency_histogram.record(
                        time.monotonic() - start,
                        attributes={"rpc.method": method, "rpc.grpc.status_code": status_code},
                    )

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
