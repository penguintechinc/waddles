"""Offline signers (core-bundle seeder, signing backfill) load platform_vault themselves.

Regression: alpha deploy run 43819105673 (2026-11-16). T4 moved artifact signing to
`PlatformSigner.from_vault()`, which reads the platform-secret cache of the CURRENT process. Only
hub-api's bootstrap seeds that cache, so the seeder Job's cache was always empty and 145/164 seeds
failed `platform_vault has no active bundle_signing key`. `load_offline_signer_cache` now reads the
vault's ACTIVE key by its public columns (no KEK in those pods) and accepts the Job's bundle-signing
Secret only when it IS that key; both CLIs call it before signing. Fail closed otherwise.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from cli import seed_core_bundles as seeder
from cli import sign_approved_bundles as backfill
from services.bundle_signing_service import PlatformSigner, build_signing_payload
from services.platform_secrets import bootstrap as vault_bootstrap
from services.platform_secrets import telemetry
from services.platform_secrets.bootstrap import PlatformVaultRefusedError
from services.platform_secrets.cache import NoActiveSecretError, PlatformSecretCache
from tests.test_platform_secrets_bootstrap import (
    GOOD_SEED,
    FakeRepo,
    _b64,
    _boot,
    _counter,
)

#: The real function, captured before conftest's autouse stub replaces the module attribute.
_REAL_LOAD = vault_bootstrap.load_offline_signer_cache


@pytest.fixture
def otel(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Private in-memory metric capture wired into the platform-secret telemetry module."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    telemetry.reset_for_tests()
    monkeypatch.setattr(telemetry.metrics, "get_meter", lambda *_a, **_k: provider.get_meter("t"))
    yield {"reader": reader}
    telemetry.reset_for_tests()


OTHER_SEED = bytes((i * 53 + 7) % 256 for i in range(32))  # a second distinct test-only seed


def _legacy_env(seed: bytes = GOOD_SEED, kid: str = "waddlebot-1") -> dict[str, str]:
    """The chart's legacy bundle-signing Secret as env (test-only seed)."""
    return {"BUNDLE_SIGNING_PRIVATE_KEY": _b64(seed), "BUNDLE_SIGNING_KEY_ID": kid}


def _engine_over(repo: FakeRepo, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """An engine whose `connect()` yields a connection the loader turns into `repo`."""
    monkeypatch.setattr(vault_bootstrap, "SqlRepository", lambda _conn: repo)
    engine = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = MagicMock()
    ctx.__exit__.return_value = False
    engine.connect.return_value = ctx
    return engine


def _load(repo_engine: MagicMock, cache: PlatformSecretCache, environ: dict[str, str]) -> int:
    return _REAL_LOAD(
        "postgresql://unused",
        environ=environ,
        kill_switch=lambda: False,
        cache=cache,
        engine=repo_engine,
    )


def test_seeder_process_cache_is_empty_without_the_offline_load() -> None:
    """The bug: a fresh process cache (seeder pod) has no key -> signing fails closed."""
    from services.errors import ApiError

    with pytest.raises(ApiError) as exc:
        PlatformSigner.from_vault(cache=PlatformSecretCache())
    assert exc.value.code == "artifact_signing_key_unavailable"


def test_offline_load_opens_the_imported_legacy_key_and_signs_verifiably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hub-api imported the legacy key; the seeder loads it and signs what executors verify."""
    repo = FakeRepo()
    report = _boot(repo, environ=_legacy_env())
    assert report.imported == ("bundle_signing",)
    cache = PlatformSecretCache()
    loaded = _load(_engine_over(repo, monkeypatch), cache, _legacy_env())
    assert loaded == 1  # only the active bundle_signing key, nothing else opened

    signer = PlatformSigner.from_vault(cache=cache)
    assert signer.key_id == "waddlebot-1"
    sig_b64, key_id = signer.sign(
        app_id="penguintechinc.waddles.x", version="1.0.0", digest="ab" * 32, approval_id=7
    )
    legacy_public = Ed25519PrivateKey.from_private_bytes(GOOD_SEED).public_key()
    legacy_public.verify(  # raises InvalidSignature if executor trust would reject it
        base64.b64decode(sig_b64),
        build_signing_payload(
            app_id="penguintechinc.waddles.x", version="1.0.0", digest="ab" * 32, approval_id=7
        ),
    )
    assert key_id == "waddlebot-1"


@pytest.mark.parametrize(
    "environ",
    [{}, {"BUNDLE_SIGNING_PRIVATE_KEY": "REPLACE_ME", "BUNDLE_SIGNING_KEY_ID": "waddlebot-1"}],
)
def test_no_usable_signing_secret_refuses(
    environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Job holds no KEK, so without the Secret's private half it cannot sign: refuse."""
    repo = FakeRepo()
    _boot(repo, environ=_legacy_env())
    cache = PlatformSecretCache()
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _load(_engine_over(repo, monkeypatch), cache, environ)
    assert exc.value.reason == "signing_key_unavailable"
    with pytest.raises(NoActiveSecretError):  # nothing half-seeded
        cache.active(1)


def test_kill_switch_on_touches_nothing() -> None:
    """Kill-switch ON: the legacy env signer is authoritative, no DB / KEK access."""
    engine = MagicMock()
    cache = PlatformSecretCache()
    n = _REAL_LOAD(
        "postgresql://unused", environ={}, kill_switch=lambda: True, cache=cache, engine=engine
    )
    assert n == 0
    engine.connect.assert_not_called()


def test_the_offline_path_never_needs_or_reads_the_kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """No TENANT_KEK_HEX anywhere and nothing is opened: the KEK stays inside hub-api."""
    repo = FakeRepo()
    _boot(repo, environ=_legacy_env())
    opened: list[int] = []
    monkeypatch.setattr(
        vault_bootstrap, "open_platform_secret", lambda *a, **k: opened.append(1) or b""
    )
    env = _legacy_env()
    assert "TENANT_KEK_HEX" not in env
    assert _load(_engine_over(repo, monkeypatch), PlatformSecretCache(), env) == 1
    assert opened == []


def test_empty_vault_refuses_instead_of_signing_nothing(
    monkeypatch: pytest.MonkeyPatch,
    otel: dict[str, Any],
) -> None:
    """No active bundle_signing row (hub-api never bootstrapped): refuse, never generate here."""
    repo = FakeRepo()
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _load(_engine_over(repo, monkeypatch), PlatformSecretCache(), _legacy_env())
    assert exc.value.reason == "signing_key_missing"
    assert repo.secrets == []  # the offline path never writes


def test_vault_key_the_executors_cannot_verify_refuses(
    monkeypatch: pytest.MonkeyPatch,
    otel: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Vault key != legacy Secret key (executor configmap trust): fail closed + drift metric."""
    repo = FakeRepo()
    _boot(repo, environ=_legacy_env(OTHER_SEED, "old-kid"))
    with caplog.at_level(logging.DEBUG), pytest.raises(PlatformVaultRefusedError) as exc:
        _load(_engine_over(repo, monkeypatch), PlatformSecretCache(), _legacy_env(GOOD_SEED))
    assert exc.value.reason == "trust_mismatch"
    drift = _counter(otel["reader"], "waddles_platform_secrets_drift_detected_total")
    assert drift == {(("outcome", "mismatch"), ("type", "bundle_signing")): 1}
    for seed in (GOOD_SEED, OTHER_SEED):  # no key material in any log line
        assert _b64(seed) not in caplog.text and seed.hex() not in caplog.text


def test_offline_load_metric_and_log_carry_counts_not_values(
    monkeypatch: pytest.MonkeyPatch,
    otel: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = FakeRepo()
    _boot(repo, environ=_legacy_env())
    with caplog.at_level(logging.DEBUG):
        _load(_engine_over(repo, monkeypatch), PlatformSecretCache(), _legacy_env())
    loads = _counter(otel["reader"], "waddles_platform_secrets_offline_loads_total")
    assert loads == {(("outcome", "ok"), ("type", "all")): 1}
    assert (
        "platform secret cache loaded for offline signing type=bundle_signing version=1 count=1"
        in caplog.text
    )
    assert _b64(GOOD_SEED) not in caplog.text


async def test_seeder_run_loads_the_vault_before_any_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run` primes the cache first; a refusal aborts the Job before any bundle is touched."""
    calls: list[str] = []

    def _refuse(database_url: str, **_kw: Any) -> int:
        calls.append(database_url)
        raise PlatformVaultRefusedError("kek_missing")

    monkeypatch.setattr(vault_bootstrap, "load_offline_signer_cache", _refuse)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    build = AsyncMock(side_effect=AssertionError("install DAL built before the vault load"))
    monkeypatch.setattr(seeder, "build_install_dal", build)
    with pytest.raises(PlatformVaultRefusedError):
        await seeder._run(MagicMock(), MagicMock())
    assert calls and calls[0].startswith("postgresql://")
    build.assert_not_called()


async def test_backfill_run_loads_the_vault_before_any_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _refuse(database_url: str, **_kw: Any) -> int:
        calls.append(database_url)
        raise PlatformVaultRefusedError("kek_missing")

    monkeypatch.setattr(vault_bootstrap, "load_offline_signer_cache", _refuse)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    build = AsyncMock(side_effect=AssertionError("install DAL built before the vault load"))
    monkeypatch.setattr(backfill, "build_install_dal", build)
    with pytest.raises(PlatformVaultRefusedError):
        await backfill._run()
    assert calls
    build.assert_not_called()
