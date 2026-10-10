"""Regression tests: each pins a way external KMS could silently weaken the guarantee.

Every test here fails loudly if a change reintroduces a quiet downgrade -- to the platform key,
to plaintext, or to a key nobody can unwrap -- or flags a customer revoked for our own outage.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping

import pytest

from services.envelope import (
    KmsProviderRegistry,
    KmsRejectedError,
    TenantEnvelopeService,
    TenantKeyUnavailableError,
    UnsupportedKmsProviderError,
)
from services.envelope.kms_adapter import (
    PROVIDER_AWS,
    PROVIDER_AZURE,
    PROVIDER_GCP,
    CanonicalConfig,
)
from services.envelope.models import (
    CONFIG_ACTIVE,
    CONFIG_REVOKED,
    KEK_KIND_CUSTOMER,
    KEK_KIND_PLATFORM,
    KmsKeyInfo,
)
from services.envelope.service import EnvelopeSettings
from tests.envelope.conftest import AWS_KEY_ARN, AWS_KEY_ARN_2, AWS_ROLE_ARN, AZURE_KEY_URL, GCP_KEY
from tests.envelope.fakes import SLUG_A, TENANT_A, FakeGate, InMemoryEnvelopeRepo
from tests.envelope.kms_mocks import MockAzureKeyVault

SLOT = {
    "table": "platform_integrations",
    "column": "access_token",
    "row_uuid": uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
}


async def test_reconfiguring_to_a_new_key_blocks_new_key_material_until_activated(
    make_harness,
) -> None:
    """regression: external-kms -- a pending reconfiguration must not downgrade to platform."""
    h = make_harness()
    await h.onboard(PROVIDER_AWS)
    await h.service.encrypt(TENANT_A, b"x", **SLOT)
    h.aws.add_key(AWS_KEY_ARN_2)
    await h.service.configure_external_kms(
        TENANT_A,
        tenant_slug=SLUG_A,
        provider=PROVIDER_AWS,
        key_ref=AWS_KEY_ARN_2,
        region=None,
        principal=AWS_ROLE_ARN,
    )
    wraps = h.kek.wraps
    with pytest.raises(TenantKeyUnavailableError) as raised:
        await h.service.rotate_dek(TENANT_A, tenant_slug=SLUG_A)
    assert raised.value.reason == "kms_pending"
    assert h.kek.wraps == wraps  # no silent platform fallback

    config, report = await h.service.activate_external_kms(TENANT_A, tenant_slug=SLUG_A)
    assert (config.status, report.ok) == (CONFIG_ACTIVE, True)
    (row,) = await h.repo.list_keys(TENANT_A)
    assert row.kek_ref == AWS_KEY_ARN_2
    # The old key can now be destroyed by the customer without losing data.
    del h.aws.keys[AWS_KEY_ARN]
    h.service.invalidate(TENANT_A)
    assert await h.service.encrypt(TENANT_A, b"still works", **SLOT)


async def test_a_revoked_config_never_lets_new_key_material_fall_back_to_the_platform_key(
    make_harness,
) -> None:
    """regression: external-kms -- 'revoked' fails closed, never a platform-wrapped DEK."""
    h = make_harness()
    await h.onboard(PROVIDER_AWS)
    await h.service.encrypt(TENANT_A, b"x", **SLOT)
    await h.repo.set_status(TENANT_A, CONFIG_REVOKED, error_code="AccessDeniedException")
    wraps = h.kek.wraps
    with pytest.raises(TenantKeyUnavailableError) as raised:
        await h.service.rotate_dek(TENANT_A, tenant_slug=SLUG_A)
    assert raised.value.reason == "kms_access_denied"
    assert h.kek.wraps == wraps
    assert {r.kek_kind for r in await h.repo.list_keys(TENANT_A)} == {KEK_KIND_CUSTOMER}


async def test_customer_wrapped_keys_without_a_config_row_are_unreadable_not_platform_decrypted(
    make_harness,
) -> None:
    """regression: external-kms -- a lost config row never routes customer DEKs to platform."""
    h = make_harness()
    await h.onboard(PROVIDER_GCP)
    field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
    await h.repo.delete(TENANT_A)  # config row gone, DEKs still customer-wrapped
    h.service.invalidate(TENANT_A)
    unwraps = h.kek.unwraps
    with pytest.raises(TenantKeyUnavailableError) as raised:
        await h.service.decrypt(TENANT_A, field, **SLOT)
    assert raised.value.reason == "kms_config_missing"
    assert h.kek.unwraps == unwraps


async def test_a_wrap_that_cannot_be_unwrapped_is_never_persisted(make_harness) -> None:
    """regression: external-kms -- never persist an un-unwrappable DEK (wrap verified)."""

    class WrapsButCannotUnwrap:
        kek_kind = KEK_KIND_CUSTOMER
        key_ref = "broken-kms"

        async def wrap(self, plaintext_dek: bytes, *, context: Mapping[str, str]) -> bytes:
            return b"opaque-blob"

        async def unwrap(self, wrapped_dek: bytes, *, context: Mapping[str, str]) -> bytes:
            return b"\x00" * 32  # a different key comes back

        async def verify(self) -> KmsKeyInfo:
            return KmsKeyInfo(provider="broken", key_ref=self.key_ref, key_state="Enabled")

    h = make_harness(entitled={SLUG_A})
    registry = KmsProviderRegistry()
    registry.register(
        PROVIDER_AWS,
        factory=lambda config, key_ref: WrapsButCannotUnwrap(),
        validator=lambda key_ref, region, principal: CanonicalConfig(key_ref, region, principal),
    )
    repo = InMemoryEnvelopeRepo()
    service = TenantEnvelopeService(
        keys=repo,
        configs=repo,
        registry=registry,
        platform_kek=lambda: h.kek,
        gate=FakeGate(entitled={SLUG_A}),
    )
    await service.configure_external_kms(
        TENANT_A,
        tenant_slug=SLUG_A,
        provider=PROVIDER_AWS,
        key_ref=AWS_KEY_ARN,
        region=None,
        principal=AWS_ROLE_ARN,
    )
    with pytest.raises(KmsRejectedError, match="unwrap check"):
        await service.activate_external_kms(TENANT_A, tenant_slug=SLUG_A)
    assert await repo.list_keys(TENANT_A) == []  # nothing was stored


async def test_our_own_provider_credential_failure_never_flags_a_tenant_revoked(
    make_harness, monkeypatch, caplog
) -> None:
    """regression: external-kms -- a rotated-away platform secret must not 'revoke' customers."""
    h = make_harness()
    await h.onboard(PROVIDER_AZURE)
    field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
    monkeypatch.setattr(MockAzureKeyVault, "PLATFORM_SECRET", "rotated-away")
    h.azure._valid_tokens.clear()  # force a fresh token request against the now-wrong secret
    h.clock.advance(601)
    h.service.invalidate(TENANT_A)
    with caplog.at_level(logging.ERROR, logger="services.envelope.service"):
        # token cache holds a token the mock no longer honours -> 401 -> refresh -> wrong secret
        with pytest.raises(TenantKeyUnavailableError) as raised:
            await h.service.decrypt(TENANT_A, field, **SLOT)
    assert raised.value.reason == "kms_platform_credentials"
    assert (await h.repo.get(TENANT_A)).status == CONFIG_ACTIVE  # NOT flagged revoked
    assert any(getattr(r, "alert", None) == "kms-platform-credentials" for r in caplog.records)
    assert not any(getattr(r, "alert", None) == "tenant-kms-revoked" for r in caplog.records)


async def test_a_provider_the_deployment_has_not_enabled_fails_loudly(make_harness) -> None:
    """regression: external-kms -- providers default OFF; unlisted ones are refused."""
    h = make_harness(entitled={SLUG_A})
    empty = TenantEnvelopeService(
        keys=h.repo,
        configs=h.repo,
        registry=KmsProviderRegistry(),  # ENVELOPE_KMS_PROVIDERS unset
        platform_kek=lambda: h.kek,
        gate=h.gate,
    )
    for provider, key_ref in (
        (PROVIDER_AWS, AWS_KEY_ARN),
        (PROVIDER_GCP, GCP_KEY),
        (PROVIDER_AZURE, AZURE_KEY_URL),
    ):
        with pytest.raises(UnsupportedKmsProviderError, match="not enabled"):
            await empty.configure_external_kms(
                TENANT_A,
                tenant_slug=SLUG_A,
                provider=provider,
                key_ref=key_ref,
                region=None,
                principal=None,
            )
    with pytest.raises(UnsupportedKmsProviderError, match="unknown"):
        await empty.configure_external_kms(
            TENANT_A,
            tenant_slug=SLUG_A,
            provider="made_up_kms",
            key_ref="x",
            region=None,
            principal=None,
        )
    assert await h.repo.get(TENANT_A) is None
    # ...while the baseline for the very same service object is untouched.
    assert (await empty.encrypt(TENANT_A, b"x", **SLOT)).dek_version == 1
    assert [r.kek_kind for r in await h.repo.list_keys(TENANT_A)] == [KEK_KIND_PLATFORM]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dek_cache_ttl_s": 0},
        {"dek_cache_ttl_s": 7200},
        {"stale_grace_s": -1},
        {"stale_grace_s": 7200},
        {"denied_backoff_s": -1},
        {"transient_backoff_s": -1},
        {"usage_flush_batch": 0},
        {"usage_cap": 1, "usage_flush_batch": 2},
    ],
)
def test_settings_reject_values_that_would_disable_a_safety_property(kwargs) -> None:
    """regression: external-kms -- a bad env var must not switch off the cache TTL or nonce cap."""
    with pytest.raises(ValueError):
        EnvelopeSettings(**kwargs)


def test_settings_from_env_parse_overrides_and_reject_garbage() -> None:
    parsed = EnvelopeSettings.from_env(
        {
            "ENVELOPE_DEK_CACHE_TTL_S": "30",
            "ENVELOPE_STALE_GRACE_S": "60",
            "ENVELOPE_DENIED_BACKOFF_S": "1",
            "ENVELOPE_TRANSIENT_BACKOFF_S": "2",
            "ENVELOPE_USAGE_FLUSH_BATCH": "10",
            "ENVELOPE_USAGE_CAP": "100",
        }
    )
    assert (parsed.dek_cache_ttl_s, parsed.usage_cap) == (30.0, 100)
    assert EnvelopeSettings.from_env({}) == EnvelopeSettings()
    with pytest.raises(ValueError):
        EnvelopeSettings.from_env({"ENVELOPE_DEK_CACHE_TTL_S": "never"})
