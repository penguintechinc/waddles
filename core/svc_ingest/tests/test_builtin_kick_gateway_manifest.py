"""Tests for `builtin_handlers.kick_gateway_manifest` -- the seeded Kick gateway ingest handler."""

from __future__ import annotations

from typing import Any

from flask_core.app_registry import AppRegistry

from builtin_handlers.kick_gateway_manifest import KICK_GATEWAY_MANIFEST, register_default_bundles
from fanout import fan_out_event

TENANT = "acme-corp"


class TestRegisterDefaultBundles:
    def test_registers_a_valid_manifest(self) -> None:
        registry = AppRegistry()
        manifest = register_default_bundles(registry)

        assert manifest.app_id == "waddles.bot.kick.default"
        assert manifest.feature == "waddles.bot.kick"
        assert manifest.is_default is True

    def test_ingest_stage_declares_kick_message_and_no_communication_model(self) -> None:
        """Transport shape (persistent socket, inbound) is declared in CODE, not the manifest.

        See `kick_gateway_manifest.py`'s own docstring for why
        `communication_model` stays unset here (that field is
        thirdparty-vendor-only).
        """
        registry = AppRegistry()
        manifest = register_default_bundles(registry)

        ingest_spec = manifest.stage_specs["ingest"]
        assert ingest_spec.communication_model is None
        assert ingest_spec.consumes == ("kick.message",)
        assert ingest_spec.entrypoint == "builtin_handlers.kick_ingest:normalize"

    def test_registered_manifest_is_retrievable_from_the_registry(self) -> None:
        registry = AppRegistry()
        register_default_bundles(registry)

        assert registry.get("waddles.bot.kick.default").app_id == "waddles.bot.kick.default"

    def test_raw_manifest_dict_matches_the_declared_shape(self) -> None:
        """Loose coupling check: the raw dict this module ships must match the parsed manifest."""
        stages = KICK_GATEWAY_MANIFEST["stages"]
        assert KICK_GATEWAY_MANIFEST["app_id"] == "waddles.bot.kick.default"
        assert stages["ingest"]["entrypoint"] == "builtin_handlers.kick_ingest:normalize"
        assert stages["ingest"]["consumes"] == ["kick.message"]
        assert "communication_model" not in stages["ingest"]


class TestFanOutReachesTheRegisteredManifest:
    """Without this manifest, `fan_out_event` finds zero consumers for `kick.message`.

    Proves the real, registered `KICK_GATEWAY_MANIFEST` (not a synthetic
    stand-in) is what `receivers/kick_pusher.py`'s `CONSUMES_TAG` fan-out
    actually reaches -- `test_fanout.py` covers `fan_out_event`'s generic
    resolution logic against synthetic manifests; this test covers THIS
    bundle's own manifest wiring specifically.
    """

    async def test_kick_message_finds_at_least_one_consumer(self, redis_client: Any) -> None:
        registry = AppRegistry()
        register_default_bundles(registry)

        fanned_out_count = await fan_out_event(
            {"platform": "kick", "text": "hi", "chatroom_id": 1, "channel_slug": "acme"},
            consumes_tag="kick.message",
            tenant=TENANT,
            community=None,
            redis_client=redis_client,
            registry=registry,
        )

        assert fanned_out_count >= 1
