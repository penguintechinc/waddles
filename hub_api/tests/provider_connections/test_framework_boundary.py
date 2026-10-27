"""The framework is provider-agnostic: a second provider plugs in through the registry alone.

"FakeChat" (``fake_provider.py``) implements the adapter contract and nothing else exists for it --
no service, store, API, DTO, migration-free test-schema or telemetry change. If these tests pass,
adding Slack/Discord/Kick really is "one adapter + one registry line".
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from blueprints.v1.provider_connections import SERVICE_CONFIG_KEY
from services.provider_connections import PlatformKeyVault, ProviderConnectionService
from services.provider_connections.adapter import AdapterRegistry
from services.provider_connections.errors import (
    CredentialsRejected,
    InvalidConnectionInput,
    ResourceUnresolvable,
)
from services.provider_connections.twitch import TwitchAdapter
from tests.provider_connections.conftest import FakeClock, admin_headers
from tests.provider_connections.fake_provider import FakeChatAdapter
from tests.provider_connections.fake_twitch import FakeTwitch
from tests.provider_connections.support import open_row, raw_rows

HUB_API = Path(__file__).resolve().parents[2]
FRAMEWORK_MODULES = [
    "services/provider_connections/adapter.py",
    "services/provider_connections/service.py",
    "services/provider_connections/store.py",
    "services/provider_connections/vault.py",
    "services/provider_connections/telemetry.py",
    "services/provider_connections/errors.py",
    "blueprints/v1/provider_connections.py",
]


@pytest.fixture
def two_provider_service(
    pc_db: Any, fake_twitch: FakeTwitch, clock: FakeClock
) -> ProviderConnectionService:
    registry = AdapterRegistry(
        [TwitchAdapter(transport=fake_twitch.transport, clock=clock), FakeChatAdapter()]
    )
    return ProviderConnectionService(pc_db.install_dal, registry, PlatformKeyVault(), clock=clock)


async def _create_room(
    service: ProviderConnectionService, db: Any, room: str = "lobby", token: str = "tok-room-1"
) -> Any:
    return await service.create(
        tenant_id=db.seed.tenant_id,
        community_id=db.seed.community_id,
        actor_user_id=db.seed.admin_user_id,
        provider="fakechat",
        access_token=token,
        refresh_token=None,
        metadata={"room": room},
        enabled=True,
    )


class TestSecondProviderThroughTheService:
    async def test_full_lifecycle_with_no_provider_specific_code_outside_the_adapter(
        self, two_provider_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        service = two_provider_service
        assert [s.provider for s in service.providers()] == ["fakechat", "twitch"]
        view = await _create_room(service, pc_db)
        conn = view.binding.connection
        assert (conn.platform, conn.resource_type, conn.resource_id) == (
            "fakechat",
            "fake_room",
            "r-1",
        )
        assert view.public_metadata == {
            "room": "lobby",
            "scopes": ["read"],
            "capabilities": ["chat_ingest"],
        }
        assert "server" not in view.public_metadata  # not in the adapter's public allow-list

        cid = conn.id
        scope = {"tenant_id": pc_db.seed.tenant_id, "community_id": pc_db.seed.community_id}
        moved = await service.update(
            connection_id=cid,
            actor_user_id=1,
            enabled=None,
            metadata={"room": "arena"},
            access_token=None,
            refresh_token=None,
            **scope,
        )
        assert moved.binding.connection.resource_id == "r-2"

        page = await service.list_for_dataplane(
            provider="fakechat", capability="chat_ingest", after=0, limit=5
        )
        assert [i.connection_id for i in page.items] == [cid]
        assert page.items[0].metadata == {
            "room": "arena",
            "scopes": ["read"],
            "capabilities": ["chat_ingest"],
            "server": "eu-1",
        }
        none = await service.list_for_dataplane(
            provider="fakechat", capability="events", after=0, limit=5
        )
        assert none.items == []  # a capability the scopes did not grant

        resolved = await service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)
        assert resolved.access_token == "tok-room-1" and resolved.provider == "fakechat"
        assert resolved.metadata["server"] == "eu-1"

        assert await service.delete(connection_id=cid, **scope) is True
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_adapter_declared_validation_applies_to_the_new_provider(
        self, two_provider_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        with pytest.raises(InvalidConnectionInput):
            await _create_room(two_provider_service, pc_db, room="NOT A ROOM")
        with pytest.raises(CredentialsRejected):
            await _create_room(two_provider_service, pc_db, token="wrong")
        with pytest.raises(ResourceUnresolvable):
            await _create_room(two_provider_service, pc_db, room="void")
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_the_same_vault_encrypts_the_new_providers_token(
        self, two_provider_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        await _create_room(two_provider_service, pc_db)
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert "tok-room-1" not in row["access_token"]
        assert open_row(row) == "tok-room-1"
        assert "tok-room-1" not in pc_db.db_path.read_bytes().decode("latin-1")

    async def test_providers_do_not_collide_or_leak_into_each_others_lists(
        self, two_provider_service: ProviderConnectionService, pc_db: Any, fake_twitch: FakeTwitch
    ) -> None:
        from tests.provider_connections.conftest import BOT, CHANNEL_LOGIN

        await _create_room(two_provider_service, pc_db)
        access, refresh = fake_twitch.mint(account=BOT)
        await two_provider_service.create(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            actor_user_id=1,
            provider="twitch",
            access_token=access,
            refresh_token=refresh,
            metadata={"channel_login": CHANNEL_LOGIN},
            enabled=True,
        )
        twitch_only = await two_provider_service.list_for_dataplane(
            provider="twitch", capability=None, after=0, limit=10
        )
        fake_only = await two_provider_service.list_for_dataplane(
            provider="fakechat", capability=None, after=0, limit=10
        )
        assert {i.provider for i in twitch_only.items} == {"twitch"}
        assert {i.provider for i in fake_only.items} == {"fakechat"}


class TestSecondProviderThroughHttp:
    async def test_descriptors_and_crud_need_no_per_provider_api_code(
        self, client: Any, pc_app: Any, pc_db: Any, two_provider_service: ProviderConnectionService
    ) -> None:
        pc_app.config[SERVICE_CONFIG_KEY] = two_provider_service
        providers = (
            await (
                await client.get("/api/v1/connection-providers", headers=admin_headers(pc_db))
            ).get_json()
        )["providers"]
        fake = next(p for p in providers if p["provider"] == "fakechat")
        assert fake["metadataFields"][0]["name"] == "room" and fake["refreshable"] is False
        assert fake["requiredScopes"] == ["read"] and fake["resourceType"] == "fake_room"

        base = f"/api/v1/communities/{pc_db.seed.community_id}/provider-connections"
        created = await client.post(
            base,
            headers=admin_headers(pc_db),
            data=json.dumps(
                {
                    "provider": "fakechat",
                    "credentials": {"access_token": "tok-http-1"},
                    "metadata": {"room": "lobby"},
                }
            ),
        )
        assert created.status_code == 201
        conn = (await created.get_json())["connection"]
        assert (conn["provider"], conn["resourceType"], conn["metadata"]) == (
            "fakechat",
            "fake_room",
            {"room": "lobby", "scopes": ["read"], "capabilities": ["chat_ingest"]},
        )
        assert "tok-http-1" not in await created.get_data(as_text=True)


class TestTheBoundaryIsReal:
    @pytest.mark.parametrize("relative", FRAMEWORK_MODULES)
    def test_no_provider_name_appears_in_framework_code(self, relative: str) -> None:
        """Identifiers and string literals (docstrings excluded) in framework code never name Twitch."""
        source = (HUB_API / relative).read_text()
        tree = ast.parse(source)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        offenders: list[str] = []
        examined = 0
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.append(node.name)
            elif isinstance(node, ast.arg):
                names.append(node.arg)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                names.append(node.value)
            examined += len(names)
            offenders += [n for n in names if "twitch" in n.lower()]
        assert examined > 20  # the scan must have looked at real code
        assert offenders == []

    def test_only_the_registry_and_the_adapter_import_the_twitch_module(self) -> None:
        importers = sorted(
            str(path.relative_to(HUB_API))
            for path in (HUB_API / "services").rglob("*.py")
            if "provider_connections.twitch" in path.read_text()
            or "from services.provider_connections.twitch" in path.read_text()
        )
        assert importers == ["services/provider_connections/registry.py"]
