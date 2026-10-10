"""Races and defensive branches: the checks that exist because the world changes between statements."""

from __future__ import annotations

import importlib
import logging
from typing import Any

import pytest

import services.provider_connections.twitch as twitch_module
from blueprints.v1 import provider_connections as bp
from services.provider_connections import service as service_module
from services.provider_connections import telemetry
from services.provider_connections.adapter import AdapterRegistry, validate_metadata
from services.provider_connections.errors import (
    ConnectionConflict,
    ConnectionNotFound,
    InvalidConnectionInput,
    ProviderConnectionError,
    ProviderUnavailable,
)
from services.provider_connections.service import ProviderConnectionService
from services.provider_connections.vault import PlatformKeyVault
from tests.provider_connections.conftest import FakeClock, admin_headers
from tests.provider_connections.fake_provider import FAKE_SPEC, FakeChatAdapter
from tests.provider_connections.support import create_twitch

TWO_FIELD_SPEC = FAKE_SPEC


def _scope(db: Any) -> dict[str, int]:
    return {"tenant_id": db.seed.tenant_id, "community_id": db.seed.community_id}


class TestUpdateGuards:
    async def test_refresh_token_without_access_token_is_refused_even_alongside_a_toggle(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        with pytest.raises(InvalidConnectionInput, match="together with access_token"):
            await pc_service.update(
                connection_id=view.binding.connection.id,
                actor_user_id=1,
                enabled=True,
                metadata=None,
                access_token=None,
                refresh_token="orphan",
                **_scope(pc_db),
            )

    async def test_the_grant_vanishing_between_read_and_toggle_is_not_found(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)

        async def gone(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(service_module.store, "set_enabled", gone)
        with pytest.raises(ConnectionNotFound):
            await pc_service.update(
                connection_id=view.binding.connection.id,
                actor_user_id=1,
                enabled=False,
                metadata=None,
                access_token=None,
                refresh_token=None,
                **_scope(pc_db),
            )

    async def test_the_grant_vanishing_between_read_and_delete_is_not_found(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)

        async def gone(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(service_module.store, "delete_for_community", gone)
        with pytest.raises(ConnectionNotFound):
            await pc_service.delete(connection_id=view.binding.connection.id, **_scope(pc_db))

    async def test_the_row_vanishing_before_a_metadata_only_update_can_fetch_a_token(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)

        async def gone(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(service_module.store, "read_resolve_state", gone)
        with pytest.raises(ConnectionNotFound):
            await pc_service.update(
                connection_id=view.binding.connection.id,
                actor_user_id=1,
                enabled=None,
                metadata={"channel_login": "othercast"},
                access_token=None,
                refresh_token=None,
                **_scope(pc_db),
            )


class TestSecondProviderUpdateGuards:
    @pytest.fixture
    def fake_service(
        self, pc_db: Any, clock: FakeClock
    ) -> tuple[ProviderConnectionService, FakeChatAdapter]:
        adapter = FakeChatAdapter()
        service = ProviderConnectionService(
            pc_db.install_dal, AdapterRegistry([adapter]), PlatformKeyVault(), clock=clock
        )
        return service, adapter

    async def _room(self, service: ProviderConnectionService, db: Any) -> Any:
        return await service.create(
            tenant_id=db.seed.tenant_id,
            community_id=db.seed.community_id,
            actor_user_id=None,
            provider="fakechat",
            access_token="tok-a",
            refresh_token=None,
            metadata={"room": "lobby"},
            enabled=True,
        )

    async def test_rotating_to_a_refresh_token_on_a_non_refreshable_provider_is_refused(
        self, fake_service: tuple[ProviderConnectionService, FakeChatAdapter], pc_db: Any
    ) -> None:
        service, _ = fake_service
        view = await self._room(service, pc_db)
        with pytest.raises(InvalidConnectionInput, match="does not use a refresh token"):
            await service.update(
                connection_id=view.binding.connection.id,
                actor_user_id=None,
                enabled=None,
                metadata=None,
                access_token="tok-b",
                refresh_token="ref",
                **_scope(pc_db),
            )

    async def test_credentials_that_now_resolve_to_a_different_resource_are_a_conflict(
        self, fake_service: tuple[ProviderConnectionService, FakeChatAdapter], pc_db: Any
    ) -> None:
        service, adapter = fake_service
        view = await self._room(service, pc_db)
        adapter.rooms["lobby"] = "r-999"  # the provider re-numbered the room behind our back
        with pytest.raises(ConnectionConflict, match="different resource"):
            await service.update(
                connection_id=view.binding.connection.id,
                actor_user_id=None,
                enabled=None,
                metadata=None,
                access_token="tok-b",
                refresh_token=None,
                **_scope(pc_db),
            )

    async def test_rotation_without_new_metadata_replays_the_stored_inputs(
        self, fake_service: tuple[ProviderConnectionService, FakeChatAdapter], pc_db: Any
    ) -> None:
        service, _ = fake_service
        view = await self._room(service, pc_db)
        rotated = await service.update(
            connection_id=view.binding.connection.id,
            actor_user_id=None,
            enabled=None,
            metadata=None,
            access_token="tok-b",
            refresh_token=None,
            **_scope(pc_db),
        )
        assert rotated.binding.connection.resource_id == "r-1"
        assert rotated.public_metadata["room"] == "lobby"


class TestSmallBranches:
    def test_an_explicit_null_for_a_required_field_is_missing(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="'room' is required"):
            validate_metadata(TWO_FIELD_SPEC, {"room": None})

    def test_error_code_can_be_overridden_per_instance(self) -> None:
        assert ProviderConnectionError("x").code == "connection_error"
        assert ProviderConnectionError("x", code="custom").code == "custom"

    async def test_an_unexpected_exception_is_labelled_error_not_its_text(self) -> None:
        with pytest.raises(RuntimeError), telemetry.operation_span("twitch", "boom"):
            raise RuntimeError("token=SECRETVALUE")
        assert telemetry._outcome_label(RuntimeError("x")) == "error"  # noqa: SLF001

    def test_the_default_app_credentials_come_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TWITCH_CLIENT_ID", "env-client-id")
        monkeypatch.setenv("TWITCH_CLIENT_SECRET", "env-client-secret")
        assert twitch_module._env_app_credentials() == ("env-client-id", "env-client-secret")  # noqa: SLF001
        monkeypatch.delenv("TWITCH_CLIENT_SECRET")
        from services.oauth_providers import ProviderNotConfigured

        with pytest.raises(ProviderNotConfigured):
            twitch_module._env_app_credentials()  # noqa: SLF001

    def test_the_httpx_log_filter_installs_exactly_once_and_is_narrow(self) -> None:
        importlib.reload(twitch_module)
        importlib.reload(twitch_module)
        installed = [
            f
            for f in logging.getLogger("httpx").filters
            if isinstance(f, twitch_module._SuppressTwitchRequestLines)  # noqa: SLF001
        ]
        assert len(installed) == 1
        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            "",
            0,
            'HTTP Request: GET https://api.twitch.tv/helix/users?login=x "200"',
            None,
            None,
        )
        other = logging.LogRecord(
            "httpx", logging.INFO, "", 0, 'HTTP Request: GET https://example.com/ "200"', None, None
        )
        assert installed[0].filter(record) is False  # Twitch lines are dropped...
        assert installed[0].filter(other) is True  # ...every other host still logs


class TestBlueprintDefensiveBranches:
    async def test_a_request_with_no_tenant_context_is_forbidden_by_the_guard(
        self, pc_app: Any
    ) -> None:
        async with pc_app.test_request_context("/"):
            with pytest.raises(Exception) as info:  # noqa: PT011
                await bp._admin_guard(1)  # noqa: SLF001
            assert getattr(info.value, "status_code", None) == 403

    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            (ProviderUnavailable("down"), 502, "PROVIDER_UNAVAILABLE"),
            (ConnectionNotFound("nope"), 404, "NOT_FOUND"),
        ],
    )
    async def test_service_errors_surface_through_the_list_route(
        self,
        client: Any,
        pc_app: Any,
        pc_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
        status: int,
        code: str,
    ) -> None:
        async def boom(*args: Any, **kwargs: Any) -> None:
            raise error

        monkeypatch.setattr(pc_app.config[bp.SERVICE_CONFIG_KEY], "list", boom)
        response = await client.get(
            f"/api/v1/communities/{pc_db.seed.community_id}/provider-connections",
            headers=admin_headers(pc_db),
        )
        assert response.status_code == status
        assert (await response.get_json())["error"]["code"] == code

    async def test_an_unmapped_error_type_is_a_500_not_an_implicit_4xx(
        self, client: Any, pc_app: Any, pc_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Novel(ProviderConnectionError):
            code = "novel"

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise Novel("surprising internal detail")

        monkeypatch.setattr(pc_app.config[bp.SERVICE_CONFIG_KEY], "list", boom)
        response = await client.get(
            f"/api/v1/communities/{pc_db.seed.community_id}/provider-connections",
            headers=admin_headers(pc_db),
        )
        assert response.status_code == 500
        text = await response.get_data(as_text=True)
        assert "surprising internal detail" not in text  # 5xx text is generic

    async def test_malformed_json_body_is_a_client_error(self, client: Any, pc_db: Any) -> None:
        response = await client.post(
            f"/api/v1/communities/{pc_db.seed.community_id}/provider-connections",
            headers=admin_headers(pc_db),
            data="{not json",
        )
        assert response.status_code == 400
