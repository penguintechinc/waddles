"""Platform-managed baseline: zero configuration, no entitlement, no external calls."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from services.envelope import (
    EnvelopeInputError,
    EnvelopeIntegrityError,
    KmsProviderRegistry,
    PlatformKekError,
    TenantEnvelopeService,
    TenantKeyNotFoundError,
)
from services.envelope.models import KEK_KIND_PLATFORM, EncryptedField
from services.envelope.platform_kek import LazyPlatformKek
from tests.envelope.fakes import (
    TENANT_A,
    TENANT_B,
    FakeGate,
    InMemoryEnvelopeRepo,
)

ROW = uuid.UUID("11111111-2222-3333-4444-555555555555")


async def test_baseline_works_with_no_config_and_never_touches_the_gate_or_a_provider(
    make_harness, aws_mock, gcp_mock, azure_mock
) -> None:
    h = make_harness()
    field = await h.service.encrypt(
        TENANT_A, b"hunter2", table="platform_integrations", column="access_token", row_uuid=ROW
    )
    plaintext = await h.service.decrypt(
        TENANT_A, field, table="platform_integrations", column="access_token", row_uuid=ROW
    )
    assert plaintext == b"hunter2"
    # Zero-config baseline: platform KEK only; no entitlement question, no provider traffic.
    assert h.gate.asked == []
    assert aws_mock.server.calls() == []
    assert gcp_mock.server.calls() == []
    assert azure_mock.server.calls() == []
    (record,) = await h.repo.list_keys(TENANT_A)
    assert record.kek_kind == KEK_KIND_PLATFORM
    assert h.kek.wraps >= 1


async def test_text_wire_form_round_trips(make_harness) -> None:
    h = make_harness()
    wire = await h.service.encrypt_text(
        TENANT_A, "s3cret", table="music_oauth_tokens", column="refresh_token", row_uuid=ROW
    )
    assert wire.startswith("wenv1.1.")
    assert "s3cret" not in wire
    assert (
        await h.service.decrypt_text(
            TENANT_A, wire, table="music_oauth_tokens", column="refresh_token", row_uuid=ROW
        )
        == "s3cret"
    )


@pytest.mark.parametrize(
    "swap",
    [
        {"table": "other_table"},
        {"column": "other_column"},
        {"row_uuid": uuid.UUID(int=7)},
    ],
)
async def test_ciphertext_moved_to_another_slot_fails_authentication(make_harness, swap) -> None:
    h = make_harness()
    slot = {"table": "platform_integrations", "column": "access_token", "row_uuid": ROW}
    field = await h.service.encrypt(TENANT_A, b"x", **slot)
    with pytest.raises(EnvelopeIntegrityError):
        await h.service.decrypt(TENANT_A, field, **{**slot, **swap})


async def test_ciphertext_cannot_be_read_by_another_tenant(make_harness) -> None:
    h = make_harness()
    slot = {"table": "platform_integrations", "column": "access_token", "row_uuid": ROW}
    field = await h.service.encrypt(TENANT_A, b"tenant-a-secret", **slot)
    await h.service.encrypt(TENANT_B, b"warm-up", **slot)  # tenant B has its own DEK v1
    with pytest.raises(EnvelopeIntegrityError):
        await h.service.decrypt(TENANT_B, field, **slot)


async def test_tampered_ciphertext_is_rejected(make_harness) -> None:
    h = make_harness()
    slot = {"table": "platform_integrations", "column": "access_token", "row_uuid": ROW}
    field = await h.service.encrypt(TENANT_A, b"x", **slot)
    flipped = bytes([field.ciphertext[0] ^ 1]) + field.ciphertext[1:]
    with pytest.raises(EnvelopeIntegrityError):
        await h.service.decrypt(
            TENANT_A,
            EncryptedField(field.dek_version, field.iv, flipped),
            **slot,
        )


@pytest.mark.parametrize("tenant_id", [0, -1, True])
async def test_invalid_tenant_ids_are_rejected_at_the_boundary(make_harness, tenant_id) -> None:
    h = make_harness()
    with pytest.raises(EnvelopeInputError):
        await h.service.encrypt(tenant_id, b"x", table="t", column="c", row_uuid=ROW)


async def test_unknown_dek_version_is_not_found(make_harness) -> None:
    h = make_harness()
    slot = {"table": "platform_integrations", "column": "access_token", "row_uuid": ROW}
    await h.service.encrypt(TENANT_A, b"x", **slot)
    with pytest.raises(TenantKeyNotFoundError):
        await h.service.decrypt(TENANT_A, EncryptedField(99, b"\0" * 12, b"\0" * 32), **slot)


async def test_dek_is_cached_so_repeated_use_does_not_re_unwrap(make_harness) -> None:
    h = make_harness()
    slot = {"table": "platform_integrations", "column": "access_token", "row_uuid": ROW}
    field = await h.service.encrypt(TENANT_A, b"x", **slot)
    unwraps_before = h.kek.unwraps
    for _ in range(5):
        await h.service.decrypt(TENANT_A, field, **slot)
    assert h.kek.unwraps == unwraps_before


async def test_concurrent_first_use_creates_exactly_one_key(make_harness) -> None:
    h = make_harness()
    slot = {"table": "platform_integrations", "column": "access_token", "row_uuid": ROW}
    await asyncio.gather(*[h.service.encrypt(TENANT_A, b"x", **slot) for _ in range(25)])
    assert [r.dek_version for r in await h.repo.list_keys(TENANT_A)] == [1]


async def test_two_replicas_racing_to_create_the_first_key_converge(make_harness) -> None:
    """The loser of the insert race re-reads the winner's row instead of minting a second key."""
    h = make_harness()
    replica = TenantEnvelopeService(
        keys=h.repo,
        configs=h.repo,
        registry=h.providers.registry,
        platform_kek=lambda: h.kek,
        gate=h.gate,
        clock=h.clock,
    )
    slot = {"table": "platform_integrations", "column": "access_token", "row_uuid": ROW}
    fields = await asyncio.gather(
        h.service.encrypt(TENANT_A, b"one", **slot), replica.encrypt(TENANT_A, b"two", **slot)
    )
    assert {f.dek_version for f in fields} == {1}
    assert len(await h.repo.list_keys(TENANT_A)) == 1
    assert await replica.decrypt(TENANT_A, fields[0], **slot) == b"one"


async def test_dek_rotation_keeps_old_ciphertext_readable(make_harness) -> None:
    h = make_harness()
    slot = {"table": "platform_integrations", "column": "access_token", "row_uuid": ROW}
    old = await h.service.encrypt(TENANT_A, b"old", **slot)
    record = await h.service.rotate_dek(TENANT_A)
    assert record.dek_version == 2
    new = await h.service.encrypt(TENANT_A, b"new", **slot)
    assert (old.dek_version, new.dek_version) == (1, 2)
    h.service.invalidate(TENANT_A)  # force re-unwrap of both versions from the key store
    assert await h.service.decrypt(TENANT_A, old, **slot) == b"old"
    assert await h.service.decrypt(TENANT_A, new, **slot) == b"new"
    statuses = {r.dek_version: r.status for r in await h.repo.list_keys(TENANT_A)}
    assert statuses == {1: "retired", 2: "active"}


async def test_usage_cap_triggers_automatic_rotation(make_harness) -> None:
    from services.envelope.service import EnvelopeSettings

    h = make_harness(settings=EnvelopeSettings(usage_flush_batch=2, usage_cap=4))
    slot = {"table": "platform_integrations", "column": "access_token", "row_uuid": ROW}
    fields = [await h.service.encrypt(TENANT_A, b"x", **slot) for _ in range(5)]
    versions = [r.dek_version for r in await h.repo.list_keys(TENANT_A)]
    assert versions == [1, 2]  # auto-rotated at the nonce-safety cap
    assert fields[-1].dek_version == 2
    h.service.invalidate(TENANT_A)
    assert await h.service.decrypt(TENANT_A, fields[0], **slot) == b"x"  # v1 still readable


async def test_missing_platform_kek_fails_loud_never_silently() -> None:
    """regression: external-kms baseline -- an unmounted platform KEK is an error, not plaintext."""
    lazy = LazyPlatformKek(factory=lambda: (_ for _ in ()).throw(PlatformKekError("not set")))
    service = TenantEnvelopeService(
        keys=InMemoryEnvelopeRepo(),
        configs=InMemoryEnvelopeRepo(),
        registry=KmsProviderRegistry(),
        platform_kek=lazy.get,
        gate=FakeGate(),
    )
    with pytest.raises(PlatformKekError):
        await service.encrypt(
            TENANT_A, b"x", table="platform_integrations", column="access_token", row_uuid=ROW
        )
