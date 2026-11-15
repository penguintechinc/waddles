"""Platform secret store T3 on REAL Postgres (migration 0109 applied): SQL, grants, locking.

The unit suite (`test_platform_secrets_bootstrap.py`) proves the decision logic over an in-memory
repository; this file proves what only a real database can: the repository SQL against the real
DDL and CHECKs, the bootstrap running as hub-api's REAL role (`waddles_hub_api`: INSERT/SELECT, no
UPDATE of secrets here, no DELETE anywhere), the advisory lock plus ON CONFLICT under two concurrent
bootstraps, wipe / root-change / chain-break refusals against real rows, and that a driver error
never echoes a bound ciphertext. Skipped (never failed) where the `docker` CLI is unavailable.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from bootstrap import BOOTSTRAP_LOCK_KEY  # noqa: E402
from services.envelope.platform_kek import PlatformKekAdapter  # noqa: E402
from services.platform_secrets.bootstrap import (  # noqa: E402
    PlatformVaultRefusedError,
    run_platform_secrets_bootstrap,
)
from services.platform_secrets.cache import PlatformSecretCache  # noqa: E402
from services.platform_secrets.event_chain import verify_chain  # noqa: E402
from services.platform_secrets.refresher import make_loader  # noqa: E402
from services.platform_secrets.registry import REGISTRY, SecretType  # noqa: E402
from services.platform_secrets.repository import NewSecret, SqlRepository  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

KEK_A = PlatformKekAdapter(bytes.fromhex("a1" * 32))  # throwaway test key
KEK_B = PlatformKekAdapter(bytes.fromhex("b2" * 32))  # throwaway test key
HUB_ROLE = "waddles_hub_api"


def _url(db: PgTestDatabase, user: str, password: str) -> str:
    """psycopg2 SQLAlchemy URL for one role."""
    return f"postgresql+psycopg2://{user}:{password}@{db.host}:{db.port}/{db.dbname}"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0109)."""
    with migrated_postgres("hub-api-platform-secrets") as db:
        yield db


@pytest.fixture(scope="module")
def owner(pg_db: PgTestDatabase) -> Iterator[sqlalchemy.engine.Engine]:
    """Owner / superuser engine: test setup and tamper simulation only."""
    engine = sqlalchemy.create_engine(_url(pg_db, pg_db.user, pg_db.password))
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def hub(pg_db: PgTestDatabase) -> Iterator[sqlalchemy.engine.Engine]:
    """Engine as hub-api's real role, with parameters hidden exactly like the bootstrap engine."""
    password = pg_db.service_role_passwords.get(HUB_ROLE)
    if password is None:
        pytest.skip("the migrated container has no waddles_hub_api login password")
    engine = sqlalchemy.create_engine(_url(pg_db, HUB_ROLE, password), hide_parameters=True)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_store(owner: sqlalchemy.engine.Engine) -> None:
    """Empty the vault before each test (superuser, triggers off) so every test starts pristine."""
    with owner.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        for table in ("secret_events", "secrets", "root_marker"):
            conn.execute(text(f"DELETE FROM platform_vault.{table}"))  # noqa: S608


def _count(owner: sqlalchemy.engine.Engine, table: str) -> int:
    with owner.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM platform_vault.{table}")).scalar() or 0)  # noqa: S608


def _boot(engine: sqlalchemy.engine.Engine, kek: PlatformKekAdapter = KEK_A, environ: Any = None):
    return run_platform_secrets_bootstrap(
        engine,
        kill_switch=lambda: False,
        kek=kek,
        environ=environ or {},
        cache=PlatformSecretCache(),
    )


def test_first_run_as_the_real_hub_role_creates_marker_rows_and_a_valid_chain(hub, owner) -> None:
    report = _boot(hub)
    assert report.first_run and set(report.generated) == {s.name for s in REGISTRY.values()}
    assert _count(owner, "root_marker") == 1
    assert _count(owner, "secrets") == len(REGISTRY)
    assert _count(owner, "secret_events") == len(REGISTRY)
    with hub.connect() as conn:
        assert verify_chain(SqlRepository(conn), KEK_A).intact
    again = _boot(hub)
    assert not again.generated and _count(owner, "secrets") == len(REGISTRY)


def test_marker_present_and_store_empty_refuses_and_generates_nothing(hub, owner) -> None:
    _boot(hub)
    with owner.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM platform_vault.secret_events"))
        conn.execute(text("DELETE FROM platform_vault.secrets"))
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(hub)
    assert exc.value.reason == "store_wiped" and _count(owner, "secrets") == 0


def test_a_changed_root_kek_refuses_and_leaves_the_rows_alone(hub, owner) -> None:
    _boot(hub)
    before = _count(owner, "secrets"), _count(owner, "secret_events")
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(hub, kek=KEK_B)
    assert exc.value.reason == "root_mismatch"
    assert (_count(owner, "secrets"), _count(owner, "secret_events")) == before


def test_a_refused_boot_rolls_back_the_marker(hub, owner) -> None:
    """Rows without a marker is `marker_missing`; and a first-run failure never leaves a marker."""
    _boot(hub)
    with owner.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM platform_vault.root_marker"))
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(hub)
    assert exc.value.reason == "marker_missing" and _count(owner, "root_marker") == 0


def test_a_tampered_event_chain_refuses(hub, owner) -> None:
    _boot(hub)
    with owner.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(
            text(
                "UPDATE platform_vault.secret_events SET chain_hash = decode(repeat('00', 32), "
                "'hex') WHERE id = (SELECT min(id) FROM platform_vault.secret_events)"
            )
        )
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(hub)
    assert exc.value.reason == "chain_break"


def test_a_tampered_ciphertext_refuses_instead_of_regenerating(hub, owner) -> None:
    _boot(hub)
    with owner.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(
            text(
                "UPDATE platform_vault.secrets SET sealed_value = set_byte(sealed_value, "
                "octet_length(sealed_value) - 1, get_byte(sealed_value, octet_length(sealed_value) "
                "- 1) # 1) WHERE id = (SELECT min(id) FROM platform_vault.secrets)"
            )
        )
    before = _count(owner, "secrets")
    with pytest.raises(PlatformVaultRefusedError) as exc:
        _boot(hub)
    assert exc.value.reason == "unopenable" and _count(owner, "secrets") == before


def test_two_concurrent_bootstraps_under_the_advisory_lock_create_one_row_per_type(
    hub, owner
) -> None:
    barrier = threading.Barrier(2)
    reports: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            with hub.connect() as lock_conn:
                lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": BOOTSTRAP_LOCK_KEY})
                try:
                    reports.append(_boot(hub))
                finally:
                    lock_conn.execute(
                        text("SELECT pg_advisory_unlock(:k)"), {"k": BOOTSTRAP_LOCK_KEY}
                    )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors
    assert sorted(r.first_run for r in reports) == [False, True]
    assert _count(owner, "secrets") == len(REGISTRY) == _count(owner, "secret_events")
    assert _count(owner, "root_marker") == 1
    values = [{(e.secret_type, e.plaintext) for e in r.entries} for r in reports]
    assert values[0] == values[1]


def test_insert_on_conflict_do_nothing_and_re_select(hub) -> None:
    import uuid
    from datetime import UTC, datetime

    from services.provider_connections.vault import (
        generate_platform_key_material,
        platform_secret_fingerprint,
        seal_platform_secret,
    )

    spec = REGISTRY[SecretType.JWT_SIGNING]

    def new() -> NewSecret:
        material = generate_platform_key_material(int(spec.algorithm))
        public_id = uuid.uuid4()
        sealed = seal_platform_secret(
            material.plaintext,
            secret_type=int(spec.secret_type),
            scope_id=None,
            version=1,
            public_id=public_id,
            kek=KEK_A,
        )
        return NewSecret(
            public_id=public_id,
            secret_type=int(spec.secret_type),
            scope_id=None,
            version=1,
            algorithm=int(spec.algorithm),
            sealed_value=sealed.sealed_value,
            kek_id=sealed.kek_id,
            seal_version=1,
            kid=material.kid,
            public_material=material.public_material,
            fingerprint=platform_secret_fingerprint(material.plaintext, kek=KEK_A),
            reason=1,
            activated_at=datetime.now(UTC),
        )

    with hub.begin() as conn:
        repo = SqlRepository(conn)
        winner = repo.insert_secret(new())
        assert winner is not None
        assert repo.insert_secret(new()) is None, "second ACTIVE v1 must be a silent no-op"
        rows = repo.rows_for(int(spec.secret_type), None)
        assert [r.public_id for r in rows] == [winner.public_id]


def test_planted_placeholder_is_not_imported_on_real_postgres(hub, owner) -> None:
    report = _boot(hub, environ={"BUNDLE_SIGNING_PRIVATE_KEY": "REPLACE_ME"})
    assert not report.imported and "bundle_signing" in report.generated
    with owner.connect() as conn:
        reason = conn.execute(
            text("SELECT reason FROM platform_vault.secrets WHERE secret_type = :t"),
            {"t": int(SecretType.BUNDLE_SIGNING)},
        ).scalar()
    assert reason == 1


def test_driver_errors_never_echo_the_bound_ciphertext(hub) -> None:
    """The engine hides parameters: a CHECK violation message carries no sealed value."""
    with pytest.raises(DBAPIError) as exc:
        with hub.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO platform_vault.secrets (secret_type, version, state, algorithm, "
                    "sealed_value, kek_id, fingerprint, reason, activated_at) "
                    "VALUES (3, 1, 2, 3, :blob, 'not-a-kek-id', :fp, 1, now())"
                ),
                {"blob": b"CANARY-CIPHERTEXT-" + b"x" * 40, "fp": b"\x00" * 32},
            )
    message = str(exc.value)
    assert "CANARY-CIPHERTEXT" not in message and "[parameters" not in message


def test_loader_reopens_everything_and_a_cache_refresh_works(hub) -> None:
    _boot(hub)
    cache = PlatformSecretCache(make_loader(hub, KEK_A))
    assert cache.refresh() == len(REGISTRY)
    assert cache.active(int(SecretType.JWT_SIGNING)).plaintext
    broken = PlatformSecretCache(make_loader(hub, KEK_B))
    with pytest.raises(Exception):  # noqa: B017, PT011 -- CacheLoadError (wrong root KEK)
        broken.refresh()


def test_notify_fires_on_generation(hub, pg_db) -> None:
    import select

    import psycopg2

    listener = psycopg2.connect(pg_db.dsn)
    listener.autocommit = True
    with listener.cursor() as cur:
        cur.execute("LISTEN platform_secrets_changed")
    _boot(hub)
    ready, _, _ = select.select([listener], [], [], 10)
    listener.poll()
    assert ready and listener.notifies and listener.notifies[0].payload == ""
    listener.close()
