"""Member-visible RCON info (`rcon/info/:id/status|players`) is an explicit allowlist DTO.

The server-manager upstream is outside this repo, so its payload is untrusted. The member response
is built only from allowlisted, type-checked fields: odd key spellings, nested objects and
IP / `host:port` / URL values never reach a plain community member.
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import bot_rcon

STATUS_FIELDS = {
    "online", "server_name", "players_online", "max_players", "map", "game", "version",
}  # fmt: skip
PLAYER_FIELDS = {"name", "score", "ping", "duration"}
RESPONSE_FIELDS = {"success", "status"}
HOST = "gs.internal.example"
DB = {"server_name": "Survival", "game": "cs2", "host": HOST}
ROW = SimpleNamespace(display_name="Survival", game_name="cs2", host=HOST)

STUFFED_STATUS = {
    "online": True,
    "server_name": "1.2.3.4:27015",
    "players": 3,
    "max_players": 20,
    "map": "de_dust2",
    "game": "cs2",
    "version": "1.0.7",
    "serverIp": "10.1.2.3",
    "connect_str": "connect 10.1.2.3:27015; password hunter2",
    "rcon_password": "hunter2",
    "host": "gs.internal.example",
    "game_port": 28015,
    "rconPort": 28016,
    "status_url": "http://internal/status",
    "conn": {"addr": "10.9.9.9:1234"},
}
UPSTREAM = {
    "success": True,
    "status": STUFFED_STATUS,
    "players": [
        {
            "name": "alice",
            "score": 12,
            "ping": 30,
            "duration": 61.5,
            "ip": "9.9.9.9",
            "steam_id_token": "t",
            "addr": {"ip": "8.8.8.8"},
        },
        {"name": "9.9.9.9:27005", "ping": "30", "score": True},
    ],
}
LEAKS = (
    "10.1.2.3", "27015", "hunter2", "gs.internal", "28015", "28016", "internal/status", "10.9.9.9",
    "9.9.9.9", "8.8.8.8", "serverIp", "connect_str", "rcon_password", "conn", "addr", "1.2.3.4",
)  # fmt: skip


def test_status_drops_unknown_keys_and_redacts_ip_values() -> None:
    shaped = bot_rcon.shape_member_status(UPSTREAM, **DB)
    assert dataclasses.asdict(shaped) == {
        "success": True,
        "status": {
            "online": True,
            "server_name": "Survival",  # from the DB row; upstream "1.2.3.4:27015" never used
            "players_online": 3,
            "max_players": 20,
            "map": "de_dust2",
            "game": "cs2",  # from the DB row, not upstream
            "version": "1.0.7",
        },
    }
    text = str(shaped)
    for leaked in LEAKS:
        assert leaked not in text


def test_status_exact_field_set() -> None:
    shaped = bot_rcon.shape_member_status(UPSTREAM, **DB)
    assert {f.name for f in dataclasses.fields(shaped)} == RESPONSE_FIELDS
    assert {f.name for f in dataclasses.fields(shaped.status)} == STATUS_FIELDS
    assert set(dataclasses.asdict(shaped)["status"]) == STATUS_FIELDS


def test_players_exact_field_set_and_values() -> None:
    shaped = bot_rcon.shape_member_players(UPSTREAM, host=HOST)
    assert {f.name for f in dataclasses.fields(shaped)} == {"success", "players"}
    for player in dataclasses.asdict(shaped)["players"]:
        assert set(player) == PLAYER_FIELDS
    assert shaped.players[0] == bot_rcon.MemberPlayer(
        name="alice", score=12.0, ping=30, duration=61.5
    )
    # name is a host:port -> redacted; ping str and score bool rejected by type validation.
    assert shaped.players[1] == bot_rcon.MemberPlayer()
    text = str(shaped)
    for leaked in LEAKS:
        assert leaked not in text


@pytest.mark.parametrize(
    "value",
    [
        "1.2.3.4",
        "1.2.3.4:27015",
        "play.example.com:27015",
        "http://x.example/y",
        "steam://connect/1.2.3.4",
        "www.example.com",
        "fe80::1",
        "play.example.com",  # bare FQDN, no port
        "Play.Example.COM",
        "play\uff0eexample\uff0ecom",  # full-width dots
        "play\u3002example\u3002com",  # ideographic full stop
        "\uff50\uff4c\uff41\uff59.\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45.\uff43\uff4f\uff4d",  # noqa: E501 full-width letters
        "pla\u200by.exam\u200bple.com",  # zero-width chars
        "\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444",  # IDN FQDN
        "map on GS.Internal.Example now",  # configured host embedded
        "Gs\uff0einternal\uff0eexample",  # configured host, full-width dots
        "[2001:db8::1]:27015",
    ],
)
def test_string_values_with_connection_details_are_redacted(
    value: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=bot_rcon.logger.name):
        shaped = bot_rcon.shape_member_status({"success": True, "status": {"map": value}}, **DB)
    assert shaped.status.map is None
    assert any("redacted" in r.getMessage() for r in caplog.records)
    assert value not in caplog.text  # warning is PII-free: field name only


@pytest.mark.parametrize("value", ["de_dust2", "Survival Server #1", "v1.2.3", "Rust: Hardcore"])
def test_benign_strings_pass_through(value: str) -> None:
    assert bot_rcon.shape_member_status({"status": {"map": value}}, **DB).status.map == value


def test_type_validation_and_malformed_payloads() -> None:
    shaped = bot_rcon.shape_member_status(
        {"status": {"online": "yes", "players_online": "3", "max_players": -1, "map": 5}}, **DB
    )
    assert shaped.status == bot_rcon.MemberServerStatus(server_name="Survival", game="cs2")
    assert shaped.success is False
    for junk in (None, [], "x", 3):
        assert bot_rcon.shape_member_status(junk, **DB).status == bot_rcon.MemberServerStatus(
            server_name="Survival", game="cs2"
        )
        assert bot_rcon.shape_member_players(junk).players == []
    assert bot_rcon.shape_member_players({"players": ["x", None, 1]}).players == []


@pytest.mark.parametrize("fn", ["get_server_status", "get_player_list"])
async def test_member_info_services_never_pass_upstream_through(
    monkeypatch: pytest.MonkeyPatch, fn: str
) -> None:
    monkeypatch.setattr(bot_rcon, "_proxy", AsyncMock(return_value=UPSTREAM))
    monkeypatch.setattr(bot_rcon, "get_active_server", lambda *_a: ROW)
    shaped: Any = await getattr(bot_rcon, fn)(None, 1, 1)
    text = str(shaped)
    for leaked in LEAKS:
        assert leaked not in text


def test_legacy_denylist_is_gone() -> None:
    assert not hasattr(bot_rcon, "_SENSITIVE_KEY_TOKENS")
    assert not hasattr(bot_rcon, "shape_member_view")


def test_server_name_and_game_come_from_db_row_never_upstream() -> None:
    shaped = bot_rcon.shape_member_status(
        {"status": {"server_name": "evil.example.com", "game": "10.0.0.1", "name": "x"}}, **DB
    )
    assert shaped.status.server_name == "Survival"
    assert shaped.status.game == "cs2"


def test_configured_host_embedded_in_map_or_player_name_is_redacted() -> None:
    status = bot_rcon.shape_member_status(
        {"status": {"map": f"dust2 @ {HOST.upper()}", "version": "1.0"}}, **DB
    )
    assert status.status.map is None
    assert status.status.version == "1.0"
    players = bot_rcon.shape_member_players(
        {"players": [{"name": f"join {HOST}"}, {"name": "carol"}]}, host=HOST
    )
    assert [p.name for p in players.players] == [None, "carol"]


def test_hostname_without_dots_matching_configured_host_is_redacted() -> None:
    status = bot_rcon.shape_member_status(
        {"status": {"map": "gameboxA"}}, **{**DB, "host": "gameboxA"}
    )
    assert status.status.map is None
