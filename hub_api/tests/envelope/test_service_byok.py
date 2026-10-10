"""Enterprise BYOK end to end: real service -> real adapter -> socket-level mock KMS.

Every provider runs the SAME scenarios (parametrized), so the contract the service
relies on -- wrap/unwrap under tenant context, revocation vs outage, exit ramp --
is proven identical across AWS KMS, Google Cloud KMS and Azure Key Vault.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from services.envelope import (
    ExternalKmsNotEntitledError,
    KmsConfigError,
    KmsRejectedError,
    TenantKeyUnavailableError,
)
from services.envelope.kms_adapter import PROVIDER_AWS, PROVIDER_AZURE, PROVIDER_GCP
from services.envelope.models import (
    CONFIG_ACTIVE,
    CONFIG_PENDING,
    CONFIG_REVOKED,
    KEK_KIND_CUSTOMER,
    KEK_KIND_PLATFORM,
)
from tests.envelope.conftest import (
    AWS_KEY_ARN,
    AWS_ROLE_ARN,
    AZURE_DIRECTORY,
    AZURE_KEY_URL,
    GCP_KEY,
    Harness,
)
from tests.envelope.fakes import SLUG_A, SLUG_B, TENANT_A, TENANT_B

ROW = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
SLOT = {"table": "platform_integrations", "column": "access_token", "row_uuid": ROW}
PROVIDERS = [PROVIDER_AWS, PROVIDER_GCP, PROVIDER_AZURE]
KEY_REFS = {PROVIDER_AWS: AWS_KEY_ARN, PROVIDER_GCP: GCP_KEY, PROVIDER_AZURE: AZURE_KEY_URL}

pytestmark = pytest.mark.parametrize("provider", PROVIDERS)


def _mock_for(h: Harness, provider: str):
    return {PROVIDER_AWS: h.aws, PROVIDER_GCP: h.gcp, PROVIDER_AZURE: h.azure}[provider]


def _kms_traffic(h: Harness, provider: str) -> int:
    """Count calls that reached the provider's *key service* (not token/STS plumbing)."""
    mock = _mock_for(h, provider)
    if provider == PROVIDER_AWS:
        return len(mock.server.calls(lambda r: "x-amz-target" in r.headers))
    if provider == PROVIDER_GCP:
        return len(mock.server.calls(lambda r: r.path.startswith("/v1/")))
    return len(mock.server.calls(lambda r: r.path.startswith("/keys/")))


async def test_activation_moves_existing_platform_wrapped_keys_without_re_encrypting_data(
    make_harness, provider
) -> None:
    h = make_harness()
    before = await h.service.encrypt(TENANT_A, b"pre-existing-secret", **SLOT)
    (row,) = await h.repo.list_keys(TENANT_A)
    assert row.kek_kind == KEK_KIND_PLATFORM

    config, report = await h.onboard(provider)

    assert config.status == CONFIG_ACTIVE
    assert (report.total, report.rewrapped, report.failed_versions) == (1, 1, ())
    (row,) = await h.repo.list_keys(TENANT_A)
    assert (row.kek_kind, row.kek_ref) == (KEK_KIND_CUSTOMER, KEY_REFS[provider])
    # The DEK never changed, so ciphertext written before BYOK still opens.
    h.service.invalidate(TENANT_A)
    assert await h.service.decrypt(TENANT_A, before, **SLOT) == b"pre-existing-secret"


async def test_fresh_tenant_gets_its_first_key_directly_under_the_customer_kms(
    make_harness, provider
) -> None:
    h = make_harness()
    config, report = await h.onboard(provider)
    assert config.status == CONFIG_ACTIVE and report.ok
    platform_wraps_before = h.kek.wraps
    field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
    assert field.dek_version == 1
    (row,) = await h.repo.list_keys(TENANT_A)
    assert row.kek_kind == KEK_KIND_CUSTOMER
    assert h.kek.wraps == platform_wraps_before  # the platform KEK never touched this tenant's DEK
    assert h.kek.unwraps == 0


async def test_the_stored_wrapped_dek_is_opaque_and_only_the_customer_kms_opens_it(
    make_harness, provider
) -> None:
    h = make_harness()
    await h.onboard(provider)
    await h.service.encrypt(TENANT_A, b"x", **SLOT)
    (row,) = await h.repo.list_keys(TENANT_A)
    assert row.wrapped_dek
    with pytest.raises(KmsRejectedError):
        await h.kek.unwrap(row.wrapped_dek, context={"waddles_purpose": "tenant-dek"})


async def test_unwrapped_key_is_cached_until_the_ttl_then_re_fetched(
    make_harness, provider
) -> None:
    h = make_harness()
    await h.onboard(provider)
    field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
    h.service.invalidate(TENANT_A)
    await h.service.decrypt(TENANT_A, field, **SLOT)  # miss -> one unwrap
    baseline = _kms_traffic(h, provider)
    for _ in range(5):
        await h.service.decrypt(TENANT_A, field, **SLOT)
    assert _kms_traffic(h, provider) == baseline  # served from cache

    h.clock.advance(601)  # past the 10-minute TTL
    await h.service.decrypt(TENANT_A, field, **SLOT)
    assert _kms_traffic(h, provider) == baseline + 1


async def test_revocation_fails_closed_immediately_and_never_falls_back_to_the_platform_key(
    make_harness, provider, caplog
) -> None:
    h = make_harness()
    await h.onboard(provider)
    field = await h.service.encrypt(TENANT_A, b"customer-secret", **SLOT)
    platform_calls = (h.kek.wraps, h.kek.unwraps)

    _mock_for(h, provider).behavior.deny = True  # the customer revokes the grant
    h.clock.advance(601)
    with (
        caplog.at_level(logging.ERROR, logger="services.envelope.service"),
        pytest.raises(TenantKeyUnavailableError) as raised,
    ):
        await h.service.decrypt(TENANT_A, field, **SLOT)
    assert raised.value.reason == "kms_access_denied"

    # New data is refused too -- no plaintext, no platform-key ciphertext.
    with pytest.raises(TenantKeyUnavailableError):
        await h.service.encrypt(TENANT_A, b"new", **SLOT)
    assert (h.kek.wraps, h.kek.unwraps) == platform_calls
    assert [r.kek_kind for r in await h.repo.list_keys(TENANT_A)] == [KEK_KIND_CUSTOMER]
    # Ops are alerted and the config is flagged.
    assert any(getattr(r, "alert", None) == "tenant-kms-revoked" for r in caplog.records)
    assert (await h.repo.get(TENANT_A)).status == CONFIG_REVOKED


async def test_access_denied_backs_off_instead_of_hammering_the_provider(
    make_harness, provider
) -> None:
    h = make_harness()
    await h.onboard(provider)
    field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
    _mock_for(h, provider).behavior.deny = True
    h.clock.advance(601)
    with pytest.raises(TenantKeyUnavailableError):
        await h.service.decrypt(TENANT_A, field, **SLOT)
    traffic = _kms_traffic(h, provider)
    for _ in range(5):
        with pytest.raises(TenantKeyUnavailableError):
            await h.service.decrypt(TENANT_A, field, **SLOT)
    assert _kms_traffic(h, provider) == traffic  # blocked locally within the backoff window


async def test_access_restored_heals_the_revoked_flag(make_harness, provider) -> None:
    h = make_harness()
    await h.onboard(provider)
    field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
    mock = _mock_for(h, provider)
    mock.behavior.deny = True
    h.clock.advance(601)
    with pytest.raises(TenantKeyUnavailableError):
        await h.service.decrypt(TENANT_A, field, **SLOT)
    assert (await h.repo.get(TENANT_A)).status == CONFIG_REVOKED

    mock.behavior.deny = False
    h.clock.advance(60)  # past the denied backoff
    assert await h.service.decrypt(TENANT_A, field, **SLOT) == b"x"
    assert (await h.repo.get(TENANT_A)).status == CONFIG_ACTIVE


async def test_transient_outage_serves_reads_from_stale_cache_but_refuses_new_writes(
    make_harness, provider
) -> None:
    h = make_harness()
    await h.onboard(provider)
    field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
    _mock_for(h, provider).behavior.fail_status = 503

    h.clock.advance(601)  # TTL elapsed, KMS down
    assert await h.service.decrypt(TENANT_A, field, **SLOT) == b"x"  # grace window: read ok
    with pytest.raises(TenantKeyUnavailableError) as raised:
        await h.service.encrypt(TENANT_A, b"new", **SLOT)  # never mint under an unverifiable key
    assert raised.value.reason == "kms_unavailable"

    h.clock.advance(900)  # grace exhausted
    with pytest.raises(TenantKeyUnavailableError):
        await h.service.decrypt(TENANT_A, field, **SLOT)

    _mock_for(h, provider).behavior.fail_status = None  # provider recovers
    h.clock.advance(10)
    assert await h.service.decrypt(TENANT_A, field, **SLOT) == b"x"
    assert (await h.repo.get(TENANT_A)).status == CONFIG_ACTIVE  # never marked revoked


async def test_one_tenants_revoked_key_does_not_affect_another_tenant(
    make_harness, provider
) -> None:
    h = make_harness()
    await h.onboard(provider, tenant_id=TENANT_A, slug=SLUG_A)
    field_a = await h.service.encrypt(TENANT_A, b"a", **SLOT)
    # Tenant B stays on the baseline.
    field_b = await h.service.encrypt(TENANT_B, b"b", **SLOT)
    _mock_for(h, provider).behavior.deny = True
    h.clock.advance(601)
    with pytest.raises(TenantKeyUnavailableError):
        await h.service.decrypt(TENANT_A, field_a, **SLOT)
    assert await h.service.decrypt(TENANT_B, field_b, **SLOT) == b"b"
    assert await h.service.encrypt(TENANT_B, b"b2", **SLOT)


async def test_lapsed_entitlement_does_not_strand_encrypted_data(make_harness, provider) -> None:
    """The gate governs NEW key material; reading existing data never needs the licence."""
    h = make_harness()
    await h.onboard(provider)
    field = await h.service.encrypt(TENANT_A, b"x", **SLOT)

    h.gate.entitled.clear()  # licence lapses
    h.service.invalidate(TENANT_A)
    assert await h.service.decrypt(TENANT_A, field, **SLOT) == b"x"
    assert await h.service.encrypt(TENANT_A, b"y", **SLOT)  # existing active key keeps working
    # ...but minting NEW key material under the customer key is refused.
    with pytest.raises(ExternalKmsNotEntitledError):
        await h.service.rotate_dek(TENANT_A, tenant_slug=SLUG_A)
    with pytest.raises(ExternalKmsNotEntitledError):
        await h.service.activate_external_kms(TENANT_A, tenant_slug=SLUG_A)


async def test_configuring_without_the_entitlement_is_refused_before_any_side_effect(
    make_harness, provider
) -> None:
    h = make_harness()
    with pytest.raises(ExternalKmsNotEntitledError):
        await h.service.configure_external_kms(
            TENANT_A,
            tenant_slug=SLUG_A,
            provider=provider,
            key_ref=KEY_REFS[provider],
            region=None,
            principal={PROVIDER_AWS: AWS_ROLE_ARN, PROVIDER_AZURE: AZURE_DIRECTORY}.get(provider),
        )
    assert await h.repo.get(TENANT_A) is None
    assert _kms_traffic(h, provider) == 0


async def test_invalid_config_is_rejected_by_the_provider_validator(make_harness, provider) -> None:
    h = make_harness(entitled={SLUG_A})
    with pytest.raises(KmsConfigError):
        await h.service.configure_external_kms(
            TENANT_A,
            tenant_slug=SLUG_A,
            provider=provider,
            key_ref="not-a-key",
            region=None,
            principal=None,
        )


async def test_activation_verifies_the_key_and_fails_loudly_without_the_customer_setup(
    make_harness, provider
) -> None:
    """Without the ExternalId proof / grant the preflight fails and nothing moves."""
    h = make_harness(entitled={SLUG_A})
    if provider == PROVIDER_GCP:
        h.gcp.add_key(GCP_KEY)  # no label
    elif provider == PROVIDER_AZURE:
        h.azure.add_key("waddles")  # no tag
        h.azure.consented.add(AZURE_DIRECTORY)
    await h.service.encrypt(TENANT_A, b"x", **SLOT)
    await h.service.configure_external_kms(
        TENANT_A,
        tenant_slug=SLUG_A,
        provider=provider,
        key_ref=KEY_REFS[provider],
        region=None,
        principal={PROVIDER_AWS: AWS_ROLE_ARN, PROVIDER_AZURE: AZURE_DIRECTORY}.get(provider),
    )
    with pytest.raises(Exception) as raised:  # noqa: PT011 - KmsConfigError or KmsAccessDeniedError
        await h.service.activate_external_kms(TENANT_A, tenant_slug=SLUG_A)
    assert type(raised.value).__name__ in {"KmsConfigError", "KmsAccessDeniedError"}
    (row,) = await h.repo.list_keys(TENANT_A)
    assert row.kek_kind == KEK_KIND_PLATFORM  # untouched
    assert (await h.repo.get(TENANT_A)).status == CONFIG_PENDING


async def test_exit_ramp_returns_every_key_to_the_platform_baseline(make_harness, provider) -> None:
    h = make_harness()
    await h.onboard(provider)
    field = await h.service.encrypt(TENANT_A, b"keep-me", **SLOT)
    await h.service.rotate_dek(TENANT_A, tenant_slug=SLUG_A)

    h.gate.entitled.clear()  # the exit ramp is never entitlement-gated
    report = await h.service.disable_external_kms(TENANT_A)

    assert report.ok and report.rewrapped == 2
    assert await h.repo.get(TENANT_A) is None
    assert {r.kek_kind for r in await h.repo.list_keys(TENANT_A)} == {KEK_KIND_PLATFORM}
    # Proof the customer KMS is no longer a dependency: take it away entirely.
    _mock_for(h, provider).stop()
    h.service.invalidate(TENANT_A)
    assert await h.service.decrypt(TENANT_A, field, **SLOT) == b"keep-me"


async def test_exit_ramp_with_a_revoked_key_reports_what_is_stuck_and_keeps_the_config(
    make_harness, provider
) -> None:
    h = make_harness()
    await h.onboard(provider)
    await h.service.encrypt(TENANT_A, b"x", **SLOT)
    _mock_for(h, provider).behavior.deny = True

    report = await h.service.disable_external_kms(TENANT_A)

    assert not report.ok and report.failed_versions == (1,)
    assert await h.repo.get(TENANT_A) is not None  # still addressable
    assert [r.kek_kind for r in await h.repo.list_keys(TENANT_A)] == [KEK_KIND_CUSTOMER]

    _mock_for(h, provider).behavior.deny = False  # customer restores access; retry succeeds
    assert (await h.service.disable_external_kms(TENANT_A)).ok
    assert await h.repo.get(TENANT_A) is None


async def test_dek_rotation_under_byok_wraps_the_new_version_with_the_customer_key(
    make_harness, provider
) -> None:
    h = make_harness()
    await h.onboard(provider)
    old = await h.service.encrypt(TENANT_A, b"old", **SLOT)
    record = await h.service.rotate_dek(TENANT_A, tenant_slug=SLUG_A)
    assert (record.dek_version, record.kek_kind) == (2, KEK_KIND_CUSTOMER)
    h.service.invalidate(TENANT_A)
    assert await h.service.decrypt(TENANT_A, old, **SLOT) == b"old"


async def test_a_partially_failed_rewrap_leaves_everything_readable_and_resumes(
    make_harness, provider
) -> None:
    h = make_harness()
    old = await h.service.encrypt(TENANT_A, b"old", **SLOT)
    await h.service.rotate_dek(TENANT_A)  # two platform-wrapped versions
    real = h.repo.replace_wrapped
    failures = {"left": 1}

    async def flaky(record, **kwargs):
        if record.dek_version == 2 and failures["left"]:
            failures["left"] -= 1
            return False  # lost the compare-and-swap race
        return await real(record, **kwargs)

    h.repo.replace_wrapped = flaky
    config, report = await h.onboard(provider)

    assert not report.ok and report.failed_versions == (2,)
    assert (
        config.status == CONFIG_PENDING
    )  # not active until EVERY version is under the customer key
    assert {r.dek_version: r.kek_kind for r in await h.repo.list_keys(TENANT_A)} == {
        1: KEK_KIND_CUSTOMER,
        2: KEK_KIND_PLATFORM,
    }
    h.service.invalidate(TENANT_A)
    assert await h.service.decrypt(TENANT_A, old, **SLOT) == b"old"  # still readable

    config, report = await h.service.activate_external_kms(TENANT_A, tenant_slug=SLUG_A)
    assert report.ok and report.already_current == 1 and report.rewrapped == 1
    assert config.status == CONFIG_ACTIVE


async def test_provider_cannot_be_swapped_while_keys_are_wrapped_by_the_customer(
    make_harness, provider
) -> None:
    h = make_harness()
    await h.onboard(provider)
    await h.service.encrypt(TENANT_A, b"x", **SLOT)
    other = next(p for p in PROVIDERS if p != provider)
    with pytest.raises(Exception, match="provider cannot change"):
        await h.service.configure_external_kms(
            TENANT_A,
            tenant_slug=SLUG_A,
            provider=other,
            key_ref=KEY_REFS[other],
            region=None,
            principal={PROVIDER_AWS: AWS_ROLE_ARN, PROVIDER_AZURE: AZURE_DIRECTORY}.get(other),
        )


async def test_external_id_is_stable_across_edits_and_fits_every_providers_token_rules(
    make_harness, provider
) -> None:
    import re

    h = make_harness(entitled={SLUG_A})
    kwargs = {
        "tenant_slug": SLUG_A,
        "provider": provider,
        "key_ref": KEY_REFS[provider],
        "region": None,
        "principal": {PROVIDER_AWS: AWS_ROLE_ARN, PROVIDER_AZURE: AZURE_DIRECTORY}.get(provider),
    }
    first = await h.service.configure_external_kms(TENANT_A, **kwargs)
    second = await h.service.configure_external_kms(TENANT_A, **kwargs)
    assert first.external_id == second.external_id
    # Lowercase [a-z0-9] <= 63 chars: satisfies AWS ExternalId, a GCP label value and an Azure tag.
    assert re.fullmatch(r"[a-z0-9]{1,63}", first.external_id)
    assert (
        first.external_id
        != (
            await h.service.configure_external_kms(TENANT_B, **{**kwargs, "tenant_slug": SLUG_B})
        ).external_id
        if SLUG_B in h.gate.entitled or h.gate.entitled.add(SLUG_B) is None
        else False
    )


async def test_no_key_material_or_secret_ever_reaches_a_log_line(
    make_harness, provider, caplog
) -> None:
    h = make_harness()
    with caplog.at_level(logging.DEBUG):
        config, _ = await h.onboard(provider)
        await h.service.encrypt(TENANT_A, b"super-secret-plaintext", **SLOT)
        await h.service.rotate_dek(TENANT_A, tenant_slug=SLUG_A)
        _mock_for(h, provider).behavior.deny = True
        h.clock.advance(601)
        with pytest.raises(TenantKeyUnavailableError):
            await h.service.encrypt(TENANT_A, b"x", **SLOT)
        await h.service.disable_external_kms(TENANT_A)
    rows = await h.repo.list_keys(TENANT_A)
    secrets_to_hide = [config.external_id, "super-secret-plaintext"]
    secrets_to_hide += [r.wrapped_dek.hex() for r in rows if r.wrapped_dek]
    # What the provider actually saw on the wire: the base64 plaintext DEK (Encrypt request /
    # wrapkey payload) and every bearer/session token. None of it may be in a log record.
    for request in _mock_for(h, provider).server.calls():
        if request.body.startswith(b"{"):
            for key in ("Plaintext", "plaintext", "value"):
                value = request.json().get(key)
                if isinstance(value, str) and len(value) > 20:
                    secrets_to_hide.append(value)
        secrets_to_hide += [
            token
            for token in (
                request.headers.get("authorization", "").removeprefix("Bearer "),
                request.headers.get("x-amz-security-token", ""),
            )
            if len(token) > 8
        ]
    rendered = "\n".join(
        f"{record.getMessage()} {sorted(record.__dict__.items(), key=str)}"
        for record in caplog.records
    )
    assert len(secrets_to_hide) > 6  # the scan really examined wire secrets, not just two literals
    for secret in secrets_to_hide:
        assert secret not in rendered, "key material or credential leaked into a log record"


async def test_byok_does_not_change_the_default_for_a_tenant_that_never_opts_in(
    make_harness, provider
) -> None:
    h = make_harness()
    await h.onboard(provider, tenant_id=TENANT_A, slug=SLUG_A)
    await h.service.encrypt(TENANT_B, b"b", **SLOT)
    (row,) = await h.repo.list_keys(TENANT_B)
    assert row.kek_kind == KEK_KIND_PLATFORM
