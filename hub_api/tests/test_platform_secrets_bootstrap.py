"""Platform secret store T3: registry, importer, bootstrap decision table, cache, refresher, leaks.

Pure unit tests over an in-memory repository that enforces the same constraints the migration does
(unique (type, scope, version), one active per (type, scope), once-only marker, linear event chain),
so the decision logic is proven without Postgres. The SQL itself is proven on real Postgres in
`test_platform_secrets_pg_integration.py`. Every negative test has a positive control next to it,
so a refusal is proven to be the guard and not a broken fixture.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from services.envelope.platform_kek import PlatformKekAdapter
from services.platform_secrets import bootstrap as vault_bootstrap
from services.platform_secrets import registry, telemetry
from services.platform_secrets.bootstrap import (
    PlatformVaultRefusedError,
    bootstrap_platform_secrets,
    db_platform_keys_disabled,
    run_platform_secrets_bootstrap,
)
from services.platform_secrets.cache import (
    CachedSecret,
    NoActiveSecretError,
    PlatformSecretCache,
    UnknownKeyIdError,
    verify_until_for,
)
from services.platform_secrets.event_chain import append_event, verify_chain
from services.platform_secrets.importer import import_candidate
from services.platform_secrets.refresher import CacheRefresher
from services.platform_secrets.registry import REGISTRY, SecretType
from services.platform_secrets.repository import (
    AnchorRow,
    EventRow,
    MarkerRow,
    NewSecret,
    SecretRow,
)
from services.provider_connections.vault import (
    PLATFORM_ALG_ED25519,
    PLATFORM_ALG_ES256,
    PLATFORM_ALG_PASSWORD,
    PLATFORM_ALG_RANDOM_256,
    generate_platform_key_material,
    platform_key_material_from_private,
)

KEK_A = PlatformKekAdapter(bytes.fromhex("a1" * 32))
KEK_B = PlatformKekAdapter(bytes.fromhex("b2" * 32))
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
ALL_NAMES = {s.name for s in REGISTRY.values()}


class FakeRepo:
    """In-memory `PlatformVaultRepository` mirroring the migration's constraints (thread-safe)."""

    def __init__(self) -> None:
        """Start empty: no marker, no secrets, no events."""
        self.marker: MarkerRow | None = None
        self.secrets: list[SecretRow] = []
        self.event_rows: list[EventRow] = []
        self.notifies = 0
        self.claimed: set[uuid.UUID] = set()
        self.anchor: AnchorRow | None = None
        self.before_insert: Any = None
        self._lock = threading.RLock()

    def read_marker(self) -> MarkerRow | None:
        """The marker."""
        return self.marker

    def insert_marker(self, kek_id: str, check_mac: bytes) -> bool:
        """ON CONFLICT DO NOTHING semantics."""
        with self._lock:
            if self.marker is not None:
                return False
            self.marker = MarkerRow(kek_id, check_mac)
            return True

    def count_secrets(self) -> int:
        """Row count."""
        return len(self.secrets)

    def rows_for(self, secret_type: int, scope_id: int | None) -> list[SecretRow]:
        """Rows of one (type, scope) by version."""
        return sorted(
            (r for r in self.secrets if r.secret_type == secret_type and r.scope_id == scope_id),
            key=lambda r: r.version,
        )

    def openable_rows(self) -> list[SecretRow]:
        """State 2 / 3 rows."""
        return [r for r in self.secrets if r.state in (2, 3)]

    def insert_secret(self, new: NewSecret) -> SecretRow | None:
        """INSERT ... ON CONFLICT DO NOTHING over both unique constraints."""
        if self.before_insert is not None:
            hook, self.before_insert = self.before_insert, None
            hook(self)
        with self._lock:
            for row in self.secrets:
                same_identity = (row.secret_type, row.scope_id, row.version) == (
                    new.secret_type,
                    new.scope_id,
                    new.version,
                )
                other_active = (row.secret_type, row.scope_id) == (new.secret_type, new.scope_id)
                if same_identity or (other_active and row.state == 2):
                    return None
            row = SecretRow(
                id=len(self.secrets) + 1,
                public_id=new.public_id,
                secret_type=new.secret_type,
                scope_id=new.scope_id,
                version=new.version,
                state=2,
                algorithm=new.algorithm,
                sealed_value=new.sealed_value,
                kek_id=new.kek_id,
                seal_version=new.seal_version,
                kid=new.kid,
                public_material=new.public_material,
                fingerprint=new.fingerprint,
                activated_at=new.activated_at,
                rotated_at=None,
                retired_at=None,
            )
            self.secrets.append(row)
            return row

    def update_state(
        self, secret_id: int, state: int, *, at: datetime, reason: int, actor_uuid: uuid.UUID | None
    ) -> bool:
        """Forward-only move of an active / verify-only row (mirrors the SQL WHERE clause)."""
        with self._lock:
            for index, row in enumerate(self.secrets):
                if row.id == secret_id:
                    if not (row.state < state and row.state in (2, 3)):
                        return False
                    self.secrets[index] = replace(
                        row,
                        state=state,
                        rotated_at=row.rotated_at or at,
                        retired_at=at if state == 4 else row.retired_at,
                    )
                    return True
        return False

    def read_anchor(self) -> AnchorRow | None:
        """The escrow anchor (None until a manifest was written)."""
        return self.anchor

    def advance_anchor(
        self, generation: int, manifest_digest: bytes, kek_id: str, mac: bytes
    ) -> bool:
        """Strictly-forward anchor move (mirrors the SQL WHERE and the trigger)."""
        with self._lock:
            if self.anchor is not None and self.anchor.generation >= generation:
                return False
            self.anchor = AnchorRow(generation, manifest_digest, kek_id, mac)
            return True

    def claim_request(
        self, idempotency_key: uuid.UUID, *, secret_type: int, action: int, actor_uuid: uuid.UUID
    ) -> bool:
        """Unique-key semantics of the replay ledger."""
        with self._lock:
            if idempotency_key in self.claimed:
                return False
            self.claimed.add(idempotency_key)
            return True

    def chain_tip(self) -> bytes | None:
        """Newest chain hash."""
        return self.event_rows[-1].chain_hash if self.event_rows else None

    def insert_event(self, event: EventRow) -> None:
        """Append, enforcing the single linear chain like the unique indexes."""
        with self._lock:
            if event.prev_chain_hash != self.chain_tip():
                raise AssertionError("non-linear event chain (unique prev_chain_hash would fire)")
            self.event_rows.append(event)

    def events(self) -> list[EventRow]:
        """The chain."""
        return list(self.event_rows)

    def notify_changed(self) -> None:
        """Count NOTIFYs."""
        self.notifies += 1


@pytest.fixture
def otel(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Private in-memory metric + span capture wired into the telemetry module."""
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry.reset_for_tests()
    monkeypatch.setattr(
        telemetry.metrics, "get_meter", lambda *_a, **_k: meter_provider.get_meter("t")
    )
    monkeypatch.setattr(
        telemetry.trace, "get_tracer", lambda *_a, **_k: tracer_provider.get_tracer("t")
    )
    yield {"reader": reader, "spans": exporter}
    telemetry.reset_for_tests()


def _counter(reader: InMemoryMetricReader, name: str) -> dict[tuple[tuple[str, str], ...], int]:
    """Sum of a counter by label set."""
    out: dict[tuple[tuple[str, str], ...], int] = {}
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        out[tuple(sorted(point.attributes.items()))] = point.value
    return out


def _total(reader: InMemoryMetricReader, name: str) -> int:
    """Total of a counter across label sets."""
    return sum(_counter(reader, name).values())


def _boot(
    repo: FakeRepo, kek: PlatformKekAdapter = KEK_A, environ: Mapping[str, str] | None = None
):
    """Run the bootstrap against `repo` with an empty (or given) environment."""
    return bootstrap_platform_secrets(repo, kek, environ=environ or {}, now=NOW)


def _b64(raw: bytes) -> str:
    """Standard base64 text."""
    return base64.b64encode(raw).decode()


GOOD_SEED = bytes((i * 37 + 11) % 256 for i in range(32))  # distinct, non-sequential test seed


# --------------------------------------------------------------------------- registry / generators


def test_registry_is_consistent_and_codes_are_ints() -> None:
    registry.check_registry()
    assert {int(t) for t in REGISTRY} == {1, 2, 3}
    assert all(isinstance(int(t), int) for t in SecretType)


def test_registry_rejects_unknown_type_and_bad_scope() -> None:
    with pytest.raises(registry.UnknownSecretTypeError):
        registry.get_spec(999)
    spec = registry.get_spec(int(SecretType.BUNDLE_SIGNING))
    registry.validate_scope(spec, None)
    with pytest.raises(registry.ScopeRuleError):
        registry.validate_scope(spec, 0)  # global types take no scope, 0 is the default tenant
    tenant_spec = replace(spec, scope_rule=registry.ScopeRule.TENANT)
    registry.validate_scope(tenant_spec, 0)
    with pytest.raises(registry.ScopeRuleError):
        registry.validate_scope(tenant_spec, None)
    with pytest.raises(registry.ScopeRuleError):
        registry.validate_scope(tenant_spec, -1)


def test_jwt_type_has_no_import_source() -> None:
    """The HS256 replacement must be freshly generated, never an already-distributed value."""
    assert REGISTRY[SecretType.JWT_SIGNING].import_source is None


@pytest.mark.parametrize(
    ("algorithm", "private_len", "public_len", "has_kid"),
    [
        (PLATFORM_ALG_ED25519, 32, 32, True),
        (PLATFORM_ALG_ES256, 32, 65, True),
        (PLATFORM_ALG_RANDOM_256, 32, None, False),
        (PLATFORM_ALG_PASSWORD, 43, None, False),
    ],
)
def test_generators_shape_and_uniqueness(algorithm, private_len, public_len, has_kid) -> None:
    first = generate_platform_key_material(algorithm)
    second = generate_platform_key_material(algorithm)
    assert len(first.plaintext) == private_len
    assert first.plaintext != second.plaintext
    assert (first.public_material is None) == (public_len is None)
    if public_len is not None:
        assert first.public_material is not None and len(first.public_material) == public_len
    assert (first.kid is not None) == has_kid
    if algorithm == PLATFORM_ALG_PASSWORD:
        assert re.fullmatch(rb"[A-Za-z0-9_-]{43}", first.plaintext)
    if algorithm in (PLATFORM_ALG_ED25519, PLATFORM_ALG_ES256):
        reparsed = platform_key_material_from_private(algorithm, first.plaintext)
        assert reparsed.public_material == first.public_material and reparsed.kid == first.kid


def test_material_repr_never_shows_the_secret() -> None:
    material = generate_platform_key_material(PLATFORM_ALG_PASSWORD)
    assert material.plaintext.decode() not in repr(material)


# --------------------------------------------------------------------------- importer


def _env(value: str | None, kid: str | None = "bundle-key-1") -> dict[str, str]:
    env = {}
    if value is not None:
        env["BUNDLE_SIGNING_PRIVATE_KEY"] = value
    if kid is not None:
        env["BUNDLE_SIGNING_KEY_ID"] = kid
    return env


def test_importer_accepts_a_good_seed_and_keeps_its_kid() -> None:
    decision = import_candidate(REGISTRY[SecretType.BUNDLE_SIGNING], _env(_b64(GOOD_SEED)))
    assert decision.usable and decision.source == "BUNDLE_SIGNING_PRIVATE_KEY"
    assert decision.material is not None and decision.material.kid == "bundle-key-1"
    assert decision.material.plaintext == GOOD_SEED
    assert GOOD_SEED.hex() not in repr(decision) and _b64(GOOD_SEED) not in repr(decision)


@pytest.mark.parametrize(
    ("value", "outcome"),
    [
        ("REPLACE_ME", "rejected_placeholder"),
        ("replace_me_please", "rejected_placeholder"),
        ("jwt-secret-change-in-production", "rejected_placeholder"),
        ("changeme", "rejected_placeholder"),
        (_b64(b"t" * 32), "rejected_invalid"),  # the repo's well-known test seed
        (_b64(bytes(32)), "rejected_invalid"),
        (_b64(bytes(range(32))), "rejected_invalid"),
        (_b64(GOOD_SEED[:16]), "rejected_invalid"),  # too short
        (_b64(GOOD_SEED + b"x"), "rejected_invalid"),  # too long
        ("not*base64!!", "rejected_invalid"),
    ],
)
def test_importer_rejects_placeholders_and_weak_values(value: str, outcome: str) -> None:
    decision = import_candidate(REGISTRY[SecretType.BUNDLE_SIGNING], _env(value))
    assert decision.outcome == outcome and not decision.usable and decision.material is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_importer_treats_unset_and_empty_as_absent(value: str | None) -> None:
    decision = import_candidate(REGISTRY[SecretType.BUNDLE_SIGNING], _env(value))
    assert decision.outcome == "absent" and not decision.usable


def test_importer_drops_a_placeholder_kid_instead_of_trusting_it() -> None:
    decision = import_candidate(
        REGISTRY[SecretType.BUNDLE_SIGNING], _env(_b64(GOOD_SEED), kid="REPLACE_ME")
    )
    assert decision.usable and decision.material is not None
    assert decision.material.kid is not None and decision.material.kid.startswith("k")


def test_importer_never_imports_a_type_without_a_source() -> None:
    env = {"JWT_SECRET": _b64(GOOD_SEED), "BUNDLE_SIGNING_PRIVATE_KEY": _b64(GOOD_SEED)}
    assert import_candidate(REGISTRY[SecretType.JWT_SIGNING], env).outcome == "absent"


# --------------------------------------------------------------------------- bootstrap: decisions


def test_first_run_writes_marker_then_generates_every_type_once() -> None:
    repo = FakeRepo()
    report = _boot(repo)
    assert report.first_run and set(report.generated) == ALL_NAMES and not report.imported
    assert repo.marker is not None and repo.marker.kek_id == KEK_A.kek_ids[0]
    assert len(repo.secrets) == len(REGISTRY) and all(r.state == 2 for r in repo.secrets)
    assert len(repo.event_rows) == len(REGISTRY) and verify_chain(repo, KEK_A).intact
    assert repo.notifies == 1 and len(report.entries) == len(REGISTRY)
    assert all(e.plaintext for e in report.entries)


def test_second_boot_changes_nothing_and_opens_the_same_values() -> None:
    repo = FakeRepo()
    first = _boot(repo)
    snapshot = ([r.public_id for r in repo.secrets], len(repo.event_rows), repo.notifies)
    second = _boot(repo)
    assert not second.first_run and not second.generated and not second.imported
    assert ([r.public_id for r in repo.secrets], len(repo.event_rows), repo.notifies) == snapshot
    assert {(e.secret_type, e.plaintext) for e in first.entries} == {
        (e.secret_type, e.plaintext) for e in second.entries
    }


def test_root_kek_mismatch_refuses_and_generates_nothing(otel) -> None:
    repo = FakeRepo()
    _boot(repo)
    before = (list(repo.secrets), list(repo.event_rows), repo.marker)
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(repo, kek=KEK_B)
    assert exc.value.reason == "root_mismatch"
    assert (list(repo.secrets), list(repo.event_rows), repo.marker) == before
    assert _counter(otel["reader"], "waddles_platform_secrets_drift_detected_total") == {
        (("outcome", "mismatch"), ("type", "all")): 1
    }
    _boot(repo)  # positive control: the right KEK still boots


def test_previous_kek_in_rotation_window_still_boots() -> None:
    repo = FakeRepo()
    _boot(repo)
    rotated = PlatformKekAdapter(bytes.fromhex("b2" * 32), bytes.fromhex("a1" * 32))
    report = _boot(repo, kek=rotated)
    assert not report.generated and len(report.entries) == len(REGISTRY)


def test_marker_present_and_store_empty_refuses_to_start(otel) -> None:
    repo = FakeRepo()
    _boot(repo)
    repo.secrets.clear()  # the wipe (a restore of an old snapshot, a recreated table)
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(repo)
    assert exc.value.reason == "store_wiped"
    assert repo.secrets == [], "a wiped store must not be repopulated"
    assert _total(otel["reader"], "waddles_platform_secrets_drift_detected_total") == 1


def test_rows_without_a_marker_refuse() -> None:
    repo = FakeRepo()
    _boot(repo)
    repo.marker = None
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(repo)
    assert exc.value.reason == "marker_missing"


def test_unopenable_row_refuses_instead_of_regenerating(otel) -> None:
    repo = FakeRepo()
    _boot(repo)
    victim = repo.secrets[0]
    tampered = bytearray(victim.sealed_value or b"")
    tampered[-1] ^= 0x01
    repo.secrets[0] = replace(victim, sealed_value=bytes(tampered))
    rows_before = len(repo.secrets)
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(repo)
    assert exc.value.reason == "unopenable"
    assert len(repo.secrets) == rows_before, "nothing may be generated because a row won't open"
    assert _total(otel["reader"], "waddles_platform_secrets_open_failures_total") == 1


def test_copied_ciphertext_between_rows_does_not_open() -> None:
    repo = FakeRepo()
    _boot(repo)
    a, b = repo.secrets[0], repo.secrets[1]
    repo.secrets[1] = replace(b, sealed_value=a.sealed_value)
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(repo)
    assert exc.value.reason == "unopenable"


def test_rows_without_an_active_one_are_inconsistent() -> None:
    repo = FakeRepo()
    _boot(repo)
    repo.secrets[0] = replace(repo.secrets[0], state=4)
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(repo)
    assert exc.value.reason == "inconsistent"


@pytest.mark.parametrize("tamper", ["hash", "drop", "reorder", "field"])
def test_event_chain_break_refuses_to_start(tamper: str) -> None:
    repo = FakeRepo()
    _boot(repo)
    assert verify_chain(repo, KEK_A).intact  # positive control
    events = repo.event_rows
    if tamper == "hash":
        events[1] = replace(events[1], chain_hash=bytes(32))
    elif tamper == "drop":
        events.pop(1)
    elif tamper == "reorder":
        events[0], events[1] = events[1], events[0]
    else:
        events[1] = replace(events[1], version=7)
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(repo)
    assert exc.value.reason == "chain_break"


def test_chain_written_under_a_previous_kek_still_verifies_after_rotation() -> None:
    repo = FakeRepo()
    _boot(repo)
    rotated = PlatformKekAdapter(bytes.fromhex("b2" * 32), bytes.fromhex("a1" * 32))
    assert verify_chain(repo, rotated).intact
    append_event(
        repo,
        rotated,
        secret_id=1,
        secret_type=1,
        scope_id=None,
        version=1,
        event_type=1,
        outcome=1,
        reason=1,
        now=NOW,
    )
    assert verify_chain(repo, rotated).intact
    assert not verify_chain(repo, KEK_B).intact  # unrelated KEK cannot vouch for it


def test_empty_chain_is_intact_with_zero_examined() -> None:
    report = verify_chain(FakeRepo(), KEK_A)
    assert report.intact and report.examined == 0


# --------------------------------------------------------------------------- bootstrap: import


def test_good_allowlisted_value_is_imported_as_version_one_on_first_run() -> None:
    repo = FakeRepo()
    report = _boot(repo, environ=_env(_b64(GOOD_SEED)))
    assert report.imported == ("bundle_signing",) and "bundle_signing" not in report.generated
    row = next(r for r in repo.secrets if r.secret_type == int(SecretType.BUNDLE_SIGNING))
    assert row.version == 1 and row.kid == "bundle-key-1"
    entry = next(e for e in report.entries if e.secret_type == row.secret_type)
    assert entry.plaintext == GOOD_SEED
    assert repo.event_rows[0].event_type == 2 and repo.event_rows[0].reason == 2


@pytest.mark.parametrize(
    "planted",
    [
        "REPLACE_ME",
        "jwt-secret-change-in-production",
        _b64(b"t" * 32),
        _b64(bytes(32)),
        "",
        "short",
    ],
)
def test_planted_placeholder_is_never_imported(planted: str, otel) -> None:
    repo = FakeRepo()
    report = _boot(repo, environ=_env(planted))
    assert not report.imported and "bundle_signing" in report.generated
    row = next(r for r in repo.secrets if r.secret_type == int(SecretType.BUNDLE_SIGNING))
    event = next(e for e in repo.event_rows if e.secret_id == row.id)
    assert (event.event_type, event.reason) == (1, 1), "recorded as generated, not imported"
    entry = next(e for e in report.entries if e.secret_type == row.secret_type)
    assert entry.plaintext != b"t" * 32 and entry.plaintext != bytes(32)
    if planted.strip():
        assert _total(otel["reader"], "waddles_platform_secrets_imported_total") == 1
        assert all(
            ("outcome", "ok") not in k
            for k in _counter(otel["reader"], "waddles_platform_secrets_imported_total")
        )
    # positive control: the same slot with a good value imports
    assert _boot(FakeRepo(), environ=_env(_b64(GOOD_SEED))).imported == ("bundle_signing",)


class _SpyEnv(dict):
    """Environment that records every key the code asked for."""

    def __init__(self, data: Mapping[str, str]) -> None:
        super().__init__(data)
        self.asked: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:  # noqa: D102
        self.asked.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> Any:  # noqa: D105
        self.asked.add(key)
        return super().__getitem__(key)


def test_import_reads_only_the_registry_allowlist_never_arbitrary_env() -> None:
    env = _SpyEnv(
        {
            **_env(_b64(GOOD_SEED)),
            "JWT_SECRET": _b64(GOOD_SEED[::-1]),
            "ATTACKER_SIGNING_KEY": _b64(GOOD_SEED[::-1]),
            "TENANT_KEK_HEX": "ab" * 32,
        }
    )
    _boot(FakeRepo(), environ=env)
    allowlist = {"BUNDLE_SIGNING_PRIVATE_KEY", "BUNDLE_SIGNING_KEY_ID"}
    assert env.asked and env.asked <= allowlist


def test_import_is_only_for_a_pristine_store() -> None:
    """A type that is missing on a NON-first boot is generated, never imported from env (H1a)."""
    repo = FakeRepo()
    _boot(repo)
    survivors = [r for r in repo.secrets if r.secret_type != int(SecretType.BUNDLE_SIGNING)]
    old_events = list(repo.event_rows)
    repo.secrets[:] = survivors
    repo.event_rows.clear()
    for event in old_events:  # rebuild a valid chain over the surviving rows
        if event.secret_type != int(SecretType.BUNDLE_SIGNING):
            append_event(
                repo,
                KEK_A,
                secret_id=event.secret_id,
                secret_type=event.secret_type,
                scope_id=None,
                version=1,
                event_type=1,
                outcome=1,
                reason=1,
                now=NOW,
            )
    assert verify_chain(repo, KEK_A).intact
    report = _boot(repo, environ=_env(_b64(GOOD_SEED)))
    assert not report.first_run
    assert report.generated == ("bundle_signing",) and not report.imported


# --------------------------------------------------------------------------- bootstrap: concurrency


def test_two_concurrent_bootstraps_create_exactly_one_row_per_type() -> None:
    repo = FakeRepo()
    advisory_lock = threading.Lock()
    barrier = threading.Barrier(2)
    reports: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            with advisory_lock:  # the real code holds pg_advisory_lock around this call
                reports.append(_boot(repo))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors and len(reports) == 2
    assert sorted(r.first_run for r in reports) == [False, True]
    assert len(repo.secrets) == len(REGISTRY) and len(repo.event_rows) == len(REGISTRY)
    assert len({(r.secret_type, r.scope_id) for r in repo.secrets}) == len(REGISTRY)
    assert verify_chain(repo, KEK_A).intact
    values = [{(e.secret_type, e.plaintext) for e in r.entries} for r in reports]
    assert values[0] == values[1], "both replicas must hold the same keys"


def test_insert_race_loser_adopts_the_winner_and_discards_its_own_key(otel) -> None:
    repo = FakeRepo()
    winner_box: dict[str, bytes] = {}

    def winner_lands_first(r: FakeRepo) -> None:
        if winner_box:
            return
        sealed_row = _boot_row_for(KEK_A, SecretType.BUNDLE_SIGNING)
        winner_box["pub"] = sealed_row.public_material or b""
        r.secrets.append(sealed_row)

    repo.before_insert = winner_lands_first
    # marker is written first so the loser is a normal first-run writer that loses the insert race
    report = _boot(repo)
    bundle_rows = [r for r in repo.secrets if r.secret_type == int(SecretType.BUNDLE_SIGNING)]
    assert len(bundle_rows) == 1 and bundle_rows[0].public_material == winner_box["pub"]
    assert "bundle_signing" not in report.generated
    assert (
        _counter(otel["reader"], "waddles_platform_secrets_generated_total").get(
            (("outcome", "conflict"), ("type", "bundle_signing"))
        )
        == 1
    )


def _boot_row_for(kek: PlatformKekAdapter, secret_type: SecretType) -> SecretRow:
    """A valid sealed active row, as a concurrent winner would have written it."""
    from services.provider_connections.vault import (
        platform_secret_fingerprint,
        seal_platform_secret,
    )

    spec = REGISTRY[secret_type]
    material = generate_platform_key_material(int(spec.algorithm))
    public_id = uuid.uuid4()
    sealed = seal_platform_secret(
        material.plaintext,
        secret_type=int(secret_type),
        scope_id=None,
        version=1,
        public_id=public_id,
        kek=kek,
    )
    return SecretRow(
        id=99,
        public_id=public_id,
        secret_type=int(secret_type),
        scope_id=None,
        version=1,
        state=2,
        algorithm=int(spec.algorithm),
        sealed_value=sealed.sealed_value,
        kek_id=sealed.kek_id,
        seal_version=1,
        kid=material.kid,
        public_material=material.public_material,
        fingerprint=platform_secret_fingerprint(material.plaintext, kek=kek),
        activated_at=NOW,
        rotated_at=None,
        retired_at=None,
    )


# --------------------------------------------------------------------------- repository source


def test_repository_never_updates_or_deletes() -> None:
    """Static: no DELETE / TRUNCATE; the ONE UPDATE is the forward-only, state-guarded move.

    Insert-only for bootstrap; `update_state` (escrow restore, rotation) is the single UPDATE and
    only ever moves `state` forward (`WHERE ... state < :state`), never touches a value column.
    """
    source = (REPO_ROOT / "hub_api/services/platform_secrets/repository.py").read_text()
    sql_lines = [ln for ln in source.splitlines() if '"' in ln]
    joined = "\n".join(sql_lines).upper()
    for forbidden in ("UPDATE PLATFORM_VAULT", "DELETE FROM", "TRUNCATE"):
        assert forbidden not in joined, forbidden
    # exactly two UPDATEs, both forward-only: secret `state`, and the escrow anchor generation
    assert joined.count("UPDATE {SCHEMA}") == 2
    assert "STATE < :STATE" in joined
    assert "ESCROW_GENERATION < :G" in joined
    assert "SEALED_VALUE =" not in joined
    assert "ON CONFLICT DO NOTHING" in source


def test_bootstrap_modules_import_no_crypto_primitives() -> None:
    """All crypto stays in the vault (seal-callers guard ratchet): no cryptography / hmac here."""
    package = REPO_ROOT / "hub_api/services/platform_secrets"
    for path in package.glob("*.py"):
        text = path.read_text()
        assert not re.search(r"^\s*(import|from)\s+(cryptography|hmac)\b", text, re.M), path.name


# --------------------------------------------------------------------------- kill switch / wrapper


def test_kill_switch_reads_the_env_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    var = "FLAG_WADDLES_DISABLE_DB_PLATFORM_KEYS"
    monkeypatch.delenv(var, raising=False)
    assert db_platform_keys_disabled() is False  # unseen = DB is the source
    monkeypatch.setenv(var, "false")
    assert db_platform_keys_disabled() is False
    monkeypatch.setenv(var, "garbage")
    assert db_platform_keys_disabled() is False
    monkeypatch.setenv(var, "true")
    assert db_platform_keys_disabled() is True


def test_kill_switch_on_touches_nothing(otel) -> None:
    engine = MagicMock()
    cache = PlatformSecretCache()
    report = run_platform_secrets_bootstrap(
        engine, kill_switch=lambda: True, cache=cache, environ={}
    )
    assert report.kill_switched and not report.entries
    engine.begin.assert_not_called()
    engine.connect.assert_not_called()
    assert cache.gauge_snapshot()[0][1] == 0


def test_missing_kek_refuses_loudly_rather_than_skipping() -> None:
    engine = MagicMock()
    with pytest.raises(PlatformVaultRefusedError) as exc:
        run_platform_secrets_bootstrap(engine, kill_switch=lambda: False, environ={})
    assert exc.value.reason == "kek_missing"
    engine.begin.assert_not_called()


def _engine_with(repo: FakeRepo, *, fail_on_exit: bool = False) -> MagicMock:
    engine = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = MagicMock()
    ctx.__exit__.side_effect = lambda *a: (
        (_ for _ in ()).throw(RuntimeError("commit failed")) if fail_on_exit else False
    )
    engine.begin.return_value = ctx
    return engine


def test_cache_is_seeded_only_after_the_transaction_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepo()
    monkeypatch.setattr(vault_bootstrap, "SqlRepository", lambda _conn: repo)
    cache = PlatformSecretCache()
    report = run_platform_secrets_bootstrap(
        _engine_with(repo), kill_switch=lambda: False, cache=cache, kek=KEK_A, environ={}
    )
    assert report.first_run and cache.active(int(SecretType.JWT_SIGNING)).plaintext
    broken_cache = PlatformSecretCache()
    with pytest.raises(RuntimeError, match="commit failed"):
        run_platform_secrets_bootstrap(
            _engine_with(FakeRepo(), fail_on_exit=True),
            kill_switch=lambda: False,
            cache=broken_cache,
            kek=KEK_A,
            environ={},
        )
    with pytest.raises(NoActiveSecretError):
        broken_cache.active(int(SecretType.JWT_SIGNING))


# --------------------------------------------------------------------------- cache


def _entry(
    version: int,
    state: int,
    kid: str,
    *,
    rotated: datetime | None = None,
    secret_type: int = int(SecretType.JWT_SIGNING),
) -> CachedSecret:
    return CachedSecret(
        secret_type=secret_type,
        scope_id=None,
        version=version,
        state=state,
        kid=kid,
        public_id=uuid.uuid4(),
        public_material=b"p" * 65,
        activated_at=NOW - timedelta(hours=1),
        verify_until=verify_until_for(secret_type, state, rotated),
        plaintext=b"v" * 32,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def test_unknown_kid_fails_closed_and_never_falls_back_to_the_active_key() -> None:
    cache = PlatformSecretCache(clock=_Clock())
    cache.seed([_entry(2, 2, "kid-new"), _entry(1, 3, "kid-old", rotated=NOW)])
    assert cache.by_kid(int(SecretType.JWT_SIGNING), "kid-new").version == 2  # positive control
    assert cache.by_kid(int(SecretType.JWT_SIGNING), "kid-old").version == 1
    with pytest.raises(UnknownKeyIdError):
        cache.by_kid(int(SecretType.JWT_SIGNING), "kid-unknown")
    with pytest.raises(UnknownKeyIdError):
        cache.by_kid(int(SecretType.BUNDLE_SIGNING), "kid-new")  # right kid, wrong type


def test_unknown_kid_lookup_lazy_loads_once_then_negative_caches() -> None:
    calls = {"n": 0}
    clock = _Clock()

    def loader() -> list[CachedSecret]:
        calls["n"] += 1
        return [_entry(1, 2, "kid-1")]

    cache = PlatformSecretCache(loader, clock=clock)
    for _ in range(5):
        with pytest.raises(UnknownKeyIdError):
            cache.by_kid(int(SecretType.JWT_SIGNING), "nope")
    assert calls["n"] == 1, "a stream of unknown ids must cost one DB load per negative TTL"
    clock.now += timedelta(seconds=31)
    with pytest.raises(UnknownKeyIdError):
        cache.by_kid(int(SecretType.JWT_SIGNING), "nope")
    assert calls["n"] == 2


def test_lazy_load_finds_a_key_added_after_the_seed() -> None:
    state = {"rows": [_entry(1, 2, "kid-1")]}
    cache = PlatformSecretCache(lambda: list(state["rows"]))
    cache.seed(state["rows"])
    state["rows"] = [_entry(2, 2, "kid-2"), _entry(1, 3, "kid-1", rotated=NOW)]
    assert cache.by_kid(int(SecretType.JWT_SIGNING), "kid-2").version == 2
    assert cache.active(int(SecretType.JWT_SIGNING)).kid == "kid-2"


def test_empty_cache_lazy_loads_instead_of_failing_every_check() -> None:
    cache = PlatformSecretCache(lambda: [_entry(1, 2, "kid-1")])
    assert cache.active(int(SecretType.JWT_SIGNING)).kid == "kid-1"
    with pytest.raises(NoActiveSecretError):
        PlatformSecretCache().active(int(SecretType.JWT_SIGNING))


def test_verify_only_version_is_honoured_until_verify_until_then_refused() -> None:
    clock = _Clock()
    window = REGISTRY[SecretType.JWT_SIGNING].verify_window_s
    cache = PlatformSecretCache(clock=clock)
    cache.seed([_entry(2, 2, "kid-new"), _entry(1, 3, "kid-old", rotated=NOW)])
    clock.now = NOW + timedelta(seconds=window - 1)
    assert cache.by_kid(int(SecretType.JWT_SIGNING), "kid-old").version == 1
    clock.now = NOW + timedelta(seconds=window + 1)
    with pytest.raises(UnknownKeyIdError):
        cache.by_kid(int(SecretType.JWT_SIGNING), "kid-old")  # no refresh happened: still refused
    assert cache.by_kid(int(SecretType.JWT_SIGNING), "kid-new").version == 2
    assert [e.version for e in cache.servable_versions(int(SecretType.JWT_SIGNING))] == [2]


def test_verify_only_without_a_demotion_time_is_never_served() -> None:
    cache = PlatformSecretCache()
    cache.seed([_entry(2, 2, "kid-new"), _entry(1, 3, "kid-old", rotated=None)])
    with pytest.raises(UnknownKeyIdError):
        cache.by_kid(int(SecretType.JWT_SIGNING), "kid-old")


def test_retired_and_destroyed_are_never_served() -> None:
    cache = PlatformSecretCache()
    cache.seed([_entry(3, 2, "kid-3"), _entry(2, 4, "kid-2"), _entry(1, 5, "kid-1")])
    for kid in ("kid-2", "kid-1"):
        with pytest.raises(UnknownKeyIdError):
            cache.by_kid(int(SecretType.JWT_SIGNING), kid)


def test_cached_secret_repr_hides_the_value() -> None:
    entry = replace(_entry(1, 2, "kid-1"), plaintext=b"super-secret-material-0123456789abcdef")
    assert "super-secret" not in repr(entry)


# --------------------------------------------------------------------------- refresher


def test_refresher_refreshes_on_notify_or_poll_and_keeps_last_known_on_failure() -> None:
    state = {"fail": False, "calls": 0}

    def loader() -> list[CachedSecret]:
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("db down")
        return [_entry(state["calls"], 2, f"kid-{state['calls']}")]

    cache = PlatformSecretCache(loader)
    waits: list[float] = []
    refresher = CacheRefresher(cache, lambda t: waits.append(t) or True, poll_s=60.0)
    assert refresher.run_once() is True and waits == [60.0]
    first = cache.active(int(SecretType.JWT_SIGNING)).kid
    state["fail"] = True
    assert refresher.run_once() is False
    assert cache.active(int(SecretType.JWT_SIGNING)).kid == first, "last-known survives a failure"
    state["fail"] = False
    assert refresher.run_once() is True
    assert cache.active(int(SecretType.JWT_SIGNING)).kid != first


def test_refresher_thread_starts_and_stops() -> None:
    ticks = threading.Event()
    cache = PlatformSecretCache(lambda: [_entry(1, 2, "kid-1")])

    def wait(_t: float) -> bool:
        ticks.set()
        return True

    refresher = CacheRefresher(cache, wait, poll_s=0.01)
    refresher.start()
    assert ticks.wait(timeout=10)
    refresher.stop()


# --------------------------------------------------------------------------- no leaks


_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")


def _sensitive_strings(repo: FakeRepo, report: Any) -> set[str]:
    """Every textual form of every value / fingerprint / ciphertext the run produced."""
    forms: set[str] = set()
    blobs = [e.plaintext for e in report.entries]
    blobs += [r.sealed_value or b"" for r in repo.secrets] + [r.fingerprint for r in repo.secrets]
    blobs += [repo.marker.check_mac] if repo.marker else []
    blobs += [e.chain_hash for e in repo.event_rows]
    for blob in blobs:
        forms |= {
            blob.hex(),
            base64.b64encode(blob).decode(),
            base64.urlsafe_b64encode(blob).decode(),
            base64.b64encode(blob).decode().rstrip("="),
            repr(blob),
        }
        if blob.isascii():
            forms.add(blob.decode("ascii", "ignore"))
    return {f for f in forms if len(f) >= 8}


def _telemetry_text(otel: dict[str, Any]) -> list[str]:
    """Every string in captured spans (names, attributes, status) and metrics (names, labels)."""
    texts: list[str] = []
    for span in otel["spans"].get_finished_spans():
        texts += [
            span.name,
            str(dict(span.attributes or {})),
            str(span.status.description),
            str(span.events),
        ]
    data = otel["reader"].get_metrics_data()
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                texts.append(metric.name)
                texts += [str(dict(p.attributes)) for p in metric.data.data_points]
    return texts


def test_no_secret_value_reaches_logs_spans_or_metrics(
    caplog: pytest.LogCaptureFixture, otel
) -> None:
    caplog.set_level(logging.DEBUG)
    repo = FakeRepo()
    report = _boot(repo, environ=_env(_b64(GOOD_SEED)))  # one import
    _boot(repo, environ=_env("REPLACE_ME"))  # second boot (no import) -- logs again
    rejected_repo = FakeRepo()
    _boot(rejected_repo, environ=_env(_b64(b"t" * 32)))  # a rejected import
    with pytest.raises(PlatformVaultRefusedError):
        _boot(repo, kek=KEK_B)  # a refusal path
    forms = _sensitive_strings(repo, report) | {_b64(GOOD_SEED), GOOD_SEED.hex(), "REPLACE_ME"}
    logs = [r.getMessage() for r in caplog.records] + [str(r.args) for r in caplog.records]
    texts = logs + _telemetry_text(otel)
    assert logs, "the run must have logged something for this test to mean anything"
    assert any(t.startswith("platform_secrets.") for t in _telemetry_text(otel))
    for text in texts:
        for form in forms:
            assert form not in text, "a secret-derived value leaked into logs/spans/metrics"
        scrubbed = re.sub(r"waddles_platform_secrets_[a-z_]+", "<metric name>", text)
        scrubbed = scrubbed.replace("BUNDLE_SIGNING_PRIVATE_KEY", "<allowlisted env name>")
        assert not _TOKEN.search(scrubbed), f"secret-shaped string in telemetry: {text[:40]!r}"
    names = {name for name in _telemetry_text(otel)}
    assert any(n.startswith("waddles_platform_secrets_") for n in names)


def test_failed_span_carries_only_the_exception_class(otel) -> None:
    secret_msg = "boom contains " + "Z" * 40
    with pytest.raises(RuntimeError):
        with telemetry.span("generate", type="jwt_signing"):
            raise RuntimeError(secret_msg)
    spans = otel["spans"].get_finished_spans()
    assert spans and spans[0].attributes["error.type"] == "RuntimeError"
    assert "Z" * 40 not in str(spans[0].events) and "Z" * 40 not in str(spans[0].status.description)
    assert spans[0].status.status_code.name == "ERROR"


def test_telemetry_outcome_labels_are_an_allowlist(otel) -> None:
    telemetry.count_generated("jwt_signing", "something-with-a-" + "k" * 40)
    keys = list(_counter(otel["reader"], "waddles_platform_secrets_generated_total"))
    assert keys == [(("outcome", "other"), ("type", "jwt_signing"))]


def test_gauges_report_active_versions_and_age(otel) -> None:
    cache = PlatformSecretCache()
    cache.seed([_entry(1, 2, "kid-1")])
    telemetry.register_cache_gauges(cache.gauge_snapshot)
    data = otel["reader"].get_metrics_data()
    names = {m.name for r in data.resource_metrics for s in r.scope_metrics for m in s.metrics}
    assert {
        "waddles_platform_secrets_active_versions",
        "waddles_platform_secrets_age_seconds",
    } <= names


def test_open_duration_histogram_records(otel) -> None:
    _boot(FakeRepo())
    data = otel["reader"].get_metrics_data()
    hist = [
        m
        for r in data.resource_metrics
        for s in r.scope_metrics
        for m in s.metrics
        if m.name == "waddles_platform_secrets_open_duration_seconds"
    ]
    assert hist and sum(p.count for p in hist[0].data.data_points) == len(REGISTRY)
