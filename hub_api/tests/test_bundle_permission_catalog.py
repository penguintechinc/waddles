"""Tests for `services.bundle_permission_catalog`."""

from __future__ import annotations

from services.bundle_permission_catalog import (
    CORE_NAMESPACE_PREFIX,
    is_dangerous,
    is_known_permission,
    resolve_risk,
)


def test_static_normal_permission_resolves() -> None:
    assert resolve_risk("storage.kv") == "normal"
    assert not is_dangerous("storage.kv")
    assert is_known_permission("storage.kv")


def test_static_dangerous_permission_resolves() -> None:
    assert resolve_risk("ai.generate") == "dangerous"
    assert is_dangerous("ai.generate")


def test_net_http_family_is_dangerous() -> None:
    assert resolve_risk("net.http:api.weatherapi.example.com") == "dangerous"
    assert is_dangerous("net.http:api.weatherapi.example.com")


def test_net_http_family_rejects_scheme() -> None:
    assert resolve_risk("net.http:https://evil.example.com") is None


def test_chat_send_family_is_normal_for_known_platform() -> None:
    assert resolve_risk("chat.send:discord") == "normal"
    assert resolve_risk("chat.send:twitch") == "normal"


def test_chat_send_family_unknown_platform_is_unknown() -> None:
    assert resolve_risk("chat.send:myspace") is None


def test_moderation_family_is_dangerous_for_known_platform() -> None:
    assert resolve_risk("moderation.twitch") == "dangerous"


def test_moderation_family_unknown_platform_is_unknown() -> None:
    assert resolve_risk("moderation.myspace") is None


def test_unknown_permission_id_resolves_to_none() -> None:
    assert resolve_risk("storage.nonexistent") is None
    assert not is_known_permission("storage.nonexistent")
    assert not is_dangerous("storage.nonexistent")


def test_core_namespace_prefix_matches_vendor_bundle_authz() -> None:
    import services.vendor_bundle_authz as vendor_bundle_authz

    assert CORE_NAMESPACE_PREFIX == vendor_bundle_authz.CORE_NAMESPACE_PREFIX
