"""In-cluster HTTPS policy for internal HTTP clients (hub-api, core-data, svc-streaming).

The Helm chart (``global.internalTransitTls``) mounts the internal-CA bundle and sets
``INTERNAL_CA_FILE`` plus ``INTERNAL_TLS_REQUIRED=true`` on every workload that dials an
internal HTTP endpoint. This module turns those variables into an ``httpx`` client that
pins trust to the internal CA, floors TLS at 1.2 (1.3 whenever the peer offers it),
negotiates HTTP/2 over ALPN, and keeps connections pooled so a process pays one
handshake per peer. Every misconfiguration raises :class:`InternalTlsError`; nothing
falls back to plaintext or to system roots.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import weakref
from dataclasses import dataclass
from pathlib import Path

import httpx

ENV_INTERNAL_CA_FILE = "INTERNAL_CA_FILE"
#: Inline PEM alternative to the file (the chart injects it from a Secret key).
ENV_INTERNAL_CA_PEM = "INTERNAL_CA_PEM"
ENV_INTERNAL_TLS_REQUIRED = "INTERNAL_TLS_REQUIRED"

#: Idle pooled connections are kept this long so keep-alive/resumption stays warm.
KEEPALIVE_EXPIRY_S = 90.0
#: Upper bound on pooled connections per client (HTTP/2 multiplexes many requests on one).
MAX_CONNECTIONS = 50


class InternalTlsError(RuntimeError):
    """The internal-TLS policy rejected the configuration or a URL."""


@dataclass(slots=True, frozen=True)
class InternalTlsPolicy:
    """Resolved policy: the pinned CA path (if any) and the plaintext-refusal flag."""

    ca_file: str | None
    required: bool
    ca_pem: str | None = None

    @classmethod
    def from_env(cls) -> InternalTlsPolicy:
        """Read the policy from the environment; ``required`` without a CA is an error."""
        required = os.environ.get(ENV_INTERNAL_TLS_REQUIRED, "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        ca_file = os.environ.get(ENV_INTERNAL_CA_FILE, "").strip() or None
        ca_pem = os.environ.get(ENV_INTERNAL_CA_PEM, "").strip() or None
        if required and ca_file is None and ca_pem is None:
            raise InternalTlsError(
                f"{ENV_INTERNAL_TLS_REQUIRED} is set but neither {ENV_INTERNAL_CA_PEM} "
                f"nor {ENV_INTERNAL_CA_FILE} is set"
            )
        return cls(ca_file=ca_file, required=required, ca_pem=ca_pem)

    def check_url(self, url: str) -> str:
        """Return ``url`` unchanged, or raise if the policy requires HTTPS and it is not."""
        if self.required and not url.strip().lower().startswith("https://"):
            raise InternalTlsError(
                f"plaintext or non-HTTPS URL refused ({ENV_INTERNAL_TLS_REQUIRED} is set): {url}"
            )
        return url

    def ssl_context(self) -> ssl.SSLContext | bool:
        """Build the verifying context: pinned CA only, TLS>=1.2, h2 + http/1.1 ALPN.

        Returns ``True`` (httpx default verification) only when no CA is configured and
        the policy is not required; a configured CA that cannot be loaded raises.
        """
        if self.ca_file is None and self.ca_pem is None:
            return True
        if self.ca_pem is None:
            assert self.ca_file is not None
            path = Path(self.ca_file)
            if not path.is_file():
                raise InternalTlsError(
                    f"{ENV_INTERNAL_CA_FILE} {self.ca_file!r} is not a readable file"
                )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        source = ENV_INTERNAL_CA_PEM if self.ca_pem is not None else repr(self.ca_file)
        try:
            if self.ca_pem is not None:
                ctx.load_verify_locations(cadata=self.ca_pem)
            else:
                ctx.load_verify_locations(cafile=str(path))
        except (ssl.SSLError, OSError, ValueError) as exc:
            raise InternalTlsError(
                f"{source} holds no valid PEM certificate: {exc}"
            ) from exc
        ctx.set_alpn_protocols(["h2", "http/1.1"])
        return ctx


def build_client(
    policy: InternalTlsPolicy | None = None, *, timeout_s: float = 10.0
) -> httpx.AsyncClient:
    """Build a pooled, HTTP/2-capable ``httpx.AsyncClient`` honouring the policy."""
    pol = policy if policy is not None else InternalTlsPolicy.from_env()
    return httpx.AsyncClient(
        verify=pol.ssl_context(),
        http2=True,
        timeout=timeout_s,
        limits=httpx.Limits(
            max_connections=MAX_CONNECTIONS,
            keepalive_expiry=KEEPALIVE_EXPIRY_S,
        ),
    )


class PooledClient:
    """Lazily builds ONE pooled client per running event loop and hands it back every call.

    An ``httpx.AsyncClient`` is bound to the loop that first used it, so the pool is keyed
    by loop: inside one Quart process there is a single loop and therefore a single pool
    (no per-request handshake), while test suites that spin a loop per test never see a
    client from a closed loop.
    """

    __slots__ = ("_clients", "_policy", "_timeout_s")

    def __init__(
        self, policy: InternalTlsPolicy | None = None, *, timeout_s: float = 10.0
    ) -> None:
        """Remember the policy/timeout; the client itself is built on first :meth:`get`."""
        self._policy = policy
        self._timeout_s = timeout_s
        self._clients: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, httpx.AsyncClient
        ] = weakref.WeakKeyDictionary()

    def get(self) -> httpx.AsyncClient:
        """Return the pooled client for the running loop, building it once."""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = build_client(self._policy, timeout_s=self._timeout_s)
            self._clients[loop] = client
        return client
