"""Admin-triggered connectivity test for a registered game server.

Mirrors the host capability's posture so the test can never become an SSRF primitive: resolve the
registered host ONCE, require EVERY answer to be publicly routable, then connect to that exact IP
(never re-resolve). With a stored RCON password it performs the real RCON login (type-3 auth,
expect a type-2 reply echoing the id); without one it only proves the TCP port accepts a
connection. The result is a closed code -- never an address, never the password.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct
from typing import Final

from services.url_guard import is_private_host

RESULT_OK_RCON: Final = "ok_rcon"
RESULT_OK_TCP: Final = "ok_tcp"
RESULT_AUTH_FAILED: Final = "auth_failed"
RESULT_UNREACHABLE: Final = "unreachable"
RESULT_TIMEOUT: Final = "timeout"
RESULT_FORBIDDEN: Final = "forbidden_address"

_LOGIN_ID: Final = 0x57414444
_TYPE_AUTH: Final = 3
_TYPE_AUTH_RESPONSE: Final = 2
_MAX_PACKET: Final = 4096 + 10
_CGNAT: Final = ipaddress.ip_network("100.64.0.0/10")
#: Reserved IPv4 ranges the stdlib does not classify (mirrors guard.rs `extra_reserved_v4`).
_V4_EXTRA: Final = tuple(
    ipaddress.ip_network(n)
    for n in ("0.0.0.0/8", "192.0.0.0/24", "198.18.0.0/15", "240.0.0.0/4", "255.255.255.255/32")
)
#: IPv6 transition/tunnel prefixes denied OUTRIGHT (never decoded), mirroring guard.rs: NAT64
#: (RFC 6052 + local-use RFC 8215), 6to4, Teredo, and the deprecated IPv4-compatible ::/96.
_V6_TRANSITION: Final = tuple(
    ipaddress.ip_network(n)
    for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "::/96")
)


def _packet(request_id: int, ptype: int, body: bytes) -> bytes:
    """Encode one RCON packet."""
    return struct.pack("<iii", 10 + len(body), request_id, ptype) + body + b"\x00\x00"


def _blocked(ip: str) -> bool:
    """True if ``ip`` (a literal) is not publicly routable (stdlib classes + CGNAT)."""
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address):
        if any(addr in net for net in _V6_TRANSITION):
            return True  # includes ::, ::1 and every IPv4-compatible form
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
    return (
        is_private_host(str(addr))
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or (
            isinstance(addr, ipaddress.IPv4Address)
            and (addr in _CGNAT or any(addr in n for n in _V4_EXTRA))
        )
    )


async def resolve_pinned(host: str, port: int) -> str | None:
    """Resolve once; return the IP to dial, or ``None`` if any answer is non-public/unresolvable."""
    bare = host.strip("[]")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(bare, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    ips = [str(info[4][0]) for info in infos]
    if not ips or any(_blocked(ip) for ip in ips):
        return None
    return ips[0]


async def _login(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, password: str) -> str:
    """Perform the RCON login; the password is written to the socket only."""
    writer.write(_packet(_LOGIN_ID, _TYPE_AUTH, password.encode()))
    await writer.drain()
    for _ in range(3):
        (size,) = struct.unpack("<i", await reader.readexactly(4))
        if not 10 <= size <= _MAX_PACKET:
            return RESULT_UNREACHABLE
        rest = await reader.readexactly(size)
        request_id, ptype = struct.unpack("<ii", rest[:8])
        if ptype == _TYPE_AUTH_RESPONSE:
            return RESULT_OK_RCON if request_id == _LOGIN_ID else RESULT_AUTH_FAILED
        if request_id == -1:
            return RESULT_AUTH_FAILED
    return RESULT_UNREACHABLE


async def probe(host: str, port: int, password: str, *, timeout: float = 3.0) -> str:  # noqa: ASYNC109
    """Test one registered target; returns a closed result code."""
    ip = await resolve_pinned(host, port)
    if ip is None:
        return RESULT_FORBIDDEN
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(ip, port)
            try:
                if not password:
                    return RESULT_OK_TCP
                return await _login(reader, writer, password)
            finally:
                writer.close()
    except TimeoutError:
        return RESULT_TIMEOUT
    except (OSError, asyncio.IncompleteReadError, struct.error):
        return RESULT_UNREACHABLE
