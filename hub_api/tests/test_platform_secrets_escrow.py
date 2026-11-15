"""Platform secret store T7: S3 escrow write, signed manifest, guarded restore, drift.

Unit tests over the in-memory repository of `test_platform_secrets_bootstrap` and an in-memory
object store. Every refusal has a positive control (the same flow succeeding) so a refusal is
proven to be the guard and not a broken fixture. Row counts are asserted after every refusal: a
refused restore must leave the table empty and generate nothing.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from services.envelope.platform_kek import PlatformKekAdapter
from services.platform_secrets import escrow as escrow_mod
from services.platform_secrets.bootstrap import (
    PlatformVaultRefusedError,
    bootstrap_platform_secrets,
)
from services.platform_secrets.escrow import (
    MANIFEST_KEY,
    EscrowMisconfiguredError,
    EscrowRefusedError,
    EscrowUnavailableError,
    S3EscrowStore,
    escrow_store_from_env,
    load_manifest,
    object_key,
    restore_from_escrow,
    restore_permitted,
    sync_escrow,
)
from services.platform_secrets.registry import REGISTRY, SecretType
from services.platform_secrets.repository import NewSecret
from services.provider_connections.vault import (
    generate_platform_key_material,
    open_platform_secret,
    platform_secret_fingerprint,
    seal_platform_secret,
)
from tests.test_platform_secrets_bootstrap import (  # noqa: F401 -- otel is a fixture
    KEK_A,
    KEK_B,
    NOW,
    FakeRepo,
    _counter,
    otel,
)

BUNDLE = int(SecretType.BUNDLE_SIGNING)
PRECOMPILE = int(SecretType.PRECOMPILE_SIGNING)


class MemStore:
    """In-memory `EscrowStore` whose failure mode and contents tests can poke at directly."""

    def __init__(self) -> None:
        """Start empty and healthy."""
        self.objects: dict[str, bytes] = {}
        self.down = False
        self.puts: list[str] = []

    def put(self, key: str, data: bytes) -> None:
        """Store an object (raises when `down`)."""
        if self.down:
            raise EscrowUnavailableError("down")
        self.puts.append(key)
        self.objects[key] = data

    def get(self, key: str) -> bytes | None:
        """Fetch an object (raises when `down`)."""
        if self.down:
            raise EscrowUnavailableError("down")
        return self.objects.get(key)


def _boot(repo: FakeRepo, store: MemStore | None = None, *, kek=KEK_A, allowed=True):
    """Bootstrap against `repo` with the escrow wired in."""
    return bootstrap_platform_secrets(
        repo, kek, environ={}, now=NOW, escrow=store, restore_allowed=allowed
    )


def _escrowed() -> tuple[FakeRepo, MemStore]:
    """A bootstrapped repo whose three secrets are escrowed."""
    repo, store = FakeRepo(), MemStore()
    _boot(repo, store)
    report = sync_escrow(repo, KEK_A, store, now=NOW)
    assert (report.status, report.written) == ("ok", 3)
    return repo, store


def _wiped_with_marker(repo: FakeRepo) -> FakeRepo:
    """A repo like `repo` after a DB wipe that left the marker (secrets and events gone)."""
    fresh = FakeRepo()
    fresh.marker = repo.marker
    return fresh


def _values(
    repo: FakeRepo, kek=KEK_A
) -> dict[tuple[int, int], tuple[bytes, bytes, str | None, int]]:
    """`(type, version) -> (plaintext, fingerprint-keyed-now, kid, state)` for every row."""
    out = {}
    for row in repo.secrets:
        plain = open_platform_secret(
            row.sealed_value,
            secret_type=row.secret_type,
            scope_id=row.scope_id,
            version=row.version,
            public_id=row.public_id,
            kek=kek,
        )
        out[(row.secret_type, row.version)] = (
            plain,
            platform_secret_fingerprint(plain, kek=kek),
            row.kid,
            row.state,
        )
    return out


def _add_v2(repo: FakeRepo, secret_type: int, now: datetime) -> None:
    """Rotate `secret_type` by hand: demote v1 to verify-only and insert an active v2."""
    spec = REGISTRY[SecretType(secret_type)]
    old = repo.rows_for(secret_type, None)[0]
    assert repo.update_state(old.id, 3, at=now, reason=4, actor_uuid=None)
    material = generate_platform_key_material(int(spec.algorithm))
    public_id = uuid.uuid4()
    sealed = seal_platform_secret(
        material.plaintext,
        secret_type=secret_type,
        scope_id=None,
        version=2,
        public_id=public_id,
        kek=KEK_A,
    )
    assert repo.insert_secret(
        NewSecret(
            public_id=public_id,
            secret_type=secret_type,
            scope_id=None,
            version=2,
            algorithm=int(spec.algorithm),
            sealed_value=sealed.sealed_value,
            kek_id=sealed.kek_id,
            seal_version=sealed.seal_version,
            kid=material.kid,
            public_material=material.public_material,
            fingerprint=platform_secret_fingerprint(material.plaintext, kek=KEK_A),
            reason=4,
            activated_at=now,
        )
    )


def _resign(store: MemStore, mutate, kek: PlatformKekAdapter = KEK_A) -> None:
    """Replace the manifest with a freshly SIGNED, mutated copy (an attacker holding the key)."""
    doc = json.loads(store.objects[MANIFEST_KEY])
    mutate(doc["manifest"])
    doc["kek_id"] = doc["manifest"]["kek_id"] = kek.kek_ids[0]
    mac = escrow_mod.sign_escrow_manifest(escrow_mod._canonical(doc["manifest"]), kek=kek)
    doc["mac"] = mac.hex()
    store.objects[MANIFEST_KEY] = json.dumps(doc).encode()


# ----------------------------------------------------------------------------------- write path


def test_bootstrap_escrows_every_secret_and_signs_a_manifest() -> None:
    repo, store = _escrowed()
    manifest = load_manifest(store, KEK_A)
    assert manifest is not None and manifest.generation == 1
    assert {e.secret_type for e in manifest.entries} == {1, 2, 3}
    for entry in manifest.entries:
        assert entry.key == object_key(entry.secret_type, None, entry.version)
        assert entry.key.startswith("platform/secrets-escrow/")
        assert entry.key in store.objects
        assert entry.fingerprint != b"\x00" * 32
    assert set(store.objects) == {MANIFEST_KEY} | {e.key for e in manifest.entries}
    # every escrow write extended the audit chain with an `escrowed` event
    assert sum(1 for e in repo.event_rows if e.event_type == 11) == 3


def test_sync_is_idempotent_and_never_rewrites_existing_objects() -> None:
    repo, store = _escrowed()
    before = dict(store.objects)
    store.puts.clear()
    report = sync_escrow(repo, KEK_A, store, now=NOW + timedelta(minutes=1))
    assert (report.status, report.written, report.drift) == ("ok", 0, 0)
    assert store.puts == []
    assert store.objects == before


def test_a_missing_object_is_rewritten_from_the_database() -> None:
    repo, store = _escrowed()
    manifest = load_manifest(store, KEK_A)
    assert manifest is not None
    victim = manifest.entries[0].key
    del store.objects[victim]
    report = sync_escrow(repo, KEK_A, store, now=NOW)
    assert report.status == "ok" and victim in store.objects


def test_nothing_in_the_escrow_is_plaintext() -> None:
    repo, store = _escrowed()
    for plain, _fp, _kid, _s in _values(repo).values():
        for blob in store.objects.values():
            assert plain not in blob
            assert base64.b64encode(plain) not in blob
    for key, blob in store.objects.items():
        if key != MANIFEST_KEY:
            assert blob[:3] == b"WPE"


def test_unreachable_store_never_fails_the_sync_and_retries_next_time() -> None:
    repo, store = FakeRepo(), MemStore()
    _boot(repo, store)
    store.down = True
    report = sync_escrow(repo, KEK_A, store, now=NOW)
    assert report.status == "unavailable"
    store.down = False
    assert sync_escrow(repo, KEK_A, store, now=NOW).written == 3


def test_write_failure_midway_keeps_the_manifest_consistent() -> None:
    repo, store = FakeRepo(), MemStore()
    _boot(repo, store)
    real_put = store.put
    calls = {"n": 0}

    def flaky(key: str, data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise EscrowUnavailableError("down")
        real_put(key, data)

    store.put = flaky  # type: ignore[method-assign]
    assert sync_escrow(repo, KEK_A, store, now=NOW).status == "unavailable"
    assert MANIFEST_KEY not in store.objects  # a manifest never lists an object that is not there
    store.put = real_put  # type: ignore[method-assign]
    assert sync_escrow(repo, KEK_A, store, now=NOW).status == "ok"


# ----------------------------------------------------------------------------------- restore


def test_restore_after_db_wipe_returns_the_same_keys() -> None:
    repo, store = _escrowed()
    want = _values(repo)
    fresh = _wiped_with_marker(repo)
    report = _boot(fresh, store)
    assert not report.first_run and report.generated == ()
    got = _values(fresh)
    assert got == want  # same plaintext, same fingerprints, same kid, same state
    assert fresh.rows_for(BUNDLE, None)[0].public_id == repo.rows_for(BUNDLE, None)[0].public_id
    assert sum(1 for e in fresh.event_rows if e.event_type == 3) == 3


def test_restore_keeps_a_rotated_history_and_the_kid() -> None:
    repo, store = FakeRepo(), MemStore()
    _boot(repo, store)
    _add_v2(repo, BUNDLE, NOW + timedelta(hours=1))
    sync_escrow(repo, KEK_A, store, now=NOW)
    want = _values(repo)
    fresh = _wiped_with_marker(repo)
    _boot(fresh, store)
    got = _values(fresh)
    assert got == want
    assert got[(BUNDLE, 1)][3] == 3 and got[(BUNDLE, 2)][3] == 2
    assert got[(BUNDLE, 2)][2] == repo.rows_for(BUNDLE, None)[1].kid


def test_restore_on_a_pristine_db_with_escrow_present_when_permitted() -> None:
    repo, store = _escrowed()
    want = _values(repo)
    pristine = FakeRepo()
    report = _boot(pristine, store)
    assert not report.first_run  # restored keys are used; nothing imported or generated
    assert report.generated == () and _values(pristine) == want


def test_restore_is_refused_when_not_permitted_and_generates_nothing() -> None:
    repo, store = _escrowed()
    for victim in (_wiped_with_marker(repo), FakeRepo()):
        with pytest.raises(PlatformVaultRefusedError) as err:
            _boot(victim, store, allowed=False)
        assert err.value.reason == "restore_blocked"
        assert victim.secrets == []  # nothing generated, nothing restored


def test_no_escrow_manifest_keeps_the_old_behaviour() -> None:
    repo, _ = _escrowed()
    wiped = _wiped_with_marker(repo)
    with pytest.raises(PlatformVaultRefusedError) as err:
        _boot(wiped, MemStore())
    assert err.value.reason == "store_wiped" and wiped.secrets == []
    first = FakeRepo()
    assert _boot(first, MemStore()).first_run and len(first.secrets) == 3  # control: generates


def test_unreachable_escrow_refuses_instead_of_generating() -> None:
    repo, store = _escrowed()
    store.down = True
    for victim in (_wiped_with_marker(repo), FakeRepo()):
        with pytest.raises(PlatformVaultRefusedError) as err:
            _boot(victim, store)
        assert err.value.reason == "escrow_unavailable" and victim.secrets == []


def test_restore_permission_policy() -> None:
    assert restore_permitted({"DEPLOYMENT_TIER": "alpha"})
    assert restore_permitted({"DEPLOYMENT_TIER": "local"})
    for tier in ("beta", "gamma", "production", ""):
        assert not restore_permitted({"DEPLOYMENT_TIER": tier})
        assert restore_permitted(
            {"DEPLOYMENT_TIER": tier, "WADDLES_PLATFORM_SECRETS_ESCROW_RESTORE": "1"}
        )
    assert not restore_permitted(
        {"DEPLOYMENT_TIER": "beta", "WADDLES_PLATFORM_SECRETS_ESCROW_RESTORE": "0"}
    )


# ----------------------------------------------------------------------------------- poisoning


def _refused_restore(store: MemStore, reason: str, kek=KEK_A) -> None:
    """Restoring into an empty repo refuses with `reason` and writes no row."""
    victim = FakeRepo()
    with pytest.raises(EscrowRefusedError) as err:
        restore_from_escrow(victim, kek, store, now=NOW)
    assert err.value.reason == reason
    assert victim.secrets == [] and victim.event_rows == []


def test_control_a_clean_escrow_restores() -> None:
    _, store = _escrowed()
    victim = FakeRepo()
    report = restore_from_escrow(victim, KEK_A, store, now=NOW)
    assert report is not None and report.restored == 3 and len(victim.secrets) == 3


def test_poisoned_object_swapped_with_another_secrets_valid_object_is_refused() -> None:
    _, store = _escrowed()
    one, two = object_key(BUNDLE, None, 1), object_key(PRECOMPILE, None, 1)
    store.objects[one], store.objects[two] = store.objects[two], store.objects[one]
    _refused_restore(store, "escrow_invalid")


def test_poisoned_object_with_attacker_bytes_is_refused() -> None:
    _, store = _escrowed()
    key = object_key(BUNDLE, None, 1)
    store.objects[key] = b"WPE\x01" + b"\x00" * 60
    _refused_restore(store, "escrow_invalid")
    store.objects[key] = b"not an envelope at all"
    _refused_restore(store, "escrow_invalid")


def test_flipped_ciphertext_bit_is_refused() -> None:
    _, store = _escrowed()
    key = object_key(BUNDLE, None, 1)
    blob = bytearray(store.objects[key])
    blob[-1] ^= 1
    store.objects[key] = bytes(blob)
    _refused_restore(store, "escrow_invalid")


def test_object_valid_for_an_older_version_cannot_stand_in_for_the_new_one() -> None:
    repo, store = FakeRepo(), MemStore()
    _boot(repo, store)
    _add_v2(repo, BUNDLE, NOW + timedelta(hours=1))
    sync_escrow(repo, KEK_A, store, now=NOW)
    v1, v2 = object_key(BUNDLE, None, 1), object_key(BUNDLE, None, 2)
    store.objects[v2] = store.objects[v1]  # an old (possibly compromised) key replayed as the new
    _refused_restore(store, "escrow_invalid")


def test_a_missing_listed_object_is_refused_not_skipped() -> None:
    _, store = _escrowed()
    del store.objects[object_key(PRECOMPILE, None, 1)]
    _refused_restore(store, "escrow_missing")


def test_unlisted_objects_are_ignored_even_if_valid_looking() -> None:
    _, store = _escrowed()
    store.objects[object_key(BUNDLE, None, 99)] = store.objects[object_key(BUNDLE, None, 1)]
    store.objects[object_key(BUNDLE, 7, 1)] = store.objects[object_key(BUNDLE, None, 1)]
    victim = FakeRepo()
    report = restore_from_escrow(victim, KEK_A, store, now=NOW)
    assert report is not None and report.restored == 3
    assert {(r.secret_type, r.version) for r in victim.secrets} == {(1, 1), (2, 1), (3, 1)}


# ----------------------------------------------------------------------------------- manifest


def test_manifest_byte_tamper_is_refused() -> None:
    _, store = _escrowed()
    doc = json.loads(store.objects[MANIFEST_KEY])
    doc["manifest"]["entries"][0]["fingerprint"] = "00" * 32
    store.objects[MANIFEST_KEY] = json.dumps(doc).encode()
    _refused_restore(store, "escrow_tampered")


def test_manifest_with_a_bad_mac_or_garbage_is_refused() -> None:
    _, store = _escrowed()
    doc = json.loads(store.objects[MANIFEST_KEY])
    doc["mac"] = "00" * 32
    store.objects[MANIFEST_KEY] = json.dumps(doc).encode()
    _refused_restore(store, "escrow_tampered")
    for junk in (b"", b"{}", b"[]", b"\xff\xfe", b'{"manifest": 1, "kek_id": "x", "mac": "zz"}'):
        store.objects[MANIFEST_KEY] = junk
        _refused_restore(store, "escrow_tampered")


def test_unsigned_manifest_injecting_an_extra_active_key_is_refused() -> None:
    _, store = _escrowed()
    doc = json.loads(store.objects[MANIFEST_KEY])
    forged = dict(doc["manifest"]["entries"][0], version=2, key=object_key(BUNDLE, None, 2))
    doc["manifest"]["entries"].append(forged)  # MAC unchanged => no longer valid
    store.objects[MANIFEST_KEY] = json.dumps(doc).encode()
    _refused_restore(store, "escrow_tampered")


def test_validly_signed_but_inconsistent_manifests_are_invalid() -> None:
    _, store = _escrowed()
    pristine = store.objects[MANIFEST_KEY]

    def duplicate(body: dict) -> None:
        body["entries"].append(dict(body["entries"][0]))

    def two_active(body: dict) -> None:
        extra = dict(body["entries"][0], version=2, key=object_key(BUNDLE, None, 2))
        body["entries"].append(extra)

    def wrong_key(body: dict) -> None:
        body["entries"][0]["key"] = "platform/secrets-escrow/../../bundles/x.sealed"

    def wrong_algorithm(body: dict) -> None:
        body["entries"][0]["algorithm"] = 3

    def unregistered(body: dict) -> None:
        body["entries"][0]["type"] = 999

    def scope_on_global(body: dict) -> None:
        body["entries"][0]["scope"] = 0

    def bool_as_int(body: dict) -> None:
        body["entries"][0]["version"] = True

    for mutate in (
        duplicate,
        two_active,
        wrong_key,
        wrong_algorithm,
        unregistered,
        scope_on_global,
        bool_as_int,
    ):
        store.objects[MANIFEST_KEY] = pristine
        _resign(store, mutate)
        _refused_restore(store, "escrow_invalid")


def test_wrong_kek_is_refused_at_every_layer() -> None:
    _, store = _escrowed()
    _refused_restore(store, "escrow_tampered", kek=KEK_B)  # manifest signed under another root
    repo = FakeRepo()
    with pytest.raises(PlatformVaultRefusedError) as err:
        _boot(repo, store, kek=KEK_B)  # pristine DB, new root, stale escrow: refuse, never generate
    assert err.value.reason == "escrow_tampered" and repo.secrets == []
    # an object sealed under another KEK behind a manifest that vouches for it
    other = FakeRepo()
    other_store = MemStore()
    _boot(other, other_store, kek=KEK_B)
    sync_escrow(other, KEK_B, other_store, now=NOW)
    mixed = MemStore()
    mixed.objects = dict(store.objects)
    key = object_key(BUNDLE, None, 1)
    mixed.objects[key] = other_store.objects[key]
    _refused_restore(mixed, "escrow_invalid")


def test_previous_kek_in_a_rotation_window_still_restores() -> None:
    _, store = _escrowed()
    rotating = PlatformKekAdapter(bytes.fromhex("c3" * 32), previous=bytes.fromhex("a1" * 32))
    victim = FakeRepo()
    report = restore_from_escrow(victim, rotating, store, now=NOW)
    assert report is not None and report.restored == 3
    # re-sealed under the CURRENT (new) KEK, not left under the old one
    assert {r.kek_id for r in victim.secrets} == {rotating.kek_ids[0]}


# ----------------------------------------------------------------------------------- drift


def test_tampered_remote_manifest_is_alerted_never_overwritten(otel) -> None:  # noqa: F811
    repo, store = _escrowed()
    store.objects[MANIFEST_KEY] = b"{}"
    report = sync_escrow(repo, KEK_A, store, now=NOW)
    assert report.status == "blocked_tampered" and store.objects[MANIFEST_KEY] == b"{}"
    drift = _counter(otel["reader"], "waddles_platform_secrets_drift_detected_total")
    assert sum(v for k, v in drift.items() if ("outcome", "escrow_tampered") in k) == 3


def test_escrow_ahead_of_the_database_is_alerted_never_overwritten(otel) -> None:  # noqa: F811
    repo, store = FakeRepo(), MemStore()
    _boot(repo, store)
    _add_v2(repo, BUNDLE, NOW + timedelta(hours=1))
    sync_escrow(repo, KEK_A, store, now=NOW)
    manifest_before = store.objects[MANIFEST_KEY]
    rolled_back = FakeRepo()
    rolled_back.marker, rolled_back.event_rows = repo.marker, []
    # a DB snapshot from before the rotation: v1 still active
    rolled_back.secrets = [replace(r, state=2) for r in repo.secrets if r.version == 1]
    report = sync_escrow(rolled_back, KEK_A, store, now=NOW)
    assert report.status == "blocked_ahead"
    assert store.objects[MANIFEST_KEY] == manifest_before
    drift = _counter(otel["reader"], "waddles_platform_secrets_drift_detected_total")
    assert sum(v for k, v in drift.items() if ("outcome", "escrow_ahead") in k) >= 1


def test_value_drift_between_db_and_escrow_keeps_the_remote_entry(otel) -> None:  # noqa: F811
    repo, store = _escrowed()
    key = object_key(BUNDLE, None, 1)
    row = repo.rows_for(BUNDLE, None)[0]
    other = generate_platform_key_material(row.algorithm)
    sealed = seal_platform_secret(
        other.plaintext,
        secret_type=BUNDLE,
        scope_id=None,
        version=1,
        public_id=row.public_id,
        kek=KEK_A,
    )
    repo.secrets[repo.secrets.index(row)] = replace(row, sealed_value=sealed.sealed_value)
    before = store.objects[key]
    report = sync_escrow(repo, KEK_A, store, now=NOW)
    assert report.drift == 1 and store.objects[key] == before
    drift = _counter(otel["reader"], "waddles_platform_secrets_drift_detected_total")
    assert sum(v for k, v in drift.items() if ("outcome", "escrow_drift") in k) == 1


# ----------------------------------------------------------------------------------- hygiene


def test_no_secret_shaped_string_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    repo, store = _escrowed()
    store.objects[MANIFEST_KEY] = b"{}"
    sync_escrow(repo, KEK_A, store, now=NOW)
    _wiped = _wiped_with_marker(repo)
    secrets_ = [p for (p, _f, _k, _s) in _values(repo).values()]
    text = caplog.text
    for plain in secrets_:
        assert base64.b64encode(plain).decode() not in text
        assert plain.hex() not in text
        assert plain.decode("latin-1") not in text or len(plain) < 8


def test_s3_store_maps_errors_without_leaking_messages() -> None:
    from botocore.exceptions import ClientError

    class Body:
        def read(self) -> bytes:
            return b"data"

    class Client:
        def __init__(self, error: str | None) -> None:
            self.error = error

        def put_object(self, **kw: object) -> None:
            if self.error:
                raise ClientError({"Error": {"Code": self.error, "Message": "SECRET-LEAK"}}, "Put")
            self.kw = kw

        def get_object(self, **kw: object) -> dict:
            if self.error:
                raise ClientError({"Error": {"Code": self.error, "Message": "SECRET-LEAK"}}, "Get")
            return {"Body": Body()}

    ok = S3EscrowStore(Client(None), "b")
    ok.put("k", b"v")
    assert ok.get("k") == b"data"
    assert S3EscrowStore(Client("NoSuchKey"), "b").get("k") is None
    for code in ("AccessDenied", "InternalError"):
        with pytest.raises(EscrowUnavailableError) as err:
            S3EscrowStore(Client(code), "b").get("k")
        assert "SECRET-LEAK" not in str(err.value) and err.value.__cause__ is None
        with pytest.raises(EscrowUnavailableError):
            S3EscrowStore(Client(code), "b").put("k", b"v")


def test_escrow_store_from_env() -> None:
    assert escrow_store_from_env({}) is None  # unconfigured: skipped
    with pytest.raises(EscrowMisconfiguredError):  # bucket without credentials: loud
        escrow_store_from_env({"PLATFORM_ESCROW_S3_BUCKET": "b"})
    store = escrow_store_from_env(
        {
            "PLATFORM_ESCROW_S3_BUCKET": "b",
            "PLATFORM_ESCROW_S3_ACCESS_KEY_ID": "AKIAEXAMPLE0000000",
            "PLATFORM_ESCROW_S3_SECRET_ACCESS_KEY": "x" * 40,
            "S3_ENDPOINT_URL": "https://s3.example.invalid:8333",
        }
    )
    assert isinstance(store, S3EscrowStore)


def test_object_keys_are_canonical() -> None:
    assert object_key(1, None, 3) == "platform/secrets-escrow/1/global/3.sealed"
    assert object_key(3, 0, 1) == "platform/secrets-escrow/3/0/1.sealed"
