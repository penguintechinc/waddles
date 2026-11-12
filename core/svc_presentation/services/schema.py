"""pydal table bindings for svc-presentation's own tables.

svc-presentation owns two tables -- `overlay_surfaces` and
`presentation_config` -- physically created by
`config/postgres/migrations/073_svc_presentation_overlays.sql` in the same
shared Postgres instance every other stage-runner writes to
(`backend-database.md` Per-Service Database Accounts: same DB, per-service
scoped credentials -- this service's own `DATABASE_URL`/`DB_USER`).
`bind_presentation_tables()` mirrors `hub_api/services/schema.py`'s
`bind_*_tables()` shape exactly: idempotent per-DAL-instance (checked via
`"presentation_config" in dal.tables`), `migrate=False` in production (the
migration file is schema's single source of truth -- `backend-database.md`
rule 9, "NO automatic Alembic/pydal migrations on startup"), `migrate=True`
in tests against a throwaway sqlite DB.
"""

from __future__ import annotations

from typing import Any

from pydal import Field


def bind_presentation_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define `overlay_surfaces` and `presentation_config` on `dal`.

    Idempotent: a second call against the same DAL instance is a no-op
    (guarded on `presentation_config`, a table unique to this service --
    same pattern/rationale `hub_api`'s `bind_streaming_tables()` documents
    for why it guards on a group-unique table, not a shared one).
    """
    if "presentation_config" in dal.tables:
        return

    dal.define_table(
        "overlay_surfaces",
        Field("community_id", "integer", notnull=True),
        Field("surface", "string", length=50, notnull=True),
        Field("enabled", "boolean", default=True),
        Field("canvas_resolution", "string", length=8, default="1080p"),
        Field("canvas_width", "integer", default=1920),
        Field("canvas_height", "integer", default=1080),
        Field("config", "json"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "presentation_config",
        Field("community_id", "integer", notnull=True, unique=True),
        Field("theme", "string", length=50, default="default"),
        Field("primary_color", "string", length=20),
        Field("secondary_color", "string", length=20),
        Field("font_family", "string", length=100),
        Field("music_enabled", "boolean", default=True),
        Field("crawler_speed_seconds", "integer", default=30),
        Field("canvas_resolution", "string", length=8, default="1080p"),
        Field("canvas_width", "integer", default=1920),
        Field("canvas_height", "integer", default=1080),
        Field("config", "json"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )
