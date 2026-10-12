"""Egress guard (SSRF protection) for workflow outbound requests.

Outbound-URL guard for workflow nodes that fetch attacker-authorable URLs
(``ACTION_WEBHOOK``). Workflow authors control the URL (and may template it
from untrusted trigger data), and the response body is reflected back into
workflow output, so an unguarded fetch is a classic SSRF primitive: reading
cloud metadata (169.254.169.254), cluster-internal services, or loopback
admin ports.

Defence in depth, all fail-loud (``EgressBlockedError``, never a silent
fetch or silent fallback):

1. ``validate_egress_url`` -- pre-flight check: scheme must be http/https, no
   URL userinfo, host is resolved and EVERY resolved address must be public.
   This is the only check that covers IP-literal hosts (aiohttp never
   consults a resolver for those).
2. ``GuardedResolver`` -- an ``aiohttp`` resolver that re-resolves and
   re-validates at connect time, and returns exactly the validated
   addresses that aiohttp then connects to (the "pin"). Because the
   validated answer IS the connect target, a DNS-rebinding server that
   answers public first and private second gains nothing -- there is no
   second lookup between check and use.
3. ``build_guarded_connector`` -- wires the resolver into a
   ``TCPConnector`` (``use_dns_cache`` off so each connect re-validates).

Blocked ranges: RFC1918, loopback, link-local (incl. cloud metadata
169.254.169.254 and fd00:ec2::254), CGNAT, unique-local (fc00::/7),
fe80::/10, multicast, reserved, unspecified, and IPv4 embedded in IPv6
(IPv4-mapped ``::ffff:a.b.c.d``, NAT64 ``64:ff9b::/96``, 6to4 ``2002::/16``,
Teredo ``2001::/32``). Anything not globally routable is refused.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

ALLOWED_SCHEMES = frozenset({"http", "https"})

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")


class EgressBlockedError(Exception):
    """Raised when an outbound request targets a disallowed destination."""


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address smuggled inside an IPv6 address, if any."""
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr in _NAT64:
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr in _SIXTOFOUR:
        return ipaddress.IPv4Address((int(addr) >> 80) & 0xFFFFFFFF)
    return None


def is_blocked_address(addr: IPAddress) -> bool:
    """Return True if ``addr`` must never be a workflow egress target.

    Default-deny: only globally routable unicast addresses pass.
    """
    if isinstance(addr, ipaddress.IPv6Address):
        if addr in _TEREDO:
            return True
        embedded = _embedded_ipv4(addr)
        if embedded is not None:
            return is_blocked_address(embedded)
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True
    return not addr.is_global


def _parse_literal(host: str) -> IPAddress | None:
    """Parse ``host`` as an IP literal (zone id stripped), else None."""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def assert_addresses_public(host: str, addresses: list[str]) -> None:
    """Raise ``EgressBlockedError`` unless every address is public.

    An empty answer is a failure, not a pass (zero examined = fail).
    """
    if not addresses:
        raise EgressBlockedError(f"egress blocked: {host!r} resolved to no addresses")
    for raw in addresses:
        parsed = _parse_literal(raw)
        if parsed is None:
            raise EgressBlockedError(
                f"egress blocked: {host!r} resolved to unparseable address {raw!r}"
            )
        if is_blocked_address(parsed):
            raise EgressBlockedError(
                f"egress blocked: {host!r} resolves to non-public address {parsed}"
            )


async def resolve_host(host: str, port: int) -> list[str]:
    """Resolve ``host`` to its address strings (IP literals pass through)."""
    literal = _parse_literal(host)
    if literal is not None:
        return [str(literal)]
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressBlockedError(f"egress blocked: cannot resolve {host!r}: {exc}") from exc
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(info[4][0] for info in infos))


async def validate_egress_url(url: str) -> None:
    """Pre-flight validate an outbound URL; raise ``EgressBlockedError`` if unsafe."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise EgressBlockedError(f"egress blocked: malformed URL: {exc}") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise EgressBlockedError(
            f"egress blocked: scheme {scheme!r} not allowed (http/https only)"
        )
    if parts.username is not None or parts.password is not None:
        raise EgressBlockedError("egress blocked: credentials in URL are not allowed")
    if not host:
        raise EgressBlockedError("egress blocked: URL has no host")

    addresses = await resolve_host(host, port or (443 if scheme == "https" else 80))
    assert_addresses_public(host, addresses)


class GuardedResolver(AbstractResolver):
    """aiohttp resolver that only ever returns validated public addresses.

    The returned addresses are the exact connect targets, so resolution and
    use are a single step (DNS-rebinding safe).
    """

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, Any]]:
        """Resolve and validate ``host``; raise ``EgressBlockedError`` on non-public answers."""
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                host, port, type=socket.SOCK_STREAM, family=family
            )
        except socket.gaierror as exc:
            raise EgressBlockedError(
                f"egress blocked: cannot resolve {host!r}: {exc}"
            ) from exc
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        assert_addresses_public(host, addresses)
        return [
            {
                "hostname": host,
                "host": info[4][0],
                "port": info[4][1],
                "family": info[0],
                "proto": info[2],
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            }
            for info in infos
        ]

    async def close(self) -> None:
        """No resources to release."""
        return None


def build_guarded_connector() -> aiohttp.TCPConnector:
    """Build a TCPConnector that can only connect to validated public addresses."""
    return aiohttp.TCPConnector(resolver=GuardedResolver(), use_dns_cache=False)
