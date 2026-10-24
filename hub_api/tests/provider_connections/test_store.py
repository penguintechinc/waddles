"""The persistence layer on its own: transactional guards that the service also checks upstream."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from penguin_dal import AsyncDB
from sqlalchemy import MetaData, text

from services.provider_connections import store
from services.provider_connections.errors import (
    ConnectionConflict,
    ConnectionNotFound,
    SchemaMissing,
)
from services.provider_connections.service import ProviderConnectionService
from tests.provider_connections.conftest import OTHER_CHANNEL_LOGIN, build_pc_metadata
from tests.provider_connections.fake_twitch import FakeTwitch
from tests.provider_connections.support import create_twitch, grant_to, raw_rows

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
async def view(pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any) -> Any:
    return await create_twitch(pc_service, pc_db, bot_tokens)


class TestUpdateLink:
    async def test_the_store_itself_refuses_to_rewrite_a_shared_link(
        self, pc_db: Any, view: Any
    ) -> None:
        """Defence in depth: a grant added after the service's pre-check must still be honoured."""
        cid = view.binding.connection.id
        await grant_to(pc_db, community_id=pc_db.seed.second_community_id, connection_id=cid)
        before = await raw_rows(pc_db, "platform_connections")
        with pytest.raises(ConnectionConflict, match="shared with other communities"):
            await store.update_link(
                pc_db.install_dal,
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=cid,
                values={"resource_id": "hijacked"},
                now=NOW,
            )
        assert await raw_rows(pc_db, "platform_connections") == before

    async def test_missing_connection(self, pc_db: Any) -> None:
        with pytest.raises(ConnectionNotFound):
            await store.update_link(
                pc_db.install_dal,
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=42,
                values={"status": "active"},
                now=NOW,
            )

    async def test_other_tenants_connection_is_not_found(self, pc_db: Any, view: Any) -> None:
        with pytest.raises(ConnectionNotFound):
            await store.update_link(
                pc_db.install_dal,
                tenant_id=pc_db.seed.other_tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=view.binding.connection.id,
                values={"status": "revoked"},
                now=NOW,
            )
        assert (await raw_rows(pc_db, "platform_connections"))[0]["status"] == "active"

    async def test_unique_violation_on_repoint_is_a_conflict(
        self, pc_service: ProviderConnectionService, pc_db: Any, view: Any, fake_twitch: FakeTwitch
    ) -> None:
        from tests.provider_connections.conftest import BOT

        other = await create_twitch(
            pc_service, pc_db, fake_twitch.mint(account=BOT), channel=OTHER_CHANNEL_LOGIN
        )
        with pytest.raises(ConnectionConflict, match="already connected"):
            await store.update_link(
                pc_db.install_dal,
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=other.binding.connection.id,
                values={"resource_id": view.binding.connection.resource_id},
                now=NOW,
            )


class TestMissingRows:
    async def test_toggle_delete_and_get_on_nothing(self, pc_db: Any) -> None:
        scope = {"tenant_id": pc_db.seed.tenant_id, "community_id": pc_db.seed.community_id}
        assert (
            await store.set_enabled(
                pc_db.install_dal, connection_id=1, enabled=True, now=NOW, **scope
            )
            is None
        )
        assert await store.delete_for_community(pc_db.install_dal, connection_id=1, **scope) is None
        assert await store.get_binding(pc_db.install_dal, connection_id=1, **scope) is None
        assert (
            await store.read_resolve_state(
                pc_db.install_dal, community_id=pc_db.seed.community_id, connection_id=1
            )
            is None
        )

    async def test_lock_for_refresh_on_nothing_yields_none(self, pc_db: Any) -> None:
        async with store.lock_for_refresh(
            pc_db.install_dal, community_id=pc_db.seed.community_id, connection_id=1
        ) as locked:
            assert locked is None

    async def test_the_lock_context_rolls_back_when_its_body_raises(
        self, pc_db: Any, view: Any
    ) -> None:
        cid = view.binding.connection.id
        with pytest.raises(RuntimeError, match="boom"):
            async with store.lock_for_refresh(
                pc_db.install_dal, community_id=pc_db.seed.community_id, connection_id=cid
            ) as locked:
                assert locked is not None
                await locked.mark_unusable(status="revoked", reason="oops", now=NOW)
                raise RuntimeError("boom")
        assert (await raw_rows(pc_db, "platform_connections"))[0]["status"] == "active"

    async def test_the_lock_context_commits_on_clean_exit(self, pc_db: Any, view: Any) -> None:
        cid = view.binding.connection.id
        async with store.lock_for_refresh(
            pc_db.install_dal, community_id=pc_db.seed.community_id, connection_id=cid
        ) as locked:
            assert locked is not None
            await locked.mark_unusable(status="expired", reason="token_expired", now=NOW)
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert (row["status"], row["status_reason"]) == ("expired", "token_expired")


class TestSchemaGuard:
    async def test_missing_tables_fail_loudly_with_the_migration_to_run(
        self, tmp_path: Any
    ) -> None:
        db = AsyncDB(f"sqlite://{tmp_path / 'empty.db'}", pool_size=1)
        await db.reflect()
        try:
            with pytest.raises(SchemaMissing, match="table.*not present"):
                await store.list_bindings(db, tenant_id=1, community_id=1)
        finally:
            await db.close()

    async def test_missing_0058_columns_fail_loudly_naming_the_migration(
        self, tmp_path: Any
    ) -> None:
        """A database that has 0035 but not 0058 must refuse, not run on a half-schema."""
        db = AsyncDB(f"sqlite://{tmp_path / 'half.db'}", pool_size=1)
        async with db.engine.begin() as conn:
            await conn.run_sync(build_pc_metadata().create_all)
            await conn.execute(
                text("CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id INTEGER)")
            )
            for column in ("resource_metadata", "expires_at", "status_reason"):
                await conn.execute(text(f"ALTER TABLE platform_connections DROP COLUMN {column}"))
            await conn.execute(text("ALTER TABLE community_connection_access DROP COLUMN enabled"))
        await db.reflect()
        try:
            with pytest.raises(SchemaMissing, match="0058_provider_connections") as info:
                await store.list_bindings(db, tenant_id=1, community_id=1)
            assert "resource_metadata" in info.value.message and "enabled" in info.value.message
        finally:
            await db.close()


class TestDecoding:
    def test_json_text_is_decoded_and_junk_is_empty(self) -> None:
        assert store._as_dict('{"a": 1}') == {"a": 1}  # noqa: SLF001
        assert store._as_dict({"a": 1}) == {"a": 1}  # noqa: SLF001
        assert store._as_dict(None) == {}  # noqa: SLF001
        assert store._as_dict("[1, 2]") == {}  # noqa: SLF001

    def test_naive_datetimes_are_treated_as_utc(self) -> None:
        assert store._aware(None) is None  # noqa: SLF001
        naive = datetime(2026, 1, 1, 0, 0)
        assert store._aware(naive) == naive.replace(tzinfo=UTC)  # noqa: SLF001
        aware = datetime(2026, 1, 1, tzinfo=UTC)
        assert store._aware(aware) is aware  # noqa: SLF001

    def test_unused_metadata_import_is_the_mirror_builder(self) -> None:
        assert isinstance(build_pc_metadata(), MetaData)


# --------------------------------------------------------------------------
# Static-secret kinds (migration 0064): sealing, secrecy, lifecycle
# --------------------------------------------------------------------------

import json  # noqa: E402
from uuid import UUID  # noqa: E402

from sqlalchemy import CheckConstraint, event  # noqa: E402

from services.platform_integrations_crypto import PlatformCredentialCryptoError  # noqa: E402
from services.provider_connections.errors import (  # noqa: E402
    InvalidConnectionInput,
    UnknownProvider,
)
from services.provider_connections.kinds import ConnectionKind  # noqa: E402
from services.provider_connections.vault import PlatformKeyVault  # noqa: E402
from tests.provider_connections.support import (  # noqa: E402
    STATIC_BEARER,
    STATIC_URL,
    create_static,
    open_row,
    row_aad,
    set_column,
)

AWS_BODY = {
    "provider": "aws_lambda",
    "scheme": 4,
    "document": {"access_key_id": "AKIAEXAMPLE", "secret_access_key": "wJalrEXAMPLEKEY"},
    "metadata": {"region": "us-east-1", "function": "waddles-hook"},
}
GCP_BODY = {
    "provider": "gcp_function",
    "scheme": 5,
    "document": {"client_email": "sa@p.iam.gserviceaccount.com", "private_key": "PEMBODY"},
    "metadata": {"project": "my-project-1", "region": "us-central1", "function": "hook"},
}


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    """A raw row's ``resource_metadata`` (sqlite hands the JSON column back as text)."""
    value = row["resource_metadata"]
    return json.loads(value) if isinstance(value, str) else dict(value)


def _kw(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in ("provider", "scheme", "document", "metadata")}


class TestStaticCreate:
    async def test_create_static_connection_seals_secret_with_row_bound_aad(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        (row,) = await raw_rows(pc_db, "platform_connections")
        whole = json.dumps({k: str(v) for k, v in row.items()})
        assert STATIC_BEARER not in whole  # not even in the row's text form
        assert row["sealed_secret"] != json.dumps({"token": STATIC_BEARER})
        assert row["access_token"] is None and row["refresh_token"] is None
        assert (row["connection_kind"], row["seal_version"]) == (2, 1)
        assert row["platform"] == row["resource_type"] == "external_http"
        assert (
            row["resource_id"]
            == str(UUID(row["public_id"]))
            == str(view.binding.connection.public_id)
        )
        assert json.loads(open_row(row, "sealed_secret")) == {"token": STATIC_BEARER}
        # non-secret target facts only
        assert _meta(row)["url"] == STATIC_URL
        assert _meta(row)["secret_scheme"] == 1
        assert STATIC_BEARER not in json.dumps(_meta(row))
        assert view.usable and view.capabilities == ["dispatch"]
        assert view.binding.connection.connection_kind == int(ConnectionKind.EXTERNAL_HTTP)

    async def test_the_ciphertext_opens_only_under_its_own_row_identity(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        await create_static(pc_static_service, pc_db, metadata={"url": "https://a.example.com"})
        await create_static(pc_static_service, pc_db, metadata={"url": "https://b.example.com"})
        first, second = await raw_rows(pc_db, "platform_connections")
        vault = PlatformKeyVault()
        with pytest.raises(Exception) as swapped:  # noqa: PT011 - VaultCorrupt, asserted below
            vault.open(first["sealed_secret"], aad=row_aad(second), seal_version=1)
        assert type(swapped.value).__name__ == "VaultCorrupt"
        # and a stripped (no-AAD) open fails too: the row-bound key is domain-separated
        with pytest.raises(PlatformCredentialCryptoError):
            from services.platform_integrations_crypto import decrypt_value

            decrypt_value(first["sealed_secret"])

    @pytest.mark.parametrize("body", [AWS_BODY, GCP_BODY], ids=["aws_lambda", "gcp_function"])
    async def test_aws_and_gcp_kinds_store_their_target_and_seal_their_key(
        self, pc_static_service: ProviderConnectionService, pc_db: Any, body: dict[str, Any]
    ) -> None:
        view = await create_static(pc_static_service, pc_db, **_kw(body))
        (row,) = await raw_rows(pc_db, "platform_connections")
        kind = {"aws_lambda": 3, "gcp_function": 4}[body["provider"]]
        assert row["connection_kind"] == kind == view.binding.connection.connection_kind
        assert json.loads(open_row(row, "sealed_secret")) == body["document"]
        for key, value in body["metadata"].items():
            assert _meta(row)[key] == value
        for secret in body["document"].values():
            assert secret not in json.dumps(
                {k: str(v) for k, v in row.items() if k != "sealed_secret"}
            )

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"provider": "nope"}, None),
            ({"provider": "twitch"}, "OAuth"),
            ({"scheme": 99}, "unknown secret scheme"),
            ({"scheme": 4}, "not supported"),
            ({"document": {"token": "x", "extra": "y"}}, "unknown"),
            ({"document": {}}, "missing"),
            ({"metadata": {"url": "http://x.example"}}, "https"),
            ({"metadata": {"url": "https://10.0.0.1/x"}}, "private"),
            ({"metadata": {"url": "https://ok.example\n"}}, "format"),
            ({"metadata": {}}, "required"),
            ({"metadata": {"url": STATIC_URL, "extra": "x"}}, "unknown"),
        ],
    )
    async def test_bad_input_is_refused_and_nothing_is_stored(
        self,
        pc_static_service: ProviderConnectionService,
        pc_db: Any,
        overrides: dict[str, Any],
        message: str | None,
    ) -> None:
        expected: type[Exception] = UnknownProvider if message is None else InvalidConnectionInput
        with pytest.raises(expected) as info:
            await create_static(pc_static_service, pc_db, **overrides)
        if message is not None:
            assert message in info.value.args[0]
        assert STATIC_BEARER not in str(info.value)
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_a_foreign_community_is_not_found_and_stores_nothing(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        with pytest.raises(ConnectionNotFound):
            await create_static(
                pc_static_service, pc_db, community_id=pc_db.seed.other_community_id
            )
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_an_oauth_create_refuses_a_static_provider(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        with pytest.raises(InvalidConnectionInput, match="secret document"):
            await pc_static_service.create(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                actor_user_id=None,
                provider="external_http",
                access_token="tok",
                refresh_token=None,
                metadata={"url": STATIC_URL},
                enabled=True,
            )
        assert pc_static_service.provider_kind("external_http") is ConnectionKind.EXTERNAL_HTTP
        assert pc_static_service.provider_kind("twitch") is ConnectionKind.PLATFORM_OAUTH
        with pytest.raises(UnknownProvider):
            pc_static_service.provider_kind("nope")


class TestStaticSecrecy:
    async def test_static_connection_secret_never_in_admin_views_or_sql(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        cid = view.binding.connection.id
        statements: list[str] = []

        def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = pc_db.install_dal.engine.sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            scope = {"tenant_id": pc_db.seed.tenant_id, "community_id": pc_db.seed.community_id}
            listed = await pc_static_service.list(**scope)
            got = await pc_static_service.get(connection_id=cid, **scope)
            await pc_static_service.update(
                connection_id=cid,
                actor_user_id=1,
                enabled=False,
                metadata=None,
                access_token=None,
                refresh_token=None,
                **scope,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert statements  # the scan below must examine something
        for sql in statements:
            assert "sealed_secret" not in sql, sql
        for shown in (view, got, listed[0]):
            assert STATIC_BEARER not in repr(shown)
        assert STATIC_BEARER not in repr(view.binding.connection)

    def test_sealed_secrets_repr_hides_the_static_ciphertext(self) -> None:
        sealed = store.SealedSecrets(
            access_token=None, refresh_token=None, seal_version=1, sealed_secret="CIPHERTEXT-S"
        )
        assert "CIPHERTEXT" not in repr(sealed)


class TestStaticLifecycle:
    async def test_toggle_works_but_editing_the_link_is_refused(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        scope = {"tenant_id": pc_db.seed.tenant_id, "community_id": pc_db.seed.community_id}
        cid = view.binding.connection.id
        off = await pc_static_service.update(
            connection_id=cid,
            actor_user_id=1,
            enabled=False,
            metadata=None,
            access_token=None,
            refresh_token=None,
            **scope,
        )
        assert off.binding.grant.enabled is False
        for change in ({"metadata": {"url": "https://other.example.com"}}, {"access_token": "x"}):
            args = {"enabled": None, "metadata": None, "access_token": None, "refresh_token": None}
            args.update(change)
            with pytest.raises(InvalidConnectionInput, match="static-secret"):
                await pc_static_service.update(connection_id=cid, actor_user_id=1, **args, **scope)
        (row,) = await raw_rows(pc_db, "platform_connections")
        assert _meta(row)["url"] == STATIC_URL  # untouched

    async def test_resolve_opens_a_static_row_through_its_own_branch(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        resolved = await pc_static_service.resolve(
            community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
        )
        assert resolved.access_token is None and resolved.kind == 2
        assert resolved.secret_fields == {"token": STATIC_BEARER}
        assert STATIC_BEARER not in repr(resolved)  # fields are excluded from repr

    async def test_delete_purges_the_sealed_secret(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        assert await pc_static_service.delete(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            connection_id=view.binding.connection.id,
        )
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_the_data_plane_list_carries_target_facts_and_never_the_secret(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        await create_static(pc_static_service, pc_db)
        page = await pc_static_service.list_for_dataplane(
            provider="external_http", capability="dispatch", after=0, limit=10
        )
        (item,) = page.items
        assert item.metadata == {"url": STATIC_URL, "secret_scheme": 1}
        assert item.resource_type == "external_http" and item.capabilities == ["dispatch"]
        assert STATIC_BEARER not in repr(item)

    async def test_two_connections_to_the_same_url_are_distinct_rows(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        await create_static(pc_static_service, pc_db)
        await create_static(pc_static_service, pc_db)
        rows = await raw_rows(pc_db, "platform_connections")
        assert len({r["public_id"] for r in rows}) == 2


class TestStaticStore:
    async def test_an_oauth_insert_defaults_to_kind_1_and_reads_it_back(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        assert view.binding.connection.connection_kind == 1
        (row,) = await raw_rows(pc_db, "platform_connections")
        assert row["sealed_secret"] is None and row["connection_kind"] == 1

    async def test_a_resolve_state_loads_the_sealed_secret_for_static_rows_only(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        state = await store.read_resolve_state(
            pc_db.install_dal,
            community_id=pc_db.seed.community_id,
            connection_id=view.binding.connection.id,
        )
        assert (
            state is not None and state.secrets.sealed_secret and state.secrets.access_token is None
        )
        assert state.connection.connection_kind == 2

    async def test_the_database_mirror_rejects_an_out_of_range_kind(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        with pytest.raises(Exception, match="CHECK|constraint"):
            await set_column(
                pc_db, "platform_connections", view.binding.connection.id, connection_kind=9
            )

    async def test_missing_0064_columns_fail_loudly_naming_the_migration(
        self, tmp_path: Any
    ) -> None:
        db = AsyncDB(f"sqlite://{tmp_path / 'no0064.db'}", pool_size=1)
        metadata = build_pc_metadata()
        table = metadata.tables["platform_connections"]
        for constraint in list(table.constraints):  # the shape CHECK names the column we remove
            if isinstance(constraint, CheckConstraint) and "sealed_secret" in str(
                constraint.sqltext
            ):
                table.constraints.discard(constraint)
        table._columns.remove(table.c.sealed_secret)  # noqa: SLF001 - simulate a pre-0064 schema
        async with db.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(
                text("CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id INTEGER)")
            )
        await db.reflect()
        try:
            with pytest.raises(SchemaMissing, match="0064_connection_kinds") as info:
                await store.list_bindings(db, tenant_id=1, community_id=1)
            assert "sealed_secret" in info.value.message
        finally:
            await db.close()


class TestStaticServiceHardening:
    async def test_the_flag_is_enforced_inside_the_service_not_only_the_blueprint(
        self,
        pc_static_service: ProviderConnectionService,
        pc_db: Any,
        entitlements: Any,
    ) -> None:
        from services.provider_connections.errors import FeatureNotEnabled

        entitlements.flag_off("waddles.integrations.static_connections")
        with pytest.raises(FeatureNotEnabled):
            await create_static(pc_static_service, pc_db)
        entitlements.flag_off("waddles.integrations.provider_connections")  # unrelated flag
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_the_free_tier_is_refused_by_the_service(
        self, pc_static_service: ProviderConnectionService, pc_db: Any, entitlements: Any
    ) -> None:
        from services.provider_connections.errors import FeatureNotEnabled

        entitlements.set_tier("free")
        with pytest.raises(FeatureNotEnabled):
            await create_static(pc_static_service, pc_db)

    async def test_the_stored_url_is_the_canonical_form(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        await create_static(
            pc_static_service, pc_db, metadata={"url": "https://OK.Example:443/Path?q=1"}
        )
        (row,) = await raw_rows(pc_db, "platform_connections")
        assert _meta(row)["url"] == "https://ok.example:443/Path?q=1"

    async def test_obfuscated_loopback_is_refused_and_nothing_is_stored(
        self, pc_static_service: ProviderConnectionService, pc_db: Any
    ) -> None:
        for host in ("2130706433", "0x7f.0.0.1", "127.1", "017700000001", "%31%32%37.0.0.1"):
            with pytest.raises(InvalidConnectionInput):
                await create_static(pc_static_service, pc_db, metadata={"url": f"https://{host}/"})
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_create_routes_through_the_adapter_credential_check(
        self, pc_db: Any, entitlements: Any, clock: Any
    ) -> None:
        """The protocol methods are the single source of truth: if one rejects, nothing is stored."""
        from services.provider_connections.adapter import AdapterRegistry
        from services.provider_connections.errors import CredentialsRejected
        from services.provider_connections.static_secret import ExternalHttpAdapter

        class Rejecting(ExternalHttpAdapter):
            async def validate_credentials(self, credentials: Any) -> Any:
                raise CredentialsRejected("adapter said no")

        service = ProviderConnectionService(
            pc_db.install_dal, AdapterRegistry([Rejecting()]), PlatformKeyVault(), clock=clock
        )
        with pytest.raises(CredentialsRejected):
            await create_static(service, pc_db)
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_insert_connection_requires_an_explicit_kind(self, pc_db: Any) -> None:
        import inspect

        param = inspect.signature(store.insert_connection).parameters["connection_kind"]
        assert param.default is inspect.Parameter.empty
