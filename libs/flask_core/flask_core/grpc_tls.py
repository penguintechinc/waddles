"""gRPC transport TLS for Waddles Flask/Quart modules.

Security audit finding A02 (Cryptographic Failures): every in-repo gRPC
server bound with ``add_insecure_port`` -- inter-service RPC traffic
(including the service JWTs validated by :mod:`grpc_auth_interceptor`-style
interceptors, see security.md Service-to-Service Auth) crossed the network
in plaintext. This module is the one place server/client TLS material is
loaded so no module reimplements its own (and inevitably drifts).

Fails closed: :func:`server_credentials`/:func:`channel_credentials` raise
:class:`GrpcTlsConfigError` when certificate material is required but
missing or unreadable, so a misconfigured deploy refuses to start rather
than silently falling back to plaintext. The only way to run without TLS is
the explicit, dev-only ``GRPC_TLS_INSECURE_DEV=true`` escape hatch -- never
an implicit default, and it is additionally refused outright under
:func:`workload_identity.is_production`.

SPIFFE-ready, not SPIFFE-wired: :mod:`workload_identity` already models the
SVID/trust-bundle shape (:class:`workload_identity.MtlsConfig`) but its
Workload API source is a documented TODO (SDK not yet wired -- see
``RealWorkloadApiSource``), so no live SVID material exists to load today.
:func:`server_credentials`/:func:`channel_credentials` load static
cert/key files instead (the practical fallback while SPIRE isn't live in a
given environment, per security.md); once the SDK is wired the same
``grpc.ssl_server_credentials``/``grpc.ssl_channel_credentials`` calls here
are the intended swap-in point for SVID-sourced material -- the call sites
that use this module would not need to change shape.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import grpc

from .workload_identity import is_production

logger = logging.getLogger(__name__)


def _running_under_pytest() -> bool:
    """True in any pytest worker process -- mirrors ``secrets._running_under_pytest``.

    Keeps a bare ``pytest`` run from being treated as production posture
    (which would otherwise refuse the dev-only insecure fallback in tests
    that intentionally exercise it), without requiring every test to set
    ``RELEASE_MODE`` explicitly. Tests exercising the fail-closed
    *production* path do so explicitly via ``RELEASE_MODE``/``ENVIRONMENT``,
    the same override shape used throughout this module's test suite.
    """
    return "pytest" in sys.modules


#: Explicit, dev-only plaintext escape hatch. Never the implicit default --
#: TLS is used unless this is set AND the resolved posture is non-production.
_INSECURE_DEV_ENV = "GRPC_TLS_INSECURE_DEV"

#: Default cap on a single gRPC message in either direction. Set explicitly
#: (rather than relying on grpcio's own built-in default) so it shows up in
#: `grpc.aio.server(options=...)` review instead of being implicit.
DEFAULT_MAX_MESSAGE_BYTES = 4 * 1024 * 1024  # 4 MiB


class GrpcTlsConfigError(RuntimeError):
    """TLS is required for this call but certificate material is missing/invalid."""


def _read_pem(env_var: str) -> bytes:
    """Read the PEM file named by ``env_var``, raising with the env var name on failure."""
    path = os.getenv(env_var, "")
    if not path:
        raise GrpcTlsConfigError(f"{env_var} is not set")
    file_path = Path(path)
    try:
        return file_path.read_bytes()
    except OSError as exc:
        raise GrpcTlsConfigError(f"{env_var}={path!r} could not be read: {exc}") from exc


def _insecure_dev_allowed(*, require_production_check: bool | None = None) -> bool:
    """Whether the dev-only plaintext fallback may be used in this process.

    Requires both the explicit opt-in env var AND a non-production posture
    (:func:`workload_identity.is_production`) -- a production-flagged
    deployment can never silently downgrade to plaintext even if the env
    var leaks into its config. A bare pytest run is never treated as
    production so tests don't need to fake an environment, unless
    ``require_production_check`` forces the real posture check -- the same
    override shape ``secrets.require_secret_key(require=...)`` uses, for
    tests that specifically exercise the production-refusal path.
    """
    opted_in = os.getenv(_INSECURE_DEV_ENV, "false").strip().lower() == "true"
    if not opted_in:
        return False
    prod = require_production_check if require_production_check is not None else (
        not _running_under_pytest() and is_production()
    )
    if prod:
        logger.error(
            "%s=true is set but this process is in production posture; "
            "refusing the plaintext fallback",
            _INSECURE_DEV_ENV,
        )
        return False
    return True


def server_credentials() -> grpc.ServerCredentials | None:
    """Build server-side TLS credentials from env-configured cert/key paths.

    Reads ``GRPC_TLS_CERT_PATH``/``GRPC_TLS_KEY_PATH`` (server cert chain +
    private key). If ``GRPC_TLS_CA_PATH`` is also set, mutual TLS is
    enabled: presented client certificates are verified against that CA,
    required unless ``GRPC_TLS_REQUIRE_CLIENT_CERT=false``.

    Returns ``None`` only when the dev-only insecure fallback applies (see
    module docstring) -- callers must bind with ``add_insecure_port`` in
    that case. Otherwise raises :class:`GrpcTlsConfigError`.
    """
    cert_path = os.getenv("GRPC_TLS_CERT_PATH", "")
    key_path = os.getenv("GRPC_TLS_KEY_PATH", "")
    if not cert_path or not key_path:
        if _insecure_dev_allowed():
            logger.warning(
                "GRPC_TLS_CERT_PATH/GRPC_TLS_KEY_PATH not set and %s=true -- "
                "binding gRPC server WITHOUT TLS. Dev only, never production.",
                _INSECURE_DEV_ENV,
            )
            return None
        raise GrpcTlsConfigError(
            "GRPC_TLS_CERT_PATH and GRPC_TLS_KEY_PATH are required to bind a gRPC "
            f"server over TLS (or set {_INSECURE_DEV_ENV}=true for local dev only)"
        )

    private_key = _read_pem("GRPC_TLS_KEY_PATH")
    certificate_chain = _read_pem("GRPC_TLS_CERT_PATH")

    ca_path = os.getenv("GRPC_TLS_CA_PATH", "")
    if ca_path:
        root_certificates = _read_pem("GRPC_TLS_CA_PATH")
        require_client_auth = (
            os.getenv("GRPC_TLS_REQUIRE_CLIENT_CERT", "true").strip().lower() == "true"
        )
        return grpc.ssl_server_credentials(
            [(private_key, certificate_chain)],
            root_certificates=root_certificates,
            require_client_auth=require_client_auth,
        )

    return grpc.ssl_server_credentials([(private_key, certificate_chain)])


def bind_secure_port(server: grpc.aio.Server, address: str) -> None:
    """Bind ``server`` to ``address``, over TLS unless the dev fallback applies.

    Drop-in replacement for ``server.add_insecure_port(address)`` at every
    server startup call site: builds credentials via
    :func:`server_credentials` and calls ``add_secure_port``, or
    ``add_insecure_port`` in the dev-only fallback case. Raises
    ``RuntimeError`` if the port could not be bound, matching the existing
    ``add_insecure_port(...) == 0`` failure convention used across these
    servers.
    """
    credentials = server_credentials()
    bound = (
        server.add_insecure_port(address)
        if credentials is None
        else server.add_secure_port(address, credentials)
    )
    if bound == 0:
        raise RuntimeError(f"Unable to bind gRPC server to {address}")


def channel_credentials() -> grpc.ChannelCredentials | None:
    """Build client-side TLS credentials from env-configured cert/key paths.

    Reads ``GRPC_TLS_CA_PATH`` (the CA that signs server certs -- required
    to validate a self-signed/internal dev CA). If
    ``GRPC_TLS_CLIENT_CERT_PATH``/``GRPC_TLS_CLIENT_KEY_PATH`` are also set,
    a client certificate is presented for mutual TLS.

    Returns ``None`` only under the same dev-only fallback as
    :func:`server_credentials`; otherwise raises :class:`GrpcTlsConfigError`.
    """
    ca_path = os.getenv("GRPC_TLS_CA_PATH", "")
    if not ca_path:
        if _insecure_dev_allowed():
            logger.warning(
                "GRPC_TLS_CA_PATH not set and %s=true -- dialing gRPC peers "
                "WITHOUT TLS. Dev only, never production.",
                _INSECURE_DEV_ENV,
            )
            return None
        raise GrpcTlsConfigError(
            "GRPC_TLS_CA_PATH is required to dial a gRPC peer over TLS "
            f"(or set {_INSECURE_DEV_ENV}=true for local dev only)"
        )

    root_certificates = _read_pem("GRPC_TLS_CA_PATH")

    client_cert_path = os.getenv("GRPC_TLS_CLIENT_CERT_PATH", "")
    client_key_path = os.getenv("GRPC_TLS_CLIENT_KEY_PATH", "")
    if client_cert_path and client_key_path:
        private_key = _read_pem("GRPC_TLS_CLIENT_KEY_PATH")
        certificate_chain = _read_pem("GRPC_TLS_CLIENT_CERT_PATH")
        return grpc.ssl_channel_credentials(
            root_certificates=root_certificates,
            private_key=private_key,
            certificate_chain=certificate_chain,
        )

    return grpc.ssl_channel_credentials(root_certificates=root_certificates)


def secure_channel(target: str, options: list[tuple[str, object]] | None = None) -> grpc.aio.Channel:
    """Open a ``grpc.aio.Channel`` to ``target``, over TLS unless the dev fallback applies.

    Drop-in replacement for ``grpc.aio.insecure_channel(target, options=...)``
    at every client call site: builds credentials via
    :func:`channel_credentials` and opens ``secure_channel``, or
    ``insecure_channel`` in the dev-only fallback case.
    """
    credentials = channel_credentials()
    if credentials is None:
        return grpc.aio.insecure_channel(target, options=options)
    return grpc.aio.secure_channel(target, credentials, options=options)


def default_server_options() -> list[tuple[str, object]]:
    """Server ``options`` enforcing an explicit max message size in both directions.

    ``GRPC_MAX_MESSAGE_BYTES`` overrides :data:`DEFAULT_MAX_MESSAGE_BYTES` for
    services that legitimately need a larger cap; the value is always
    explicit rather than left to grpcio's own default so it is visible in
    config review.
    """
    max_bytes = int(os.getenv("GRPC_MAX_MESSAGE_BYTES", str(DEFAULT_MAX_MESSAGE_BYTES)))
    return [
        ("grpc.max_receive_message_length", max_bytes),
        ("grpc.max_send_message_length", max_bytes),
    ]
