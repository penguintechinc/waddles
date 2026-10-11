"""Service edge paths: best-effort bookkeeping never masks a failure, and nothing fails silently."""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace

import pytest

from services.envelope import (
    EnvelopeError,
    ExternalKmsNotEntitledError,
    TenantEnvelopeService,
    TenantKeyNotFoundError,
    TenantKeyUnavailableError,
)
from services.envelope.kms_adapter import PROVIDER_AWS
from services.envelope.models import CONFIG_ACTIVE, CONFIG_REVOKED, KEK_KIND_PLATFORM
from services.envelope.repository import KeyConflictError
from services.envelope.service import EnvelopeSettings
from tests.envelope.conftest import AWS_KEY_ARN, AWS_ROLE_ARN
from tests.envelope.fakes import SLUG_A, TENANT_A, TENANT_B, FakeGate

SLOT = {
    "table": "platform_integrations",
    "column": "access_token",
    "row_uuid": uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
}


def test_invalidating_an_unknown_tenant_is_a_noop(make_harness) -> None:
    make_harness().service.invalidate(999_999)


class TestSlugResolution:
    """The entitlement system keys on slugs; background callers only know ids."""

    async def test_the_resolver_supplies_the_slug_and_is_consulted_once(self, make_harness) -> None:
        h = make_harness(entitled={SLUG_A})
        lookups: list[int] = []

        async def resolver(tenant_id: int) -> str | None:
            lookups.append(tenant_id)
            return SLUG_A

        service = TenantEnvelopeService(
            keys=h.repo,
            configs=h.repo,
            registry=h.providers.registry,
            platform_kek=lambda: h.kek,
            gate=h.gate,
            slug_resolver=resolver,
            clock=h.clock,
        )
        h.service = service
        await h.onboard(PROVIDER_AWS)  # passes the slug explicitly: no lookup needed
        lookups.clear()
        await service.rotate_dek(TENANT_A)  # no slug passed -> resolver
        await service.rotate_dek(TENANT_A)  # remembered
        assert lookups == []  # the slug from onboarding was remembered on the tenant state

        service.invalidate(TENANT_A)
        service._states[TENANT_A].slug = None
        await service.rotate_dek(TENANT_A)
        await service.rotate_dek(TENANT_A)
        assert lookups == [TENANT_A]

    async def test_an_unresolvable_tenant_is_not_entitled(self, make_harness) -> None:
        h = make_harness(entitled={SLUG_A})

        async def resolver(tenant_id: int) -> str | None:
            return None

        service = TenantEnvelopeService(
            keys=h.repo,
            configs=h.repo,
            registry=h.providers.registry,
            platform_kek=lambda: h.kek,
            gate=h.gate,
            slug_resolver=resolver,
        )
        with pytest.raises(ExternalKmsNotEntitledError):
            await service.activate_external_kms(TENANT_A, tenant_slug="")

    async def test_no_resolver_and_no_slug_is_not_entitled(self, make_harness) -> None:
        h = make_harness(entitled={SLUG_A})
        with pytest.raises(ExternalKmsNotEntitledError):
            await h.service.configure_external_kms(
                TENANT_A,
                tenant_slug="",
                provider=PROVIDER_AWS,
                key_ref=AWS_KEY_ARN,
                region=None,
                principal=AWS_ROLE_ARN,
            )
        assert h.gate.asked == []  # an empty slug is refused without asking the licence system


class TestCorruptStores:
    """Bad rows in the key store are loud errors, not guesses."""

    async def test_an_unknown_kek_kind_is_a_loud_error(self, make_harness) -> None:
        h = make_harness()
        field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
        (row,) = await h.repo.list_keys(TENANT_A)
        h.repo.rows[TENANT_A][0] = replace(row, kek_kind="mystery")
        h.service.invalidate(TENANT_A)
        with pytest.raises(EnvelopeError, match="unknown kek_kind"):
            await h.service.decrypt(TENANT_A, field, **SLOT)

    async def test_a_destroyed_key_is_not_found_never_a_guess(self, make_harness) -> None:
        h = make_harness()
        field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
        (row,) = await h.repo.list_keys(TENANT_A)
        h.repo.rows[TENANT_A][0] = replace(row, status="destroyed", wrapped_dek=None)
        h.service.invalidate(TENANT_A)
        with pytest.raises(TenantKeyNotFoundError):
            await h.service.decrypt(TENANT_A, field, **SLOT)

    async def test_a_blob_the_provider_rejects_backs_off_and_does_not_flag_revocation(
        self, make_harness
    ) -> None:
        h = make_harness()
        await h.onboard(PROVIDER_AWS)
        field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
        (row,) = await h.repo.list_keys(TENANT_A)
        assert row.wrapped_dek is not None
        corrupt = row.wrapped_dek[:-1] + bytes([row.wrapped_dek[-1] ^ 1])
        h.repo.rows[TENANT_A][0] = replace(row, wrapped_dek=corrupt)
        h.service.invalidate(TENANT_A)
        with pytest.raises(TenantKeyUnavailableError) as raised:
            await h.service.decrypt(TENANT_A, field, **SLOT)
        assert raised.value.reason == "kms_rejected"
        traffic = len(h.aws.kms_calls("Decrypt"))
        with pytest.raises(TenantKeyUnavailableError):
            await h.service.decrypt(TENANT_A, field, **SLOT)  # blocked locally, no new KMS call
        assert len(h.aws.kms_calls("Decrypt")) == traffic
        assert (await h.repo.get(TENANT_A)).status == CONFIG_ACTIVE  # corruption != revocation


class TestBestEffortBookkeeping:
    """Flagging revoked/active and counting usage are never allowed to mask the real outcome."""

    async def test_a_failing_revoked_flag_does_not_mask_the_access_denied(
        self, make_harness, caplog
    ) -> None:
        h = make_harness()
        await h.onboard(PROVIDER_AWS)
        field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
        h.repo.fail_set_status = True
        h.aws.behavior.deny = True
        h.clock.advance(601)
        with caplog.at_level(logging.ERROR, logger="services.envelope.service"):
            with pytest.raises(TenantKeyUnavailableError) as raised:
                await h.service.decrypt(TENANT_A, field, **SLOT)
        assert raised.value.reason == "kms_access_denied"
        assert any(r.getMessage() == "envelope.kms.revoked_flag_failed" for r in caplog.records)

    async def test_a_failing_restore_flag_does_not_block_recovery(
        self, make_harness, caplog
    ) -> None:
        h = make_harness()
        await h.onboard(PROVIDER_AWS)
        field = await h.service.encrypt(TENANT_A, b"x", **SLOT)
        h.aws.behavior.deny = True
        h.clock.advance(601)
        with pytest.raises(TenantKeyUnavailableError):
            await h.service.decrypt(TENANT_A, field, **SLOT)
        assert (await h.repo.get(TENANT_A)).status == CONFIG_REVOKED
        h.aws.behavior.deny = False
        h.repo.fail_set_status = True
        h.clock.advance(60)
        with caplog.at_level(logging.ERROR, logger="services.envelope.service"):
            assert await h.service.decrypt(TENANT_A, field, **SLOT) == b"x"
        assert any(r.getMessage() == "envelope.kms.restore_flag_failed" for r in caplog.records)

    async def test_a_failing_usage_flush_is_retained_and_retried(
        self, make_harness, caplog
    ) -> None:
        h = make_harness(settings=EnvelopeSettings(usage_flush_batch=2, usage_cap=100))
        real = h.repo.add_usage
        state = {"fail": True}

        async def flaky(tenant_id: int, version: int, count: int) -> int:
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("db hiccup")
            return await real(tenant_id, version, count)

        h.repo.add_usage = flaky  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR, logger="services.envelope.service"):
            for _ in range(4):
                await h.service.encrypt(TENANT_A, b"x", **SLOT)  # none of them may fail
        assert any(r.getMessage() == "envelope.usage.flush_failed" for r in caplog.records)
        # The failed batch was carried forward, not dropped: flushed + still-pending == all four.
        flushed = sum(count for _, _, count in h.repo.usage_flushes)
        assert flushed + h.service._states[TENANT_A].pending_usage == 4
        assert flushed == 3

    async def test_a_failing_auto_rotation_does_not_fail_the_write(
        self, make_harness, caplog
    ) -> None:
        h = make_harness(settings=EnvelopeSettings(usage_flush_batch=1, usage_cap=1))

        async def broken_rotate(*args: object, **kwargs: object) -> None:
            raise RuntimeError("rotation store down")

        h.service.rotate_dek = broken_rotate  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR, logger="services.envelope.service"):
            assert await h.service.encrypt(TENANT_A, b"x", **SLOT)
        assert any(r.getMessage() == "envelope.key.auto_rotate_failed" for r in caplog.records)


class TestWritePathsDuringBackoff:
    async def test_new_writes_are_blocked_locally_during_a_denial_backoff(
        self, make_harness
    ) -> None:
        h = make_harness()
        await h.onboard(PROVIDER_AWS)
        await h.service.encrypt(TENANT_A, b"x", **SLOT)
        h.aws.behavior.deny = True
        h.clock.advance(601)
        with pytest.raises(TenantKeyUnavailableError):
            await h.service.encrypt(TENANT_A, b"x", **SLOT)
        traffic = len(h.aws.server.requests)
        with pytest.raises(TenantKeyUnavailableError) as raised:
            await h.service.encrypt(TENANT_A, b"x", **SLOT)
        assert raised.value.reason == "kms_access_denied"
        assert len(h.aws.server.requests) == traffic


class TestKeyCreationRaces:
    async def test_a_lost_creation_race_returns_the_winners_key(self, make_harness) -> None:
        h = make_harness()
        winner = await h.service.ensure_tenant_key(TENANT_B)

        class LosesTheRace(type(h.repo)):  # type: ignore[misc]
            async def insert_active(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
                raise KeyConflictError("lost")

        racer_repo = LosesTheRace()
        racer_repo.rows = h.repo.rows
        racer_repo.configs = h.repo.configs
        service = TenantEnvelopeService(
            keys=racer_repo,
            configs=racer_repo,
            registry=h.providers.registry,
            platform_kek=lambda: h.kek,
            gate=h.gate,
        )
        # get_active() sees the winner's row once the insert conflicts.
        assert (await service.ensure_tenant_key(TENANT_B)).id == winner.id

    async def test_a_conflict_with_no_winner_is_not_swallowed(self, make_harness) -> None:
        h = make_harness()

        async def always_conflicts(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise KeyConflictError("phantom")

        h.repo.insert_active = always_conflicts  # type: ignore[method-assign]
        with pytest.raises(KeyConflictError):
            await h.service.ensure_tenant_key(TENANT_A)


class TestRewrapAndStatus:
    async def test_rewrap_tenant_is_resumable(self, make_harness) -> None:
        h = make_harness()
        await h.service.encrypt(TENANT_A, b"x", **SLOT)
        await h.service.rotate_dek(TENANT_A)
        # An all-platform tenant is already "current": nothing to do, nothing failed.
        report = await h.service.rewrap_tenant(TENANT_A)
        assert (report.total, report.already_current, report.rewrapped) == (2, 2, 0)
        assert report.ok and report.target_kind == KEK_KIND_PLATFORM

    async def test_a_row_without_a_wrapped_dek_is_reported_failed_not_skipped(
        self, make_harness
    ) -> None:
        h = make_harness()
        await h.onboard(PROVIDER_AWS)
        await h.service.encrypt(TENANT_A, b"x", **SLOT)
        (row,) = await h.repo.list_keys(TENANT_A)
        h.repo.rows[TENANT_A][0] = replace(row, kek_kind=KEK_KIND_PLATFORM, wrapped_dek=None)
        report = await h.service.disable_external_kms(TENANT_A)
        assert not report.ok and report.failed_versions == (1,)

    async def test_status_summarises_keys_without_secrets(self, make_harness) -> None:
        h = make_harness()
        await h.onboard(PROVIDER_AWS)
        await h.service.encrypt(TENANT_A, b"x", **SLOT)
        status = await h.service.get_status(TENANT_A)
        assert status.config is not None and status.config.provider == PROVIDER_AWS
        assert [k.dek_version for k in status.keys] == [1]
        assert status.supported_providers == tuple(sorted(status.supported_providers))
        assert "wrapped" not in repr(status).lower() or "wrapped_dek" not in repr(status)
        assert (await h.service.get_status(TENANT_B)).config is None

    async def test_an_unentitled_gate_is_asked_about_the_right_tenant(self, make_harness) -> None:
        h = make_harness()
        h.gate = FakeGate()
        service = TenantEnvelopeService(
            keys=h.repo,
            configs=h.repo,
            registry=h.providers.registry,
            platform_kek=lambda: h.kek,
            gate=h.gate,
        )
        with pytest.raises(ExternalKmsNotEntitledError):
            await service.configure_external_kms(
                TENANT_A,
                tenant_slug="solo",
                provider=PROVIDER_AWS,
                key_ref=AWS_KEY_ARN,
                region=None,
                principal=AWS_ROLE_ARN,
            )
        assert h.gate.asked == ["solo"]
