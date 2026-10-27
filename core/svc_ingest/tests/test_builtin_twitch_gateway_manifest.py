"""Tests for `builtin_handlers.twitch_gateway_manifest` -- the Twitch chat + EventSub handlers."""

from __future__ import annotations

from flask_core.app_registry import AppRegistry

from builtin_handlers.twitch_gateway_manifest import (
    TWITCH_EVENTSUB_MANIFEST,
    TWITCH_GATEWAY_MANIFEST,
    register_default_bundles,
)


class TestRegisterDefaultBundles:
    def test_registers_both_valid_manifests(self) -> None:
        registry = AppRegistry()
        gateway, eventsub = register_default_bundles(registry)

        assert gateway.app_id == "waddles.bot.twitch.default"
        assert gateway.feature == "waddles.bot.twitch"
        assert gateway.is_default is True

        assert eventsub.app_id == "waddles.bot.twitchevents.eventsub"
        assert eventsub.feature == "waddles.bot.twitchevents"
        assert eventsub.is_default is True

    def test_gateway_ingest_stage_declares_twitch_message(self) -> None:
        """Also a regression test for the manifest-load crash a `gateway_socket` value caused.

        `communication_model` is thirdparty-vendor-only
        (`flask_core.app_manifest.KNOWN_COMMUNICATION_MODELS` ==
        `{webhook_push, rest_pull}`); an earlier draft set it to
        `"gateway_socket"` here, which `parse_manifest` rejects
        (`ManifestError`) -- confirmed via a live `register_default_
        bundles()` call, crashing `app.py`'s `@app.before_serving` on
        every startup regardless of whether Twitch was even configured.
        Fixed by dropping the key entirely, matching `builtin_handlers/
        discord_gateway_manifest.py`'s own precedent (transport shape is
        declared in CODE via the `waddle_transports.Transport` ABC, not
        this field).
        """
        registry = AppRegistry()
        gateway, _ = register_default_bundles(registry)

        ingest_spec = gateway.stage_specs["ingest"]
        assert ingest_spec.communication_model is None
        assert ingest_spec.consumes == ("twitch.message",)
        assert ingest_spec.entrypoint == "builtin_handlers.twitch_ingest:normalize"

    def test_eventsub_ingest_stage_declares_twitch_eventsub(self) -> None:
        registry = AppRegistry()
        _, eventsub = register_default_bundles(registry)

        ingest_spec = eventsub.stage_specs["ingest"]
        assert ingest_spec.communication_model == "webhook_push"
        assert ingest_spec.consumes == ("twitch.eventsub",)
        assert ingest_spec.entrypoint == "builtin_handlers.twitch_eventsub_ingest:normalize"

    def test_both_manifests_are_retrievable_from_the_registry(self) -> None:
        registry = AppRegistry()
        register_default_bundles(registry)

        assert registry.get("waddles.bot.twitch.default").app_id == "waddles.bot.twitch.default"
        assert (
            registry.get("waddles.bot.twitchevents.eventsub").app_id
            == "waddles.bot.twitchevents.eventsub"
        )

    def test_raw_manifest_dicts_match_the_seeded_shapes(self) -> None:
        """Loose coupling check against the DB rows.

        `TWITCH_GATEWAY_MANIFEST` must match
        `083_discord_twitch_demo_convergence.sql`'s twitch row (on the
        merged `feature/v3-svc-gateway-discord` branch); the eventsub
        manifest currently has no DB row (out of demo scope, deferred --
        see this module's own docstring). Both must describe the
        identical bundles (same app_id/entrypoint/consumes); the SQL
        itself isn't executable here.
        """
        gateway_stages = TWITCH_GATEWAY_MANIFEST["stages"]
        assert TWITCH_GATEWAY_MANIFEST["app_id"] == "waddles.bot.twitch.default"
        assert gateway_stages["ingest"]["entrypoint"] == "builtin_handlers.twitch_ingest:normalize"
        assert gateway_stages["ingest"]["consumes"] == ["twitch.message"]
        assert "communication_model" not in gateway_stages["ingest"]

        eventsub_stages = TWITCH_EVENTSUB_MANIFEST["stages"]
        assert TWITCH_EVENTSUB_MANIFEST["app_id"] == "waddles.bot.twitchevents.eventsub"
        assert (
            eventsub_stages["ingest"]["entrypoint"]
            == "builtin_handlers.twitch_eventsub_ingest:normalize"
        )
        assert eventsub_stages["ingest"]["consumes"] == ["twitch.eventsub"]
