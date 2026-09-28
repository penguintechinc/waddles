"""`services/keystore_invalidation.py` -- `keys:tenant-dek:invalidate` publisher (spec Sec5c)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from services.keystore_invalidation import INVALIDATION_STREAM, build_invalidation_publisher


class _FakeValkeyClient:
    def __init__(self) -> None:
        self.xadd = AsyncMock()


async def test_publishes_expected_fields_to_the_invalidation_stream() -> None:
    fake_client = _FakeValkeyClient()
    with patch(
        "services.keystore_invalidation.build_client", return_value=fake_client
    ) as build_client_mock:
        publish = build_invalidation_publisher()
        await publish(123, "ingest-stream", 4, "scheduled_rotation")

    build_client_mock.assert_called_once()
    fake_client.xadd.assert_awaited_once_with(
        INVALIDATION_STREAM,
        {
            "tenant_id": "123",
            "purpose": "ingest-stream",
            "version": "4",
            "reason": "scheduled_rotation",
        },
    )


async def test_client_is_built_lazily_only_once_across_multiple_publishes() -> None:
    """Not rebuilt per call -- constructing this at import/startup time never blocks."""
    fake_client = _FakeValkeyClient()
    with patch(
        "services.keystore_invalidation.build_client", return_value=fake_client
    ) as build_client_mock:
        publish = build_invalidation_publisher()
        await publish(1, "ingest-stream", 1, "rotation")
        await publish(2, "ingest-stream", 1, "shred")

    build_client_mock.assert_called_once()
    assert fake_client.xadd.await_count == 2


async def test_each_publisher_instance_gets_its_own_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def _fake_build_client() -> _FakeValkeyClient:
        client = _FakeValkeyClient()
        calls.append(client)
        return client

    with patch("services.keystore_invalidation.build_client", side_effect=_fake_build_client):
        publish_a = build_invalidation_publisher()
        publish_b = build_invalidation_publisher()
        await publish_a(1, "ingest-stream", 1, "rotation")
        await publish_b(2, "ingest-stream", 1, "rotation")

    assert len(calls) == 2
