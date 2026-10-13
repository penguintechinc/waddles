"""DSAR access + erasure reach the mem0/Qdrant chat-memory store (GRC release audit, Art. 15/17).

Qdrant is faked at the HTTP layer (`httpx.MockTransport`) by an in-memory implementation of
the four REST calls the service uses (list collections, scroll, count, delete-by-filter), so the
real `mem0_privacy` request/pagination/filter code runs; the DB side is the real sqlite-backed
DSAR path (`data_privacy_service.collect_user_data` / `anonymize_user_data`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from flask_core.database import AsyncDAL
from pydal import Field

from services import data_privacy_service as privacy
from services import mem0_privacy
from services.mem0_privacy import Mem0PrivacyError, Mem0StoreConfig, mem0_user_key
from services.schema import bind_admin_privacy_tables

SUBJECT_UUID = "11111111-1111-4111-8111-111111111111"
OTHER_UUID = "22222222-2222-4222-8222-222222222222"
NOBODY_UUID = "33333333-3333-4333-8333-333333333333"
SUBJECT_KEY = mem0_user_key(SUBJECT_UUID)
OTHER_KEY = mem0_user_key(OTHER_UUID)
CFG = Mem0StoreConfig(url="http://qdrant.test:6333", api_key="k")


class FakeQdrant:
    """In-memory Qdrant REST subset: `{collection: [point, ...]}` with `payload.user_id`."""

    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        """Seed the fake with `{collection_name: [points]}`."""
        self.collections = collections
        self.fail_with: int | None = None
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _match(point: dict[str, Any], flt: dict[str, Any]) -> bool:
        for cond in flt["must"]:
            if point["payload"].get(cond["key"]) not in cond["match"]["any"]:
                return False
        return True

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"status": {"error": "boom"}})
        if path == "/collections":
            names = [{"name": n} for n in self.collections]
            return httpx.Response(200, json={"result": {"collections": names}})
        match = re.match(r"^/collections/([^/]+)/points/(scroll|count|delete)$", path)
        assert match, path
        name, op = match.groups()
        body = json.loads(request.content)
        points = self.collections[name]
        hits = [p for p in points if self._match(p, body["filter"])]
        if op == "count":
            return httpx.Response(200, json={"result": {"count": len(hits)}})
        if op == "delete":
            self.collections[name] = [p for p in points if p not in hits]
            return httpx.Response(200, json={"result": {"status": "completed"}})
        assert body["with_vector"] is False  # vectors are never exported
        start = int(body.get("offset") or 0)
        page = hits[start : start + body["limit"]]
        nxt = start + body["limit"] if start + body["limit"] < len(hits) else None
        return httpx.Response(200, json={"result": {"points": page, "next_page_offset": nxt}})


def _point(pid: str, owner: str, text: str, **meta: Any) -> dict[str, Any]:
    payload = {
        "user_id": owner,
        "data": text,
        "hash": "h",
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": None,
        **meta,
    }
    return {"id": pid, "payload": payload, "vector": [0.1, 0.2]}


@pytest.fixture
def qdrant(monkeypatch: pytest.MonkeyPatch) -> FakeQdrant:
    fake = FakeQdrant(
        {
            "community_7": [
                _point("a", SUBJECT_KEY, "likes pineapple pizza", topic="food"),
                _point("b", OTHER_KEY, "other user memory"),
                _point("c", "community_7", "community-wide memory"),
            ],
            "community_9": [_point("d", SUBJECT_KEY, "second community memory")],
            "unrelated_collection": [_point("e", SUBJECT_KEY, "not a mem0 community collection")],
        }
    )
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(
        mem0_privacy,
        "_client",
        lambda config: httpx.AsyncClient(base_url=config.url, transport=transport),
    )
    monkeypatch.setenv("QDRANT_URL", CFG.url)
    return fake


class TestKeying:
    def test_user_key_is_built_from_a_uuid(self) -> None:
        assert mem0_user_key(SUBJECT_UUID.upper()) == f"user_{SUBJECT_UUID}"

    @pytest.mark.parametrize("raw", ["alice", "alice@example.com", "12345", "", "user_alice"])
    def test_raw_pii_is_never_a_key(self, raw: str) -> None:
        with pytest.raises(ValueError):
            mem0_user_key(raw)


class TestExport:
    async def test_exports_all_of_the_subjects_memories_across_communities(
        self, qdrant: FakeQdrant
    ) -> None:
        rows = await mem0_privacy.export_user_memories([SUBJECT_UUID])
        assert sorted((r["community_id"], r["memory_id"]) for r in rows) == [(7, "a"), (9, "d")]
        first = next(r for r in rows if r["memory_id"] == "a")
        assert first == {
            "community_id": 7,
            "memory_id": "a",
            "memory": "likes pineapple pizza",
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": None,
            "metadata": {"topic": "food"},
        }

    async def test_never_exports_other_users_or_community_wide_or_foreign_collections(
        self, qdrant: FakeQdrant
    ) -> None:
        rows = await mem0_privacy.export_user_memories([SUBJECT_UUID])
        assert {r["memory_id"] for r in rows}.isdisjoint({"b", "c", "e"})

    async def test_tenant_scoping_limits_the_collections_read(self, qdrant: FakeQdrant) -> None:
        rows = await mem0_privacy.export_user_memories([SUBJECT_UUID], community_ids={9})
        assert [r["memory_id"] for r in rows] == ["d"]
        assert not any("community_7" in path for _m, path in qdrant.calls)

    async def test_pagination_returns_every_point(
        self, qdrant: FakeQdrant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mem0_privacy, "_SCROLL_PAGE", 2)
        qdrant.collections["community_7"] = [
            _point(f"p{i}", SUBJECT_KEY, f"m{i}") for i in range(5)
        ]
        rows = await mem0_privacy.export_user_memories([SUBJECT_UUID], community_ids={7})
        assert sorted(r["memory_id"] for r in rows) == [f"p{i}" for i in range(5)]

    async def test_unknown_user_exports_nothing(self, qdrant: FakeQdrant) -> None:
        assert await mem0_privacy.export_user_memories([NOBODY_UUID]) == []

    async def test_no_tokens_means_no_qdrant_call(self, qdrant: FakeQdrant) -> None:
        assert await mem0_privacy.export_user_memories([]) == []
        assert await mem0_privacy.export_user_memories(["not-a-uuid"]) == []
        assert qdrant.calls == []

    async def test_qdrant_failure_is_loud_and_carries_no_payload(self, qdrant: FakeQdrant) -> None:
        qdrant.fail_with = 500
        with pytest.raises(Mem0PrivacyError) as err:
            await mem0_privacy.export_user_memories([SUBJECT_UUID])
        assert "boom" not in str(err.value)

    async def test_unconfigured_store_exports_nothing(
        self, qdrant: FakeQdrant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QDRANT_URL")
        assert await mem0_privacy.export_user_memories([SUBJECT_UUID]) == []
        assert qdrant.calls == []


class TestErase:
    async def test_removes_all_of_the_subjects_points_in_every_community(
        self, qdrant: FakeQdrant
    ) -> None:
        result = await mem0_privacy.erase_user_memories([SUBJECT_UUID])
        assert result == mem0_privacy.Mem0EraseResult(configured=True, deleted=2)
        assert [p["id"] for p in qdrant.collections["community_7"]] == ["b", "c"]
        assert qdrant.collections["community_9"] == []

    async def test_leaves_other_users_community_wide_and_foreign_collections(
        self, qdrant: FakeQdrant
    ) -> None:
        await mem0_privacy.erase_user_memories([SUBJECT_UUID])
        assert [p["id"] for p in qdrant.collections["unrelated_collection"]] == ["e"]
        assert any(p["payload"]["user_id"] == OTHER_KEY for p in qdrant.collections["community_7"])

    async def test_erasure_then_export_is_empty(self, qdrant: FakeQdrant) -> None:
        await mem0_privacy.erase_user_memories([SUBJECT_UUID])
        assert await mem0_privacy.export_user_memories([SUBJECT_UUID]) == []

    async def test_nonexistent_user_is_a_clean_zero(self, qdrant: FakeQdrant) -> None:
        result = await mem0_privacy.erase_user_memories([NOBODY_UUID])
        assert result == mem0_privacy.Mem0EraseResult(configured=True, deleted=0)
        assert not any(path.endswith("/delete") for _m, path in qdrant.calls)

    async def test_is_idempotent(self, qdrant: FakeQdrant) -> None:
        await mem0_privacy.erase_user_memories([SUBJECT_UUID])
        again = await mem0_privacy.erase_user_memories([SUBJECT_UUID])
        assert again.deleted == 0

    async def test_qdrant_failure_raises(self, qdrant: FakeQdrant) -> None:
        qdrant.fail_with = 503
        with pytest.raises(Mem0PrivacyError):
            await mem0_privacy.erase_user_memories([SUBJECT_UUID])

    async def test_unconfigured_store_reports_not_configured(
        self, qdrant: FakeQdrant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QDRANT_URL")
        result = await mem0_privacy.erase_user_memories([SUBJECT_UUID])
        assert result == mem0_privacy.Mem0EraseResult(configured=False, deleted=0)


@pytest.fixture(scope="module")
def module_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    path = tmp_path_factory.mktemp("mem0priv") / "mem0priv.db"
    async_dal = AsyncDAL(f"sqlite://{path}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=True,
    )
    bind_admin_privacy_tables(dal, migrate=True)
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    dal.commit()
    yield async_dal
    dal.close()


@pytest.fixture
def db(module_db: Any) -> Any:
    dal = module_db.dal
    for table_name in dal.tables:
        if table_name != "tenants":
            dal(dal[table_name]).delete()
    dal.commit()
    return module_db


def _subject(db: Any, *, uuid: str = SUBJECT_UUID, name: str = "mem0user") -> int:
    now = datetime.now(UTC)
    user_id = db.dal.hub_users.insert(
        email=f"{name}@example.com",
        username=name,
        password_hash=None,
        is_active=True,
        uuid=uuid,
        created_at=now,
        updated_at=now,
    )
    db.dal.commit()
    return int(user_id)


def _ledger(db: Any, user_id: int) -> Any:
    return db.dal(db.dal.data_deletion_requests.hub_user_id == user_id).select().first()


class TestDsarFlow:
    """The shared DSAR core (self-service AND the tenant-admin console) reaches mem0."""

    async def test_access_export_includes_mem0_memories(self, db: Any, qdrant: FakeQdrant) -> None:
        user_id = _subject(db)
        data, failures = await privacy.collect_user_data(db, db.dal, user_id=user_id)
        assert failures == []
        assert sorted(r["memory_id"] for r in data["mem0_memories"]) == ["a", "d"]

    async def test_admin_export_is_limited_to_the_tenants_communities(
        self, db: Any, qdrant: FakeQdrant
    ) -> None:
        user_id = _subject(db)
        data, _ = await privacy.collect_user_data(db, db.dal, user_id=user_id, community_ids={9})
        assert [r["memory_id"] for r in data["mem0_memories"]] == ["d"]

    async def test_export_for_a_user_without_memories_is_empty_not_failed(
        self, db: Any, qdrant: FakeQdrant
    ) -> None:
        user_id = _subject(db, uuid=NOBODY_UUID, name="nomem")
        data, failures = await privacy.collect_user_data(db, db.dal, user_id=user_id)
        assert failures == []
        assert data["mem0_memories"] == []

    async def test_export_reports_a_qdrant_outage_as_incomplete(
        self, db: Any, qdrant: FakeQdrant
    ) -> None:
        user_id = _subject(db)
        qdrant.fail_with = 500
        data, failures = await privacy.collect_user_data(db, db.dal, user_id=user_id)
        assert "mem0_memories" not in data
        assert [f["source"] for f in failures] == ["mem0_memories"]

    async def test_erasure_purges_mem0_and_records_it_in_the_ledger(
        self, db: Any, qdrant: FakeQdrant
    ) -> None:
        user_id = _subject(db)
        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email="mem0user@example.com")
        assert [p["id"] for p in qdrant.collections["community_7"]] == ["b", "c"]
        assert qdrant.collections["community_9"] == []
        ledger = _ledger(db, user_id)
        assert ledger.status == "completed"
        assert ledger.deletion_scope["mem0_memories"] == 2

    async def test_erasure_of_a_user_with_no_memories_completes(
        self, db: Any, qdrant: FakeQdrant
    ) -> None:
        user_id = _subject(db, uuid=NOBODY_UUID, name="nomem")
        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email="nomem@example.com")
        ledger = _ledger(db, user_id)
        assert ledger.status == "completed"
        assert ledger.deletion_scope["mem0_memories"] == 0
        assert len(qdrant.collections["community_7"]) == 3

    async def test_qdrant_failure_aborts_erasure_leaving_the_account_whole(
        self, db: Any, qdrant: FakeQdrant
    ) -> None:
        user_id = _subject(db)
        qdrant.fail_with = 500
        with pytest.raises(Mem0PrivacyError):
            await privacy.anonymize_user_data(
                db, db.dal, user_id=user_id, email="mem0user@example.com"
            )
        row = db.dal(db.dal.hub_users.id == user_id).select().first()
        assert row.email == "mem0user@example.com"
        assert _ledger(db, user_id).status == "failed"

    async def test_erasure_without_qdrant_configured_still_completes(
        self, db: Any, qdrant: FakeQdrant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QDRANT_URL")
        user_id = _subject(db)
        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email="mem0user@example.com")
        ledger = _ledger(db, user_id)
        assert ledger.status == "completed"
        assert "mem0_memories" not in ledger.deletion_scope
        assert len(qdrant.collections["community_7"]) == 3
