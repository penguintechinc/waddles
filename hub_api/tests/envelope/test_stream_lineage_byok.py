"""The ingest-stream DEK lineage follows the tenant's BYOK lifecycle (#442).

One tenant KMS config covers every key lineage: activating BYOK must move the stream DEKs onto
the customer key, and the exit ramp must move them back BEFORE the config is dropped -- or a
disable would strand stream DEKs under a key hub-api can no longer address. Real adapters
against the mock AWS KMS socket; in-memory key rows.
"""

from __future__ import annotations

from collections.abc import Callable

from services.envelope.kms_adapter import PROVIDER_AWS
from services.envelope.models import KEK_KIND_CUSTOMER, KEK_KIND_PLATFORM
from services.envelope.repository import PURPOSE_INGEST_STREAM
from services.envelope.service import TenantEnvelopeService
from tests.envelope.conftest import Harness
from tests.envelope.fakes import SLUG_A, TENANT_A, InMemoryEnvelopeRepo


def _wire(h: Harness) -> tuple[TenantEnvelopeService, InMemoryEnvelopeRepo]:
    """Rewire the harness like `build_envelope_runtime`: at-rest owner + stream sibling."""
    stream_repo = InMemoryEnvelopeRepo()
    stream_repo.configs = h.repo.configs  # one config store for both lineages
    h.service = TenantEnvelopeService(
        keys=h.repo,
        configs=h.repo,
        registry=h.providers.registry,
        platform_kek=lambda: h.kek,
        gate=h.gate,
        clock=h.clock,
        sibling_keys=(stream_repo,),
    )
    stream = TenantEnvelopeService(
        keys=stream_repo,
        configs=h.repo,
        registry=h.providers.registry,
        platform_kek=lambda: h.kek,
        gate=h.gate,
        clock=h.clock,
        lineage=PURPOSE_INGEST_STREAM,
        sibling_keys=(h.repo,),
    )
    return stream, stream_repo


async def test_activate_and_disable_move_stream_deks_with_the_tenant(
    make_harness: Callable[..., Harness],
) -> None:
    h = make_harness()
    stream, stream_repo = _wire(h)
    version, key = await stream.data_key(TENANT_A)
    assert version == 1
    (row,) = await stream_repo.list_keys(TENANT_A)
    assert row.kek_kind == KEK_KIND_PLATFORM

    _, report = await h.onboard(PROVIDER_AWS)
    assert report.ok  # type: ignore[attr-defined]
    (row,) = await stream_repo.list_keys(TENANT_A)
    assert row.kek_kind == KEK_KIND_CUSTOMER
    stream.invalidate(TENANT_A)
    assert await stream.data_key(TENANT_A, 1) == (1, key)  # same DEK, new KEK

    # New stream key material under active BYOK goes to the customer key.
    await stream.rotate_dek(TENANT_A, tenant_slug=SLUG_A)
    assert {r.kek_kind for r in await stream_repo.list_keys(TENANT_A)} == {KEK_KIND_CUSTOMER}

    h.gate.entitled.clear()
    report = await h.service.disable_external_kms(TENANT_A)
    assert report.ok
    assert await h.repo.get(TENANT_A) is None
    assert {r.kek_kind for r in await stream_repo.list_keys(TENANT_A)} == {KEK_KIND_PLATFORM}
    h.aws.stop()  # customer KMS gone entirely: stream keys still usable
    stream.invalidate(TENANT_A)
    assert await stream.data_key(TENANT_A, 1) == (1, key)


async def test_stream_and_at_rest_subkeys_are_domain_separated(
    make_harness: Callable[..., Harness],
) -> None:
    h = make_harness()
    stream, _ = _wire(h)
    _, stream_key = await stream.data_key(TENANT_A)
    _, rest_key = await h.service.data_key(TENANT_A)
    assert stream_key != rest_key
