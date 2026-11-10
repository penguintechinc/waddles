"""Approval / upgrade / uninstall drive the `db` v2 table lifecycle (real Postgres).

The hub-api install state lives in the sqlite-backed `install_dal` fixture; the bundle
tables are provisioned on a REAL Postgres (`bundle_data_pg_conn`, testcontainers) through
`BundleDataProvisioner`, so these tests exercise the actual DDL, registry rows and the
fail-closed abort path. Skipped (not failed) without Docker, like the other `bundle_data_pg`
tests.
"""

# ruff: noqa: S608 -- test SQL interpolates only server-derived physical table names

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import psycopg2
import pytest

from services.bundle_approval_service import install_version_globally, uninstall_globally
from services.bundle_data_provisioner import (
    BundleDataProvisioner,
    provider_for,
    schema_from_manifest,
)
from services.bundle_data_v2 import derive_v2_table_identity
from services.errors import ApiError

_APP = "waddles.cal.test"
_OTHER_APP = "waddles.other.test"
_RAW_SCHEMA: dict[str, Any] = {
    "tables": [
        {
            "name": "events",
            "columns": [
                {"name": "title", "type": "text(200)", "nullable": False},
                {"name": "kind", "type": "int4", "nullable": True},
            ],
            "indexes": [{"columns": ["kind"]}],
        },
        {
            "name": "subs",
            "columns": [{"name": "slug", "type": "text(64)", "nullable": False}],
            "indexes": [{"columns": ["slug"], "unique": True}],
        },
    ]
}


def _manifest(app_id: str = _APP, version: str = "1.0.0", schema: Any = _RAW_SCHEMA) -> dict:
    data: dict[str, Any] = {"tables": ["events", "subs"]}
    if schema is not None:
        data["schema"] = copy.deepcopy(schema)
    return {
        "schema_version": 2,
        "app_id": app_id,
        "name": "Cal",
        "version": version,
        "feature": "waddles.cal",
        "module": "cal",
        "provider": "thirdparty",
        "language": "python",
        "artifact": "source",
        "stages": {
            "process": {
                "entry": "x:y",
                "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
            },
        },
        "data": data,
    }


def _phys(logical: str, app: str = _APP) -> str:
    return derive_v2_table_identity(app, logical, provider=provider_for(app)).table


async def _seed(install_dal: Any, manifest: dict) -> None:
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id=manifest["app_id"],
        version=manifest["version"],
        artifact_digest="sha256:" + "a" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=manifest["app_id"],
        version=manifest["version"],
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=manifest,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def pg(bundle_data_pg_conn: Any, bundle_data_pg_url: str) -> Any:
    """The fresh Postgres database with the `bundle_data` platform tables, plus a provisioner."""
    with bundle_data_pg_conn.cursor() as c:
        c.execute("CREATE SCHEMA bundle_data")
        c.execute(
            "CREATE TABLE bundle_data.table_registry (app_id text NOT NULL,"
            " logical_name text NOT NULL, schema_kind text NOT NULL,"
            " physical_table text NOT NULL, columns jsonb NOT NULL,"
            " indexes jsonb NOT NULL DEFAULT '[]'::jsonb,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            " updated_at timestamptz NOT NULL DEFAULT now(),"
            " PRIMARY KEY (app_id, logical_name), UNIQUE (schema_kind, physical_table))"
        )
        c.execute(
            "CREATE TABLE bundle_data.usage (app_id text NOT NULL, tenant_id integer NOT NULL,"
            " community_id integer NOT NULL, row_count bigint NOT NULL DEFAULT 0,"
            " byte_count bigint NOT NULL DEFAULT 0, PRIMARY KEY (app_id, tenant_id, community_id))"
        )
    dsn = bundle_data_pg_url.rsplit("/", 1)[0] + "/" + bundle_data_pg_conn.info.dbname
    return _Pg(bundle_data_pg_conn, BundleDataProvisioner(lambda: psycopg2.connect(dsn)))


class _Pg:
    """Probe helpers over the autocommit admin connection."""

    def __init__(self, conn: Any, provisioner: BundleDataProvisioner) -> None:
        self.conn = conn
        self.provisioner = provisioner

    def tables(self) -> set[str]:
        with self.conn.cursor() as c:
            c.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema IN ('app_core', 'app_community')"
            )
            return {r[0] for r in c.fetchall()}

    def registry(self, app_id: str) -> list[str]:
        with self.conn.cursor() as c:
            c.execute(
                "SELECT logical_name FROM bundle_data.table_registry WHERE app_id = %s ORDER BY 1",
                (app_id,),
            )
            return [r[0] for r in c.fetchall()]

    def columns(self, physical: str) -> set[str]:
        with self.conn.cursor() as c:
            c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (physical,),
            )
            return {r[0] for r in c.fetchall()}


async def _install(install_dal: Any, pg: Any, version: str = "1.0.0", app: str = _APP) -> Any:
    return await install_version_globally(
        install_dal,
        app_id=app,
        version=version,
        installed_by=1,
        data_provisioner=pg.provisioner,
    )


def test_schema_from_manifest_none_without_schema_and_422_on_invalid() -> None:
    assert schema_from_manifest(_manifest(schema=None)) is None
    bad = _manifest()
    bad["data"]["tables"] = ["events"]  # != data.schema.tables
    with pytest.raises(ApiError) as exc:
        schema_from_manifest(bad)
    assert exc.value.status_code == 422


def test_provider_is_derived_from_namespace_not_the_manifest() -> None:
    assert provider_for("waddles.core.x.default") == "builtin"
    assert provider_for(_APP) == "thirdparty"


async def test_approval_provisions_every_declared_table(install_dal: Any, pg: Any) -> None:
    await _seed(install_dal, _manifest())
    row = await _install(install_dal, pg)
    assert row.version == "1.0.0"
    assert {_phys("events"), _phys("subs")} <= pg.tables()
    assert pg.registry(_APP) == ["events", "subs"]


async def test_bundle_without_schema_provisions_nothing_and_never_connects(
    install_dal: Any,
) -> None:
    def _boom() -> Any:
        raise AssertionError("must not connect when there is nothing to provision")

    await _seed(install_dal, _manifest(schema=None))
    row = await install_version_globally(
        install_dal,
        app_id=_APP,
        version="1.0.0",
        installed_by=1,
        data_provisioner=BundleDataProvisioner(_boom),
    )
    assert row.version == "1.0.0"


async def test_provisioning_failure_aborts_approval_and_leaves_nothing(
    install_dal: Any, pg: Any
) -> None:
    await _seed(install_dal, _manifest())
    # An unregistered table already squats on the derived physical name.
    with pg.conn.cursor() as c:
        c.execute(f"CREATE TABLE app_community.{_phys('subs')} (x int)")
    with pytest.raises(ApiError) as exc:
        await _install(install_dal, pg)
    assert exc.value.code == "bundle_data_ownership_conflict"
    assert exc.value.status_code == 409
    # migrator transaction rolled back: no registry rows, `events` not created
    assert pg.registry(_APP) == []
    assert _phys("events") not in pg.tables()
    # and the approval itself was not recorded
    assert (
        await install_dal(install_dal.app_global_installs.app_id == _APP).select()
    ).first() is None


async def test_unreachable_migrator_aborts_approval_loudly(install_dal: Any) -> None:
    def _down() -> Any:
        raise psycopg2.OperationalError("connection refused")

    await _seed(install_dal, _manifest())
    with pytest.raises(ApiError) as exc:
        await install_version_globally(
            install_dal,
            app_id=_APP,
            version="1.0.0",
            installed_by=1,
            data_provisioner=BundleDataProvisioner(_down),
        )
    assert exc.value.code == "bundle_data_provisioning_unavailable"
    assert (
        await install_dal(install_dal.app_global_installs.app_id == _APP).select()
    ).first() is None


async def test_unset_migrator_password_aborts_approval(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BUNDLE_MIGRATOR_PASSWORD", raising=False)
    await _seed(install_dal, _manifest())
    with pytest.raises(ApiError) as exc:
        await install_version_globally(install_dal, app_id=_APP, version="1.0.0", installed_by=1)
    assert exc.value.code == "bundle_data_provisioning_unavailable"
    assert (
        await install_dal(install_dal.app_global_installs.app_id == _APP).select()
    ).first() is None


async def test_upgrade_migrates_additively(install_dal: Any, pg: Any) -> None:
    await _seed(install_dal, _manifest())
    await _install(install_dal, pg)
    v2_schema = copy.deepcopy(_RAW_SCHEMA)
    v2_schema["tables"][0]["columns"].append(
        {"name": "note", "type": "text(100)", "nullable": True}
    )
    await _seed(install_dal, _manifest(version="1.1.0", schema=v2_schema))
    await _install(install_dal, pg, version="1.1.0")
    assert "note" in pg.columns(_phys("events"))


async def test_destructive_upgrade_is_refused_and_not_recorded(install_dal: Any, pg: Any) -> None:
    await _seed(install_dal, _manifest())
    await _install(install_dal, pg)
    dropped = copy.deepcopy(_RAW_SCHEMA)
    dropped["tables"] = dropped["tables"][:1]
    manifest = _manifest(version="1.1.0", schema=dropped)
    manifest["data"]["tables"] = ["events"]
    await _seed(install_dal, manifest)
    with pytest.raises(ApiError) as exc:
        await _install(install_dal, pg, version="1.1.0")
    assert exc.value.code == "bundle_data_migration_refused"
    assert pg.registry(_APP) == ["events", "subs"]
    live = (
        await install_dal(
            (install_dal.app_global_installs.app_id == _APP)
            & (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
        ).select()
    ).first()
    assert live.version == "1.0.0"


async def test_uninstall_tears_down_only_owned_tables(install_dal: Any, pg: Any) -> None:
    await _seed(install_dal, _manifest())
    await _install(install_dal, pg)
    other = _manifest(app_id=_OTHER_APP)
    await _seed(install_dal, other)
    await _install(install_dal, pg, app=_OTHER_APP)
    with pg.conn.cursor() as c:
        c.execute("CREATE TABLE app_community.unrelated_bystander (x int)")

    await uninstall_globally(
        install_dal, app_id=_APP, revoked_by=1, data_provisioner=pg.provisioner
    )

    tables = pg.tables()
    assert _phys("events") not in tables
    assert _phys("subs") not in tables
    assert pg.registry(_APP) == []
    assert {_phys("events", _OTHER_APP), _phys("subs", _OTHER_APP)} <= tables
    assert "unrelated_bystander" in tables
    assert pg.registry(_OTHER_APP) == ["events", "subs"]


async def test_failed_teardown_aborts_uninstall(install_dal: Any, pg: Any) -> None:
    await _seed(install_dal, _manifest())
    await _install(install_dal, pg)
    # Tamper: the registry row no longer maps to the derived physical name.
    with pg.conn.cursor() as c:
        c.execute(
            "UPDATE bundle_data.table_registry SET physical_table = 'tampered'"
            " WHERE app_id = %s AND logical_name = 'subs'",
            (_APP,),
        )
    with pytest.raises(ApiError) as exc:
        await uninstall_globally(
            install_dal, app_id=_APP, revoked_by=1, data_provisioner=pg.provisioner
        )
    assert exc.value.code == "bundle_data_ownership_conflict"
    assert {_phys("events"), _phys("subs")} <= pg.tables()  # nothing dropped
    live = (
        await install_dal(
            (install_dal.app_global_installs.app_id == _APP)
            & (install_dal.app_global_installs.revoked_at == None)  # noqa: E711
        ).select()
    ).first()
    assert live is not None  # still installed
