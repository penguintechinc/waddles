"""Boundary tests for `services.overlay_push` (svc-action -> hub-api gRPC mint -> svc-presentation).

The mint is a gRPC call (`waddle_transports.service_jwt_client`, whose real client<->server path is
covered against the real hub-api servicer in `hub_api/tests/test_service_auth_grpc.py`); here it is
replaced by `_FakeMint` so the push/caching/error logic is tested in isolation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from flask_core import AsyncDAL
from waddle_transports.service_jwt_client import MintedToken, ServiceTokenError

from services import overlay_push
from services.overlay_push import (
    OverlayPushError,
    mint_push_token,
    push_overlay,
    resolve_overlay_code,
)

_CODE = "0123456789abcdef"


@pytest.fixture
async def dal(tmp_path: Path) -> AsyncIterator[AsyncDAL]:
    """Real sqlite DAL with two communities, each with its own overlay code."""
    async_dal = AsyncDAL(f"sqlite://{tmp_path}/op.db", pool_size=1, migrate=True)
    d = async_dal.dal
    d.define_table("communities", d.Field("overlay_code", "string"), migrate=True)
    d.communities.insert(overlay_code=_CODE)
    d.communities.insert(overlay_code="fedcba9876543210")
    d.commit()
    yield async_dal
    try:
        await async_dal.close_async()
    except Exception:  # noqa: BLE001, S110 -- pydal cross-thread close gotcha
        pass  # nosec B110


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sa = tmp_path / "sa"
    sa.write_text("sa-token")
    monkeypatch.setenv("PRESENTATION_URL", "http://presentation.test:8207/")
    monkeypatch.setenv("SERVICE_JWT_TOKEN_ENDPOINT", "https://hub-grpc.test:50204")
    monkeypatch.setenv("SERVICE_JWT_SA_TOKEN_PATH", str(sa))
    overlay_push.reset_cache_for_tests()


class _FakeMint:
    """Records `mint_service_token` calls; optionally raises like a failed bootstrap."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: ServiceTokenError | None = None

    async def __call__(self, **kwargs: Any) -> MintedToken:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return MintedToken(token="jwt", expires_in=300.0)  # noqa: S106


@pytest.fixture(autouse=True)
def mint(monkeypatch: pytest.MonkeyPatch) -> _FakeMint:
    fake = _FakeMint()
    monkeypatch.setattr(overlay_push, "mint_service_token", fake)
    return fake


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok_router(calls: list[httpx.Request], *, push_status: int = 202):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(push_status, json={})

    return handler


async def test_push_posts_to_code_route_with_community_bound_jwt(
    dal: AsyncDAL, mint: _FakeMint
) -> None:
    calls: list[httpx.Request] = []
    async with _client(_ok_router(calls)) as client:
        status = await push_overlay(
            client,
            dal,
            community_id=1,
            surface="alert_box",
            body={"alert": {"alert_type": "follow"}},
        )
    assert status == 202
    (push,) = calls
    assert [(c["scope"], c["community_id"], c["endpoint"]) for c in mint.calls] == [
        ("presentation.overlay:push", 1, "https://hub-grpc.test:50204")
    ]
    assert str(push.url) == f"http://presentation.test:8207/{_CODE}/alert_box/push"
    assert push.headers["Authorization"] == "Bearer jwt"
    assert json.loads(push.content) == {"alert": {"alert_type": "follow"}}


async def test_second_community_resolves_its_own_code_and_token(
    dal: AsyncDAL, mint: _FakeMint
) -> None:
    calls: list[httpx.Request] = []
    async with _client(_ok_router(calls)) as client:
        await push_overlay(client, dal, community_id=2, surface="media", body={})
    (push,) = calls
    assert mint.calls[0]["community_id"] == 2
    assert push.url.path == "/fedcba9876543210/media/push"


async def test_jwt_is_cached_per_community(dal: AsyncDAL, mint: _FakeMint) -> None:
    calls: list[httpx.Request] = []
    async with _client(_ok_router(calls)) as client:
        await push_overlay(client, dal, community_id=1, surface="media", body={})
        await push_overlay(client, dal, community_id=1, surface="media", body={})
    assert len(mint.calls) == 1


async def test_auth_rejection_evicts_cached_jwt(dal: AsyncDAL, mint: _FakeMint) -> None:
    calls: list[httpx.Request] = []
    async with _client(_ok_router(calls, push_status=403)) as client:
        with pytest.raises(OverlayPushError, match="HTTP 403"):
            await push_overlay(client, dal, community_id=1, surface="media", body={})
        with pytest.raises(OverlayPushError):
            await push_overlay(client, dal, community_id=1, surface="media", body={})
    assert len(mint.calls) == 2


@pytest.mark.parametrize("missing", ["PRESENTATION_URL", "SERVICE_JWT_TOKEN_ENDPOINT"])
async def test_unset_deployment_env_fails_loud(
    dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)
    calls: list[httpx.Request] = []
    async with _client(_ok_router(calls)) as client:
        with pytest.raises(OverlayPushError, match=missing):
            await push_overlay(client, dal, community_id=1, surface="media", body={})
    assert not any(c.url.path.endswith("/push") for c in calls)


async def test_unknown_community_fails_loud(dal: AsyncDAL) -> None:
    with pytest.raises(OverlayPushError, match="not found"):
        await resolve_overlay_code(dal, 99)


@pytest.mark.parametrize("bad", [None, "", "XYZ", "0123456789ABCDEF", "0123456789abcdef0"])
async def test_malformed_overlay_code_fails_loud(dal: AsyncDAL, bad: str | None) -> None:
    dal.dal(dal.dal.communities.id == 1).update(overlay_code=bad)
    dal.dal.commit()
    with pytest.raises(OverlayPushError, match="overlay_code"):
        await resolve_overlay_code(dal, 1)


@pytest.mark.parametrize(
    ("reason", "match"),
    [
        ("projected ServiceAccount token unreadable: FileNotFoundError", "unreadable"),
        ("projected ServiceAccount token is empty", "empty"),
        ("hub-api refused the token request: UNAUTHENTICATED", "UNAUTHENTICATED"),
        ("hub-api token response malformed", "malformed"),
    ],
)
async def test_failed_mint_fails_loud(mint: _FakeMint, reason: str, match: str) -> None:
    mint.error = ServiceTokenError(reason)
    with pytest.raises(OverlayPushError, match=match):
        await mint_push_token(1)
    assert overlay_push._jwt_cache == {}


async def test_default_sa_path_used_when_unset(
    monkeypatch: pytest.MonkeyPatch, mint: _FakeMint
) -> None:
    monkeypatch.delenv("SERVICE_JWT_SA_TOKEN_PATH")
    await mint_push_token(1)
    assert mint.calls[0]["sa_token_path"] == overlay_push._DEFAULT_SA_TOKEN_PATH


async def test_push_transport_errors_fail_loud(dal: AsyncDAL) -> None:
    state = {"n": 0}

    def push_down(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        raise httpx.ReadTimeout("slow")

    async with _client(push_down) as client:
        with pytest.raises(OverlayPushError, match="svc-presentation unreachable"):
            await push_overlay(client, dal, community_id=1, surface="media", body={})
    assert state["n"] == 1
