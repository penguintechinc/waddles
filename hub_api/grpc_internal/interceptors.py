"""grpc.aio interceptors for hub-api's internal gRPC server.

Chain order (see `server.py`): `OTelInterceptor` (outermost, so rejected RPCs are traced and
counted too) -> `DeadlineInterceptor` -> `AuthInterceptor` -> `RateLimitInterceptor`. Auth must run
before rate limiting so the limiter can key on the caller's verified `sub` (SPIFFE ID) rather than
an unauthenticated, spoofable value.
"""

from __future__ import annotations

import logging
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
from opentelemetry import metrics, propagate, trace
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

logger = logging.getLogger(__name__)

_tenantless_counter = metrics.get_meter("hub_api.grpc_internal").create_counter(
    name="grpc_server_tenantless_token_rejections",
    description="Valid machine JWTs rejected for carrying no tenant claim, by RPC method.",
)


@dataclass(slots=True)
class _RpcInstruments:
    """The per-RPC histogram + handled counter, bound to one MeterProvider."""

    latency: Any
    handled: Any


_instrument_cache: dict[int, _RpcInstruments] = {}


def _rpc_instruments() -> _RpcInstruments:
    """Instruments for the CURRENT global MeterProvider (created once per provider).

    Resolved at call time rather than import time so the SDK provider `telemetry.start()`
    installs in `before_serving` is what records them, regardless of import order.
    """
    provider = metrics.get_meter_provider()
    cached = _instrument_cache.get(id(provider))
    if cached is None:
        meter = metrics.get_meter("hub_api.grpc_internal")
        cached = _RpcInstruments(
            latency=meter.create_histogram(
                name="grpc_server_request_duration_seconds",
                unit="s",
                description="Internal gRPC request latency, by RPC method and status code.",
            ),
            handled=meter.create_counter(
                name="grpc_server_handled",
                unit="{call}",
                description="Internal gRPC calls completed, by RPC method and status code.",
            ),
        )
        _instrument_cache.clear()
        _instrument_cache[id(provider)] = cached
    return cached


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
    """Verifies the `authorization` metadata's ES256 machine JWT on every RPC.

    `required_scopes` maps a full gRPC method name (e.g.
    ``/waddles.hub.internal.v1.IdentityService/MintEphemeralPseudonyms``) to
    the scope it requires -- an unlisted method fails closed
    (UNAUTHENTICATED), never defaults to "no scope required".

    A token that verifies but carries no `tenant` claim is rejected
    (PERMISSION_DENIED): every internal token is tenant-bound by the issuer
    (`flask_core.service_jwt.ServiceIdentity.tenant`; `system` = operator
    plane), and the servicers read that claim from `service_claims` to confine
    each request -- they never trust a tenant named in the request body alone
    (security.md Tenant Isolation: "Every token MUST carry tenant claim").
    """

    verifier: ServiceJwtVerifier
    #: Method -> one required scope, or a tuple of alternatives of which a token must hold
    #: ANY one (e.g. `connections:resolve` OR `connections:resolve:static`).
    required_scopes: dict[str, str | tuple[str, ...]]
    #: Bootstrap RPCs (the machine-JWT mint + JWKS) -- reachable without a machine JWT because
    #: they are what produce / verify one. The servicer authenticates its own caller. An RPC is
    #: public only if listed HERE explicitly; everything else still fails closed.
    public_methods: frozenset[str] = frozenset()

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
        if method in self.public_methods:
            return handler
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
            claims: dict[str, Any] | None = None
            for candidate in (
                (required_scope,) if isinstance(required_scope, str) else required_scope
            ):
                try:
                    claims = self.verifier.verify(token, required_scope=candidate)
                    break
                except (UnknownKeyId, InvalidServiceToken):
                    continue
            if claims is None:
                # Never distinguish which check failed to the caller
                # (security.md Service-to-Service Auth / JWT Claims).
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
                raise AssertionError("unreachable")
            tenant_claim = claims.get("tenant")
            if not isinstance(tenant_claim, str) or not tenant_claim.strip():
                _tenantless_counter.add(1, {"rpc.method": method})
                logger.warning(
                    "internal rpc rejected: token carries no tenant claim",
                    extra={
                        "action": "grpc_tenantless_token",
                        "rpc_method": method,
                        "caller": str(claims.get("sub", ""))[:128],
                    },
                )
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "tenant claim required")
            service_claims.set(claims)
            return await inner(request, cast(grpc.ServicerContext, context))

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


def _peer_host(peer: str | None) -> str:
    """Host part of a gRPC peer string (``ipv4:1.2.3.4:5678`` / ``ipv6:[::1]:5``), no port."""
    if not peer:
        return "unknown"
    rest = peer.split(":", 1)[1] if ":" in peer else peer
    if rest.startswith("["):
        return rest[1 : rest.index("]")] if "]" in rest else rest
    return rest.rsplit(":", 1)[0]


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
    #: Full method name -> ``(requests_per_second, burst)`` for RPCs that need a tighter
    #: budget than the default (a PII-returning RPC). Such a method has its OWN bucket per
    #: caller, so it neither borrows from nor drains the shared one.
    method_limits: dict[str, tuple[float, float]] = field(default_factory=dict)
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
        method = handler_call_details.method
        limit = self.method_limits.get(method)

        async def wrapped(request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
            claims = service_claims.get()
            key = claims["sub"] if claims else f"peer:{_peer_host(context.peer())}"
            if limit is not None:
                key = f"{key}|{method}"
            if not self._take(key, limit):
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "rate limit exceeded")
            return await inner(request, cast(grpc.ServicerContext, context))

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )

    def _take(self, key: str, limit: tuple[float, float] | None = None) -> bool:
        rate, burst = limit if limit is not None else (self.requests_per_second, self.burst)
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=burst, last_refill=now)
            self._buckets[key] = bucket
        elapsed = now - bucket.last_refill
        bucket.tokens = min(burst, bucket.tokens + elapsed * rate)
        bucket.last_refill = now
        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True


class OTelInterceptor(grpc.aio.ServerInterceptor):
    """Per-RPC server span (W3C ``traceparent`` parent), latency histogram and handled counter.

    Sits OUTERMOST in the chain so calls rejected by the deadline/auth/rate-limit interceptors
    still produce a span and a ``grpc_server_handled{rpc.grpc.status_code=...}`` data point. The
    parent context is extracted from the invocation metadata with the global propagator (W3C
    tracecontext + baggage), so a client instrumented with `GrpcAioInstrumentorClient` continues
    its trace here. Only the RPC method and status code are recorded, never request content.
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
        service_name, _, short_method = method.lstrip("/").partition("/")
        inner = handler.unary_unary

        async def wrapped(request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
            start = time.monotonic()
            status_code = "OK"
            carrier = {
                key.lower(): value
                for key, value in (context.invocation_metadata() or ())
                if isinstance(key, str) and isinstance(value, str)
            }
            parent = propagate.extract(carrier)
            tracer = trace.get_tracer("hub_api.grpc_internal")
            with tracer.start_as_current_span(
                method,
                context=parent,
                kind=SpanKind.SERVER,
                attributes={
                    "rpc.system": "grpc",
                    "rpc.service": service_name,
                    "rpc.method": short_method,
                },
            ) as span:
                try:
                    response = await inner(request, cast(grpc.ServicerContext, context))
                    return response
                except grpc.aio.AbortError:
                    status_code = context.code().name if context.code() else "UNKNOWN"
                    span.set_status(Status(StatusCode.ERROR, status_code))
                    raise
                except Exception as exc:  # noqa: BLE001 - re-raised after recording span status
                    status_code = "INTERNAL"
                    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                    raise
                finally:
                    span.set_attribute("rpc.grpc.status_code", status_code)
                    attributes = {"rpc.method": method, "rpc.grpc.status_code": status_code}
                    instruments = _rpc_instruments()
                    instruments.latency.record(time.monotonic() - start, attributes=attributes)
                    instruments.handled.add(1, attributes=attributes)

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
