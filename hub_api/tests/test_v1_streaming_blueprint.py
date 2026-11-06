"""`blueprints/v1/streaming.py` + `services/stream_keys_service.py` -- stream key / destination API.

Real DB (file-backed pydal DAL, `streaming_db`), no mocked service layer. Covers the security
contract: plaintext key only on create/rotate, hash-only storage, never on GET / in logs / in any
column, rotation invalidates the old key, destination credentials sealed (and decryptable only
with the row-bound AAD), cross-community 404 / non-admin 403, exact response field sets, and the
HEVC/AV1 rejection.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.streaming as streaming_module
from blueprints.v1.streaming import streaming_bp
from config import HubAPIConfig
from services import stream_keys_service as svc
from services.platform_integrations_crypto import decrypt_value, stream_destination_aad
from tests.conftest import TENANT_SLUG, make_user_token

_ENC_KEY = "11" * 32
_DEST = {
    "platform": "twitch",
    "rtmp_url": "rtmp://8.8.8.8/app",
    "stream_key": "live_SUPERSECRET_twitch_key",
    "max_resolution": "1080p",
}


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app(streaming_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(streaming_bp)
    quart_app.config["dal"] = streaming_db.dal
    quart_app.config["async_dal"] = streaming_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Encryption key + broadcast flag ON + ingest base URL for every test."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _ENC_KEY)
    monkeypatch.setenv("STREAMING_INGEST_RTMP_URL", "rtmp://ingest.example.com/live")
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(streaming_module, "feature_enabled", stub)
    return stub


def _community(db: Any, name: str = "acme-community") -> int:
    tenant = db.dal(db.dal.tenants.slug == TENANT_SLUG).select().first()
    cid: int = db.dal.communities.insert(name=name, tenant_id=int(tenant.id))
    db.dal.commit()
    return cid


def _admin(db: Any, *, community_id: int, user_id: int = 1) -> dict[str, str]:
    role_id = db.dal.community_roles.insert(
        community_id=community_id,
        name="community-admin",
        base_claims={"scopes": ["community:manage_members"]},
    )
    db.dal.community_members.insert(
        community_id=community_id,
        user_id=str(user_id),
        role="community-admin",
        community_role_id=role_id,
        is_active=True,
    )
    db.dal.commit()
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}


def _url(cid: int, tail: str = "") -> str:
    return f"/api/v1/admin/{cid}/streams{tail}"


async def _create(client: Any, cid: int, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post(_url(cid), headers=headers)
    assert response.status_code == 201
    return dict(await response.get_json())


class TestAuth:
    async def test_missing_token_401(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        assert (await client.get(_url(cid))).status_code == 401

    async def test_non_admin_member_403(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        streaming_db.dal.community_members.insert(
            community_id=cid, user_id="1", role="member", is_active=True
        )
        streaming_db.dal.commit()
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        for method, tail in (("get", ""), ("post", ""), ("post", "/key/regenerate")):
            assert (
                await getattr(client, method)(_url(cid, tail), headers=headers)
            ).status_code == 403

    async def test_admin_of_other_community_403(self, client: Any, streaming_db: Any) -> None:
        a, b = _community(streaming_db, "a"), _community(streaming_db, "b")
        headers = _admin(streaming_db, community_id=b, user_id=2)
        assert (await client.get(_url(a), headers=headers)).status_code == 403
        assert (await client.post(_url(a), headers=headers)).status_code == 403
        assert (await client.post(_url(a, "/key/regenerate"), headers=headers)).status_code == 403


class TestKeyLifecycle:
    async def test_get_before_create_is_null_config(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        body = await (await client.get(_url(cid), headers=headers)).get_json()
        assert body == {"success": True, "config": None}

    async def test_create_reveals_key_once_with_exact_fields(
        self, client: Any, streaming_db: Any
    ) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        response = await client.post(_url(cid), headers=headers)
        assert response.status_code == 201
        assert response.headers["Cache-Control"] == "no-store"
        body = await response.get_json()
        assert set(body) == {"success", "config", "streamKey"}
        assert body["streamKey"].startswith("wbsk_") and len(body["streamKey"]) >= 48
        assert set(body["config"]) == {
            "enabled",
            "ingest_url",
            "stream_key_set",
            "stream_key_fingerprint",
            "stream_key_rotated_at",
            "video_codec",
            "audio_codec",
            "created_at",
            "updated_at",
        }
        assert body["config"]["ingest_url"] == "rtmp://ingest.example.com/live"
        assert body["config"]["stream_key_set"] is True

    async def test_key_never_returned_by_get_or_stored_plaintext(
        self, client: Any, streaming_db: Any
    ) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        key = (await _create(client, cid, headers))["streamKey"]
        raw = await (await client.get(_url(cid), headers=headers)).get_json()
        assert key not in str(raw)
        assert "stream_key" not in raw["config"]
        row = (
            streaming_db.dal(streaming_db.dal.streaming_configs.community_id == cid)
            .select()
            .first()
        )
        assert key not in repr(row.as_dict())
        assert row.stream_key_hash == svc.hash_stream_key(key)
        assert row.stream_key_fingerprint == row.stream_key_hash[:8]

    async def test_source_url_is_never_empty_or_the_key(
        self, client: Any, streaming_db: Any
    ) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        key = (await _create(client, cid, headers))["streamKey"]
        row = (
            streaming_db.dal(streaming_db.dal.streaming_configs.community_id == cid)
            .select()
            .first()
        )
        assert row.source_url and row.source_url != key
        assert len(svc.generate_stream_key()) >= svc.MIN_KEY_LEN

    async def test_second_create_is_409(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        await _create(client, cid, headers)
        assert (await client.post(_url(cid), headers=headers)).status_code == 409

    async def test_rotate_invalidates_old_key(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        old = (await _create(client, cid, headers))["streamKey"]
        response = await client.post(_url(cid, "/key/regenerate"), headers=headers)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        new = (await response.get_json())["streamKey"]
        assert new != old
        dal = streaming_db.dal
        # What svc-streaming's ingest-auth does: hash the presented key, match the stored hash.
        match = lambda k: dal(  # noqa: E731
            dal.streaming_configs.stream_key_hash == svc.hash_stream_key(k)
        ).count()
        assert match(old) == 0
        assert match(new) == 1

    async def test_rotate_without_config_404(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        assert (await client.post(_url(cid, "/key/regenerate"), headers=headers)).status_code == 404

    async def test_keys_unique_per_call(self) -> None:
        assert len({svc.generate_stream_key() for _ in range(50)}) == 50

    async def test_key_and_hash_never_logged(
        self, client: Any, streaming_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        key = (await _create(client, cid, headers))["streamKey"]
        key2 = (
            await (await client.post(_url(cid, "/key/regenerate"), headers=headers)).get_json()
        )["streamKey"]
        text = caplog.text
        for secret in (key, key2, svc.hash_stream_key(key), svc.hash_stream_key(key2)):
            assert secret not in text
        assert "stream_key_rotated" in text

    async def test_non_object_body_400(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        response = await client.post(_url(cid), headers=headers, json=["x"])
        assert response.status_code == 400

    async def test_disabled_config_flag(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        response = await client.post(_url(cid), headers=headers, json={"enabled": False})
        assert (await response.get_json())["config"]["enabled"] is False


class TestDestinations:
    async def _setup(self, client: Any, db: Any) -> tuple[int, dict[str, str]]:
        cid = _community(db)
        headers = _admin(db, community_id=cid)
        await _create(client, cid, headers)
        return cid, headers

    async def test_add_returns_redacted_exact_fields(self, client: Any, streaming_db: Any) -> None:
        cid, headers = await self._setup(client, streaming_db)
        response = await client.post(_url(cid, "/destinations"), headers=headers, json=_DEST)
        assert response.status_code == 201
        body = await response.get_json()
        dest = body["destination"]
        assert set(dest) == {
            "id",
            "platform",
            "rtmp_url",
            "max_resolution",
            "video_codec",
            "enabled",
            "force_cut",
            "created_at",
        }
        assert dest["rtmp_url"] == "rtmp://8.8.8.8"
        assert "SUPERSECRET" not in str(body)
        assert dest["force_cut"] is False and dest["video_codec"] == "h264"

    async def test_credentials_sealed_at_rest(self, client: Any, streaming_db: Any) -> None:
        cid, headers = await self._setup(client, streaming_db)
        await client.post(_url(cid, "/destinations"), headers=headers, json=_DEST)
        row = streaming_db.dal(streaming_db.dal.streaming_destinations.id > 0).select().first()
        assert "SUPERSECRET" not in repr(row.as_dict())
        assert "8.8.8.8" not in row.rtmp_url_enc
        aad = stream_destination_aad(config_id=int(row.config_id), public_id=row.public_id)
        assert decrypt_value(row.stream_key_enc, aad=aad) == _DEST["stream_key"]
        assert decrypt_value(row.rtmp_url_enc, aad=aad) == _DEST["rtmp_url"]
        other = stream_destination_aad(config_id=int(row.config_id) + 1, public_id=row.public_id)
        with pytest.raises(Exception, match="decrypt"):
            decrypt_value(row.stream_key_enc, aad=other)

    async def test_secret_not_in_logs_or_list(
        self, client: Any, streaming_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        cid, headers = await self._setup(client, streaming_db)
        await client.post(_url(cid, "/destinations"), headers=headers, json=_DEST)
        listing = await (await client.get(_url(cid, "/destinations"), headers=headers)).get_json()
        assert len(listing["destinations"]) == 1
        assert "SUPERSECRET" not in str(listing) + caplog.text

    async def test_camel_case_aliases_accepted(self, client: Any, streaming_db: Any) -> None:
        cid, headers = await self._setup(client, streaming_db)
        response = await client.post(
            _url(cid, "/destinations"),
            headers=headers,
            json={
                "platform": "YouTube",
                "rtmpUrl": "rtmps://8.8.4.4:443/live2",
                "streamKey": "k",
                "forceCut": True,
                "enabled": False,
            },
        )
        dest = (await response.get_json())["destination"]
        assert dest["platform"] == "youtube"
        assert dest["rtmp_url"] == "rtmps://8.8.4.4:443"
        assert dest["force_cut"] is True and dest["enabled"] is False

    @pytest.mark.parametrize(
        "patch",
        [
            {"platform": "myspace"},
            {"platform": ""},
            {"rtmp_url": "http://8.8.8.8/app"},
            {"rtmp_url": "rtmp://"},
            {"rtmp_url": "x" * 2000},
            {"stream_key": ""},
            {"stream_key": "k" * 501},
            {"stream_key": 5},
            {"max_resolution": "8k"},
            {"video_codec": "vp9"},
        ],
    )
    async def test_invalid_input_400(
        self, client: Any, streaming_db: Any, patch: dict[str, Any]
    ) -> None:
        cid, headers = await self._setup(client, streaming_db)
        response = await client.post(
            _url(cid, "/destinations"), headers=headers, json={**_DEST, **patch}
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("codec", ["h265", "HEVC", "av1"])
    async def test_hevc_av1_rejected_like_svc_streaming(
        self, client: Any, streaming_db: Any, codec: str
    ) -> None:
        cid, headers = await self._setup(client, streaming_db)
        response = await client.post(
            _url(cid, "/destinations"), headers=headers, json={**_DEST, "video_codec": codec}
        )
        assert response.status_code == 400
        assert "HEVC/AV1" in str((await response.get_json())["error"])
        assert streaming_db.dal(streaming_db.dal.streaming_destinations.id > 0).count() == 0

    @pytest.mark.parametrize(
        "url", ["rtmp://127.0.0.1/app", "rtmp://169.254.169.254/a", "rtmp://10.0.0.5/x"]
    )
    async def test_ssrf_blocked(self, client: Any, streaming_db: Any, url: str) -> None:
        cid, headers = await self._setup(client, streaming_db)
        response = await client.post(
            _url(cid, "/destinations"), headers=headers, json={**_DEST, "rtmp_url": url}
        )
        assert response.status_code == 400

    async def test_add_without_config_404(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        response = await client.post(_url(cid, "/destinations"), headers=headers, json=_DEST)
        assert response.status_code == 404

    async def test_cap_enforced(self, client: Any, streaming_db: Any) -> None:
        cid, headers = await self._setup(client, streaming_db)
        for _ in range(svc.MAX_DESTINATIONS_PER_CONFIG):
            r = await client.post(_url(cid, "/destinations"), headers=headers, json=_DEST)
            assert r.status_code == 201
        r = await client.post(_url(cid, "/destinations"), headers=headers, json=_DEST)
        assert r.status_code == 409

    async def test_list_without_config_is_empty(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        body = await (await client.get(_url(cid, "/destinations"), headers=headers)).get_json()
        assert body == {"success": True, "destinations": []}

    async def test_force_cut_toggle_and_explicit(self, client: Any, streaming_db: Any) -> None:
        cid, headers = await self._setup(client, streaming_db)
        added = await (
            await client.post(_url(cid, "/destinations"), headers=headers, json=_DEST)
        ).get_json()
        did = added["destination"]["id"]
        path = _url(cid, f"/destinations/{did}/force-cut")
        toggled = await (await client.put(path, headers=headers)).get_json()
        assert toggled["destination"]["force_cut"] is True
        again = await (await client.put(path, headers=headers)).get_json()
        assert again["destination"]["force_cut"] is False
        explicit = await (
            await client.put(path, headers=headers, json={"forceCut": True})
        ).get_json()
        assert explicit["destination"]["force_cut"] is True
        bad = await client.put(path, headers=headers, json={"force_cut": "yes"})
        assert bad.status_code == 400

    async def test_delete(self, client: Any, streaming_db: Any) -> None:
        cid, headers = await self._setup(client, streaming_db)
        added = await (
            await client.post(_url(cid, "/destinations"), headers=headers, json=_DEST)
        ).get_json()
        did = added["destination"]["id"]
        r = await client.delete(_url(cid, f"/destinations/{did}"), headers=headers)
        assert r.status_code == 200
        assert (await r.get_json()) == {
            "success": True,
            "message": "Destination removed successfully",
        }
        assert (
            await client.delete(_url(cid, f"/destinations/{did}"), headers=headers)
        ).status_code == 404

    async def test_cross_community_destination_is_404(self, client: Any, streaming_db: Any) -> None:
        """Admin of A and B: A's destination id via B's path never resolves (ownership 404)."""
        a, b = _community(streaming_db, "a"), _community(streaming_db, "b")
        headers = _admin(streaming_db, community_id=a)
        # same user is also admin of B
        role_id = streaming_db.dal.community_roles.insert(
            community_id=b,
            name="community-admin",
            base_claims={"scopes": ["community:manage_members"]},
        )
        streaming_db.dal.community_members.insert(
            community_id=b,
            user_id="1",
            role="community-admin",
            community_role_id=role_id,
            is_active=True,
        )
        streaming_db.dal.commit()
        await _create(client, a, headers)
        await _create(client, b, headers)
        added = await (
            await client.post(_url(a, "/destinations"), headers=headers, json=_DEST)
        ).get_json()
        did = added["destination"]["id"]
        assert (
            await client.delete(_url(b, f"/destinations/{did}"), headers=headers)
        ).status_code == 404
        assert (
            await client.put(_url(b, f"/destinations/{did}/force-cut"), headers=headers)
        ).status_code == 404
        listing = await (await client.get(_url(b, "/destinations"), headers=headers)).get_json()
        assert listing["destinations"] == []
        still = await (await client.get(_url(a, "/destinations"), headers=headers)).get_json()
        assert len(still["destinations"]) == 1

    async def test_malformed_destination_id_404(self, client: Any, streaming_db: Any) -> None:
        cid, headers = await self._setup(client, streaming_db)
        assert (
            await client.delete(_url(cid, "/destinations/1"), headers=headers)
        ).status_code == 404

    async def test_destinations_gated_on_broadcast_flag(
        self, client: Any, streaming_db: Any, _env: AsyncMock
    ) -> None:
        cid, headers = await self._setup(client, streaming_db)
        _env.return_value = False
        assert (await client.get(_url(cid, "/destinations"), headers=headers)).status_code == 402
        assert (
            await client.post(_url(cid, "/destinations"), headers=headers, json=_DEST)
        ).status_code == 402
        assert (await client.get(_url(cid), headers=headers)).status_code == 402
        # minting/rotating a key is not forwarding -- stays available on every tier
        assert (await client.post(_url(cid, "/key/regenerate"), headers=headers)).status_code == 200


class TestStatus:
    async def test_offline_without_config(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        body = await (await client.get(_url(cid, "/status"), headers=headers)).get_json()
        assert body["status"] == {
            "is_streaming": False,
            "started_at": None,
            "transcode_applied": False,
            "destinations_enabled": 0,
        }

    async def test_live_from_latest_running_session(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        await _create(client, cid, headers)
        await client.post(_url(cid, "/destinations"), headers=headers, json=_DEST)
        cfg = (
            streaming_db.dal(streaming_db.dal.streaming_configs.community_id == cid)
            .select()
            .first()
        )
        streaming_db.dal.streaming_sessions.insert(
            config_id=cfg.id, status="running", transcode_applied=True
        )
        streaming_db.dal.commit()
        body = await (await client.get(_url(cid, "/status"), headers=headers)).get_json()
        assert body["status"]["is_streaming"] is True
        assert body["status"]["transcode_applied"] is True
        assert body["status"]["destinations_enabled"] == 1
        streaming_db.dal.streaming_sessions.insert(config_id=cfg.id, status="stopped")
        streaming_db.dal.commit()
        body = await (await client.get(_url(cid, "/status"), headers=headers)).get_json()
        assert body["status"]["is_streaming"] is False

    async def test_stopped_session_is_offline(self, client: Any, streaming_db: Any) -> None:
        cid = _community(streaming_db)
        headers = _admin(streaming_db, community_id=cid)
        await _create(client, cid, headers)
        body = await (await client.get(_url(cid, "/status"), headers=headers)).get_json()
        assert body["status"]["is_streaming"] is False


class TestHelpers:
    def test_redact_strips_userinfo_path_query(self) -> None:
        assert (
            svc.redact_rtmp_url("rtmps://user:pw@host.tv:443/app/KEY?token=x")
            == "rtmps://host.tv:443"
        )

    def test_hash_is_sha256_hex(self) -> None:
        assert len(svc.hash_stream_key("a")) == 64

    def test_missing_encryption_key_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
        with pytest.raises(Exception, match="CREDENTIAL_ENCRYPTION_KEY"):
            from services.platform_integrations_crypto import encrypt_token

            encrypt_token("x")
