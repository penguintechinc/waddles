"""hub-api's internal gRPC server (`waddles.hub.internal.v1`).

Runs `grpc.aio.server()` in-process alongside the Quart/hypercorn HTTP
app, on the same event loop, bound to a separate internal port
(`GRPC_PORT`, default 50204 per k8s/helm/waddlebot/values.yaml
`pipeline.hubApi.grpcPort`) -- one container, one image, two listeners,
matching the Deployment/Service already shipped in
`k8s/helm/waddlebot/templates/hub-api.yaml`. A standalone sidecar
process was considered and rejected: it would need its own copy of the
DAL/tenant/service-identity wiring `app.py` already performs at
startup, for a second process with no independent scaling need (the
gRPC surface serves the same three callers, at the same low internal
QPS, as the REST surface it sits next to).

TLS is mandatory; SPIFFE-ready (mTLS optional today, `ssl_target_name_
override`/X.509-SVID slot reserved for when Skauswatch/SPIRE issues
workload certs -- see `penguintech.md` SPIFFE Identity). Client cert
verification stays optional until SPIRE is deployed in a given
environment; the EdDSA machine JWT (`AuthInterceptor`) is the mandatory
authn layer regardless of whether mTLS is live yet.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import grpc
from flask_core.service_jwt import ServiceJwtIssuer
from waddles.hub.internal.v1 import identity_pb2_grpc, key_pb2_grpc

from grpc_internal.interceptors import (
    AuthInterceptor,
    DeadlineInterceptor,
    OTelInterceptor,
    RateLimitInterceptor,
)
from grpc_internal.servicers import REQUIRED_SCOPES, IdentityServicer, KeyServicer

logger = logging.getLogger(__name__)

#: Rejects any message over 4 MiB either direction -- generous for a
#: <=100-item batch, small enough that one internal caller can't exhaust
#: server memory with an oversized payload.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


def _load_server_credentials() -> grpc.ServerCredentials:
    """Loads the internal gRPC server's TLS material from env-mounted paths.

    `GRPC_TLS_CLIENT_CA_PATH` is optional: unset means "TLS required,
    client certs not yet verified" (today's SPIRE-less environments);
    set means the server additionally requires and verifies a client
    X.509-SVID (`require_client_auth=True`), the SPIFFE/mTLS end state.
    """
    key_path = os.environ["GRPC_TLS_KEY_PATH"]
    cert_path = os.environ["GRPC_TLS_CERT_PATH"]
    with open(key_path, "rb") as fh:
        private_key = fh.read()
    with open(cert_path, "rb") as fh:
        cert_chain = fh.read()
    client_ca_path = os.getenv("GRPC_TLS_CLIENT_CA_PATH")
    if client_ca_path:
        with open(client_ca_path, "rb") as fh:
            root_certs = fh.read()
        return grpc.ssl_server_credentials(
            [(private_key, cert_chain)],
            root_certificates=root_certs,
            require_client_auth=True,
        )
    return grpc.ssl_server_credentials([(private_key, cert_chain)])


def build_interceptor_chain(issuer: ServiceJwtIssuer) -> list[grpc.aio.ServerInterceptor]:
    """The production interceptor chain: deadline -> auth -> rate limit -> OTel.

    Auth runs before rate limiting so the limiter keys on the caller's verified
    identity, never an unauthenticated claim; the servicers rely on auth having
    populated `service_claims` (they fail closed without it). Tests build their
    in-process servers from this same function so they exercise the real chain,
    never a servicer reached around its interceptors.
    """
    return [
        DeadlineInterceptor(),
        AuthInterceptor(verifier=issuer.as_verifier(), required_scopes=REQUIRED_SCOPES),
        RateLimitInterceptor(
            requests_per_second=float(os.getenv("GRPC_RATE_LIMIT_RPS", "50")),
            burst=float(os.getenv("GRPC_RATE_LIMIT_BURST", "100")),
        ),
        OTelInterceptor(),
    ]


async def build_internal_grpc_server(
    *, issuer: ServiceJwtIssuer, async_dal: Any = None
) -> grpc.aio.Server:
    """Builds (but does not start) the internal gRPC server.

    Interceptor chain: see :func:`build_interceptor_chain`.
    """
    server = grpc.aio.server(
        interceptors=build_interceptor_chain(issuer),
        options=[
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        ],
    )
    identity_pb2_grpc.add_IdentityServiceServicer_to_server(IdentityServicer(async_dal), server)
    key_pb2_grpc.add_KeyServiceServicer_to_server(KeyServicer(), server)

    bind_addr = f"0.0.0.0:{os.environ['GRPC_PORT']}"  # noqa: S104 - internal ClusterIP-only listener, see NetworkPolicy
    server.add_secure_port(bind_addr, _load_server_credentials())
    logger.info("hub_api.grpc_internal.configured", extra={"bind_addr": bind_addr})
    return server


async def start_internal_grpc_server(
    *, issuer: ServiceJwtIssuer, async_dal: Any = None
) -> grpc.aio.Server:
    """Builds and starts the server -- called from `app.py`'s `before_serving`."""
    server = await build_internal_grpc_server(issuer=issuer, async_dal=async_dal)
    await server.start()
    logger.info("hub_api.grpc_internal.started")
    return server


async def stop_internal_grpc_server(server: grpc.aio.Server) -> None:
    """Graceful shutdown -- called from `app.py`'s `after_serving`."""
    await server.stop(grace=5.0)
    logger.info("hub_api.grpc_internal.stopped")
