"""Art. 17 erasure of the non-Postgres stores: Valkey bundle KV and SeaweedFS/S3 objects.

Unit tests drive `erase_kv_keys` / `delete_owned_objects_strict` / `EnvExternalErasure` against
doubles (`tests/erasure_support.py`) with REAL glob matching for the KV namespace. A real-Valkey
test (`WADDLES_TEST_VALKEY_URL`, skipped otherwise) proves the SCAN pattern against the actual
server, because glob semantics are the one thing a dict-backed fake could get subtly wrong.
"""

from __future__ import annotations

import os
import uuid as uuidlib
from typing import Any

import pytest

from services import erasure_external as external
from services import storage_service
from tests.erasure_support import FakeRedis, FakeS3

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
OTHER = "99999999-9999-4999-8999-999999999999"
PREFIX = "bundlekv:acme:main:waddles.bot.counter"


def _data(key: str) -> str:
    return f"{PREFIX}:data:{key}"


class TestKvErasure:
    """Per-user bundle state is found by the subject's uuid token in the guest key."""

    def test_removes_only_keys_naming_the_subject(self) -> None:
        redis = FakeRedis(
            {
                _data(f"lastseen.{U1}"): "2026-10-01",
                _data(f"cooldown.{U2}"): "1",
                _data(f"lastseen.{OTHER}"): "2026-10-02",
                _data("global.counter"): "5",
            }
        )
        assert external.erase_kv_keys(redis, [U1, U2]) == 2
        assert set(redis.data) == {_data(f"lastseen.{OTHER}"), _data("global.counter")}

    def test_matches_the_hex_spelling_too(self) -> None:
        redis = FakeRedis({_data(f"user.{uuidlib.UUID(U1).hex}"): "x", _data("keep"): "y"})
        assert external.erase_kv_keys(redis, [U1]) == 1
        assert set(redis.data) == {_data("keep")}

    def test_tenant_wide_and_per_community_namespaces_are_both_covered(self) -> None:
        redis = FakeRedis(
            {
                f"bundlekv:acme:_tenant:app.a:data:u.{U1}": "1",
                f"bundlekv:globex:community-9:app.b:data:u.{U1}": "1",
            }
        )
        assert external.erase_kv_keys(redis, [U1]) == 2
        assert redis.data == {}

    def test_other_key_families_are_never_touched(self) -> None:
        redis = FakeRedis(
            {
                f"{PREFIX}:count": 3,  # the quota counter is not a data key
                f"spine:stream:{U1}": "x",  # another subsystem that happens to name the uuid
                f"bundlekv:acme:main:app:opr:{U1}": "x",  # rate key: no ':data:' segment
            }
        )
        assert external.erase_kv_keys(redis, [U1]) == 0
        assert len(redis.data) == 3

    def test_the_per_app_key_counter_is_lowered(self) -> None:
        redis = FakeRedis(
            {
                f"{PREFIX}:count": 4,
                _data(f"a.{U1}"): "1",
                _data(f"b.{U1}"): "1",
                _data("c"): "1",
                _data("d"): "1",
            }
        )
        external.erase_kv_keys(redis, [U1])
        assert redis.data[f"{PREFIX}:count"] == 2

    def test_a_counter_that_would_go_negative_is_dropped_for_the_host_to_reconcile(self) -> None:
        redis = FakeRedis({f"{PREFIX}:count": 1, _data(f"a.{U1}"): "1", _data(f"b.{U1}"): "1"})
        external.erase_kv_keys(redis, [U1])
        assert f"{PREFIX}:count" not in redis.data

    def test_an_absent_counter_is_left_absent(self) -> None:
        redis = FakeRedis({_data(f"a.{U1}"): "1"})
        external.erase_kv_keys(redis, [U1])
        assert redis.data == {}

    def test_a_value_that_is_not_a_uuid_is_never_used_as_a_glob(self) -> None:
        redis = FakeRedis({_data("anything"): "1"})
        assert external.erase_kv_keys(redis, ["*", "a?b", "[x]", ""]) == 0
        assert _data("anything") in redis.data

    def test_no_uuids_scans_nothing(self) -> None:
        redis = FakeRedis({_data(f"a.{U1}"): "1"})
        assert external.erase_kv_keys(redis, []) == 0
        assert len(redis.data) == 1


class TestObjectStorage:
    """Only OUR objects are deleted, and a failure is never swallowed."""

    @pytest.fixture
    def s3(self, monkeypatch: pytest.MonkeyPatch) -> FakeS3:
        fake = FakeS3()
        monkeypatch.setattr(storage_service, "_client", lambda: fake)
        monkeypatch.setenv("S3_PUBLIC_BASE_URL", "https://assets.example/waddlebot-assets")
        monkeypatch.setenv("S3_BUCKET_NAME", "waddlebot-assets")
        return fake

    def test_deletes_our_objects_and_skips_foreign_urls(self, s3: FakeS3) -> None:
        urls = [
            "https://assets.example/waddlebot-assets/avatars/a.png",
            "https://cdn.discordapp.com/avatars/1/x.png",  # a platform CDN: not ours to delete
        ]
        assert storage_service.delete_owned_objects_strict(urls) == 1
        assert s3.deleted == [("waddlebot-assets", "avatars/a.png")]

    def test_query_string_and_fragment_are_not_part_of_the_key(self, s3: FakeS3) -> None:
        url = "https://assets.example/waddlebot-assets/avatars/a.png?v=3#frag"
        assert storage_service.delete_owned_objects_strict([url]) == 1
        assert s3.deleted == [("waddlebot-assets", "avatars/a.png")]

    def test_a_path_traversal_url_is_ignored(self, s3: FakeS3) -> None:
        url = "https://assets.example/waddlebot-assets/../other-bucket/secret"
        assert storage_service.delete_owned_objects_strict([url]) == 0
        assert s3.deleted == []

    def test_a_failed_delete_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(storage_service, "_client", lambda: FakeS3(RuntimeError("s3 down")))
        monkeypatch.setenv("S3_PUBLIC_BASE_URL", "https://assets.example/waddlebot-assets")
        with pytest.raises(RuntimeError, match="s3 down"):
            storage_service.delete_owned_objects_strict(
                ["https://assets.example/waddlebot-assets/avatars/a.png"]
            )

    def test_nothing_to_delete_never_builds_a_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Any:
            raise AssertionError("no URL -> no S3 client")

        monkeypatch.setattr(storage_service, "_client", boom)
        monkeypatch.setenv("S3_PUBLIC_BASE_URL", "https://assets.example/waddlebot-assets")
        assert storage_service.delete_owned_objects_strict([]) == 0
        assert storage_service.delete_owned_objects_strict(["https://cdn.example/x.png"]) == 0


class TestEnvExternalErasure:
    """The production default reads its Valkey from the environment and fails loud."""

    def test_deletes_objects_and_kv_and_reports_non_zero_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        redis = FakeRedis({_data(f"a.{U1}"): "1"})
        monkeypatch.setenv("VALKEY_URL", "redis://valkey:6379/0")
        monkeypatch.setattr(
            storage_service, "delete_owned_objects_strict", lambda urls: len(list(urls))
        )
        import redis as redis_module

        monkeypatch.setattr(redis_module.Redis, "from_url", staticmethod(lambda *_a, **_k: redis))
        counts = external.EnvExternalErasure().erase(object_urls=["u1", "u2"], user_uuids=[U1])
        assert counts == {"objects_deleted": 2, "kv_keys_deleted": 1}

    def test_an_unconfigured_valkey_is_skipped_with_a_warning_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("VALKEY_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        counts = external.EnvExternalErasure().erase(object_urls=[], user_uuids=[U1])
        assert counts == {}
        assert any(r.message == "dsar.kv_erasure_skipped_unconfigured" for r in caplog.records)

    def test_redis_url_is_the_fallback_for_valkey_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VALKEY_URL", raising=False)
        monkeypatch.setenv("REDIS_URL", "redis://fallback:6379/0")
        seen: list[str] = []
        import redis as redis_module

        def from_url(url: str, **_kw: Any) -> FakeRedis:
            seen.append(url)
            return FakeRedis()

        monkeypatch.setattr(redis_module.Redis, "from_url", staticmethod(from_url))
        external.EnvExternalErasure().erase(object_urls=[], user_uuids=[U1])
        assert seen == ["redis://fallback:6379/0"]

    def test_a_valkey_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VALKEY_URL", "redis://valkey:6379/0")
        import redis as redis_module

        def from_url(*_a: Any, **_k: Any) -> Any:
            raise ConnectionError("valkey down")

        monkeypatch.setattr(redis_module.Redis, "from_url", staticmethod(from_url))
        with pytest.raises(ConnectionError):
            external.EnvExternalErasure().erase(object_urls=[], user_uuids=[U1])

    def test_no_uuids_means_no_valkey_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VALKEY_URL", "redis://valkey:6379/0")
        import redis as redis_module

        def from_url(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("must not connect")

        monkeypatch.setattr(redis_module.Redis, "from_url", staticmethod(from_url))
        assert external.EnvExternalErasure().erase(object_urls=[], user_uuids=[]) == {}


@pytest.mark.skipif(
    not os.getenv("WADDLES_TEST_VALKEY_URL"), reason="WADDLES_TEST_VALKEY_URL not set"
)
class TestRealValkey:
    """The actual SCAN/UNLINK/DECRBY semantics against a live server (unique key namespace)."""

    def test_scan_pattern_finds_and_removes_exactly_the_subjects_keys(self) -> None:
        import redis

        client = redis.Redis.from_url(os.environ["WADDLES_TEST_VALKEY_URL"])
        run = uuidlib.uuid4().hex[:12]
        prefix = f"bundlekv:erasure-test-{run}:_tenant:app.x"
        subject, other = str(uuidlib.uuid4()), str(uuidlib.uuid4())
        keys = {
            f"{prefix}:data:lastseen.{subject}": "1",
            f"{prefix}:data:cooldown.{uuidlib.UUID(subject).hex}": "1",
            f"{prefix}:data:lastseen.{other}": "1",
            f"{prefix}:count": "3",
        }
        try:
            for key, value in keys.items():
                client.set(key, value)
            assert external.erase_kv_keys(client, [subject]) == 2
            remaining = {k.decode() for k in client.scan_iter(match=f"{prefix}:*")}
            assert remaining == {f"{prefix}:data:lastseen.{other}", f"{prefix}:count"}
            assert client.get(f"{prefix}:count") == b"1"
        finally:
            for key in client.scan_iter(match=f"bundlekv:erasure-test-{run}:*"):
                client.delete(key)
            client.close()
