"""Tests for `builtin_handlers.slack_gateway_manifest` -- seeded Slack gateway ingest handler."""

from __future__ import annotations

from typing import Any

from flask_core.app_registry import AppRegistry

from builtin_handlers.slack_gateway_manifest import SLACK_GATEWAY_MANIFEST, register_default_bundles
from fanout import fan_out_event

TENANT = "acme-corp"


class TestRegisterDefaultBundles:
    def test_registers_a_valid_manifest(self) -> None:
        registry = AppRegistry()
        manifest = register_default_bundles(registry)

        assert manifest.app_id == "waddles.bot.slack.default"
        assert manifest.feature == "waddles.bot.slack"
        assert manifest.is_default is True

    def test_ingest_stage_declares_slack_message_and_no_communication_model(self) -> None:
        """Transport shape (persistent socket, inbound) is declared in CODE, not the manifest.

        See `slack_gateway_manifest.py`'s own docstring for why
        `communication_model` stays unset here (that field is
        thirdparty-vendor-only).
        """
        registry = AppRegistry()
        manifest = register_default_bundles(registry)

        ingest_spec = manifest.stage_specs["ingest"]
        assert ingest_spec.communication_model is None
        assert ingest_spec.consumes == ("slack.message",)
        assert ingest_spec.entrypoint == "builtin_handlers.slack_ingest:normalize"

    def test_registered_manifest_is_retrievable_from_the_registry(self) -> None:
        registry = AppRegistry()
        register_default_bundles(registry)

        assert registry.get("waddles.bot.slack.default").app_id == "waddles.bot.slack.default"

    def test_raw_manifest_dict_matches_the_declared_shape(self) -> None:
        """Loose coupling check: the raw dict this module ships must match the parsed manifest."""
        stages = SLACK_GATEWAY_MANIFEST["stages"]
        assert SLACK_GATEWAY_MANIFEST["app_id"] == "waddles.bot.slack.default"
        assert stages["ingest"]["entrypoint"] == "builtin_handlers.slack_ingest:normalize"
        assert stages["ingest"]["consumes"] == ["slack.message"]
        assert "communication_model" not in stages["ingest"]


class TestFanOutReachesTheRegisteredManifest:
    """Regression for gh-318: without this manifest, `fan_out_event` finds zero consumers.

    Proves the real, registered `SLACK_GATEWAY_MANIFEST` (not a synthetic
    stand-in) is what `receivers/slack_socket.py`'s `CONSUMES_TAG` fan-out
    actually reaches -- `test_fanout.py` covers `fan_out_event`'s generic
    resolution logic against synthetic manifests; this test covers THIS
    bundle's own manifest wiring specifically.
    """

    async def test_slack_message_finds_at_least_one_consumer(self, redis_client: Any) -> None:
        registry = AppRegistry()
        register_default_bundles(registry)

        fanned_out_count = await fan_out_event(
            {"event_type": "message", "text": "hi", "platform_user_id": "U123"},
            consumes_tag="slack.message",
            tenant=TENANT,
            community=None,
            redis_client=redis_client,
            registry=registry,
        )

        assert fanned_out_count >= 1
