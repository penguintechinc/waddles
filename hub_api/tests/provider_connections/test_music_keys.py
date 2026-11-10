"""SoundCloud app credentials + YouTube Data API key: sealed, write-only, resolver-only, not logged.

Real path: real blueprints, service, vault and store over sqlite; no network (the adapters never
call out). Spotify app credentials are env-sourced (see ``test_oauth_providers``), not per community.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from flask_core.service_jwt import ServiceJwtIssuer
from quart import Quart

from blueprints.v1.provider_connections import SERVICE_CONFIG_KEY
from services.provider_connections import PlatformKeyVault, ProviderConnectionService
from services.provider_connections.errors import RefreshNotConfigured
from services.provider_connections.registry import build_default_registry
from tests.provider_connections.conftest import FakeClock, admin_headers
from tests.provider_connections.support import open_row, raw_rows
from tests.provider_connections.test_resolve_v2 import _bound, _url

SC_ID = "scclientid0123456789abcdef"
SC_SECRET = "fake-soundcloud-secret-" + "a" * 12
YT_KEY = "AIza" + "A" * 35
YT_KEY_2 = "AIza" + "B" * 35


@pytest.fixture
def music_service(pc_db: Any, pc_app: Quart, clock: FakeClock) -> ProviderConnectionService:
    """The default registry (real adapters) wired into the app."""
    service = ProviderConnectionService(
        pc_db.install_dal, build_default_registry(), PlatformKeyVault(), clock=clock
    )
    pc_app.config[SERVICE_CONFIG_KEY] = service
    return service


def _base(db: Any) -> str:
    return f"/api/v1/communities/{db.seed.community_id}/provider-connections"


def _sc(secret: str = SC_SECRET, client_id: str = SC_ID) -> dict[str, Any]:
    return {
        "provider": "soundcloud",
        "credentials": {"access_token": secret},
        "metadata": {"client_id": client_id},
    }


def _yt(key: str = YT_KEY, label: str = "main") -> dict[str, Any]:
    return {
        "provider": "youtube_api",
        "credentials": {"access_token": key},
        "metadata": {"key_label": label},
    }


@pytest.mark.parametrize(
    ("body", "secret", "platform", "public_key"),
    [
        (_sc(), SC_SECRET, "soundcloud", "client_id"),
        (_yt(), YT_KEY, "youtube_api", "key_label"),
    ],
)
class TestRoundTrip:
    async def test_create_is_write_only_sealed_and_resolves_to_the_authorized_identity(
        self,
        client: Any,
        pc_db: Any,
        music_service: Any,
        service_issuer: ServiceJwtIssuer,
        body: dict[str, Any],
        secret: str,
        platform: str,
        public_key: str,
    ) -> None:
        created = await client.post(_base(pc_db), headers=admin_headers(pc_db), json=body)
        text = await created.get_data(as_text=True)
        assert created.status_code == 201, text
        assert secret not in text
        conn = json.loads(text)["connection"]
        assert conn["provider"] == platform and conn["usable"] is True
        assert public_key in conn["metadata"]
        for url in (_base(pc_db), f"{_base(pc_db)}/{conn['id']}"):
            reply = await client.get(url, headers=admin_headers(pc_db))
            assert secret not in await reply.get_data(as_text=True)

        (row,) = await raw_rows(pc_db, "platform_connections")
        assert secret not in row["access_token"]
        assert open_row(row) == secret

        community = pc_db.seed.community_id
        resolved = await client.post(
            _url(community, conn["id"]),
            headers=_bound(service_issuer, community, scope="connections:resolve"),
        )
        data = await resolved.get_json()
        assert resolved.status_code == 200, data
        assert data["accessToken"] == secret and data["provider"] == platform
        assert data["metadata"][public_key] == body["metadata"][public_key]

    async def test_secret_is_never_logged(
        self,
        client: Any,
        pc_db: Any,
        music_service: Any,
        caplog: pytest.LogCaptureFixture,
        body: dict[str, Any],
        secret: str,
        platform: str,
        public_key: str,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        await client.post(_base(pc_db), headers=admin_headers(pc_db), json=body)
        bad = json.loads(json.dumps(body))
        bad["credentials"]["access_token"] = "not valid!"
        refused = await client.post(_base(pc_db), headers=admin_headers(pc_db), json=bad)
        assert refused.status_code == 400
        assert "not valid!" not in await refused.get_data(as_text=True)
        assert caplog.records
        assert secret not in "\n".join(r.getMessage() for r in caplog.records)

    async def test_rotation_replaces_the_secret_and_bumps_the_counter(
        self,
        client: Any,
        pc_db: Any,
        music_service: Any,
        body: dict[str, Any],
        secret: str,
        platform: str,
        public_key: str,
    ) -> None:
        created = await client.post(_base(pc_db), headers=admin_headers(pc_db), json=body)
        cid = (await created.get_json())["connection"]["id"]
        (before,) = await raw_rows(pc_db, "platform_connections")
        new = SC_SECRET[:-1] + "Z" if platform == "soundcloud" else YT_KEY_2
        patched = await client.patch(
            f"{_base(pc_db)}/{cid}",
            headers=admin_headers(pc_db),
            json={"credentials": {"access_token": new}},
        )
        assert patched.status_code == 200, await patched.get_data(as_text=True)
        (after,) = await raw_rows(pc_db, "platform_connections")
        assert open_row(after) == new
        assert after["secret_version"] == before["secret_version"] + 1


@pytest.mark.parametrize(
    "body",
    [
        _sc(secret="short"),
        _sc(secret="has space in it 0123456789"),
        _sc(client_id="x"),
        _sc(client_id="bad id!!bad id!!bad"),
        _yt(key="not-a-google-key"),
        _yt(key="AIza" + "A" * 34),
        _yt(label="bad label!"),
    ],
)
async def test_malformed_input_is_a_400_and_stores_nothing(
    client: Any, pc_db: Any, music_service: Any, body: dict[str, Any]
) -> None:
    response = await client.post(_base(pc_db), headers=admin_headers(pc_db), json=body)
    assert response.status_code == 400, await response.get_data(as_text=True)
    assert await raw_rows(pc_db, "platform_connections") == []


async def test_descriptors_list_both_providers(client: Any, pc_db: Any, music_service: Any) -> None:
    response = await client.get("/api/v1/connection-providers", headers=admin_headers(pc_db))
    providers = {p["provider"]: p for p in (await response.get_json())["providers"]}
    assert providers["soundcloud"]["resourceType"] == "soundcloud_app"
    assert providers["youtube_api"]["resourceType"] == "youtube_api_key"
    assert providers["soundcloud"]["refreshable"] is False


async def test_static_keys_have_no_refresh_flow() -> None:
    from services.provider_connections.music_keys import SoundcloudAdapter, YoutubeApiKeyAdapter

    for adapter in (SoundcloudAdapter(), YoutubeApiKeyAdapter()):
        with pytest.raises(RefreshNotConfigured):
            await adapter.refresh("x", {})
        assert adapter.granted_capabilities(()) == adapter.spec.capabilities
