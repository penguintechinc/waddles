"""`GET/PUT /communities/<id>/calendar/event-sync` (#876) on REAL Postgres.

The sqlite suite proves routing and gating. Here the toggle is persisted to / read back from a
migrated Postgres (`guild_tenant_pairings.event_sync_enabled`, migration 0039, NOT NULL) and the
`ApiError` handler (the #876 fix: 4xx -> WARNING, 5xx -> ERROR + traceback, PII-free) is asserted
on its actual log records instead of a returned default. Demo-critical behaviours locked down:

- admin scope is required for the write (read scope cannot flip the toggle), no token is refused,
- another tenant can neither read nor flip a community's toggle,
- a toggle persists across a fresh connection and fans out to EVERY paired guild,
- a mid-loop failure is LOGGED with `community_id/op/status/code/exc_type`, returned as the real
  status (never a silent 200/default), and never leaks the guild id or any user token.

Skipped (never failed) where the `docker` CLI is unavailable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from flask_core.database import AsyncDAL
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.event_discord_sync as module
from blueprints.v1.event_discord_sync import (
    event_discord_sync_admin_bp,
    event_discord_sync_internal_bp,
)
from services.errors import ApiError
from services.guild_pairing import create_pairing
from tests import demo_pg_support as pg

pytestmark = pytest.mark.skipif(
    not pg.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

WRITE = "community.guild_pairing:write"
READ = "community.guild_pairing:read"
GUILDS = ("123456789012345678", "223456789012345678")  # gitleaks:allow - fake snowflakes
LOGGER = "blueprints.v1.event_discord_sync"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[pg.PgTestDatabase]:
    """One migrated Postgres 17 container for the module."""
    if not pg.DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with pg.migrated_postgres("hub-api-event-sync-pg") as db:
        pg.prepare_database(db)
        yield db


@pytest.fixture
def adal(pg_db: pg.PgTestDatabase) -> Iterator[AsyncDAL]:
    """Per-test truncated, bound pydal `AsyncDAL` on the real container."""
    yield from pg.fresh_dal(pg_db)


@pytest.fixture
def community_id(adal: AsyncDAL) -> int:
    """`acme-corp` (the `auth_headers` default tenant) + one community under it."""
    dal = adal.dal
    tid = pg.scalar(
        dal,
        "INSERT INTO tenants (slug, display_name, is_global, is_active) "
        "VALUES ('acme-corp', 'Acme', false, true) RETURNING id",
    )
    pg.q(
        dal,
        "INSERT INTO tenants (slug, display_name, is_global, is_active) "
        "VALUES ('someone-else', 'Other', false, true)",
    )
    return int(
        pg.scalar(
            dal,
            "INSERT INTO communities (name, tenant_id) VALUES ('c1', %s) RETURNING id",
            [tid],
        )
    )


@pytest.fixture
def client(adal: AsyncDAL, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(module, "feature_enabled", AsyncMock(return_value=True))
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(event_discord_sync_admin_bp)
    app.register_blueprint(event_discord_sync_internal_bp)
    app.config["dal"] = adal.dal
    return app.test_client()


def _url(community_id: int) -> str:
    return f"/api/v1/communities/{community_id}/calendar/event-sync"


def _pair(dal: Any, community_id: int, n: int = 1) -> list[int]:
    return [
        create_pairing(
            dal,
            community_id,
            discord_guild_id=GUILDS[i],
            direction="bidirectional",
            role_name_prefix="[BC]",
            actor_user_id=None,
        ).id
        for i in range(n)
    ]


def _db_flags(dal: Any, community_id: int) -> list[bool]:
    rows = pg.q(
        dal,
        "SELECT event_sync_enabled FROM guild_tenant_pairings WHERE community_id=%s ORDER BY id",
        [community_id],
    )
    return [bool(r[0]) for r in rows]


async def _put(client: Any, headers: dict[str, str], cid: int, body: Any) -> Any:
    return await client.put(
        _url(cid),
        headers={**headers, "Content-Type": "application/json"},
        data=body if isinstance(body, str) else json.dumps(body),
    )


# ------------------------------------------------------------------ authz


async def test_put_requires_write_scope_and_never_mutates(
    client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
) -> None:
    _pair(adal.dal, community_id)
    for headers in (auth_headers(scope=READ), auth_headers(scope=""), {}):
        resp = await _put(client, headers, community_id, {"enabled": True})
        assert resp.status_code in (401, 403)
    assert _db_flags(adal.dal, community_id) == [False]


async def test_get_requires_read_scope(
    client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
) -> None:
    _pair(adal.dal, community_id)
    assert (await client.get(_url(community_id))).status_code in (401, 403)
    resp = await client.get(_url(community_id), headers=auth_headers(scope="other:read"))
    assert resp.status_code == 403
    resp = await client.get(_url(community_id), headers=auth_headers(scope=READ))
    assert resp.status_code == 200


async def test_other_tenant_cannot_read_or_flip(
    client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
) -> None:
    _pair(adal.dal, community_id)
    stranger = auth_headers(scope=f"{READ} {WRITE}", tenant="someone-else")
    got = await client.get(_url(community_id), headers=stranger)
    put = await _put(client, stranger, community_id, {"enabled": True})
    assert got.status_code in (403, 404) and put.status_code in (403, 404)
    assert _db_flags(adal.dal, community_id) == [False]


async def test_flag_off_is_402_and_persists_nothing(
    client: Any,
    auth_headers: Any,
    adal: AsyncDAL,
    community_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "feature_enabled", AsyncMock(return_value=False))
    _pair(adal.dal, community_id)
    resp = await _put(client, auth_headers(scope=WRITE), community_id, {"enabled": True})
    assert resp.status_code == 402
    assert _db_flags(adal.dal, community_id) == [False]


# ------------------------------------------------------------------ persistence


async def test_toggle_persists_to_every_pairing_and_state_aggregates(
    client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
) -> None:
    _pair(adal.dal, community_id, n=2)
    resp = await _put(client, auth_headers(scope=WRITE), community_id, {"enabled": True})
    body = await resp.get_json()
    assert resp.status_code == 200
    assert (body["event_sync_enabled"], body["pairings_total"], body["pairings_enabled"]) == (
        True,
        2,
        2,
    )
    assert _db_flags(adal.dal, community_id) == [True, True]

    got = await client.get(_url(community_id), headers=auth_headers(scope=READ))
    assert (await got.get_json())["event_sync_enabled"] is True

    # one guild turned off out-of-band => aggregate is NOT "enabled" (all-or-nothing semantics)
    pg.q(
        adal.dal,
        "UPDATE guild_tenant_pairings SET event_sync_enabled=false WHERE discord_guild_id=%s",
        [GUILDS[1]],
    )
    got = await client.get(_url(community_id), headers=auth_headers(scope=READ))
    mixed = await got.get_json()
    assert (mixed["event_sync_enabled"], mixed["pairings_enabled"]) == (False, 1)

    resp = await _put(client, auth_headers(scope=WRITE), community_id, {"enabled": False})
    assert (await resp.get_json())["pairings_enabled"] == 0
    assert _db_flags(adal.dal, community_id) == [False, False]


@pytest.mark.parametrize("body", ['{"enabled": "true"}', '{"enabled": 1}', "{}", "[]", "not json"])
async def test_non_bool_body_is_400_and_persists_nothing(
    client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int, body: str
) -> None:
    _pair(adal.dal, community_id)
    resp = await _put(client, auth_headers(scope=WRITE), community_id, body)
    assert resp.status_code == 400
    assert _db_flags(adal.dal, community_id) == [False]


async def test_no_pairings_is_404(client: Any, auth_headers: Any, community_id: int) -> None:
    resp = await _put(client, auth_headers(scope=WRITE), community_id, {"enabled": True})
    assert resp.status_code == 404
    got = await client.get(_url(community_id), headers=auth_headers(scope=READ))
    body = await got.get_json()
    assert (body["event_sync_enabled"], body["pairings_total"]) == (False, 0)


# ------------------------------------------------------------------ ApiError logging path


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "event_sync_toggle_failed" in r.getMessage()]


async def test_4xx_api_error_is_logged_warning_without_traceback_and_returned(
    client: Any,
    auth_headers: Any,
    adal: AsyncDAL,
    community_id: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pairing deleted between list and update -> the REAL service raises 404 `not_found`."""
    _first, second = _pair(adal.dal, community_id, n=2)
    real_update = module.pairing_svc.update_pairing

    def racing_update(dal: Any, cid: int, pairing_id: int, **kw: Any) -> Any:
        if pairing_id == second:
            pg.q(dal, "DELETE FROM guild_tenant_pairings WHERE id=%s", [second])
        return real_update(dal, cid, pairing_id, **kw)

    monkeypatch.setattr(module.pairing_svc, "update_pairing", racing_update)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        resp = await _put(client, auth_headers(scope=WRITE), community_id, {"enabled": True})
    assert resp.status_code == 404
    assert (await resp.get_json())["error"]["code"] == "NOT_FOUND"

    (rec,) = _records(caplog)
    msg = rec.getMessage()
    assert rec.levelno == logging.WARNING and not rec.exc_info
    for field in (
        f"community_id={community_id}",
        "op=put",
        "status=404",
        "code=NOT_FOUND",
        "exc_type=ApiError",
    ):
        assert field in msg
    assert GUILDS[0] not in msg and GUILDS[1] not in msg and "Bearer" not in msg
    # state is honest about the partial write: first pairing flipped, second gone
    assert _db_flags(adal.dal, community_id) == [True]


async def test_5xx_api_error_is_logged_error_with_traceback_and_not_swallowed(
    client: Any,
    auth_headers: Any,
    adal: AsyncDAL,
    community_id: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _pair(adal.dal, community_id, n=2)
    calls: list[int] = []
    real_update = module.pairing_svc.update_pairing

    def failing_update(dal: Any, cid: int, pairing_id: int, **kw: Any) -> Any:
        calls.append(pairing_id)
        if len(calls) == 2:
            raise ApiError("database unavailable", 503, "SERVICE_UNAVAILABLE")
        return real_update(dal, cid, pairing_id, **kw)

    monkeypatch.setattr(module.pairing_svc, "update_pairing", failing_update)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        resp = await _put(client, auth_headers(scope=WRITE), community_id, {"enabled": True})

    assert resp.status_code == 503
    body = await resp.get_json()
    assert body == {
        "success": False,
        "error": {"code": "SERVICE_UNAVAILABLE", "message": "database unavailable"},
    }
    (rec,) = _records(caplog)
    msg = rec.getMessage()
    assert rec.levelno == logging.ERROR
    assert rec.exc_info is not None and rec.exc_info[0] is ApiError
    for field in (
        f"community_id={community_id}",
        "op=put",
        "status=503",
        "code=SERVICE_UNAVAILABLE",
        "exc_type=ApiError",
    ):
        assert field in msg
    assert GUILDS[0] not in msg and GUILDS[1] not in msg and "acme-corp" not in msg
    # GET reports the honest partial state, never a rosy default
    got = await client.get(_url(community_id), headers=auth_headers(scope=READ))
    state = await got.get_json()
    assert (state["event_sync_enabled"], state["pairings_enabled"], state["pairings_total"]) == (
        False,
        1,
        2,
    )


async def test_success_path_logs_no_failure_record(
    client: Any,
    auth_headers: Any,
    adal: AsyncDAL,
    community_id: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _pair(adal.dal, community_id)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        resp = await _put(client, auth_headers(scope=WRITE), community_id, {"enabled": True})
    assert resp.status_code == 200 and not _records(caplog)


# ------------------------------------------------------------------ internal (service-key) toggle


async def test_internal_toggle_requires_service_key_and_persists(
    client: Any, adal: AsyncDAL, community_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key")
    _pair(adal.dal, community_id, n=2)
    url = "/api/v1/internal/calendar/guild-pairings/event-sync"
    payload = json.dumps({"community_id": community_id, "enabled": True})
    denied = await client.post(url, data=payload, headers={"X-Service-Key": "wrong"})
    assert denied.status_code == 401 and _db_flags(adal.dal, community_id) == [False, False]
    ok = await client.post(url, data=payload, headers={"X-Service-Key": "test-service-key"})
    assert ok.status_code == 200
    assert (await ok.get_json())["data"]["pairings_updated"] == 2
    assert _db_flags(adal.dal, community_id) == [True, True]
    missing = await client.post(
        url,
        data=json.dumps({"community_id": community_id + 99, "enabled": True}),
        headers={"X-Service-Key": "test-service-key"},
    )
    assert missing.status_code == 404
