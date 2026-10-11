"""Overlay mint/rotate write-through to `overlay_view_credentials` (svc-presentation's table).

Regression for: hub-api's overlay mint wrote ONLY `community_overlay_tokens`, but svc-presentation
validates a presented `?key=` as `SHA-256(key) == overlay_view_credentials.key_hash`, so every key
the UI/API handed out was rejected. These tests run the real blueprint + real pydal queries and
re-implement svc-presentation's check (`core/overlay_auth/src/view.rs::validate_view_token`) in
`_svc_presentation_accepts` so a divergence between what hub-api stores and what the data plane
compares fails here, not in production.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.overlay as overlay_module
from blueprints.v1.overlay import SCOPE_ADMIN, overlay_bp
from services import overlay_service
from tests.conftest import TENANT_SLUG, make_user_token, seed_community, seed_membership
from tests.test_v1_overlay_blueprint import _test_config

#: `overlay_auth::view::ROTATION_GRACE_PERIOD` -- the previous key stays valid this long.
GRACE = timedelta(minutes=5)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _svc_presentation_accepts(cred: Any, presented: str, *, now: datetime | None = None) -> bool:
    """Python mirror of `overlay_auth::view::validate_view_token` over a credentials row."""
    if not presented or cred is None or not cred.is_active:
        return False
    now = now or datetime.now(UTC)
    presented_hash = _sha256_hex(presented)
    if presented_hash == cred.key_hash:
        return True
    if cred.previous_key_hash and cred.rotated_at is not None:
        rotated_at = cred.rotated_at
        if rotated_at.tzinfo is None:
            rotated_at = rotated_at.replace(tzinfo=UTC)
        return presented_hash == cred.previous_key_hash and now <= rotated_at + GRACE
    return False


def _creds(overlay_db: Any, community_id: int) -> Any:
    dal = overlay_db.dal
    row = dal(dal.overlay_view_credentials.community_id == community_id).select().first()
    dal.commit()
    return row


def _token_row(overlay_db: Any, community_id: int) -> Any:
    dal = overlay_db.dal
    row = dal(dal.community_overlay_tokens.community_id == community_id).select().first()
    dal.commit()
    return row


@pytest.fixture
def app(overlay_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(overlay_bp)
    quart_app.config["dal"] = overlay_db.dal
    quart_app.config["async_dal"] = overlay_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(overlay_module, "feature_enabled", stub)
    return stub


@pytest.fixture
def owner(overlay_db: Any) -> tuple[dict[str, str], int]:
    """Seed a community + owner; returns (auth headers, community_id)."""
    community_id = seed_community(overlay_db)
    seed_membership(overlay_db, community_id=community_id, user_id=501)
    token = make_user_token(user_id=501, scope=SCOPE_ADMIN, tenant=TENANT_SLUG)
    return {"Authorization": f"Bearer {token}"}, community_id


class TestMintWritesThrough:
    async def test_mint_writes_both_tables_with_matching_hash(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        response = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        assert response.status_code == 200
        key = (await response.get_json())["overlay"]["overlay_key"]

        legacy = _token_row(overlay_db, community_id)
        cred = _creds(overlay_db, community_id)
        assert legacy is not None and legacy.overlay_key == key
        assert cred is not None
        assert cred.key_hash == _sha256_hex(key)
        assert cred.previous_key_hash is None
        assert cred.is_active is True

    async def test_minted_key_validates_the_way_svc_presentation_checks_it(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        """regression: the key handed out was dead -- svc-presentation rejected it."""
        headers, community_id = owner
        response = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        key = (await response.get_json())["overlay"]["overlay_key"]

        cred = _creds(overlay_db, community_id)
        assert _svc_presentation_accepts(cred, key)
        assert not _svc_presentation_accepts(cred, key[:-1] + ("0" if key[-1] != "0" else "1"))
        assert not _svc_presentation_accepts(cred, "")

    async def test_repeat_get_is_idempotent_one_credential_row(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        first = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        second = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        key = (await first.get_json())["overlay"]["overlay_key"]
        assert (await second.get_json())["overlay"]["overlay_key"] == key
        dal = overlay_db.dal
        assert dal(dal.overlay_view_credentials.community_id == community_id).count() == 1
        assert _creds(overlay_db, community_id).key_hash == _sha256_hex(key)

    async def test_response_never_leaks_key_hash(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        response = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        body = await response.get_json()
        key = body["overlay"]["overlay_key"]
        raw = await response.get_data(as_text=True)
        assert _sha256_hex(key) not in raw
        assert "key_hash" not in raw and "previous_key_hash" not in raw
        assert set(body["overlay"]) == {
            "id",
            "community_id",
            "overlay_key",
            "previous_key",
            "is_active",
            "theme_config",
            "enabled_sources",
            "last_accessed",
            "access_count",
            "created_at",
            "updated_at",
            "rotated_at",
            "overlayUrl",
        }

    async def test_legacy_token_without_credentials_is_healed_on_get(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        """A token minted before write-through existed gets its hash row on next access."""
        headers, community_id = owner
        legacy_key = "ab" * 32
        now = datetime.now(UTC)
        dal = overlay_db.dal
        dal.community_overlay_tokens.insert(
            community_id=community_id,
            overlay_key=legacy_key,
            is_active=True,
            enabled_sources=["chat"],
            theme_config={},
            access_count=0,
            created_at=now,
            updated_at=now,
        )
        dal.commit()
        assert _creds(overlay_db, community_id) is None

        response = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        assert (await response.get_json())["overlay"]["overlay_key"] == legacy_key
        assert _creds(overlay_db, community_id).key_hash == _sha256_hex(legacy_key)

    async def test_mint_is_atomic_when_credential_write_fails(
        self, overlay_db: Any, owner: tuple[dict[str, str], int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the credentials write blows up, the token row must not persist either."""
        _, community_id = owner

        def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("credentials write failed")

        monkeypatch.setattr(overlay_service, "_upsert_credentials", _boom)
        with pytest.raises(RuntimeError, match="credentials write failed"):
            await overlay_service.get_or_create_overlay(
                overlay_db, overlay_db.dal, _test_config(), community_id=community_id
            )
        assert _token_row(overlay_db, community_id) is None
        assert _creds(overlay_db, community_id) is None


class TestRotateWritesThrough:
    async def test_rotate_updates_both_tables_consistently(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        created = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        old_key = (await created.get_json())["overlay"]["overlay_key"]

        rotated = await client.post(f"/api/v1/admin/{community_id}/overlay/rotate", headers=headers)
        assert rotated.status_code == 200
        new_key = (await rotated.get_json())["overlay"]["overlay_key"]
        assert new_key != old_key

        legacy = _token_row(overlay_db, community_id)
        cred = _creds(overlay_db, community_id)
        assert legacy.overlay_key == new_key and legacy.previous_key == old_key
        assert cred.key_hash == _sha256_hex(new_key)
        assert cred.previous_key_hash == _sha256_hex(old_key)
        assert cred.rotated_at is not None

    async def test_rotated_keys_follow_svc_presentation_grace_window(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        created = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        old_key = (await created.get_json())["overlay"]["overlay_key"]
        rotated = await client.post(f"/api/v1/admin/{community_id}/overlay/rotate", headers=headers)
        new_key = (await rotated.get_json())["overlay"]["overlay_key"]

        cred = _creds(overlay_db, community_id)
        assert _svc_presentation_accepts(cred, new_key)
        assert _svc_presentation_accepts(cred, old_key)  # inside the 5-minute grace window
        later = datetime.now(UTC) + GRACE + timedelta(seconds=30)
        assert _svc_presentation_accepts(cred, new_key, now=later)
        assert not _svc_presentation_accepts(cred, old_key, now=later)

    async def test_double_rotate_drops_the_oldest_key(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        first = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        key1 = (await first.get_json())["overlay"]["overlay_key"]
        second = await client.post(f"/api/v1/admin/{community_id}/overlay/rotate", headers=headers)
        key2 = (await second.get_json())["overlay"]["overlay_key"]
        third = await client.post(f"/api/v1/admin/{community_id}/overlay/rotate", headers=headers)
        key3 = (await third.get_json())["overlay"]["overlay_key"]

        cred = _creds(overlay_db, community_id)
        assert cred.key_hash == _sha256_hex(key3)
        assert cred.previous_key_hash == _sha256_hex(key2)
        assert not _svc_presentation_accepts(cred, key1)

    async def test_rotate_response_never_leaks_hashes(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        rotated = await client.post(f"/api/v1/admin/{community_id}/overlay/rotate", headers=headers)
        raw = await rotated.get_data(as_text=True)
        cred = _creds(overlay_db, community_id)
        assert cred.key_hash not in raw
        assert "key_hash" not in raw

    async def test_rotate_is_atomic_when_credential_write_fails(
        self,
        client: Any,
        overlay_db: Any,
        owner: tuple[dict[str, str], int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        headers, community_id = owner
        created = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        old_key = (await created.get_json())["overlay"]["overlay_key"]

        def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("credentials write failed")

        monkeypatch.setattr(overlay_service, "_upsert_credentials", _boom)
        with pytest.raises(RuntimeError, match="credentials write failed"):
            await overlay_service.rotate_overlay_key(
                overlay_db, overlay_db.dal, _test_config(), community_id=community_id
            )
        assert _token_row(overlay_db, community_id).overlay_key == old_key
        assert _creds(overlay_db, community_id).key_hash == _sha256_hex(old_key)


class TestDeactivateFollowsThrough:
    async def test_disabling_overlay_makes_svc_presentation_reject_the_key(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        created = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        key = (await created.get_json())["overlay"]["overlay_key"]
        assert _svc_presentation_accepts(_creds(overlay_db, community_id), key)

        response = await client.put(
            f"/api/v1/admin/{community_id}/overlay", headers=headers, json={"isActive": False}
        )
        assert response.status_code == 200
        assert not _svc_presentation_accepts(_creds(overlay_db, community_id), key)

        await client.put(
            f"/api/v1/admin/{community_id}/overlay", headers=headers, json={"isActive": True}
        )
        assert _svc_presentation_accepts(_creds(overlay_db, community_id), key)


def _add_overlay_code_column(overlay_db: Any) -> None:
    """Emulate alembic 0057's column on the sqlite test DB (nullable so a NULL case is testable)."""
    overlay_db.dal.executesql("ALTER TABLE communities ADD COLUMN overlay_code VARCHAR(16)")
    overlay_db.dal.commit()


def _overlay_code(overlay_db: Any, community_id: int) -> Any:
    rows = overlay_db.dal.executesql(
        f"SELECT overlay_code FROM communities WHERE id = {community_id}"  # noqa: S608
    )
    overlay_db.dal.commit()
    return rows[0][0]


class TestOverlayCode:
    async def test_null_overlay_code_is_generated_on_mint(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        _add_overlay_code_column(overlay_db)
        assert _overlay_code(overlay_db, community_id) is None

        response = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        assert response.status_code == 200
        code = _overlay_code(overlay_db, community_id)
        assert code is not None and len(code) == 16
        assert all(ch in "0123456789abcdef" for ch in code)

    async def test_existing_overlay_code_is_left_alone(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        _add_overlay_code_column(overlay_db)
        overlay_db.dal.executesql(
            f"UPDATE communities SET overlay_code = 'a1b2c3d4e5f60718' WHERE id = {community_id}"  # noqa: S608
        )
        overlay_db.dal.commit()

        await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        await client.post(f"/api/v1/admin/{community_id}/overlay/rotate", headers=headers)
        assert _overlay_code(overlay_db, community_id) == "a1b2c3d4e5f60718"

    async def test_rotate_also_backfills_a_null_overlay_code(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        headers, community_id = owner
        await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        _add_overlay_code_column(overlay_db)
        await client.post(f"/api/v1/admin/{community_id}/overlay/rotate", headers=headers)
        assert len(_overlay_code(overlay_db, community_id)) == 16

    async def test_missing_overlay_code_column_does_not_break_mint(
        self, client: Any, overlay_db: Any, owner: tuple[dict[str, str], int]
    ) -> None:
        """Pre-0057 database: column absent -- mint still works and credentials are written."""
        headers, community_id = owner  # note: column deliberately NOT added
        response = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        assert response.status_code == 200
        key = (await response.get_json())["overlay"]["overlay_key"]
        assert _creds(overlay_db, community_id).key_hash == _sha256_hex(key)
