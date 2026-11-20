"""Tests for `AppRegistry.all_apps()` (svc-gateway design, 2026-09-02).

Added because svc-gateway's fan-out needs to scan every registered App's
ingest `consumes` tags across ALL Features, not one known Feature at a
time -- `apps_for_feature`/`default_app_for`/`get` all require the caller
to already know which Feature/app_id to ask about.
"""

from __future__ import annotations

from flask_core.app_manifest import parse_manifest
from flask_core.app_registry import AppRegistry

_BASE: dict[str, object] = {
    "version": "1.0.0",
    "module": "bot",
    "provider": "builtin",
}


def _manifest(app_id: str, feature: str) -> object:
    data = dict(_BASE)
    data["app_id"] = app_id
    data["feature"] = feature
    data["name"] = app_id
    return parse_manifest(data)


def test_all_apps_returns_every_registration_in_order() -> None:
    registry = AppRegistry()
    first = _manifest("penguintechinc.waddles.discord-gateway", "penguintechinc.waddles.discord")
    second = _manifest("penguintechinc.waddles.twitch-gateway", "penguintechinc.waddles.twitch")
    registry.register(first)
    registry.register(second)

    assert registry.all_apps() == (first, second)


def test_all_apps_empty_registry_returns_empty_tuple() -> None:
    assert AppRegistry().all_apps() == ()
