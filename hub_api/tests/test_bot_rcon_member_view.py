"""Member-visible RCON info (`rcon/info/:id/status|players`) is an explicit allowlist DTO.

The server-manager upstream is outside this repo, so its payload is untrusted. The member response
is built only from allowlisted, type-checked fields: odd key spellings, nested objects and
IP / `host:port` / URL values never reach a plain community member.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import bot_rcon

STATUS_FIELDS = {
    "online", "server_name", "players_online", "max_players", "map", "game", "version",
}  # fmt: skip
PLAYER_FIELDS = {"name", "score", "ping", "duration"}
RESPONSE_FIELDS = {"success", "status"}

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
    shaped = bot_rcon.shape_member_status(UPSTREAM)
    assert dataclasses.asdict(shaped) == {
        "success": True,
        "status": {
            "online": True,
            "server_name": None,  # "1.2.3.4:27015" redacted
            "players_online": 3,
            "max_players": 20,
            "map": "de_dust2",
            "game": "cs2",
            "version": "1.0.7",
        },
    }
    text = str(shaped)
    for leaked in LEAKS:
        assert leaked not in text


def test_status_exact_field_set() -> None:
    shaped = bot_rcon.shape_member_status(UPSTREAM)
    assert {f.name for f in dataclasses.fields(shaped)} == RESPONSE_FIELDS
    assert {f.name for f in dataclasses.fields(shaped.status)} == STATUS_FIELDS
    assert set(dataclasses.asdict(shaped)["status"]) == STATUS_FIELDS


def test_players_exact_field_set_and_values() -> None:
    shaped = bot_rcon.shape_member_players(UPSTREAM)
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
        "[2001:db8::1]:27015",
    ],
)
def test_string_values_with_connection_details_are_redacted(
    value: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=bot_rcon.logger.name):
        shaped = bot_rcon.shape_member_status({"success": True, "status": {"map": value}})
    assert shaped.status.map is None
    assert any("redacted" in r.getMessage() for r in caplog.records)
    assert value not in caplog.text  # warning is PII-free: field name only


@pytest.mark.parametrize("value", ["de_dust2", "Survival Server #1", "v1.2.3", "Rust: Hardcore"])
def test_benign_strings_pass_through(value: str) -> None:
    assert bot_rcon.shape_member_status({"status": {"map": value}}).status.map == value


def test_type_validation_and_malformed_payloads() -> None:
    shaped = bot_rcon.shape_member_status(
        {"status": {"online": "yes", "players_online": "3", "max_players": -1, "map": 5}}
    )
    assert shaped.status == bot_rcon.MemberServerStatus()
    assert shaped.success is False
    for junk in (None, [], "x", 3):
        assert bot_rcon.shape_member_status(junk).status == bot_rcon.MemberServerStatus()
        assert bot_rcon.shape_member_players(junk).players == []
    assert bot_rcon.shape_member_players({"players": ["x", None, 1]}).players == []


@pytest.mark.parametrize("fn", ["get_server_status", "get_player_list"])
async def test_member_info_services_never_pass_upstream_through(
    monkeypatch: pytest.MonkeyPatch, fn: str
) -> None:
    monkeypatch.setattr(bot_rcon, "_proxy", AsyncMock(return_value=UPSTREAM))
    shaped: Any = await getattr(bot_rcon, fn)(None, 1, 1)
    text = str(shaped)
    for leaked in LEAKS:
        assert leaked not in text


def test_legacy_denylist_is_gone() -> None:
    assert not hasattr(bot_rcon, "_SENSITIVE_KEY_TOKENS")
    assert not hasattr(bot_rcon, "shape_member_view")
