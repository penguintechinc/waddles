"""M4 (row-bound AAD sealing) for `platform_integrations` + the reseal job, on REAL Postgres.

regression: M4 -- a connection ciphertext is bound to the row it was sealed for. The sqlite suites
prove the logic; only the migrated Postgres proves the real column (`seal_version`, migration 0063),
the pydal column list against the real DDL, and that `scripts/reseal_connections.py` locks and
upgrades real rows. Skipped (never failed) where the `docker` CLI is unavailable.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg2
import pytest
from flask_core.database import AsyncDAL

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services import community_connections as svc  # noqa: E402
from services import credential_resolver as cr  # noqa: E402
from services.platform_integrations_crypto import (  # noqa: E402
    PlatformCredentialCryptoError,
    connection_aad,
    platform_connection_aad,
)
from services.provider_connections.vault import open_secret, seal_secret  # noqa: E402
from services.schema import (  # noqa: E402
    bind_auth_tables,
    bind_bar_citizen_tables,
    bind_platform_tables,
)

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_KEY = "ab" * 32  # throwaway test key
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reseal_connections.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("reseal_connections_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reseal = _load_script()


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0063)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-seal-aad") as db:
        yield db


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)


def _sql(db: PgTestDatabase, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return list(cur.fetchall()) if cur.description else []
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean(pg_db: PgTestDatabase) -> None:
    """Each test starts from empty connection tables (module-scoped container)."""
    _sql(pg_db, "TRUNCATE platform_connections, platform_integrations RESTART IDENTITY CASCADE")


@pytest.fixture
def adal(pg_db: PgTestDatabase) -> Iterator[AsyncDAL]:
    """A real pydal `AsyncDAL` on the migrated container (`migrate=False`: the schema is real)."""
    async_dal = AsyncDAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    bind_platform_tables(async_dal.dal, migrate=False)
    yield async_dal
    async_dal.dal.close()


@pytest.fixture
def cr_dal(pg_db: PgTestDatabase) -> Iterator[Any]:
    """The pydal path `credential_resolver` uses, on the real migrated tables (`migrate=False`)."""
    from pydal import Field

    async_dal = AsyncDAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug"),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean"),
        Field("is_active", "boolean"),
        Field("config", "json"),
        migrate=False,
    )
    bind_auth_tables(dal, migrate=False)
    bind_bar_citizen_tables(dal, migrate=False)
    yield dal
    dal.close()


def _community(db: PgTestDatabase, name: str) -> int:
    return int(_sql(db, "INSERT INTO communities (name) VALUES (%s) RETURNING id", (name,))[0][0])


def _integration_row(db: PgTestDatabase, community_id: int, provider: str) -> tuple[Any, ...]:
    return _sql(
        db,
        "SELECT id, access_token, refresh_token, seal_version FROM platform_integrations "
        "WHERE community_id=%s AND platform=%s AND integration_type='community_oauth'",
        (community_id, provider),
    )[0]


def _insert_legacy_integration(
    db: PgTestDatabase, community_id: int, provider: str, tag: str
) -> None:
    """A `community_oauth` row exactly as a pre-M4 writer left it: no AAD, seal_version 0."""
    _sql(
        db,
        "INSERT INTO platform_integrations (platform, integration_type, community_id, "
        "access_token, refresh_token, is_active, is_encrypted, seal_version) "
        "VALUES (%s, 'community_oauth', %s, %s, %s, TRUE, TRUE, 0)",
        (provider, community_id, seal_secret(f"acc-{tag}"), seal_secret(f"ref-{tag}")),
    )


def _insert_v1_integration(db: PgTestDatabase, community_id: int, provider: str, tag: str) -> None:
    """A `community_oauth` row as the current writer stores it: AAD-bound, seal_version 1.

    Written with SQL because the pydal `scopes` binding (`list:string`) cannot insert into the real
    `TEXT[]` column on Postgres -- a pre-existing gap outside this change (the write path itself is
    covered over sqlite in `test_community_connections_service.py`).
    """
    aad = connection_aad(community_id=community_id, provider=provider)
    _sql(
        db,
        "INSERT INTO platform_integrations (platform, integration_type, community_id, "
        "access_token, refresh_token, is_active, is_encrypted, seal_version) "
        "VALUES (%s, 'community_oauth', %s, %s, %s, TRUE, TRUE, 1)",
        (
            provider,
            community_id,
            seal_secret(f"acc-{tag}", aad=aad),
            seal_secret(f"ref-{tag}", aad=aad),
        ),
    )


def _insert_legacy_connection(db: PgTestDatabase, tenant_id: int, tag: str) -> int:
    row = _sql(
        db,
        "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, "
        "access_token, refresh_token, seal_version) "
        "VALUES (%s, 'twitch', 'twitch_channel', %s, %s, %s, 0) RETURNING id",
        (tenant_id, f"res-{tag}", seal_secret(f"acc-{tag}"), seal_secret(f"ref-{tag}")),
    )
    return int(row[0][0])


def _tenant(db: PgTestDatabase, slug: str) -> int:
    return int(_sql(db, "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (slug,))[0][0])


class TestPlatformIntegrationsAad:
    async def test_a_v1_row_opens_only_under_its_own_identity(
        self, adal: AsyncDAL, pg_db: PgTestDatabase
    ) -> None:
        community = _community(pg_db, "c-v1")
        _insert_v1_integration(pg_db, community, "twitch", "v1")
        tokens = await svc.get_decrypted_tokens(adal, community, "twitch")
        assert tokens is not None
        assert (tokens.access_token, tokens.refresh_token) == ("acc-v1", "ref-v1")
        _, access_ct, _, version = _integration_row(pg_db, community, "twitch")
        assert version == 1
        with pytest.raises(PlatformCredentialCryptoError):
            open_secret(access_ct, aad=None, seal_version=1)

    async def test_ciphertext_swapped_between_rows_fails_open(
        self, adal: AsyncDAL, pg_db: PgTestDatabase
    ) -> None:
        one, two = _community(pg_db, "c-swap-1"), _community(pg_db, "c-swap-2")
        _insert_v1_integration(pg_db, one, "twitch", "one")
        _insert_v1_integration(pg_db, two, "twitch", "two")
        id_one, id_two = (
            _integration_row(pg_db, one, "twitch")[0],
            _integration_row(pg_db, two, "twitch")[0],
        )
        _sql(
            pg_db,
            "UPDATE platform_integrations SET access_token = CASE id WHEN %s THEN "
            "(SELECT access_token FROM platform_integrations WHERE id=%s) ELSE "
            "(SELECT access_token FROM platform_integrations WHERE id=%s) END WHERE id IN (%s, %s)",
            (id_one, id_two, id_one, id_one, id_two),
        )
        # Neither row may return the other community's plaintext -- both fail closed.
        for community in (one, two):
            with pytest.raises(PlatformCredentialCryptoError):
                await svc.get_decrypted_tokens(adal, community, "twitch")

    async def test_the_same_ciphertext_does_not_open_under_another_provider(
        self, adal: AsyncDAL, pg_db: PgTestDatabase
    ) -> None:
        community = _community(pg_db, "c-provider-swap")
        _insert_v1_integration(pg_db, community, "twitch", "t")
        _, access_ct, _, _ = _integration_row(pg_db, community, "twitch")
        _sql(
            pg_db,
            "UPDATE platform_integrations SET platform='spotify' WHERE community_id=%s",
            (community,),
        )
        with pytest.raises(PlatformCredentialCryptoError):
            await svc.get_decrypted_tokens(adal, community, "spotify")
        assert access_ct  # the row existed and was examined

    async def test_legacy_v0_row_still_opens_then_reseals_on_read(
        self, adal: AsyncDAL, pg_db: PgTestDatabase
    ) -> None:
        community = _community(pg_db, "c-legacy")
        _insert_legacy_integration(pg_db, community, "twitch", "legacy")
        tokens = await svc.get_decrypted_tokens(adal, community, "twitch")
        assert tokens is not None
        assert (tokens.access_token, tokens.refresh_token) == ("acc-legacy", "ref-legacy")
        _, access_ct, refresh_ct, version = _integration_row(pg_db, community, "twitch")
        assert version == 1
        aad = connection_aad(community_id=community, provider="twitch")
        assert open_secret(access_ct, aad=aad, seal_version=1) == "acc-legacy"
        assert open_secret(refresh_ct, aad=aad, seal_version=1) == "ref-legacy"

    async def test_refreshing_a_legacy_row_reseals_its_untouched_refresh_token(
        self, adal: AsyncDAL, pg_db: PgTestDatabase
    ) -> None:
        community = _community(pg_db, "c-legacy-refresh")
        _insert_legacy_integration(pg_db, community, "twitch", "lr")
        await svc.store_refreshed_access_token(
            adal, community, "twitch", access_token="acc-new", expires_at=None
        )
        tokens = await svc.get_decrypted_tokens(adal, community, "twitch")
        assert tokens is not None
        assert (tokens.access_token, tokens.refresh_token) == ("acc-new", "ref-lr")
        assert _integration_row(pg_db, community, "twitch")[3] == 1


class TestDowngradeResistance:
    """regression: M4 -- flipping `seal_version` to 0 must not switch the AAD control off."""

    async def test_downgrade_column_does_not_disable_aad(
        self, adal: AsyncDAL, pg_db: PgTestDatabase
    ) -> None:
        one, two = _community(pg_db, "c-down-1"), _community(pg_db, "c-down-2")
        _insert_v1_integration(pg_db, one, "twitch", "one")
        _insert_v1_integration(pg_db, two, "twitch", "two")
        _sql(
            pg_db, "UPDATE platform_integrations SET seal_version = 0 WHERE community_id=%s", (one,)
        )

        # (a) the real ciphertext still opens (via the AAD) ...
        tokens = await svc.get_decrypted_tokens(adal, one, "twitch")
        assert tokens is not None and tokens.access_token == "acc-one"
        # ... and the lazy reseal did NOT silently re-bless the flipped row
        assert _integration_row(pg_db, one, "twitch")[3] == 0

        # (b) a ciphertext substituted from another row still fails closed, column or not
        stolen = _integration_row(pg_db, two, "twitch")[1]
        _sql(
            pg_db,
            "UPDATE platform_integrations SET access_token=%s WHERE community_id=%s",
            (stolen, one),
        )
        with pytest.raises(PlatformCredentialCryptoError):
            await svc.get_decrypted_tokens(adal, one, "twitch")

    async def test_a_substituted_unbound_ciphertext_is_refused_once_the_flag_is_set(
        self, adal: AsyncDAL, pg_db: PgTestDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        community = _community(pg_db, "c-flag")
        _insert_legacy_integration(pg_db, community, "twitch", "legacy")
        tokens = await svc.get_decrypted_tokens(adal, community, "twitch")  # window: still opens
        assert tokens is not None and tokens.access_token == "acc-legacy"

        other = _community(pg_db, "c-flag-2")
        _insert_legacy_integration(pg_db, other, "twitch", "legacy2")
        monkeypatch.setenv("CONNECTION_SEAL_REFUSE_LEGACY", "1")
        with pytest.raises(PlatformCredentialCryptoError):  # true legacy v0 row now fails closed
            await svc.get_decrypted_tokens(adal, other, "twitch")
        assert _integration_row(pg_db, other, "twitch")[3] == 0  # and was left untouched

    async def test_refresh_stamp_seals_a_plaintext_refresh_token(
        self, adal: AsyncDAL, pg_db: PgTestDatabase
    ) -> None:
        community = _community(pg_db, "c-plain")
        _sql(
            pg_db,
            "INSERT INTO platform_integrations (platform, integration_type, community_id, "
            "access_token, refresh_token, is_active, is_encrypted, seal_version) "
            "VALUES ('twitch', 'community_oauth', %s, 'plain-acc', 'plain-ref', TRUE, FALSE, 0)",
            (community,),
        )
        await svc.store_refreshed_access_token(
            adal, community, "twitch", access_token="acc-new", expires_at=None
        )
        tokens = await svc.get_decrypted_tokens(adal, community, "twitch")
        assert tokens is not None
        assert (tokens.access_token, tokens.refresh_token) == ("acc-new", "plain-ref")
        _, _, refresh_ct, version = _integration_row(pg_db, community, "twitch")
        assert version == 1 and refresh_ct != "plain-ref"


class TestCredentialResolverOnPostgres:
    """The `credential_resolver` pydal path on real Postgres: uuid `public_id`, guarded reseal."""

    def _tenant(self, pg_db: PgTestDatabase) -> int:
        return _tenant(pg_db, f"t-cr-{uuid4().hex[:6]}")

    def test_upsert_binds_a_real_uuid_public_id_and_round_trips(
        self, cr_dal: Any, pg_db: PgTestDatabase
    ) -> None:
        tenant = self._tenant(pg_db)
        connection_id = cr.upsert_platform_connection(
            cr_dal,
            tenant_id=tenant,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="chan-1",
            access_token="acc-1",
            refresh_token="ref-1",
            installed_by_user_id=None,
        )
        version, public_id = _sql(
            pg_db,
            "SELECT seal_version, public_id::text FROM platform_connections WHERE id=%s",
            (connection_id,),
        )[0]
        assert version == 1 and str(UUID(public_id)) == public_id  # a real uuid column value
        got = cr.get_platform_connection(
            cr_dal, tenant_id=tenant, platform="twitch", resource_id="chan-1"
        )
        assert got is not None and (got.access_token, got.refresh_token) == ("acc-1", "ref-1")
        # an update keeps the row's identity (its tokens' AAD)
        cr.upsert_platform_connection(
            cr_dal,
            tenant_id=tenant,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="chan-1",
            access_token="acc-2",
            refresh_token=None,
            installed_by_user_id=None,
        )
        assert _sql(
            pg_db, "SELECT public_id::text FROM platform_connections WHERE id=%s", (connection_id,)
        ) == [(public_id,)]

    def test_legacy_row_opens_then_the_guarded_update_reseals_it(
        self, cr_dal: Any, pg_db: PgTestDatabase
    ) -> None:
        tenant = self._tenant(pg_db)
        connection_id = _insert_legacy_connection(pg_db, tenant, "cr-legacy")
        got = cr.get_platform_connection(
            cr_dal, tenant_id=tenant, platform="twitch", resource_id="res-cr-legacy"
        )
        assert got is not None and (got.access_token, got.refresh_token) == (
            "acc-cr-legacy",
            "ref-cr-legacy",
        )
        access_ct, public_id, version = _sql(
            pg_db,
            "SELECT access_token, public_id::text, seal_version FROM platform_connections "
            "WHERE id=%s",
            (connection_id,),
        )[0]
        assert version == 1
        aad = platform_connection_aad(public_id=UUID(public_id), platform="twitch")
        assert open_secret(access_ct, aad=aad, seal_version=1) == "acc-cr-legacy"

    def test_a_flipped_column_is_not_re_blessed_and_a_swap_fails_closed(
        self, cr_dal: Any, pg_db: PgTestDatabase
    ) -> None:
        tenant = self._tenant(pg_db)
        ids = []
        for res in ("chan-x", "chan-y"):
            ids.append(
                cr.upsert_platform_connection(
                    cr_dal,
                    tenant_id=tenant,
                    platform="twitch",
                    resource_type="twitch_channel",
                    resource_id=res,
                    access_token=f"acc-{res}",
                    refresh_token=None,
                    installed_by_user_id=None,
                )
            )
        _sql(pg_db, "UPDATE platform_connections SET seal_version = 0 WHERE id=%s", (ids[0],))
        got = cr.get_platform_connection(
            cr_dal, tenant_id=tenant, platform="twitch", resource_id="chan-x"
        )
        assert got is not None and got.access_token == "acc-chan-x"
        assert _sql(
            pg_db, "SELECT seal_version FROM platform_connections WHERE id=%s", (ids[0],)
        ) == [(0,)]
        _sql(
            pg_db,
            "UPDATE platform_connections SET access_token = "
            "(SELECT access_token FROM platform_connections WHERE id=%s) WHERE id=%s",
            (ids[1], ids[0]),
        )
        with pytest.raises(cr.TransportUnavailable):
            cr.get_platform_connection(
                cr_dal, tenant_id=tenant, platform="twitch", resource_id="chan-x"
            )


class TestResealJob:
    def _run(self, db: PgTestDatabase, *argv: str) -> tuple[int, str]:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = reseal.main(list(argv), connect=lambda: psycopg2.connect(db.dsn))
        return code, buffer.getvalue().strip()

    def _seed_three(self, db: PgTestDatabase) -> tuple[int, int]:
        tenant = _tenant(db, f"t-reseal-{uuid4().hex[:6]}")
        community = _community(db, f"c-reseal-{uuid4().hex[:6]}")
        _insert_legacy_integration(db, community, "twitch", "i1")
        first = _insert_legacy_connection(db, tenant, "c1")
        _insert_legacy_connection(db, tenant, "c2")
        return community, first

    def test_reseal_reports_examined_count(self, pg_db: PgTestDatabase) -> None:
        community, conn_id = self._seed_three(pg_db)
        code, out = self._run(pg_db, "--batch-size", "2")
        assert (code, out) == (0, "examined=3 resealed=3 failed=0")
        # everything is now AAD-bound and opens under its own row identity
        assert _integration_row(pg_db, community, "twitch")[3] == 1
        access_ct, public_id = _sql(
            pg_db,
            "SELECT access_token, public_id::text FROM platform_connections WHERE id=%s",
            (conn_id,),
        )[0]
        from uuid import UUID

        aad = platform_connection_aad(public_id=UUID(public_id), platform="twitch")
        assert open_secret(access_ct, aad=aad, seal_version=1) == "acc-c1"
        # a second run examines (and verifies) every row but reseals none -- and still exits 0
        assert self._run(pg_db) == (0, "examined=3 resealed=0 failed=0")

    def test_reseal_tamper_row_counts_failed(self, pg_db: PgTestDatabase) -> None:
        tenant = _tenant(pg_db, f"t-tamper-{uuid4().hex[:6]}")
        good_one = _insert_legacy_connection(pg_db, tenant, "g1")
        bad = _insert_legacy_connection(pg_db, tenant, "bad")
        good_two = _insert_legacy_connection(pg_db, tenant, "g2")
        _sql(pg_db, "UPDATE platform_connections SET access_token='AAAA' WHERE id=%s", (bad,))
        code, out = self._run(pg_db)
        assert (code, out) == (1, "examined=3 resealed=2 failed=1")
        versions = dict(_sql(pg_db, "SELECT id, seal_version FROM platform_connections"))
        assert versions == {good_one: 1, bad: 0, good_two: 1}  # the corrupt row is left untouched

    def test_nothing_examined_is_not_a_pass(self, pg_db: PgTestDatabase) -> None:
        assert self._run(pg_db)[0] == 0  # empty tables, nothing expected: genuinely clean
        code, out = self._run(pg_db, "--expect-rows")
        assert (code, out) == (2, "examined=0 resealed=0 failed=0")

    def test_exit_code_fails_when_rows_exist_but_none_were_examined(self) -> None:
        assert reseal.exit_code(reseal.Counts(total_rows=5), expect_rows=False) == 2
        assert (
            reseal.exit_code(reseal.Counts(examined=5, resealed=5, total_rows=5), expect_rows=False)
            == 0
        )
        assert (
            reseal.exit_code(reseal.Counts(examined=3, failed=1, total_rows=3), expect_rows=True)
            == 1
        )

    def test_a_missing_key_is_cannot_run_not_a_thousand_row_failures(
        self, pg_db: PgTestDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_three(pg_db)
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
        code, out = self._run(pg_db)
        assert (code, out) == (3, "")
        assert {v for (v,) in _sql(pg_db, "SELECT seal_version FROM platform_connections")} == {0}

    def test_an_unreachable_database_is_cannot_run(self) -> None:
        def refuse() -> Any:
            raise psycopg2.OperationalError("connection refused")

        assert reseal.main([], connect=refuse) == 3
