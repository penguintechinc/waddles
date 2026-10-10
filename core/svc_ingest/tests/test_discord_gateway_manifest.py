"""Tests for `builtin_handlers.discord_gateway_manifest` -- Discord gateway ingest handler."""

from __future__ import annotations

from flask_core.app_registry import AppRegistry

from builtin_handlers.discord_gateway_manifest import (
    DISCORD_GATEWAY_MANIFEST,
    register_default_bundles,
)


class TestRegisterDefaultBundles:
    def test_registers_a_valid_manifest(self) -> None:
        registry = AppRegistry()
        manifest = register_default_bundles(registry)

        assert manifest.app_id == "waddles.bot.discord.default"
        assert manifest.feature == "waddles.bot.discord"
        assert manifest.is_default is True

    def test_ingest_stage_declares_discord_message_and_no_communication_model(self) -> None:
        """Transport shape (persistent socket, inbound) is declared in CODE, not the manifest.

        See `discord_gateway_manifest.py`'s own docstring for why
        `communication_model` stays unset here (that field is
        thirdparty-vendor-only).
        """
        registry = AppRegistry()
        manifest = register_default_bundles(registry)

        ingest_spec = manifest.stage_specs["ingest"]
        assert ingest_spec.communication_model is None
        assert ingest_spec.consumes == ("discord.message",)
        assert ingest_spec.entrypoint == "builtin_handlers.discord_ingest:normalize"

    def test_registered_manifest_is_retrievable_from_the_registry(self) -> None:
        registry = AppRegistry()
        register_default_bundles(registry)

        assert registry.get("waddles.bot.discord.default").app_id == "waddles.bot.discord.default"

    def test_raw_manifest_dict_matches_the_unified_migrations_seeded_shape(self) -> None:
        """Loose coupling check against 083_discord_twitch_demo_convergence.sql's discord row.

        Both must describe the identical bundle (same
        app_id/entrypoint/consumes) -- this test only asserts the
        in-process side; the SQL itself isn't executable here.
        """
        stages = DISCORD_GATEWAY_MANIFEST["stages"]
        assert DISCORD_GATEWAY_MANIFEST["app_id"] == "waddles.bot.discord.default"
        assert stages["ingest"]["entrypoint"] == "builtin_handlers.discord_ingest:normalize"
        assert stages["ingest"]["consumes"] == ["discord.message"]
        assert "communication_model" not in stages["ingest"]
