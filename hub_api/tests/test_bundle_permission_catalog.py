"""Tests for `services.bundle_permission_catalog`."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from services.bundle_permission_catalog import (
    CORE_NAMESPACE_PREFIX,
    is_dangerous,
    is_known_permission,
    is_valid_fqdn,
    is_valid_private_ip_or_cidr,
    is_valid_public_ip,
    permission_family,
    resolve_risk,
)


def test_static_normal_permission_resolves() -> None:
    assert resolve_risk("storage.kv") == "normal"
    assert not is_dangerous("storage.kv")
    assert is_known_permission("storage.kv")


def test_static_dangerous_permission_resolves() -> None:
    assert resolve_risk("ai.generate") == "dangerous"
    assert is_dangerous("ai.generate")


def test_interaction_pii_receive_is_dangerous() -> None:
    """Raw PII in form/modal/interaction inputs is a `dangerous`, default-no catalog member."""
    assert resolve_risk("interaction.pii.receive") == "dangerous"
    assert is_dangerous("interaction.pii.receive")
    assert is_known_permission("interaction.pii.receive")


def test_streaming_lifecycle_subscribe_is_normal() -> None:
    """Read-only subscribe to svc-streaming-rust's lifecycle hooks (issue #456) is `normal`."""
    assert resolve_risk("streaming.lifecycle.subscribe") == "normal"
    assert not is_dangerous("streaming.lifecycle.subscribe")
    assert is_known_permission("streaming.lifecycle.subscribe")


def test_chat_send_family_is_normal_for_known_platform() -> None:
    assert resolve_risk("chat.send:discord") == "normal"
    assert resolve_risk("chat.send:twitch") == "normal"


def test_chat_send_family_unknown_platform_is_unknown() -> None:
    assert resolve_risk("chat.send:myspace") is None


def test_moderation_family_is_dangerous_for_known_platform() -> None:
    assert resolve_risk("moderation.twitch") == "dangerous"


def test_moderation_family_unknown_platform_is_unknown() -> None:
    assert resolve_risk("moderation.myspace") is None


@pytest.mark.parametrize("family", ["chat.delete", "dm.send"])
def test_provider_framework_outbound_families_are_dangerous(family: str) -> None:
    """`chat.delete:`/`dm.send:<platform>` (issue #719) are `dangerous` for known platforms."""
    for platform in ("discord", "twitch"):
        assert resolve_risk(f"{family}:{platform}") == "dangerous"
        assert is_dangerous(f"{family}:{platform}")
        assert is_known_permission(f"{family}:{platform}")
        assert permission_family(f"{family}:{platform}") == family


@pytest.mark.parametrize(
    "raw", ["chat.delete", "dm.send", "chat.delete:myspace", "dm.send:myspace"]
)
def test_provider_framework_outbound_families_reject_bare_or_unknown_platform(raw: str) -> None:
    """A bare family id or an uncompiled platform is not a catalog member (fail closed)."""
    assert resolve_risk(raw) is None


def test_python_catalog_matches_rust_catalog_for_outbound_families() -> None:
    """The Rust gate catalog and this module must both define the new outbound families."""
    from pathlib import Path

    rust = Path(__file__).resolve().parents[2] / "core/bundle_capability_gate/src/permission.rs"
    if not rust.exists():
        pytest.skip("Rust catalog source not present in this checkout (hub_api-only image)")
    text = rust.read_text()
    for family in ("chat.delete", "dm.send"):
        assert f'=> "{family}"' in text, f"{family} missing from the Rust catalog"
        assert resolve_risk(f"{family}:discord") == "dangerous"


def test_unknown_permission_id_resolves_to_none() -> None:
    assert resolve_risk("storage.nonexistent") is None
    assert not is_known_permission("storage.nonexistent")
    assert not is_dangerous("storage.nonexistent")


def test_core_namespace_prefix_matches_vendor_bundle_authz() -> None:
    import services.vendor_bundle_authz as vendor_bundle_authz

    assert CORE_NAMESPACE_PREFIX == vendor_bundle_authz.CORE_NAMESPACE_PREFIX


# -- net.http.fqdn --------------------------------------------------------


def test_net_http_fqdn_is_normal() -> None:
    assert resolve_risk("net.http.fqdn:api.weatherapi.example.com") == "normal"
    assert not is_dangerous("net.http.fqdn:api.weatherapi.example.com")
    assert is_valid_fqdn("api.weatherapi.example.com")


def test_net_http_fqdn_rejects_scheme() -> None:
    assert resolve_risk("net.http.fqdn:https://evil.example.com") is None
    assert not is_valid_fqdn("https://evil.example.com")


def test_net_http_fqdn_rejects_wildcard() -> None:
    assert resolve_risk("net.http.fqdn:*.example.com") is None
    assert not is_valid_fqdn("*.example.com")


def test_net_http_fqdn_rejects_ip_literal() -> None:
    assert resolve_risk("net.http.fqdn:93.184.216.34") is None
    assert not is_valid_fqdn("93.184.216.34")
    assert resolve_risk("net.http.fqdn:2001:db8::1") is None


# -- net.http.public-ip ----------------------------------------------------


def test_net_http_public_ip_is_dangerous() -> None:
    assert resolve_risk("net.http.public-ip:93.184.216.34") == "dangerous"
    assert is_dangerous("net.http.public-ip:93.184.216.34")
    assert is_valid_public_ip("93.184.216.34")


def test_net_http_public_ip_rejects_cidr() -> None:
    assert resolve_risk("net.http.public-ip:93.184.216.0/24") is None
    assert not is_valid_public_ip("93.184.216.0/24")


def test_net_http_public_ip_rejects_private_address() -> None:
    assert resolve_risk("net.http.public-ip:10.0.0.5") is None
    assert not is_valid_public_ip("10.0.0.5")


def test_net_http_public_ip_rejects_loopback() -> None:
    assert not is_valid_public_ip("127.0.0.1")
    assert not is_valid_public_ip("::1")


def test_net_http_public_ip_rejects_link_local_and_metadata() -> None:
    assert not is_valid_public_ip("169.254.169.254")
    assert not is_valid_public_ip("169.254.1.1")
    assert not is_valid_public_ip("fe80::1")


def test_net_http_public_ip_rejects_garbage() -> None:
    assert not is_valid_public_ip("not-an-ip")
    assert not is_valid_public_ip("")


# -- net.http.private-ip ---------------------------------------------------


def test_net_http_private_ip_single_address_is_dangerous() -> None:
    assert resolve_risk("net.http.private-ip:10.0.0.5") == "dangerous"
    assert is_dangerous("net.http.private-ip:10.0.0.5")
    assert is_valid_private_ip_or_cidr("10.0.0.5")


def test_net_http_private_ip_cidr_within_bound_is_dangerous() -> None:
    assert resolve_risk("net.http.private-ip:10.20.0.0/16") == "dangerous"
    assert is_valid_private_ip_or_cidr("10.20.0.0/16")


def test_net_http_private_ip_rejects_coarser_than_slash16() -> None:
    assert resolve_risk("net.http.private-ip:10.0.0.0/8") is None
    assert not is_valid_private_ip_or_cidr("10.0.0.0/8")


def test_net_http_private_ip_rejects_public_address() -> None:
    assert resolve_risk("net.http.private-ip:93.184.216.34") is None
    assert not is_valid_private_ip_or_cidr("93.184.216.34")


def test_net_http_private_ip_rejects_loopback() -> None:
    assert not is_valid_private_ip_or_cidr("127.0.0.1")
    assert not is_valid_private_ip_or_cidr("127.0.0.0/24")


def test_net_http_private_ip_rejects_link_local_and_metadata() -> None:
    assert not is_valid_private_ip_or_cidr("169.254.169.254")
    assert not is_valid_private_ip_or_cidr("169.254.0.0/24")


def test_net_http_private_ip_rejects_garbage() -> None:
    assert not is_valid_private_ip_or_cidr("not-a-cidr")


def test_net_http_private_ip_v6_bound() -> None:
    assert is_valid_private_ip_or_cidr("fd12:3456:789a::/64")
    assert not is_valid_private_ip_or_cidr("fd12:3456:789a::/48")
    assert not is_valid_private_ip_or_cidr("::1/128")


@pytest.fixture
def cluster_cidrs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CLUSTER_POD_CIDR", "10.42.0.0/16")
    monkeypatch.setenv("CLUSTER_SERVICE_CIDR", "10.43.0.0/16")
    monkeypatch.setenv("CLUSTER_NODE_CIDR", "10.44.0.0/16")
    yield
    for key in ("CLUSTER_POD_CIDR", "CLUSTER_SERVICE_CIDR", "CLUSTER_NODE_CIDR"):
        os.environ.pop(key, None)


def test_net_http_private_ip_rejects_cluster_pod_cidr(cluster_cidrs: None) -> None:
    assert not is_valid_private_ip_or_cidr("10.42.5.5")
    assert not is_valid_private_ip_or_cidr("10.42.0.0/16")


def test_net_http_private_ip_rejects_cluster_service_and_node_cidrs(cluster_cidrs: None) -> None:
    assert not is_valid_private_ip_or_cidr("10.43.1.1")
    assert not is_valid_private_ip_or_cidr("10.44.1.1")


def test_net_http_private_ip_unaffected_outside_cluster_cidrs(cluster_cidrs: None) -> None:
    assert is_valid_private_ip_or_cidr("10.99.0.0/16")
