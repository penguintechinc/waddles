"""`services/community_connections.py` -- direct service-layer tests (gh-320 chunk C1).

Own fixture (`connections_db`, this file only -- `hub_api/services/
community_connections.py`, `hub_api/services/platform_integrations_crypto.py`,
and this test file are the only files this task may edit): real `AsyncDAL`
(file-backed sqlite, `pool_size=1`) + real `bind_platform_tables()`,
exercising the actual production binding code path rather than a
hand-duplicated `Field` list -- same rationale as
`test_services_community_loyalty.py`'s `loyalty_db` fixture. Unlike that
fixture, no `tenants`/`communities` rows are seeded: `platform_integrations.
community_id` is a plain (non-FK) integer column, so an arbitrary int
community id round-trips fine without a real tenant/community chain.

Also covers `platform_integrations_crypto.encrypt_token()` (gh-320's new
write path on that module) round-tripping against its own pre-existing
`decrypt_value()`, mirroring `test_platform_integrations_crypto.py`'s own
key-fixture pattern.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from flask_core.database import AsyncDAL

from services import community_connections as svc
from services.errors import ApiError
from services.platform_integrations_crypto import (
    PlatformCredentialCryptoError,
    connection_aad,
    decrypt_value,
    encrypt_token,
)
from services.provider_connections.vault import open_secret
from services.schema import bind_platform_tables

# Fixed test-only AES key, not a real credential.
_KEY = "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"  # gitleaks:allow

COMMUNITY_ID = 101


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)


@pytest.fixture
def connections_db(tmp_path: Any) -> Any:
    """`AsyncDAL` (file-backed sqlite) with `platform_integrations` (+ M3 group) bound."""
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'community_connections_test.db'}", pool_size=1)
    dal = async_dal.dal
    bind_platform_tables(dal, migrate=True)
    yield async_dal
    dal.close()


def _row(dal: Any, community_id: int, provider: str) -> Any:
    t = dal.platform_integrations
    return dal((t.community_id == community_id) & (t.platform == provider)).select().first()


class TestListConnections:
    async def test_no_rows_returns_all_disconnected(self, connections_db: Any) -> None:
        result = await svc.list_connections(connections_db, COMMUNITY_ID)
        assert [c.provider for c in result] == list(svc.SUPPORTED_PROVIDERS)
        assert all(c.connected is False for c in result)
        assert all(c.scopes == [] for c in result)
        assert all(c.expires_at is None and c.updated_at is None for c in result)

    async def test_invalid_community_id_raises(self, connections_db: Any) -> None:
        with pytest.raises(ApiError):
            await svc.list_connections(connections_db, 0)

    async def test_connected_provider_reflected_others_stay_disconnected(
        self, connections_db: Any
    ) -> None:
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "twitch",
            access_token="tok-abc",
            refresh_token="ref-abc",
            expires_at=None,
            scopes=["chat:read"],
            account_label="mychannel",
            actor_user_id=7,
        )
        result = await svc.list_connections(connections_db, COMMUNITY_ID)
        by_provider = {c.provider: c for c in result}
        assert by_provider["twitch"].connected is True
        assert by_provider["twitch"].scopes == ["chat:read"]
        assert by_provider["twitch"].account_label == "mychannel"
        assert by_provider["discord"].connected is False


class TestGetConnection:
    async def test_returns_none_when_absent(self, connections_db: Any) -> None:
        assert await svc.get_connection(connections_db, COMMUNITY_ID, "discord") is None

    async def test_unsupported_provider_raises(self, connections_db: Any) -> None:
        with pytest.raises(svc.UnsupportedProvider):
            await svc.get_connection(connections_db, COMMUNITY_ID, "myspace")

    async def test_invalid_community_id_raises(self, connections_db: Any) -> None:
        with pytest.raises(ApiError):
            await svc.get_connection(connections_db, -1, "discord")

    async def test_returns_status_after_upsert(self, connections_db: Any) -> None:
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "youtube",
            access_token="tok",
            refresh_token=None,
            expires_at=None,
            scopes=[],
            account_label=None,
            actor_user_id=None,
        )
        status = await svc.get_connection(connections_db, COMMUNITY_ID, "youtube")
        assert status is not None
        assert status.connected is True
        assert status.account_label is None
        assert status.to_dict()["provider"] == "youtube"


class TestUpsertConnection:
    async def test_insert_then_update_keeps_single_row(self, connections_db: Any) -> None:
        dal = connections_db.dal
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "slack",
            access_token="tok1",
            refresh_token="ref1",
            expires_at=None,
            scopes=["chat:write"],
            account_label="workspace-a",
            actor_user_id=1,
        )
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "slack",
            access_token="tok2",
            refresh_token="ref2",
            expires_at=None,
            scopes=["chat:write", "channels:read"],
            account_label="workspace-b",
            actor_user_id=2,
        )
        t = dal.platform_integrations
        rows = dal((t.community_id == COMMUNITY_ID) & (t.platform == "slack")).select()
        assert len(rows) == 1

        status = await svc.get_connection(connections_db, COMMUNITY_ID, "slack")
        assert status is not None
        assert status.scopes == ["chat:write", "channels:read"]
        assert status.account_label == "workspace-b"

        tokens = await svc.get_decrypted_tokens(connections_db, COMMUNITY_ID, "slack")
        assert tokens is not None
        assert tokens.access_token == "tok2"
        assert tokens.refresh_token == "ref2"

    async def test_stores_ciphertext_not_plaintext(self, connections_db: Any) -> None:
        dal = connections_db.dal
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "kick",
            access_token="super-secret-token",
            refresh_token=None,
            expires_at=None,
            scopes=[],
            account_label=None,
            actor_user_id=None,
        )
        row = _row(dal, COMMUNITY_ID, "kick")
        assert row.access_token != "super-secret-token"
        assert row.is_encrypted is True

    async def test_blank_access_token_rejected(self, connections_db: Any) -> None:
        with pytest.raises(ApiError):
            await svc.upsert_connection(
                connections_db,
                COMMUNITY_ID,
                "kick",
                access_token="   ",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                account_label=None,
                actor_user_id=None,
            )

    async def test_unsupported_provider_raises(self, connections_db: Any) -> None:
        with pytest.raises(svc.UnsupportedProvider):
            await svc.upsert_connection(
                connections_db,
                COMMUNITY_ID,
                "myspace",
                access_token="tok",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                account_label=None,
                actor_user_id=None,
            )

    async def test_invalid_community_id_raises(self, connections_db: Any) -> None:
        with pytest.raises(ApiError):
            await svc.upsert_connection(
                connections_db,
                0,
                "discord",
                access_token="tok",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                account_label=None,
                actor_user_id=None,
            )


class TestDeleteConnection:
    async def test_delete_existing_returns_true_and_clears_tokens(
        self, connections_db: Any
    ) -> None:
        dal = connections_db.dal
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "discord",
            access_token="tok",
            refresh_token="ref",
            expires_at=None,
            scopes=["bot"],
            account_label=None,
            actor_user_id=None,
        )
        deleted = await svc.delete_connection(connections_db, COMMUNITY_ID, "discord", 9)
        assert deleted is True
        assert await svc.get_connection(connections_db, COMMUNITY_ID, "discord") is None

        row = _row(dal, COMMUNITY_ID, "discord")
        assert row.access_token is None
        assert row.refresh_token is None
        assert row.is_active is False

    async def test_delete_nonexistent_returns_false(self, connections_db: Any) -> None:
        deleted = await svc.delete_connection(connections_db, COMMUNITY_ID, "spotify", None)
        assert deleted is False

    async def test_unsupported_provider_raises(self, connections_db: Any) -> None:
        with pytest.raises(svc.UnsupportedProvider):
            await svc.delete_connection(connections_db, COMMUNITY_ID, "myspace", None)


class TestGetDecryptedTokens:
    async def test_round_trip(self, connections_db: Any) -> None:
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "youtube",
            access_token="real-access",
            refresh_token="real-refresh",
            expires_at=None,
            scopes=["youtube.readonly"],
            account_label=None,
            actor_user_id=None,
        )
        tokens = await svc.get_decrypted_tokens(connections_db, COMMUNITY_ID, "youtube")
        assert tokens is not None
        assert tokens.access_token == "real-access"
        assert tokens.refresh_token == "real-refresh"
        assert tokens.scopes == ["youtube.readonly"]

    async def test_none_when_not_connected(self, connections_db: Any) -> None:
        assert await svc.get_decrypted_tokens(connections_db, COMMUNITY_ID, "twitch") is None

    async def test_no_refresh_token_returns_none_for_it(self, connections_db: Any) -> None:
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "spotify",
            access_token="tok",
            refresh_token=None,
            expires_at=None,
            scopes=[],
            account_label=None,
            actor_user_id=None,
        )
        tokens = await svc.get_decrypted_tokens(connections_db, COMMUNITY_ID, "spotify")
        assert tokens is not None
        assert tokens.refresh_token is None

    async def test_tampered_ciphertext_raises(self, connections_db: Any) -> None:
        dal = connections_db.dal
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "spotify",
            access_token="tok",
            refresh_token=None,
            expires_at=None,
            scopes=[],
            account_label=None,
            actor_user_id=None,
        )
        row = _row(dal, COMMUNITY_ID, "spotify")
        tampered = row.access_token[:-4] + ("AAAA" if row.access_token[-4:] != "AAAA" else "BBBB")
        dal(dal.platform_integrations.id == row.id).update(access_token=tampered)
        dal.commit()
        with pytest.raises(PlatformCredentialCryptoError):
            await svc.get_decrypted_tokens(connections_db, COMMUNITY_ID, "spotify")


class TestStoreRefreshedAccessToken:
    async def test_updates_existing_access_token_and_expiry(self, connections_db: Any) -> None:
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "twitch",
            access_token="old",
            refresh_token="ref",
            expires_at=None,
            scopes=[],
            account_label=None,
            actor_user_id=None,
        )
        new_expiry = datetime.now(UTC) + timedelta(hours=1)
        await svc.store_refreshed_access_token(
            connections_db, COMMUNITY_ID, "twitch", access_token="new", expires_at=new_expiry
        )
        tokens = await svc.get_decrypted_tokens(connections_db, COMMUNITY_ID, "twitch")
        assert tokens is not None
        assert tokens.access_token == "new"
        assert tokens.refresh_token == "ref"
        assert tokens.expires_at is not None

    async def test_raises_not_found_when_no_active_connection(self, connections_db: Any) -> None:
        with pytest.raises(ApiError):
            await svc.store_refreshed_access_token(
                connections_db, COMMUNITY_ID, "kick", access_token="new", expires_at=None
            )

    async def test_unsupported_provider_raises(self, connections_db: Any) -> None:
        with pytest.raises(svc.UnsupportedProvider):
            await svc.store_refreshed_access_token(
                connections_db, COMMUNITY_ID, "myspace", access_token="new", expires_at=None
            )

    async def test_blank_access_token_rejected(self, connections_db: Any) -> None:
        with pytest.raises(ApiError):
            await svc.store_refreshed_access_token(
                connections_db, COMMUNITY_ID, "twitch", access_token="  ", expires_at=None
            )


class TestAccountLabelFallback:
    async def test_non_dict_config_data_yields_none_account_label(
        self, connections_db: Any
    ) -> None:
        dal = connections_db.dal
        await svc.upsert_connection(
            connections_db,
            COMMUNITY_ID,
            "discord",
            access_token="tok",
            refresh_token=None,
            expires_at=None,
            scopes=[],
            account_label="will-be-cleared",
            actor_user_id=None,
        )
        row = _row(dal, COMMUNITY_ID, "discord")
        dal(dal.platform_integrations.id == row.id).update(config_data=None)
        dal.commit()

        status = await svc.get_connection(connections_db, COMMUNITY_ID, "discord")
        assert status is not None
        assert status.account_label is None


class TestEncryptTokenRoundTrip:
    """`platform_integrations_crypto.encrypt_token()` -- gh-320's new write path."""

    def test_round_trip(self) -> None:
        ciphertext = encrypt_token("plaintext-secret")
        assert ciphertext != "plaintext-secret"
        assert decrypt_value(ciphertext) == "plaintext-secret"

    def test_wrong_key_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ciphertext = encrypt_token("plaintext-secret")
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "00" * 32)
        with pytest.raises(PlatformCredentialCryptoError):
            decrypt_value(ciphertext)

    def test_tampered_ciphertext_fails(self) -> None:
        ciphertext = encrypt_token("plaintext-secret")
        tampered = ciphertext[:-4] + ("AAAA" if ciphertext[-4:] != "AAAA" else "BBBB")
        with pytest.raises(PlatformCredentialCryptoError):
            decrypt_value(tampered)


class TestRowBoundSealing:
    """regression: M4 -- `platform_integrations` ciphertext is bound to `community_id:provider`."""

    async def _put(self, db: Any, community_id: int, provider: str, tag: str) -> None:
        await svc.upsert_connection(
            db,
            community_id,
            provider,
            access_token=f"acc-{tag}",
            refresh_token=f"ref-{tag}",
            expires_at=None,
            scopes=["s"],
            account_label=None,
            actor_user_id=None,
        )

    async def test_new_writes_are_version_1(self, connections_db: Any) -> None:
        await self._put(connections_db, COMMUNITY_ID, "twitch", "w")
        row = _row(connections_db.dal, COMMUNITY_ID, "twitch")
        assert row.seal_version == 1
        aad = connection_aad(community_id=COMMUNITY_ID, provider="twitch")
        assert open_secret(row.access_token, aad=aad, seal_version=1) == "acc-w"
        assert open_secret(row.refresh_token, aad=aad, seal_version=1) == "ref-w"
        with pytest.raises(PlatformCredentialCryptoError):
            decrypt_value(row.access_token)  # not openable as a no-AAD (legacy) ciphertext

    async def test_ciphertext_swapped_between_rows_fails_open(self, connections_db: Any) -> None:
        await self._put(connections_db, 1, "twitch", "one")
        await self._put(connections_db, 2, "twitch", "two")
        dal = connections_db.dal
        row_one, row_two = _row(dal, 1, "twitch"), _row(dal, 2, "twitch")
        t = dal.platform_integrations
        dal(t.id == row_one.id).update(access_token=row_two.access_token)
        dal(t.id == row_two.id).update(access_token=row_one.access_token)
        dal.commit()
        for community in (1, 2):
            with pytest.raises(PlatformCredentialCryptoError):
                await svc.get_decrypted_tokens(connections_db, community, "twitch")

    async def test_legacy_v0_row_still_opens_then_reseals_on_read(
        self, connections_db: Any
    ) -> None:
        await self._put(connections_db, COMMUNITY_ID, "twitch", "x")
        dal = connections_db.dal
        t = dal.platform_integrations
        dal(t.community_id == COMMUNITY_ID).update(
            access_token=encrypt_token("acc-legacy"),
            refresh_token=encrypt_token("ref-legacy"),
            seal_version=0,
        )
        dal.commit()

        tokens = await svc.get_decrypted_tokens(connections_db, COMMUNITY_ID, "twitch")
        assert tokens is not None
        assert (tokens.access_token, tokens.refresh_token) == ("acc-legacy", "ref-legacy")
        row = _row(dal, COMMUNITY_ID, "twitch")
        assert row.seal_version == 1
        aad = connection_aad(community_id=COMMUNITY_ID, provider="twitch")
        assert open_secret(row.access_token, aad=aad, seal_version=1) == "acc-legacy"
        again = await svc.get_decrypted_tokens(connections_db, COMMUNITY_ID, "twitch")
        assert again is not None and again.refresh_token == "ref-legacy"

    async def test_refresh_of_a_legacy_row_keeps_the_refresh_token_readable(
        self, connections_db: Any
    ) -> None:
        await self._put(connections_db, COMMUNITY_ID, "twitch", "x")
        dal = connections_db.dal
        t = dal.platform_integrations
        dal(t.community_id == COMMUNITY_ID).update(
            access_token=encrypt_token("acc-legacy"),
            refresh_token=encrypt_token("ref-legacy"),
            seal_version=0,
        )
        dal.commit()
        await svc.store_refreshed_access_token(
            connections_db, COMMUNITY_ID, "twitch", access_token="acc-new", expires_at=None
        )
        tokens = await svc.get_decrypted_tokens(connections_db, COMMUNITY_ID, "twitch")
        assert tokens is not None
        assert (tokens.access_token, tokens.refresh_token) == ("acc-new", "ref-legacy")
        assert _row(dal, COMMUNITY_ID, "twitch").seal_version == 1

    async def test_a_failed_lazy_reseal_never_breaks_the_read(
        self, connections_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._put(connections_db, COMMUNITY_ID, "twitch", "x")
        dal = connections_db.dal
        t = dal.platform_integrations
        dal(t.community_id == COMMUNITY_ID).update(
            access_token=encrypt_token("acc-legacy"), refresh_token=None, seal_version=0
        )
        dal.commit()

        def boom(*_a: Any, **_k: Any) -> bool:
            raise RuntimeError("lock timeout")

        monkeypatch.setattr(svc, "_sync_reseal_legacy", boom)
        tokens = await svc.get_decrypted_tokens(connections_db, COMMUNITY_ID, "twitch")
        assert tokens is not None and tokens.access_token == "acc-legacy"
        assert _row(dal, COMMUNITY_ID, "twitch").seal_version == 0  # left for the reseal job
