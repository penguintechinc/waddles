"""Mem0Service keying + DSAR hooks (GDPR Art. 15/17): PII-free owner keys, export, erase.

`mem0` (LLM/embedder/Qdrant client) is replaced by an in-memory `Memory` fake so the real
`Mem0Service` logic runs without a live Qdrant/Ollama. The hub-api side that reads Qdrant
directly for the DSAR flow is covered by `hub_api/tests/test_mem0_privacy.py`.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"


class FakeMemory:
    """In-memory stand-in for `mem0.Memory`, keyed by owner `user_id`."""

    def __init__(self) -> None:
        self.store: dict[str, list[Any]] = {}

    @classmethod
    def from_config(cls, _config: dict[str, Any]) -> FakeMemory:
        return cls()

    def add(self, messages: Any, user_id: str, metadata: Any = None) -> dict[str, str]:
        self.store.setdefault(user_id, []).append(messages)
        return {"id": f"m{len(self.store[user_id])}"}

    def get_all(self, user_id: str) -> list[Any]:
        return list(self.store.get(user_id, []))

    def delete_all(self, user_id: str) -> None:
        self.store.pop(user_id, None)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake_module = types.ModuleType("mem0")
    fake_module.Memory = FakeMemory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mem0", fake_module)
    monkeypatch.delitem(sys.modules, "services.mem0_service", raising=False)
    from services.mem0_service import Mem0Service

    return Mem0Service(community_id=7)


class TestOwnerKeys:
    def test_user_key_is_a_uuid_token(self, service: Any) -> None:
        assert service.user_key(UUID_A.upper()) == f"user_{UUID_A}"

    @pytest.mark.parametrize("raw", ["alice", "alice@example.com", "12345", "", "user_alice"])
    def test_raw_pii_is_rejected(self, service: Any, raw: str) -> None:
        with pytest.raises(ValueError):
            service.user_key(raw)

    async def test_add_memory_rejects_raw_pii_owner_keys(self, service: Any) -> None:
        with pytest.raises(ValueError):
            await service.add_memory("likes pizza", user_id="alice@example.com")
        assert service._memory.store == {}

    async def test_add_memory_accepts_uuid_and_community_keys(self, service: Any) -> None:
        await service.add_memory("a", user_id=service.user_key(UUID_A))
        await service.add_memory("b")
        assert set(service._memory.store) == {f"user_{UUID_A}", "community_7"}

    async def test_add_messages_attributes_to_the_user_token(self, service: Any) -> None:
        await service.add_messages([{"role": "user", "content": "hi"}], user_uuid=UUID_A)
        await service.add_messages([{"role": "user", "content": "yo"}])
        assert set(service._memory.store) == {f"user_{UUID_A}", "community_7"}

    async def test_add_messages_rejects_a_non_uuid_user(self, service: Any) -> None:
        with pytest.raises(ValueError):
            await service.add_messages([{"role": "user", "content": "hi"}], user_uuid="alice")


class TestDsarHooks:
    async def test_export_returns_only_the_subjects_memories(self, service: Any) -> None:
        await service.add_messages([{"role": "user", "content": "mine"}], user_uuid=UUID_A)
        await service.add_messages([{"role": "user", "content": "theirs"}], user_uuid=UUID_B)
        exported = await service.export_user_memories(UUID_A)
        assert exported == [[{"role": "user", "content": "mine"}]]

    async def test_export_for_unknown_user_is_empty(self, service: Any) -> None:
        assert await service.export_user_memories(UUID_A) == []

    async def test_export_unwraps_a_results_envelope(self, service: Any) -> None:
        service._memory.get_all = lambda user_id: {"results": [{"id": "x"}]}
        assert await service.export_user_memories(UUID_A) == [{"id": "x"}]

    async def test_delete_removes_every_memory_of_the_subject_only(self, service: Any) -> None:
        await service.add_messages([{"role": "user", "content": "1"}], user_uuid=UUID_A)
        await service.add_messages([{"role": "user", "content": "2"}], user_uuid=UUID_A)
        await service.add_messages([{"role": "user", "content": "3"}], user_uuid=UUID_B)
        await service.delete_user_memories(UUID_A)
        assert await service.export_user_memories(UUID_A) == []
        assert len(await service.export_user_memories(UUID_B)) == 1

    async def test_delete_for_unknown_user_is_a_noop(self, service: Any) -> None:
        await service.delete_user_memories(UUID_A)

    async def test_delete_failure_propagates(self, service: Any) -> None:
        def boom(user_id: str) -> None:
            raise RuntimeError("qdrant down")

        service._memory.delete_all = boom
        with pytest.raises(RuntimeError):
            await service.delete_user_memories(UUID_A)
