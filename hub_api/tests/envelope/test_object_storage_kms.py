"""Object-storage SSE: AES256 baseline by default, entitled SSE-KMS on top, fail-loud otherwise."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from services import object_storage_kms, storage_service
from services.envelope import ExternalKmsNotEntitledError, KmsConfigError
from services.object_storage_kms import (
    SSE_BASELINE,
    SSE_KMS,
    ObjectStorageKmsSettings,
    ObjectStorageSse,
    get_object_storage_sse,
    reset_object_storage_sse,
)
from tests.envelope.conftest import FakeClock
from tests.envelope.fakes import FakeGate
from tests.envelope.kms_mocks import MockS3

#: tests/conftest.py stubs `write_bundle_sidecar` for every test (autouse); capture the real one
#: at import time so the on-the-wire tests below exercise it too.
_REAL_WRITE_BUNDLE_SIDECAR = storage_service.write_bundle_sidecar

KEY_ID = "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"
GCP_KEY_ID = "projects/p-one/locations/us/keyRings/r/cryptoKeys/k"


class TestSettings:
    """``OBJECT_STORAGE_KMS_*`` default OFF; a half-configured KMS mode never starts."""

    def test_off_by_default(self) -> None:
        settings = ObjectStorageKmsSettings.from_env({})
        assert settings.enabled is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
    def test_truthy_values_enable_it(self, value: str) -> None:
        env = {"OBJECT_STORAGE_KMS_ENABLED": value, "OBJECT_STORAGE_KMS_KEY_ID": KEY_ID}
        assert ObjectStorageKmsSettings.from_env(env).enabled is True

    @pytest.mark.parametrize("value", ["false", "0", "", "no", "maybe"])
    def test_anything_else_stays_off(self, value: str) -> None:
        assert (
            ObjectStorageKmsSettings.from_env({"OBJECT_STORAGE_KMS_ENABLED": value}).enabled
            is False
        )

    @pytest.mark.parametrize("key_id", ["", "has space", "bad;char", "x" * 600, "new\nline"])
    def test_enabled_requires_a_clean_key_id(self, key_id: str) -> None:
        with pytest.raises(KmsConfigError, match="OBJECT_STORAGE_KMS_KEY_ID"):
            ObjectStorageKmsSettings.from_env(
                {"OBJECT_STORAGE_KMS_ENABLED": "true", "OBJECT_STORAGE_KMS_KEY_ID": key_id}
            )

    def test_accepts_aws_arns_aliases_and_gcp_resource_names(self) -> None:
        for key_id in (KEY_ID, "alias/waddles-assets", GCP_KEY_ID):
            settings = ObjectStorageKmsSettings(enabled=True, key_id=key_id)
            assert settings.key_id == key_id

    def test_enabled_requires_an_entitlement_subject(self) -> None:
        with pytest.raises(KmsConfigError, match="OBJECT_STORAGE_KMS_TENANT"):
            ObjectStorageKmsSettings(enabled=True, key_id=KEY_ID, tenant_slug="")


class TestPutParams:
    """The decision every put_object goes through."""

    async def test_baseline_needs_no_gate_and_changes_nothing(self) -> None:
        gate = FakeGate()
        sse = ObjectStorageSse(ObjectStorageKmsSettings(), gate)
        assert await sse.put_params() == {"ServerSideEncryption": SSE_BASELINE}
        assert gate.asked == [] and sse.kms_mode is False

    async def test_entitled_kms_mode_returns_kms_params(self) -> None:
        gate = FakeGate(entitled={"system"})
        sse = ObjectStorageSse(ObjectStorageKmsSettings(enabled=True, key_id=KEY_ID), gate)
        assert await sse.put_params() == {"ServerSideEncryption": SSE_KMS, "SSEKMSKeyId": KEY_ID}
        assert gate.asked == ["system"] and sse.kms_mode is True

    async def test_unentitled_kms_mode_raises_instead_of_writing_under_the_platform_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """regression: external-kms -- no silent AES256 downgrade once KMS mode is requested."""
        sse = ObjectStorageSse(ObjectStorageKmsSettings(enabled=True, key_id=KEY_ID), FakeGate())
        with caplog.at_level(logging.ERROR, logger="services.object_storage_kms"):
            with pytest.raises(ExternalKmsNotEntitledError, match="instead"):
                await sse.put_params()
        assert any(getattr(r, "alert", None) == "object-storage-kms" for r in caplog.records)

    async def test_the_subject_tenant_is_configurable(self) -> None:
        gate = FakeGate(entitled={"acme"})
        settings = ObjectStorageKmsSettings(enabled=True, key_id=KEY_ID, tenant_slug="acme")
        assert (await ObjectStorageSse(settings, gate).put_params())["SSEKMSKeyId"] == KEY_ID
        assert gate.asked == ["acme"]

    async def test_positive_answers_are_cached_for_a_minute_then_rechecked(self) -> None:
        gate = FakeGate(entitled={"system"})
        clock = FakeClock()
        sse = ObjectStorageSse(
            ObjectStorageKmsSettings(enabled=True, key_id=KEY_ID), gate, clock=clock
        )
        for _ in range(5):
            await sse.put_params()
        assert gate.asked == ["system"]  # one licence lookup for a burst of uploads
        gate.entitled.clear()  # licence lapses
        await sse.put_params()  # still inside the 60s window: documented bounded staleness
        clock.advance(ObjectStorageSse.ENTITLEMENT_TTL_S + 1)
        with pytest.raises(ExternalKmsNotEntitledError):
            await sse.put_params()  # a lapse takes effect within a minute

    async def test_negative_answers_are_never_cached(self) -> None:
        gate = FakeGate()
        sse = ObjectStorageSse(ObjectStorageKmsSettings(enabled=True, key_id=KEY_ID), gate)
        with pytest.raises(ExternalKmsNotEntitledError):
            await sse.put_params()
        gate.entitled.add("system")  # licence arrives
        assert (await sse.put_params())["ServerSideEncryption"] == SSE_KMS


class TestStartupCheck:
    """The boot-time posture line; an unentitled KMS mode is an ERROR, never a crash."""

    async def test_baseline_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="services.object_storage_kms"):
            await ObjectStorageSse(ObjectStorageKmsSettings(), FakeGate()).startup_check()
        assert [r.getMessage() for r in caplog.records] == ["object_storage.sse.baseline"]

    async def test_entitled_kms_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        sse = ObjectStorageSse(
            ObjectStorageKmsSettings(enabled=True, key_id=KEY_ID), FakeGate(entitled={"system"})
        )
        with caplog.at_level(logging.INFO, logger="services.object_storage_kms"):
            await sse.startup_check()
        assert "object_storage.sse.kms_enabled" in [r.getMessage() for r in caplog.records]

    async def test_unentitled_kms_is_a_loud_error_but_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sse = ObjectStorageSse(ObjectStorageKmsSettings(enabled=True, key_id=KEY_ID), FakeGate())
        with caplog.at_level(logging.INFO, logger="services.object_storage_kms"):
            await sse.startup_check()
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert [r.getMessage() for r in errors] == ["object_storage.kms.not_entitled"]


class TestProcessSingleton:
    """``get_object_storage_sse`` reads the environment once and is replaceable in tests."""

    @pytest.fixture(autouse=True)
    def _isolate(self) -> Iterator[None]:
        reset_object_storage_sse()
        yield
        reset_object_storage_sse()

    def test_builds_from_the_environment_lazily_and_caches(self, monkeypatch) -> None:
        monkeypatch.setenv("OBJECT_STORAGE_KMS_ENABLED", "true")
        monkeypatch.setenv("OBJECT_STORAGE_KMS_KEY_ID", KEY_ID)
        first = get_object_storage_sse()
        assert first.kms_mode is True and get_object_storage_sse() is first

    def test_a_broken_kms_mode_fails_on_first_use_not_silently(self, monkeypatch) -> None:
        monkeypatch.setenv("OBJECT_STORAGE_KMS_ENABLED", "true")
        monkeypatch.delenv("OBJECT_STORAGE_KMS_KEY_ID", raising=False)
        with pytest.raises(KmsConfigError):
            get_object_storage_sse()

    def test_default_environment_is_the_baseline(self, monkeypatch) -> None:
        monkeypatch.delenv("OBJECT_STORAGE_KMS_ENABLED", raising=False)
        assert get_object_storage_sse().kms_mode is False
        assert object_storage_kms._instance is not None


@pytest.fixture
def s3(monkeypatch) -> Iterator[MockS3]:
    """A loopback S3 endpoint wired into storage_service through its real env settings."""
    mock = MockS3()
    monkeypatch.setenv("S3_ENDPOINT_URL", mock.url)
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AKTEST")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "sktest-secret")  # noqa: S105
    monkeypatch.setenv("S3_BUCKET_NAME", "assets")
    monkeypatch.setenv("BUNDLE_BUCKET_NAME", "bundles")
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", "http://cdn.test/assets")
    monkeypatch.setattr(storage_service, "write_bundle_sidecar", _REAL_WRITE_BUNDLE_SIDECAR)
    reset_object_storage_sse()
    yield mock
    reset_object_storage_sse()
    mock.stop()


def _sse_headers(request) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {
        k: v for k, v in request.headers.items() if k.startswith("x-amz-server-side-encryption")
    }


def _install_kms_mode(entitled: bool) -> None:
    gate = FakeGate(entitled={"system"} if entitled else set())
    reset_object_storage_sse(
        ObjectStorageSse(ObjectStorageKmsSettings(enabled=True, key_id=KEY_ID), gate)
    )


async def _all_writes() -> None:
    await storage_service.upload_avatar(b"img", "a.png", "image/png")
    await storage_service.upload_community_asset(
        b"img", "l.png", "image/png", folder="community-logos"
    )
    await storage_service.upload_bundle_component("waddles.x.y", "1.0.0", "ab" * 32, b"wasm")
    await storage_service.write_bundle_sidecar("waddles.x.y", "1.0.0", "ab" * 32, {"k": "v"})


class TestStorageServiceOnTheWire:
    """The headers SeaweedFS actually receives, from every one of hub-api's write paths."""

    async def test_baseline_writes_send_aes256_exactly_as_before(self, s3: MockS3) -> None:
        await _all_writes()
        puts = s3.puts()
        assert len(puts) == 5  # avatar, logo, component + sidecar, signed sidecar
        for request in puts:
            assert _sse_headers(request) == {"x-amz-server-side-encryption": "AES256"}

    async def test_entitled_kms_mode_sends_aws_kms_with_the_key_on_every_write(
        self, s3: MockS3
    ) -> None:
        _install_kms_mode(entitled=True)
        await _all_writes()
        puts = s3.puts()
        assert len(puts) == 5
        for request in puts:
            assert _sse_headers(request) == {
                "x-amz-server-side-encryption": "aws:kms",
                "x-amz-server-side-encryption-aws-kms-key-id": KEY_ID,
            }

    async def test_unentitled_kms_mode_writes_nothing_at_all(self, s3: MockS3) -> None:
        """regression: external-kms -- a refused KMS write must not fall back to AES256."""
        _install_kms_mode(entitled=False)
        with pytest.raises(ExternalKmsNotEntitledError):
            await storage_service.upload_avatar(b"img", "a.png", "image/png")
        with pytest.raises(ExternalKmsNotEntitledError):
            await storage_service.upload_bundle_component("waddles.x.y", "1.0.0", "ab" * 32, b"w")
        assert s3.puts() == []  # not a single object reached the store, under any key
