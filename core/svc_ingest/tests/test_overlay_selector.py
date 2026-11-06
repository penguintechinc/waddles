"""Overlay selector (Python mirror of the Rust selector) + app.py suppression polarity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from quart.testing.app import LifespanError

import overlay_selector
from app import app as quart_app
from config import Config
from overlay_selector import OverlayConfigError, OverlaySelector, parse_webhook_platforms


class TestSelectorParsing:
    """Strict env-baseline parsing, default OFF."""

    def test_default_is_all_legacy(self) -> None:
        sel = OverlaySelector.from_env({})
        assert all(sel.legacy(p) for p in ("twitch", "discord", "youtube"))
        assert not sel.overlay("nonsense")

    def test_on_values(self) -> None:
        sel = OverlaySelector.from_env(
            {"OVERLAY_TWITCH_ENABLED": "true", "OVERLAY_YOUTUBE_ENABLED": " 1 "}
        )
        assert sel.overlay("twitch") and sel.overlay("youtube") and sel.legacy("discord")

    def test_explicit_off_values(self) -> None:
        sel = OverlaySelector.from_env(
            {"OVERLAY_TWITCH_ENABLED": "FALSE", "OVERLAY_YOUTUBE_ENABLED": "0"}
        )
        assert not sel.twitch and not sel.youtube

    @pytest.mark.parametrize(
        "name", ["OVERLAY_TWITCH_ENABLED", "OVERLAY_DISCORD_ENABLED", "OVERLAY_YOUTUBE_ENABLED"]
    )
    @pytest.mark.parametrize("bad", ["yes", "", "tru"])
    def test_malformed_is_fatal_and_names_item_only(self, name: str, bad: str) -> None:
        with pytest.raises(OverlayConfigError) as exc:
            OverlaySelector.from_env({name: bad})
        assert name in str(exc.value)

    def test_discord_on_blocked_until_identify_budget(self) -> None:
        env = {"OVERLAY_DISCORD_ENABLED": "true"}
        with pytest.raises(OverlayConfigError, match="identify budget, not yet ported"):
            OverlaySelector.from_env(env, identify_budget_ready=False)
        assert OverlaySelector.from_env(env, identify_budget_ready=True).discord

    def test_discord_on_follows_the_module_const(self) -> None:
        env = {"OVERLAY_DISCORD_ENABLED": "true"}
        if overlay_selector.IDENTIFY_BUDGET_READY:
            assert OverlaySelector.from_env(env).discord
        else:
            with pytest.raises(OverlayConfigError):
                OverlaySelector.from_env(env)

    def test_webhook_platforms(self) -> None:
        assert parse_webhook_platforms("") == ()
        assert parse_webhook_platforms("Twitch, discord,twitch") == ("twitch", "discord")
        for bad in ("youtube", "twitch,,discord"):
            with pytest.raises(OverlayConfigError):
                parse_webhook_platforms(bad)

    def test_eventsub_and_outbound_rules(self) -> None:
        base = {"OVERLAY_TWITCH_ENABLED": "true"}
        assert OverlaySelector.from_env(base).twitch_eventsub_legacy
        listed = {**base, "CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS": "twitch"}
        assert not OverlaySelector.from_env(listed).twitch_eventsub_legacy
        # a listed platform with the overlay OFF never suppresses
        assert OverlaySelector.from_env(
            {"CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS": "twitch"}
        ).twitch_eventsub_legacy
        assert OverlaySelector.from_env(base).twitch_outbound_legacy
        tools = {**base, "CONNECTOR_TOOLS_ENABLED": "true"}
        assert not OverlaySelector.from_env(tools).twitch_outbound_legacy

    def test_does_not_use_the_flag_client(self) -> None:
        # regression: the *.waddles.app bypass reads every flag ON.
        src = open(overlay_selector.__file__, encoding="utf-8").read().lower()
        code = "\n".join(
            ln for ln in src.splitlines() if ln.lstrip().startswith(("import", "from"))
        )
        assert "posthog" not in code and "licensing" not in code


_APP_KEYS = (
    "twitch_eventsub_handler",
    "twitch_leased_receivers",
    "twitch_outbound_drain",
    "twitch_outbound_drain_redis_client",
    "youtube_leased_receivers",
    "overlay_selector",
)


@pytest.fixture(autouse=True)
def _clean_app_config() -> object:
    """`quart_app.config` is process-global; keep this module's keys from leaking."""
    for k in _APP_KEYS:
        quart_app.config.pop(k, None)
    yield
    for k in _APP_KEYS:
        quart_app.config.pop(k, None)


@pytest.fixture
def twitch_and_youtube_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure every Python receiver so suppression is the only variable."""
    monkeypatch.setattr(Config, "TWITCH_BOT_TOKEN_REF", "FAKE_TWITCH_TOKEN_REF")
    monkeypatch.setattr(Config, "TWITCH_CHANNELS", ["somechannel"])
    monkeypatch.setattr(Config, "TWITCH_EVENTSUB_SECRET", "s3cret")
    monkeypatch.setattr(Config, "YOUTUBE_LIVE_CHANNELS", ["UC123"], raising=False)
    monkeypatch.setattr(Config, "youtube_credentials_configured", classmethod(lambda c: True))


async def _registered() -> set[str]:
    return set(quart_app.config["supervisor"]._receivers)  # noqa: SLF001 - test introspection


class TestAppSuppression:
    """app.py registers/skips receivers by the same env baselines."""

    async def test_overlays_off_registers_everything_as_before(
        self, twitch_and_youtube_configured: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for n in ("OVERLAY_TWITCH_ENABLED", "OVERLAY_YOUTUBE_ENABLED", "CONNECTOR_TOOLS_ENABLED"):
            monkeypatch.delenv(n, raising=False)
        async with quart_app.test_app():
            reg = await _registered()
            assert "twitch_irc:somechannel" in reg and "twitch_outbound_drain" in reg
            assert any(r.startswith("youtube") for r in reg)
            assert "twitch_eventsub_handler" in quart_app.config

    async def test_twitch_overlay_on_skips_inbound_keeps_outbound_and_eventsub(
        self, twitch_and_youtube_configured: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OVERLAY_TWITCH_ENABLED", "true")
        monkeypatch.delenv("CONNECTOR_TOOLS_ENABLED", raising=False)
        monkeypatch.delenv("CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS", raising=False)
        async with quart_app.test_app():
            reg = await _registered()
            assert "twitch_irc:somechannel" not in reg
            assert "twitch_outbound_drain" in reg
            assert "twitch_eventsub_handler" in quart_app.config
            assert any(r.startswith("youtube") for r in reg)

    async def test_twitch_overlay_with_tools_and_webhook_skips_all_twitch(
        self, twitch_and_youtube_configured: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OVERLAY_TWITCH_ENABLED", "true")
        monkeypatch.setenv("CONNECTOR_TOOLS_ENABLED", "true")
        monkeypatch.setenv("CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS", "twitch")
        async with quart_app.test_app():
            reg = await _registered()
            assert not any(r.startswith("twitch") for r in reg)
            assert "twitch_eventsub_handler" not in quart_app.config

    async def test_youtube_overlay_on_skips_youtube_poll(
        self, twitch_and_youtube_configured: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OVERLAY_YOUTUBE_ENABLED", "true")
        monkeypatch.delenv("OVERLAY_TWITCH_ENABLED", raising=False)
        async with quart_app.test_app():
            reg = await _registered()
            assert not any(r.startswith("youtube") for r in reg)
            assert "twitch_irc:somechannel" in reg

    async def test_discord_overlay_on_is_fatal_at_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OVERLAY_DISCORD_ENABLED", "true")
        with pytest.raises(LifespanError):
            async with quart_app.test_app():
                pass

    async def test_malformed_overlay_value_is_fatal_at_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OVERLAY_TWITCH_ENABLED", "maybe")
        with pytest.raises(LifespanError):
            async with quart_app.test_app():
                pass


_TABLE = json.loads(
    (Path(__file__).parent / "fixtures" / "overlay_selector_table.json").read_text("utf-8")
)["cases"]


class TestSharedDecisionTable:
    """Parity with the Rust selector: both read tests/fixtures/overlay_selector_table.json."""

    def test_fixture_is_not_empty(self) -> None:
        assert len(_TABLE) >= 100  # a zero-case table would pass vacuously

    @pytest.mark.parametrize(
        "case", _TABLE, ids=lambda c: json.dumps(c["env"], sort_keys=True)[:80]
    )
    def test_case(self, case: dict) -> None:
        env = case["env"]
        if case["expect"] == "error":
            with pytest.raises(OverlayConfigError):
                OverlaySelector.from_env(env, identify_budget_ready=case["identify_budget_ready"])
            return
        sel = OverlaySelector.from_env(env, identify_budget_ready=case["identify_budget_ready"])
        exp = case["expect"]
        assert sel.legacy("twitch") is exp["twitch_inbound"]
        assert sel.twitch_eventsub_legacy is exp["twitch_eventsub"]
        assert sel.twitch_outbound_legacy is exp["twitch_outbound"]
        assert sel.legacy("discord") is exp["discord_inbound"]
        assert sel.legacy("youtube") is exp["youtube_inbound"]


class TestSuppressedFactoriesNeverInvoked:
    """A suppressed platform's receiver factory must not even be called."""

    async def test_factories_gated_by_selector(
        self, twitch_and_youtube_configured: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_mod

        calls: list[str] = []
        names = {
            "_register_youtube_live_receiver": "youtube",
            "_register_twitch_eventsub": "eventsub",
        }
        for attr, tag in names.items():
            monkeypatch.setattr(app_mod, attr, lambda *a, _t=tag, **k: calls.append(_t))
        real_twitch = app_mod._register_twitch_receivers  # noqa: SLF001

        def spy(*a: object, **k: object) -> None:
            calls.append(f"twitch:in={k['register_inbound']}:out={k['register_outbound']}")
            real_twitch(*a, **k)

        monkeypatch.setattr(app_mod, "_register_twitch_receivers", spy)
        monkeypatch.setenv("OVERLAY_YOUTUBE_ENABLED", "true")
        monkeypatch.setenv("OVERLAY_TWITCH_ENABLED", "true")
        monkeypatch.setenv("CONNECTOR_RUNTIME_ENABLED", "true")
        monkeypatch.setenv("CONNECTOR_TOOLS_ENABLED", "true")
        monkeypatch.setenv("CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS", "twitch")
        async with quart_app.test_app():
            pass
        assert calls == ["twitch:in=False:out=False"]  # youtube + eventsub never called

    async def test_flag_fetch_is_never_attempted(self) -> None:
        # fail-safe direction: with no env, legacy runs; there is no flag/network input at all
        assert not OverlaySelector.from_env({}).overlay("twitch")
