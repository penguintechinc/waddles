"""Tests for feature #684 one-time secrets: create -> pull -> gone, expiry, authz, encryption."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema
from sqlalchemy import text, update

from blueprints.v1 import one_time_secrets as bp_module
from services import one_time_secret_service as svc
from tests.conftest import TENANT_SLUG, make_user_token

_KEY = "ab" * 32
_MESSAGE = "the launch code is hunter2"
CREATE = "/api/v1/one-time-secrets"
PULL = "/api/v1/one-time-secrets/pull"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONE_TIME_SECRET_ENCRYPTION_KEY", _KEY)
    monkeypatch.setattr(bp_module, "feature_enabled", AsyncMock(return_value=True))


@pytest.fixture
async def world(bundle_install_db: Any, install_dal: Any) -> dict[str, Any]:
    """Quart app + a community, a target user (id 1) and an other user (id 2), uuids set."""
    app = Quart(__name__)
    QuartSchema(app)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    app.register_blueprint(bp_module.one_time_secrets_bp)

    target_uuid, other_uuid = uuid.uuid4(), uuid.uuid4()
    community_id = int(await install_dal.communities.async_insert(tenant_id=1, name="c1"))
    async with install_dal.engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE hub_users (id INTEGER PRIMARY KEY, username TEXT, uuid CHAR(32))")
        )
        await conn.execute(
            text("INSERT INTO hub_users (id, username, uuid) VALUES (:i, :n, :u)"),
            [
                {"i": 1, "n": "target", "u": target_uuid.hex},
                {"i": 2, "n": "other", "u": other_uuid.hex},
            ],
        )
        await conn.run_sync(svc.METADATA.create_all)
    return {
        "app": app,
        "dal": install_dal,
        "community_id": community_id,
        "target_uuid": target_uuid,
        "other_uuid": other_uuid,
    }


def _creator() -> dict[str, str]:
    token = make_user_token(user_id=99, scope="secret_messaging:create", tenant=TENANT_SLUG)
    return {"Authorization": f"Bearer {token}"}


def _puller(user_id: int) -> dict[str, str]:
    token = make_user_token(user_id=user_id, scope="secret_messaging:pull", tenant=TENANT_SLUG)
    return {"Authorization": f"Bearer {token}"}


async def _create(w: dict[str, Any], **overrides: Any) -> Any:
    body = {
        "communityId": w["community_id"],
        "targetUserUuid": str(w["target_uuid"]),
        "message": _MESSAGE,
    } | overrides
    return await w["app"].test_client().post(CREATE, headers=_creator(), json=body)


async def test_create_pull_then_second_pull_is_gone(world: dict[str, Any]) -> None:
    created = await _create(world)
    assert created.status_code == 201
    token = (await created.get_json())["token"]
    client = world["app"].test_client()

    first = await client.post(PULL, headers=_puller(1), json={"token": token})
    assert first.status_code == 200
    assert (await first.get_json())["message"] == _MESSAGE
    assert first.headers["Cache-Control"] == "no-store"

    second = await client.post(PULL, headers=_puller(1), json={"token": token})
    assert second.status_code == 410
    async with world["dal"].engine.connect() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM one_time_secrets"))).scalar()
    assert count == 0


async def test_concurrent_pulls_only_one_wins(world: dict[str, Any]) -> None:
    token = (await (await _create(world)).get_json())["token"]
    client = world["app"].test_client()
    results = await asyncio.gather(
        *[client.post(PULL, headers=_puller(1), json={"token": token}) for _ in range(4)]
    )
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1
    assert all(c == 410 for c in codes if c != 200)


async def test_wrong_user_denied_and_secret_not_consumed(world: dict[str, Any]) -> None:
    token = (await (await _create(world)).get_json())["token"]
    client = world["app"].test_client()

    denied = await client.post(PULL, headers=_puller(2), json={"token": token})
    assert denied.status_code == 403
    assert _MESSAGE not in await denied.get_data(as_text=True)

    ok = await client.post(PULL, headers=_puller(1), json={"token": token})
    assert ok.status_code == 200


async def test_expired_secret_is_gone(world: dict[str, Any]) -> None:
    token = (await (await _create(world)).get_json())["token"]
    past = datetime.now(UTC) - timedelta(seconds=5)
    async with world["dal"].engine.begin() as conn:
        await conn.execute(update(svc.one_time_secrets).values(expires_at=past))
    resp = await world["app"].test_client().post(PULL, headers=_puller(1), json={"token": token})
    assert resp.status_code == 410
    async with world["dal"].engine.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM one_time_secrets"))).scalar() == 0


async def test_unknown_token_gone(world: dict[str, Any]) -> None:
    resp = await world["app"].test_client().post(PULL, headers=_puller(1), json={"token": "nope"})
    assert resp.status_code == 410


async def test_ciphertext_at_rest_and_token_hashed(world: dict[str, Any]) -> None:
    token = (await (await _create(world)).get_json())["token"]
    async with world["dal"].engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT ciphertext, iv, token_hash FROM one_time_secrets"))
        ).one()
    assert _MESSAGE.encode() not in bytes(row.ciphertext)
    assert b"hunter2" not in bytes(row.ciphertext)
    assert len(bytes(row.iv)) == 12
    assert row.token_hash == svc.hash_token(token)
    assert row.token_hash != token


async def test_scope_required(world: dict[str, Any]) -> None:
    no_scope = {"Authorization": f"Bearer {make_user_token(user_id=1, tenant=TENANT_SLUG)}"}
    client = world["app"].test_client()
    body = {"communityId": 1, "targetUserUuid": str(world["target_uuid"]), "message": "x"}
    assert (await client.post(CREATE, headers=no_scope, json=body)).status_code == 403
    assert (await client.post(PULL, headers=no_scope, json={"token": "t"})).status_code == 403


async def test_flag_off_returns_404(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bp_module, "feature_enabled", AsyncMock(return_value=False))
    assert (await _create(world)).status_code == 404
    resp = await world["app"].test_client().post(PULL, headers=_puller(1), json={"token": "t"})
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [{"message": ""}, {"message": "x" * 4001}, {"ttlSeconds": 1}, {"targetUserUuid": "not-a-uuid"}],
)
async def test_create_validation(world: dict[str, Any], overrides: dict[str, Any]) -> None:
    assert (await _create(world, **overrides)).status_code in (400, 422)


async def test_create_unknown_target_and_foreign_community(world: dict[str, Any]) -> None:
    assert (await _create(world, targetUserUuid=str(uuid.uuid4()))).status_code == 404
    assert (await _create(world, communityId=9999)).status_code == 404


async def test_missing_key_fails_loud(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONE_TIME_SECRET_ENCRYPTION_KEY")
    assert (await _create(world)).status_code == 500
    async with world["dal"].engine.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM one_time_secrets"))).scalar() == 0


async def test_logs_are_pii_free(world: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    token = (await (await _create(world)).get_json())["token"]
    await world["app"].test_client().post(PULL, headers=_puller(2), json={"token": token})
    await world["app"].test_client().post(PULL, headers=_puller(1), json={"token": token})
    blob = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert _MESSAGE not in blob
    assert token not in blob
    assert "target" not in blob.replace("target_user", "")
