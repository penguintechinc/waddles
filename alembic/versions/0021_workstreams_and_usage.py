"""workstreams (spec Sec6.11, D30) + workstream_usage_hourly (spec Sec6.12, D31).

`workstreams` is 1:1 with `ingest_sources`, backfilled here so every
source that existed before this migration has a `workstream_id` the
moment D30 code ships (spec Sec15.4's literal requirement). Created
going forward by `services/workstream_service.py` inside
`services/ingest_source_service.py::create_source()`; disabled (never
deleted) when the owning source is removed.

**Why `ingest_source_id` is a `bigint` FK, not the spec's literal
`source_id text` FK.** Spec Sec6.11 writes `source_id | text | FK
intake_sources(source_id), UNIQUE`. In this schema, `ingest_sources.
source_id` is unique only per `(tenant_id, platform, source_id)` (a
three-column UNIQUE, migration 0020), not globally -- two different
tenants can register the same `source_id` string on different
platforms. A workstream's one-to-one FK therefore targets
`ingest_sources.id` (the real, globally-unique surrogate key), not the
non-unique `source_id` column the spec's shorthand assumes. `platform`/
`source_id` are denormalized onto `workstreams` anyway (read
convenience for config/consent views), so nothing the spec's readers
actually need is lost. `ingest_source_id` is nullable with `ON DELETE
SET NULL`, never `ON DELETE CASCADE`: `workstream_usage_hourly` rows
keep their `workstream_id` FK target for the life of the tenant's usage
history even after the owning source is deleted -- spec Sec6.11 says "a
disabled workstream mints nothing further," not that the row is
removed, and a cascade delete would orphan every usage row's FK.

`workstream_usage_hourly` is written by hub-api's usage aggregator only
(`services/usage_aggregator_service.py`) -- SELECT/INSERT, no
UPDATE/DELETE for anyone, including hub_api (spec Sec5.12: "corrections
are new rows for the same key, summed at query time, never an UPDATE of
a settled hour"). Grants are rendered from
config/postgres/rbac-matrix.yaml, same generator migration 0020 used.

Revision ID: 0021_workstreams_and_usage
Revises: 0020_ingest_sources_rbac
Create Date: 2026-09-22
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0021_workstreams_and_usage"
down_revision = "0020_ingest_sources_rbac"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
)
_MATRIX_TABLES = frozenset({"workstreams", "workstream_usage_hourly"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0021", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Register in sys.modules BEFORE exec_module() -- see migration
    # 0020's identical comment for why an unregistered module crashes
    # rbac_matrix.py's own @dataclass class creation.
    sys.modules["waddles_rbac_matrix_0021"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workstreams (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            ingest_source_id BIGINT UNIQUE REFERENCES ingest_sources(id) ON DELETE SET NULL,
            platform VARCHAR(50) NOT NULL,
            source_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            disabled_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE workstreams IS "
        "'hub-api-owned, 1:1 with ingest_sources (spec Sec6.11, D30). "
        "ingest_source_id is nullable/ON DELETE SET NULL so usage history "
        "in workstream_usage_hourly outlives a deleted source.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workstreams_tenant "
        "ON workstreams (tenant_id, community_id) WHERE disabled_at IS NULL"
    )

    op.execute(
        """
        INSERT INTO workstreams
            (tenant_id, community_id, ingest_source_id, platform, source_id, created_at)
        SELECT s.tenant_id, s.community_id, s.id, s.platform, s.source_id, s.created_at
        FROM ingest_sources s
        WHERE NOT EXISTS (
            SELECT 1 FROM workstreams w WHERE w.ingest_source_id = s.id
        )
        """
    )
    op.execute(
        "COMMENT ON COLUMN workstreams.ingest_source_id IS "
        "'Backfilled 1:1 from every pre-existing ingest_sources row (spec Sec15.4); "
        "NULL only after the owning source has been deleted.'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workstream_usage_hourly (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER REFERENCES communities(id),
            workstream_id UUID NOT NULL REFERENCES workstreams(id),
            stage VARCHAR(20) NOT NULL
                CHECK (stage IN ('ingest', 'process', 'action', 'streaming')),
            app_id VARCHAR(255) REFERENCES app_catalog(app_id),
            hour TIMESTAMPTZ NOT NULL,
            events BIGINT NOT NULL DEFAULT 0,
            invocations BIGINT NOT NULL DEFAULT 0,
            host_calls BIGINT NOT NULL DEFAULT 0,
            actions_delivered BIGINT NOT NULL DEFAULT 0,
            fuel_ms BIGINT NOT NULL DEFAULT 0,
            outbound_bytes BIGINT NOT NULL DEFAULT 0,
            media_minutes NUMERIC,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE workstream_usage_hourly IS "
        "'Append-only (spec Sec6.12, D31) -- no UNIQUE on the natural key, "
        "a correction is a new row, summed at query time. hub_api has "
        "SELECT/INSERT only, never UPDATE/DELETE.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workstream_usage_hourly_lookup "
        "ON workstream_usage_hourly (tenant_id, community_id, workstream_id, hour)"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get(
        "RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH)
    )
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute("GRANT USAGE ON SEQUENCE workstream_usage_hourly_id_seq TO hub_api;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workstream_usage_hourly")
    op.execute("DROP TABLE IF EXISTS workstreams")
