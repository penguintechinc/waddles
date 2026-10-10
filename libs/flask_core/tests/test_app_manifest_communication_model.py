"""Tests for `communication_model` validation (`webhook_push`/`rest_pull` enum).

`test_app_framework.py` already covers both legitimate values being
accepted (its own `communication_model` cases); this file covers the new
behavior added alongside them -- rejecting anything else with a typed
`ManifestError`, and a stage that never sets it at all still parsing fine.
A persistent-socket transport (Discord gateway etc.) is deliberately NOT
a third value here -- see `app_manifest.py`'s own `KNOWN_COMMUNICATION_
MODELS` comment for why that's modeled by the shared `waddle_transports`
boundary instead.
"""

from __future__ import annotations

from flask_core.app_manifest import (
    REASON_INVALID_COMMUNICATION_MODEL,
    ManifestError,
    parse_manifest,
)

import pytest

_BASE: dict[str, object] = {
    "app_id": "waddles.bot.discord.gateway",
    "name": "Discord Gateway Ingest",
    "version": "1.0.0",
    "feature": "waddles.bot.discord",
    "module": "bot",
    "provider": "builtin",
    "is_default": True,
}


def test_unknown_communication_model_is_rejected() -> None:
    data = dict(_BASE)
    data["stages"] = {"action": {"communication_model": "gateway_socket"}}
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest(data)
    assert exc_info.value.reason == REASON_INVALID_COMMUNICATION_MODEL


def test_missing_communication_model_still_parses() -> None:
    data = dict(_BASE)
    data["stages"] = {"ingest": {"entrypoint": "builtin_handlers.discord_ingest:normalize"}}
    manifest = parse_manifest(data)
    assert manifest.stage_specs["ingest"].communication_model is None
