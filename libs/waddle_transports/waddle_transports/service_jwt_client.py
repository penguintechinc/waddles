"""Machine-JWT mint over gRPC: the Python client of hub-api's `ServiceAuthService`.

The Python counterpart of `core/service_auth`'s `MachineJwtClient`. It calls
`waddles.hub.internal.v1.ServiceAuthService/MintServiceToken` (the gRPC replacement for the
removed REST `POST /internal/service-token`), authenticating with the pod's projected,
audience-bound Kubernetes ServiceAccount token sent as `authorization: Bearer` metadata over
TLS. Channels are long-lived: one `grpc.aio` channel per (endpoint, CA, event loop) is reused
for every mint, with keepalive pings so an idle connection is not silently dropped.

Fail loud: a non-loopback plaintext endpoint, a missing/unreadable CA file, an unreadable SA
token or any non-OK status raises `ServiceTokenError`; there is no fallback transport. Errors
carry the gRPC status name only -- never a token.

`hub_api/grpc_internal/pb` holds the server-side generated stubs; this module hand-binds the
two unary methods to the message classes in `_service_auth_pb2` (generated from the same
`proto/waddles/hub/internal/v1/service_auth.proto`), so no second copy of the stub layer exists.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import grpc

from . import _service_auth_pb2 as pb

logger = logging.getLogger(__name__)

_MINT_METHOD = "/waddles.hub.internal.v1.ServiceAuthService/MintServiceToken"
_DEFAULT_SA_TOKEN_PATH = "/var/run/secrets/waddlebot/service-jwt/service-jwt-bootstrap-token"  # noqa: S105,E501 - path
_CHANNEL_OPTIONS = (
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
)
#: (endpoint, ca_file, loop id) -> channel; grpc.aio channels are bound to the loop that made them.
_channels: dict[tuple[str, str | None, int], grpc.aio.Channel] = {}


class ServiceTokenError(RuntimeError):
    """Minting a machine JWT failed (config, transport or hub-api rejection)."""


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A freshly minted machine JWT and its lifetime in seconds."""

    token: str
    expires_in: float


def _is_loopback(host: str) -> bool:
    """True for localhost / a loopback IP -- the only hosts allowed without TLS (dev + tests)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read_text(path: str) -> str:
    """Read a small text file (run via `asyncio.to_thread` so the loop never blocks on disk)."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _read_bytes(path: str) -> bytes:
    """Read a PEM file."""
    with open(path, "rb") as handle:
        return handle.read()


def _channel_for(endpoint: str, ca_file: str | None) -> grpc.aio.Channel:
    """Return the cached long-lived channel for `endpoint`, creating it on first use.

    `endpoint` is a URL (`https://host:port`, or `http://127.0.0.1:port` for loopback only).
    """
    parsed = urlparse(endpoint)
    host, port = parsed.hostname, parsed.port
    if parsed.scheme not in ("http", "https") or not host or port is None:
        raise ServiceTokenError(
            f"invalid hub-api gRPC endpoint (need scheme://host:port): {endpoint!r}"
        )
    key = (endpoint, ca_file, id(asyncio.get_running_loop()))
    cached = _channels.get(key)
    if cached is not None:
        return cached
    target = f"{host}:{port}"
    if parsed.scheme == "http":
        if not _is_loopback(host):
            raise ServiceTokenError(
                "refusing plaintext gRPC to a non-loopback host: the machine-JWT bootstrap "
                "carries a ServiceAccount token and must use TLS (set an https:// endpoint)"
            )
        channel = grpc.aio.insecure_channel(target, options=_CHANNEL_OPTIONS)
    else:
        if not ca_file:
            raise ServiceTokenError("HUB_API_GRPC_CA_FILE is required for an https:// endpoint")
        try:
            roots = _read_bytes(ca_file)
        except OSError as exc:
            raise ServiceTokenError(f"cannot read gRPC CA file: {type(exc).__name__}") from exc
        channel = grpc.aio.secure_channel(
            target, grpc.ssl_channel_credentials(root_certificates=roots), options=_CHANNEL_OPTIONS
        )
    _channels[key] = channel
    return channel


async def close_channels() -> None:
    """Close and forget every cached channel (shutdown and tests)."""
    channels = list(_channels.values())
    _channels.clear()
    for channel in channels:
        await channel.close()


async def mint_service_token(
    *,
    endpoint: str,
    scope: str,
    community_id: int | None = None,
    connection_id: int | None = None,
    sa_token_path: str | None = None,
    ca_file: str | None = None,
    timeout_s: float = 3.0,
) -> MintedToken:
    """Mint a machine JWT for `scope` (optionally bound to a community/connection).

    Raises `ServiceTokenError` for an unreadable/empty SA token, a transport failure, a gRPC
    rejection, or a malformed response. The SA token and the minted JWT are never logged.
    """
    path = sa_token_path or os.environ.get("SERVICE_JWT_SA_TOKEN_PATH") or _DEFAULT_SA_TOKEN_PATH
    try:
        sa_token = (await asyncio.to_thread(_read_text, path)).strip()
    except OSError as exc:
        raise ServiceTokenError(
            f"projected ServiceAccount token unreadable: {type(exc).__name__}"
        ) from exc
    if not sa_token:
        raise ServiceTokenError("projected ServiceAccount token is empty")

    ca = ca_file if ca_file is not None else (os.environ.get("HUB_API_GRPC_CA_FILE") or None)
    channel = _channel_for(endpoint, ca)
    call = channel.unary_unary(
        _MINT_METHOD,
        request_serializer=pb.MintServiceTokenRequest.SerializeToString,
        response_deserializer=pb.MintServiceTokenResponse.FromString,
    )
    request = pb.MintServiceTokenRequest(api_version="v1", scope=scope)
    if community_id is not None:
        request.community_id = community_id
    if connection_id is not None:
        request.connection_id = connection_id
    try:
        response = await call(
            request, metadata=(("authorization", f"Bearer {sa_token}"),), timeout=timeout_s
        )
    except grpc.aio.AioRpcError as exc:
        raise ServiceTokenError(f"hub-api refused the token request: {exc.code().name}") from exc
    if not response.token or response.expires_in <= 0:
        raise ServiceTokenError("hub-api token response malformed")
    return MintedToken(token=response.token, expires_in=float(response.expires_in))
