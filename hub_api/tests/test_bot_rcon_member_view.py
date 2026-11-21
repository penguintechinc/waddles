"""Member-visible RCON info (`rcon/info/:id/status|players`) never leaks connection details.

The server-manager upstream is outside this repo, so its payload is untrusted: host, ports,
addresses, URLs and credentials are stripped before a plain community member can see them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import bot_rcon

UPSTREAM = {
    "success": True,
    "status": {
        "online": True,
        "server_name": "Survival",
        "players": 3,
        "max_players": 20,
        "host": "10.1.2.3",
        "game_port": 28015,
        "rconPort": 28016,
        "password": "hunter2",
        "api_key": "k",
        "status_url": "http://internal/status",
    },
    "players": [{"name": "alice", "ip": "9.9.9.9", "ping": 30, "steam_id_token": "t"}],
}


def test_shape_member_view_strips_connection_details_and_keeps_game_data() -> None:
    shaped = bot_rcon.shape_member_view(UPSTREAM)
    assert shaped["status"] == {
        "online": True,
        "server_name": "Survival",
        "players": 3,
        "max_players": 20,
    }
    assert shaped["players"] == [{"name": "alice", "ping": 30}]
    assert shaped["success"] is True
    text = str(shaped)
    for leaked in ("10.1.2.3", "28015", "28016", "hunter2", "internal", "9.9.9.9"):
        assert leaked not in text


@pytest.mark.parametrize("fn", ["get_server_status", "get_player_list"])
async def test_member_info_services_shape_the_upstream_response(
    monkeypatch: pytest.MonkeyPatch, fn: str
) -> None:
    monkeypatch.setattr(bot_rcon, "_proxy", AsyncMock(return_value=UPSTREAM))
    shaped: Any = await getattr(bot_rcon, fn)(None, 1, 1)
    assert "host" not in shaped["status"] and "password" not in shaped["status"]
    assert shaped["players"] == [{"name": "alice", "ping": 30}]
