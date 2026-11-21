"""Server Manager (RCON/Voice) service -- ports `controllers/rconController.js`.

CRUD for `server_status_configs` (with the same SSRF host-block and
AES-256-GCM credential encryption Node applied), plus a thin proxy to
`server-manager-service` for the live-command surface (test/execute/
kick/ban/channels/move/message/policy/enforce/access-log) -- hub-api
never talks RCON/TeamSpeak/Mumble wire protocols itself, matching the
Node controller's own docstring ("proxies commands to Python module").
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from . import bot_crypto
from .url_guard import is_private_host

# Re-exported for backward compatibility -- callers/tests import
# `bot_rcon.is_private_host` (this module's original name, before the
# SSRF security-review fix promoted it to the shared `url_guard.py` so
# `bot_ai_knowledge.py` could reuse the identical check rather than
# duplicating it).
__all__ = ["is_private_host"]

logger = logging.getLogger(__name__)


class RconValidationError(ValueError):
    """A request failed the same checks `rconController.js` enforces (-> 400)."""


class RconNotFoundError(LookupError):
    """The addressed server/row doesn't exist for this community (-> 404)."""


def _server_manager_url() -> str:
    return os.environ.get("SERVER_MANAGER_URL", "http://server-manager-service:8098")


async def _proxy(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    """Ports `proxyToModule` -- fire-and-forget JSON proxy, response passed through verbatim."""
    async with httpx.AsyncClient(base_url=_server_manager_url(), timeout=15.0) as client:
        response = await client.request(method, path, json=body)
    return response.json()


#: A string value is connection-detail-shaped when it holds a URL, a dotted-quad IPv4, an IPv6
#: literal, or a `host:port` pair. Such values are redacted from the member view.
_URL_RE = re.compile(r"[a-z][a-z0-9+.\-]*://|\bwww\.", re.IGNORECASE)
_IPV4_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
_HOST_PORT_RE = re.compile(r"(?<![\w.\-])[\w\-]+(?:\.[\w\-]+)*:\d{2,5}(?!\d)")
_IPV6_RE = re.compile(r"[0-9a-f:.]*:[0-9a-f:.]*:[0-9a-f:.]*", re.IGNORECASE)


def _looks_like_connection_detail(value: str) -> bool:
    """True when `value` embeds a URL, IP address or `host:port` string."""
    if _URL_RE.search(value) or _IPV4_RE.search(value) or _HOST_PORT_RE.search(value):
        return True
    for token in _IPV6_RE.findall(value):
        try:
            ipaddress.IPv6Address(token.strip("[]"))
        except ValueError:
            continue
        return True
    return False


@dataclass(slots=True, frozen=True)
class MemberServerStatus:
    """Allowlist DTO -- the only status fields a plain member may see for a game server."""

    online: bool | None = None
    server_name: str | None = None
    players_online: int | None = None
    max_players: int | None = None
    map: str | None = None
    game: str | None = None
    version: str | None = None


@dataclass(slots=True, frozen=True)
class MemberPlayer:
    """Allowlist DTO -- the only per-player fields a plain member may see."""

    name: str | None = None
    score: float | None = None
    ping: int | None = None
    duration: float | None = None


@dataclass(slots=True, frozen=True)
class MemberStatusResponse:
    """Member response for `rcon/info/:id/status`."""

    success: bool
    status: MemberServerStatus


@dataclass(slots=True, frozen=True)
class MemberPlayersResponse:
    """Member response for `rcon/info/:id/players`."""

    success: bool
    players: list[MemberPlayer]


def _first(src: dict[str, Any], *keys: str) -> Any:
    """First present value among `keys` (upstream spelling aliases), else None."""
    for key in keys:
        if key in src:
            return src[key]
    return None


def _clean_str(field_name: str, value: Any) -> str | None:
    """Return `value` if a plain string free of connection details, else None (PII-free warning)."""
    if not isinstance(value, str):
        return None
    text = value.strip()[:128]
    if _looks_like_connection_detail(text):
        logger.warning("rcon member view: redacted connection-like value in field %s", field_name)
        return None
    return text or None


def _clean_int(value: Any) -> int | None:
    """Return a non-negative int (bools and non-numbers rejected), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if 0 <= value < 2**31 else None


def _clean_number(value: Any) -> float | None:
    """Return a finite non-negative number (bools and non-numbers rejected), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if 0 <= value < 2**31 else None


def shape_member_status(payload: Any) -> MemberStatusResponse:
    """Build the member status response ONLY from allowlisted, type-checked fields.

    The server-manager upstream is outside this repo, so its payload is untrusted. Unknown keys
    (any spelling of host/ip/port/credential, nested objects) are dropped by construction, and
    string values that look like an IP, URL or `host:port` are redacted.
    """
    top = payload if isinstance(payload, dict) else {}
    nested = top.get("status")
    src = nested if isinstance(nested, dict) else top
    online = _first(src, "online")
    return MemberStatusResponse(
        success=top.get("success") is True,
        status=MemberServerStatus(
            online=online if isinstance(online, bool) else None,
            server_name=_clean_str("server_name", _first(src, "server_name", "name")),
            players_online=_clean_int(_first(src, "players_online", "clients_online", "players")),
            max_players=_clean_int(_first(src, "max_players")),
            map=_clean_str("map", _first(src, "map")),
            game=_clean_str("game", _first(src, "game")),
            version=_clean_str("version", _first(src, "version")),
        ),
    )


def shape_member_players(payload: Any) -> MemberPlayersResponse:
    """Build the member player list ONLY from allowlisted, type-checked per-player fields."""
    top = payload if isinstance(payload, dict) else {}
    raw = _first(top, "players", "clients", "users")
    rows = raw if isinstance(raw, list) else []
    players = [
        MemberPlayer(
            name=_clean_str("player.name", _first(row, "name", "nickname")),
            score=_clean_number(_first(row, "score")),
            ping=_clean_int(_first(row, "ping")),
            duration=_clean_number(_first(row, "duration")),
        )
        for row in rows[:512]
        if isinstance(row, dict)
    ]
    return MemberPlayersResponse(success=top.get("success") is True, players=players)


# ── DTOs ──────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class ServerCreate:
    """Request DTO for `POST .../rcon/servers`."""

    display_name: str
    host: str
    game_name: str | None = None
    server_type: str = "rcon"
    game_port: int | None = None
    rcon_port: int | None = None
    password: str | None = None
    game_type: str = "other"
    visibility: str = "admin_only"
    status_api_type: str = "rcon"
    status_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ServerUpdate:
    """Request DTO for `PUT .../rcon/servers/:id` -- `None` means "leave unchanged"."""

    display_name: str | None = None
    game_name: str | None = None
    server_type: str | None = None
    host: str | None = None
    game_port: int | None = None
    rcon_port: int | None = None
    password: str | None = None
    game_type: str | None = None
    visibility: str | None = None
    status_api_type: str | None = None
    status_url: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class Server:
    """Response DTO -- one `server_status_configs` row, admin view (host/port visible)."""

    id: int
    display_name: str | None
    game_name: str
    server_type: str
    host: str | None
    game_port: int | None
    rcon_port: int | None
    game_type: str
    visibility: str
    status_api_type: str
    is_active: bool
    metadata: dict[str, Any]
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class ServerMemberView:
    """Response DTO -- member-visible server listing, no host/port/credentials."""

    id: int
    display_name: str | None
    game_name: str
    server_type: str
    game_port: int | None
    game_type: str
    visibility: str
    is_active: bool
    metadata: dict[str, Any]


@dataclass(slots=True)
class CommandLogEntry:
    """A row from `rcon_command_log`, joined with server/user display names."""

    id: int
    server_config_id: int
    command: str
    response_summary: str | None
    success: bool
    executed_at: str | None
    user_id: int | None
    server_name: str | None
    user_name: str | None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _to_server(row: Any) -> Server:
    return Server(
        id=row.id,
        display_name=row.display_name,
        game_name=row.game_name,
        server_type=row.server_type,
        host=row.host,
        game_port=row.game_port,
        rcon_port=row.rcon_port,
        game_type=row.game_type,
        visibility=row.visibility,
        status_api_type=row.status_api_type,
        is_active=bool(row.is_active),
        metadata=dict(row.metadata or {}),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _to_member_view(row: Any) -> ServerMemberView:
    return ServerMemberView(
        id=row.id,
        display_name=row.display_name,
        game_name=row.game_name,
        server_type=row.server_type,
        game_port=row.game_port,
        game_type=row.game_type,
        visibility=row.visibility,
        is_active=bool(row.is_active),
        metadata=dict(row.metadata or {}),
    )


# ── Admin: server CRUD ───────────────────────────────────────────────────


def list_servers(dal: Any, community_id: int, *, is_admin: bool) -> list[Any]:
    """`GET .../rcon/servers` (admin) / `.../rcon/info` (member, `is_admin=False`)."""
    base = (dal.server_status_configs.community_id == community_id) & (
        dal.server_status_configs.deleted_at == None  # noqa: E711 - pydal IS NULL idiom
    )
    if is_admin:
        rows = dal(base).select(orderby=dal.server_status_configs.display_name)
        return [_to_server(row) for row in rows]

    rows = dal(
        base & dal.server_status_configs.visibility.belongs(["members", "registered"])
    ).select(orderby=dal.server_status_configs.display_name)
    return [_to_member_view(row) for row in rows]


def create_server(dal: Any, community_id: int, payload: ServerCreate, *, added_by: int) -> Server:
    """`POST .../rcon/servers`."""
    if not payload.display_name.strip():
        raise RconValidationError("Display name is required")
    if not payload.host.strip():
        raise RconValidationError("Host is required")
    if is_private_host(payload.host.strip()):
        raise RconValidationError("Private/reserved IP addresses are not allowed")

    credential_enc: bytes | None = None
    credential_iv: bytes | None = None
    if payload.password:
        credential_enc, credential_iv = bot_crypto.encrypt(payload.password)

    game_name = payload.game_name or re.sub(r"\s+", "_", payload.display_name.strip().lower())

    new_id = dal.server_status_configs.insert(
        community_id=community_id,
        display_name=payload.display_name.strip(),
        game_name=game_name,
        server_type=payload.server_type,
        host=payload.host.strip(),
        game_port=payload.game_port,
        rcon_port=payload.rcon_port,
        credential_enc=credential_enc,
        credential_iv=credential_iv,
        game_type=payload.game_type,
        visibility=payload.visibility,
        status_api_type=payload.status_api_type,
        status_url=payload.status_url,
        added_by=added_by,
        metadata=dict(payload.metadata),
        is_active=True,
    )
    dal.commit()
    return _to_server(dal.server_status_configs[new_id])


def update_server(dal: Any, community_id: int, server_id: int, payload: ServerUpdate) -> Server:
    """`PUT .../rcon/servers/:id` -- `None` fields leave the stored value unchanged (COALESCE)."""
    if payload.host and is_private_host(payload.host.strip()):
        raise RconValidationError("Private/reserved IP addresses are not allowed")

    row = (
        dal(
            (dal.server_status_configs.id == server_id)
            & (dal.server_status_configs.community_id == community_id)
            & (dal.server_status_configs.deleted_at == None)  # noqa: E711
        )
        .select()
        .first()
    )
    if row is None:
        raise RconNotFoundError("Server not found")

    fields: dict[str, Any] = {}
    for attr, column in (
        ("display_name", "display_name"),
        ("game_name", "game_name"),
        ("server_type", "server_type"),
        ("host", "host"),
        ("game_port", "game_port"),
        ("rcon_port", "rcon_port"),
        ("game_type", "game_type"),
        ("visibility", "visibility"),
        ("status_api_type", "status_api_type"),
    ):
        value = getattr(payload, attr)
        if value is not None:
            fields[column] = value.strip() if isinstance(value, str) else value
    # status_url/metadata: Node always sets these (even to NULL), no COALESCE.
    fields["status_url"] = payload.status_url
    if payload.metadata is not None:
        fields["metadata"] = dict(payload.metadata)
    if payload.password:
        fields["credential_enc"], fields["credential_iv"] = bot_crypto.encrypt(payload.password)
    fields["updated_at"] = datetime.utcnow()

    dal(dal.server_status_configs.id == server_id).update(**fields)
    dal.commit()
    return _to_server(dal.server_status_configs[server_id])


def delete_server(dal: Any, community_id: int, server_id: int) -> None:
    """`DELETE .../rcon/servers/:id` -- soft delete (`deleted_at`)."""
    row = (
        dal(
            (dal.server_status_configs.id == server_id)
            & (dal.server_status_configs.community_id == community_id)
            & (dal.server_status_configs.deleted_at == None)  # noqa: E711
        )
        .select()
        .first()
    )
    if row is None:
        raise RconNotFoundError("Server not found")
    dal(dal.server_status_configs.id == server_id).update(deleted_at=datetime.utcnow())
    dal.commit()


def get_active_server(dal: Any, community_id: int, server_id: int) -> Any:
    """Active (not soft-deleted) server of this community, else `RconNotFoundError`."""
    row = (
        dal(
            (dal.server_status_configs.id == server_id)
            & (dal.server_status_configs.community_id == community_id)
            & (dal.server_status_configs.deleted_at == None)  # noqa: E711
        )
        .select()
        .first()
    )
    if row is None:
        raise RconNotFoundError("Server not found")
    return row


# ── Admin: live-command proxy ────────────────────────────────────────────


async def test_connection(
    dal: Any, community_id: int, server_id: int, *, password: str | None = None
) -> Any:
    """`POST .../rcon/servers/:id/test`."""
    server = get_active_server(dal, community_id, server_id)
    body: dict[str, Any] = {
        "server_type": server.server_type,
        "host": server.host,
        "port": server.rcon_port,
    }
    if password:
        body["password"] = password
    if server.server_type == "teamspeak":
        body["username"] = (server.metadata or {}).get("ts_username", "serveradmin")
    return await _proxy(f"/api/v1/server-manager/{community_id}/connect-test", "POST", body)


async def execute_command(
    dal: Any, community_id: int, server_id: int, *, command: str, user_id: int
) -> Any:
    """`POST .../rcon/servers/:id/command`."""
    if not command.strip():
        raise RconValidationError("Command is required")
    return await _proxy(
        f"/api/v1/server-manager/{community_id}/command",
        "POST",
        {"server_id": server_id, "command": command.strip(), "user_id": user_id},
    )


async def get_server_status(dal: Any, community_id: int, server_id: int) -> MemberStatusResponse:
    """`GET .../rcon/info/:id/status` (member-visible: allowlist DTO only)."""
    return shape_member_status(
        await _proxy(f"/api/v1/server-manager/{community_id}/servers/{server_id}/status")
    )


async def get_player_list(dal: Any, community_id: int, server_id: int) -> MemberPlayersResponse:
    """`GET .../rcon/info/:id/players` (member-visible: allowlist DTO only)."""
    return shape_member_players(
        await _proxy(f"/api/v1/server-manager/{community_id}/servers/{server_id}/players")
    )


async def kick_player(
    dal: Any, community_id: int, server_id: int, *, player: str, reason: str, user_id: int
) -> Any:
    """`POST .../rcon/servers/:id/kick`."""
    if not player:
        raise RconValidationError("Player identifier is required")
    return await _proxy(
        f"/api/v1/server-manager/{community_id}/servers/{server_id}/kick",
        "POST",
        {"player": player, "reason": reason or "", "user_id": user_id},
    )


async def ban_player(
    dal: Any,
    community_id: int,
    server_id: int,
    *,
    player: str,
    reason: str,
    duration: int,
    user_id: int,
) -> Any:
    """`POST .../rcon/servers/:id/ban`."""
    if not player:
        raise RconValidationError("Player identifier is required")
    return await _proxy(
        f"/api/v1/server-manager/{community_id}/servers/{server_id}/ban",
        "POST",
        {"player": player, "reason": reason or "", "duration": duration or 0, "user_id": user_id},
    )


async def get_channels(dal: Any, community_id: int, server_id: int) -> Any:
    """`GET .../rcon/servers/:id/channels`."""
    return await _proxy(f"/api/v1/server-manager/{community_id}/servers/{server_id}/channels")


async def move_user(
    dal: Any, community_id: int, server_id: int, *, target_user_id: str, channel_id: int
) -> Any:
    """`POST .../rcon/servers/:id/move`."""
    if not target_user_id or channel_id is None:
        raise RconValidationError("user_id and channel_id are required")
    return await _proxy(
        f"/api/v1/server-manager/{community_id}/servers/{server_id}/move",
        "POST",
        {"user_id": target_user_id, "channel_id": channel_id},
    )


async def send_message(
    dal: Any,
    community_id: int,
    server_id: int,
    *,
    text: str,
    channel_id: int = 0,
    target_mode: int = 2,
) -> Any:
    """`POST .../rcon/servers/:id/message`."""
    if not text.strip():
        raise RconValidationError("Message text is required")
    return await _proxy(
        f"/api/v1/server-manager/{community_id}/servers/{server_id}/message",
        "POST",
        {"text": text.strip(), "channel_id": channel_id or 0, "target_mode": target_mode or 2},
    )


async def get_access_policy(dal: Any, community_id: int, server_id: int) -> Any:
    """`GET .../rcon/servers/:id/policy`."""
    return await _proxy(f"/api/v1/server-manager/{community_id}/servers/{server_id}/policy")


async def update_access_policy(
    dal: Any, community_id: int, server_id: int, policy: dict[str, Any]
) -> Any:
    """`PUT .../rcon/servers/:id/policy`."""
    return await _proxy(
        f"/api/v1/server-manager/{community_id}/servers/{server_id}/policy", "PUT", policy
    )


async def trigger_enforcement(dal: Any, community_id: int, server_id: int) -> Any:
    """`POST .../rcon/servers/:id/enforce`."""
    return await _proxy(
        f"/api/v1/server-manager/{community_id}/servers/{server_id}/enforce", "POST"
    )


async def get_access_log(
    dal: Any, community_id: int, server_id: int, *, limit: int = 50, offset: int = 0
) -> Any:
    """`GET .../rcon/servers/:id/access-log`."""
    return await _proxy(
        f"/api/v1/server-manager/{community_id}/servers/{server_id}/access-log"
        f"?limit={limit}&offset={offset}"
    )


# ── Command log (local table, not proxied) ───────────────────────────────


def get_command_log(
    dal: Any, community_id: int, *, limit: int = 50, offset: int = 0, server_id: int | None = None
) -> list[CommandLogEntry]:
    """`GET .../rcon/log` -- joined with server display name + user display name."""
    ssc = dal.server_status_configs
    rcl = dal.rcon_command_log
    hu = dal.hub_users

    query = (
        (rcl.server_config_id == ssc.id)
        & (ssc.community_id == community_id)
        & (
            ssc.deleted_at == None  # noqa: E711
        )
    )
    if server_id is not None:
        query &= rcl.server_config_id == server_id

    rows = dal(query).select(
        rcl.id,
        rcl.server_config_id,
        rcl.command,
        rcl.response_summary,
        rcl.success,
        rcl.executed_at,
        rcl.user_id,
        ssc.display_name,
        hu.display_name,
        left=hu.on(rcl.user_id == hu.id),
        orderby=~rcl.executed_at,
        limitby=(offset, offset + limit),
    )
    return [
        CommandLogEntry(
            id=row.rcon_command_log.id,
            server_config_id=row.rcon_command_log.server_config_id,
            command=row.rcon_command_log.command,
            response_summary=row.rcon_command_log.response_summary,
            success=bool(row.rcon_command_log.success),
            executed_at=_iso(row.rcon_command_log.executed_at),
            user_id=row.rcon_command_log.user_id,
            server_name=row.server_status_configs.display_name,
            user_name=row.hub_users.display_name if row.hub_users else None,
        )
        for row in rows
    ]
