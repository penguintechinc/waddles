"""The hub-side SSRF classifier must mirror the Rust guard (`guard.rs`) exactly.

``BLOCKED`` is copied verbatim from `core/bundle_host_gameserver_net/src/guard.rs`
(`blocks_every_non_public_class`) and extended with the NAT64 local-use prefix; if either side
drifts, one of the two suites fails. Regression: the classifier previously let NAT64 / 6to4 /
Teredo forms through.
"""

from __future__ import annotations

import pytest

from services.provider_connections.gameserver_probe import _blocked, resolve_pinned

BLOCKED = [
    "10.0.0.5",
    "172.16.4.4",
    "192.168.1.1",
    "127.0.0.1",
    "169.254.169.254",
    "169.254.1.1",
    "100.64.0.1",
    "100.100.100.200",
    "224.0.0.1",
    "0.0.0.0",  # noqa: S104
    "0.1.2.3",
    "240.0.0.1",
    "255.255.255.255",
    "198.18.0.1",
    "192.0.0.8",
    "::1",
    "::",
    "fc00::1",
    "fd12::1",
    "fe80::1",
    "ff02::1",
    "::ffff:10.0.0.1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "64:ff9b::a00:1",
    "64:ff9b::808:808",
    "64:ff9b:1::1",
    "64:ff9b:1:ffff::1",
    "2002::1",
    "2002:a00:1::1",
    "2001::1",
    "::a00:1",
]
ALLOWED = ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"]


@pytest.mark.parametrize("ip", BLOCKED)
def test_every_blocked_class_is_refused(ip: str) -> None:
    assert _blocked(ip), ip


@pytest.mark.parametrize("ip", ALLOWED)
def test_public_addresses_are_allowed(ip: str) -> None:
    assert not _blocked(ip), ip


@pytest.mark.parametrize("host", ["64:ff9b::a00:1", "2002::1", "2001::1", "[64:ff9b:1::1]"])
async def test_resolve_pinned_refuses_transition_literals(host: str) -> None:
    assert await resolve_pinned(host, 25575) is None
