"""ICE server list handed to browser WHIP/WHEP clients, from configuration.

`STREAMING_RTC_ICE_SERVERS` is an optional JSON array of `{"urls": ["stun:host:port", ...]}`
objects. Unset or empty (the default) means no ICE servers: the browser then uses host/srflx
candidates only. TURN is deliberately NOT supported yet (tracked for v3.3.x, #1318): a `turn:` /
`turns:` URL, or any `username` / `credential` key, is refused loudly rather than half-honoured,
so no relay credential can leak into a response or an environment by accident.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from services.calls_proxy import StreamingTargetNotConfiguredError

_ENV = "STREAMING_RTC_ICE_SERVERS"
_STUN_SCHEMES = ("stun:", "stuns:")
_MAX_SERVERS = 8


class IceConfigError(StreamingTargetNotConfiguredError):
    """`STREAMING_RTC_ICE_SERVERS` is malformed (a deployment error, answered with a 503)."""


@dataclass(slots=True, frozen=True)
class IceServer:
    """One STUN entry in the RTCPeerConnection `iceServers` shape (no credentials)."""

    urls: list[str]


def ice_servers_from_env() -> list[IceServer]:
    """Parse and validate `STREAMING_RTC_ICE_SERVERS`; `[]` when unset or blank."""
    raw = os.getenv(_ENV, "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IceConfigError(f"{_ENV} is not valid JSON") from exc
    if not isinstance(data, list) or len(data) > _MAX_SERVERS:
        raise IceConfigError(f"{_ENV} must be a JSON array of at most {_MAX_SERVERS} entries")
    servers: list[IceServer] = []
    for entry in data:
        if not isinstance(entry, dict) or set(entry) != {"urls"}:
            raise IceConfigError(
                f'{_ENV} entries must be exactly {{"urls": [...]}} (TURN credentials are not '
                "supported yet)"
            )
        urls = entry["urls"]
        if (
            not isinstance(urls, list)
            or not urls
            or not all(isinstance(u, str) and u.startswith(_STUN_SCHEMES) for u in urls)
        ):
            raise IceConfigError(f"{_ENV} urls must be a non-empty list of stun:/stuns: URLs")
        servers.append(IceServer(urls=list(urls)))
    return servers
