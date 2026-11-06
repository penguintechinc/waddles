# ruff: noqa: F811
"""AI-knowledge source authz + token-at-rest tests (IDOR, list scope, plaintext PAT).

Runs against the real pydal schema (`bind_bot_tables`, sqlite) and the real
`bot_ai_knowledge` service -- only the outbound crawl is stubbed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from typing import Any

import pytest

os.environ.setdefault("RCON_ENCRYPTION_KEY", "ab" * 32)

from blueprints.v1 import bot as bot_blueprint  # noqa: E402
from services import bot_ai_knowledge  # noqa: E402
from services.github_sync_service import decrypt_token  # noqa: E402
from tests.test_bot_blueprint import (  # noqa: E402,F401 - fixtures
    app,
    client,
    community_id,
    dal,
    other_tenant_community_id,
)

PLAINTEXT = "ghp_PLAINTEXT_SENTINEL_0123456789"
WRITE = "bot.ai_knowledge:write"
READ = "bot.ai_knowledge:read"
BASE = "/api/v1/admin/ai-knowledge/sources"
SOURCE_FIELDS = {
    "id", "community_id", "vendor_id", "module_id", "source_name", "source_type", "source_url",
    "branch", "docs_path", "refresh_interval", "is_active", "last_indexed_at",
    "indexed_page_count", "index_errors", "created_at", "updated_at",
}  # fmt: skip


@pytest.fixture(autouse=True)
def _key(monkeypatch: Any) -> None:
    monkeypatch.setenv("GITHUB_SYNC_ENCRYPTION_KEY", "cd" * 32)


@pytest.fixture
def users(dal: Any) -> dict[str, int]:
    """Real hub_users rows: a vendor, another vendor, and a super admin."""
    ids = {}
    for name, sa in (("vendor", False), ("other_vendor", False), ("root", True)):
        ids[name] = dal.hub_users.insert(display_name=name, is_super_admin=sa)
    dal.commit()
    return ids


def _src(dal: Any, **kw: Any) -> int:
    kw.setdefault("source_name", "S")
    kw.setdefault("source_type", "manual")
    sid: int = dal.ai_knowledge_sources.insert(is_active=True, **kw)
    dal.commit()
    return sid


def _name(dal: Any, sid: int) -> str:
    dal.commit()
    return str(dal.ai_knowledge_sources[sid].source_name)


class TestCrossCommunityRefused:
    async def test_update_other_tenant_404_and_unchanged(
        self, client: Any, auth_headers: Any, dal: Any, other_tenant_community_id: int
    ) -> None:
        sid = _src(dal, community_id=other_tenant_community_id, source_name="victim")
        r = await client.put(
            f"{BASE}/{sid}", headers=auth_headers(scope=WRITE), json={"source_name": "pwned"}
        )
        assert r.status_code == 404
        assert _name(dal, sid) == "victim"

    async def test_delete_other_tenant_404_and_row_kept(
        self, client: Any, auth_headers: Any, dal: Any, other_tenant_community_id: int
    ) -> None:
        sid = _src(dal, community_id=other_tenant_community_id)
        r = await client.delete(f"{BASE}/{sid}", headers=auth_headers(scope=WRITE))
        assert r.status_code == 404
        assert dal.ai_knowledge_sources[sid] is not None

    async def test_reindex_other_tenant_404_and_no_task(
        self,
        client: Any,
        auth_headers: Any,
        dal: Any,
        other_tenant_community_id: int,
        monkeypatch: Any,
    ) -> None:
        sid = _src(dal, community_id=other_tenant_community_id)
        called: list[int] = []

        async def fake(_d: Any, s: int) -> None:
            called.append(s)

        monkeypatch.setattr(bot_ai_knowledge, "index_source", fake)
        r = await client.post(f"{BASE}/{sid}/reindex", headers=auth_headers(scope=WRITE))
        await asyncio.sleep(0)
        assert r.status_code == 404
        assert called == []

    async def test_foreign_and_missing_are_indistinguishable(
        self, client: Any, auth_headers: Any, dal: Any, other_tenant_community_id: int
    ) -> None:
        sid = _src(dal, community_id=other_tenant_community_id)
        foreign = await client.delete(f"{BASE}/{sid}", headers=auth_headers(scope=WRITE))
        missing = await client.delete(f"{BASE}/999999", headers=auth_headers(scope=WRITE))
        assert foreign.status_code == missing.status_code == 404
        assert await foreign.get_json() == await missing.get_json()

    async def test_same_tenant_allowed(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int
    ) -> None:
        sid = _src(dal, community_id=community_id, source_name="mine")
        r = await client.put(
            f"{BASE}/{sid}", headers=auth_headers(scope=WRITE), json={"source_name": "renamed"}
        )
        assert r.status_code == 200
        assert _name(dal, sid) == "renamed"
        r = await client.delete(f"{BASE}/{sid}", headers=auth_headers(scope=WRITE))
        assert r.status_code == 200


class TestVendorAndGlobalScope:
    async def test_vendor_source_other_vendor_404(
        self, client: Any, auth_headers: Any, dal: Any, users: dict[str, int]
    ) -> None:
        sid = _src(dal, vendor_id=users["vendor"], source_name="v")
        r = await client.put(
            f"{BASE}/{sid}",
            headers=auth_headers(scope=WRITE, user_id=str(users["other_vendor"])),
            json={"source_name": "x"},
        )
        assert r.status_code == 404
        assert _name(dal, sid) == "v"

    async def test_vendor_source_owner_ok(
        self, client: Any, auth_headers: Any, dal: Any, users: dict[str, int]
    ) -> None:
        sid = _src(dal, vendor_id=users["vendor"])
        r = await client.put(
            f"{BASE}/{sid}",
            headers=auth_headers(scope=WRITE, user_id=str(users["vendor"])),
            json={"source_name": "x"},
        )
        assert r.status_code == 200

    async def test_global_source_non_superadmin_404_superadmin_ok(
        self, client: Any, auth_headers: Any, dal: Any, users: dict[str, int]
    ) -> None:
        sid = _src(dal)
        r = await client.delete(
            f"{BASE}/{sid}", headers=auth_headers(scope=WRITE, user_id=str(users["vendor"]))
        )
        assert r.status_code == 404
        r = await client.delete(
            f"{BASE}/{sid}", headers=auth_headers(scope=WRITE, user_id=str(users["root"]))
        )
        assert r.status_code == 200

    async def test_unidentified_caller_never_matches_vendor_zero(
        self, client: Any, auth_headers: Any, dal: Any
    ) -> None:
        sid = _src(dal, vendor_id=0)  # `_user_id()` == 0 for non-numeric sub
        r = await client.delete(f"{BASE}/{sid}", headers=auth_headers(scope=WRITE))
        assert r.status_code == 404


class TestListScope:
    async def test_missing_community_refused_for_non_superadmin(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int
    ) -> None:
        _src(dal, community_id=community_id)
        r = await client.get(BASE, headers=auth_headers(scope=READ))
        assert r.status_code == 400

    async def test_other_tenant_community_refused(
        self, client: Any, auth_headers: Any, other_tenant_community_id: int
    ) -> None:
        r = await client.get(
            f"{BASE}?communityId={other_tenant_community_id}", headers=auth_headers(scope=READ)
        )
        assert r.status_code == 403

    async def test_scoped_list_only_that_community(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int,
        other_tenant_community_id: int,
    ) -> None:  # fmt: skip
        _src(dal, community_id=community_id, source_name="mine")
        _src(dal, community_id=other_tenant_community_id, source_name="theirs")
        for q in (f"communityId={community_id}", f"community_id={community_id}"):
            r = await client.get(f"{BASE}?{q}", headers=auth_headers(scope=READ))
            assert r.status_code == 200
            assert [s["source_name"] for s in (await r.get_json())["sources"]] == ["mine"]

    async def test_superadmin_may_list_unfiltered(
        self, client: Any, auth_headers: Any, dal: Any, users: dict[str, int],
        community_id: int, other_tenant_community_id: int,
    ) -> None:  # fmt: skip
        _src(dal, community_id=community_id)
        _src(dal, community_id=other_tenant_community_id)
        r = await client.get(BASE, headers=auth_headers(scope=READ, user_id=str(users["root"])))
        assert r.status_code == 200
        assert len((await r.get_json())["sources"]) == 2


class TestCreateScope:
    async def test_other_tenant_community_403(
        self, client: Any, auth_headers: Any, other_tenant_community_id: int
    ) -> None:
        r = await client.post(
            BASE,
            headers=auth_headers(scope=WRITE),
            json={
                "source_name": "x",
                "source_type": "manual",
                "community_id": int(other_tenant_community_id),
            },
        )
        assert r.status_code == 403

    async def test_global_create_non_superadmin_403(self, client: Any, auth_headers: Any) -> None:
        r = await client.post(
            BASE,
            headers=auth_headers(scope=WRITE),
            json={"source_name": "x", "source_type": "manual"},
        )
        assert r.status_code == 403

    async def test_spoofed_vendor_id_403(
        self, client: Any, auth_headers: Any, users: dict[str, int], community_id: int
    ) -> None:
        r = await client.post(
            BASE,
            headers=auth_headers(scope=WRITE, user_id=str(users["other_vendor"])),
            json={
                "source_name": "x",
                "source_type": "manual",
                "community_id": int(community_id),
                "vendor_id": int(users["vendor"]),
            },
        )
        assert r.status_code == 403


class TestTokenAtRest:
    async def _create(
        self, client: Any, auth_headers: Any, community_id: int, **extra: Any
    ) -> tuple[Any, Any]:
        r = await client.post(
            BASE,
            headers=auth_headers(scope=WRITE),
            json={
                "source_name": "Repo",
                "source_type": "manual",
                "community_id": int(community_id),
                **extra,
            },
        )
        return r, await r.get_json()

    @pytest.mark.parametrize("field", ["token", "encrypted_token"])
    async def test_create_stores_ciphertext_and_never_returns_token(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int, field: str,
        caplog: Any,
    ) -> None:  # fmt: skip
        caplog.set_level(logging.DEBUG)
        r, body = await self._create(client, auth_headers, community_id, **{field: PLAINTEXT})
        assert r.status_code == 201
        stored = dal.ai_knowledge_sources[body["source"]["id"]].encrypted_token
        assert stored and stored != PLAINTEXT and PLAINTEXT not in stored
        assert decrypt_token(stored) == PLAINTEXT
        assert PLAINTEXT not in str(body) and "token" not in str(body).lower()
        assert PLAINTEXT not in caplog.text

    async def test_response_field_set_is_exact(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        _, body = await self._create(client, auth_headers, community_id, token=PLAINTEXT)
        assert set(body) == {"success", "source"}
        assert set(body["source"]) == SOURCE_FIELDS

    async def test_list_response_field_set_is_exact(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int
    ) -> None:
        _src(dal, community_id=community_id, encrypted_token="x")
        r = await client.get(f"{BASE}?communityId={community_id}", headers=auth_headers(scope=READ))
        body = await r.get_json()
        assert set(body) == {"success", "sources"}
        assert set(body["sources"][0]) == SOURCE_FIELDS

    async def test_update_token_encrypts_and_empty_keeps(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int
    ) -> None:
        sid = _src(dal, community_id=community_id)
        r = await client.put(
            f"{BASE}/{sid}", headers=auth_headers(scope=WRITE), json={"token": PLAINTEXT}
        )
        assert r.status_code == 200 and PLAINTEXT not in str(await r.get_json())
        dal.commit()
        first = dal.ai_knowledge_sources[sid].encrypted_token
        assert first != PLAINTEXT and decrypt_token(first) == PLAINTEXT
        r = await client.put(
            f"{BASE}/{sid}",
            headers=auth_headers(scope=WRITE),
            json={"token": "", "source_name": "n"},
        )
        assert r.status_code == 200
        dal.commit()
        assert dal.ai_knowledge_sources[sid].encrypted_token == first

    async def test_update_deprecated_alias_encrypts(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int
    ) -> None:
        sid = _src(dal, community_id=community_id)
        r = await client.put(
            f"{BASE}/{sid}", headers=auth_headers(scope=WRITE), json={"encrypted_token": PLAINTEXT}
        )
        assert r.status_code == 200
        dal.commit()
        stored = dal.ai_knowledge_sources[sid].encrypted_token
        assert stored != PLAINTEXT and decrypt_token(stored) == PLAINTEXT

    async def test_missing_key_is_500_and_nothing_stored(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("GITHUB_SYNC_ENCRYPTION_KEY")
        r, body = await self._create(client, auth_headers, community_id, token=PLAINTEXT)
        assert r.status_code == 500
        assert PLAINTEXT not in str(body)
        assert dal(dal.ai_knowledge_sources.id > 0).count() == 0

    def test_payload_repr_hides_token(self) -> None:
        p = bot_ai_knowledge.KnowledgeSourceCreate(
            source_name="n", source_type="manual", token=PLAINTEXT
        )
        assert PLAINTEXT not in repr(p)
        assert "token" in {f.name for f in dataclasses.fields(p)}

    async def test_index_source_decrypts_for_fetch(
        self, dal: Any, community_id: int, monkeypatch: Any
    ) -> None:
        sealed = bot_ai_knowledge.encrypt_token(PLAINTEXT)
        sid = _src(
            dal, community_id=community_id, source_type="github_repo_markdown",
            source_url="https://github.com/o/r", encrypted_token=sealed,
        )  # fmt: skip
        seen: list[str | None] = []

        async def fake_fetch(_u: str, _b: str, _d: str, token: str | None) -> list[Any]:
            seen.append(token)
            return []

        monkeypatch.setattr(bot_ai_knowledge, "_fetch_github_markdown", fake_fetch)
        await bot_ai_knowledge.index_source(dal, sid)
        assert seen == [PLAINTEXT]

    async def test_index_source_undecryptable_token_fails_loud_without_leak(
        self, dal: Any, community_id: int
    ) -> None:
        sid = _src(
            dal, community_id=community_id, source_type="github_repo_markdown",
            source_url="https://github.com/o/r", encrypted_token=PLAINTEXT,
        )  # fmt: skip
        with pytest.raises(bot_ai_knowledge.KnowledgeServiceError) as ei:
            await bot_ai_knowledge.index_source(dal, sid)
        assert PLAINTEXT not in str(ei.value)
        dal.commit()
        assert PLAINTEXT not in (dal.ai_knowledge_sources[sid].index_errors or "")


def test_super_admin_lookup_guards(dal: Any, users: dict[str, int]) -> None:
    assert bot_blueprint._is_super_admin_sync(dal, 0) is False
    assert bot_blueprint._is_super_admin_sync(dal, 999999) is False
    assert bot_blueprint._is_super_admin_sync(dal, users["vendor"]) is False
    assert bot_blueprint._is_super_admin_sync(dal, users["root"]) is True


_SCOPE = {"search": READ, "suggest": WRITE}


class TestSearchSuggestScope:
    """`/search` and `/suggest` must not read across communities."""

    @pytest.fixture
    def spy(self, monkeypatch: Any) -> list[tuple[str, int | None]]:
        calls: list[tuple[str, int | None]] = []

        async def fake_search(
            _d: Any, _q: str, *, community_id: int | None = None, **_k: Any
        ) -> list[Any]:
            calls.append(("search", community_id))
            return []

        async def fake_gen(_d: Any, _t: int, _x: str, *, community_id: int | None = None) -> None:
            calls.append(("suggest", community_id))

        monkeypatch.setattr(bot_ai_knowledge, "search_knowledge", fake_search)
        monkeypatch.setattr(bot_ai_knowledge, "generate_suggestion", fake_gen)
        return calls

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("search", {"query": "q"}),
            ("suggest", {"ticketId": 1, "ticketText": "t"}),
        ],
    )
    async def test_missing_community_refused_non_superadmin(
        self, client: Any, auth_headers: Any, spy: Any, path: str, body: dict[str, Any]
    ) -> None:
        r = await client.post(
            f"/api/v1/admin/ai-knowledge/{path}",
            headers=auth_headers(scope=_SCOPE[path]),
            json=body,
        )
        assert r.status_code == 400
        assert spy == []

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("search", {"query": "q"}),
            ("suggest", {"ticketId": 1, "ticketText": "t"}),
        ],
    )
    async def test_foreign_community_refused(
        self, client: Any, auth_headers: Any, spy: Any, other_tenant_community_id: int,
        path: str, body: dict[str, Any],
    ) -> None:  # fmt: skip
        r = await client.post(
            f"/api/v1/admin/ai-knowledge/{path}",
            headers=auth_headers(scope=_SCOPE[path]),
            json={**body, "communityId": int(other_tenant_community_id)},
        )
        assert r.status_code == 403
        assert spy == []

    async def test_own_community_passes_scope_through(
        self, client: Any, auth_headers: Any, spy: Any, community_id: int
    ) -> None:
        cid = int(community_id)
        for path, body in (
            ("search", {"query": "q"}),
            ("suggest", {"ticketId": 1, "ticketText": "t"}),
        ):
            r = await client.post(
                f"/api/v1/admin/ai-knowledge/{path}",
                headers=auth_headers(scope=_SCOPE[path]),
                json={**body, "communityId": cid},
            )
            assert r.status_code == 200
        assert spy == [("search", cid), ("suggest", cid)]

    async def test_superadmin_may_search_unscoped(
        self, client: Any, auth_headers: Any, spy: Any, users: dict[str, int]
    ) -> None:
        r = await client.post(
            "/api/v1/admin/ai-knowledge/search",
            headers=auth_headers(scope=READ, user_id=str(users["root"])),
            json={"query": "q"},
        )
        assert r.status_code == 200
        assert spy == [("search", None)]

    async def test_service_sql_filters_by_community(self, monkeypatch: Any) -> None:
        captured: dict[str, Any] = {}

        async def emb(_t: str) -> list[float]:
            return [0.1, 0.2]

        class _Dal:
            def executesql(self, sql: str, placeholders: list[Any]) -> list[Any]:
                captured["sql"], captured["ph"] = sql, placeholders
                return []

        monkeypatch.setattr(bot_ai_knowledge, "_generate_embedding", emb)
        await bot_ai_knowledge.search_knowledge(_Dal(), "q", community_id=42, vendor_id=7)
        assert "ks.community_id = %s" in captured["sql"]
        assert 42 in captured["ph"] and 7 in captured["ph"]


class TestVendorOwnedConfusedDeputy:
    """A community admin must not drive a vendor-owned source, nor redirect its credential."""

    async def test_community_admin_vendor_owned_source_404_all_verbs(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int,
        users: dict[str, int], monkeypatch: Any,
    ) -> None:  # fmt: skip
        sid = _src(dal, community_id=community_id, vendor_id=users["vendor"], source_name="v")
        called: list[int] = []

        async def fake(_d: Any, s: int) -> None:
            called.append(s)

        monkeypatch.setattr(bot_ai_knowledge, "index_source", fake)
        h = auth_headers(scope=WRITE)
        assert (
            await client.put(f"{BASE}/{sid}", headers=h, json={"source_name": "x"})
        ).status_code == 404
        assert (await client.post(f"{BASE}/{sid}/reindex", headers=h)).status_code == 404
        assert (await client.delete(f"{BASE}/{sid}", headers=h)).status_code == 404
        await asyncio.sleep(0)
        assert called == []
        assert _name(dal, sid) == "v"

    async def test_super_admin_may_manage_vendor_owned_source(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int,
        users: dict[str, int], monkeypatch: Any,
    ) -> None:  # fmt: skip
        monkeypatch.setattr(bot_blueprint, "_is_super_admin_sync", lambda _d, _u: True)
        sid = _src(dal, community_id=community_id, vendor_id=users["vendor"])
        r = await client.put(
            f"{BASE}/{sid}", headers=auth_headers(scope=WRITE), json={"source_name": "ok"}
        )
        assert r.status_code == 200

    @pytest.fixture(autouse=True)
    def _no_dns(self, monkeypatch: Any) -> None:
        """SSRF DNS resolution is covered elsewhere; here only token handling is under test."""
        monkeypatch.setattr(bot_ai_knowledge, "_validate_source_url", lambda _u: None)

    @pytest.mark.parametrize(
        "change",
        [
            {"source_url": "https://docs.example.com/evil"},
            {"branch": "attacker"},
            {"docs_path": "/other"},
        ],
    )
    async def test_target_change_without_token_clears_stored_token(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int, change: dict[str, str]
    ) -> None:
        sid = _src(
            dal, community_id=community_id, source_url="https://docs.example.com/a",
            branch="main", docs_path="/docs", encrypted_token="sealed-ciphertext",
        )  # fmt: skip
        r = await client.put(f"{BASE}/{sid}", headers=auth_headers(scope=WRITE), json=change)
        assert r.status_code == 200
        dal.commit()
        assert dal.ai_knowledge_sources[sid].encrypted_token is None

    async def test_target_change_with_new_token_stores_new_token(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int
    ) -> None:
        sid = _src(
            dal, community_id=community_id, source_url="https://docs.example.com/a",
            encrypted_token="old",
        )  # fmt: skip
        r = await client.put(
            f"{BASE}/{sid}",
            headers=auth_headers(scope=WRITE),
            json={"source_url": "https://docs.example.com/b", "token": PLAINTEXT},
        )
        assert r.status_code == 200
        dal.commit()
        assert decrypt_token(dal.ai_knowledge_sources[sid].encrypted_token) == PLAINTEXT

    async def test_unchanged_target_or_rename_keeps_token(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int
    ) -> None:
        sid = _src(
            dal, community_id=community_id, source_url="https://docs.example.com/a",
            encrypted_token="keep",
        )  # fmt: skip
        r = await client.put(
            f"{BASE}/{sid}",
            headers=auth_headers(scope=WRITE),
            json={"source_name": "n", "source_url": "https://docs.example.com/a"},
        )
        assert r.status_code == 200
        dal.commit()
        assert dal.ai_knowledge_sources[sid].encrypted_token == "keep"
