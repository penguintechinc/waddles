"""Real-Postgres tests for 0113_overlay_scene_components.

Proves the two overlay tables and the canvas column land, CHECK constraints reject out-of-range
geometry / unknown int codes, widget registration is unique per community, rows cascade with the
community, and the merged chain is single-headed. A SKIP means docker is absent -- treat as FAILURE.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)
_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0113)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0113-overlay") as database:
        yield database


def _engine(db: PgTestDatabase) -> sqlalchemy.Engine:
    """SQLAlchemy engine for the test database."""
    return sqlalchemy.create_engine(
        db.dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
    )


def _community(conn: sqlalchemy.Connection, name: str) -> tuple[int, int]:
    """Insert a minimal community under the first tenant; returns (community_id, tenant_id)."""
    tenant = int(
        conn.execute(
            sqlalchemy.text("SELECT id FROM tenants ORDER BY id LIMIT 1")
        ).scalar_one()
    )
    cid = int(
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO communities (name, tenant_id) VALUES (:n, :t) RETURNING id"
            ),
            {"n": name, "t": tenant},
        ).scalar_one()
    )
    return cid, tenant


def test_chain_is_single_headed() -> None:
    """The merge revision leaves exactly one alembic head."""
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == [
        "0113_overlay_scene_components"
    ]


@requires_docker
def test_registry_unique_per_community_and_cascade(db: PgTestDatabase) -> None:
    """A widget registers once per community and its rows die with the community."""
    engine = _engine(db)
    insert = sqlalchemy.text(
        "INSERT INTO overlay_widget_registry (tenant_id, community_id, bundle_app_id, widget_id, "
        "display_name) VALUES (:t, :c, 'a.b', 'w.one', 'One')"
    )
    with engine.begin() as conn:
        cid, tid = _community(conn, "ovl-unique")
        conn.execute(insert, {"t": tid, "c": cid})
    # The same widget_id from a DIFFERENT bundle is a distinct widget, not a duplicate.
    with engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO overlay_widget_registry (tenant_id, community_id, bundle_app_id, "
                "widget_id, display_name) VALUES (:t, :c, 'c.d', 'w.one', 'One')"
            ),
            {"t": tid, "c": cid},
        )
    with pytest.raises(sqlalchemy.exc.IntegrityError), engine.begin() as conn:
        conn.execute(insert, {"t": tid, "c": cid})
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("DELETE FROM communities WHERE id=:c"), {"c": cid})
        left = conn.execute(
            sqlalchemy.text(
                "SELECT count(*) FROM overlay_widget_registry WHERE community_id=:c"
            ),
            {"c": cid},
        ).scalar()
    assert left == 0
    engine.dispose()


@requires_docker
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("x_px", 3841),
        ("y_px", -1),
        ("width_px", 31),
        ("height_px", 2161),
        ("z_index", 1001),
        ("status_code", 3),
        ("output_kind_code", 5),
    ],
)
def test_scene_component_check_constraints(
    db: PgTestDatabase, column: str, value: int
) -> None:
    """Every geometry bound and int-code domain is enforced by the database."""
    engine = _engine(db)
    with engine.begin() as conn:
        cid, tid = _community(conn, f"ovl-chk-{column}")
    with pytest.raises(sqlalchemy.exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                f"INSERT INTO overlay_scene_components (tenant_id, community_id, bundle_app_id, "
                f"widget_id, display_name, {column}) VALUES (:t, :c, 'a.b', 'w', 'W', :v)"
            ),
            {"t": tid, "c": cid, "v": value},
        )
    engine.dispose()


@requires_docker
def test_canvas_code_default_and_check(db: PgTestDatabase) -> None:
    """canvas_resolution_code defaults to 1 (1080p) and rejects unknown codes."""
    engine = _engine(db)
    with engine.begin() as conn:
        cid, _ = _community(conn, "ovl-canvas")
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO presentation_config (community_id) VALUES (:c)"
            ),
            {"c": cid},
        )
        code = conn.execute(
            sqlalchemy.text(
                "SELECT canvas_resolution_code FROM presentation_config WHERE community_id=:c"
            ),
            {"c": cid},
        ).scalar()
    assert code == 1
    with pytest.raises(sqlalchemy.exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "UPDATE presentation_config SET canvas_resolution_code=4 WHERE community_id=:c"
            ),
            {"c": cid},
        )
    engine.dispose()
