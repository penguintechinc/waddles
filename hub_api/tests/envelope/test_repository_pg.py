"""The envelope key store against a REAL, fully-migrated Postgres (production DDL, run for real).

``alembic/tests/pg_docker.py`` boots one Postgres 17 container and runs the actual Alembic chain
to ``head`` -- including ``0054_tenant_external_kms`` -- so the SQL in
``services/envelope/repository.py`` is proven against the migration's own schema, indexes,
constraints and RBAC grants rather than a hand-copied DDL. It also drives the real
``TenantEnvelopeService`` through that repository (key creation, rotation, BYOK re-wrap) with the
cloud KMS faked only at the socket.

Skipped (not failed) where the ``docker`` CLI is unavailable, like every other real-Postgres test
in this repo (see ``pg_docker.py``).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg2
import pytest

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[3] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services.bundle_install_dal import build_install_dal  # noqa: E402
from services.envelope import TenantEnvelopeService  # noqa: E402
from services.envelope.kms_adapter import PROVIDER_AWS  # noqa: E402
from services.envelope.models import (  # noqa: E402
    CONFIG_ACTIVE,
    CONFIG_PENDING,
    KEK_KIND_CUSTOMER,
    KEK_KIND_PLATFORM,
    KEY_ACTIVE,
    KEY_RETIRED,
    DekRecord,
)
from services.envelope.repository import (  # noqa: E402
    KeyConflictError,
    PenguinDalEnvelopeRepository,
)
from tests.envelope.conftest import AWS_KEY_ARN, AWS_ROLE_ARN  # noqa: E402
from tests.envelope.fakes import CountingPlatformKek, FakeGate  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

SLOT = {
    "table": "platform_integrations",
    "column": "access_token",
    "row_uuid": uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
}
DATA_PLANE_ROLES = (
    "waddles_publisher",
    "svc_ingest",
    "svc_process",
    "svc_action",
    "svc_streaming",
    "webui",
)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container, migrated to ``head``, shared by this module."""
    with migrated_postgres("hub-api-envelope-kms") as db:
        yield db


@pytest.fixture(scope="module")
def sql(pg_db: PgTestDatabase) -> Iterator[psycopg2.extensions.connection]:
    """A superuser connection for seeding and schema assertions (autocommit)."""
    conn = psycopg2.connect(pg_db.dsn)
    conn.autocommit = True
    yield conn
    conn.close()


def _scalar(conn, query: str, params: tuple = ()) -> object:  # type: ignore[no-untyped-def]
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        return row[0] if row else None


@pytest.fixture
def tenant_ids(sql) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """Two fresh tenants per test (names are unique so tests never collide)."""
    ids = []
    for _ in range(2):
        slug = f"t-{uuid.uuid4().hex[:12]}"
        ids.append(_scalar(sql, "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (slug,)))
    return int(ids[0]), int(ids[1])  # type: ignore[arg-type]


@pytest.fixture
async def repo(pg_db: PgTestDatabase) -> AsyncIterator[PenguinDalEnvelopeRepository]:
    dal = await build_install_dal(pg_db.dsn, pool_size=4)
    yield PenguinDalEnvelopeRepository(dal)
    await dal.close()


class TestMigratedSchema:
    """What migration 0049 actually produced."""

    def test_tables_and_the_one_active_index_exist(self, sql) -> None:  # type: ignore[no-untyped-def]
        assert _scalar(sql, "SELECT to_regclass('keystore.tenant_encryption_keys')") is not None
        assert _scalar(sql, "SELECT to_regclass('tenant_kms_configs')") is not None
        definition = str(
            _scalar(
                sql,
                "SELECT indexdef FROM pg_indexes WHERE indexname = "
                "'uq_tenant_encryption_keys_one_active'",
            )
        )
        assert "UNIQUE" in definition and "status" in definition and "active" in definition

    def test_the_key_store_lives_in_its_own_schema_not_alongside_app_tables(self, sql) -> None:  # type: ignore[no-untyped-def]
        assert _scalar(sql, "SELECT to_regclass('public.tenant_encryption_keys')") is None

    def test_hub_api_is_the_only_role_that_can_touch_keys(self, sql) -> None:  # type: ignore[no-untyped-def]
        def can(role: str, table: str, privilege: str) -> bool:
            return bool(
                _scalar(sql, "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege))
            )

        keys, configs = "keystore.tenant_encryption_keys", "tenant_kms_configs"
        for privilege in ("SELECT", "INSERT", "UPDATE"):
            assert can("hub_api", keys, privilege)
        assert not can("hub_api", keys, "DELETE")  # keys are retired, never hard-deleted
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert can("hub_api", configs, privilege)
        for role in DATA_PLANE_ROLES:
            for table in (keys, configs):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not can(role, table, privilege), (role, table, privilege)
        assert not bool(
            _scalar(sql, "SELECT has_schema_privilege('svc_ingest', 'keystore', 'USAGE')")
        )

    def test_a_live_key_must_carry_a_wrapped_dek(self, sql, tenant_ids) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(psycopg2.errors.CheckViolation), sql.cursor() as cur:
            cur.execute(
                "INSERT INTO keystore.tenant_encryption_keys "
                "(tenant_id, dek_version, wrapped_dek, kek_ref) VALUES (%s, 1, NULL, 'x')",
                (tenant_ids[0],),
            )

    def test_unknown_kek_kinds_are_rejected(self, sql, tenant_ids) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(psycopg2.errors.CheckViolation), sql.cursor() as cur:
            cur.execute(
                "INSERT INTO keystore.tenant_encryption_keys "
                "(tenant_id, dek_version, wrapped_dek, kek_ref, kek_kind) "
                "VALUES (%s, 1, '\\x00', 'x', 'plaintext')",
                (tenant_ids[0],),
            )

    def test_config_rows_are_removed_with_their_tenant(self, sql, tenant_ids) -> None:  # type: ignore[no-untyped-def]
        with sql.cursor() as cur:
            cur.execute(
                "INSERT INTO tenant_kms_configs (tenant_id, provider, key_ref, external_id) "
                "VALUES (%s, 'aws_kms', 'k', 'e')",
                (tenant_ids[0],),
            )
            cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_ids[0],))
        assert (
            _scalar(
                sql,
                "SELECT count(*) FROM tenant_kms_configs WHERE tenant_id = %s",
                (tenant_ids[0],),
            )
            == 0
        )

    def test_the_ddl_is_idempotent(self, sql) -> None:  # type: ignore[no-untyped-def]
        """Order-independent with the open DEK-broker migration: re-applying is a no-op."""
        import importlib.util

        path = Path(__file__).resolve().parents[3] / "alembic" / "versions"
        spec = importlib.util.spec_from_file_location(
            "migration_0054", path / "0054_tenant_external_kms.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with sql.cursor() as cur:
            for statement in module.DDL_STATEMENTS:
                cur.execute(statement)


class TestKeyRepository:
    """Every statement, against the real schema."""

    async def test_insert_get_list_and_usage(self, repo, tenant_ids) -> None:  # type: ignore[no-untyped-def]
        tenant, _ = tenant_ids
        assert await repo.get_active(tenant) is None
        first = await repo.insert_active(tenant, b"wrapped-1", KEK_KIND_PLATFORM, "platform:abc")
        assert (first.dek_version, first.status, first.usage_count) == (1, KEY_ACTIVE, 0)
        assert first.wrapped_dek == b"wrapped-1"
        assert (await repo.get_active(tenant)).dek_version == 1  # type: ignore[union-attr]
        assert (await repo.get_version(tenant, 1)).id == first.id  # type: ignore[union-attr]
        assert await repo.get_version(tenant, 9) is None
        assert await repo.add_usage(tenant, 1, 5) == 5
        assert await repo.add_usage(tenant, 1, 7) == 12
        assert await repo.add_usage(tenant, 99, 1) == 0
        assert [r.dek_version for r in await repo.list_keys(tenant)] == [1]

    async def test_a_second_active_key_is_a_conflict(self, repo, tenant_ids) -> None:  # type: ignore[no-untyped-def]
        tenant, _ = tenant_ids
        await repo.insert_active(tenant, b"w1", KEK_KIND_PLATFORM, "platform:abc")
        with pytest.raises(KeyConflictError):
            await repo.insert_active(tenant, b"w2", KEK_KIND_PLATFORM, "platform:abc")

    async def test_racing_first_key_creation_has_exactly_one_winner(self, repo, tenant_ids) -> None:  # type: ignore[no-untyped-def]
        tenant, _ = tenant_ids
        results = await asyncio.gather(
            *[
                repo.insert_active(tenant, f"w{i}".encode(), KEK_KIND_PLATFORM, "platform:abc")
                for i in range(6)
            ],
            return_exceptions=True,
        )
        winners = [r for r in results if not isinstance(r, BaseException)]
        losers = [r for r in results if isinstance(r, KeyConflictError)]
        assert len(winners) == 1 and len(losers) == 5
        assert len(await repo.list_keys(tenant)) == 1

    async def test_rotation_retires_the_old_version_and_never_deletes_it(
        self, repo, tenant_ids
    ) -> None:  # type: ignore[no-untyped-def]
        tenant, _ = tenant_ids
        await repo.insert_active(tenant, b"w1", KEK_KIND_PLATFORM, "platform:abc")
        second = await repo.rotate_active(
            tenant,
            expected_version=1,
            wrapped_dek=b"w2",
            kek_kind=KEK_KIND_CUSTOMER,
            kek_ref=AWS_KEY_ARN,
        )
        assert (second.dek_version, second.kek_kind) == (2, KEK_KIND_CUSTOMER)
        statuses = {r.dek_version: r.status for r in await repo.list_keys(tenant)}
        assert statuses == {1: KEY_RETIRED, 2: KEY_ACTIVE}
        old = await repo.get_version(tenant, 1)
        assert old is not None and old.wrapped_dek == b"w1" and old.retired_at is not None

    async def test_rotation_with_a_stale_expectation_conflicts_and_changes_nothing(  # type: ignore[no-untyped-def]
        self, repo, tenant_ids
    ) -> None:
        tenant, _ = tenant_ids
        await repo.insert_active(tenant, b"w1", KEK_KIND_PLATFORM, "platform:abc")
        for expected in (None, 5):
            with pytest.raises(KeyConflictError):
                await repo.rotate_active(
                    tenant,
                    expected_version=expected,
                    wrapped_dek=b"w2",
                    kek_kind=KEK_KIND_PLATFORM,
                    kek_ref="platform:abc",
                )
        assert [(r.dek_version, r.status) for r in await repo.list_keys(tenant)] == [
            (1, KEY_ACTIVE)
        ]

    async def test_replace_wrapped_is_compare_and_swap(self, repo, tenant_ids) -> None:  # type: ignore[no-untyped-def]
        tenant, _ = tenant_ids
        record = await repo.insert_active(tenant, b"w1", KEK_KIND_PLATFORM, "platform:abc")
        assert await repo.replace_wrapped(
            record, new_wrapped=b"w1b", kek_kind=KEK_KIND_CUSTOMER, kek_ref=AWS_KEY_ARN
        )
        # The same (now stale) snapshot cannot swap again.
        assert not await repo.replace_wrapped(
            record, new_wrapped=b"w1c", kek_kind=KEK_KIND_PLATFORM, kek_ref="platform:abc"
        )
        current = await repo.get_version(tenant, 1)
        assert current is not None
        assert (current.wrapped_dek, current.kek_kind, current.kek_ref) == (
            b"w1b",
            KEK_KIND_CUSTOMER,
            AWS_KEY_ARN,
        )

    async def test_every_query_is_scoped_to_its_tenant(self, repo, tenant_ids) -> None:  # type: ignore[no-untyped-def]
        """regression: external-kms -- no statement can address another tenant's key or config."""
        a, b = tenant_ids
        record = await repo.insert_active(a, b"a-secret", KEK_KIND_PLATFORM, "platform:abc")
        assert await repo.get_active(b) is None
        assert await repo.list_keys(b) == []
        assert await repo.get_version(b, 1) is None
        assert await repo.add_usage(b, 1, 3) == 0
        # B cannot CAS-swap A's row even holding A's row id (tenant is part of the predicate).
        forged = DekRecord(
            id=record.id,
            tenant_id=b,
            dek_version=1,
            wrapped_dek=b"a-secret",
            kek_kind=KEK_KIND_PLATFORM,
            kek_ref="platform:abc",
            status=KEY_ACTIVE,
            usage_count=0,
            activated_at=None,
        )
        assert not await repo.replace_wrapped(
            forged, new_wrapped=b"evil", kek_kind=KEK_KIND_PLATFORM, kek_ref="platform:abc"
        )
        assert (await repo.get_active(a)).wrapped_dek == b"a-secret"  # type: ignore[union-attr]
        await repo.upsert(
            a, provider="aws_kms", key_ref="k", region=None, principal=None, new_external_id="e"
        )
        assert await repo.get(b) is None


class TestConfigRepository:
    """``tenant_kms_configs`` semantics."""

    async def test_upsert_keeps_the_external_id_and_resets_to_pending(
        self, repo, tenant_ids
    ) -> None:  # type: ignore[no-untyped-def]
        tenant, _ = tenant_ids
        first = await repo.upsert(
            tenant,
            provider="aws_kms",
            key_ref=AWS_KEY_ARN,
            region="us-east-1",
            principal=AWS_ROLE_ARN,
            new_external_id="e" * 48,
        )
        assert (first.status, first.external_id) == (CONFIG_PENDING, "e" * 48)
        await repo.set_status(tenant, CONFIG_ACTIVE, verified=True)
        active = await repo.get(tenant)
        assert active is not None and active.status == CONFIG_ACTIVE
        assert active.last_verified_at is not None and active.last_error_code is None

        edited = await repo.upsert(
            tenant,
            provider="aws_kms",
            key_ref=AWS_KEY_ARN.replace("1234abcd", "ffffffff"),
            region="us-east-1",
            principal=AWS_ROLE_ARN,
            new_external_id="DIFFERENT",
        )
        assert edited.external_id == "e" * 48  # the customer's trust policy pins it
        assert edited.status == CONFIG_PENDING and edited.last_verified_at is None
        assert edited.created_at == first.created_at and edited.updated_at is not None

    async def test_set_status_records_the_error_code_without_stamping_verification(  # type: ignore[no-untyped-def]
        self, repo, tenant_ids
    ) -> None:
        tenant, _ = tenant_ids
        await repo.upsert(
            tenant,
            provider="gcp_kms",
            key_ref="k",
            region=None,
            principal=None,
            new_external_id="e",
        )
        await repo.set_status(tenant, "revoked", error_code="PERMISSION_DENIED")
        config = await repo.get(tenant)
        assert config is not None
        assert (config.status, config.last_error_code, config.last_verified_at) == (
            "revoked",
            "PERMISSION_DENIED",
            None,
        )

    async def test_delete_removes_only_that_tenants_config(self, repo, tenant_ids) -> None:  # type: ignore[no-untyped-def]
        a, b = tenant_ids
        for tenant in (a, b):
            await repo.upsert(
                tenant,
                provider="aws_kms",
                key_ref="k",
                region=None,
                principal=None,
                new_external_id="e",
            )
        await repo.delete(a)
        assert await repo.get(a) is None and await repo.get(b) is not None

    async def test_status_check_constraint_rejects_unknown_states(self, repo, tenant_ids) -> None:  # type: ignore[no-untyped-def]
        tenant, _ = tenant_ids
        await repo.upsert(
            tenant,
            provider="aws_kms",
            key_ref="k",
            region=None,
            principal=None,
            new_external_id="e",
        )
        with pytest.raises(Exception, match="status"):
            await repo.set_status(tenant, "owned")


class TestServiceOnRealPostgres:
    """The real service + real SQL, with the cloud KMS faked only at the socket."""

    @pytest.fixture
    def service(self, repo, make_harness):  # type: ignore[no-untyped-def]
        harness = make_harness(entitled=set())
        kek = CountingPlatformKek(b"\x07" * 32)
        service = TenantEnvelopeService(
            keys=repo,
            configs=repo,
            registry=harness.providers.registry,
            platform_kek=lambda: kek,
            gate=FakeGate(),
            clock=harness.clock,
        )
        return service, harness, kek

    async def test_baseline_byok_activation_rotation_and_exit_ramp(  # type: ignore[no-untyped-def]
        self, service, repo, tenant_ids
    ) -> None:
        svc, harness, kek = service
        tenant, _ = tenant_ids
        slug = f"slug-{tenant}"
        svc._gate.entitled.add(slug)  # type: ignore[attr-defined]

        # Baseline: platform KEK, no entitlement asked, row visible in the real key store.
        sealed = await svc.encrypt(tenant, b"top-secret", **SLOT)
        (row,) = await repo.list_keys(tenant)
        assert row.kek_kind == KEK_KIND_PLATFORM and row.wrapped_dek != b"top-secret"

        # BYOK: configure -> customer sets up trust -> activate re-wraps the persisted row.
        config = await svc.configure_external_kms(
            tenant,
            tenant_slug=slug,
            provider=PROVIDER_AWS,
            key_ref=AWS_KEY_ARN,
            region=None,
            principal=AWS_ROLE_ARN,
        )
        harness.provider_side(config)
        config, report = await svc.activate_external_kms(tenant, tenant_slug=slug)
        assert (config.status, report.ok) == (CONFIG_ACTIVE, True)
        (row,) = await repo.list_keys(tenant)
        assert (row.kek_kind, row.kek_ref) == (KEK_KIND_CUSTOMER, AWS_KEY_ARN)

        # The pre-BYOK ciphertext still opens (the DEK never changed), after a cold cache.
        svc.invalidate(tenant)
        assert await svc.decrypt(tenant, sealed, **SLOT) == b"top-secret"

        # Rotation under BYOK, then the ungated exit ramp back to the baseline.
        await svc.rotate_dek(tenant, tenant_slug=slug)
        svc._gate.entitled.clear()  # type: ignore[attr-defined]
        assert (await svc.disable_external_kms(tenant)).ok
        assert {r.kek_kind for r in await repo.list_keys(tenant)} == {KEK_KIND_PLATFORM}
        assert await repo.get(tenant) is None
        harness.aws.stop()  # the customer KMS is no longer needed to read anything
        svc.invalidate(tenant)
        assert await svc.decrypt(tenant, sealed, **SLOT) == b"top-secret"
