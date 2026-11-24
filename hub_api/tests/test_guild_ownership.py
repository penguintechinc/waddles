"""`services/guild_ownership.verify_guild_control` -- the S3 pairing proof (fail-closed)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from services import guild_ownership as own
from services.credential_resolver import TransportUnavailable
from services.errors import ApiError
from tests.test_role_sync_service import _FakeCredentialResolver, _link_identity

GUILD = "123456789012345678"  # gitleaks:allow - fake snowflake
ACTOR_DISCORD = "800001"
ROLE_ADMIN = "555"


def _http(guild_status: int = 200, guild: Any = None, member: Any = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/guilds/{GUILD}"):
            body = guild if guild is not None else {"owner_id": "1", "roles": []}
            return httpx.Response(guild_status, json=body)
        if member is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=member)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _verify(dal: Any, cid: int, client: httpx.AsyncClient, actor: int | None = 1) -> None:
    await own.verify_guild_control(
        dal,
        cid,
        GUILD,
        actor_user_id=actor,
        http_client=client,
        credential_resolver=_FakeCredentialResolver(),  # type: ignore[arg-type]
    )


@pytest.fixture
def linked(guild_pairing_db: Any) -> tuple[Any, int]:
    dal, cid, _t, _g = guild_pairing_db
    _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=ACTOR_DISCORD)
    return dal, cid


async def test_guild_owner_passes(linked: tuple[Any, int]) -> None:
    dal, cid = linked
    await _verify(dal, cid, _http(guild={"owner_id": ACTOR_DISCORD, "roles": []}))


@pytest.mark.parametrize("perm", [0x8, 0x20])
async def test_admin_or_manage_guild_role_passes(linked: tuple[Any, int], perm: int) -> None:
    dal, cid = linked
    guild = {"owner_id": "1", "roles": [{"id": ROLE_ADMIN, "permissions": str(perm)}]}
    await _verify(dal, cid, _http(guild=guild, member={"roles": [ROLE_ADMIN]}))


@pytest.mark.parametrize(
    ("client_kwargs", "code"),
    [
        ({"guild_status": 404}, "guild_bot_not_installed"),
        ({}, "guild_control_not_member"),
        (
            {
                "guild": {"owner_id": "1", "roles": [{"id": "9", "permissions": "268435456"}]},
                "member": {"roles": ["9"]},
            },
            "guild_control_insufficient",
        ),
    ],
)
async def test_refusals(linked: tuple[Any, int], client_kwargs: Any, code: str) -> None:
    dal, cid = linked
    with pytest.raises(ApiError) as excinfo:
        await _verify(dal, cid, _http(**client_kwargs))
    assert excinfo.value.status_code == 403 and excinfo.value.code == code


async def test_unlinked_actor_refused(guild_pairing_db: Any) -> None:
    dal, cid, _t, _g = guild_pairing_db
    with pytest.raises(ApiError) as excinfo:
        await _verify(dal, cid, _http(), actor=None)
    assert excinfo.value.code == "guild_control_unlinked"


async def test_missing_bot_credentials_is_503(linked: tuple[Any, int]) -> None:
    dal, cid = linked

    class _NoCreds:
        async def resolve(self, *a: Any, **kw: Any) -> Any:
            raise TransportUnavailable("none")

    with pytest.raises(ApiError) as excinfo:
        await own.verify_guild_control(
            dal,
            cid,
            GUILD,
            actor_user_id=1,
            http_client=_http(),
            credential_resolver=_NoCreds(),  # type: ignore[arg-type]
        )
    assert excinfo.value.status_code == 503


async def test_transport_and_malformed_errors_fail_closed(linked: tuple[Any, int]) -> None:
    dal, cid = linked

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x", request=request)

    with pytest.raises(ApiError) as excinfo:
        await _verify(dal, cid, httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    assert excinfo.value.status_code == 502
    with pytest.raises(ApiError):
        await _verify(dal, cid, _http(guild=["not", "a", "dict"]))
