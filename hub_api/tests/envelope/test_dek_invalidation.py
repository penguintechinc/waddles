"""`dek-rotated` publish: rotation and revocation emit the event the Rust listener reads."""

from __future__ import annotations

import pytest

from services.envelope.errors import KmsAccessDeniedError, TenantKeyUnavailableError
from services.envelope.invalidation import (
    AT_REST_PURPOSE,
    DEK_INVALIDATION_STREAM,
    DekInvalidationPublishError,
    publish_dek_invalidation,
)
from tests.envelope.conftest import TENANT_A

pytestmark = pytest.mark.asyncio


class FakeStreamClient:
    """Records XADDs; optionally fails the write."""

    def __init__(self, fail: bool = False) -> None:
        """Start empty."""
        self.entries: list[tuple[str, dict[str, str]]] = []
        self.fail = fail
        self.closed = False

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs: object) -> str:
        if self.fail:
            raise ConnectionError("valkey down")
        self.entries.append((stream, fields))
        return "1-0"

    async def aclose(self) -> None:
        self.closed = True


def _recorder(client: FakeStreamClient):
    async def publish(tenant_id: int, reason: str) -> None:
        await publish_dek_invalidation(tenant_id, reason, client=client)  # type: ignore[arg-type]

    return publish


async def test_publish_uses_the_exact_stream_and_fields_the_rust_listener_reads() -> None:
    client = FakeStreamClient()
    await publish_dek_invalidation(7, "rotated", client=client)
    assert client.entries == [
        ("keys:tenant-dek:invalidate", {"tenant": "7", "purpose": "at-rest", "reason": "rotated"})
    ]
    assert DEK_INVALIDATION_STREAM == "keys:tenant-dek:invalidate"
    assert AT_REST_PURPOSE == "at-rest"


async def test_publish_failure_is_loud() -> None:
    with pytest.raises(DekInvalidationPublishError):
        await publish_dek_invalidation(7, "rotated", client=FakeStreamClient(fail=True))


async def test_rotation_publishes_after_commit(make_harness) -> None:
    client = FakeStreamClient()
    h = make_harness()
    h.service._invalidation_publisher = _recorder(client)
    record = await h.service.rotate_dek(TENANT_A)
    assert len(client.entries) == 1
    stream, fields = client.entries[0]
    assert stream == DEK_INVALIDATION_STREAM
    assert fields == {"tenant": str(TENANT_A), "purpose": "at-rest", "reason": "rotated"}
    # the new version is already committed when the event fires, and carries no key material
    assert (await h.repo.get_active(TENANT_A)).dek_version == record.dek_version
    assert set(fields) == {"tenant", "purpose", "reason"}


async def test_rotation_publish_failure_raises_but_rotation_stands(make_harness) -> None:
    h = make_harness()
    h.service._invalidation_publisher = _recorder(FakeStreamClient(fail=True))
    with pytest.raises(DekInvalidationPublishError):
        await h.service.rotate_dek(TENANT_A)
    assert (await h.repo.get_active(TENANT_A)).dek_version == 1


async def test_revocation_publishes_and_still_raises_unavailable(make_harness) -> None:
    client = FakeStreamClient()
    h = make_harness()
    h.service._invalidation_publisher = _recorder(client)
    await h.service.ensure_tenant_key(TENANT_A)

    async def deny(*_a: object, **_k: object) -> bytes:
        raise KmsAccessDeniedError("denied", code="AccessDenied")

    h.kek.unwrap = deny  # type: ignore[method-assign]
    with pytest.raises(TenantKeyUnavailableError):
        await h.service.export_dek(TENANT_A)
    assert [f for _, f in client.entries] == [
        {"tenant": str(TENANT_A), "purpose": "at-rest", "reason": "revoked"}
    ]


async def test_revocation_publish_failure_does_not_mask_the_revocation(make_harness) -> None:
    h = make_harness()
    h.service._invalidation_publisher = _recorder(FakeStreamClient(fail=True))
    await h.service.ensure_tenant_key(TENANT_A)

    async def deny(*_a: object, **_k: object) -> bytes:
        raise KmsAccessDeniedError("denied", code="AccessDenied")

    h.kek.unwrap = deny  # type: ignore[method-assign]
    with pytest.raises(TenantKeyUnavailableError):
        await h.service.export_dek(TENANT_A)
