"""
App Bundle 3-tier persistence models
======================================

pydal table definitions for the App Bundle SDK's install -> available ->
activate lifecycle (``docs/plans/2026-08-31-app-bundle-sdk-design.md`` §5.1,
Phase C3). Schema is created by
``config/postgres/migrations/069_app_bundle_tiers.sql`` -- the source of
truth; every ``define_table`` call here passes ``migrate=False``, matching
the established convention for tables whose DDL already exists (see
``core/video_proxy_module/services/database.py``, ``core/engagement_module/
app.py``, ``action/pushing/*_action_module``'s "table already exists"
comments) -- pydal maps onto the already-migrated table, it never owns this
DDL.

Three tiers, narrowing global -> tenant -> community, with the hard
**subset** invariant ``activated ⊆ available ⊆ installed`` -- enforced at
the application layer, at write time
(:mod:`flask_core.app_installations_db`), not expressible as a single SQL
constraint across three tables (design doc §5.1).

Define-order dependency: ``app_tenant_availability``/``app_activations``
declare ``reference tenants`` / ``reference communities`` fields, so
``init_app_bundle_tables`` must run on a ``dal`` that already has
``tenants`` and ``communities`` defined -- same ordering pydal itself
requires for any FK, and the same pattern used by ``auth.py``'s
``init_auth`` (``auth_user`` before ``auth_user_roles``) and the
``tenants -> communities -> posts`` fixture ordering in ``test_tenancy.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def init_app_bundle_tables(dal: Any) -> None:
    """
    Define ``app_catalog`` / ``app_tenant_availability`` / ``app_activations``
    on ``dal`` (an :class:`~flask_core.database.AsyncDAL` or a raw
    ``pydal.DAL``). Call once per process during service startup, alongside
    ``init_auth`` and other ``init_*`` table registrations -- after
    ``tenants``/``communities`` are already defined on the same ``dal``.
    """
    dal.define_table(
        "app_catalog",
        dal.Field("app_id", "string", notnull=True),  # waddles.<module>.<feature>.<app>
        dal.Field("manifest_version", "string", notnull=True),
        dal.Field("module", "string", notnull=True),
        dal.Field("feature", "string", notnull=True),
        dal.Field("provider", "string", notnull=True),  # builtin | thirdparty
        dal.Field("execution_model", "string", notnull=True),  # native | thirdparty
        dal.Field("is_default", "boolean", default=False),
        dal.Field("compatible_with", "list:string", default=[]),
        dal.Field("incompatible_with", "list:string", default=[]),
        dal.Field("platform_compatibility", "json", notnull=True),
        dal.Field("status", "string", default="active"),  # active | deprecated | yanked
        # Per-stage {entrypoint, config, spec} JSON, keyed by ingest/process/
        # action (migration 071) -- this field was missing from this binding
        # until the svc-action bundle-runtime proof (docs/plans/...-app-
        # bundle-discord-action-proof.md), a confirmed gap: hub_api/services/
        # schema.py's own separate app_catalog binding already carried this
        # column (used by the distribution endpoint), but nothing on this
        # binding's side -- the one svc-action's ActionConfigLookup actually
        # queries against -- could ever read app_catalog.stages until now.
        # default={} so a pre-071 row reads back as an empty dict, never None.
        dal.Field("stages", "json", default={}),
        dal.Field("installed_at", "datetime", default=datetime.utcnow),
        # migration 0026 -- attribution/marketplace metadata, kept in
        # lockstep with `hub_api/services/schema.py`'s own two `app_catalog`
        # bindings (see that module's own comment above these fields).
        dal.Field("author", "string"),
        dal.Field("license", "string"),
        dal.Field("license_review_required", "boolean", default=False),
        dal.Field("source_url", "string"),
        dal.Field("alternative_to", "list:string", default=[]),
        dal.Field("homepage_url", "string"),
        dal.Field("notice", "string"),
        dal.Field("category", "string"),
        primarykey=["app_id"],
        migrate=False,
    )

    dal.define_table(
        "app_tenant_availability",
        dal.Field("tenant_id", "reference tenants", notnull=True),
        dal.Field("app_id", "reference app_catalog.app_id", notnull=True),
        dal.Field("available", "boolean", default=True),
        dal.Field("config_defaults", "json", default={}),
        migrate=False,
    )

    dal.define_table(
        "app_activations",
        dal.Field("community_id", "reference communities", notnull=True, ondelete="CASCADE"),
        dal.Field("tenant_id", "reference tenants", notnull=True),  # denormalized, ACL/stream scoping
        dal.Field("app_id", "reference app_catalog.app_id", notnull=True),
        dal.Field("enabled", "boolean", default=True),
        dal.Field("config", "json", default={}),
        dal.Field("activated_by", "integer"),
        dal.Field("activated_at", "datetime", default=datetime.utcnow),
        dal.Field("updated_at", "datetime", default=datetime.utcnow, update=datetime.utcnow),
        migrate=False,
    )
