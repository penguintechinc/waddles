"""`services/discord_bot_install.py` -- the SaaS/global bot-install link builder.

No Discord call ever happens here -- `build_install_url()` is pure string
building off an env var, so these tests assert the URL shape and the
fail-loud `ProviderNotConfigured` path directly, no HTTP mocking needed.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from services.discord_bot_install import (
    DISCORD_BOT_INSTALL_INTEGRATION_TYPE,
    DISCORD_BOT_INSTALL_PERMISSIONS,
    DISCORD_BOT_INSTALL_SCOPES,
    build_install_url,
)
from services.oauth_providers import ProviderNotConfigured


def test_build_install_url_missing_client_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_CLIENT_ID", raising=False)
    with pytest.raises(ProviderNotConfigured):
        build_install_url()


def test_build_install_url_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_CLIENT_ID", "123456789012345678")
    url = build_install_url()

    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "discord.com"
    assert parsed.path == "/oauth2/authorize"

    params = parse_qs(parsed.query)
    assert params["client_id"] == ["123456789012345678"]
    assert params["scope"] == [DISCORD_BOT_INSTALL_SCOPES]
    assert params["permissions"] == [str(DISCORD_BOT_INSTALL_PERMISSIONS)]
    assert params["integration_type"] == [str(DISCORD_BOT_INSTALL_INTEGRATION_TYPE)]


def test_build_install_url_never_leaks_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The install link is client-id-only -- it must never reference a client secret/bot token."""
    monkeypatch.setenv("DISCORD_CLIENT_ID", "123456789012345678")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "s3cret-should-never-appear")  # gitleaks:allow
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token-should-never-appear")  # gitleaks:allow

    url = build_install_url()

    assert "s3cret-should-never-appear" not in url
    assert "bot-token-should-never-appear" not in url


def test_permissions_bitfield_covers_expected_bits() -> None:
    """Spot-check a subset of the named bits the module docstring maps to real code paths.

    Not a brittle "exact magic number" test -- new permission additions
    should not need this test rewritten, only an OR against the new bit.
    """
    expected_bits = {
        1 << 1: "KICK_MEMBERS",
        1 << 2: "BAN_MEMBERS",
        1 << 6: "ADD_REACTIONS",
        1 << 10: "VIEW_CHANNEL",
        1 << 11: "SEND_MESSAGES",
        1 << 13: "MANAGE_MESSAGES",
        1 << 14: "EMBED_LINKS",
        1 << 16: "READ_MESSAGE_HISTORY",
        1 << 28: "MANAGE_ROLES",
        1 << 29: "MANAGE_WEBHOOKS",
        1 << 33: "MANAGE_EVENTS",
        1 << 40: "MODERATE_MEMBERS",
    }
    for bit, name in expected_bits.items():
        assert DISCORD_BOT_INSTALL_PERMISSIONS & bit, f"missing {name} ({bit}) bit"

    # Never request Administrator (1 << 3).
    assert not DISCORD_BOT_INSTALL_PERMISSIONS & (1 << 3)
    # Never request voice CONNECT/SPEAK -- DiscordVoiceSink is an
    # unimplemented scaffold, not a shipped feature.
    assert not DISCORD_BOT_INSTALL_PERMISSIONS & (1 << 20)
    assert not DISCORD_BOT_INSTALL_PERMISSIONS & (1 << 21)
