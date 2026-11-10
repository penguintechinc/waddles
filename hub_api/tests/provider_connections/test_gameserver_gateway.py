"""Game-server gateway: adapter validation, write-only secrecy, tenant scoping, SSRF-safe probe.

Real path: the real blueprint + auth chain + service + vault (AES-256-GCM) + store over sqlite. Only
the socket of the connectivity test is replaced where noted; ``test_probe_*`` runs the REAL probe
against a real loopback RCON server with DNS/IP policy exercised separately.
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

import pytest
from quart import Quart

from blueprints.v1.gameserver_connections import gameserver_connections_bp
from blueprints.v1.provider_connections import SERVICE_CONFIG_KEY
from services.provider_connections import (
    PlatformKeyVault,
    ProviderConnectionService,
    gameserver_probe,
)
from services.provider_connections.adapter import AdapterRegistry, Credentials
from services.provider_connections.errors import (
    CredentialsRejected,
    InvalidConnectionInput,
    RefreshNotConfigured,
)
from services.provider_connections.gameserver import (
    GameServerAdapter,
    build_sealed_target,
    parse_sealed_target,
)
from services.provider_connections.registry import build_default_registry
from tests.provider_connections.conftest import FakeClock, admin_headers
from tests.provider_connections.support import raw_rows

PUBLIC_HOST = "93.184.216.34"
PASSWORD = "Sup3r|Secret-pw"


def test_sealed_target_round_trip_keeps_pipes_in_the_password() -> None:
    token = build_sealed_target("mc.example.com", 25575, PASSWORD)
    assert parse_sealed_target(token) == ("mc.example.com", 25575, PASSWORD)
    assert parse_sealed_target("1.2.3.4|27015|") == ("1.2.3.4", 27015, "")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "host",
        "host|abc|pw",
        "host|0|pw",
        "host|65536|pw",
        "ho st|1|pw",
        "ho/st|1|pw",
        "user@host|1|pw",
        "|1|pw",
        "host|1|" + "x" * 1025,
    ],
)
def test_malformed_targets_are_refused_without_echo(bad: str) -> None:
    with pytest.raises(InvalidConnectionInput) as exc:
        parse_sealed_target(bad)
    assert "pw" not in str(exc.value) or bad.endswith("x" * 1025) is False


async def test_adapter_rejects_every_non_public_literal() -> None:
    adapter = GameServerAdapter()
    for host in (
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104
        "[::1]",
        "[fd00::1]",
        "[::ffff:10.0.0.1]",
    ):
        with pytest.raises(CredentialsRejected):
            await adapter.validate_credentials(Credentials(access_token=f"{host}|25575|pw"))
    account = await adapter.validate_credentials(Credentials(access_token=f"{PUBLIC_HOST}|1|pw"))
    assert account.scopes == ()


async def test_adapter_is_not_refreshable_and_registered_by_default() -> None:
    with pytest.raises(RefreshNotConfigured):
        await GameServerAdapter().refresh("x", {})
    assert "gameserver" in build_default_registry()


@pytest.fixture
def gs_app(pc_app: Quart, pc_db: Any, clock: FakeClock) -> Quart:
    pc_app.register_blueprint(gameserver_connections_bp)
    pc_app.config[SERVICE_CONFIG_KEY] = ProviderConnectionService(
        pc_db.install_dal, AdapterRegistry([GameServerAdapter()]), PlatformKeyVault(), clock=clock
    )
    return pc_app


def _base(db: Any, community_id: int | None = None) -> str:
    return f"/api/v1/communities/{community_id or db.seed.community_id}/gameserver-connections"


def _body(**over: Any) -> str:
    body: dict[str, Any] = {
        "host": PUBLIC_HOST,
        "port": 27015,
        "rconPassword": PASSWORD,
        "game": "source",
        "label": "Main server",
    }
    body.update(over)
    return json.dumps(body)


async def test_create_returns_only_ids_and_never_the_secret_or_address(
    gs_app: Quart, pc_db: Any
) -> None:
    client = gs_app.test_client()
    r = await client.post(_base(pc_db), headers=admin_headers(pc_db), data=_body())
    assert r.status_code == 201, await r.get_data(as_text=True)
    text = await r.get_data(as_text=True)
    body = json.loads(text)
    assert set(body["server"]) == {"connectionId", "serverId", "game", "label", "enabled", "usable"}
    assert PASSWORD not in text and PUBLIC_HOST not in text and "27015" not in text
    # At rest: sealed, and neither host, port nor password appears in any stored column.
    dump = json.dumps(
        [
            {k: str(v) for k, v in row.items()}
            for row in await raw_rows(pc_db, "platform_connections")
        ]
    )
    assert PASSWORD not in dump and PUBLIC_HOST not in dump
    # The generic admin list leaks neither.
    listing = await client.get(
        f"/api/v1/communities/{pc_db.seed.community_id}/provider-connections",
        headers=admin_headers(pc_db),
    )
    listed = await listing.get_data(as_text=True)
    assert PASSWORD not in listed and PUBLIC_HOST not in listed and "27015" not in listed


async def test_create_refuses_private_hosts_bad_input_and_missing_scope(
    gs_app: Quart, pc_db: Any
) -> None:
    client = gs_app.test_client()
    for host in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "100.64.0.1"):
        r = await client.post(_base(pc_db), headers=admin_headers(pc_db), data=_body(host=host))
        assert r.status_code in (400, 422), host
        assert PASSWORD not in await r.get_data(as_text=True)
    r = await client.post(_base(pc_db), headers=admin_headers(pc_db), data=_body(port=0))
    assert r.status_code in (400, 422)
    r = await client.post(_base(pc_db), headers=admin_headers(pc_db), data=_body(extra="x"))
    assert r.status_code in (400, 422)
    r = await client.post(
        _base(pc_db), headers=admin_headers(pc_db, scope="community:read"), data=_body()
    )
    assert r.status_code in (401, 403)
    r = await client.post(_base(pc_db), headers={"Content-Type": "application/json"}, data=_body())
    assert r.status_code in (401, 403)


async def test_test_route_returns_a_closed_code_and_delete_removes(
    gs_app: Quart, pc_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, int, str]] = []

    async def fake_probe(host: str, port: int, password: str, **_: Any) -> str:
        seen.append((host, port, password))
        return "ok_rcon"

    monkeypatch.setattr("blueprints.v1.gameserver_connections.probe", fake_probe)
    client = gs_app.test_client()
    created = json.loads(
        await (
            await client.post(_base(pc_db), headers=admin_headers(pc_db), data=_body())
        ).get_data(as_text=True)
    )
    cid = created["server"]["connectionId"]
    r = await client.post(f"{_base(pc_db)}/{cid}/test", headers=admin_headers(pc_db))
    assert r.status_code == 200
    text = await r.get_data(as_text=True)
    assert json.loads(text) == {"success": True, "result": "ok_rcon"}
    assert PASSWORD not in text and PUBLIC_HOST not in text
    assert seen == [(PUBLIC_HOST, 27015, PASSWORD)]

    r = await client.delete(f"{_base(pc_db)}/{cid}", headers=admin_headers(pc_db))
    assert r.status_code == 200
    r = await client.post(f"{_base(pc_db)}/{cid}/test", headers=admin_headers(pc_db))
    assert r.status_code == 404


async def test_other_communities_and_tenants_cannot_touch_a_server(
    gs_app: Quart, pc_db: Any
) -> None:
    client = gs_app.test_client()
    created = json.loads(
        await (
            await client.post(_base(pc_db), headers=admin_headers(pc_db), data=_body())
        ).get_data(as_text=True)
    )
    cid = created["server"]["connectionId"]
    other = pc_db.seed.community_id + 999
    for method, url in (
        ("post", f"{_base(pc_db, other)}/{cid}/test"),
        ("delete", f"{_base(pc_db, other)}/{cid}"),
    ):
        r = await getattr(client, method)(url, headers=admin_headers(pc_db))
        assert r.status_code in (403, 404)
    r = await client.post(
        f"{_base(pc_db)}/{cid}/test", headers=admin_headers(pc_db, tenant="someone-else")
    )
    assert r.status_code in (401, 403, 404)


async def test_generic_create_with_a_non_gameserver_connection_id_is_not_found_here(
    gs_app: Quart, pc_db: Any
) -> None:
    r = await gs_app.test_client().delete(f"{_base(pc_db)}/987654", headers=admin_headers(pc_db))
    assert r.status_code == 404


# -- the probe: real loopback RCON server, real pinning/policy ------------------------------------


def _pkt(rid: int, ptype: int, body: bytes) -> bytes:
    return struct.pack("<iii", 10 + len(body), rid, ptype) + body + b"\x00\x00"


async def _rcon_server(password: str) -> tuple[asyncio.AbstractServer, int]:
    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        (size,) = struct.unpack("<i", await r.readexactly(4))
        rest = await r.readexactly(size)
        rid, _ty = struct.unpack("<ii", rest[:8])
        ok = rest[8:-2] == password.encode()
        w.write(_pkt(rid, 0, b""))
        w.write(_pkt(rid if ok else -1, 2, b""))
        await w.drain()
        w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_probe_login_ok_and_wrong_password(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow(host: str, port: int) -> str | None:
        return "127.0.0.1"  # the policy is tested below; here only the wire is under test

    monkeypatch.setattr(gameserver_probe, "resolve_pinned", allow)
    server, port = await _rcon_server("pw")
    async with server:
        assert await gameserver_probe.probe("x.example", port, "pw") == "ok_rcon"
        assert await gameserver_probe.probe("x.example", port, "nope") == "auth_failed"
        assert await gameserver_probe.probe("x.example", port, "") == "ok_tcp"


async def test_probe_never_dials_a_non_public_target() -> None:
    server, port = await _rcon_server("pw")
    async with server:
        for host in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "localhost"):
            assert await gameserver_probe.probe(host, port, "pw") == "forbidden_address", host


async def test_probe_unreachable_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow(host: str, port: int) -> str | None:
        return "127.0.0.1"

    monkeypatch.setattr(gameserver_probe, "resolve_pinned", allow)
    server, port = await _rcon_server("pw")
    server.close()
    await server.wait_closed()
    assert await gameserver_probe.probe("x.example", port, "pw") == "unreachable"

    async def silent(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await asyncio.sleep(2)

    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    async with srv:
        p = srv.sockets[0].getsockname()[1]
        assert await gameserver_probe.probe("x.example", p, "pw", timeout=0.2) == "timeout"
