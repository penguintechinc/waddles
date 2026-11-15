"""`GET /api/v1/communities/<id>/music` -- member-only music page, replica read, flag-gated.

Standalone Quart app over the `music_station_db` fixture (real JWTs, real authz chain).
The bundle's db-v2 physical tables are created in the same sqlite file under the names
`derive_v2_table_identity` yields; sqlite has no schemas, so `_qualified_table` is patched
to the bare table name. The DJ activation lookup is patched except in the direct
`dj_bundle_active` test, which uses the real `install_dal` fixture.
"""

# ruff: noqa: S608
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.community_music as music_module
from blueprints.v1.community_music import community_music_bp
from services import community_music_service as svc
from services.bundle_data_v2 import derive_v2_table_identity
from tests.conftest import TENANT_SLUG, make_user_token

REQUESTER_UUID = "11111111-2222-3333-4444-555555555555"
ROOT_KEYS = {"now_playing", "queue", "history", "ai_dj", "dmca_safe", "top_artists", "top_songs"}


def _table(logical: str) -> str:
    return derive_v2_table_identity(svc.MUSIC_REQUESTS_APP_ID, logical, provider="builtin").table


class RecordingDal:
    """Replica stand-in: records `executesql` calls and delegates to the real DAL."""

    def __init__(self, real: Any) -> None:
        """Wrap `real` and start the call counter at zero."""
        self._real = real
        self.calls = 0

    def executesql(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self._real.executesql(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Delegate every other attribute (e.g. `_dbname`, `commit`) to the real DAL."""
        return getattr(self._real, name)


@pytest.fixture(autouse=True)
def _patches(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(svc, "_qualified_table", lambda logical: f'"{_table(logical)}"')
    flag = AsyncMock(return_value=True)
    monkeypatch.setattr(music_module, "feature_enabled", flag)
    dj = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "dj_bundle_active", dj)
    return {"flag": flag, "dj": dj}


@pytest.fixture
def app(music_station_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(community_music_bp)
    quart_app.config["dal"] = music_station_db.dal
    quart_app.config["async_dal"] = music_station_db
    quart_app.config["install_dal"] = object()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _create_tables(db: Any) -> None:
    ex = db.dal.executesql
    ex(
        f'CREATE TABLE "{_table("requests")}" (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        "tenant_id INT, community_id INT, title TEXT, artist TEXT, source_url TEXT, "
        "duration_ms INT, requester TEXT, kind INT, status INT, queued_at TEXT, "
        "started_at TEXT, provider TEXT)"
    )
    ex(
        f'CREATE TABLE "{_table("history")}" (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        "tenant_id INT, community_id INT, title TEXT, artist TEXT, source_url TEXT, "
        "duration_ms INT, played_at TEXT, requester TEXT, kind INT, provider TEXT)"
    )
    ex(
        f'CREATE TABLE "{_table("settings")}" (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        "tenant_id INT, community_id INT, skey TEXT, ival INT)"
    )
    ex(
        f'CREATE TABLE "{_table("artist_plays")}" (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        "tenant_id INT, community_id INT, artist_key TEXT, artist TEXT, plays INT)"
    )
    ex(
        f'CREATE TABLE "{_table("song_plays")}" (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        "tenant_id INT, community_id INT, song_key TEXT, title TEXT, artist TEXT, plays INT)"
    )
    db.dal.commit()


def _req(db: Any, t: int, c: int, title: str, status: int, at: str, kind: int = 1) -> None:
    db.dal.executesql(
        f'INSERT INTO "{_table("requests")}" (tenant_id, community_id, title, artist, '
        "source_url, duration_ms, requester, kind, status, queued_at, started_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        placeholders=[
            t,
            c,
            title,
            "Art",
            "https://x.example/" + title,
            1000,
            REQUESTER_UUID,
            kind,
            status,
            at,
            at,
        ],
    )
    db.dal.commit()


def _hist(db: Any, t: int, c: int, title: str, at: str) -> None:
    db.dal.executesql(
        f'INSERT INTO "{_table("history")}" (tenant_id, community_id, title, artist, '
        "source_url, duration_ms, played_at, requester, kind) VALUES (?,?,?,?,?,?,?,?,?)",
        placeholders=[t, c, title, "Art", "", 1000, at, REQUESTER_UUID, 1],
    )
    db.dal.commit()


def _setting(db: Any, t: int, c: int, key: str, value: int) -> None:
    db.dal.executesql(
        f'INSERT INTO "{_table("settings")}" (tenant_id, community_id, skey, ival) '
        "VALUES (?,?,?,?)",
        placeholders=[t, c, key, value],
    )
    db.dal.commit()


def _artist_plays(db: Any, t: int, c: int, artist: str, plays: int) -> None:
    db.dal.executesql(
        f'INSERT INTO "{_table("artist_plays")}" (tenant_id, community_id, artist_key, '
        "artist, plays) VALUES (?,?,?,?,?)",
        placeholders=[t, c, artist.lower(), artist, plays],
    )
    db.dal.commit()


def _song_plays(db: Any, t: int, c: int, title: str, artist: str, plays: int) -> None:
    db.dal.executesql(
        f'INSERT INTO "{_table("song_plays")}" (tenant_id, community_id, song_key, title, '
        "artist, plays) VALUES (?,?,?,?,?,?)",
        placeholders=[t, c, title.lower(), title, artist, plays],
    )
    db.dal.commit()


def _tenant(db: Any) -> int:
    return int(db.dal(db.dal.tenants.slug == TENANT_SLUG).select().first().id)


def _community(db: Any, tenant_id: int | None = None) -> int:
    cid: int = db.dal.communities.insert(
        name=f"c{db.dal(db.dal.communities).count()}", tenant_id=tenant_id or _tenant(db)
    )
    db.dal.commit()
    return cid


def _member(db: Any, community_id: int, user_id: int = 2) -> dict[str, str]:
    db.dal.community_members.insert(
        community_id=community_id, user_id=str(user_id), role="member", is_active=True
    )
    db.dal.commit()
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}


async def _get(client: Any, community_id: int, headers: dict[str, str]) -> Any:
    return await client.get(f"/api/v1/communities/{community_id}/music", headers=headers)


class TestMusicPage:
    async def test_happy_path(self, client: Any, music_station_db: Any) -> None:
        db = music_station_db
        _create_tables(db)
        t, c = _tenant(db), _community(db)
        headers = _member(db, c)
        _req(db, t, c, "now", 2, "2026-01-01T00:00:00")
        _req(db, t, c, "q2", 1, "2026-01-01T00:02:00", kind=2)
        _req(db, t, c, "q1", 1, "2026-01-01T00:01:00")
        for i in range(7):
            _hist(db, t, c, f"h{i}", f"2026-01-0{i + 1}T00:00:00")
        resp = await _get(client, c, headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert set(body) == ROOT_KEYS
        assert body["now_playing"]["title"] == "now"
        assert [q["title"] for q in body["queue"]] == ["q1", "q2"]
        assert body["queue"][1]["kind"] == "dj"
        assert set(body["queue"][0]) == {"title", "artist", "source_url", "duration_ms", "kind"}
        assert [h["title"] for h in body["history"]] == ["h6", "h5", "h4", "h3", "h2"]
        assert set(body["history"][0]) == {
            "title",
            "artist",
            "source_url",
            "duration_ms",
            "played_at",
        }
        assert body["ai_dj"] == {"enabled": False}
        assert body["dmca_safe"] is False
        assert body["top_artists"] == [] and body["top_songs"] == []

    async def test_queue_capped_at_50(self, client: Any, music_station_db: Any) -> None:
        db = music_station_db
        _create_tables(db)
        t, c = _tenant(db), _community(db)
        headers = _member(db, c)
        for i in range(60):
            _req(db, t, c, f"q{i:02d}", 1, f"2026-01-01T00:{i:02d}:00")
        body = await (await _get(client, c, headers)).get_json()
        assert len(body["queue"]) == 50

    async def test_no_requester_leak(self, client: Any, music_station_db: Any) -> None:
        db = music_station_db
        _create_tables(db)
        t, c = _tenant(db), _community(db)
        headers = _member(db, c)
        _req(db, t, c, "now", 2, "2026-01-01T00:00:00")
        _req(db, t, c, "q", 1, "2026-01-01T00:01:00")
        _hist(db, t, c, "h", "2026-01-01T00:00:00")
        _artist_plays(db, t, c, "Art", 3)
        _song_plays(db, t, c, "Song", "Art", 3)
        resp = await _get(client, c, headers)
        raw = (await resp.get_data()).decode()
        assert REQUESTER_UUID not in raw
        assert "requester" not in raw
        parsed = json.loads(raw)
        assert parsed["queue"] and parsed["top_artists"] and parsed["top_songs"]
        assert set(parsed["top_artists"][0]) == {"artist", "plays"}
        assert set(parsed["top_songs"][0]) == {"title", "artist", "plays"}

    async def test_non_member_refused(self, client: Any, music_station_db: Any) -> None:
        db = music_station_db
        _create_tables(db)
        c = _community(db)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=99)}"}
        assert (await _get(client, c, headers)).status_code == 403

    async def test_unauthenticated_refused(self, client: Any, music_station_db: Any) -> None:
        _create_tables(music_station_db)
        c = _community(music_station_db)
        resp = await client.get(f"/api/v1/communities/{c}/music")
        assert resp.status_code in (401, 403)

    async def test_cross_community_and_tenant_isolated(
        self, client: Any, music_station_db: Any
    ) -> None:
        db = music_station_db
        _create_tables(db)
        t, a, b = _tenant(db), _community(db), _community(db)
        other_t = int(db.dal.tenants.insert(slug="other-tenant", is_active=True))
        db.dal.commit()
        foreign = _community(db, tenant_id=other_t)
        headers = _member(db, a)
        _req(db, t, a, "mine", 1, "2026-01-01T00:00:00")
        _req(db, t, b, "other-community", 1, "2026-01-01T00:00:00")
        _req(db, other_t, a, "other-tenant-same-cid", 1, "2026-01-01T00:00:00")
        _hist(db, t, b, "other-hist", "2026-01-01T00:00:00")
        body = await (await _get(client, a, headers)).get_json()
        assert [q["title"] for q in body["queue"]] == ["mine"]
        assert body["history"] == []
        # member of A asking for B (same tenant) and a foreign-tenant community: refused.
        assert (await _get(client, b, headers)).status_code == 403
        _member(db, foreign)
        assert (await _get(client, foreign, headers)).status_code == 403

    async def test_flag_off_is_404(
        self, client: Any, music_station_db: Any, _patches: dict[str, Any]
    ) -> None:
        db = music_station_db
        _create_tables(db)
        c = _community(db)
        headers = _member(db, c)
        _patches["flag"].return_value = False
        assert (await _get(client, c, headers)).status_code == 404
        assert _patches["flag"].await_args.args[0] == "waddles.music.queue-page"
        assert _patches["flag"].await_args.kwargs["default"] is False

    async def test_missing_tables_empty(self, client: Any, music_station_db: Any) -> None:
        db = music_station_db
        c = _community(db)
        headers = _member(db, c)
        resp = await _get(client, c, headers)
        assert resp.status_code == 200
        assert await resp.get_json() == {
            "now_playing": None,
            "queue": [],
            "history": [],
            "ai_dj": {"enabled": False},
            "dmca_safe": False,
            "top_artists": [],
            "top_songs": [],
        }

    async def test_replica_is_used(self, client: Any, music_station_db: Any) -> None:
        db = music_station_db
        _create_tables(db)
        t, c = _tenant(db), _community(db)
        headers = _member(db, c)
        _req(db, t, c, "q", 1, "2026-01-01T00:00:00")
        replica = RecordingDal(db.dal)
        db.read_dal = replica
        try:
            body = await (await _get(client, c, headers)).get_json()
        finally:
            db.read_dal = None
        # playing, queued, history, settings, artist_plays, song_plays
        assert replica.calls == 6
        assert [q["title"] for q in body["queue"]] == ["q"]


class TestTopLists:
    async def test_top5_ordered_and_community_scoped(
        self, client: Any, music_station_db: Any
    ) -> None:
        db = music_station_db
        _create_tables(db)
        t, a, b = _tenant(db), _community(db), _community(db)
        headers = _member(db, a)
        for i in range(7):
            _artist_plays(db, t, a, f"A{i}", i + 1)
            _song_plays(db, t, a, f"S{i}", f"A{i}", i + 1)
        _artist_plays(db, t, b, "OtherCommunityArtist", 999)
        _song_plays(db, t, b, "OtherCommunitySong", "X", 999)
        body = await (await _get(client, a, headers)).get_json()
        assert [x["artist"] for x in body["top_artists"]] == ["A6", "A5", "A4", "A3", "A2"]
        assert body["top_artists"][0] == {"artist": "A6", "plays": 7}
        assert body["top_songs"][0] == {"title": "S6", "artist": "A6", "plays": 7}
        assert len(body["top_songs"]) == 5
        assert "OtherCommunity" not in json.dumps(body)

    async def test_other_tenant_rows_excluded(self, client: Any, music_station_db: Any) -> None:
        db = music_station_db
        _create_tables(db)
        t, c = _tenant(db), _community(db)
        headers = _member(db, c)
        _artist_plays(db, t + 1000, c, "ForeignTenant", 50)
        body = await (await _get(client, c, headers)).get_json()
        assert body["top_artists"] == []

    async def test_missing_top_tables_empty(self, client: Any, music_station_db: Any) -> None:
        db = music_station_db
        _create_tables(db)
        db.dal.executesql(f'DROP TABLE "{_table("artist_plays")}"')
        db.dal.executesql(f'DROP TABLE "{_table("song_plays")}"')
        db.dal.commit()
        t, c = _tenant(db), _community(db)
        headers = _member(db, c)
        _req(db, t, c, "q", 1, "2026-01-01T00:00:00")
        resp = await _get(client, c, headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["top_artists"] == [] and body["top_songs"] == []
        assert len(body["queue"]) == 1


class TestDmcaSafe:
    @pytest.mark.parametrize(("value", "expected"), [(1, True), (0, False)])
    async def test_dmca_safe_setting(
        self, client: Any, music_station_db: Any, value: int, expected: bool
    ) -> None:
        db = music_station_db
        _create_tables(db)
        t, c = _tenant(db), _community(db)
        headers = _member(db, c)
        _setting(db, t, c, "dmca_safe", value)
        body = await (await _get(client, c, headers)).get_json()
        assert body["dmca_safe"] is expected
        assert body["ai_dj"] == {"enabled": False}  # independent of ai_dj_on

    async def test_dmca_safe_is_community_scoped(self, client: Any, music_station_db: Any) -> None:
        db = music_station_db
        _create_tables(db)
        t, a, b = _tenant(db), _community(db), _community(db)
        headers = _member(db, a)
        _setting(db, t, b, "dmca_safe", 1)
        assert (await (await _get(client, a, headers)).get_json())["dmca_safe"] is False


class TestAiDj:
    async def _ai_dj(
        self,
        client: Any,
        db: Any,
        setting: int | None,
        dj_active: bool,
        patches: dict[str, Any],
    ) -> dict[str, Any]:
        _create_tables(db)
        t, c = _tenant(db), _community(db)
        headers = _member(db, c)
        if setting is not None:
            _setting(db, t, c, "ai_dj_on", setting)
        patches["dj"].return_value = dj_active
        resp = await _get(client, c, headers)
        assert resp.status_code == 200
        return dict((await resp.get_json())["ai_dj"])

    async def test_on_when_setting_and_bundle(
        self, client: Any, music_station_db: Any, _patches: dict[str, Any]
    ) -> None:
        assert await self._ai_dj(client, music_station_db, 1, True, _patches) == {"enabled": True}

    async def test_off_when_setting_zero(
        self, client: Any, music_station_db: Any, _patches: dict[str, Any]
    ) -> None:
        assert await self._ai_dj(client, music_station_db, 0, True, _patches) == {"enabled": False}

    async def test_off_when_bundle_inactive(
        self, client: Any, music_station_db: Any, _patches: dict[str, Any]
    ) -> None:
        assert await self._ai_dj(client, music_station_db, 1, False, _patches) == {"enabled": False}

    async def test_off_when_no_setting_row(
        self, client: Any, music_station_db: Any, _patches: dict[str, Any]
    ) -> None:
        assert await self._ai_dj(client, music_station_db, None, True, _patches) == {
            "enabled": False
        }


class TestDjBundleActive:
    async def test_activation_lookup(
        self, install_dal: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.undo()  # real function, not the autouse AsyncMock
        from services.bundle_install_dal import raw_sql_write

        async def active(tenant: int, community: int) -> bool:
            return await svc.dj_bundle_active(install_dal, tenant_id=tenant, community_id=community)

        assert not await active(1, 7)
        await raw_sql_write(
            install_dal,
            "INSERT INTO app_active_versions (app_id, tenant_id, community_id, version_id)"
            " VALUES (:a, 1, 7, 1)",
            {"a": svc.DJ_APP_ID},
        )
        assert await active(1, 7)
        assert not await active(1, 8)  # other community
        assert not await active(2, 7)  # other tenant


def test_unsafe_identifier_refused() -> None:
    with pytest.raises(ValueError):
        svc._quote('x"; DROP TABLE y; --')


def test_contract_app_id_derives() -> None:
    """The real service app_id must be derivable, or its tables can never be provisioned."""
    derive_v2_table_identity("waddles.core.service.music-requests", "requests", provider="builtin")
